# scheduled_runner_v1.4.py
"""
Scheduled Runner — JPY Strength Strategy
==========================================
[保留原 docstring]
"""

import time
from datetime import datetime, timezone

from config import (
    CHECK_INTERVAL_MINUTES,
    MIN_VALID_PAIRS_TO_TRADE,
    RISK_LEVEL,
    RISK_PROFILE,
)
import config as _config
import custom_strategy_v1 as _strategy
from custom_strategy_v1 import analyze_custom_strategy, get_last_signal
from utils import execute_market_trade
from utils.schemas import TradeSignal
from utils.position_direction import (
    PositionDecision,
    PositionDirectionError,
    resolve_and_prepare_entry,
)
from retry import with_retry

from utils.mc_loader_local import get_latest_mc_local

from config import POST_EXIT_SHADOW_MODE
from state.post_exit_context import PostExitTracker
from utils.post_exit_gate import PostExitGate

from utils import risk_integration as _risk
from utils.risk_integration import ENABLE_DYNAMIC_RISK_MANAGER
from utils.oanda_execution import open_oanda_order
from utils.dynamic_risk_manager import ActionType, RiskStateEnum

import json
import os
from pathlib import Path

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

def run_cycle():
    profile = RISK_PROFILE[RISK_LEVEL]
    print(
        f"\n[{datetime.now().isoformat()}] === JPY Strength Scan | Risk Level: {RISK_LEVEL} ==="
    )
    print(f"  [RISK] Dynamic risk manager: {'ENABLED' if ENABLE_DYNAMIC_RISK_MANAGER else 'DISABLED'}")

    cycle_strength_matrix = None
    if ENABLE_DYNAMIC_RISK_MANAGER:
        try:
            cycle_strength_matrix = _strategy.build_strength_matrix()
        except Exception as strength_error:
            print(f"  [THESIS-OBSERVATION] Strength matrix unavailable: {strength_error}")

    # --- Phase A0: global kill-switch sweep ---
    if ENABLE_DYNAMIC_RISK_MANAGER:
        swept_instruments = _risk.enforce_global_invalidation_sweep(cycle_strength_matrix)
        if swept_instruments:
            print(f"  [RISK] Global sweep flattened (untracked-or-tracked): {sorted(swept_instruments)}")

    # --- Phase A: manage existing risk-managed positions ---
    managed_instruments = _risk.manage_open_positions(cycle_strength_matrix)
    if managed_instruments is None:
        print("  [RISK] Managed-position state unavailable — aborting cycle before entry evaluation.")
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

        # === 根据模式构建候选列表 ===
        candidates = [signal_data]
        if mode == "cautious":
            # CONSOLIDATION: 只卡信号强度 (不动仓位, 保留原有 dominance 判断)
            score = abs(signal_data.get("strength_score", 0.0))
            hurdle = _config.MC_REGIME_STRENGTH_HURDLE_CONSOLIDATION
            if score < hurdle:
                print(
                    f"🚫 CONSOLIDATION 谨慎: {signal_data['pair']} "
                    f"strength_score={score:.4f} < {hurdle}, 本周期不开仓"
                )
                return
            print(f"  [MC REGIME] CONSOLIDATION 通过强度门槛 ({score:.4f} ≥ {hurdle})")
        elif mode == "aggressive":
            # STRONG MOMENTUM: 取 top2 并过滤方向冲突
            try:
                top2 = _strategy.get_top_signals(n=2)
            except Exception as e:
                top2 = [signal_data]
                print(f"  [MC REGIME] 取 top2 失败, 回退 top1: {e}")
            if not top2:
                top2 = [signal_data]
            # 从第2个起做方向兼容过滤
            compatible = [top2[0]]
            for sig in top2[1:]:
                if all(_jpy_cross_direction_compatible(sig, c) for c in compatible):
                    compatible.append(sig)
                else:
                    print(
                        f"  [MC REGIME] 跳过 {sig['pair']} ({sig['action']}): "
                        f"与已选候选方向冲突 (避免 long USDJPY + short AUDJPY 式对冲)"
                    )
            candidates = compatible
            print(
                f"  [MC REGIME] STRONG MOMENTUM → 候选对: "
                f"{[c['pair'] for c in candidates]}"
            )

        # === 逐候选执行: Post-Exit Shadow → 风控拦截 → managed 检查 → 方向检查 → 下单 ===
        for cand in candidates:
            pair = cand["pair"]
            action = cand["action"]

            # --- Post-Exit Shadow Gate (per-pair, observational) ---
            # Evaluated immediately after candidate scoring; verdict is IGNORED for
            # live execution and logged to JSONL for offline analysis.
            allow_open = True  # 默认放行 (无 shadow 时)
            if POST_EXIT_SHADOW_MODE:
                try:
                    _post_exit_tracker = PostExitTracker()
                    ctx = _post_exit_tracker.get_context(pair)

                    close_reason = ctx.get("close_reason") or ""
                    if not ctx["closed_at"]:
                        tier = "tier1"
                    elif "closed_by_own_risk_action" in close_reason:
                        tier = "tier3"
                    else:
                        tier = "tier2"

                    rules = getattr(_config, "POST_EXIT_RULES", {})
                    tier_cfg = rules.get(tier, {"baseline": 1.0, "m_reason": 1.0})
                    baseline = tier_cfg["baseline"]
                    m_reason = tier_cfg["m_reason"]

                    gap_delta = abs(cand.get("strength_score", 0.0))
                    alignment = getattr(_config, "REQUIRE_ALIGNED", 3)
                    rank = 1

                    allow_open, shadow = PostExitGate.check_risk_before_open(
                        baseline=baseline,
                        m_reason=m_reason,
                        consecutive_failures=ctx["consecutive_failures"],
                        elapsed_hours=ctx["elapsed_hours"],
                        alignment=alignment,
                        rank=rank,
                        gap_delta=gap_delta,
                    )

                    shadow_record = {
                        "log_type": "post_exit_shadow",
                        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                        "pair": pair,
                        "action": action,
                        "tier": tier,
                        "consecutive_failures": ctx["consecutive_failures"],
                        "elapsed_hours": ctx["elapsed_hours"],
                        "gap_delta": gap_delta,
                        "alignment": alignment,
                        "rank": rank,
                        "mc_regime": mc_regime,
                        "mc_mode": mode,
                        **shadow,
                    }
                    _log_shadow(shadow_record)
                    print(f"  [POST_EXIT_SHADOW] {pair} → {shadow['verdict']} "
                          f"(hurdle={shadow['effective_hurdle']}, "
                          f"decay={shadow['m_decay']}, streak={shadow['m_streak']}, "
                          f"reset={shadow['regime_reset_triggered']})")
                except Exception as _pe_err:
                    print(f"  [POST_EXIT_SHADOW] Evaluation failed (non-fatal): {_pe_err}")
            # --- end Post-Exit Shadow Gate ---

            # 风控拦截判断
            if POST_EXIT_SHADOW_MODE and not allow_open:
                print(f"🚫 风控拦截: {pair} 暂不开仓")
                continue

            # 1b. Skip if the risk layer is already managing this pair this cycle
            if pair in managed_instruments:
                print(f"[CYCLE] {pair} already under dynamic risk management. Skipping new entry.")
                continue

            # 2. Direction-aware existing-position check
            try:
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
                print(f"[CYCLE] Already holding a {action} position in {pair} matching the signal direction. Skipping.")
                continue
            if decision == PositionDecision.SKIP_HEDGED:
                print(f"[CYCLE] {pair} has both long AND short units open simultaneously (hedged) — "
                      f"ambiguous, skipping automatic handling for safety. Investigate manually.")
                continue
            if decision == PositionDecision.CLOSE_THEN_ENTER:
                print(f"[CYCLE] Existing opposite-direction position in {pair} was closed to allow the new {action} signal.")
                PostExitGate.record_exit("ACTIVE")

            # --- Fetch local Monte Carlo results for the signaled pair ---
            mc_data_i = get_latest_mc_local(pair=pair, day=True)
            p_up = mc_data_i.get("p_up") if mc_data_i else None
            p_down = mc_data_i.get("p_down") if mc_data_i else None
            mc_regime_i = mc_data_i.get("regime", "N/A") if mc_data_i else "NO_LOCAL_MC_DATA"

            print(f"\n  ✅ SIGNAL: {action} {pair}")
            print(f"     Entry      : {cand['entry']}")
            print(f"     Stop Loss  : {cand['stop_loss']}")
            print(f"     Take Profit: {cand['take_profit']}")
            print(f"     R:R Ratio  : {cand['risk_reward']:.2f}")
            if mc_data_i:
                print(f"     MC Forecast: P(UP)={p_up}% | P(DOWN)={p_down}% | Regime={mc_regime_i}")
            else:
                print("     MC Forecast: [No local MC result found]")
            print(f"     Reason     : {cand['reasoning']}")
            print("\n  → Sending order to OANDA...")

            if ENABLE_DYNAMIC_RISK_MANAGER:
                fill = open_oanda_order(cand, units=profile["units"])
                if fill.get("status") == "SUCCESS":
                    print(f"  ✅ Order filled: {fill['order_id']} @ {fill['filled_price']}")
                    try:
                        cluster = _risk.new_cluster_from_fill(cand, fill)
                        _risk.save_cluster_data(pair, cluster.to_dict())
                        print(f"  [RISK] {pair} now under dynamic risk management (trade_id={fill.get('trade_id')}).")
                    except Exception as e:
                        print(f"  [RISK ERROR] Order filled but cluster creation failed: {e}")
                        print(f"  ⚠️  {pair} has a LIVE position at OANDA (trade_id={fill.get('trade_id')}) "
                              f"NOT under dynamic risk management. It still has its native SL/TP from "
                              f"the order fill. Investigate before next cycle.")
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
                if success := execute_market_trade(signal, units_override=profile["units"]):
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
    print(f"  Dynamic risk manager: {'ENABLED' if ENABLE_DYNAMIC_RISK_MANAGER else 'disabled'}")
    print(f"  MC Regime gating: {'ENABLED' if _config.MC_REGIME_ENABLED else 'disabled'}")
    print("=" * 60)

    run_cycle()