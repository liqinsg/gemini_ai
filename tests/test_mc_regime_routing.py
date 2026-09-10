"""
test_mc_regime_routing.py — MC Regime 路由逻辑单元测试
=====================================================

覆盖 scheduled_runner_v1.4 的三种 regime 分支 + 方向冲突过滤 + bypass 开关。
全量 monkeypatch，零真实网络 / 零真实下单。

运行:  cd ~/projects/gemini_ai && pytest tests/test_mc_regime_routing.py -v -s
"""

import os
import sys
from unittest.mock import MagicMock
from types import SimpleNamespace

import pytest
from utils.position_direction import PositionDecision


# ============================================================
# Helper 工厂函数
# ============================================================

def make_candidate(pair="USD_JPY", action="SELL", strength_score=-5.4165,
                   entry=153.350, stop_loss=154.000, take_profit=152.350,
                   risk_reward=1.5, reasoning="unit test signal"):
    """构造符合 runner 期望 schema 的候选信号 dict。"""
    return {
        "pair": pair,
        "action": action,
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "strength_score": strength_score,
        "risk_reward": risk_reward,
        "reasoning": reasoning,
    }

def make_mc_data(regime="🔹 NEUTRAL", p_up=44.0, p_down=56.0, price=153.42):
    """构造 get_latest_mc_local 的返回 dict。"""
    return {
        "regime": regime,
        "p_up": p_up,
        "p_down": p_down,
        "current_price": price,
    }


# ============================================================
# Autouse fixture — 全量 mock + 模块安全导入
# ============================================================

@pytest.fixture(autouse=True)
def mock_runner_env(monkeypatch):
    """在任何 test 函数运行之前, 把 runner 依赖的全部外部点替成假实现。
    
    patch 顺序很关键:
      1. 先设 env vars, 避免 runner 模块级 sys.exit(1) 被触发
      2. 再 import scheduled_runner_v1.4 (argparse + 所有下游 import 此时执行)
      3. 再在 sr.* / sr._strategy.* / sr._risk.* 上逐个 patch
    """

    # --- Step 1: env vars + sys.argv, 让 runner 模块级代码安全通过 ---
    monkeypatch.setenv("OANDA_ACCOUNT_ID_1", "FAKE_ACCT_1")
    monkeypatch.setenv("OANDA_ACCOUNT_ID_2", "FAKE_ACCT_2")
    monkeypatch.setenv("OANDA_ACCOUNT_ID_3", "FAKE_ACCT_3")
    monkeypatch.setenv("OANDA_ACCOUNT_ID_4", "FAKE_ACCT_4")
    monkeypatch.setenv("POST_EXIT_SHADOW_LOG_PATH", "/tmp/test_shadow.jsonl")
    monkeypatch.setattr(sys, "argv", ["pytest", "--account", "1"])

    # --- Step 2: 清掉 sys.modules 里可能残留的 runner (若前序测试 import 过) ---
    for _mod in list(sys.modules):
        if "scheduled_runner_v1" in _mod:
            del sys.modules[_mod]

    # --- Step 3: import runner ---
    import scheduled_runner_v1.4 as sr

    # --- Step 4: config 可覆盖项 ---
    monkeypatch.setattr(sr._config, "MC_REGIME_ENABLED", True)
    monkeypatch.setattr(sr._config, "MC_REGIME_STRENGTH_HURDLE_CONSOLIDATION", 0.05)
    monkeypatch.setattr(sr._config, "POST_EXIT_SHADOW_MODE", False)
    monkeypatch.setattr(sr._config, "TRADE_PAIRS", ["USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY"])

    # --- Step 5: 策略层 — 用容器 dict 传信号, 每个 test 填自己的 ---
    _container = {"last": None, "all": []}

    def _mock_analyze(scores=None):
        return "fake report", None

    def _mock_get_last():
        return _container["last"]

    def _mock_get_top(n=2):
        valid = _container["all"] or []
        return sorted(valid, key=lambda x: abs(x["strength_score"]), reverse=True)[:n]

    # runner 通过 from custom_strategy_v1 import analyze_custom_strategy, get_last_signal 绑定了别名
    # 同时也有 import custom_strategy_v1 as _strategy
    monkeypatch.setattr(sr, "analyze_custom_strategy", _mock_analyze)
    monkeypatch.setattr(sr, "get_last_signal", _mock_get_last)
    monkeypatch.setattr(sr._strategy, "analyze_custom_strategy", _mock_analyze)
    monkeypatch.setattr(sr._strategy, "get_last_signal", _mock_get_last)
    monkeypatch.setattr(sr._strategy, "get_top_signals", _mock_get_top)
    monkeypatch.setattr(sr._strategy, "build_strength_matrix", lambda: {"USD_JPY": 1.0})

    # --- Step 6: 风险层 ---
    monkeypatch.setattr(sr._risk, "enforce_global_invalidation_sweep", lambda m: [])
    monkeypatch.setattr(sr._risk, "manage_open_positions", lambda m: set())
    _fake_cluster = MagicMock()
    _fake_cluster.to_dict.return_value = {"fake": True}
    monkeypatch.setattr(sr._risk, "new_cluster_from_fill", lambda c, f: _fake_cluster)
    mock_save_cluster = MagicMock()
    monkeypatch.setattr(sr._risk, "save_cluster_data", mock_save_cluster)

    # --- Step 7: MC loader — 默认 NEUTRAL, 可 per-test 覆盖 ---
    _mc_registry = {}

    def _mock_get_mc(pair=None, day=True):
        if pair and pair in _mc_registry:
            return _mc_registry[pair]
        return make_mc_data("🔹 NEUTRAL")

    monkeypatch.setattr(sr, "get_latest_mc_local", _mock_get_mc)

    # --- Step 8: 方向判断 + OANDA 下单 ---
    mock_open_order = MagicMock(return_value={
        "status": "SUCCESS", "order_id": "FAKE_001",
        "filled_price": 153.342, "trade_id": "FAKE_T1",
    })
    monkeypatch.setattr(sr, "open_oanda_order", mock_open_order)
    monkeypatch.setattr(
        sr, "resolve_and_prepare_entry",
        lambda p, a: PositionDecision.CLOSE_THEN_ENTER,
    )

    # --- Step 9: post-exit gate — 默认全放行, quiet ---
    monkeypatch.setattr(
        sr.PostExitGate, "check_risk_before_open",
        lambda **kw: (True, {"verdict": "PASS", "effective_hurdle": 1.0,
                             "m_decay": 1.0, "m_streak": 1.0,
                             "regime_reset_triggered": True}),
    )
    monkeypatch.setattr(sr.PostExitGate, "record_exit", MagicMock())
    monkeypatch.setattr(
        sr.PostExitTracker, "get_context",
        lambda self, pair: {"closed_at": None, "close_reason": "",
                            "consecutive_failures": 0, "elapsed_hours": 0.0},
    )

    yield SimpleNamespace(
        sr=sr,
        signals=_container,
        mc_registry=_mc_registry,
        mock_open_order=mock_open_order,
        mock_save_cluster=mock_save_cluster,
        fake_cluster=_fake_cluster,
    )


# ============================================================
# Test 矩阵
# ============================================================

class TestConsolidationBlock:
    """CONSOLIDATION (cautious) 分支 — 强度低于门槛时整个 cycle return, 不下单。"""

    def test_strength_below_hurdle_blocks_entry(self, mock_runner_env, monkeypatch, capsys):
        """门槛设 100.0 (远超 strength_score=5.4165), 应拦截且无任何下单。"""
        monkeypatch.setattr(mock_runner_env.sr._config,
                            "MC_REGIME_STRENGTH_HURDLE_CONSOLIDATION", 100.0)
        monkeypatch.setattr(mock_runner_env.sr._config, "MC_REGIME_ENABLED", True)

        mock_runner_env.mc_registry["USD_JPY"] = make_mc_data(
            regime="⏳ D CONSOLIDATION RANGE", p_up=44.0, p_down=56.0
        )
        cand = make_candidate(pair="USD_JPY", action="SELL", strength_score=-5.4165)
        mock_runner_env.signals["last"] = cand
        mock_runner_env.signals["all"] = [cand]

        mock_runner_env.sr.run_cycle(dry_run=False)
        out = capsys.readouterr().out

        assert mock_runner_env.mock_open_order.call_count == 0, (
            f"order should NOT be placed, but was called "
            f"{mock_runner_env.mock_open_order.call_count} time(s)"
        )
        assert mock_runner_env.mock_save_cluster.call_count == 0
        assert "🚫 CONSOLIDATION 谨慎" in out
        assert "strength_score=5.4165 < 100.0" in out
        assert "本周期不开仓" in out

    def test_strength_above_hurdle_allows_entry(self, mock_runner_env, monkeypatch, capsys):
        """门槛 0.03, strength_score=5.4165 远大于它 — 正常下单。"""
        monkeypatch.setattr(mock_runner_env.sr._config,
                            "MC_REGIME_STRENGTH_HURDLE_CONSOLIDATION", 0.03)
        monkeypatch.setattr(mock_runner_env.sr._config, "MC_REGIME_ENABLED", True)

        mock_runner_env.mc_registry["USD_JPY"] = make_mc_data(
            regime="⏳ D CONSOLIDATION RANGE"
        )
        cand = make_candidate(strength_score=-5.4165)
        mock_runner_env.signals["last"] = cand
        mock_runner_env.signals["all"] = [cand]

        mock_runner_env.sr.run_cycle(dry_run=False)
        out = capsys.readouterr().out

        assert mock_runner_env.mock_open_order.call_count == 1
        assert mock_runner_env.mock_save_cluster.call_count == 1
        assert "CONSOLIDATION 通过强度门槛" in out


class TestStrongMomentumAggressive:
    """STRONG MOMENTUM (aggressive) 分支 — 取 top2 + 方向兼容过滤。"""

    def test_same_direction_top2_two_clusters(self, mock_runner_env, monkeypatch, capsys):
        """top1=USD_JPY SELL, top2=GBP_JPY SELL — 同向不冲突, 应下 2 单。"""
        monkeypatch.setattr(mock_runner_env.sr._config, "MC_REGIME_ENABLED", True)

        mock_runner_env.mc_registry["USD_JPY"] = make_mc_data(
            regime="⚡ D STRONG MOMENTUM", p_up=38.8, p_down=61.2
        )
        mock_runner_env.mc_registry["GBP_JPY"] = make_mc_data(
            regime="⚡ D STRONG MOMENTUM"
        )
        s1 = make_candidate("USD_JPY", "SELL", strength_score=-5.4165)
        s2 = make_candidate("GBP_JPY", "SELL", strength_score=-5.0225)
        mock_runner_env.signals["last"] = s1
        mock_runner_env.signals["all"] = [s1, s2]

        mock_runner_env.sr.run_cycle(dry_run=False)
        out = capsys.readouterr().out

        # --- Debug: 如果 call_count != 2, 打印跳过原因 ---
        if mock_runner_env.mock_open_order.call_count != 2:
            for needle in ("跳过", "already under dynamic", "风控拦截",
                           "SKIP_SAME_DIRECTION", "SKIP_HEDGED",
                           "POSITION ERROR", "NETWORK ERROR",
                           "取 top2 失败"):
                hits = [ln for ln in out.splitlines() if needle in ln]
                if hits:
                    print(f"\n[DEBUG SKIP REASON] needle='{needle}':")
                    for h in hits:
                        print(f"  {h}")

        assert mock_runner_env.mock_open_order.call_count == 2, (
            f"expected 2 orders, got {mock_runner_env.mock_open_order.call_count}\n"
            f"--- full output ---\n{out}"
        )
        assert mock_runner_env.mock_save_cluster.call_count == 2
        assert "候选对" in out
        assert "USD_JPY" in out and "GBP_JPY" in out

        # 验证 save_cluster_data 被 pair 参数正确调用
        saved_pairs = {
            args[0] for args, _ in mock_runner_env.mock_save_cluster.call_args_list
        }
        assert saved_pairs == {"USD_JPY", "GBP_JPY"}

    def test_direction_conflict_filters_top2(self, mock_runner_env, monkeypatch, capsys):
        """top1=USD_JPY SELL, top2=GBP_JPY BUY — 方向冲突, top2 被过滤, 只下 1 单。"""
        monkeypatch.setattr(mock_runner_env.sr._config, "MC_REGIME_ENABLED", True)

        mock_runner_env.mc_registry["USD_JPY"] = make_mc_data(
            regime="⚡ D STRONG MOMENTUM"
        )
        mock_runner_env.mc_registry["GBP_JPY"] = make_mc_data(
            regime="⚡ D STRONG MOMENTUM"
        )
        s1 = make_candidate("USD_JPY", "SELL", strength_score=-5.4165)
        s2 = make_candidate("GBP_JPY", "BUY", strength_score=+5.0225)
        mock_runner_env.signals["last"] = s1
        mock_runner_env.signals["all"] = [s1, s2]

        mock_runner_env.sr.run_cycle(dry_run=False)
        out = capsys.readouterr().out

        assert mock_runner_env.mock_open_order.call_count == 1
        assert mock_runner_env.mock_save_cluster.call_count == 1
        assert "跳过 GBP_JPY" in out
        assert "方向冲突" in out

        saved_pairs = {
            args[0] for args, _ in mock_runner_env.mock_save_cluster.call_args_list
        }
        assert saved_pairs == {"USD_JPY"}

    def test_get_top_signals_raises_fallback_to_top1(self, mock_runner_env, monkeypatch, capsys):
        """aggressive 模式下 get_top_signals() 抛异常 — 应 fallback 到 top1, 不崩溃。"""
        monkeypatch.setattr(mock_runner_env.sr._config, "MC_REGIME_ENABLED", True)

        def _boom(n=2):
            raise RuntimeError("intentional test explosion")
        monkeypatch.setattr(mock_runner_env.sr._strategy, "get_top_signals", _boom)

        mock_runner_env.mc_registry["USD_JPY"] = make_mc_data(
            regime="⚡ D STRONG MOMENTUM"
        )
        s1 = make_candidate("USD_JPY", "SELL", strength_score=-5.4165)
        mock_runner_env.signals["last"] = s1
        mock_runner_env.signals["all"] = [s1]

        mock_runner_env.sr.run_cycle(dry_run=False)
        out = capsys.readouterr().out

        assert mock_runner_env.mock_open_order.call_count == 1
        assert "取 top2 失败" in out
        assert "回退 top1" in out


class TestNeutralMode:
    """NEUTRAL (normal) 分支 — 保持 top1, 不做任何 regime 额外处理。"""

    def test_neutral_normal_flow(self, mock_runner_env, monkeypatch, capsys):
        monkeypatch.setattr(mock_runner_env.sr._config, "MC_REGIME_ENABLED", True)

        mock_runner_env.mc_registry["USD_JPY"] = make_mc_data(regime="🔹 NEUTRAL")
        s1 = make_candidate("USD_JPY", "SELL")
        mock_runner_env.signals["last"] = s1
        mock_runner_env.signals["all"] = [s1]

        mock_runner_env.sr.run_cycle(dry_run=False)
        out = capsys.readouterr().out

        assert mock_runner_env.mock_open_order.call_count == 1
        assert mock_runner_env.mock_save_cluster.call_count == 1
        assert "mode=normal" in out
        assert "CONSOLIDATION 谨慎" not in out


class TestRegimeBypassSwitch:
    """MC_REGIME_ENABLED=False — 完全旁路, mode 强制 normal。"""

    def test_switch_off_ignores_consolidation(self, mock_runner_env, monkeypatch, capsys):
        """regime 是 CONSOLIDATION, 但开关关了 — 应像 NEUTRAL 一样正常下单。"""
        monkeypatch.setattr(mock_runner_env.sr._config, "MC_REGIME_ENABLED", False)

        mock_runner_env.mc_registry["USD_JPY"] = make_mc_data(
            regime="⏳ D CONSOLIDATION RANGE"
        )
        s1 = make_candidate("USD_JPY", "SELL", strength_score=-0.01)  # 很小, 若开关开了会被卡
        mock_runner_env.signals["last"] = s1
        mock_runner_env.signals["all"] = [s1]

        mock_runner_env.sr.run_cycle(dry_run=False)
        out = capsys.readouterr().out

        assert mock_runner_env.mock_open_order.call_count == 1
        assert mock_runner_env.mock_save_cluster.call_count == 1
        assert "mode=normal" in out
        assert "🚫" not in out
        assert "CONSOLIDATION 谨慎" not in out


class TestRegimeEdgeCases:
    """regime 字符串边界情况。"""

    @pytest.mark.parametrize("regime_str,expected_mode", [
        ("", "normal"),
        (None, "normal"),
        ("N/A", "normal"),
        ("NO_LOCAL_MC_DATA", "normal"),
        ("⏳ D CONSOLIDATION RANGE", "cautious"),
        ("💤 consolidation range", "cautious"),          # 小写
        ("⚡ H4 STRONG MOMENTUM", "aggressive"),
        ("⚡ STRONG MOMENTUM H4", "aggressive"),           # 顺序颠倒
        ("STRONG MOMENTUM ONLY", "aggressive"),
        ("🔹 D NEUTRAL", "normal"),
    ])
    def test_regime_policy_mapping(self, mock_runner_env, regime_str, expected_mode):
        """直接测 _regime_policy 的映射逻辑 (不跑整个 cycle, 更快)。"""
        assert mock_runner_env.sr._regime_policy(regime_str) == expected_mode

    def test_missing_mc_data_falls_back_normal(self, mock_runner_env, monkeypatch, capsys):
        """get_latest_mc_local 返回 None → mc_regime='NO_LOCAL_MC_DATA' → mode=normal。"""
        monkeypatch.setattr(mock_runner_env.sr, "get_latest_mc_local",
                            lambda pair=None, day=True: None)

        s1 = make_candidate("USD_JPY", "SELL")
        mock_runner_env.signals["last"] = s1
        mock_runner_env.signals["all"] = [s1]

        mock_runner_env.sr.run_cycle(dry_run=False)
        out = capsys.readouterr().out

        assert mock_runner_env.mock_open_order.call_count == 1
        assert "NO_LOCAL_MC_DATA" in out
        assert "mode=normal" in out

    def test_missing_regime_key_falls_back_normal(self, mock_runner_env, monkeypatch, capsys):
        """MC dict 有数据但没 regime key → regime=N/A → mode=normal。"""
        def _no_regime(pair=None, day=True):
            return {"p_up": 44.0, "p_down": 56.0}  # no "regime" key
        monkeypatch.setattr(mock_runner_env.sr, "get_latest_mc_local", _no_regime)

        s1 = make_candidate("USD_JPY", "SELL")
        mock_runner_env.signals["last"] = s1
        mock_runner_env.signals["all"] = [s1]

        mock_runner_env.sr.run_cycle(dry_run=False)
        out = capsys.readouterr().out

        assert mock_runner_env.mock_open_order.call_count == 1
        assert "mode=normal" in out


class TestTPMultiplier:
    """验证 TP 倍率在三种 regime 下是否正确应用 (SELL vs BUY 方向都测)。"""

    @pytest.mark.parametrize("mode_label,regime_str,tp_mult,should_log", [
        ("cautious", "⏳ D CONSOLIDATION RANGE", 0.8, True),
        ("normal", "🔹 NEUTRAL", 1.5, True),
        ("aggressive", "⚡ D STRONG MOMENTUM", 1.0, False),  # ×1.0 → skip, 不打日志
    ])
    def test_tp_multiplier_applied(self, mock_runner_env, monkeypatch, capsys,
                                    mode_label, regime_str, tp_mult, should_log):
        """每 mode 分别设 TP_MULTIPLIER, 跑一个 SELL + BUY 候选, 验证 take_profit 调整。"""
        monkeypatch.setattr(mock_runner_env.sr._config,
                            f"MC_TP_MULTIPLIER_{mode_label.upper()}", tp_mult)
        monkeypatch.setattr(mock_runner_env.sr._config,
                            "MC_REGIME_STRENGTH_HURDLE_CONSOLIDATION", 0.01)

        mock_runner_env.mc_registry["USD_JPY"] = make_mc_data(regime=regime_str)
        mock_runner_env.mc_registry["EUR_JPY"] = make_mc_data(regime=regime_str)

        s_sell = make_candidate("USD_JPY", "SELL", entry=153.400, stop_loss=154.000,
                                take_profit=152.400, strength_score=-4.5)
        s_buy = make_candidate("EUR_JPY", "BUY", entry=163.400, stop_loss=162.800,
                               take_profit=164.400, strength_score=+4.3)

        if mode_label == "cautious":
            # cautious 限 top1
            mock_runner_env.signals["last"] = s_sell
            mock_runner_env.signals["all"] = [s_sell, s_buy]
        else:
            # normal / aggressive 允许多个, 两个同向都保留
            mock_runner_env.signals["last"] = s_sell
            mock_runner_env.signals["all"] = [s_sell, s_buy]

        mock_runner_env.sr.run_cycle(dry_run=False)
        out = capsys.readouterr().out

        # 基础断言: 至少有 1 个下单 (cautious=1, normal/aggressive=2)
        n_expected = 1 if mode_label == "cautious" else 2
        assert mock_runner_env.mock_open_order.call_count == n_expected

        # TP 调整日志验证
        if should_log:
            assert "[MC TP]" in out, f"{mode_label} 应打印 [MC TP] 但没有"
        else:
            assert "[MC TP]" not in out, f"{mode_label} TP×1.0 不应打印 [MC TP]"

        # 验证传给 open_oanda_order 的 cand 里 TP 确实改了
        for cand_arg, _kw in mock_runner_env.mock_open_order.call_args_list:
            cand_dict = cand_arg[0] if isinstance(cand_arg, tuple) else cand_arg
            pair = cand_dict["pair"]
            entry = cand_dict["entry"]
            sl = cand_dict["stop_loss"]
            actual_tp = cand_dict["take_profit"]

            if should_log and tp_mult != 1.0:
                risk_dist = abs(entry - sl)
                if cand_dict["action"].upper() == "SELL":
                    expected_tp = round(entry - risk_dist * tp_mult, 5)
                else:
                    expected_tp = round(entry + risk_dist * tp_mult, 5)
                assert actual_tp == expected_tp, (
                    f"{pair}: TP mismatch — expected {expected_tp}, got {actual_tp}"
                )

    def test_tp_mult_one_preserves_original(self, mock_runner_env, monkeypatch, capsys):
        """aggressive 默认 ×1.0 — TP 不应修改, 无 [MC TP] 日志。"""
        monkeypatch.setattr(mock_runner_env.sr._config,
                            "MC_TP_MULTIPLIER_AGGRESSIVE", 1.0)

        mock_runner_env.mc_registry["USD_JPY"] = make_mc_data(
            regime="⚡ D STRONG MOMENTUM"
        )
        s1 = make_candidate("USD_JPY", "SELL", entry=153.400, stop_loss=154.000,
                            take_profit=152.400, strength_score=-5.4)
        mock_runner_env.signals["last"] = s1
        mock_runner_env.signals["all"] = [s1]

        mock_runner_env.sr.run_cycle(dry_run=False)
        out = capsys.readouterr().out

        assert "[MC TP]" not in out
        # 直接验证传给 open_oanda_order 的 TP 就是原始值
        cand_arg = mock_runner_env.mock_open_order.call_args.args[0]
        assert cand_arg["take_profit"] == 152.400


class TestNeutralMultiPosition:
    """验证 NEUTRAL 模式能按 MC_MAX_POSITIONS_NEUTRAL 开多单 + 方向兼容过滤。"""

    def test_neutral_take_top3_same_direction(self, mock_runner_env, monkeypatch, capsys):
        """NEUTRAL 模式下 3 个同向候选 — 应全部通过方向过滤, 开 3 单。"""
        monkeypatch.setattr(mock_runner_env.sr._config, "MC_REGIME_ENABLED", True)
        monkeypatch.setattr(mock_runner_env.sr._config, "MC_MAX_POSITIONS_NEUTRAL", 3)

        mock_runner_env.mc_registry["USD_JPY"] = make_mc_data(regime="🔹 NEUTRAL")
        mock_runner_env.mc_registry["GBP_JPY"] = make_mc_data(regime="🔹 NEUTRAL")
        mock_runner_env.mc_registry["EUR_JPY"] = make_mc_data(regime="🔹 NEUTRAL")

        s1 = make_candidate("USD_JPY", "SELL", strength_score=-5.4)
        s2 = make_candidate("GBP_JPY", "SELL", strength_score=-4.8)
        s3 = make_candidate("EUR_JPY", "SELL", strength_score=-4.2)

        mock_runner_env.signals["last"] = s1
        mock_runner_env.signals["all"] = [s1, s2, s3]

        mock_runner_env.sr.run_cycle(dry_run=False)
        out = capsys.readouterr().out

        assert mock_runner_env.mock_open_order.call_count == 3, (
            f"NEUTRAL max_pos=3 同向应开 3 单, 实际 {mock_runner_env.mock_open_order.call_count}\n"
            f"{out}"
        )
        assert "NEUTRAL → 候选对 (3)" in out
        assert "USD_JPY" in out and "GBP_JPY" in out and "EUR_JPY" in out

    def test_neutral_filter_direction_conflict(self, mock_runner_env, monkeypatch, capsys):
        """NEUTRAL 模式 top3 里有一个方向相反 — 应被过滤, 只开 2 单。"""
        monkeypatch.setattr(mock_runner_env.sr._config, "MC_REGIME_ENABLED", True)
        monkeypatch.setattr(mock_runner_env.sr._config, "MC_MAX_POSITIONS_NEUTRAL", 3)

        mock_runner_env.mc_registry["USD_JPY"] = make_mc_data(regime="🔹 NEUTRAL")
        mock_runner_env.mc_registry["GBP_JPY"] = make_mc_data(regime="🔹 NEUTRAL")
        mock_runner_env.mc_registry["EUR_JPY"] = make_mc_data(regime="🔹 NEUTRAL")

        s1 = make_candidate("USD_JPY", "SELL", strength_score=-5.4)
        s2 = make_candidate("GBP_JPY", "SELL", strength_score=-4.8)
        s3 = make_candidate("EUR_JPY", "BUY", strength_score=+4.2)  # 冲突

        mock_runner_env.signals["last"] = s1
        mock_runner_env.signals["all"] = [s1, s2, s3]

        mock_runner_env.sr.run_cycle(dry_run=False)
        out = capsys.readouterr().out

        assert mock_runner_env.mock_open_order.call_count == 2
        assert "跳过 EUR_JPY" in out
        assert "方向冲突" in out