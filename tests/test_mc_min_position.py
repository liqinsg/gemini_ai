"""Regression tests for the MC-regime minimum-position rule (2026-11-22).

新规则（run.env 软开关 MIN_LOT_STRONG_ENABLED，无条件生效）：
  1. 新开仓按 regime 分档作基：CONSOLIDATION=最小仓位 / NEUTRAL=最小×2 / STRONG=最大。
  2. 老仓逐轮扫描：regime=STRONG_MOMENTUM 且 MC 方向与持仓一致时补齐，每轮只升一级
     （最小仓位 × 1/2/4/8），目标=regime 档，上限=名义最大仓位，绝不一次倒满。

Run:
    python -m unittest -v tests.test_mc_min_position
"""

import os
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import scheduled_runner_v3 as runner

ROOT_TAG = "GEMINIAIBOT_V3::"


def _trade(tid, instr, units, sl="1.00000", tp="2.00000", tag=None):
    return {
        "id": str(tid),
        "instrument": instr,
        "currentUnits": str(units),
        "clientExtensions": {"tag": tag or f"{ROOT_TAG}JPY-STRENGTH_{instr}_BUY_20261120"},
        "stopLossOrder": {"price": sl},
        "takeProfitOrder": {"price": tp},
    }


class _FakeCore:
    """Records execute_market_trade calls."""

    def __init__(self):
        self.orders = []

    def execute_market_trade(
        self, instrument, action, units, stop_loss, take_profit, *, dry_run=False,
        client_extensions=None,
    ):
        self.orders.append(
            {
                "instrument": instrument,
                "action": action,
                "units": units,
                "sl": stop_loss,
                "tp": take_profit,
                "ce": client_extensions,
            }
        )
        return {"trade_id": "9", "filled_price": "1.0"}


class TestRegimeTier(unittest.TestCase):
    def test_tier_mapping(self):
        self.assertEqual(runner._regime_tier_units("CONSOLIDATION", 1, 4), 1)
        self.assertEqual(runner._regime_tier_units("NEUTRAL", 1, 4), 2)
        self.assertEqual(runner._regime_tier_units("STRONG_MOMENTUM", 1, 4), 4)

    def test_tier_uses_min_lot_multiple(self):
        # demo: min_lot=1000 → NEUTRAL=2000，STRONG=最大
        self.assertEqual(runner._regime_tier_units("CONSOLIDATION", 1000, 5000), 1000)
        self.assertEqual(runner._regime_tier_units("NEUTRAL", 1000, 5000), 2000)
        self.assertEqual(runner._regime_tier_units("STRONG_MOMENTUM", 1000, 5000), 5000)

    def test_tier_neutral_clamped_to_max(self):
        self.assertEqual(runner._regime_tier_units("NEUTRAL", 4, 4), 4)


class TestLadderStep(unittest.TestCase):
    def test_one_level_at_a_time(self):
        self.assertEqual(runner._next_ladder_units(1, 1, 4), 2)
        self.assertEqual(runner._next_ladder_units(2, 1, 4), 4)
        self.assertIsNone(runner._next_ladder_units(4, 1, 4))

    def test_caps_at_target(self):
        # cur=2, ladder next=4 但目标 cap=3 → 3（不越过目标）
        self.assertEqual(runner._next_ladder_units(2, 1, 3), 3)

    def test_non_ladder_current_rounds_up_one_level(self):
        self.assertEqual(runner._next_ladder_units(3, 1, 8), 4)

    def test_demo_scale(self):
        self.assertEqual(runner._next_ladder_units(1000, 1000, 8000), 2000)
        self.assertEqual(runner._next_ladder_units(2000, 1000, 8000), 4000)


class TestTopupScan(unittest.TestCase):
    def _drive(self, trades, regime, p_up, p_down, *, enabled=True, min_lot=1,
               max_lots=4, dry_run=False):
        core = _FakeCore()
        patches = [
            patch.object(runner, "_MC_MIN_POS_ENABLED", enabled),
            patch.object(runner, "_MC_MIN_LOT", min_lot),
            patch.object(runner, "_EFFECTIVE_LOTS", max_lots, create=True),
            patch.object(runner, "_trading_core", core),
            patch.object(runner, "_all_open_trades_snapshot", lambda: list(trades)),
            patch.object(runner, "_get_mc_item", lambda pair: {"regime": regime}),
            patch.object(
                runner, "_fetch_mc_direction_prob",
                lambda pair: {"p_up": p_up, "p_down": p_down},
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        buf = StringIO()
        with redirect_stdout(buf):
            runner._mc_min_position_topup_scan_v31(dry_run)
        return core, buf.getvalue()

    def test_strong_aligned_adds_one_level(self):
        core, out = self._drive([_trade(1, "EUR_JPY", 1)], "STRONG_MOMENTUM", 0.7, 0.3)
        self.assertEqual(len(core.orders), 1)
        self.assertEqual(core.orders[0]["action"], "BUY")
        self.assertEqual(core.orders[0]["units"], 1)  # 1 → 2
        self.assertIn("MC-TOPUP", out)

    def test_step_from_two_to_four(self):
        core, _ = self._drive([_trade(1, "EUR_JPY", 2)], "STRONG_MOMENTUM", 0.7, 0.3)
        self.assertEqual(core.orders[0]["units"], 2)  # 2 → 4

    def test_already_at_cap_no_action(self):
        core, _ = self._drive([_trade(1, "EUR_JPY", 4)], "STRONG_MOMENTUM", 0.7, 0.3)
        self.assertEqual(core.orders, [])

    def test_direction_mismatch_no_action(self):
        core, out = self._drive([_trade(1, "EUR_JPY", 1)], "STRONG_MOMENTUM", 0.3, 0.7)
        self.assertEqual(core.orders, [])
        self.assertIn("不符", out)

    def test_non_strong_regime_no_action(self):
        core, _ = self._drive([_trade(1, "EUR_JPY", 1)], "NEUTRAL", 0.7, 0.3)
        self.assertEqual(core.orders, [])
        core2, _ = self._drive([_trade(1, "EUR_JPY", 1)], "CONSOLIDATION", 0.7, 0.3)
        self.assertEqual(core2.orders, [])

    def test_sell_side_aligned(self):
        core, _ = self._drive([_trade(1, "EUR_JPY", -1)], "STRONG_MOMENTUM", 0.3, 0.7)
        self.assertEqual(core.orders[0]["action"], "SELL")

    def test_switch_off_dormant(self):
        core, _ = self._drive(
            [_trade(1, "EUR_JPY", 1)], "STRONG_MOMENTUM", 0.7, 0.3, enabled=False
        )
        self.assertEqual(core.orders, [])

    def test_missing_sl_tp_skips(self):
        t = _trade(1, "EUR_JPY", 1)
        t["stopLossOrder"] = {}
        t["takeProfitOrder"] = {}
        core, out = self._drive([t], "STRONG_MOMENTUM", 0.7, 0.3)
        self.assertEqual(core.orders, [])
        self.assertIn("缺 SL/TP", out)

    def test_dry_run_no_order(self):
        core, out = self._drive(
            [_trade(1, "EUR_JPY", 1)], "STRONG_MOMENTUM", 0.7, 0.3, dry_run=True
        )
        self.assertEqual(core.orders, [])
        self.assertIn("DRY", out)

    def test_manual_trade_ignored(self):
        t = _trade(1, "EUR_JPY", 1, tag="MANUAL_TAG")
        core, _ = self._drive([t], "STRONG_MOMENTUM", 0.7, 0.3)
        self.assertEqual(core.orders, [])


class TestWiring(unittest.TestCase):
    def test_run_env_declares_keys(self):
        text = (Path(PROJECT_ROOT) / "run.env").read_text(encoding="utf-8")
        self.assertIn("MIN_LOT_STRONG_ENABLED=", text)
        self.assertIn("MIN_LOT_LIVE=", text)
        self.assertIn("MIN_LOT_DEMO=", text)

    def test_v3_reads_switch(self):
        text = (Path(PROJECT_ROOT) / "scheduled_runner_v3.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('_env_or_config("MIN_LOT_STRONG_ENABLED"', text)
        self.assertIn("_mc_min_position_topup_scan_v31", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
