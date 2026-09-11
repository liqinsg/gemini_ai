# scheduled_runner_v1.4.1.py
"""
Scheduled Runner — JPY Strength Strategy
==========================================

v1.4 新增:
    • MC Regime gating — 根据 MC 预测的 regime 调整开仓策略
    CONSOLIDATION  → cautious  只卡信号强度 (|strength_score| ≥ MC_REGIME_STRENGTH_HURDLE_CONSOLIDATION)
        NEUTRAL        → normal    原始行为 (top1)
    STRONG MOMENTUM→ aggressive 取 top2, 过滤方向冲突后各开一仓
    • 多账户支持 — 通过 --account 选择 OANDA account (1/2/3/4)

运行方式:
    python scheduled_runner_v1.4.py                      # 默认 account 1
    python scheduled_runner_v1.4.py --account 2          # account 2    
    python scheduled_runner_v1.4.py -a 3                 # account 3 (短格式)
    python scheduled_runner_v1.4.py --dry-run            # scan and read positions without trading

.env 必须对应:
    OANDA_ACCOUNT_ID_1 / OANDA_ACCOUNT_ID_2 / OANDA_ACCOUNT_ID_3 / OANDA_ACCOUNT_ID_4

关键配置 (config.py):
    MC_REGIME_ENABLED = True                            # 总开关, False → 所有 regime 逻辑旁路
    MC_REGIME_STRENGTH_HURDLE_CONSOLIDATION = 0.05       # cautious 模式强度门槛 (量纲同 MIN_MARKET_STRENGTH=0.03)
    POST_EXIT_SHADOW_MODE = True                         # shadow gate 观测模式, 不影响实盘
    MIN_DOMINANCE_RATIO = 1.5                            # JPY cross 方向一致性门槛 (不动)

MC regime 读取来源: utils/mc_loader_local.py 的 get_latest_mc_local()
    匹配关键字: 'CONSOLIDATION' → cautious, 'STRONG MOMENTUM' → aggressive, 其余 → normal
"""

import sys
import time
import argparse
from datetime import datetime, timezone
from pathlib import Path

import config as _config

_account_map = {
    1: "OANDA_ACCOUNT_ID_1",
    2: "OANDA_ACCOUNT_ID_2",
    3: "OANDA_ACCOUNT_ID_3",
    4: "OANDA_ACCOUNT_ID_4",
}

_parser = argparse.ArgumentParser(description="JPY Strength Strategy — pick OANDA profile")
_parser.add_argument(
    "--profile",
    "-p",
    "--account",
    "-a",
    dest="profile",
    type=int,
    default=1,
    choices=[1, 2, 3, 4],
    help="OANDA profile number (1=default, 2/3/4=alternate)",
)
_parser.add_argument(
    "--dry-run",
    action="store_true",
    help="scan and read positions without opening, closing, or modifying orders",
)
_args, _ = _parser.parse_known_args()

_key = _account_map[_args.profile]
_val = getattr(_config, _key, "")
if not _val:
    print(f"[PROFILE] ERROR: env var {_key} not set — abort. Add it to .env or run.env first.")
    sys.exit(1)
_config.OANDA_ACCOUNT_ID = _val

from config import (
    CHECK_INTERVAL_MINUTES,
    MIN_VALID_PAIRS_TO_TRADE,
    RISK_LEVEL,
    RISK_PROFILE,
    POST_EXIT_GATE_ENABLED,
    POST_EXIT_GATE_SHADOW,
    ALIGNMENT_THRESHOLD,
    DYNAMIC_RISK_TIMEFRAME,
    TP_RATIO,
    SL_RATIO,
)

import custom_strategy_v1 as _strategy
from custom_strategy_v1 import analyze_custom_strategy, get_last_signal
from utils import execute_market_trade
from utils.schemas import TradeSignal
from utils.position_direction import (
    PositionDecision,
    PositionDirectionError,
    get_position_direction,
    resolve_signal_vs_position,
    resolve_and_prepare_entry,
)
from retry import with_retry

from utils.mc_loader_local import get_latest_mc_local

from state.post_exit_context import PostExitTracker
from utils.post_exit_gate import PostExitGate

from utils import risk_integration as _risk
from utils.risk_integration import ENABLE_DYNAMIC_RISK_MANAGER
from utils.oanda_execution import open_oanda_order
from utils.dynamic_risk_manager import ActionType, RiskStateEnum
from utils.logging_utils import get_logger

import json
import os

_log = get_logger("scheduled_runner_v1.4.1")

POST_EXIT_SHADOW_LOG_PATH = os.environ.get(
    "POST_EXIT_SHADOW_LOG_PATH", "logs/post_exit_gate_shadow.jsonl"
)


def _log_shadow(record: dict) -> bool:
    """Append one JSON record to the post-exit shadow log. Never raises."""
    try:
        path = Path(POST_EXIT_SHADOW_LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str, sort_keys=True) + "\n")
        return True
    except Exception as e:
        print(f"  [POST_EXIT_SHADOW] Log write failed (non-fatal): {e}")
        return False


# === MC Regime → 交易模式映射 (新增) ===
def _regime_policy(mc_regime: str) -> str:
    """Map MC regime string → trading mode.
    返回 'cautious' / 'aggressive' / 'normal'。
    匹配关键字, 兼容 '🔹 D NEUTRAL' / '⏳ D CONSOLIDATION RANGE' /
    '⚡ H4 STRONG MOMENTUM' 这种带 emoji + timeframe 前缀的格式。
    """
    reg = (mc_regime or "").upper()
    if "CONSOLIDATION" in reg:
        return "cautious"
    if "STRONG" in reg and "MOMENTUM" in reg:
        return "aggressive"
    return "normal"  # NEUTRAL 或未知


# === MC Regime shadow multiplier (RFC v4.2 Section 11, observation-only) ===
# v1.4 的 live-gating (cautious/aggressive) 是独立的实验路径, 不受此 helper 影响。
# 此 helper 仅把 RFC v4.2 定义的 shadow_regime_multiplier 计算出来并写入 shadow log,
# 便于未来对比 "v1.4 live-gating 效果" vs "纯 shadow multiplier 效果"。
MC_REGIME_MULTIPLIER_SHADOW = {
    "STRONG_MOMENTUM": 0.90,
    "NEUTRAL":         1.00,
    "CONSOLIDATION":   1.10,
}


def _regime_shadow_multiplier(mc_regime: str) -> float:
    """根据 MC regime 字符串返回 RFC v4.2 定义的 shadow multiplier。
    匹配关键字, 兼容 '🔹 D NEUTRAL' / '⏳ D CONSOLIDATION RANGE' /
    '⚡ D STRONG MOMENTUM' 这种带 emoji + timeframe 前缀的格式。
    未知 regime 默认 1.00 (中性, 不增不减)。
    此值仅供 shadow log 记录, 不参与任何 live 决策。
    """
    reg = (mc_regime or "").upper()
    if "CONSOLIDATION" in reg:
        return MC_REGIME_MULTIPLIER_SHADOW["CONSOLIDATION"]
    if "STRONG" in reg and "MOMENTUM" in reg:
        return MC_REGIME_MULTIPLIER_SHADOW["STRONG_MOMENTUM"]
    return MC_REGIME_MULTIPLIER_SHADOW["NEUTRAL"]


def _regime_params(mode: str) -> dict:
    """返回指定 mode 的参数 dict (从 config 读, 零计算)。"""
    return {
        "max_positions": {
            "cautious": _config.MC_MAX_POSITIONS_CONSOLIDATION,
            "normal": _config.MC_MAX_POSITIONS_NEUTRAL,
            "aggressive": _config.MC_MAX_POSITIONS_AGGRESSIVE,
        }.get(mode, 1),
        "tp_multiplier": {
            "cautious": _config.MC_TP_MULTIPLIER_CONSOLIDATION,
            "normal": _config.MC_TP_MULTIPLIER_NEUTRAL,
            "aggressive": _config.MC_TP_MULTIPLIER_AGGRESSIVE,
        }.get(mode, 1.0),
        "exit_tightness": {
            "cautious": _config.MC_EXIT_TIGHTNESS_CONSOLIDATION,
            "normal": _config.MC_EXIT_TIGHTNESS_NEUTRAL,
            "aggressive": _config.MC_EXIT_TIGHTNESS_AGGRESSIVE,
        }.get(mode, 1.0),
    }


def _is_jpy_cross(pair: str) -> bool:
    """判断是否为 JPY cross (兼容 'USD_JPY' 和 'USDJPY=X' 两种格式)。"""
    p = pair.upper().replace("=X", "").replace("_", "")
    return p.endswith("JPY")


def _jpy_cross_direction_compatible(s1: dict, s2: dict) -> bool:
    """两个候选信号是否方向兼容。
    - 都是 JPY cross 时, action 必须同向 (都 BUY 或都 SELL),
      避免 long USDJPY + short AUDJPY 这种对冲式组合。
    - 否则不限制。
    """
    if not (_is_jpy_cross(s1["pair"]) and _is_jpy_cross(s2["pair"])):
        return True
    return s1["action"].upper() == s2["action"].upper()


def _print_mc_snapshot():
    """打印所有 TRADE_PAIRS 的当日 MC regime 快照。
    仅供观察, 不参与任何决策。
    """
    print("\n  === MC DAILY REGIME SNAPSHOT ===")
    for pair in _config.TRADE_PAIRS:
        try:
            mc = get_latest_mc_local(pair=pair, day=True)
            if mc:
                regime = mc.get("regime", "N/A")
                p_up = mc.get("p_up", "?")
                p_down = mc.get("p_down", "?")
                price = mc.get("current_price", mc.get("expected_price", "?"))
                print(
                    f"  {pair:10s} | regime={regime} | P(UP)={p_up}% P(DOWN)={p_down}% | last={price}"
                )
            else:
                print(f"  {pair:10s} | [No local MC data]")
        except Exception as e:
            print(f"  {pair:10s} | [MC load error: {e}]")
    print("  === END MC SNAPSHOT ===\n")


def run_cycle(dry_run=None):
    if dry_run is None:
        dry_run = _args.dry_run

    profile = RISK_PROFILE[RISK_LEVEL]
    post_exit_tracker = PostExitTracker()
    print(
        f"\n[{datetime.now().isoformat()}] === JPY Strength Scan | Risk Level: {RISK_LEVEL} ==="
    )
    print(
        f"  [RISK] Dynamic risk manager: {'ENABLED' if ENABLE_DYNAMIC_RISK_MANAGER else 'DISABLED'}"
    )

    _print_mc_snapshot()

    cycle_strength_matrix = None
    if ENABLE_DYNAMIC_RISK_MANAGER:
        try:
            cycle_strength_matrix = _strategy.build_strength_matrix()
        except Exception as strength_error:
            print(
                f"  [THESIS-OBSERVATION] Strength matrix unavailable: {strength_error}"
            )

    # --- Phase A0: global kill-switch sweep ---
    if ENABLE_DYNAMIC_RISK_MANAGER and not dry_run:
        swept_instruments = _risk.enforce_global_invalidation_sweep(
            cycle_strength_matrix
        )
        if swept_instruments:
            print(
                f"  [RISK] Global sweep flattened (untracked-or-tracked): {sorted(swept_instruments)}"
            )

    # --- Phase A: manage existing risk-managed positions ---
    managed_instruments = (
        [] if dry_run else _risk.manage_open_positions(cycle_strength_matrix)
    )
    if managed_instruments is None:
        print(
            "  [RISK] Managed-position state unavailable — aborting cycle before entry evaluation."
        )
        return
    if ENABLE_DYNAMIC_RISK_MANAGER:
        if managed_instruments:
            print(f"  [RISK] Currently managing: {sorted(managed_instruments)}")
        else:
            print(
                "  [RISK] No instruments currently under dynamic risk management. "
                "(Note: pre-existing OANDA positions opened before this pair was first "
                "entered through this risk-managed flow are NOT automatically adopted — "
                "only positions this runner itself opened and registered are tracked.)"
            )

    try:
        # 1. Run full strategy scan (retry up to 3 times)
        scan_result = with_retry(
            lambda: analyze_custom_strategy(cycle_strength_matrix),
            max_attempts=3,
            delay=5,
            label="strategy_scan",
        )

        signal_data = get_last_signal()

        if signal_data is None:
            print("[CYCLE] No qualifying signals this cycle. HOLD.")
            return

        # === MC Regime 前移: 在风控判断之前确定交易模式 ===
        mc_data = get_latest_mc_local(pair=signal_data["pair"], day=True)
        mc_regime = mc_data.get("regime", "N/A") if mc_data else "NO_LOCAL_MC_DATA"
        mode = _regime_policy(mc_regime) if _config.MC_REGIME_ENABLED else "normal"
        print(f"  [MC REGIME] {signal_data['pair']} → {mc_regime} | mode={mode}")
        params = _regime_params(mode)
        _max_pos = params["max_positions"]
        _tp_mult = params["tp_multiplier"]
        _exit_tight = params["exit_tightness"]

        if mode == "cautious":
            print(
                f"  [MC DECISION] CONSOLIDATION detected — "
                f"strength hurdle ≥ {_config.MC_REGIME_STRENGTH_HURDLE_CONSOLIDATION} | "
                f"max_pos=1 | TP×{_tp_mult} | exit_tightness={_exit_tight}"
            )
        elif mode == "aggressive":
            print(
                f"  [MC DECISION] STRONG MOMENTUM detected — "
                f"expanding to top{_max_pos} (direction-consistency filter) | "
                f"TP×{_tp_mult} | exit_tightness={_exit_tight}"
            )
        else:
            print(
                f"  [MC DECISION] NEUTRAL — "
                f"max_pos={_max_pos} (direction-consistency filter) | "
                f"TP×{_tp_mult} | exit_tightness={_exit_tight}"
            )

        # === 根据模式构建候选列表 ===
        candidates = [signal_data]
        if mode == "cautious":
            # CONSOLIDATION: 严格 top1 + 强度门槛
            score = abs(signal_data.get("strength_score", 0.0))
            hurdle = _config.MC_REGIME_STRENGTH_HURDLE_CONSOLIDATION
            if score < hurdle:
                print(
                    f"🚫 CONSOLIDATION 谨慎: {signal_data['pair']} "
                    f"strength_score={score:.4f} < {hurdle}, 本周期不开仓"
                )
                return
            print(f"  [MC REGIME] CONSOLIDATION 通过强度门槛 ({score:.4f} ≥ {hurdle})")
        elif mode in ("aggressive", "normal") and _config.ENABLE_MC_BASKET_EXECUTION:
            # NEUTRAL / STRONG MOMENTUM + basket enabled: 取 top N + 方向兼容过滤
            try:
                top_n = _strategy.get_top_signals(n=_max_pos)
            except Exception as e:
                top_n = [signal_data]
                print(f"  [MC REGIME] 取 top{_max_pos} 失败, 回退 top1: {e}")
            if not top_n:
                top_n = [signal_data]
            compatible = [top_n[0]]
            for sig in top_n[1:]:
                if all(_jpy_cross_direction_compatible(sig, c) for c in compatible):
                    compatible.append(sig)
                else:
                    print(
                        f"  [MC REGIME] 跳过 {sig['pair']} ({sig['action']}): "
                        f"与已选候选方向冲突 (避免 long USDJPY + short AUDJPY 式对冲)"
                    )
            candidates = compatible
            _label = "STRONG MOMENTUM" if mode == "aggressive" else "NEUTRAL"
            print(
                f"  [MC REGIME] {_label} BASKET → 候选对 ({len(candidates)}): "
                f"{[c['pair'] for c in candidates]}"
            )
        else:
            # v1.3 compatible single-pair mode: ignore max_pos / candidate pool.
            # candidates 保持 = [signal_data], 永远只执行 top1 pair.
            if _config.ENABLE_MC_BASKET_EXECUTION:
                pass  # cautious mode already handled above
            else:
                _mode_label = "STRONG MOMENTUM" if mode == "aggressive" else (
                    "NEUTRAL" if mode == "normal" else "UNKNOWN"
                )
                print(
                    f"  [MC REGIME] {_mode_label} → BASKET EXECUTION DISABLED "
                    f"(ENABLE_MC_BASKET_EXECUTION=False), 回退 v1.3 单 pair 行为: "
                    f"仅执行 top1 = {signal_data['pair']}"
                )
        # Keep rank tied to strength order, independent of later filters.
        candidates = sorted(
            candidates,
            key=lambda candidate: abs(candidate.get("strength_score", 0.0)),
            reverse=True,
        )

        # === 逐候选执行: TP倍率调整 → Post-Exit LIVE Gate → 风控拦截 → managed 检查 → 方向检查 → 下单 ===
        for cand_idx, cand in enumerate(candidates):
            cand = dict(cand)  # shallow copy, 避免污染 all_valid_signals 引用
            pair = cand["pair"]
            action = cand["action"]

            # --- TP 倍率调整 (per-candidate, 基于 regime params) ---
            if _tp_mult != 1.0:
                _risk_dist = abs(cand["entry"] - cand["stop_loss"])
                if action.upper() == "SELL":
                    cand["take_profit"] = cand["entry"] - _risk_dist * _tp_mult
                else:
                    cand["take_profit"] = cand["entry"] + _risk_dist * _tp_mult
                cand["take_profit"] = round(cand["take_profit"], 5)
                cand["risk_reward"] = round(
                    abs(cand["take_profit"] - cand["entry"]) / _risk_dist, 2
                )
                print(
                    f"  [MC TP] {pair}: TP×{_tp_mult} → {cand['take_profit']} "
                    f"(risk_reward → {cand['risk_reward']:.2f})"
                )

            # --- Post-Exit Gate (LIVE Adaptive Threshold Engine) ---
            # Gate evaluates tier × rank × MC regime multipliers on three AND checks:
            # strength, gap, alignment. Decision is LIVE unless POST_EXIT_GATE_SHADOW=True.
            gate_decision = None
            allow_open = True
            effective_units = profile["units"]
            gate_enabled = getattr(_config, "POST_EXIT_GATE_ENABLED", True)
            gate_shadow = getattr(_config, "POST_EXIT_GATE_SHADOW", False)

            if gate_enabled:
                try:
                    # Compute candidate rank (1-indexed from sorted strength order)
                    candidate_rank = cand_idx + 1
                    # MC regime for this specific pair (already fetched earlier)
                    pair_mc_data = get_latest_mc_local(pair=pair, day=True)
                    pair_mc_regime = (
                        pair_mc_data.get("regime", "N/A") if pair_mc_data else "NO_LOCAL_MC_DATA"
                    )

                    gate_decision = PostExitGate.evaluate(
                        instrument=pair,
                        action=action,
                        strength_score=cand.get("strength_score", 0.0),
                        candidate_rank=candidate_rank,
                        mc_regime_raw=pair_mc_regime,
                        baseline_units=profile["units"],
                        tracker=post_exit_tracker,
                    )

                    if gate_shadow:
                        print(
                            f"  [POST-EXIT SHADOW] {pair} → shadow verdict: "
                            f"{gate_decision.reason_code} (NOT applied — shadow only)"
                        )
                    else:
                        allow_open = gate_decision.is_allowed
                        effective_units = gate_decision.effective_units

                except Exception as _pe_err:
                    print(
                        f"  [POST_EXIT] LIVE gate evaluation failed (non-fatal, fallback to baseline): {_pe_err}"
                    )
                    import traceback
                    traceback.print_exc()
                    allow_open = True
                    effective_units = profile["units"]
            else:
                print("  [POST_EXIT] Gate disabled via config — baseline behavior")

            # 风控拦截判断
            if gate_enabled and not gate_shadow and not allow_open:
                print(
                    f"🚫 POST-EXIT GATE REJECTED: {pair} → {gate_decision.reason_code} "
                    f"(effective_multiplier=×{gate_decision.effective_multiplier_live:.2f})"
                )
                continue

            # 1b. Skip if the risk layer is already managing this pair this cycle
            if pair in managed_instruments:
                print(
                    f"[CYCLE] {pair} already under dynamic risk management. Skipping new entry."
                )
                continue

            # 2. Direction-aware existing-position check
            try:
                if dry_run:
                    existing_direction = get_position_direction(pair)
                    decision = resolve_signal_vs_position(action, existing_direction)
                    print(
                        f"  [DRY RUN] Position={existing_direction or 'FLAT'}; would be {decision.value}."
                    )
                else:
                    decision = resolve_and_prepare_entry(pair, action)
            except PositionDirectionError as e:
                print(f"  [POSITION ERROR] {e}")
                print("  → Will retry next cycle.")
                continue
            except Exception as e:
                print(f"  [NETWORK ERROR] OANDA connection failed: {e}")
                print("  → Will retry next cycle.")
                continue

            if decision == PositionDecision.SKIP_SAME_DIRECTION:
                print(
                    f"[CYCLE] Already holding a {action} position in {pair} matching the signal direction. Skipping."
                )
                continue
            if decision == PositionDecision.SKIP_HEDGED:
                print(
                    f"[CYCLE] {pair} has both long AND short units open simultaneously (hedged) — "
                    f"ambiguous, skipping automatic handling for safety. Investigate manually."
                )
                continue
            if decision == PositionDecision.CLOSE_THEN_ENTER:
                if dry_run:
                    print(
                        f"[DRY RUN] Would close the opposite-direction position in {pair}, then open {action}."
                    )
                else:
                    print(
                        f"[CYCLE] Existing opposite-direction position in {pair} was closed to allow the new {action} signal."
                    )
                    post_exit_tracker.record_exit(pair, "ACTIVE")
                    PostExitGate.record_exit("ACTIVE")

            # --- Fetch local Monte Carlo results for the signaled pair ---
            mc_data_i = get_latest_mc_local(pair=pair, day=True)
            p_up = mc_data_i.get("p_up") if mc_data_i else None
            p_down = mc_data_i.get("p_down") if mc_data_i else None
            mc_regime_i = (
                mc_data_i.get("regime", "N/A") if mc_data_i else "NO_LOCAL_MC_DATA"
            )

            print(f"\n  ✅ SIGNAL: {action} {pair}")
            print(f"     Entry      : {cand['entry']}")
            print(f"     Stop Loss  : {cand['stop_loss']}")
            print(f"     Take Profit: {cand['take_profit']}")
            print(f"     R:R Ratio  : {cand['risk_reward']:.2f}")
            if mc_data_i:
                print(
                    f"     MC Forecast: P(UP)={p_up}% | P(DOWN)={p_down}% | Regime={mc_regime_i}"
                )
            else:
                print("     MC Forecast: [No local MC result found]")
            print(f"     Reason     : {cand['reasoning']}")
            if dry_run:
                print(
                    "\n  [DRY RUN] Would send order to OANDA; no order was submitted."
                )
                continue

            print("\n  → Sending order to OANDA...")

            if ENABLE_DYNAMIC_RISK_MANAGER:
                fill = open_oanda_order(cand, units=effective_units)
                if fill.get("status") == "SUCCESS":
                    print(
                        f"  ✅ Order filled: {fill['order_id']} @ {fill['filled_price']}"
                    )
                    try:
                        cluster = _risk.new_cluster_from_fill(
                            cand, fill, exit_tightness=_exit_tight
                        )
                        _risk.save_cluster_data(pair, cluster.to_dict())
                        print(
                            f"  [RISK] {pair} now under dynamic risk management (trade_id={fill.get('trade_id')}, "
                            f"exit_tightness={_exit_tight})."
                        )
                    except Exception as e:
                        if getattr(
                            _config, "ENABLE_CLUSTER_LOUD_LOG_ON_FILL_FAILURE", True
                        ):
                            _log.error(
                                "[RISK ERROR] Cluster creation FAILED after OANDA fill — "
                                "pair=%s filled_price=%s units=%s trade_id=%s "
                                "exit_tightness=%s | %s: %s",
                                pair,
                                fill.get("filled_price"),
                                fill.get("units"),
                                fill.get("trade_id"),
                                _exit_tight,
                                type(e).__name__,
                                e,
                            )
                        print(
                            f"  [RISK ERROR] Order filled but cluster creation failed: "
                            f"{type(e).__name__}: {e}"
                        )
                        print(
                            f"  ⚠️  {pair} has a LIVE position at OANDA (trade_id={fill.get('trade_id')}) "
                            f"NOT under dynamic risk management. It still has its native SL/TP from "
                            f"the order fill. Investigate before next cycle."
                        )
                else:
                    print(f"  ❌ Order NOT confirmed: {fill.get('message')}")
            else:
                signal = TradeSignal(
                    pair_to_trade=pair,
                    action=action,
                    confidence_score=0.85,
                    stop_loss=cand["stop_loss"],
                    take_profit=cand["take_profit"],
                    reasoning=cand["reasoning"],
                )
                if success := execute_market_trade(
                    signal, units_override=effective_units
                ):
                    print("  ✅ Order submitted successfully")
                else:
                    print("  ❌ Order NOT confirmed — check logs above")

    except Exception as e:
        import traceback

        print(f"[CYCLE FAILED] {str(e)}")
        traceback.print_exc()
        print("  → Will retry on next scheduled run")


if __name__ == "__main__":
    print("=" * 60)
    print("JPY STRENGTH TRADING BOT — SCHEDULED RUNNER")
    print("=" * 60)
    print(
        f"  Strategy : Trade top pair if ≥ {MIN_VALID_PAIRS_TO_TRADE} valid JPY crosses qualify"
    )
    print(
        f"  Risk     : Level {RISK_LEVEL} ({RISK_PROFILE[RISK_LEVEL]['units']:,} units per trade)"
    )
    print(f"  Interval : Every {CHECK_INTERVAL_MINUTES} minutes (cron-driven)")
    print(f"  OANDA profile: #{_args.profile} ({_config.OANDA_ACCOUNT_ID})")
    print(f"  Dry run: {'ENABLED' if _args.dry_run else 'disabled'}")
    print(
        f"  Dynamic risk manager: {'ENABLED' if ENABLE_DYNAMIC_RISK_MANAGER else 'disabled'}"
    )
    print(
        f"  MC Regime gating: {'ENABLED' if _config.MC_REGIME_ENABLED else 'disabled'}"
    )
    print(
        f"  PostExitGate: {'ENABLED' if POST_EXIT_GATE_ENABLED else 'disabled'} | "
        f"mode: {'SHADOW (observation only)' if POST_EXIT_GATE_SHADOW else 'LIVE (demo decisions)'}"
    )
    print("=" * 60)

    run_cycle()