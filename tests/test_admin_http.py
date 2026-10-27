from __future__ import annotations

import asyncio
import base64
import json
import shlex
import tempfile
import time
import unittest
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode, urlsplit

import requests

from cursor_dashboard import admin, desktop, server, snapshot, store


@dataclass
class Response:
    status_code: int
    headers: dict[str, str]
    text: str

    def json(self):
        return json.loads(self.text)


class PageMarkup(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.elements = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def credential(marker: str, expires: int) -> desktop.DesktopSession:
    def token(kind: str, expiry: int) -> str:
        claims = {"sub": "auth0|user_test", "type": "session", "exp": expiry,
                  "marker": marker, "kind": kind}
        encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        return f"eyJhbGciOiJIUzI1NiJ9.{encoded}.signature"

    return desktop.DesktopSession(token("access", expires), "auth0|user_test", expires,
                                  token("refresh", expires + 3600), "session")


def browser_fetch(session: desktop.DesktopSession, data: dict):
    """Synthetic Cursor: each login is pending on its first poll and confirmed on the next."""
    polled = set()

    async def fetch(_cookie, _label, name, *args):
        if name == "desktop_poll":
            if args[0] not in polled:
                polled.add(args[0])
                response = requests.Response()
                response.status_code = 404
                raise requests.HTTPError(response=response)
            return {"accessToken": session.token, "refreshToken": session.refresh_token}
        return data[name]

    return fetch


class AdminHTTPTest(unittest.IsolatedAsyncioTestCase):
    password = "test password strong 3bF!"

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for target, name, value in (
            (store, "DATABASE_PATH", self.root / "accounts.db"),
            (store, "LEGACY_ACCOUNTS_PATH", self.root / "missing.json"),
            (server, "PANEL_TOKEN", ""),
            (server, "_scheduler", None),
        ):
            handle = patch.object(target, name, value)
            handle.start()
            self.addCleanup(handle.stop)
        store._initialized.clear()
        snapshot._snapshots.clear()
        snapshot._inflight.clear()
        self.addCleanup(store._initialized.clear)
        self.addCleanup(snapshot._snapshots.clear)
        self.addCleanup(snapshot._inflight.clear)
        server._switch_links.clear()
        self.addCleanup(server._switch_links.clear)
        server._authorizations.clear()
        self.addCleanup(server._authorizations.clear)
        self.cookies: dict[str, str] = {}
        self.csrf = ""
        self.expiry = int(time.time()) + 7200
        self.accounts = [
            store.upsert_account("private-cookie-alpha", "alpha@example.test", "Alpha", "Research",
                                 session=credential("alpha", self.expiry)),
            store.upsert_account("private-cookie-beta", "beta@example.test", "Beta", "Operations",
                                 session=credential("beta", self.expiry)),
            store.upsert_account("private-cookie-special", "special@example.test", "100%_owner", "Research",
                                 session=credential("special", self.expiry)),
            store.upsert_account("private-cookie-legacy", "legacy@example.test", "Legacy", ""),
        ]
        for account in self.accounts:
            snapshot.record_success(store.account_id(account), account["cookie"], {
                "email": account["email"], "quota": {"overall": {"remaining_pct": 50}},
            })
        admin.initialize_admin(self.password)
        outbound = patch.object(server, "fetch_cursor", new_callable=AsyncMock)
        self.outbound = outbound.start()
        self.outbound.side_effect = AssertionError("HTTP tests must not call Cursor")
        self.addCleanup(outbound.stop)

    async def request(self, method: str, url: str, *, data=None, headers=None,
                      authenticated: bool = True) -> Response:
        parsed = urlsplit(url)
        payload = json.dumps(data).encode() if data is not None else b""
        request_headers = {"host": parsed.netloc or "testserver", **(headers or {})}
        if data is not None:
            request_headers["content-type"] = "application/json"
        if authenticated and self.cookies:
            request_headers["cookie"] = "; ".join(f"{key}={value}" for key, value in self.cookies.items())
        sent_request = False
        completed = asyncio.Event()
        messages = []

        async def receive():
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {"type": "http.request", "body": payload, "more_body": False}
            await completed.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                completed.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1", "method": method, "scheme": parsed.scheme or "http",
            "path": parsed.path, "raw_path": parsed.path.encode(),
            "query_string": parsed.query.encode(), "root_path": "",
            "headers": [(key.lower().encode(), value.encode()) for key, value in request_headers.items()],
            "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
        }
        await asyncio.wait_for(server.app(scope, receive, send), timeout=5)
        start = next(message for message in messages if message["type"] == "http.response.start")
        response_headers = {key.decode(): value.decode() for key, value in start["headers"]}
        if authenticated:
            for key, value in start["headers"]:
                if key == b"set-cookie":
                    parsed_cookie = SimpleCookie()
                    parsed_cookie.load(value.decode())
                    for name, morsel in parsed_cookie.items():
                        if morsel["max-age"] == "0":
                            self.cookies.pop(name, None)
                        else:
                            self.cookies[name] = morsel.value
        body = b"".join(message.get("body", b"") for message in messages
                        if message["type"] == "http.response.body")
        return Response(start["status"], response_headers, body.decode())

    async def login(self):
        response = await self.request("POST", "/api/admin/login", data={"password": self.password})
        self.assertEqual(response.status_code, 200, response.text)
        self.csrf = response.json()["csrf_token"]
        return response

    async def authorize(self, *, data, headers=None, authenticated=True) -> Response:
        """Start a browser authorization and poll until the synthetic confirmation completes."""
        started = await self.request("POST", "/api/authorizations", data=data, headers=headers,
                                     authenticated=authenticated)
        if started.status_code != 200:
            return started
        key = started.json()["id"]
        for _ in range(3):
            pending = server._authorizations.get(key)
            if pending:
                pending.next_poll = 0
            response = await self.request("POST", f"/api/authorizations/{key}/poll", headers=headers,
                                          authenticated=authenticated)
            if response.status_code != 200 or response.json()["status"] == "completed":
                return response
        self.fail("Browser authorization did not complete")

    async def save_policy(self, *, all_accounts=False, departments=None, account_ids=None):
        return await self.request("PUT", "/api/admin/switch-policy", headers={"X-Admin-CSRF": self.csrf},
                                  data={"all_accounts": all_accounts, "departments": departments or [],
                                        "account_ids": account_ids or []})

    async def make_switch_link(self, *, authenticated=True, base_url=""):
        account = self.accounts[0]
        with patch.object(server.sessions, "ensure_account", return_value=account), \
             patch.object(server.sessions, "request_account",
                          return_value={"email": account["email"], "authId": account["auth_subject"]}), \
             patch.object(server, "take_manual_token", return_value=True):
            response = await self.request("POST", base_url + "/api/accounts/alpha@example.test/switch-command",
                headers={"X-Admin-CSRF": self.csrf}, authenticated=authenticated)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        # shlex extracts the URL without executing any generated script.
        url = next(part for part in shlex.split(result["commands"]["macos"]["command"].split(")", 1)[0])
                   if part.startswith(("http://", "https://")))
        return result, url

    async def test_short_link_download_is_exact_single_use_and_needs_no_browser_auth(self):
        await self.login()
        with patch.object(server, "PANEL_TOKEN", "private-panel-token"):
            result, url = await self.make_switch_link()
            for item in result["commands"].values():
                self.assertLess(len(item["command"]), 400)
                self.assertNotIn(self.accounts[0]["access_token"], item["command"])
                self.assertNotIn("private-panel-token", item["command"])
            self.assertLessEqual(result["download_expires_at"], time.time() + 300)
            self.assertLessEqual(result["download_expires_at"], result["expires_at"])
            head = await self.request("HEAD", url, authenticated=False)
            self.assertEqual(head.status_code, 405)
            bad = await self.request("GET", url.rsplit("/", 1)[0] + "/linux", authenticated=False)
            self.assertEqual(bad.status_code, 410)
            download = await self.request("GET", url, authenticated=False)
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.text, result["commands"]["macos"]["script"])
            self.assertEqual(download.headers["cache-control"], "no-store")
            self.assertIn("charset=utf-8", download.headers["content-type"])
            for path in (url, url.rsplit("/", 1)[0] + "/windows"):
                repeat = await self.request("GET", path, authenticated=False)
                self.assertEqual(repeat.status_code, 410)
                self.assert_no_credentials(repeat)

    async def test_windows_download_matches_preview_and_consumes_mac_link(self):
        await self.login()
        result, url = await self.make_switch_link()
        download = await self.request("GET", url.rsplit("/", 1)[0] + "/windows", authenticated=False)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.text, result["commands"]["windows"]["script"])
        self.assertEqual((await self.request("GET", url, authenticated=False)).status_code, 410)

    async def test_concurrent_downloads_only_deliver_once(self):
        await self.login()
        _, url = await self.make_switch_link()
        responses = await asyncio.gather(*(self.request("GET", url, authenticated=False) for _ in range(4)))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 410, 410, 410])

    async def test_expired_and_restart_cleared_links_return_no_credentials(self):
        await self.login()
        result, url = await self.make_switch_link()
        with patch("cursor_dashboard.switch_links.time.time", return_value=result["download_expires_at"]):
            expired = await self.request("GET", url, authenticated=False)
        self.assertEqual(expired.status_code, 410)
        self.assert_no_credentials(expired)
        _, url = await self.make_switch_link()
        server._switch_links.clear()
        cleared = await self.request("GET", url, authenticated=False)
        self.assertEqual(cleared.status_code, 410)

    async def test_download_rechecks_guest_policy_and_admin_session(self):
        await self.login()
        await self.save_policy(all_accounts=True)
        _, guest_url = await self.make_switch_link(authenticated=False)
        await self.save_policy()
        denied = await self.request("GET", guest_url, authenticated=False)
        self.assertEqual(denied.status_code, 403)
        self.assert_no_credentials(denied)
        _, admin_url = await self.make_switch_link()
        await self.save_policy(all_accounts=True)
        await self.request("POST", "/api/admin/logout", headers={"X-Admin-CSRF": self.csrf})
        denied = await self.request("GET", admin_url, authenticated=False)
        self.assertEqual(denied.status_code, 403)
        self.assert_no_credentials(denied)

    async def test_reauthorized_or_recreated_account_invalidates_download(self):
        await self.login()
        _, url = await self.make_switch_link()
        store.upsert_account("new-cookie", "alpha@example.test", "Alpha", "Research",
                             session=credential("new", self.expiry))
        denied = await self.request("GET", url, authenticated=False)
        self.assertEqual(denied.status_code, 410)
        self.assert_no_credentials(denied)
        self.accounts[0] = store.get_account("alpha@example.test")
        _, url = await self.make_switch_link()
        store.delete_account("alpha@example.test")
        store.upsert_account("new-cookie", "alpha@example.test", "Alpha", "Research",
                             session=credential("new", self.expiry))
        denied = await self.request("GET", url, authenticated=False)
        self.assertEqual(denied.status_code, 410)
        self.assert_no_credentials(denied)

    async def test_link_url_uses_external_host_protocol_and_port(self):
        await self.login()
        result, url = await self.make_switch_link(base_url="https://panel.example:9443")
        self.assertTrue(url.startswith("https://panel.example:9443/api/s/"))
        self.assertIn(url.rsplit("/", 1)[0] + "/windows", result["commands"]["windows"]["command"])

    def assert_no_credentials(self, response: Response):
        for account in self.accounts:
            for key in ("cookie", "access_token", "refresh_token"):
                if account[key]:
                    self.assertNotIn(account[key], response.text)

    async def test_admin_routes_share_quota_shell_and_controls(self):
        homepage = await self.request("GET", "/")
        for path in ("/", "/admin", "/admin/"):
            with self.subTest(path=path):
                response = await self.request("GET", path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.text, homepage.text)
                self.assert_no_credentials(response)
                self.assertNotIn("__ADMIN_CONTENT__", response.text)
                self.assertNotIn("__ASSET_VERSION__", response.text)
                elements = PageMarkup(response.text).elements
                for tag in ("html", "head", "body"):
                    self.assertEqual(sum(name == tag for name, _ in elements), 1, tag)
                ids = [attrs["id"] for _, attrs in elements if "id" in attrs]
                self.assertEqual(len(ids), len(set(ids)), "Merged shell must not duplicate element IDs")
                for element_id in ("sidebar", "admin-menu-link", "admin-workspace", "login-view",
                                   "admin-view", "account-department", "page-size"):
                    self.assertIn(element_id, ids)
                menu = next(attrs for _, attrs in elements if attrs.get("id") == "admin-menu-link")
                self.assertEqual(menu.get("href"), "#admin")
                self.assertIn("hidden", menu)
                assets = {urlsplit(attrs.get("src") or attrs.get("href") or "").path
                          for tag, attrs in elements if tag in ("script", "link")}
                for asset in ("/static/css/base.css", "/static/css/ui.css", "/static/css/admin.css",
                              "/static/js/ui.js", "/static/js/app.js", "/static/js/admin.js"):
                    self.assertIn(asset, assets)

    async def test_login_cookie_logout_and_replay(self):
        shell = await self.request("GET", "/admin")
        self.assertEqual(shell.status_code, 200)
        self.assert_no_credentials(shell)
        self.assertNotIn(self.accounts[0]["email"], shell.text)
        response = await self.request("GET", "/api/admin/session")
        self.assertFalse(response.json()["authenticated"])
        rejected = await self.request("POST", "/api/admin/login", data={"password": "wrong"})
        self.assertEqual(rejected.status_code, 401)
        self.assertNotIn("cursor_panel_admin", self.cookies)
        accepted = await self.login()
        cookie = accepted.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertGreater(accepted.json()["expires_at"], time.time())
        saved_cookie = self.cookies["cursor_panel_admin"]
        self.assertNotIn(saved_cookie, accepted.text)
        rejected_logout = await self.request("POST", "/api/admin/logout")
        self.assertEqual(rejected_logout.status_code, 403)
        response = await self.request("POST", "/api/admin/logout", headers={"X-Admin-CSRF": self.csrf})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("cursor_panel_admin", self.cookies)
        replay = await self.request("GET", "/api/admin/accounts", authenticated=False,
                                    headers={"cookie": f"cursor_panel_admin={saved_cookie}"})
        self.assertEqual(replay.status_code, 401)

    async def test_login_throttling_and_validation_errors_do_not_echo_secrets(self):
        invalid_password = {"secret": "password-must-not-appear-in-errors"}
        rejected = await self.request("POST", "/api/admin/login", data={"password": invalid_password})
        self.assertEqual(rejected.status_code, 422)
        self.assertNotIn(invalid_password["secret"], rejected.text)
        for _ in range(admin.CLIENT_LOGIN_LIMIT):
            failed = await self.request("POST", "/api/admin/login", data={"password": "wrong"})
            self.assertEqual(failed.status_code, 401)
        throttled = await self.request("POST", "/api/admin/login", data={"password": self.password})
        self.assertEqual(throttled.status_code, 429)
        self.assertGreater(int(throttled.headers["retry-after"]), 0)
        self.assertNotIn(self.password, throttled.text)
        self.assertNotIn("cursor_panel_admin", self.cookies)

    async def test_admin_boundaries_and_panel_token_are_independent(self):
        with patch.object(server, "PANEL_TOKEN", "shared-panel-token"):
            for path in ("/api/admin/accounts", "/api/admin/switch-policy"):
                response = await self.request("GET", path, headers={"X-Panel-Token": "shared-panel-token"})
                self.assertEqual(response.status_code, 401, path)
            await self.login()
            response = await self.request("GET", "/api/admin/accounts")
            self.assertEqual(response.status_code, 200)
            response = await self.request("GET", "/api/accounts")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(all(account["can_switch"] for account in response.json()["accounts"]))

    async def test_expired_admin_session_loses_access_and_switch_privileges(self):
        await self.login()
        conn = store._connect()
        try:
            conn.execute("UPDATE admin_sessions SET expires_at = 1")
            conn.commit()
        finally:
            conn.close()
        session = await self.request("GET", "/api/admin/session")
        self.assertFalse(session.json()["authenticated"])
        protected = await self.request("GET", "/api/admin/accounts")
        self.assertEqual(protected.status_code, 401)
        public = await self.request("GET", "/api/accounts")
        self.assertFalse(any(account["can_switch"] for account in public.json()["accounts"]))
        command = await self.request("POST", "/api/accounts/alpha@example.test/switch-command",
                                     headers={"X-Admin-CSRF": self.csrf})
        self.assertEqual(command.status_code, 403)
        self.outbound.assert_not_called()

    async def test_foreign_origin_and_csrf_cannot_change_policy(self):
        foreign = await self.request("POST", "/api/admin/login", data={"password": self.password},
                                     headers={"Origin": "https://foreign.example"})
        self.assertEqual(foreign.status_code, 403)
        await self.login()
        enabled = {"all_accounts": True, "departments": [], "account_ids": []}
        for headers in ({}, {"X-Admin-CSRF": "incorrect"},
                        {"X-Admin-CSRF": self.csrf, "Origin": "https://foreign.example"},
                        {"X-Admin-CSRF": self.csrf, "Sec-Fetch-Site": "cross-site"}):
            response = await self.request("PUT", "/api/admin/switch-policy", data=enabled, headers=headers)
            self.assertEqual(response.status_code, 403)
        unchanged = await self.request("GET", "/api/admin/switch-policy")
        self.assertFalse(unchanged.json()["policy"]["all_accounts"])
        same_origin = await self.request("PUT", "/api/admin/switch-policy", data=enabled,
                                         headers={"X-Admin-CSRF": self.csrf, "Origin": "http://testserver"})
        self.assertEqual(same_origin.status_code, 200)

    async def test_admin_account_search_pagination_and_safe_metadata(self):
        await self.login()
        first = await self.request("GET", "/api/admin/accounts?page=1&page_size=2")
        self.assertEqual(first.status_code, 200, first.text)
        page = first.json()
        self.assertEqual(page["total"], 4)
        self.assertEqual(page["page"], 1)
        self.assertEqual(len(page["accounts"]), 2)
        first_ids = {account["db_id"] for account in page["accounts"]}
        last = await self.request("GET", "/api/admin/accounts?page=999&page_size=2")
        self.assertEqual(last.json()["page"], 2)
        self.assertEqual(len(last.json()["accounts"]), 2)
        self.assertFalse(first_ids & {account["db_id"] for account in last.json()["accounts"]})
        for query, expected in (("ALPHA", 1), ("beta@example.test", 1), ("Research", 2),
                                ("%_", 1), ("%", 1), ("_", 1), ("' OR 1=1 --", 0)):
            response = await self.request("GET", "/api/admin/accounts?" + urlencode({"q": query}))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["total"], expected, query)
            self.assert_no_credentials(response)
        filtered = await self.request("GET", "/api/admin/accounts?department=Research&q=Alpha")
        row = filtered.json()["accounts"][0]
        self.assertEqual(filtered.json()["total"], 1)
        timestamp = lambda name: datetime.fromisoformat(row["auth"][name]).timestamp()
        self.assertEqual(timestamp("access_expires_at"), self.expiry)
        self.assertEqual(timestamp("refresh_expires_at"), self.expiry + 3600)
        self.assertLess(timestamp("refresh_due_at"), self.expiry)
        self.assertGreater(timestamp("refreshed_at"), 0)
        self.assert_no_credentials(first)
        self.assert_no_credentials(last)
        self.assert_no_credentials(filtered)
        self.assertIn("no-store", first.headers.get("cache-control", ""))

    async def test_admin_pagination_rejects_unbounded_or_invalid_inputs(self):
        await self.login()
        for query in ("page=0", "page=-1", "page_size=0", "page_size=101", "page=oops"):
            response = await self.request("GET", "/api/admin/accounts?" + query)
            self.assertEqual(response.status_code, 422, query)

    async def test_public_views_hide_auth_metadata_and_default_to_no_switch(self):
        for path in ("/api/accounts", "/api/account-index", "/api/accounts/alpha@example.test"):
            response = await self.request("GET", path)
            self.assertEqual(response.status_code, 200)
            self.assert_no_credentials(response)
            payload = response.json()
            accounts = payload["accounts"] if "accounts" in payload else [payload["account"]]
            for account in accounts:
                self.assertFalse(account["can_switch"])
                self.assertNotIn("auth", account)
                self.assertNotIn("token_expires_at", account)

    async def test_department_and_account_grants_are_inclusive_and_revocable(self):
        await self.login()
        saved = await self.save_policy(departments=["Research"], account_ids=[self.accounts[1]["db_id"]])
        self.assertEqual(saved.status_code, 200, saved.text)
        for path in ("/api/accounts", "/api/account-index"):
            response = await self.request("GET", path, authenticated=False)
            permissions = {account["email"]: account["can_switch"] for account in response.json()["accounts"]}
            self.assertEqual(permissions, {"alpha@example.test": True, "beta@example.test": True,
                                          "special@example.test": True, "legacy@example.test": False})
        await self.save_policy(all_accounts=True)
        response = await self.request("GET", "/api/accounts", authenticated=False)
        self.assertTrue(all(account["can_switch"] for account in response.json()["accounts"]))
        await self.save_policy()
        response = await self.request("GET", "/api/accounts", authenticated=False)
        self.assertFalse(any(account["can_switch"] for account in response.json()["accounts"]))
        admin_view = await self.request("GET", "/api/accounts")
        self.assertTrue(all(account["can_switch"] for account in admin_view.json()["accounts"]))

    async def test_unknown_policy_references_do_not_change_existing_grants(self):
        await self.login()
        self.assertEqual((await self.save_policy(departments=["Research"])).status_code, 200)
        for changes in ({"departments": ["missing-department"]}, {"account_ids": [999999]}):
            response = await self.save_policy(**changes)
            self.assertEqual(response.status_code, 400)
        policy = await self.request("GET", "/api/admin/switch-policy")
        self.assertEqual(policy.json()["policy"]["departments"], ["Research"])
        self.assertFalse(policy.json()["policy"]["all_accounts"])

    async def test_denied_switch_stops_before_credentials_or_outbound_work(self):
        with patch.object(server.sessions, "ensure_account", new_callable=AsyncMock) as ensure, \
             patch.object(server, "build_commands") as build, \
             patch.object(server, "take_manual_token") as take:
            response = await self.request("POST", "/api/accounts/alpha@example.test/switch-command")
        self.assertEqual(response.status_code, 403)
        ensure.assert_not_called()
        build.assert_not_called()
        take.assert_not_called()
        self.outbound.assert_not_called()

    async def test_switch_requires_admin_csrf_and_guest_grant(self):
        await self.login()
        account = self.accounts[0]
        fake_result = desktop.build_commands(credential("alpha", self.expiry), account["email"], preview=True)
        with patch.object(server.sessions, "ensure_account", new_callable=AsyncMock, return_value=account), \
             patch.object(server.sessions, "request_account", new_callable=AsyncMock,
                          return_value={"email": account["email"], "authId": account["auth_subject"]}), \
             patch.object(server, "build_commands", return_value=fake_result), \
             patch.object(server, "take_manual_token", return_value=True):
            denied = await self.request("POST", "/api/accounts/alpha@example.test/switch-command")
            self.assertEqual(denied.status_code, 403)
            accepted = await self.request("POST", "/api/accounts/alpha@example.test/switch-command",
                                          headers={"X-Admin-CSRF": self.csrf})
            self.assertEqual(accepted.status_code, 200, accepted.text)
            self.assertIn("no-store", accepted.headers.get("cache-control", ""))
            await self.save_policy(account_ids=[account["db_id"]])
            guest = await self.request("POST", "/api/accounts/alpha@example.test/switch-command", authenticated=False)
            self.assertEqual(guest.status_code, 200)
            foreign = await self.request("POST", "/api/accounts/alpha@example.test/switch-command", authenticated=False,
                                         headers={"Origin": "https://foreign.example"})
            self.assertEqual(foreign.status_code, 403)

    async def test_revocation_during_cursor_request_prevents_command_delivery(self):
        await self.login()
        account = self.accounts[0]
        await self.save_policy(account_ids=[account["db_id"]])

        async def revoke(*_args):
            self.assertEqual((await self.save_policy()).status_code, 200)
            return {"email": account["email"], "authId": account["auth_subject"]}

        with patch.object(server.sessions, "ensure_account", new_callable=AsyncMock, return_value=account), \
             patch.object(server.sessions, "request_account", side_effect=revoke), \
             patch.object(server, "build_commands") as build, \
             patch.object(server, "take_manual_token", return_value=True):
            response = await self.request("POST", "/api/accounts/alpha@example.test/switch-command", authenticated=False)
        self.assertEqual(response.status_code, 403, response.text)
        build.assert_not_called()

    async def test_admin_logout_during_cursor_request_prevents_command_delivery(self):
        account = self.accounts[0]

        async def logout(*_args):
            response = await self.request("POST", "/api/admin/logout", headers={"X-Admin-CSRF": self.csrf})
            self.assertEqual(response.status_code, 200)
            return {"email": account["email"], "authId": account["auth_subject"]}

        for panel_token, public_grant, expected_status in (("", False, 403),
                                                          ("configured-panel-token", True, 401)):
            with self.subTest(panel_token_enabled=bool(panel_token)), \
                 patch.object(server, "PANEL_TOKEN", panel_token), \
                 patch.object(server.sessions, "ensure_account", new_callable=AsyncMock, return_value=account), \
                 patch.object(server.sessions, "request_account", side_effect=logout), \
                 patch.object(server, "build_commands") as build, \
                 patch.object(server, "take_manual_token", return_value=True):
                await self.login()
                self.assertEqual((await self.save_policy(all_accounts=public_grant)).status_code, 200)
                response = await self.request("POST", "/api/accounts/alpha@example.test/switch-command",
                                              headers={"X-Admin-CSRF": self.csrf})
                self.assertEqual(response.status_code, expected_status, response.text)
                build.assert_not_called()

    async def test_guest_department_changes_and_deletion_keep_original_access(self):
        await self.login()
        await self.save_policy(departments=["Research"])
        before = store.get_account("beta@example.test")
        changed = await self.request("PATCH", "/api/accounts/beta@example.test/department",
                                     data={"department": "Research"}, authenticated=False)
        self.assertEqual(changed.status_code, 200)
        saved = store.get_account("beta@example.test")
        self.assertEqual(saved["department"], "Research")
        for key in ("cookie", "access_token", "refresh_token"):
            self.assertEqual(saved[key], before[key])
        row = await self.request("GET", "/api/accounts/beta@example.test", authenticated=False)
        self.assertTrue(row.json()["account"]["can_switch"])

        changed = await self.request("PATCH", "/api/accounts/beta@example.test/department",
                                     data={"department": "Operations"}, authenticated=False)
        self.assertEqual(changed.status_code, 200)
        row = await self.request("GET", "/api/accounts/beta@example.test", authenticated=False)
        self.assertFalse(row.json()["account"]["can_switch"])
        deleted = await self.request("DELETE", "/api/accounts/beta@example.test", authenticated=False)
        self.assertEqual(deleted.status_code, 200)
        self.assertIsNone(store.get_account("beta@example.test"))
        missing = await self.request("GET", "/api/accounts/beta@example.test", authenticated=False)
        self.assertEqual(missing.status_code, 404)
        self.outbound.assert_not_called()

    async def test_card_mutations_still_require_configured_panel_token(self):
        with patch.object(server, "PANEL_TOKEN", "shared-panel-token"):
            before = store.get_account("beta@example.test")
            for method, path, data in (
                ("PATCH", "/api/accounts/beta@example.test/department", {"department": "Research"}),
                ("DELETE", "/api/accounts/beta@example.test", None),
            ):
                for headers in ({}, {"X-Panel-Token": "wrong"}):
                    rejected = await self.request(method, path, data=data, headers=headers, authenticated=False)
                    self.assertEqual(rejected.status_code, 401)
                    self.assertEqual(store.get_account("beta@example.test"), before)
            changed = await self.request("PATCH", "/api/accounts/beta@example.test/department",
                                         data={"department": "Research"}, authenticated=False,
                                         headers={"X-Panel-Token": "shared-panel-token"})
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(store.get_account("beta@example.test")["department"], "Research")
            deleted = await self.request("DELETE", "/api/accounts/beta@example.test", authenticated=False,
                                         headers={"X-Panel-Token": "shared-panel-token"})
            self.assertEqual(deleted.status_code, 200)
            self.assertIsNone(store.get_account("beta@example.test"))
        self.outbound.assert_not_called()

    async def test_guest_enrollment_honors_selected_department(self):
        await self.login()
        await self.save_policy(departments=["Research"])
        session = credential("reenrolled", self.expiry)
        for email in ("beta@example.test", "new@example.test"):
            data = {
                "desktop_me": {"email": email, "authId": session.subject},
                "desktop_plan": {"planInfo": {"planName": "Free"}},
                "desktop_profile": {"membershipType": "free"},
                "desktop_period": {"planUsage": {"totalSpend": 0}},
                "desktop_grok": {"includedLimitZero": True},
                "desktop_limit": {"noUsageBasedAllowed": True},
            }

            fetch = browser_fetch(session, data)
            with patch.object(server, "fetch_cursor", side_effect=fetch):
                response = await self.authorize(authenticated=False, data={"department": "Research"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "completed")
            self.assertEqual(store.get_account(email)["department"], "Research")
            view = await self.request("GET", "/api/accounts/" + email, authenticated=False)
            self.assertTrue(view.json()["account"]["can_switch"])

    async def test_admin_logout_during_enrollment_rechecks_panel_access(self):
        email = "beta@example.test"
        session = credential("logout-enrollment", self.expiry)
        data = {
            "desktop_me": {"email": email, "authId": session.subject},
            "desktop_plan": {"planInfo": {"planName": "Free"}},
            "desktop_profile": {"membershipType": "free"},
            "desktop_period": {"planUsage": {"totalSpend": 0}},
            "desktop_grok": {"includedLimitZero": True},
            "desktop_limit": {"noUsageBasedAllowed": True},
        }

        confirmed = browser_fetch(session, data)

        async def fetch(cookie, label, name, *args):
            if name == "desktop_me" and not logged_out:
                # The admin session ends after the browser confirmed but before the account is saved.
                logged_out.append(await self.request("POST", "/api/admin/logout", headers={"X-Admin-CSRF": self.csrf}))
                self.assertEqual(logged_out[-1].status_code, 200)
            return await confirmed(cookie, label, name, *args)

        for panel_token, expected_status in (("", 200), ("configured-panel-token", 401)):
            logged_out = []
            with self.subTest(panel_token_enabled=bool(panel_token)), \
                 patch.object(server, "PANEL_TOKEN", panel_token), \
                 patch.object(server, "fetch_cursor", side_effect=fetch):
                await self.login()
                before = store.get_account(email)
                response = await self.authorize(headers={"X-Admin-CSRF": self.csrf}, data={"department": "Research"})
                self.assertEqual(response.status_code, expected_status, response.text)
                saved = store.get_account(email)
                if panel_token:
                    self.assertEqual(saved, before)
                else:
                    self.assertEqual(saved["department"], "Research")


if __name__ == "__main__":
    unittest.main()
