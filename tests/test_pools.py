"""同套餐的额度池登记表。

用满的账号三个百分比都被截在 100，自己解不出池子，靠同套餐里解得出的账号补上。
表里的每个数都来自真实反解，不是写死的常量——这几个测试就是守这条线的。
"""

import unittest
from copy import deepcopy

from cursor_dashboard import pools
from cursor_dashboard.domain.core import Conflict
from cursor_dashboard.usage import assemble

PRO = {"name": "Pro", "membership_type": "pro"}
SOLVED = {
    "plan": PRO,
    "quota": {
        "cursor_models": {"used_pct": 45.77, "remaining_pct": 54.23, "limit_usd": 450.0},
        "other_models": {"used_pct": 56.18, "remaining_pct": 43.82, "limit_usd": 45.0},
        "overall": {"used_pct": 46.72, "remaining_pct": 53.28, "limit_usd": 495.0},
    },
}
# 用满了：pool_limits 三档全给 None
CAPPED = {
    "plan": PRO,
    "quota": {
        "cursor_models": {"used_pct": 100.0, "remaining_pct": 0.0, "limit_usd": None},
        "other_models": {"used_pct": 100.0, "remaining_pct": 0.0, "limit_usd": None},
        "overall": {"used_pct": 100.0, "remaining_pct": 0.0, "limit_usd": None},
    },
}


def capacity_snapshot(cursor=450, other=22.5):
    result = deepcopy(SOLVED)
    result['cycle'] = {'start': '2026-09-16T07:00:00Z', 'reset_at': '2026-10-16T07:00:00Z'}
    for slot, limit in zip(result['quota'].values(), (cursor, other, cursor + other)):
        slot['limit_usd'] = limit
    return result


def period_snapshot(spend, auto, api, total):
    result = assemble('', {}, {'planInfo': {'planName': 'Pro'}}, {'membershipType': 'pro'},
                      {'planUsage': {'totalSpend': spend, 'autoPercentUsed': auto,
                                     'apiPercentUsed': api, 'totalPercentUsed': total}}, {})
    result.update(plan=deepcopy(PRO), cycle=capacity_snapshot()['cycle'])
    return result


class PoolsTest(unittest.TestCase):
    def setUp(self) -> None:
        pools.reset()

    def test_fills_a_capped_account_from_the_same_plan(self):
        pools.observe("solved@x.com", SOLVED)
        filled = pools.fill(CAPPED)["quota"]
        self.assertEqual(filled["cursor_models"]["limit_usd"], 450.0)
        self.assertEqual(filled["other_models"]["limit_usd"], 45.0)
        self.assertEqual(filled["overall"]["limit_usd"], 495.0)
        # 补出来的美元数要跟着这个账号自己的百分比走，不是照抄来源账号的
        self.assertEqual(filled["overall"]["used_usd"], 495.0)
        self.assertTrue(filled["overall"]["limit_inferred"])

    def test_nothing_to_copy_leaves_it_empty(self):
        """没有任何账号解出过就老实留空，绝不退回写死的 450。"""
        filled = pools.fill(CAPPED)["quota"]
        for slot in filled.values():
            self.assertIsNone(slot["limit_usd"])

    def test_never_overwrites_a_self_solved_limit(self):
        pools.observe("solved@x.com", {"plan": PRO, "quota": {
            "cursor_models": {"used_pct": 10, "limit_usd": 999.0},
            "other_models": {"used_pct": 10, "limit_usd": 99.0},
            "overall": {"used_pct": 10, "limit_usd": 1098.0},
        }})
        kept = pools.fill(SOLVED)["quota"]
        self.assertEqual(kept["cursor_models"]["limit_usd"], 450.0)
        self.assertNotIn("limit_inferred", kept["cursor_models"])

    def test_partial_solutions_only_supply_the_known_total(self):
        pools.observe("half@x.com", {"plan": PRO, "quota": {
            "cursor_models": {"used_pct": 98.03, "limit_usd": None},
            "other_models": {"used_pct": 100.0, "limit_usd": None},
            "overall": {"used_pct": 98.4, "limit_usd": 495.0},
        }})
        self.assertEqual(pools.resolve(PRO), (None, None, 495.0))

    def test_median_shrugs_off_one_bad_account(self):
        for i, limits in enumerate([(450.0, 45.0, 495.0), (450.0, 45.0, 495.0),
                                    (402.0, 93.0, 495.0)]):
            pools.observe(f"a{i}@x.com", {"plan": PRO, "quota": {
                "cursor_models": {"used_pct": 1, "limit_usd": limits[0]},
                "other_models": {"used_pct": 1, "limit_usd": limits[1]},
                "overall": {"used_pct": 1, "limit_usd": limits[2]},
            }})
        self.assertEqual(pools.resolve(PRO), (450.0, 45.0, 495.0))

    def test_plans_do_not_bleed_into_each_other(self):
        pools.observe("pro@x.com", SOLVED)
        business = {**CAPPED, "plan": {"name": "Business"}}
        for slot in pools.fill(business)["quota"].values():
            self.assertIsNone(slot["limit_usd"])

    def test_same_account_counts_once(self):
        for limit in (450.0, 450.0, 450.0):
            pools.observe("same@x.com", {"plan": PRO, "quota": {
                "cursor_models": {"used_pct": 1, "limit_usd": limit},
                "other_models": {"used_pct": 1, "limit_usd": 45.0},
                "overall": {"used_pct": 1, "limit_usd": 495.0},
            }})
        self.assertEqual(pools.snapshot_state(), {"pro": 1})


class VisibleHistoryTest(unittest.TestCase):
    def setUp(self):
        self.solved, self.capped = deepcopy(SOLVED), deepcopy(CAPPED)
        for snapshot in (self.solved, self.capped):
            snapshot['cycle'] = {'start': '2026-09-01T00:00:00+00:00', 'reset_at': '2026-10-01T00:00:00+00:00'}

    def test_same_cycle_history_survives_repeated_capped_refreshes_without_mutation(self):
        original = deepcopy(self.capped)
        retained = pools.retain_own_limits(self.capped, self.solved)
        retained = pools.retain_own_limits(self.capped, retained)
        self.assertEqual(self.capped, original)
        for key, limit in zip(('cursor_models', 'other_models', 'overall'), (450, 45, 495)):
            self.assertEqual(retained['quota'][key]['limit_usd'], limit)
            self.assertEqual(retained['quota'][key]['remaining_pct'], 0)
            self.assertEqual(retained['quota'][key]['limit_source'], 'history')
            self.assertTrue(retained['quota'][key]['limit_inferred'])

    def test_period_or_plan_changes_drop_history(self):
        for area, field, value in (
            ('cycle', 'start', '2026-10-01T00:00:00Z'), ('cycle', 'start', None),
            ('cycle', 'reset_at', '2026-11-01T00:00:00Z'), ('cycle', 'reset_at', None),
            ('cycle', 'start', '2026-09-01T00:00:00'), ('cycle', 'start', 'invalid'),
            ('plan', 'name', 'Business'), ('plan', 'included_usd', 60), ('plan', 'price', 60),
        ):
            with self.subTest(area=area, field=field, value=value):
                changed = deepcopy(self.capped)
                changed[area][field] = value
                self.assertIsNone(pools.retain_own_limits(changed, self.solved)['quota']['overall']['limit_usd'])

    def test_new_direct_and_partial_solutions_win_over_history(self):
        current = deepcopy(self.capped)
        current['quota']['overall']['limit_usd'] = 500
        retained = pools.retain_own_limits(current, self.solved)
        self.assertEqual(retained['quota']['overall']['limit_usd'], 500)
        self.assertNotIn('limit_inferred', retained['quota']['overall'])
        partial = deepcopy(self.solved)
        partial['quota']['cursor_models']['limit_usd'] = None
        retained = pools.retain_own_limits(self.capped, partial)
        self.assertIsNone(retained['quota']['cursor_models']['limit_usd'])
        self.assertEqual(retained['quota']['overall']['limit_usd'], 495)

    def test_peer_estimates_are_never_persisted_as_own_history_or_recycled(self):
        peer = pools.fill_visible(self.capped, [self.solved])
        self.assertEqual(peer['quota']['overall']['limit_source'], 'plan')
        self.assertIsNone(pools.retain_own_limits(self.capped, peer)['quota']['overall']['limit_usd'])
        self.assertIsNone(pools.fill_visible(self.capped, [peer])['quota']['overall']['limit_usd'])
        own = pools.retain_own_limits(self.capped, self.solved)
        self.assertEqual(pools.fill_visible(self.capped, [own])['quota']['overall']['limit_usd'], 495)

    def test_visible_partial_history_keeps_known_total_without_inventing_model_limits(self):
        partial = deepcopy(self.solved)
        for slot in ('cursor_models', 'other_models'):
            partial['quota'][slot]['limit_usd'] = None
        filled = pools.fill_visible(self.capped, [partial])['quota']
        self.assertEqual(filled['overall']['limit_usd'], 495)
        self.assertIsNone(filled['cursor_models']['limit_usd'])
        self.assertIsNone(filled['other_models']['limit_usd'])
        self.assertIsNone(pools.fill_visible(self.capped, [])['quota']['overall']['limit_usd'])

    def test_invalid_observations_do_not_become_history(self):
        for value in (-1, 0, float('nan'), float('inf'), '450', True):
            with self.subTest(value=value):
                invalid = deepcopy(self.solved)
                invalid['quota']['overall']['limit_usd'] = value
                self.assertIsNone(pools.retain_own_limits(self.capped, invalid)['quota']['overall']['limit_usd'])

    def test_equivalent_iso_timestamps_preserve_history(self):
        self.capped['cycle']['start'] = '2026-09-01T08:00:00+08:00'
        self.assertEqual(pools.retain_own_limits(self.capped, self.solved)['quota']['overall']['limit_usd'], 495)

    def test_explicit_reference_survives_refresh_but_never_becomes_a_peer_observation(self):
        reference = {'cycle_start': self.capped['cycle']['start'], 'cursor_models': 450, 'other_models': 45, 'overall': 495}
        saved = pools.set_reference_limits(self.capped, reference)
        refreshed = pools.retain_own_limits(self.capped, saved)
        for key in ('cursor_models', 'other_models', 'overall'):
            self.assertEqual(refreshed['quota'][key]['limit_usd'], reference[key])
            self.assertEqual(refreshed['quota'][key]['limit_source'], 'reference')
            self.assertEqual(refreshed['quota'][key]['remaining_pct'], 0)
        self.assertIsNone(pools.fill_visible(self.capped, [refreshed])['quota']['overall']['limit_usd'])
        self.assertIsNone(pools.set_reference_limits(refreshed, {'cycle_start': reference['cycle_start']})['quota']['overall']['limit_usd'])
        for part, key, value in [('cycle', 'start', '2026-10-01T00:00:00Z'), ('plan', 'name', 'Business')]:
            changed = deepcopy(self.capped)
            changed[part][key] = value
            self.assertIsNone(pools.retain_own_limits(changed, saved)['quota']['overall']['limit_usd'])
        # The next usable observation replaces the reference automatically.
        self.assertEqual(pools.retain_own_limits(self.solved, saved), self.solved)

    def test_reference_rejects_invalid_values_wrong_cycle_and_inconsistent_totals(self):
        reference = {'cycle_start': self.capped['cycle']['start']}
        for invalid in [0, -1, True, '450', float('nan'), float('inf'), 1e10]:
            with self.subTest(invalid=invalid), self.assertRaises(Conflict):
                pools.set_reference_limits(self.capped, {**reference, 'overall': invalid})
        for invalid in [{'cycle_start': '2026-10-01T00:00:00Z'}, {**reference, 'overall': 400, 'cursor_models': 450, 'other_models': 45}]:
            with self.assertRaises(Conflict):
                pools.set_reference_limits(self.capped, invalid)


class CapacityMatchingTest(unittest.TestCase):
    def setUp(self):
        pools.reset()
        self.old = capacity_snapshot(other=45)
        self.new = capacity_snapshot()

    def test_capped_and_nearly_equal_percentages_match_the_known_total(self):
        # Sanitized usage-only inputs: capped usage may exceed the bucket's limit.
        cases = [(5675, 7.606666666666667, 100, 12.010582010582011),
                 (3203, 1.7133333333333334, 100, 6.778835978835978),
                 (3782, 7.984444444444444, 8.4, 8.004232804232805),
                 (54, .12, 0, 54 / 47250 * 100)]
        peers = [deepcopy(self.old) for _ in range(16)] + [deepcopy(self.new) for _ in range(13)]
        for i, peer in enumerate(peers):
            pools.observe(str(i), peer)
        for inputs in cases:
            with self.subTest(inputs=inputs):
                data = period_snapshot(*inputs)
                original = deepcopy(data)
                self.assertIsNone(data['quota']['other_models']['limit_usd'])
                for result in (pools.fill(data), pools.fill_visible(data, peers)):
                    self.assertEqual(tuple(result['quota'][key]['limit_usd'] for key in pools._SLOTS),
                                     (450, 22.5, 472.5))
                    self.assertEqual(result['quota']['other_models']['limit_source'], 'plan')
                    for key in pools._SLOTS:
                        self.assertEqual(result['quota'][key]['remaining_pct'], data['quota'][key]['remaining_pct'])
                self.assertEqual(data, original)

    def test_a_different_total_is_never_used_as_a_fallback(self):
        data = period_snapshot(3203, 1.7133333333333334, 100, 6.778835978835978)
        filled = pools.fill_visible(data, [self.old])
        self.assertIsNone(filled['quota']['other_models']['limit_usd'])
        self.assertIsNone(filled['quota']['cursor_models']['limit_usd'])
        self.assertEqual(filled['quota']['overall']['limit_usd'], 472.5)

    def test_unknown_capacity_does_not_choose_a_majority_or_average_distinct_tiers(self):
        for copies in (1, 5):
            for used in (0, 100):
                with self.subTest(copies=copies, used=used):
                    data = period_snapshot(0, used, used, used)
                    filled = pools.fill_visible(data, [self.old] * copies + [self.new])['quota']
                    self.assertEqual(filled['cursor_models']['limit_usd'], 450)
                    self.assertIsNone(filled['other_models']['limit_usd'])
                    self.assertIsNone(filled['overall']['limit_usd'])

    def test_known_model_limit_can_identify_capacity_without_a_total(self):
        data = period_snapshot(0, 100, 100, 100)
        data['quota']['other_models']['limit_usd'] = 22.5
        filled = pools.fill_visible(data, [self.old, self.new])['quota']
        self.assertEqual(filled['overall']['limit_usd'], 472.5)

    def test_different_plan_terms_and_nonoverlapping_cycles_are_excluded(self):
        data = period_snapshot(3203, 1.7133333333333334, 100, 6.778835978835978)
        for field, value in [('price', '$60/mo'), ('included_usd', 60), ('membership_type', 'business')]:
            peer = deepcopy(self.new)
            peer['plan'][field] = value
            self.assertIsNone(pools.fill_visible(data, [peer])['quota']['other_models']['limit_usd'])
        for cycle in ({}, {'start': '2026-08-16T07:00:00Z', 'reset_at': '2026-09-16T07:00:00Z'}):
            peer = deepcopy(self.new)
            peer['cycle'] = cycle
            self.assertIsNone(pools.fill_visible(data, [peer])['quota']['other_models']['limit_usd'])

    def test_cent_rounding_is_tolerated_but_conflicting_history_is_discarded(self):
        data = period_snapshot(3203, 1.7133333333333334, 100, 6.778835978835978)
        data['quota']['overall']['limit_usd'] = 472.49
        filled = pools.fill_visible(data, [self.new])['quota']
        self.assertEqual(filled['other_models']['limit_usd'], 22.5)
        retained = pools.retain_own_limits(data, self.old)
        self.assertIsNone(retained['quota']['other_models']['limit_usd'])
        self.assertEqual(retained['quota']['overall']['limit_usd'], 472.49)

    def test_ambiguous_splits_do_not_create_an_unobserved_average(self):
        other = capacity_snapshot(cursor=427.5, other=45)
        data = period_snapshot(3203, 1.7133333333333334, 100, 6.778835978835978)
        filled = pools.fill_visible(data, [self.new, other])['quota']
        self.assertIsNone(filled['cursor_models']['limit_usd'])
        self.assertIsNone(filled['other_models']['limit_usd'])

    def test_legacy_observations_are_replaced_on_unsolved_refresh_plan_change_and_removal(self):
        pools.observe('account', self.old)
        pools.observe('account', period_snapshot(0, 0, 0, 0))
        self.assertEqual(pools.resolve(PRO), (None, None, None))
        pools.observe('account', self.old)
        changed = deepcopy(self.new)
        changed['plan']['name'] = 'Business'
        pools.observe('account', changed)
        self.assertEqual(pools.resolve(PRO), (None, None, None))
        pools.forget('account')
        self.assertEqual(pools.snapshot_state(), {})

    def test_inconsistent_observations_do_not_supply_limits(self):
        invalid = deepcopy(self.old)
        invalid['quota']['overall']['limit_usd'] = 472.5
        data = period_snapshot(3203, 1.7133333333333334, 100, 6.778835978835978)
        self.assertEqual(pools.fill_visible(data, [invalid]), data)


if __name__ == "__main__":
    unittest.main()
