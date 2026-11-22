"""Regression tests for ``utils/weak_strength_exit`` (weak-strength proactive exit).

Locks in the 2026-11-22 decision:

    • 删除最短持仓时间闸 MIN_HOLD_MIN —— 年龄不再决定是否主动平仓。
    • 新增组规模闸 MIN_GROUP_SIZE（默认 2）—— 组内可评估笔数不足时整组本轮不主动平，
      消除「单笔组必然垫底 → 必然被平」造成的提前出局。
    • 命中时仍只平「组内垫底 N 名」（N 默认 1），不平整组。

Nothing here contacts OANDA and no trading decision is changed.

Run (all three work):
    python tests/test_weak_strength_exit.py
    cd tests && python test_weak_strength_exit.py
    python -m unittest -v tests.test_weak_strength_exit
"""

import os
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from utils import weak_strength_exit as w


def _pos(tid, instr, quote, side, support, age_min):
    return {
        "trade_id": tid,
        "instrument": instr,
        "quote": quote,
        "side": side,
        "support": support,
        "age_min": age_min,
    }


class TestGroupSizeGate(unittest.TestCase):
    def test_single_member_group_is_not_closed(self):
        # 组内只有 1 笔：旧逻辑「该笔即垫底 → 照平」；新逻辑整组不主动平。
        plan = w.compute_weak_exit_plan(
            [_pos("1", "USD_JPY", "JPY", "BUY", -0.9, 300)],
            {"ENABLED": True, "N": 1, "MIN_GROUP_SIZE": 2},
        )
        self.assertEqual(plan["close_ids"], [])
        reasons = [s["reason"] for s in plan["skipped"]]
        self.assertEqual(reasons, ["group_too_small(1<2)"])

    def test_two_member_group_closes_bottom_one(self):
        plan = w.compute_weak_exit_plan(
            [
                _pos("1", "USD_JPY", "JPY", "BUY", 0.40, 300),
                _pos("2", "EUR_JPY", "JPY", "BUY", 0.10, 300),
            ],
            {"ENABLED": True, "N": 1, "MIN_GROUP_SIZE": 2},
        )
        # 支撑分更低（更不被支持）的 T2 被平；T1 保留。
        self.assertEqual(plan["close_ids"], ["2"])
        detail = plan["details"][0]
        self.assertEqual(detail["rank"], 1)
        self.assertEqual(detail["group_size"], 2)

    def test_ineligible_member_does_not_satisfy_group_size(self):
        # 2 笔中 1 笔缺 support → 可评估仅 1 笔 < 2 → 整组不主动平。
        plan = w.compute_weak_exit_plan(
            [
                _pos("1", "USD_JPY", "JPY", "BUY", 0.40, 300),
                _pos("2", "EUR_JPY", "JPY", "BUY", None, 300),
            ],
            {"ENABLED": True, "N": 1, "MIN_GROUP_SIZE": 2},
        )
        self.assertEqual(plan["close_ids"], [])
        self.assertEqual(
            sorted(s["reason"] for s in plan["skipped"]),
            ["group_too_small(1<2)", "no_support_score"],
        )


class TestMinHoldRemoved(unittest.TestCase):
    def test_young_position_is_now_eligible(self):
        # 旧逻辑：age=5 < MIN_HOLD_MIN(180) → 跳过。新逻辑：年龄不再拦截。
        plan = w.compute_weak_exit_plan(
            [
                _pos("1", "USD_JPY", "JPY", "BUY", 0.40, 5),
                _pos("2", "EUR_JPY", "JPY", "BUY", 0.10, 5),
            ],
            {"ENABLED": True, "N": 1, "MIN_GROUP_SIZE": 2},
        )
        self.assertEqual(plan["close_ids"], ["2"])
        self.assertNotIn("min_hold", " ".join(s["reason"] for s in plan["skipped"]))

    def test_legacy_min_hold_cfg_key_is_ignored(self):
        # 传入旧的 MIN_HOLD_MIN 不应影响结果（键已废弃、无副作用）。
        plan = w.compute_weak_exit_plan(
            [
                _pos("1", "USD_JPY", "JPY", "BUY", 0.40, 5),
                _pos("2", "EUR_JPY", "JPY", "BUY", 0.10, 5),
            ],
            {"ENABLED": True, "N": 1, "MIN_HOLD_MIN": 180.0},
        )
        self.assertEqual(plan["close_ids"], ["2"])


class TestFailClosedGuards(unittest.TestCase):
    def test_missing_support_score_is_skipped(self):
        plan = w.compute_weak_exit_plan(
            [_pos("1", "USD_JPY", "JPY", "BUY", None, 300)],
            {"ENABLED": True, "MIN_GROUP_SIZE": 2},
        )
        self.assertEqual(plan["close_ids"], [])
        self.assertEqual(plan["skipped"][0]["reason"], "no_support_score")

    def test_missing_age_is_skipped(self):
        plan = w.compute_weak_exit_plan(
            [_pos("1", "USD_JPY", "JPY", "BUY", 0.4, None)],
            {"ENABLED": True, "MIN_GROUP_SIZE": 2},
        )
        self.assertEqual(plan["close_ids"], [])
        self.assertEqual(plan["skipped"][0]["reason"], "no_age")

    def test_disabled_returns_empty(self):
        plan = w.compute_weak_exit_plan(
            [
                _pos("1", "USD_JPY", "JPY", "BUY", 0.40, 300),
                _pos("2", "EUR_JPY", "JPY", "BUY", 0.10, 300),
            ],
            {"ENABLED": False},
        )
        self.assertFalse(plan["enabled"])
        self.assertEqual(plan["close_ids"], [])

    def test_never_raises_on_malformed_input(self):
        plan = w.compute_weak_exit_plan([None, "x", {}, {"support": "n/a"}], {})
        self.assertEqual(plan["close_ids"], [])


class TestNSemantics(unittest.TestCase):
    def test_only_bottom_n_closed_not_whole_group(self):
        plan = w.compute_weak_exit_plan(
            [
                _pos("1", "USD_JPY", "JPY", "BUY", 0.40, 300),
                _pos("2", "EUR_JPY", "JPY", "BUY", 0.10, 300),
                _pos("3", "GBP_JPY", "JPY", "BUY", 0.25, 300),
            ],
            {"ENABLED": True, "N": 1, "MIN_GROUP_SIZE": 2},
        )
        self.assertEqual(plan["close_ids"], ["2"])
        self.assertEqual(plan["details"][0]["group_size"], 3)


class TestSupportScoreDirection(unittest.TestCase):
    def test_buy_and_sell_are_mirrored(self):
        scores = {"USD": 1.0, "JPY": 0.0}
        self.assertEqual(w.support_score(scores, "USD_JPY", "BUY"), 1.0)
        self.assertEqual(w.support_score(scores, "USD_JPY", "SELL"), -1.0)

    def test_unknown_currency_returns_none(self):
        self.assertIsNone(w.support_score({"USD": 1.0}, "USD_JPY", "BUY"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
