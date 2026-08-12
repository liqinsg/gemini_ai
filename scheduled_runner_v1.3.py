# scheduled_runner_v1.1.py
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
from custom_strategy_v1 import analyze_custom_strategy, get_last_signal
from utils import execute_market_trade
from utils.schemas import TradeSignal
from utils.position_direction import (
    PositionDecision,
    PositionDirectionError,
    resolve_and_prepare_entry,
)
from retry import with_retry

# Imports UNCONDITIONAL now (previously gated behind `if ENABLE_DYNAMIC_RISK_MANAGER:`,
# which meant a misconfigured/missing flag made this entire module invisible with zero
# import errors — exactly the defect that caused a full session of silent Phase 2 no-op).
# ENABLE_DYNAMIC_RISK_MANAGER is resolved ONCE, in risk_integration.py, and imported
# from there — this file no longer computes its own independent copy of the flag.
from utils import risk_integration as _risk
from utils.risk_integration import ENABLE_DYNAMIC_RISK_MANAGER
from utils.oanda_execution import open_oanda_order
from utils.dynamic_risk_manager import ActionType, RiskStateEnum


def run_cycle():
    profile = RISK_PROFILE[RISK_LEVEL]
    print(
        f"\n[{datetime.now().isoformat()}] === JPY Strength Scan | Risk Level: {RISK_LEVEL} ==="
    )
    # Per-cycle diagnostic (not just the __main__ startup banner) — cron invokes a
    # fresh process every cycle, so this line must appear in every single log entry,
    # unambiguously, to make a misconfigured flag immediately visible in the log
    # rather than silently invisible (see module docstring / this fix's root cause).
    print(f"  [RISK] Dynamic risk manager: {'ENABLED' if ENABLE_DYNAMIC_RISK_MANAGER else 'DISABLED'}")

    # --- Phase A: manage existing risk-managed positions (no-op if flag is off) ---
    managed_instruments = _risk.manage_open_positions()
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
            analyze_custom_strategy, max_attempts=3, delay=5, label="strategy_scan"
        )

        signal_data = get_last_signal()

        if signal_data is None:
            print("[CYCLE] No qualifying signals this cycle. HOLD.")
            return

        pair = signal_data["pair"]
        action = signal_data["action"]

        # 1b. (new) Skip if the risk layer is already managing this pair this cycle
        if pair in managed_instruments:
            print(f"[CYCLE] {pair} already under dynamic risk management. Skipping new entry.")
            return

        # 2. Direction-aware existing-position check (fixes the bug where the
        #    runner would skip entry on ANY existing position, even one in the
        #    opposite direction — e.g. holding SHORT AUD/JPY while the strategy
        #    now signals BUY, leaving a stale wrong-direction position open
        #    indefinitely). Applies regardless of ENABLE_DYNAMIC_RISK_MANAGER —
        #    by this point `pair` is guaranteed NOT in managed_instruments (see
        #    1b above), so this only ever touches untracked/leftover positions.
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
        # else decision == PositionDecision.ENTER — no existing position, proceed normally.

        print(f"\n  ✅ SIGNAL: {action} {pair}")
        print(f"     Entry      : {signal_data['entry']}")
        print(f"     Stop Loss  : {signal_data['stop_loss']}")
        print(f"     Take Profit: {signal_data['take_profit']}")
        print(f"     R:R Ratio  : {signal_data['risk_reward']:.2f}")
        print(f"     Reason     : {signal_data['reasoning']}")
        print("\n  → Sending order to OANDA...")

        if ENABLE_DYNAMIC_RISK_MANAGER:
            # Use open_oanda_order (not execute_market_trade) because we need the
            # ACTUAL fill price/units/trade_id back to construct the PyramidCluster
            # from — execute_market_trade only returns True/False.
            fill = open_oanda_order(signal_data, units=profile["units"])
            if fill.get("status") == "SUCCESS":
                print(f"  ✅ Order filled: {fill['order_id']} @ {fill['filled_price']}")
                try:
                    cluster = _risk.new_cluster_from_fill(signal_data, fill)
                    _risk.save_cluster_data(pair, cluster.to_dict())
                    print(f"  [RISK] {pair} now under dynamic risk management (trade_id={fill.get('trade_id')}).")
                except Exception as e:
                    # The OANDA order already filled — a failure here must NOT be
                    # treated as "no trade happened". Log loudly; the position exists
                    # at the broker with its native SL/TP even though our risk layer
                    # isn't tracking it yet. Manual follow-up needed.
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
                confidence_score=0.85,  # High confidence rule-based
                stop_loss=signal_data["stop_loss"],
                take_profit=signal_data["take_profit"],
                reasoning=signal_data["reasoning"],
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
    print("=" * 60)

    run_cycle()