from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cursor_dashboard import pools, snapshot, store
from tests.test_pools import capacity_snapshot, period_snapshot


class SnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_database_path = store.DATABASE_PATH
        self.original_legacy_path = store.LEGACY_ACCOUNTS_PATH
        root = Path(self.temp_dir.name)
        store.DATABASE_PATH = root / "accounts.db"
        store.LEGACY_ACCOUNTS_PATH = root / "accounts.json"
        store._initialized.clear()
        snapshot._snapshots.clear()
        snapshot._inflight.clear()
        pools.reset()
        self.acc = {"label": "张三", "email": "zhang@example.com",
                    "department": "智慧运维", "cookie": "cookie-1"}

    def tearDown(self) -> None:
        store.DATABASE_PATH = self.original_database_path
        store.LEGACY_ACCOUNTS_PATH = self.original_legacy_path
        store._initialized.clear()
        snapshot._snapshots.clear()
        snapshot._inflight.clear()
        pools.reset()
        self.temp_dir.cleanup()

    def data(self, remaining: float = 61.0) -> dict:
        return {"email": "zhang@example.com",
                "quota": {"overall": {"remaining_pct": remaining}}}

    def test_failure_never_overwrites_a_successful_result(self) -> None:
        """这是整个改动的核心：限流不能把好数据擦掉。"""
        snapshot.record_success("zhang@example.com", "cookie-1", self.data())
        snapshot.record_failure(
            "zhang@example.com", "cookie-1", "rate_limited", "被挡住了"
        )

        view = snapshot.view(self.acc, "zhang@example.com")

        self.assertTrue(view["ok"])
        self.assertEqual(view["data"], self.data())
        self.assertTrue(view["stale"])
        self.assertFalse(view["expired"])
        self.assertEqual(view["error_kind"], "rate_limited")
        self.assertIsNotNone(view["ok_at"])

    def test_one_auth_failure_alone_does_not_mark_a_card_expired(self) -> None:
        snapshot.record_success("zhang@example.com", "cookie-1", self.data())
        snapshot.record_failure("zhang@example.com", "cookie-1", "expired", "失效")

        self.assertFalse(snapshot.view(self.acc, "zhang@example.com")["expired"])

        snapshot.record_failure("zhang@example.com", "cookie-1", "expired", "失效")
        view = snapshot.view(self.acc, "zhang@example.com")

        self.assertTrue(view["expired"])
        self.assertFalse(view["ok"])

    def test_a_rate_limit_between_auth_failures_restarts_the_count(self) -> None:
        """限流一次再失效一次，不该被凑成"确认失效"。"""
        snapshot.record_success("zhang@example.com", "cookie-1", self.data())
        snapshot.record_failure("zhang@example.com", "cookie-1", "expired", "失效")
        snapshot.record_failure("zhang@example.com", "cookie-1", "rate_limited", "挡住")
        snapshot.record_failure("zhang@example.com", "cookie-1", "expired", "失效")

        self.assertFalse(snapshot.view(self.acc, "zhang@example.com")["expired"])

    def test_never_seen_account_is_expired_on_first_auth_failure(self) -> None:
        """没有旧数据可保护时不必等确认，直接告诉用户重新粘贴。"""
        snapshot.record_failure("zhang@example.com", "cookie-1", "expired", "失效")

        view = snapshot.view(self.acc, "zhang@example.com")

        self.assertTrue(view["expired"])
        self.assertIsNone(view["data"])

    def test_changing_the_cookie_invalidates_the_snapshot(self) -> None:
        snapshot.record_success("zhang@example.com", "cookie-1", self.data())

        renewed = {**self.acc, "cookie": "cookie-2"}
        view = snapshot.view(renewed, "zhang@example.com")

        self.assertTrue(view["pending"])
        self.assertIsNone(view["data"])

    def test_snapshots_survive_a_restart(self) -> None:
        snapshot.record_success("zhang@example.com", "cookie-1", self.data(42.0))
        snapshot.record_failure("zhang@example.com", "cookie-1", "rate_limited", "挡住")

        snapshot._snapshots.clear()          # 相当于重启
        self.assertEqual(snapshot.load(), 1)

        view = snapshot.view(self.acc, "zhang@example.com")
        self.assertEqual(view["data"]["quota"]["overall"]["remaining_pct"], 42.0)
        self.assertEqual(view["error_kind"], "rate_limited")
        self.assertTrue(view["stale"])

    def test_view_never_leaks_the_cookie(self) -> None:
        snapshot.record_success("zhang@example.com", "cookie-1", self.data())

        view = snapshot.view(self.acc, "zhang@example.com")

        self.assertNotIn("cookie", view)
        self.assertNotIn("cookie-1", repr(view))

    def test_stored_snapshot_holds_no_cookie(self) -> None:
        snapshot.record_success("zhang@example.com", "cookie-1", self.data())

        rows = store.load_snapshots()

        self.assertNotIn("cookie-1", repr(rows))
        self.assertEqual(rows["zhang@example.com"]["fingerprint"],
                         store.fingerprint("cookie-1"))

    def test_dropping_an_account_removes_the_stored_snapshot(self) -> None:
        snapshot.record_success("zhang@example.com", "cookie-1", self.data())

        snapshot.drop("zhang@example.com")

        self.assertEqual(store.load_snapshots(), {})
        self.assertTrue(snapshot.view(self.acc, "zhang@example.com")["pending"])

    def test_own_capacity_survives_exhaustion_and_restart_then_expires_with_the_cycle(self):
        ident = self.acc['email']
        snapshot.record_success(ident, 'cookie-1', capacity_snapshot())
        capped = period_snapshot(3203, 1.7133333333333334, 100, 6.778835978835978)
        snapshot.record_success(ident, 'cookie-1', capped)
        snapshot._snapshots.clear()
        pools.reset()
        snapshot.load()
        snapshot.record_success(ident, 'cookie-1', capped)
        slot = snapshot.get(ident, 'cookie-1')['data']['quota']['other_models']
        self.assertEqual((slot['limit_usd'], slot['limit_source']), (22.5, 'history'))
        next_cycle = period_snapshot(0, 0, 0, 0)
        next_cycle['cycle'] = {'start': '2026-10-16T07:00:00Z', 'reset_at': '2026-11-16T07:00:00Z'}
        snapshot.record_success(ident, 'cookie-1', next_cycle)
        self.assertIsNone(snapshot.view(self.acc, ident)['data']['quota']['other_models']['limit_usd'])

    def test_drop_and_reload_remove_observations_for_deleted_accounts(self):
        ident = self.acc['email']
        snapshot.record_success(ident, 'cookie-1', capacity_snapshot())
        self.assertTrue(pools.snapshot_state())
        snapshot.drop(ident)
        self.assertEqual(pools.snapshot_state(), {})
        pools.observe('stale', capacity_snapshot())
        snapshot.load()
        self.assertEqual(pools.snapshot_state(), {})


if __name__ == "__main__":
    unittest.main()
