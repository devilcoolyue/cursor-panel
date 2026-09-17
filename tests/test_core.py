from __future__ import annotations

import asyncio
import base64
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from sqlalchemy import select, text

from cursor_dashboard.client import AuthExpired, RateLimited
from cursor_dashboard.domain.core import Actor, Conflict, Locked, NotFound, SecretError, Secrets
from cursor_dashboard.infrastructure.persistence.legacy import import_backup, read_backup
from cursor_dashboard.infrastructure.persistence.models import (AccountTag, Credential, Grant, Lease,
    LegacyMapping, Membership, Metadata, Tag, User)
from cursor_dashboard.infrastructure.secrets import FileKeyProvider
from cursor_dashboard.runtime.core import Core
from cursor_dashboard.runtime.settings import CoreConfig


def token(marker="base", subject="user_test", expiry=None):
    claims = {"sub": f"auth0|{subject}", "type": "session", "exp": expiry or int(time.time()) + 7200,
              "marker": marker}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{body}.signature"


def data(limit=None, cycle="2026-09-01T00:00:00+00:00"):
    return {"plan": {"name": "Pro"}, "cycle": {"start": cycle}, "quota": {
        name: {"limit_usd": (limit if name == "overall" else limit / 2) if limit is not None else None,
               "used_pct": 25, "remaining_pct": 75}
        for name in ("cursor_models", "other_models", "overall")}}


def make_legacy(path, *, ambiguous=False):
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript('''
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO metadata VALUES ('schema_version', '4');
            INSERT INTO metadata VALUES ('admin_switch_policy', '{"all_accounts":true}');
            CREATE TABLE accounts (id INTEGER PRIMARY KEY, label TEXT, cookie TEXT, email TEXT,
                department TEXT, updated_at INTEGER, access_token TEXT, refresh_token TEXT,
                token_expires_at INTEGER, auth_subject TEXT, auth_generation TEXT,
                auth_invalid INTEGER, auth_refreshed_at INTEGER);
            CREATE TABLE snapshots (account_id TEXT PRIMARY KEY, fingerprint TEXT, payload TEXT,
                ok_at INTEGER, error_kind TEXT, error_message TEXT, error_at INTEGER,
                failures INTEGER, attempted_at INTEGER);
        ''')
        conn.execute("INSERT INTO accounts VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("Legacy", "legacy-cookie-secret", "legacy@example.test", "研发部", 100,
             token(), "legacy-refresh-secret", int(time.time()) + 7200, "auth0|user_test", "old-generation", 0, 99))
        conn.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)", ("legacy@example.test",
            hashlib.sha256(b"legacy-cookie-secret").hexdigest()[:16], json.dumps(data(42)), 100,
            "network", "contains legacy-cookie-secret", 101, 1, 101))
        if ambiguous:
            for ident in (2, 3):
                conn.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ident, "Same label", f"fake-{ident}", None, "", 0, "", "", 0, None, "", 0, 0))
            conn.execute("INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)", ("Same label", "unused", "{}", 0,
                                                                           None, None, None, 0, 0))


class CoreFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="core-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = self.root / "key.json"
        FileKeyProvider.create(self.key)
        self.config = CoreConfig(self.root / "data", self.key, request_interval=0, refresh_margin=30)
        async def offline(*args):
            raise AssertionError("Unexpected network operation")
        self.core = Core(self.config, initialize=True, gateway=offline)
        self.addCleanup(self.core.close)
        self.repo = self.core.repository
        first = self.repo.create_workspace("first@example.test", "First", "team")
        second = self.repo.create_workspace("second@example.test", "Second", "team")
        self.first, self.second = first["workspace_id"], second["workspace_id"]
        self.actor, self.other = Actor(first["user_id"]), Actor(second["user_id"])

    def account(self, *, workspace=None, actor=None, marker="one", email="same@example.test", expiry=None, snapshot=None):
        return self.repo.put_authorization(actor or self.actor, workspace or self.first, email=email,
            subject="user_test" if email == "same@example.test" else marker, label=marker,
            secrets=Secrets(f"secret-cookie-{marker}", token(marker), f"secret-rt-{marker}"),
            expires_at=expiry or int(time.time()) + 7200, data=snapshot)


class PersistenceTest(CoreFixture, unittest.TestCase):
    def test_spaces_allow_same_email_without_cross_scope_lookup(self):
        a = self.account()
        b = self.account(workspace=self.second, actor=self.other, marker="two")
        self.assertNotEqual(a.ref.account_id, b.ref.account_id)
        self.assertEqual(len(self.core.accounts.list(self.actor, self.first)), 1)
        with self.assertRaises(NotFound):
            self.core.accounts.list(self.actor, self.second)
        with self.assertRaises(NotFound):
            self.repo.authorized(self.second, a.ref.account_id)
        with self.assertRaises(Conflict):
            self.account(marker="duplicate")

    def test_ciphertext_and_public_views_hide_plaintext_and_bind_identity(self):
        a = self.account(snapshot=data())
        b = self.account(workspace=self.second, actor=self.other, marker="two")
        public = json.dumps(self.core.accounts.list(self.actor, self.first)) + repr(a)
        disk = b"".join(path.read_bytes() for path in self.config.data_dir.glob("core.db*"))
        for secret in (a.secrets.cookie, a.secrets.access_token, a.secrets.refresh_token):
            self.assertNotIn(secret, public)
            self.assertNotIn(secret.encode(), disk)
        with self.core.db.transaction(write=True) as session:
            source = session.get(Credential, (self.first, a.ref.account_id))
            target = session.get(Credential, (self.second, b.ref.account_id))
            target.ciphertext, target.key_id = source.ciphertext, source.key_id
        with self.assertRaises(SecretError):
            self.repo.authorized(self.second, b.ref.account_id)
        with self.core.db.transaction(write=True) as session:
            record = session.get(Credential, (self.first, a.ref.account_id))
            record.ciphertext = record.ciphertext[:-1] + bytes([record.ciphertext[-1] ^ 1])
        with self.assertRaises(SecretError):
            self.repo.verify()

    def test_key_missing_wrong_and_restored(self):
        self.account()
        self.core.close()
        saved = self.key.read_bytes()
        self.key.unlink()
        with self.assertRaises(SecretError):
            Core(self.config)
        FileKeyProvider.create(self.key)
        with self.assertRaises(SecretError):
            Core(self.config)
        self.key.write_bytes(saved)
        with Core(self.config) as recovered:
            self.assertEqual(recovered.repository.verify()["accounts"], 1)
        with self.assertRaises(SecretError):
            FileKeyProvider.create(self.key)

    def test_data_directory_lock_blocks_other_process_and_upgrade(self):
        with self.assertRaises(Locked):
            Core(self.config, upgrade=True)
        command = [sys.executable, "-c", "from pathlib import Path; from cursor_dashboard.runtime.lock import RuntimeLock; "
                   "from cursor_dashboard.domain.core import Locked; import sys; "
                   "lock=RuntimeLock(Path(sys.argv[1])); lock.acquire()", str(self.config.lock_file)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("data directory is in use", result.stderr)
        self.core.close()
        with Core(self.config, upgrade=True) as opened:
            self.assertEqual(opened.repository.verify()["accounts"], 0)

    def test_composite_foreign_keys_and_transaction_rollback(self):
        a = self.account()
        with self.core.db.transaction(write=True) as session:
            tag = Tag(workspace_id=self.second, name="Other")
            session.add(tag)
            session.flush()
            tag_id = tag.id
        with self.assertRaises(Conflict):
            with self.core.db.transaction(write=True) as session:
                session.add(AccountTag(workspace_id=self.first, account_id=a.ref.account_id, tag_id=tag_id))
        with self.assertRaises(Conflict):
            self.repo.put_authorization(self.actor, self.first, email="new@example.test", subject="new", label="New",
                                        secrets=Secrets("not-committed"), tags=["x" * 129])
        self.assertEqual(self.repo.verify()["accounts"], 1)

    def test_migration_revision_is_current_and_no_environment_binding(self):
        with patch.dict(os.environ, {"DATABASE_PATH": str(self.root / "forbidden.db"), "ACCOUNTS_PATH": str(self.root / "forbidden.json")}):
            self.account()
        self.assertFalse((self.root / "forbidden.db").exists())
        with self.core.db.transaction() as session:
            self.assertEqual(session.execute(text("SELECT version_num FROM alembic_version")).scalar(), "0004_retention")
        with self.assertRaises(Conflict):
            self.repo.create_workspace("first@example.test", "Bad", "enterprise")

    def test_snapshot_cas_rotation_and_reauthorization(self):
        a = self.account(snapshot=data(42))
        self.assertTrue(self.repo.claim_lease(self.first, a.ref.account_id, "worker", 30))
        self.assertTrue(self.repo.rotate(a, Secrets(a.secrets.cookie, token("next"), "next-rt"),
                                        int(time.time()) + 8000, lease_owner="worker"))
        current = self.repo.authorized(self.first, a.ref.account_id)
        self.assertEqual(current.ref.generation, a.ref.generation)
        self.assertGreater(current.ref.version, a.ref.version)
        self.assertIsNotNone(self.core.accounts.get(self.actor, self.first, a.ref.account_id)["data"])
        replaced = self.repo.put_authorization(self.actor, self.first, email=a.email, subject=a.subject,
            label="Reauthorized", secrets=Secrets("new-cookie", token("new"), "new-rt"), expected=current.ref)
        self.assertNotEqual(replaced.ref.generation, a.ref.generation)
        self.assertFalse(self.repo.record_snapshot(a.ref, data=data(99)))
        self.assertFalse(self.repo.rotate(current, a.secrets, a.expires_at, lease_owner="worker"))
        self.assertTrue(self.core.accounts.get(self.actor, self.first, a.ref.account_id)["pending"])
        self.core.accounts.delete(self.actor, self.first, a.ref.account_id)
        self.assertFalse(self.repo.record_snapshot(replaced.ref, data=data()))
        self.assertFalse(self.repo.rotate(replaced, a.secrets, a.expires_at, lease_owner="worker"))
        self.assertEqual(self.repo.verify()["accounts"], 0)

    def test_failed_snapshots_preserve_success_and_confirm_expiry(self):
        a = self.account(snapshot=data(42))
        self.repo.record_snapshot(a.ref, error=("expired", "Expired"))
        first = self.core.accounts.get(self.actor, self.first, a.ref.account_id)
        self.assertTrue(first["stale"])
        self.assertFalse(first["expired"])
        self.repo.record_snapshot(a.ref, error=("expired", "Expired"))
        second = self.core.accounts.get(self.actor, self.first, a.ref.account_id)
        self.assertTrue(second["expired"])
        self.assertEqual(first["data"], second["data"])

    def test_own_pool_history_persists_across_restart_and_same_account_reauthorization_but_not_new_cycle(self):
        a = self.account(snapshot=data(42))
        capped = data()
        for slot in capped['quota'].values():
            slot.update(used_pct=100, remaining_pct=0)
        self.assertTrue(self.repo.record_snapshot(a.ref, data=capped, actor=self.actor))
        self.core.close()
        self.core = Core(self.config)
        self.addCleanup(self.core.close)
        self.repo = self.core.repository
        self.assertTrue(self.repo.record_snapshot(a.ref, data=capped, actor=self.actor))
        slot = self.core.accounts.get(self.actor, self.first, a.ref.account_id)['data']['quota']['overall']
        self.assertEqual((slot['limit_usd'], slot['remaining_pct'], slot['limit_source']), (42, 0, 'history'))
        self.repo.record_snapshot(a.ref, data=data(cycle='2026-10-01T00:00:00Z'), actor=self.actor)
        self.assertIsNone(self.core.accounts.get(self.actor, self.first, a.ref.account_id)['data']['quota']['overall']['limit_usd'])
        self.repo.record_snapshot(a.ref, data=data(42), actor=self.actor)
        updated = self.repo.put_authorization(self.actor, self.first, email=a.email, subject=a.subject,
            label=a.label, secrets=Secrets('new-cookie', token(), 'new-rt'), expected=a.ref, data=capped)
        self.assertFalse(self.repo.record_snapshot(a.ref, data=data(999), actor=self.actor))
        self.assertEqual(self.core.accounts.get(self.actor, self.first, updated.ref.account_id)['data']['quota']['overall']['limit_usd'], 42)

    def test_visible_observations_never_use_other_spaces_or_ungranted_accounts(self):
        capped = self.account(snapshot=data())
        solved = self.account(email="solved@example.test", marker="solved", snapshot=data(42))
        self.account(workspace=self.second, actor=self.other, marker="other", snapshot=data(999))
        with self.core.db.transaction(write=True) as session:
            session.add(Membership(workspace_id=self.first, user_id=self.other.user_id, role="viewer"))
            session.flush()
            session.add(Grant(workspace_id=self.first, account_id=capped.ref.account_id, user_id=self.other.user_id, level="view"))
        view = self.core.accounts.get(self.other, self.first, capped.ref.account_id)
        self.assertIsNone(view["data"]["quota"]["overall"]["limit_usd"])
        owner = self.core.accounts.get(self.actor, self.first, capped.ref.account_id)
        self.assertEqual(owner["data"]["quota"]["overall"]["limit_usd"], 42)
        # A source becoming capped keeps its own history, without copying it into the recipient.
        self.repo.record_snapshot(solved.ref, data=data(), actor=self.actor)
        self.assertEqual(self.core.accounts.get(self.actor, self.first, capped.ref.account_id)['data']['quota']['overall']['limit_usd'], 42)
        self.assertIsNone(self.core.accounts.get(self.other, self.first, capped.ref.account_id)['data']['quota']['overall']['limit_usd'])
        self.core.accounts.delete(self.actor, self.first, solved.ref.account_id)
        self.assertIsNone(self.core.accounts.get(self.actor, self.first, capped.ref.account_id)["data"]["quota"]["overall"]["limit_usd"])

    def test_reference_edit_preserves_snapshot_time_and_survives_restart_and_refresh(self):
        account = self.account(snapshot=data())
        before = self.core.accounts.get(self.actor, self.first, account.ref.account_id)
        self.core.accounts.edit(self.actor, self.first, account.ref.account_id,
            quota_reference={'cycle_start': before['data']['cycle']['start'], 'cursor_models': 450, 'other_models': 45, 'overall': 495})
        edited = self.core.accounts.get(self.actor, self.first, account.ref.account_id)
        self.assertEqual(edited['ok_at'], before['ok_at'])
        self.assertEqual(edited['attempted_at'], before['attempted_at'])
        self.core.close()
        self.core = Core(self.config)
        self.addCleanup(self.core.close)
        self.repo = self.core.repository
        self.repo.record_snapshot(account.ref, data=data(), actor=self.actor)
        self.assertEqual(self.core.accounts.get(self.actor, self.first, account.ref.account_id)['data']['quota']['overall']['limit_usd'], 495)
        other = self.account(marker='other', email='other@example.test', snapshot=data())
        self.assertIsNone(self.core.accounts.get(self.actor, self.first, other.ref.account_id)['data']['quota']['overall']['limit_usd'])


class MigrationTest(CoreFixture, unittest.TestCase):
    def test_import_is_read_only_atomic_idempotent_and_private(self):
        source = self.root / "legacy.db"
        make_legacy(source, ambiguous=True)
        original = source.read_bytes()
        report = read_backup(source).report
        self.assertEqual(report["skipped"], {"snapshot_ambiguous": 1})
        result = import_backup(self.repo, self.actor, self.first, source)
        self.assertEqual(result["accounts"], 3)
        self.assertFalse(result["already_imported"])
        self.assertEqual(source.read_bytes(), original)
        rows = self.core.accounts.list(self.actor, self.first)
        account = next(row for row in rows if row["email"])
        self.assertEqual(account["tags"], ["研发部"])
        self.assertEqual(account["data"]["quota"]["overall"]["limit_usd"], 42)
        self.assertNotIn("legacy-cookie-secret", json.dumps(rows))
        with self.core.db.transaction() as session:
            self.assertEqual(len(list(session.scalars(select(LegacyMapping)))), 3)
            self.assertEqual(list(session.scalars(select(Grant))), [])
            self.assertIsNone(session.get(Metadata, "admin_switch_policy"))
        self.core.accounts.edit(self.actor, self.first, account["id"], label="Changed")
        self.assertTrue(import_backup(self.repo, self.actor, self.first, source)["already_imported"])
        self.assertEqual(self.core.accounts.get(self.actor, self.first, account["id"])["label"], "Changed")
        self.core.accounts.delete(self.actor, self.first, account["id"])
        import_backup(self.repo, self.actor, self.first, source)
        self.assertEqual(self.repo.verify()["accounts"], 2)
        self.assertEqual(source.read_bytes(), original)
        with self.assertRaises(Conflict):
            import_backup(self.repo, self.other, self.second, source)

    def test_failed_import_rolls_back_all_rows_and_receipt(self):
        source = self.root / "legacy.db"
        make_legacy(source, ambiguous=True)
        seal = self.repo.cipher.seal
        count = 0
        def fail_midway(*args):
            nonlocal count
            count += 1
            if count == 2:
                raise SecretError("Simulated key failure")
            return seal(*args)
        with patch.object(self.repo.cipher, "seal", side_effect=fail_midway), self.assertRaises(SecretError):
            import_backup(self.repo, self.actor, self.first, source)
        self.assertEqual(self.repo.verify()["accounts"], 0)
        self.assertEqual(self.repo.verify()["imports"], 0)
        self.assertEqual(import_backup(self.repo, self.actor, self.first, source)["accounts"], 3)

    def test_active_wal_unknown_version_duplicates_and_nonempty_target_rejected(self):
        source = self.root / "legacy.db"
        make_legacy(source)
        wal = Path(f"{source}-wal")
        wal.write_bytes(b"active")
        with self.assertRaises(Conflict):
            read_backup(source)
        wal.unlink()
        with closing(sqlite3.connect(source)) as conn, conn:
            conn.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
        with self.assertRaises(Conflict):
            read_backup(source)
        with closing(sqlite3.connect(source)) as conn, conn:
            conn.execute("UPDATE metadata SET value='4' WHERE key='schema_version'")
        self.account()
        with self.assertRaises(Conflict):
            import_backup(self.repo, self.actor, self.first, source)

    def test_conflicting_normalized_email_is_not_silently_merged(self):
        source = self.root / "legacy.db"
        make_legacy(source)
        with closing(sqlite3.connect(source)) as conn, conn:
            conn.execute("INSERT INTO accounts (id,label,cookie,email) VALUES (2,'Duplicate','fake','LEGACY@example.test')")
        with self.assertRaises(Conflict):
            read_backup(source)
        self.assertEqual(self.repo.verify()["accounts"], 0)

    def test_unknown_database_never_upgraded_or_overwritten(self):
        self.core.close()
        before = self.config.database.read_bytes()
        with self.assertRaises(Conflict):
            Core(self.config, initialize=True)
        self.assertEqual(before, self.config.database.read_bytes())

    def test_stopped_database_and_key_backup_restore(self):
        source = self.root / "legacy.db"
        make_legacy(source)
        import_backup(self.repo, self.actor, self.first, source)
        self.core.close()
        backup = self.root / "restored"
        backup.mkdir()
        with closing(sqlite3.connect(self.config.database)) as original, closing(sqlite3.connect(backup / "core.db")) as target:
            original.backup(target)
        shutil.copy2(self.key, self.root / "restored-key.json")
        with Core(CoreConfig(backup, self.root / "restored-key.json")) as restored:
            self.assertEqual(restored.repository.verify()["credentials_decryptable"], 1)


class CredentialConcurrencyTest(CoreFixture, unittest.IsolatedAsyncioTestCase):
    def gateway(self, callback):
        self.core.credentials.gateway = callback
        self.core.accounts.gateway = callback

    async def test_authorization_validates_usage_before_committing_credentials(self):
        calls = []
        async def fetch(cookie, label, name, *args):
            calls.append(name)
            if name in {"me", "desktop_me"}:
                return {"email": "created@example.test", "sub": "auth0|user_test", "authId": "auth0|user_test"}
            if name == "desktop_poll":
                return {"accessToken": token("created"), "refreshToken": "created-rt"}
            return {}
        self.gateway(fetch)
        view = await self.core.accounts.authorize(self.actor, self.first, "user_test::" + token("web"), label="Created")
        self.assertIsNotNone(view["data"])
        self.assertEqual(view["email"], "created@example.test")
        self.assertIn("desktop_grok", calls)
        saved = self.repo.authorized(self.first, view["id"])
        async def failing(cookie, label, name, *args):
            if name == "desktop_plan":
                raise RateLimited("temporary")
            return await fetch(cookie, label, name, *args)
        self.gateway(failing)
        from cursor_dashboard.application.queries import QueryFailure
        with self.assertRaises(QueryFailure):
            await self.core.accounts.authorize(self.actor, self.first, "user_test::" + token("new-web"),
                                               label="Failed update", account_id=view["id"])
        self.assertEqual(self.repo.authorized(self.first, view["id"]), saved)

    async def test_concurrent_refresh_and_heartbeat_keep_one_rotation(self):
        a = self.account(expiry=int(time.time()) + 1, snapshot=data(42))
        self.core.credentials.config = replace(self.config, lease_ttl=9, lease_wait=15)
        calls = 0
        renewed = threading.Event()
        original_renew = self.repo.renew_lease
        def renew(*args):
            saved = original_renew(*args)
            if saved:
                renewed.set()
            return saved
        async def fetch(*args):
            nonlocal calls
            calls += 1
            self.assertTrue(await asyncio.to_thread(renewed.wait, 8), "Lease heartbeat did not complete")
            return {"access_token": token("renewed"), "refresh_token": "renewed-rt"}
        self.gateway(fetch)
        with patch.object(self.repo, "renew_lease", side_effect=renew):
            results = await asyncio.gather(*(self.core.credentials.ensure(self.first, a.ref.account_id) for _ in range(12)),
                                           return_exceptions=True)
        self.assertFalse([result for result in results if isinstance(result, BaseException)])
        self.assertEqual(calls, 1)
        self.assertEqual({r.ref.version for r in results}, {2})
        self.assertEqual(results[0].ref.generation, a.ref.generation)
        self.assertIsNotNone(self.core.accounts.get(self.actor, self.first, a.ref.account_id)["data"])

    async def test_stale_refresh_success_and_failure_cannot_overwrite_reauthorization(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                a = self.account(marker=str(failed), email=f"{failed}@example.test", expiry=int(time.time())+1)
                async def fetch(*args):
                    self.repo.put_authorization(self.actor, self.first, email=a.email, subject=a.subject, label="Replacement",
                        secrets=Secrets("replacement", token("replacement", a.subject), "replacement-rt"),
                        expires_at=int(time.time())+7200, expected=a.ref)
                    return {"shouldLogout": True} if failed else {"access_token": token("stale", a.subject), "refresh_token":"stale-rt"}
                self.gateway(fetch)
                current = await self.core.credentials.ensure(self.first, a.ref.account_id)
                self.assertEqual(current.secrets.refresh_token, "replacement-rt")
                self.assertFalse(current.invalid)

    async def test_delete_during_refresh_does_not_recreate(self):
        a = self.account(expiry=int(time.time())+1)
        async def fetch(*args):
            self.core.accounts.delete(self.actor, self.first, a.ref.account_id)
            return {"access_token": token("stale"), "refresh_token":"stale-rt"}
        self.gateway(fetch)
        with self.assertRaises(NotFound):
            await self.core.credentials.ensure(self.first, a.ref.account_id)
        self.assertEqual(self.repo.verify()["accounts"], 0)

    async def test_rate_limit_preserves_credentials_and_revocation_marks_invalid(self):
        a = self.account(expiry=int(time.time())+1)
        async def limited(*args):
            raise RateLimited("limited")
        self.gateway(limited)
        with self.assertRaises(RateLimited):
            await self.core.credentials.ensure(self.first, a.ref.account_id)
        self.assertEqual(self.repo.authorized(self.first, a.ref.account_id), a)
        self.assertTrue(self.repo.claim_lease(self.first, a.ref.account_id, "test", 1))
        self.repo.release_lease(self.first, a.ref.account_id, "test")
        async def revoked(*args):
            return {"shouldLogout": True}
        self.gateway(revoked)
        with self.assertRaises(AuthExpired):
            await self.core.credentials.ensure(self.first, a.ref.account_id)
        current = self.repo.authorized(self.first, a.ref.account_id)
        self.assertTrue(current.invalid)
        self.assertEqual(current.refreshed_at, a.refreshed_at)

    async def test_expired_lease_cannot_commit(self):
        a = self.account(expiry=int(time.time())+1)
        async def fetch(*args):
            with self.core.db.transaction(write=True) as session:
                lease = session.get(Lease, (self.first, a.ref.account_id))
                lease.expires_at = 0
            return {"access_token": token("stale"), "refresh_token":"stale-rt"}
        self.gateway(fetch)
        with self.assertRaises(Conflict):
            await self.core.credentials.ensure(self.first, a.ref.account_id)
        self.assertEqual(self.repo.authorized(self.first, a.ref.account_id), a)

    async def test_detail_cache_rechecks_access_and_authorization_generation(self):
        a = self.account(snapshot=data(42))
        calls = 0
        async def fetch(*args):
            nonlocal calls
            calls += 1
            return {"aggregations": []}
        self.gateway(fetch)
        await self.core.accounts.detail(self.actor, self.first, a.ref.account_id)
        await self.core.accounts.detail(self.actor, self.first, a.ref.account_id)
        self.assertEqual(calls, 1)
        with self.assertRaises(NotFound):
            await self.core.accounts.detail(self.other, self.first, a.ref.account_id)
        self.repo.put_authorization(self.actor, self.first, email=a.email, subject=a.subject, label="Replacement",
            secrets=Secrets("new", token("new"), "new-rt"), expires_at=int(time.time())+7200,
            expected=a.ref, data=data(42))
        await self.core.accounts.detail(self.actor, self.first, a.ref.account_id)
        self.assertEqual(calls, 2)

    async def test_refresh_error_classification_and_old_generation_discard(self):
        a = self.account(snapshot=data(42))
        async def failed(cookie, label, name, *args):
            if name == "desktop_me":
                raise AuthExpired("expired")
            if name == "desktop_refresh":
                return {"access_token": token("next"), "refresh_token":"next-rt"}
            raise RateLimited("limited")
        self.gateway(failed)
        result = await self.core.accounts.refresh(self.actor, self.first, a.ref.account_id)
        self.assertEqual(result["error_kind"], "rate_limited")
        self.assertIsNotNone(result["data"])

    async def test_refresh_result_cannot_overwrite_reauthorization_snapshot(self):
        a = self.account(snapshot=data(42))
        replaced = False
        async def fetch(cookie, label, name, *args):
            nonlocal replaced
            if not replaced:
                replaced = True
                self.repo.put_authorization(self.actor, self.first, email=a.email, subject=a.subject, label="Replacement",
                    secrets=Secrets("replacement", token("replacement"), "replacement-rt"), expires_at=int(time.time())+7200,
                    expected=a.ref, data=data(123))
            return {"email": a.email, "authId": a.subject} if name == "desktop_me" else {}
        self.gateway(fetch)
        with self.assertRaises(Conflict):
            await self.core.accounts.refresh(self.actor, self.first, a.ref.account_id)
        view = self.core.accounts.get(self.actor, self.first, a.ref.account_id)
        self.assertEqual(view["data"]["quota"]["overall"]["limit_usd"], 123)

    async def test_detail_rechecks_membership_after_network_wait(self):
        a = self.account(snapshot=data(42))
        async def fetch(*args):
            with self.core.db.transaction(write=True) as session:
                session.get(User, self.actor.user_id).active = False
            return {"aggregations": []}
        self.gateway(fetch)
        with self.assertRaises(NotFound):
            await self.core.accounts.detail(self.actor, self.first, a.ref.account_id)
        self.assertEqual(len(self.core.accounts._details), 0)

    async def test_cancelled_refresh_releases_lease(self):
        a = self.account(expiry=int(time.time())+1)
        ready = asyncio.Event()
        async def fetch(*args):
            ready.set()
            await asyncio.Event().wait()
        self.gateway(fetch)
        task = asyncio.create_task(self.core.credentials.ensure(self.first, a.ref.account_id))
        await ready.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.repo.claim_lease(self.first, a.ref.account_id, "next", 1))

    async def test_imported_cookie_exchange_persists_subject_without_losing_snapshot(self):
        a = self.repo.put_authorization(self.actor, self.first, email="same@example.test", subject=None,
                    label="Legacy", secrets=Secrets("legacy-cookie"), data=data(42))
        from cursor_dashboard.desktop import DesktopSession
        renewed = DesktopSession(token("exchanged"), "auth0|user_test", int(time.time())+7200, "exchanged-rt", "session")
        with patch("cursor_dashboard.application.credentials.exchange_cookie", return_value=(renewed, a.email)):
            current = await self.core.credentials.ensure(self.first, a.ref.account_id)
        self.assertEqual(current.subject, "user_test")
        self.assertEqual(current.ref.generation, a.ref.generation)
        self.assertEqual(current.secrets.refresh_token, "exchanged-rt")
        self.assertIsNotNone(self.core.accounts.get(self.actor, self.first, a.ref.account_id)["data"])


if __name__ == "__main__":
    unittest.main()
