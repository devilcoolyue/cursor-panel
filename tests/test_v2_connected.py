from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import secrets
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import unittest
from unittest.mock import patch
import uuid

import requests
from sqlalchemy import select

from cursor_dashboard.api.app import create_app
from cursor_dashboard.client import AuthExpired
from cursor_dashboard.application.devices import challenge, redirect_uri
from cursor_dashboard.domain.core import Conflict, Forbidden, NotFound, SecretError, Unauthenticated
from cursor_dashboard.infrastructure.persistence.models import DeviceAuthorization, UserSession
from cursor_dashboard.local.remote import Connections, DeviceStore, RemoteError, public_route, remote_http
from test_core import CoreFixture, data, token
from test_identity import IdentityFixture, PASSWORD
from test_v2_api import APIClient


class DeviceFixture(IdentityFixture):
    def setUp(self):
        super().setUp()
        self.devices = self.core.devices
        self.logged = self.sign_in()

    def approve(self, actor=None, device_id=None):
        verifier, state = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        device_id = device_id or str(uuid.uuid4())
        callback = "http://127.0.0.1:32123/callback"
        result = self.devices.authorize(actor or self.logged.actor, code_challenge=challenge(verifier), state=state,
            callback=callback, device_id=device_id, device_name="Synthetic laptop")
        values = parse_qs(urlsplit(result["callback_url"]).query)
        self.assertEqual(values["state"], [state])
        return {"code": values["code"][0], "verifier": verifier, "callback": callback, "device_id": device_id}

    def device(self, actor=None, device_id=None):
        result = self.devices.exchange(**self.approve(actor, device_id))
        return result, self.identity.authenticate(result["token"], kind="device")


class DeviceIdentityTest(DeviceFixture, unittest.TestCase):
    def test_pkce_binding_atomic_single_use_and_no_plaintext_in_database(self):
        params = self.approve()
        for changed in ({"verifier": "x" * 43}, {"device_id": str(uuid.uuid4())},
                        {"callback": "http://127.0.0.1:32124/callback"}):
            with self.assertRaises(Unauthenticated):
                self.devices.exchange(**{**params, **changed})
        def exchange(_):
            try:
                return self.devices.exchange(**params)
            except Unauthenticated:
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = [value for value in pool.map(exchange, range(4)) if value]
        self.assertEqual(len(results), 1)
        device = results[0]
        with self.assertRaises(Unauthenticated):
            self.identity.authenticate(device["token"])
        with self.assertRaises(Unauthenticated):
            self.identity.authenticate(self.logged.token, kind="device")
        with self.core.db.transaction() as session:
            self.assertIsNone(session.scalar(select(DeviceAuthorization)))
            record = session.get(UserSession, device["session_id"])
            self.assertEqual(record.kind, "device")
        disk = b"".join(p.read_bytes() for p in self.config.data_dir.glob("core.db*"))
        for value in (device["token"], params["code"], params["verifier"]):
            self.assertNotIn(value.encode(), disk)

    def test_callback_validation_browser_revocation_and_code_expiry(self):
        for value in ("https://evil.test/callback", "http://localhost:1234/callback", "http://127.0.0.1/callback",
                      "http://127.0.0.1:1234/callback?x=1", "http://user@127.0.0.1:1234/callback",
                      "http://127.0.0.1:1234/other", "http://127.0.0.1:bad/callback"):
            with self.assertRaises(Conflict):
                redirect_uri(value)
        params = self.approve()
        self.identity.revoke_session(self.logged.actor, self.logged.actor.session_id)
        with self.assertRaises(Unauthenticated):
            self.devices.exchange(**params)
        self.logged = self.sign_in()
        params = self.approve()
        with self.core.db.transaction(write=True) as session:
            for row in session.scalars(select(DeviceAuthorization)):
                row.expires_at = 0
        with self.assertRaises(Unauthenticated):
            self.devices.exchange(**params)

    def test_devices_are_independent_reconnecting_replaces_only_same_device(self):
        params = self.approve()
        first = self.devices.exchange(**params)
        second, actor = self.device()
        third, _ = self.device(device_id=params["device_id"])
        with self.assertRaises(Unauthenticated):
            self.identity.authenticate(first["token"], kind="device")
        self.identity.authenticate(second["token"], kind="device")
        self.identity.authenticate(third["token"], kind="device")
        self.assertEqual(len(self.devices.sessions(self.logged.actor)), 2)
        with self.assertRaises(Forbidden):
            self.approve(actor)
        with self.assertRaises(NotFound):
            self.devices.revoke(self.other, second["session_id"])
        self.devices.revoke(self.logged.actor, second["session_id"])
        with self.assertRaises(Unauthenticated):
            self.identity.me(actor)
        self.identity.authenticate(third["token"], kind="device")

    def test_password_recovery_disable_and_expiration_revoke_device_access(self):
        result, actor = self.device()
        with self.core.db.transaction(write=True) as session:
            session.get(UserSession, result["session_id"]).expires_at = 0
        with self.assertRaises(Unauthenticated):
            self.identity.me(actor)
        member = self.join()
        logged = self.identity.login("member@example.test", PASSWORD)
        _, member_device = self.device(logged.actor)
        self.identity.set_user_active(self.actor, member.user_id, False)
        self.identity.set_user_active(self.actor, member.user_id, True)
        with self.assertRaises(Unauthenticated):
            self.identity.me(member_device)
        _, actor = self.device()
        self.identity.change_password(self.logged.actor, PASSWORD, "Changed synthetic password 42!")
        with self.assertRaises(Unauthenticated):
            self.identity.me(actor)
        self.identity.recover_password("first@example.test", PASSWORD)
        self.logged = self.sign_in()
        _, actor = self.device()
        self.identity.recover_password("first@example.test", PASSWORD)
        with self.assertRaises(Unauthenticated):
            self.identity.me(actor)


class DeviceHTTPTest(DeviceFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.core.config = replace(self.config, mode="server")
        self.app = create_app(self.core, public_origin="https://panel.example.test", _device_switch_test=True)
        self.web = APIClient(self.app)
        self.row = self.account(snapshot=data(42))
        self.path = f"/api/v1/workspaces/{self.first}/accounts/{self.row.ref.account_id}"

    async def native(self, token, method, path, body=None, **headers):
        return await APIClient(self.app).request(method, path, body, headers={"origin": None, "cookie": None,
            "authorization": "Bearer " + token, **headers})

    async def test_browser_approval_exchange_contract_and_native_browser_separation(self):
        await self.web.login()
        verifier, state, device_id = secrets.token_urlsafe(32), secrets.token_urlsafe(32), str(uuid.uuid4())
        body = {"code_challenge": challenge(verifier), "state": state, "callback": "http://127.0.0.1:12345/callback",
                "device_id": device_id, "device_name": "HTTP fixture"}
        self.assertEqual((await self.web.request("POST", "/api/v1/auth/devices/authorize", body,
                         headers={"x-csrf-token": None}))[0], 403)
        status, approved, _ = await self.web.request("POST", "/api/v1/auth/devices/authorize", body)
        self.assertEqual(status, 200)
        code = parse_qs(urlsplit(approved["callback_url"]).query)["code"][0]
        exchange = {"code": code, "verifier": verifier, "callback": body["callback"], "device_id": device_id}
        self.assertEqual((await self.web.request("POST", "/api/v1/auth/devices/exchange", exchange))[0], 403)
        status, device, headers = await APIClient(self.app).request("POST", "/api/v1/auth/devices/exchange", exchange,
            headers={"origin": None, "cookie": None})
        self.assertEqual(status, 200)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertNotIn("set-cookie", headers)
        self.assertEqual((await self.native(device["token"], "GET", "/api/v1/me"))[0], 200)
        for extra in ({"origin": "https://panel.example.test"}, {"sec-fetch-mode": "cors"}, {"cookie": "x=y"}):
            self.assertEqual((await self.native(device["token"], "GET", self.path, **extra))[0], 403)
        self.assertEqual((await self.native(device["token"], "POST", self.path + "/manual-switch"))[0], 403)
        self.assertEqual((await self.web.request("POST", self.path + "/device-switch"))[0], 403)
        self.assertEqual((await self.native(device["token"], "GET", "/api/v1/auth/csrf"))[0], 403)

    async def test_production_gate_and_native_secret_delivery_replay_and_device_binding(self):
        first, _ = self.device()
        second, _ = self.device()
        production = APIClient(create_app(self.core, public_origin="https://panel.example.test"))
        _, boot, _ = await production.request("GET", "/api/v1/bootstrap")
        self.assertTrue(boot["capabilities"]["device_sessions"])
        self.assertFalse(boot["capabilities"]["remote_switch"])
        self.assertEqual((await production.request("POST", self.path + "/device-switch", headers={"origin": None,
            "cookie": None, "authorization": "Bearer " + first["token"]}))[0], 403)
        status, ticket, _ = await self.native(first["token"], "POST", self.path + "/device-switch")
        self.assertEqual(status, 200)
        self.assertEqual((await self.native(second["token"], "POST", "/api/v1/device-switch/consume", {"token": ticket["token"]}))[0], 404)
        status, delivered, _ = await self.native(first["token"], "POST", "/api/v1/device-switch/consume", {"token": ticket["token"]})
        self.assertEqual(status, 200)
        self.assertEqual(delivered["access_token"], self.row.secrets.access_token)
        self.assertNotIn("cookie", delivered)
        self.assertEqual((await self.native(first["token"], "POST", "/api/v1/device-switch/consume", {"token": ticket["token"]}))[0], 404)
        self.assertEqual((await self.native(first["token"], "POST", f"/api/v1/device-switch/{delivered['ticket_id']}/result", {"result": "failure"}))[0], 204)
        _, public, _ = await self.native(first["token"], "GET", self.path)
        for secret in (self.row.secrets.access_token, self.row.secrets.refresh_token, first["token"]):
            self.assertNotIn(secret, json.dumps(public))

    async def test_viewer_use_revocation_version_change_and_device_revoke(self):
        member = self.join()
        logged = self.identity.login("member@example.test", PASSWORD)
        device, actor = self.device(logged.actor)
        self.spaces.set_grant(self.actor, self.first, self.row.ref.account_id, member.user_id, "view")
        self.assertEqual((await self.native(device["token"], "POST", self.path + "/device-switch"))[0], 403)
        self.spaces.set_grant(self.actor, self.first, self.row.ref.account_id, member.user_id, "use")
        _, ticket, _ = await self.native(device["token"], "POST", self.path + "/device-switch")
        self.spaces.set_grant(self.actor, self.first, self.row.ref.account_id, member.user_id, None)
        self.assertEqual((await self.native(device["token"], "POST", "/api/v1/device-switch/consume", {"token": ticket["token"]}))[0], 404)
        self.spaces.set_grant(self.actor, self.first, self.row.ref.account_id, member.user_id, "use")
        _, ticket, _ = await self.native(device["token"], "POST", self.path + "/device-switch")
        from cursor_dashboard.infrastructure.persistence.models import Credential
        with self.core.db.transaction(write=True) as session:
            session.get(Credential, (self.first, self.row.ref.account_id)).version += 1
        self.assertEqual((await self.native(device["token"], "POST", "/api/v1/device-switch/consume", {"token": ticket["token"]}))[0], 404)
        with self.core.db.transaction(write=True) as session:
            session.get(Credential, (self.first, self.row.ref.account_id)).version -= 1
        async def slow(*args):
            self.devices.revoke(logged.actor, device["session_id"])
            return {"aggregations": []}
        self.core.credentials.gateway = slow
        self.assertEqual((await self.native(device["token"], "GET", self.path + "/detail"))[0], 401)
        self.assertEqual(len(self.core.accounts._details), 0)
        with self.assertRaises(Unauthenticated):
            self.identity.me(actor)


class MemoryDeviceStore:
    def __init__(self):
        self.values = {}
    def read_session(self, key):
        return self.values.get(key)
    def save_session(self, key, value):
        self.values[key] = value
    def delete_session(self, key):
        self.values.pop(key, None)


class NativeConnectionTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="p5-connections-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.store, self.opened, self.calls = MemoryDeviceStore(), [], []
        self.token = secrets.token_urlsafe(32)
        self.session_id = str(uuid.uuid4())
        self.info = {"api_version": 1, "mode": "server", "initialized": True, "capabilities": {"device_sessions": True, "remote_switch": False}}
        def http(origin, method, path, *, token=None, body=None):
            self.calls.append((origin, method, path))
            if path.endswith("/bootstrap"):
                return self.info
            if path.endswith("/exchange"):
                self.exchange = body
                return {"token": self.token, "session_id": self.session_id, "expires_at": time.time() + 86400}
            return {"id": "remote-user"}
        self.connections = Connections(self.directory, store=self.store, transport=http,
                                       browser=lambda url: self.opened.append(url) or True)
        self.addCleanup(self.connections.close)
        self.connections.add("Fixture team", "https://panel.example.test")
        self.row = self.connections.snapshot()["items"][0]

    def connect(self):
        self.connections.login(self.row["id"])
        params = parse_qs(urlsplit(self.opened[-1]).fragment.split("?", 1)[1])
        callback = params["callback"][0] + "?" + urlencode({"state": params["state"][0], "code": "synthetic-code-for-fixture"})
        response = requests.get(callback, timeout=5)
        self.assertEqual(response.status_code, 200)
        self.connections.select(self.row["id"])

    def test_browser_loopback_state_exchange_and_system_store_only(self):
        self.connections.login(self.row["id"])
        pending = self.connections.pending
        bad = pending["callback"] + "?" + urlencode({"code": "synthetic-code-for-fixture", "state": "wrong"})
        self.assertEqual(requests.get(bad, timeout=5).status_code, 400)
        self.assertEqual(self.store.values, {})
        good = pending["callback"] + "?" + urlencode({"code": "synthetic-code-for-fixture", "state": pending["state"]})
        self.assertEqual(requests.get(good, timeout=5).status_code, 200)
        self.assertEqual(challenge(self.exchange["verifier"]), parse_qs(urlsplit(self.opened[-1]).fragment.split("?", 1)[1])["code_challenge"][0])
        self.assertEqual(self.store.values[self.row["id"]]["token"], self.token)
        for path in self.directory.iterdir():
            self.assertNotIn(self.token, path.read_text())
        self.assertNotIn(self.token, json.dumps(self.connections.snapshot()))
        reopened = Connections(self.directory, store=self.store, transport=self.connections.http)
        self.addCleanup(reopened.close)
        self.assertIsNone(reopened.active)
        reopened.select(self.row["id"])
        self.assertEqual(reopened.request(self.row["id"], "GET", "/api/v1/me")["id"], "remote-user")

    def test_fixed_paths_origins_protocol_offline_and_expired_session(self):
        for origin in ("http://example.test", "https://name:password@example.test", "https://example.test/path", "file:///etc/passwd"):
            with self.assertRaises(Exception):
                self.connections.add("Bad", origin)
        space, item = f"/api/v1/workspaces/{uuid.uuid4()}", uuid.uuid4()
        for path in ("/api/v1/manual-switch/consume", "/api/v1/device-switch/consume", "/api/v1/auth/devices/exchange",
                     "https://evil.test/api/v1/me", "/api/v1/../native/recover", "/api/v1/me?url=https://evil.test",
                     f"{space}/accounts", f"{space}/accounts/{item}/authorization", f"{space}/authorizations/x/poll"):
            with self.assertRaises(Conflict):
                public_route("POST", path)
        for method, path in (("POST", f"{space}/authorizations"), ("POST", f"{space}/authorizations/{item}/poll"),
                             ("DELETE", f"{space}/authorizations/{item}")):
            self.assertEqual(public_route(method, path), path)
        self.connect()
        self.info["api_version"] = 2
        with self.assertRaises(RemoteError) as raised:
            self.connections.request(self.row["id"], "GET", "/api/v1/me")
        self.assertEqual(raised.exception.status, 426)
        self.info["api_version"] = 1
        self.store.values[self.row["id"]]["expires_at"] = 0
        with self.assertRaises(RemoteError) as raised:
            self.connections.request(self.row["id"], "GET", "/api/v1/me")
        self.assertEqual(raised.exception.status, 401)

    def test_switching_instances_rejects_late_responses_and_cancels_login(self):
        self.connect()
        entered, release = threading.Event(), threading.Event()
        http = self.connections.http
        def slow(origin, method, path, **kwargs):
            if path.endswith("/me"):
                entered.set()
                release.wait(5)
            return http(origin, method, path, **kwargs)
        self.connections.http = slow
        with ThreadPoolExecutor() as pool:
            future = pool.submit(self.connections.request, self.row["id"], "GET", "/api/v1/me")
            self.assertTrue(entered.wait(5))
            self.connections.select(None)
            release.set()
            with self.assertRaises(Conflict):
                future.result()
        self.connections.login(self.row["id"])
        pending = self.connections.pending
        self.connections.select(None)
        with self.assertRaises(Conflict):
            self.connections._complete(self.row, pending, "synthetic-code")
        self.assertTrue(pending["cancel"].is_set())

    def test_offline_disconnect_clears_local_secret_and_reports_unconfirmed_revocation(self):
        self.connect()
        self.connections.http = lambda *args, **kwargs: (_ for _ in ()).throw(RemoteError(502, "offline"))
        result = self.connections.disconnect(self.row["id"], remove=True)
        self.assertFalse(result["revoked"])
        self.assertEqual(result["items"], [])
        self.assertEqual(self.store.values, {})
        self.assertIsNone(result["active_id"])

    def test_locked_system_store_never_falls_back_and_remote_switch_gate_prevents_delivery(self):
        self.connect()
        with self.assertRaises(RemoteError):
            self.connections.delivery(self.row["id"], str(uuid.uuid4()), str(uuid.uuid4()))
        self.assertFalse(any("device-switch" in path for _, _, path in self.calls))
        with patch.object(DeviceStore, "backend", side_effect=RuntimeError("synthetic keyring locked")):
            with self.assertRaises(SecretError):
                DeviceStore(self.directory).read_session(self.row["id"])

    def test_http_adapter_disables_redirects_environment_proxies_and_cookie_reuse(self):
        with patch("cursor_dashboard.local.remote.requests.Session") as constructor:
            client = constructor.return_value.__enter__.return_value
            response = client.request.return_value.__enter__.return_value
            response.status_code = 302
            with self.assertRaises(RemoteError):
                remote_http("https://panel.example.test", "GET", "/api/v1/me", token=self.token)
            self.assertFalse(client.trust_env)
            self.assertFalse(client.request.call_args.kwargs["allow_redirects"])
            self.assertEqual(client.request.call_args.kwargs["headers"], {"Authorization": "Bearer " + self.token})


class SharedRefreshRiskTest(CoreFixture, unittest.IsolatedAsyncioTestCase):
    """Counterexample model, deliberately NOT a claim about Cursor's real behavior."""
    async def test_external_client_consuming_shared_rt_bypasses_database_lease(self):
        account = self.account(expiry=int(time.time()) - 10, snapshot=data(42))
        current_rt = account.secrets.refresh_token

        async def strict_upstream(cookie, label, operation, supplied):
            nonlocal current_rt
            if supplied != current_rt:
                raise AuthExpired("Synthetic single-use RT already rotated")
            current_rt = "synthetic-rotated-rt"
            return {"access_token": token("rotated"), "refresh_token": current_rt}

        await strict_upstream("", "External Cursor fixture", "desktop_refresh", current_rt)
        self.core.credentials.gateway = strict_upstream
        with self.assertRaises(AuthExpired):
            await self.core.credentials.ensure(self.first, account.ref.account_id)
        saved = self.repo.authorized(self.first, account.ref.account_id)
        self.assertTrue(saved.invalid)
        self.assertEqual(self.core.accounts.get(self.actor, self.first, account.ref.account_id)["data"], data(42))

    async def test_server_and_external_client_race_cannot_both_refresh_single_use_rt(self):
        account = self.account(expiry=int(time.time()) - 10)
        current_rt = account.secrets.refresh_token
        entered, release = asyncio.Event(), asyncio.Event()

        async def strict_upstream(cookie, label, operation, supplied):
            nonlocal current_rt
            entered.set()
            await release.wait()
            if supplied != current_rt:
                raise AuthExpired("Synthetic shared RT conflict")
            current_rt = "synthetic-new-rt"
            return {"access_token": token("rotated"), "refresh_token": current_rt}

        self.core.credentials.gateway = strict_upstream
        server = asyncio.create_task(self.core.credentials.ensure(self.first, account.ref.account_id))
        await entered.wait()
        client = asyncio.create_task(strict_upstream("", "External Cursor fixture", "desktop_refresh", current_rt))
        release.set()
        results = await asyncio.gather(server, client, return_exceptions=True)
        self.assertEqual(sum(isinstance(value, AuthExpired) for value in results), 1)


if __name__ == "__main__":
    unittest.main()
