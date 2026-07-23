"""
Scheduled Runner — JPY Strength Strategy
==========================================
Scans JPY crosses every 15 minutes.
Requires ≥2 valid pairs → trades only the top strongest/weakest vs JPY.
Uses custom_strategy rules + OANDA execution.
"""
import time
import schedule
from datetime import datetime

from config import (
    CHECK_INTERVAL_MINUTES, RISK_LEVEL, RISK_PROFILE
)
from custom_strategy import analyze_custom_strategy, get_last_signal
from utils import execute_market_trade, get_open_position
from utils.schemas import TradeSignal
from retry import with_retry


def run_cycle():
    profile = RISK_PROFILE[RISK_LEVEL]
    print(f"\n[{datetime.now().isoformat()}] === JPY Strength Scan | Risk Level: {RISK_LEVEL} ===")

    try:
        # 1. Run full strategy scan (retry up to 3 times)
        scan_result = with_retry(
            analyze_custom_strategy,
            max_attempts=3,
            delay=5,
            label="strategy_scan"
        )

        signal_data = get_last_signal()

        if signal_data is None:
            print("[CYCLE] No qualifying signals (need ≥2 valid pairs). HOLD.")
            return

        pair = signal_data["pair"]
        action = signal_data["action"]

        # 2. Skip if already holding this pair
        try:
            existing = get_open_position(pair)
        except Exception as e:
            print(f"  [NETWORK ERROR] OANDA connection failed: {e}")
            print("  → Will retry next cycle.")
            return

        if existing:
            long_units = float(existing.get("long", {}).get("units", 0))
            short_units = float(existing.get("short", {}).get("units", 0))
            if long_units != 0 or short_units != 0:
                print(f"[CYCLE] Already holding position in {pair}. Skipping.")
                return

        # 3. Build & execute trade signal
        signal = TradeSignal(
            pair_to_trade=pair,
            action=action,
            confidence_score=0.85,  # High confidence rule-based
            stop_loss=signal_data["stop_loss"],
            take_profit=signal_data["take_profit"],
            reasoning=signal_data["reasoning"]
        )

        print(f"\n  ✅ SIGNAL: {action} {pair}")
        print(f"     Entry      : {signal_data['entry']}")
        print(f"     Stop Loss  : {signal.stop_loss}")
        print(f"     Take Profit: {signal.take_profit}")
        print(f"     R:R Ratio  : {signal_data['risk_reward']:.2f}")
        print(f"     Reason     : {signal.reasoning}")
        print("\n  → Sending order to OANDA...")

        execute_market_trade(signal, units_override=profile["units"])
        print("  ✅ Order submitted successfully")

    except Exception as e:
        import traceback
        print(f"[CYCLE FAILED] {str(e)}")
        traceback.print_exc()
        print("  → Will retry on next scheduled run")


if __name__ == "__main__":
    print("=" * 60)
    print("JPY STRENGTH TRADING BOT — SCHEDULED RUNNER")
    print("=" * 60)
    print("  Strategy : Trade top pair if ≥2 valid JPY crosses qualify")
    print(f"  Risk     : Level {RISK_LEVEL} ({RISK_PROFILE[RISK_LEVEL]['units']:,} units per trade)")
    print(f"  Interval : Every {CHECK_INTERVAL_MINUTES} minutes")
    print("  Press Ctrl+C to stop gracefully\n")

    # Run first scan immediately
    run_cycle()

    # Schedule recurring runs
    schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(run_cycle)

    # Keep running
    while True:
        schedule.run_pending()
        time.sleep(5)