from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import requests

from cursor_dashboard import desktop, sessions, snapshot, store, server
from cursor_dashboard.client import AuthExpired, RateLimited


def credential(marker="initial", expires=None):
    expires = int(time.time()) + 7200 if expires is None else expires
    claims = {"sub": "auth0|user_test", "type": "session", "exp": expires, "marker": marker}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return desktop.DesktopSession(f"eyJhbGciOiJIUzI1NiJ9.{body}.signature", claims["sub"], expires,
                                  "refresh-" + marker, "session")


def pending_poll():
    response = requests.Response()
    response.status_code = 404
    return requests.HTTPError(response=response)


class SessionStoreTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        for name, value in (("DATABASE_PATH", self.root / "accounts.db"),
                            ("LEGACY_ACCOUNTS_PATH", self.root / "missing.json")):
            p = patch.object(store, name, value)
            p.start()
            self.addCleanup(p.stop)
        store._initialized.clear()
        snapshot._snapshots.clear()
        snapshot._inflight.clear()
        self.addCleanup(store._initialized.clear)
        self.addCleanup(snapshot._snapshots.clear)
        self.addCleanup(snapshot._inflight.clear)
        server._authorizations.clear()
        self.addCleanup(server._authorizations.clear)
        self.email = "test@example.test"
        self.account = store.upsert_account("expired-web-cookie", self.email, "Test", session=credential())

    async def test_saved_desktop_session_never_uses_web_cookie(self):
        fetch = AsyncMock(return_value={"planUsage": {"totalSpend": 10}})
        await sessions.request_account(self.account, fetch, "desktop_period")
        fetch.assert_awaited_once_with("", "Test", "desktop_period", self.account["access_token"])

    async def test_concurrent_requests_refresh_only_once_and_persist_rotation(self):
        store.update_session(self.account, credential(expires=int(time.time()) + 20))
        renewed = credential("renewed")
        calls = []
        async def fetch(cookie, label, name, *args):
            calls.append((cookie, name, args))
            await asyncio.sleep(.04)
            return {"access_token": renewed.token, "refresh_token": renewed.refresh_token, "shouldLogout": False}
        results = await asyncio.gather(*(sessions.ensure_account(self.account, fetch) for _ in range(12)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0:2], ("", "desktop_refresh"))
        self.assertTrue(all(a["refresh_token"] == renewed.refresh_token for a in results))
        self.assertEqual(store.get_account(self.email)["refresh_token"], renewed.refresh_token)
        self.assertEqual(results[0]["auth_generation"], self.account["auth_generation"])

    async def test_401_forces_one_refresh_then_retries_with_new_at(self):
        renewed = credential("new")
        fetch = AsyncMock(side_effect=[AuthExpired("401"), {"access_token": renewed.token}, {"ok": True}])
        result = await sessions.request_account(self.account, fetch, "desktop_period")
        self.assertEqual(result, {"ok": True})
        self.assertEqual([c.args[2] for c in fetch.await_args_list], ["desktop_period", "desktop_refresh", "desktop_period"])
        self.assertEqual(fetch.await_args_list[-1].args[3], renewed.token)
        self.assertEqual(store.get_account(self.email)["refresh_token"], renewed.token)

    async def test_revoked_session_does_not_retry_with_old_cookie(self):
        fetch = AsyncMock(return_value={"shouldLogout": True})
        with self.assertRaises(AuthExpired), patch.object(store.time, "time", return_value=time.time() + 120):
            await sessions.ensure_account(self.account, fetch, rejected_token=self.account["access_token"])
        with self.assertRaises(AuthExpired):
            await sessions.ensure_account(self.account, fetch)
        fetch.assert_awaited_once()
        self.assertTrue(store.get_account(self.email)["auth_invalid"])
        self.assertEqual(store.get_account(self.email)["auth_refreshed_at"], self.account["auth_refreshed_at"])

    async def test_transient_refresh_error_preserves_credentials_and_releases_lease(self):
        fetch = AsyncMock(side_effect=RateLimited("limited"))
        with self.assertRaises(RateLimited):
            await sessions.ensure_account(self.account, fetch, rejected_token=self.account["access_token"])
        saved = store.get_account(self.email)
        self.assertEqual(saved["refresh_token"], self.account["refresh_token"])
        self.assertFalse(saved["auth_invalid"])
        self.assertTrue(store.claim_auth_lease(self.email, "next"))

    async def test_refresh_cannot_overwrite_concurrent_reauthorization(self):
        replacement = credential("replacement")
        async def fetch(*args):
            store.upsert_account("new-cookie", self.email, None, session=replacement)
            return {"access_token": credential("stale").token}
        result = await sessions.ensure_account(self.account, fetch, rejected_token=self.account["access_token"])
        self.assertEqual(result["access_token"], replacement.token)
        self.assertEqual(store.get_account(self.email)["cookie"], "new-cookie")

    async def test_refresh_failure_cannot_revoke_concurrent_reauthorization(self):
        replacement = credential("replacement")
        async def fetch(*args):
            store.upsert_account("new-cookie", self.email, None, session=replacement)
            return {"shouldLogout": True}
        result = await sessions.ensure_account(self.account, fetch, rejected_token=self.account["access_token"])
        self.assertEqual(result["access_token"], replacement.token)
        self.assertFalse(result["auth_invalid"])

    async def test_deleted_account_is_not_recreated_by_inflight_refresh(self):
        async def fetch(*args):
            store.delete_account(self.email)
            return {"access_token": credential("new").token}
        with self.assertRaises(store.AccountsError):
            await sessions.ensure_account(self.account, fetch, rejected_token=self.account["access_token"])
        self.assertIsNone(store.get_account(self.email))

    async def test_legacy_record_without_refresh_token_requires_browser_reauthorization(self):
        legacy = store.upsert_account("user_test::" + credential().token, self.email, "Test")
        fetch = AsyncMock()
        with self.assertRaises(AuthExpired):
            await sessions.ensure_account(legacy, fetch)
        fetch.assert_not_called()

    async def browser_authorize(self, **request):
        """Start through the HTTP handler and poll until the synthetic browser confirmation arrives."""
        started = await server.api_authorization_start(server.AuthorizationReq(**request))
        self.assertTrue(started["login_url"].startswith("https://cursor.com/loginDeepControl?"))
        for _ in range(3):
            pending = server._authorizations.get(started["id"])
            if pending:
                pending.next_poll = 0
            result = await server.api_authorization_poll(started["id"])
            if result["status"] == "completed":
                return result
        self.fail("Browser authorization did not complete")

    async def test_renewal_keeps_snapshot_and_public_views_do_not_leak_tokens(self):
        snapshot.record_success(self.email, self.account["cookie"], {"email":self.email, "quota":{"overall":{"remaining_pct":50}}})
        fetch = AsyncMock(return_value={"access_token": credential("new").token})
        updated = await sessions.ensure_account(self.account, fetch, rejected_token=self.account["access_token"])
        view = snapshot.view(updated, self.email)
        self.assertTrue(view["ok"])
        self.assertEqual(view["auth"]["mode"], "desktop")
        for secret in (updated["cookie"], updated["access_token"], updated["refresh_token"]):
            self.assertNotIn(secret, repr(view))
            self.assertNotIn(secret, repr(server.account_index([updated])))

    async def test_browser_authorization_commits_credentials_without_cookie(self):
        new = credential("saved")
        old_snapshot = {"email": self.email, "quota": {"overall": {"remaining_pct": 10}}}
        snapshot.record_success(self.email, self.account["cookie"], old_snapshot)
        data = {
            "desktop_me": {"email": self.email, "authId": new.subject},
            "desktop_plan": {"planInfo":{"planName":"Free"}},
            "desktop_profile": {"membershipType":"free"},
            "desktop_period": {"planUsage":{"totalSpend":0}},
            "desktop_grok": {"includedLimitZero":True}, "desktop_limit":{"noUsageBasedAllowed":True},
        }
        polls = []
        async def fetch(cookie, label, name, *args):
            self.assertEqual(cookie, "")
            if name == "desktop_poll":
                polls.append(args)
                if len(polls) == 1:
                    raise pending_poll()
                return {"accessToken": new.token, "refreshToken": new.refresh_token}
            return data[name]
        with patch.object(server, "fetch_cursor", side_effect=fetch):
            result = await self.browser_authorize(label="Saved", account_key=self.email)
        self.assertEqual((result["status"], result["email"], result["label"]), ("completed", self.email, "Saved"))
        saved = store.get_account(self.email)
        self.assertEqual(saved["refresh_token"], new.refresh_token)
        # The column keeps a non-secret authorization marker, so the old snapshot is replaced.
        self.assertTrue(saved["cookie"].startswith("browser:"))
        self.assertNotEqual(saved["cookie"], self.account["cookie"])
        self.assertTrue(snapshot.view(saved, self.email)["ok"])
        self.assertNotIn(new.token, repr(result))
        self.assertEqual(server._authorizations, {})

    async def test_mismatched_or_failed_authorization_keeps_existing_credentials(self):
        other = credential("other")
        async def mismatched(cookie, label, name, *args):
            if name == "desktop_poll":
                return {"accessToken": other.token, "refreshToken": other.refresh_token}
            if name == "desktop_me":
                return {"email": "other@example.test", "authId": other.subject}
            raise AssertionError("Another identity must be rejected before usage queries")
        async def expired(cookie, label, name, *args):
            if name == "desktop_poll":
                return {"accessToken": other.token, "refreshToken": other.refresh_token}
            raise AuthExpired("invalid")
        for fetch, status in ((mismatched, 409), (expired, 400)):
            with self.subTest(status=status), patch.object(server, "fetch_cursor", side_effect=fetch), \
                 self.assertRaises(server.HTTPException) as raised:
                await self.browser_authorize(account_key=self.email)
            self.assertEqual(raised.exception.status_code, status)
        self.assertEqual(store.get_account(self.email)["access_token"], self.account["access_token"])
        self.assertEqual(server._authorizations, {})
        with self.assertRaises(server.HTTPException) as raised:
            await server.api_authorization_start(server.AuthorizationReq(account_key="missing@example.test"))
        self.assertEqual(raised.exception.status_code, 404)

    async def test_browser_authorization_paces_tolerates_transient_errors_expires_and_cancels(self):
        with patch.object(server, "fetch_cursor", side_effect=[pending_poll(), RateLimited("limited")]) as fetch:
            started = await server.api_authorization_start(server.AuthorizationReq())
            for _ in range(2):
                server._authorizations[started["id"]].next_poll = 0
                self.assertEqual(await server.api_authorization_poll(started["id"]), {"status": "pending"})
            self.assertEqual(await server.api_authorization_poll(started["id"]), {"status": "pending"})
            self.assertEqual(fetch.await_count, 2)
            self.assertEqual(server._authorizations[started["id"]].failures, 1)
            server._authorizations[started["id"]].expires_at = 0
            with self.assertRaises(server.HTTPException) as raised:
                await server.api_authorization_poll(started["id"])
            self.assertEqual(raised.exception.status_code, 410)
            started = await server.api_authorization_start(server.AuthorizationReq())
            await server.api_authorization_cancel(started["id"])
            with self.assertRaises(server.HTTPException) as raised:
                await server.api_authorization_poll(started["id"])
            self.assertEqual(raised.exception.status_code, 404)
        with self.assertRaises(server.HTTPException) as raised:
            server.api_save_cookie()
        self.assertEqual(raised.exception.status_code, 410)

    async def test_refresh_with_stale_account_record_uses_current_snapshot_identity(self):
        new = store.upsert_account('new-web-cookie', self.email, 'Renamed', session=credential('replacement'))
        responses = {
            'desktop_me': {'email':self.email,'authId':new['auth_subject']},
            'desktop_plan':{'planInfo':{'planName':'Free'}},
            'desktop_profile':{'membershipType':'free'},
            'desktop_period':{'planUsage':{'totalSpend':0}},
            'desktop_grok':{}, 'desktop_limit':{'noUsageBasedAllowed':True},
        }
        async def fetch(cookie, label, name, *args):
            return responses[name]
        with patch.object(server, 'fetch_cursor', side_effect=fetch):
            result = await server.refresh_account(self.account)
        self.assertIsNone(result)
        self.assertTrue(snapshot.view(new, self.email)['ok'])
