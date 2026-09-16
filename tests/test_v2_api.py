from __future__ import annotations

import asyncio
from dataclasses import replace
from http.cookies import SimpleCookie
import json
from urllib.parse import urlsplit
import unittest
from unittest.mock import AsyncMock, patch

import requests

from cursor_dashboard.api.app import create_app
from cursor_dashboard.desktop import DesktopSessionError
from test_core import data
from test_desktop import cookie_for
from test_identity import IdentityFixture, PASSWORD


class APIClient:
    def __init__(self, app):
        self.app, self.cookies, self.csrf = app, {}, ""

    async def request(self, method, url, body=None, *, headers=None, raw=None):
        parsed = urlsplit(url)
        payload = raw if raw is not None else json.dumps(body).encode() if body is not None else b""
        request_headers = {"host": "panel.example.test", "origin": "https://panel.example.test",
            "content-type": "application/json", "cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items()),
            "x-csrf-token": self.csrf, **(headers or {})}
        request_headers = {k: v for k, v in request_headers.items() if v is not None}
        sent, messages = False, []
        completed = asyncio.Event()
        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": payload, "more_body": False}
            await completed.wait()
            return {"type": "http.disconnect"}
        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                completed.set()
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1",
            "method": method, "scheme": "https", "path": parsed.path, "raw_path": parsed.path.encode(),
            "query_string": parsed.query.encode(), "root_path": "",
            "headers": [(k.encode(), v.encode()) for k, v in request_headers.items()],
            "client": ("127.0.0.1", 12345), "server": ("panel.example.test", 443)}
        await asyncio.wait_for(self.app(scope, receive, send), 15)
        start = next(m for m in messages if m["type"] == "http.response.start")
        response_headers = {k.decode(): v.decode() for k, v in start["headers"]}
        for key, value in start["headers"]:
            if key == b"set-cookie":
                cookie = SimpleCookie()
                cookie.load(value.decode())
                for name, morsel in cookie.items():
                    if morsel["max-age"] == "0":
                        self.cookies.pop(name, None)
                    else:
                        self.cookies[name] = morsel.value
        content = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        result = (json.loads(content) if 'application/json' in response_headers.get('content-type', '')
                  else content.decode()) if content else None
        return start["status"], result, response_headers

    async def login(self, login="first@example.test"):
        result = await self.request("POST", "/api/v1/auth/login", {"login": login, "password": PASSWORD})
        if result[0] == 200:
            self.csrf = result[1]["csrf_token"]
        return result


class V2HTTPTest(IdentityFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.core.config = replace(self.config, mode="server")
        self.app = create_app(self.core, public_origin="https://panel.example.test")
        self.client = APIClient(self.app)
        self.account_row = self.account(snapshot=data(42))
        self.path = f"/api/v1/workspaces/{self.first}/accounts"

    async def test_cookie_errors_are_422_and_provider_errors_remain_sanitized_502(self):
        await self.client.login()
        cases = [(cookie_for(sub='google-oauth2|user_other'), 'cookie_account_mismatch'),
                 (cookie_for(exp=1), 'invalid_session_cookie'), ('synthetic-invalid-cookie', 'invalid_session_cookie')]
        with patch.object(self.core.accounts, 'gateway', new_callable=AsyncMock) as gateway:
            for cookie, code in cases:
                with self.subTest(code=code):
                    status, body, _ = await self.client.request('POST', self.path, {'cookie': cookie})
                    self.assertEqual(status, 422)
                    self.assertEqual(body['code'], code)
                    self.assertNotIn(cookie, json.dumps(body))
            gateway.assert_not_awaited()
            for error in (requests.Timeout('synthetic-upstream-secret'), DesktopSessionError('synthetic-upstream-secret')):
                gateway.side_effect = error
                status, body, _ = await self.client.request('POST', self.path, {'cookie': cookie_for()})
                self.assertEqual(status, 502)
                self.assertNotIn('synthetic-upstream-secret', json.dumps(body))
                self.assertNotIn('code', body)

    async def test_quota_reference_edit_is_scoped_validated_and_preserves_freshness(self):
        account = self.account(email='reference@example.test', marker='reference', snapshot=data())
        path = f'{self.path}/{account.ref.account_id}'
        await self.client.login()
        reference = {'cycle_start': data()['cycle']['start'], 'cursor_models': 450, 'other_models': 45, 'overall': 495}
        for invalid in [0, -1, True, '450', 1e10]:
            self.assertEqual((await self.client.request('PATCH', path, {'quota_reference': {**reference, 'overall': invalid}}))[0], 422)
        self.assertEqual((await self.client.request('PATCH', path, {'quota_reference': {**reference, 'overall': 400}}))[0], 409)
        self.assertEqual((await self.client.request('PATCH', path, {'quota_reference': {**reference, 'cycle_start': '2026-10-01T00:00:00Z'}}))[0], 409)
        before = (await self.client.request('GET', path))[1]
        status, result, _ = await self.client.request('PATCH', path, {'quota_reference': reference})
        self.assertEqual(status, 200)
        self.assertEqual(result['ok_at'], before['ok_at'])
        self.assertEqual(result['data']['quota']['overall']['limit_source'], 'reference')
        viewer = self.join('reference-viewer@example.test', 'viewer')
        other = APIClient(self.app)
        self.assertEqual((await other.login('reference-viewer@example.test'))[0], 200)
        self.assertEqual((await other.request('PATCH', path, {'quota_reference': reference}))[0], 404)
        self.spaces.set_grant(self.actor, self.first, account.ref.account_id, viewer.user_id, 'view')
        self.assertEqual((await other.request('PATCH', path, {'quota_reference': reference}))[0], 403)
        status, result, _ = await self.client.request('PATCH', path, {'quota_reference': {'cycle_start': reference['cycle_start']}})
        self.assertEqual(status, 200)
        self.assertEqual(result['data']['quota']['overall']['limit_usd'], 42)
        self.assertEqual(result['data']['quota']['overall']['limit_source'], 'plan')
        own = next(row for row in self.repo.list_views(self.actor, self.first) if row['id'] == account.ref.account_id)
        self.assertIsNone(own['data']['quota']['overall']['limit_usd'])

    async def test_bootstrap_login_cookie_csrf_logout_and_legacy_isolation(self):
        status, info, _ = await self.client.request("GET", "/api/v1/bootstrap", headers={"origin": None})
        self.assertEqual(status, 200)
        self.assertFalse(info["capabilities"]["remote_switch"])
        for headers in ({}, {"authorization": "Bearer old-panel-token"}, {"x-admin-token": "old-admin"},
                        {"cookie": "admin_session=old; panel_token=old"}):
            self.assertEqual((await self.client.request("GET", self.path, headers=headers))[0], 401)
        status, body, headers = await self.client.login()
        self.assertEqual(status, 200, body)
        for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/"):
            self.assertIn(flag, headers["set-cookie"])
        self.assertIn("__Host-cursor_session=", headers["set-cookie"])
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertNotIn("token", (await self.client.request("GET", "/api/v1/me"))[1])
        status, body, _ = await self.client.request("GET", "/api/v1/auth/csrf")
        self.assertEqual(body["csrf_token"], self.client.csrf)
        self.assertEqual((await self.client.request("POST", "/api/v1/auth/logout", headers={"x-csrf-token": None}))[0], 403)
        self.assertEqual((await self.client.request("POST", "/api/v1/auth/logout"))[0], 204)
        self.assertEqual((await self.client.request("GET", "/api/v1/me"))[0], 401)
        for path in ("/api/admin/login", "/api/accounts", "/admin", "/api/v1/auth/register", "/api/v1/setup"):
            self.assertEqual((await self.client.request("POST", path, {"password": PASSWORD}))[0], 404)

    async def test_origin_host_request_body_and_validation_do_not_echo_secrets(self):
        for headers in ({"origin": "https://evil.test"}, {"origin": "null"}, {"origin": None},
                        {"sec-fetch-site": "cross-site"}, {"host": "evil.test"}):
            status, _, _ = await self.client.request("POST", "/api/v1/auth/login",
                {"login": "first@example.test", "password": PASSWORD}, headers=headers)
            self.assertIn(status, {400, 403})
        await self.client.login()
        secret = "DO-NOT-ECHO-SYNTHETIC-CREDENTIAL"
        for body in ({"cookie": secret, "role": "owner", "user_id": self.actor.user_id},
                     {"cookie": secret, "tags": [123]}, {"cookie": {"secret": secret}}):
            status, result, _ = await self.client.request("POST", self.path, body)
            self.assertEqual(status, 422)
            self.assertNotIn(secret, json.dumps(result))
        status, result, _ = await self.client.request("POST", self.path, raw=secret.encode())
        self.assertEqual(status, 422)
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual((await self.client.request("POST", self.path, raw=b"x" * 65537))[0], 413)
        self.assertEqual((await self.client.request("POST", self.path, {}, headers={"content-length": "999999"}))[0], 413)

    async def test_two_user_invitation_grant_flow_and_statistics(self):
        await self.client.login()
        status, invitation, _ = await self.client.request("POST", f"/api/v1/workspaces/{self.first}/invitations",
            {"login": "member@example.test", "role": "member"})
        self.assertEqual(status, 201)
        member_client = APIClient(self.app)
        status, accepted, _ = await member_client.request("POST", "/api/v1/invitations/accept",
            {"token": invitation["token"], "password": PASSWORD})
        self.assertEqual(status, 200, accepted)
        self.assertEqual((await member_client.login("member@example.test"))[0], 200)
        status, result, _ = await member_client.request("GET", self.path)
        self.assertEqual(result["total"], 0)
        account_path = f"{self.path}/{self.account_row.ref.account_id}"
        self.assertEqual((await member_client.request("GET", account_path))[0], 404)
        grant_path = f"{account_path}/grants/{accepted['user_id']}"
        self.assertEqual((await self.client.request("PUT", grant_path, {"level": "view"}))[0], 204)
        status, result, _ = await member_client.request("GET", self.path)
        self.assertEqual(result["total"], 1)
        self.assertFalse(result["items"][0]["capabilities"]["refresh"])
        self.assertEqual((await member_client.request("POST", account_path + "/refresh"))[0], 403)
        self.assertEqual((await member_client.request("PATCH", account_path, {"tags": ["bad"]}))[0], 403)
        for path in (self.path, account_path + "/authorization"):
            self.assertEqual((await member_client.request("POST", path, {"cookie": "blocked-before-provider"}))[0], 403)
        guessed = f"/api/v1/workspaces/{self.second}/accounts/{self.account_row.ref.account_id}"
        self.assertEqual((await member_client.request("GET", guessed))[0], 404)
        self.assertEqual((await self.client.request("GET", guessed))[0], 404)
        self.assertEqual((await self.client.request("DELETE", grant_path))[0], 204)
        self.assertEqual((await member_client.request("GET", account_path))[0], 404)
        serialized = json.dumps(result)
        for secret in (self.account_row.secrets.cookie, self.account_row.secrets.access_token, self.account_row.secrets.refresh_token):
            self.assertNotIn(secret, serialized)

    async def test_live_role_changes_and_instance_disable_take_effect(self):
        member = self.join()
        await self.client.login()
        member_client = APIClient(self.app)
        await member_client.login("member@example.test")
        account_path = f"{self.path}/{self.account_row.ref.account_id}"
        grant_path = f"{account_path}/grants/{member.user_id}"
        self.assertEqual((await self.client.request("PUT", grant_path, {"level": "use"}))[0], 204)
        self.assertTrue((await member_client.request("GET", account_path))[1]["capabilities"]["switch"])
        self.assertEqual((await self.client.request("PUT", f"/api/v1/workspaces/{self.first}/members/{member.user_id}",
                        {"role": "viewer"}))[0], 204)
        self.assertFalse((await member_client.request("GET", account_path))[1]["capabilities"]["switch"])
        self.assertEqual((await member_client.request("GET", "/api/v1/instance/users"))[0], 403)
        self.assertEqual((await self.client.request("PUT", f"/api/v1/instance/users/{member.user_id}", {"active": False}))[0], 204)
        self.assertEqual((await member_client.request("GET", account_path))[0], 401)
        self.assertEqual((await self.client.request("PUT", f"/api/v1/instance/users/{member.user_id}", {"active": True}))[0], 204)
        self.assertEqual((await member_client.request("GET", account_path))[0], 401)

    async def test_account_maintenance_and_audit_request_correlation(self):
        await self.client.login()
        account_path = f"{self.path}/{self.account_row.ref.account_id}"
        status, result, headers = await self.client.request("PATCH", account_path, {"label": "Renamed", "tags": ["team"]})
        self.assertEqual(status, 200)
        self.assertEqual(result["tags"], ["team"])
        self.assertEqual((await self.client.request("GET", self.path + "?q=Renamed&tag=team"))[1]["total"], 1)
        status, events, _ = await self.client.request("GET", f"/api/v1/workspaces/{self.first}/audit")
        event = next(e for e in events if e["action"] == "account.edit")
        self.assertEqual(event["request_id"], headers["x-request-id"])
        self.assertEqual((await self.client.request("DELETE", account_path))[0], 204)
        self.assertEqual((await self.client.request("GET", account_path))[0], 404)
        self.assertEqual((await self.client.request("GET", self.path))[1]["stats"]["accounts"], 0)
        for suffix in ("/switch-tickets", "/credentials"):
            self.assertEqual((await self.client.request("POST", account_path + suffix))[0], 404)

    async def test_password_and_session_routes(self):
        await self.client.login()
        second = APIClient(self.app)
        await second.login()
        status, sessions, _ = await self.client.request("GET", "/api/v1/auth/sessions")
        self.assertEqual(status, 200)
        other = next(s for s in sessions if not s["current"])
        self.assertEqual((await self.client.request("DELETE", f"/api/v1/auth/sessions/{other['id']}"))[0], 204)
        self.assertEqual((await second.request("GET", "/api/v1/me"))[0], 401)
        self.assertEqual((await self.client.request("PUT", "/api/v1/auth/password",
            {"current_password": PASSWORD, "new_password": "Changed password for tests!"}))[0], 204)
        self.assertEqual((await self.client.request("GET", "/api/v1/me"))[0], 401)

    async def test_slow_http_detail_cannot_return_after_session_revoke(self):
        await self.client.login()
        async def fetch(*args):
            logged = self.identity.authenticate(self.client.cookies["__Host-cursor_session"])
            self.identity.revoke_session(logged, logged.session_id)
            return {"aggregations": []}
        self.core.credentials.gateway = fetch
        status, result, _ = await self.client.request("GET", f"{self.path}/{self.account_row.ref.account_id}/detail")
        self.assertEqual(status, 401)
        self.assertNotIn("models", result)
        self.assertEqual(len(self.core.accounts._details), 0)

    async def test_http_login_rate_limit(self):
        for _ in range(8):
            self.assertEqual((await self.client.request("POST", "/api/v1/auth/login",
                {"login": "missing@example.test", "password": "bad"}))[0], 401)
        self.assertEqual((await self.client.request("POST", "/api/v1/auth/login",
            {"login": "missing@example.test", "password": "bad"}))[0], 429)
