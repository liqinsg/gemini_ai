# scheduled_runner_v13.py
"""
Scheduled Runner — JPY Strength Strategy
==========================================
Scans JPY crosses every 15 minutes (invoked by cron — no in-process loop).
Requires ≥2 valid pairs → trades only the top strongest/weakest vs JPY.
Uses custom_strategy rules + OANDA execution.

Phase 2 addition: when config.ENABLE_DYNAMIC_RISK_MANAGER is True, every
cycle first revisits any already-open, risk-managed position (break-even,
Chandelier trailing, time-decay) via utils.risk_integration, BEFORE
scanning for a new entry signal. When the flag is False (default), behavior
is IDENTICAL to the original file — the entry-scan logic below is untouched.

Strategy-Driven Exit (Strength Invalidation Close): within that same Phase A
pass, before any SL/trailing/time-decay evaluation, each managed position's
held direction is checked against the freshly built Currency Strength
Matrix (`build_strength_matrix()` below). If the thesis has inverted (e.g.
holding LONG USD_JPY while USD is now weaker than JPY), the position is
flattened immediately via a real OANDA TradeClose call — see
`utils.risk_integration.check_strategy_invalidation()` /
`_close_on_invalidation()` — freeing its risk slot before this cycle's
new-entry scan runs. Gated by config.ENABLE_STRATEGY_INVALIDATION_CLOSE
(default True); logged under CLOSE_REASON_STRATEGY_INVALIDATION. A sibling
check, `check_technical_invalidation()`, does the same for MA5 alignment
(CLOSE_REASON_TECHNICAL_INVALIDATION).

Global Invalidation Sweep (kill switch): runs BEFORE even Phase A, via
`utils.risk_integration.enforce_global_invalidation_sweep()`. Both checks
above only ever evaluated instruments THIS runner had registered in a local
cluster-state file — a manually-opened or otherwise untracked position would
never be seen by them at all. The sweep instead queries OANDA directly for
every open trade on the account and flattens any of them (tracked or not)
that fail the same invalidation checks. Gated by
config.ENABLE_GLOBAL_INVALIDATION_SWEEP (default True).

Duplicate-entry protection (broker-authoritative): the LOCAL `state/` directory
that used to back both the risk layer and the post-exit tracker is RETIRED —
no idempotency decision is read from (or written to) a file any more. Before a
new entry, `utils.oanda_state.check_pair_level_strategy_position()` asks OANDA
for this instrument's live open trades and pending orders and blocks the entry
if any of them carries this strategy's tag: same-direction duplicate, the
prohibited opposite-direction dual position, or an order already in flight.
Query failures fail CLOSED. Post-exit context is likewise derived from OANDA's
CLOSED trades via `utils.post_exit_context.PostExitTracker` (read-only, no
ledger file).
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

# Post-Exit Shadow Gate — strictly observational; never influences execution.
from config import POST_EXIT_SHADOW_MODE, STRATEGY_TAG_PREFIX
# OANDA-backed post-exit context — replaces the retired state/post_exit_context.
from utils.post_exit_context import PostExitTracker
from utils.oanda_state import build_client_extensions, check_pair_level_strategy_position
from utils.post_exit_gate import PostExitGate
from utils.post_exit_gate_prev import PostExitGate as PostExitShadowGate
# 🎯 风控总控
# ENABLE_RISK = True
# ─── 平仓时记录（放在你每一处平仓逻辑后）───
# 止损平仓 → PostExitGate.record_exit("SL")
# 止盈平仓 → PostExitGate.record_exit("TP")
# 主动平仓 → PostExitGate.record_exit("ACTIVE")

# Imports UNCONDITIONAL now (previously gated behind `if ENABLE_DYNAMIC_RISK_MANAGER:`,
# which meant a misconfigured/missing flag made this entire module invisible with zero
# import errors — exactly the defect that caused a full session of silent Phase 2 no-op).
# ENABLE_DYNAMIC_RISK_MANAGER is resolved ONCE, in risk_integration.py, and imported
# from there — this file no longer computes its own independent copy of the flag.
from utils import risk_integration as _risk
from utils.risk_integration import ENABLE_DYNAMIC_RISK_MANAGER
from utils.oanda_execution import open_oanda_order
# v20 client used for broker-authoritative idempotency queries.
from utils.trading_core import oanda_client
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

        pair = signal_data["pair"]
        action = signal_data["action"]

        # --- Post-Exit Shadow Gate (observational only) ---
        # Evaluated immediately after candidate scoring; verdict is IGNORED for
        # live execution and logged to JSONL for offline analysis.
        if POST_EXIT_SHADOW_MODE:
            try:
                _post_exit_tracker = PostExitTracker()
                ctx = _post_exit_tracker.get_context(pair)

                # Map close_reason to tier / m_reason
                close_reason = ctx.get("close_reason") or ""
                # The OANDA-derived context classifies the exit itself (profit
                # close -> tier1, loss close -> tier3); the legacy string rules
                # below only apply when the broker could not be queried.
                tier = ctx.get("tier") or (
                    "tier1"
                    if not ctx.get("closed_at")
                    else ("tier3" if "closed_by_own_risk_action" in close_reason else "tier2")
                )

                rules = getattr(_config, "POST_EXIT_RULES", {})
                tier_cfg = rules.get(tier, {"baseline": 1.0, "m_reason": 1.0})
                baseline = tier_cfg["baseline"]
                m_reason = tier_cfg["m_reason"]

                gap_delta = abs(signal_data.get("strength_score", 0.0))
                alignment = getattr(_config, "REQUIRE_ALIGNED", 3)
                rank = 1  # top_pair is always rank 1

                shadow = PostExitShadowGate.evaluate_shadow(
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
                    **shadow,
                }
                _log_shadow(shadow_record)
                print(f"  [POST_EXIT_SHADOW] {pair} → {shadow['verdict']} "
                      f"(hurdle={shadow['effective_hurdle']}, "
                      f"decay={shadow['m_decay']}, streak={shadow['m_streak']}, "
                      f"reset={shadow['regime_reset_triggered']})")
            except Exception as _pe_err:
                print(f"  [POST_EXIT_SHADOW] Evaluation failed (non-fatal): {_pe_err}")
        # --- end Post-Exit Shadow Gate (observational only — never gates live execution) ---

        # 1b. Broker-authoritative duplicate-entry guard.
        # OANDA's live open trades + pending orders are the single source of
        # truth here — no local state/ file participates in this decision, so a
        # stale/lost/CWD-relative copy can neither cause a duplicate entry nor
        # suppress a legitimate one.
        allowed, idempotency_reason = check_pair_level_strategy_position(
            oanda_client, _config.OANDA_ACCOUNT_ID, pair, action, STRATEGY_TAG_PREFIX
        )
        if not allowed:
            print(f"[CYCLE] {pair} {action} BLOCKED by OANDA idempotency guard: {idempotency_reason}")
            return
        print(f"  [IDEMPOTENCY] {pair} {action} allowed — {idempotency_reason}")

        # 1c. Skip if the risk layer is already managing this pair this cycle
        if pair in managed_instruments:
            print(f"[CYCLE] {pair} already under dynamic risk management. Skipping new entry.")
            return

        # 2. Direction-aware existing-position check
        try:
            decision = resolve_and_prepare_entry(pair, action)
        except PositionDirectionError as e:
            print(f"  [POSITION ERROR] {e}")
            print("  → Will retry next cycle.")
            return
        except Exception as e:
            print(f"  [NETWORK ERROR] OANDA connection failed: {e}")
            print("  → Will retry next cycle.")
            return

        if decision == PositionDecision.SKIP_SAME_DIRECTION:
            print(f"[CYCLE] Already holding a {action} position in {pair} matching the signal direction. Skipping.")
            return
        if decision == PositionDecision.SKIP_HEDGED:
            print(f"[CYCLE] {pair} has both long AND short units open simultaneously (hedged) — "
                  f"ambiguous, skipping automatic handling for safety. Investigate manually.")
            return
        if decision == PositionDecision.CLOSE_THEN_ENTER:
            print(f"[CYCLE] Existing opposite-direction position in {pair} was closed to allow the new {action} signal.")
            PostExitGate.record_exit("ACTIVE")

        # --- Fetch local Monte Carlo results for the signaled pair ---
        mc_data = get_latest_mc_local(pair=pair, day=True)
        p_up = mc_data.get("p_up") if mc_data else None
        p_down = mc_data.get("p_down") if mc_data else None
        mc_regime = mc_data.get("regime", "N/A") if mc_data else "NO_LOCAL_MC_DATA"

        print(f"\n  ✅ SIGNAL: {action} {pair}")
        print(f"     Entry      : {signal_data['entry']}")
        print(f"     Stop Loss  : {signal_data['stop_loss']}")
        print(f"     Take Profit: {signal_data['take_profit']}")
        print(f"     R:R Ratio  : {signal_data['risk_reward']:.2f}")
        if mc_data:
            print(f"     MC Forecast: P(UP)={p_up}% | P(DOWN)={p_down}% | Regime={mc_regime}")
        else:
            print("     MC Forecast: [No local MC result found]")
        print(f"     Reason     : {signal_data['reasoning']}")
        print("\n  → Sending order to OANDA...")

        # Stamp the order with the same strategy tag the idempotency guard
        # matches on — that tag (not a local file) is what makes re-entry
        # detection work, and it also makes OANDA itself reject a duplicate
        # clientExtensions.id should the identical signal be re-sent.
        client_extensions = build_client_extensions(
            signal_data,
            strategy_tag=STRATEGY_TAG_PREFIX,
            bar_time=signal_data.get("bar_time"),
        )

        if ENABLE_DYNAMIC_RISK_MANAGER:
            fill = open_oanda_order(
                signal_data, units=profile["units"], client_extensions=client_extensions
            )
            if fill.get("status") == "SUCCESS":
                print(f"  ✅ Order filled: {fill['order_id']} @ {fill['filled_price']}")
                print(f"  [RISK] {pair} is live at OANDA (trade_id={fill.get('trade_id')}) — position "
                      f"state is owned by the broker; no local cluster file is written.")
            else:
                print(f"  ❌ Order NOT confirmed: {fill.get('message')}")
        else:
            signal = TradeSignal(
                pair_to_trade=pair,
                action=action,
                confidence_score=0.85,
                stop_loss=signal_data["stop_loss"],
                take_profit=signal_data["take_profit"],
                reasoning=signal_data["reasoning"],
            )
            if success := execute_market_trade(
                signal,
                units_override=profile["units"],
                client_extensions=client_extensions,
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
    print(f"  Dynamic risk manager: {'ENABLED' if ENABLE_DYNAMIC_RISK_MANAGER else 'disabled'}")
    print("=" * 60)

    run_cycle()