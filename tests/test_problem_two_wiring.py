"""Regression tests for 「问题二」— concurrency cap + MC-conviction lot sizing.

Locks in the 2026-11-22 decisions:

    • 并发上限 = MC regime 的 max_positions（NEUTRAL/AGGRESSIVE=4，CONSOLIDATION=1）。
      v1441 此前**没有实盘并发闸**（只有 pair 级幂等）→ 新增 `_strategy_open_count()`
      + 每周期开仓循环内的并发闸；查询失败 fail-closed（拒绝开新仓）。
    • 每周期最多开 1 笔（间隔一个 15 分钟周期）。
    • 单笔手数 = 名义手数(LIVE_LOT_SIZE=4) × prob-zone(MIN_LOT 注入后 shrink-only)
      → clamp 到整数 [1, 名义手数]，实现「MC 置信度 1..4」。
    • prob-zone 键错配修复：模块内部读 `MIN_LOT`，config 只定义 MIN_LOT_LIVE/DEMO
      → 两个 runner 在调用前注入 MIN_LOT。

Nothing here contacts OANDA and no trading decision is changed.

Run (all three work):
    python tests/test_problem_two_wiring.py
    cd tests && python test_problem_two_wiring.py
    python -m unittest -v tests.test_problem_two_wiring
"""

import os
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config as cfg_mod
import config_bot as cfg_bot
import scheduled_runner_v1441 as runner
from utils.prob_zone_sizing import compute_prob_plan


# ─────────────────────────────────────────────────────────────────────────────
# 1) 配置矩阵 —— 决策的权威副本
# ─────────────────────────────────────────────────────────────────────────────
class TestConfigMatrix(unittest.TestCase):
    def test_config_py_concurrency_cap_is_4_1_4(self):
        self.assertEqual(cfg_mod.MC_MAX_POSITIONS_NEUTRAL, 4)
        self.assertEqual(cfg_mod.MC_MAX_POSITIONS_CONSOLIDATION, 1)
        self.assertEqual(cfg_mod.MC_MAX_POSITIONS_AGGRESSIVE, 4)

    def test_config_bot_mirror_matches_config(self):
        # v3 通过 getattr(_config_bot, "MC_MAX_POSITIONS_*") 读取 → 必须与 config.py 一致。
        self.assertEqual(cfg_bot.MC_MAX_POSITIONS_NEUTRAL, 4)
        self.assertEqual(cfg_bot.MC_MAX_POSITIONS_CONSOLIDATION, 1)
        self.assertEqual(cfg_bot.MC_MAX_POSITIONS_AGGRESSIVE, 4)

    def test_weak_exit_cfg_has_group_gate_and_no_min_hold(self):
        we = cfg_mod.WEAK_STRENGTH_EXIT
        self.assertEqual(we["MIN_GROUP_SIZE"], 2)
        self.assertNotIn("MIN_HOLD_MIN", we)

    def test_prob_zone_exposes_only_min_lot_live_demo(self):
        # 记录键错配根因：模块读 MIN_LOT，config 只给 MIN_LOT_LIVE/DEMO → 需 runner 注入。
        pz = cfg_mod.PROB_ZONE_SIZING
        self.assertIn("MIN_LOT_LIVE", pz)
        self.assertIn("MIN_LOT_DEMO", pz)
        self.assertNotIn("MIN_LOT", pz)


# ─────────────────────────────────────────────────────────────────────────────
# 2) v1441 并发计数 helper
# ─────────────────────────────────────────────────────────────────────────────
def _trade(tid, instrument, tag, units="1000"):
    return {"id": tid, "instrument": instrument, "tag": tag, "currentUnits": units}


class TestStrategyOpenCount(unittest.TestCase):
    def _with_core(self, trades):
        core = SimpleNamespace(get_all_open_trades=lambda: trades)
        patcher = patch.object(runner, "_trading_core", core)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_counts_only_own_jpy_strength_trades(self):
        prefixed = runner.BOT_OWNED_TAG_ROOT + runner._BOT_TAG_SEP + "JPY-STRENGTH_EUR_JPY_SELL_20261122"
        self._with_core(
            [
                _trade("1", "USD_JPY", "JPY-STRENGTH_USD_JPY_BUY_20261122"),
                _trade("2", "EUR_JPY", prefixed, units="-1000"),
                # 非本策略单：不得计入并发数
                _trade("3", "EUR_USD", "USD-STRENGTH_EUR_USD_SELL_20261122"),
                _trade("4", "GBP_USD", ""),
            ]
        )
        self.assertEqual(runner._strategy_open_count(), 2)

    def test_no_own_trades_counts_zero(self):
        self._with_core([_trade("1", "EUR_USD", "MANUAL_ORDER")])
        self.assertEqual(runner._strategy_open_count(), 0)

    def test_query_failure_returns_none_fail_closed(self):
        def _boom():
            raise RuntimeError("oanda down")

        core = SimpleNamespace(get_all_open_trades=_boom)
        with patch.object(runner, "_trading_core", core):
            with redirect_stdout(StringIO()) as buffer:
                self.assertIsNone(runner._strategy_open_count())
        self.assertIn("[MAX_POS] open-trade query failed", buffer.getvalue())


# ─────────────────────────────────────────────────────────────────────────────
# 3) 名义手数解析（LIVE_LOT_SIZE=4 生效）
# ─────────────────────────────────────────────────────────────────────────────
class TestResolveEffectiveLots(unittest.TestCase):
    def test_cli_lots_has_highest_priority(self):
        with patch.object(runner, "_args", SimpleNamespace(live=True, lots=3)):
            lots, source = runner._resolve_effective_lots()
        self.assertEqual(lots, 3)
        self.assertIn("CLI --lots=3", source)

    def test_live_default_comes_from_run_env(self):
        # run.env: LIVE_LOT_SIZE=4 → 名义满仓 = 4（prob-zone 在其内只缩不放大）。
        with patch.object(runner, "_args", SimpleNamespace(live=True, lots=None)):
            lots, source = runner._resolve_effective_lots()
        self.assertEqual(lots, 4)
        self.assertIn("LIVE_LOT_SIZE", source)


# ─────────────────────────────────────────────────────────────────────────────
# 4) MIN_LOT 注入契约 —— prob-zone 内部读 MIN_LOT
# ─────────────────────────────────────────────────────────────────────────────
class TestMinLotInjectionContract(unittest.TestCase):
    """runner 在调用前把 MIN_LOT_LIVE/DEMO 映射为 MIN_LOT；此处锁定该语义。"""

    def test_without_injection_min_lot_defaults_to_one(self):
        # 只给 MIN_LOT_LIVE/DEMO（config 现状），未注入 MIN_LOT → 模块默认 1。
        cfg = {"ENABLED": True, "F_LO": 0.25, "MIN_LOT_LIVE": 1, "MIN_LOT_DEMO": 1000}
        plan = compute_prob_plan(None, "HOLD", cfg, 1000)  # 非法方向 → floor plan
        self.assertEqual(plan["size_units"], max(1, int(1000 * 0.25 + 0.5)))

    def test_injected_min_lot_is_honored(self):
        # 注入后（runner 实际行为）：MIN_LOT=1000 生效，floor 不低于 1000。
        cfg = {
            "ENABLED": True,
            "F_LO": 0.25,
            "MIN_LOT_LIVE": 1,
            "MIN_LOT_DEMO": 1000,
            "MIN_LOT": 1000,
        }
        plan = compute_prob_plan(None, "HOLD", cfg, 1000)
        self.assertEqual(plan["size_units"], 1000)

    def test_shrink_only_never_amplifies(self):
        # 满仓 4 + 空 rec → floor plan（F_LO=0.25）→ 只缩不放大。
        cfg = {"ENABLED": True, "F_LO": 0.25, "MIN_LOT": 1}
        plan = compute_prob_plan(None, "HOLD", cfg, 4)
        self.assertEqual(plan["size_units"], 1)
        self.assertLessEqual(plan["size_scale"], 1.0)


# ─────────────────────────────────────────────────────────────────────────────
# 5) 并发闸 端到端（dry-run，全 mock）
# ─────────────────────────────────────────────────────────────────────────────
def _good_signal(**overrides):
    signal = {
        "pair": "USD_JPY",
        "action": "BUY",
        "entry": 155.0,
        "stop_loss": 154.0,
        "take_profit": 157.0,
        "strength_score": -1.0,
        "risk_reward": 2.0,
        "reasoning": "unit-test",
    }
    signal.update(overrides)
    return signal


class TestConcurrencyCapEndToEnd(unittest.TestCase):
    def _run_cycle(self, open_trades, max_positions=1):
        core = SimpleNamespace(
            oanda_account_id="TEST-ACCOUNT",
            get_all_open_trades=(open_trades if callable(open_trades) else lambda: open_trades),
        )
        patches = [
            patch.object(runner, "_trading_core", core),
            patch.object(runner, "_print_mc_snapshot", lambda: None),
            patch.object(runner, "_validate_and_repair_sltp", lambda *a, **k: None),
            patch.object(runner, "_is_emergency_lock_active_v144", lambda: False),
            patch.object(runner, "with_retry", MagicMock()),
            patch.object(runner, "get_last_signal", lambda: _good_signal()),
            patch.object(runner, "_get_mc_for_pair", lambda *a, **k: {"regime": "NEUTRAL"}),
            patch.object(
                runner,
                "_regime_params",
                lambda mode: {
                    "max_positions": max_positions,
                    "tp_multiplier": 1.0,
                    "exit_tightness": 1.0,
                },
            ),
            patch.object(
                runner,
                "_check_pair_level_strategy_position",
                lambda pair, action: (True, "no JPY-STRENGTH position on pair"),
            ),
            patch.object(runner._config, "ENABLE_MC_BASKET_EXECUTION", False),
            patch.object(runner._config, "POST_EXIT_GATE_ENABLED", False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        buffer = StringIO()
        with redirect_stdout(buffer):
            runner.run_cycle(dry_run=True)
        return buffer.getvalue()

    def test_cap_reached_blocks_new_entry(self):
        existing = [_trade("1", "USD_JPY", "JPY-STRENGTH_USD_JPY_BUY_20261122")]
        output = self._run_cycle(existing, max_positions=1)
        self.assertIn("[MAX_POS]", output)
        self.assertIn("skip new entries", output)
        self.assertIn("Entries blocked: 1", output)
        self.assertIn("Core reason: max_positions(", output)
        # 达到上限 → 不得进入下单/DRY-RUN 校验分支
        self.assertNotIn("[DRY RUN] Signal validated", output)

    def test_room_below_cap_allows_entry(self):
        # 上限 2、在场 1 → 可以开新仓（dry-run 校验通过）。
        existing = [_trade("1", "USD_JPY", "JPY-STRENGTH_USD_JPY_BUY_20261122")]
        output = self._run_cycle(existing, max_positions=2)
        self.assertNotIn("skip new entries", output)
        self.assertIn("[DRY RUN] Signal validated", output)

    def test_query_failure_fails_closed(self):
        def _boom():
            raise RuntimeError("oanda down")

        output = self._run_cycle(_boom, max_positions=4)
        self.assertIn("open-trade query failed (fail-closed)", output)
        self.assertIn("skip new entries", output)
        self.assertNotIn("[DRY RUN] Signal validated", output)


# ─────────────────────────────────────────────────────────────────────────────
# 6) 接线守卫 —— 两个 runner 都注入 MIN_LOT 且 clamp 整数手
# ─────────────────────────────────────────────────────────────────────────────
class TestRunnerWiringPresent(unittest.TestCase):
    def test_both_runners_inject_min_lot_and_clamp_lots(self):
        for name in ("scheduled_runner_v1441.py", "scheduled_runner_v3.py"):
            text = Path(PROJECT_ROOT, name).read_text(encoding="utf-8")
            self.assertIn('"MIN_LOT": _pz_cfg.get(', text, msg=name)
            self.assertIn("max(1, min(int(", text, msg=name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
