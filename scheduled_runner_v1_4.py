"""
Scheduled Runner — JPY Strength Strategy · 极简修正版
========================================================
✅ 不改结构、不硬编码、不读返回值
✅ 直接用程序已打印出的「strength vs JPY」数值判方向
   差 > 0 → BUY | 差 < 0 → SELL | |差|<1.5 → 观望
✅ 方向错就自动反转 + 互换止损止盈
"""

import time
import re
from datetime import datetime

import schedule
from config import CHECK_INTERVAL_MINUTES, RISK_LEVEL, RISK_PROFILE
from custom_strategy import analyze_custom_strategy, get_last_signal
from utils import execute_market_trade, get_open_position
from utils.schemas import TradeSignal
from retry import with_retry

STRENGTH_GAP_THRESHOLD = 1.5  # 绝对值≥此数才开仓


def run_cycle():
    profile = RISK_PROFILE[RISK_LEVEL]
    print(
        f"\n[{datetime.now().isoformat()}] === JPY Strength Scan | Risk Level: {RISK_LEVEL} ==="
    )
    try:
        # 1. Run strategy — 输出里自带 "strength vs JPY: ±X.XXXX"
        scan_result = with_retry(
            analyze_custom_strategy, max_attempts=3, delay=5, label="strategy_scan"
        )
        signal_data = get_last_signal()
        if signal_data is None:
            print("[CYCLE] No qualifying signals (need ≥2 valid pairs). HOLD.")
            return

        pair = signal_data["pair"]
        action = signal_data["action"]

        # ─── ✅ 直接从程序输出里提取「相对强弱差」───
        # 日志格式: "[AUD_JPY] (strength vs JPY: -1.9367)"
        # 直接拿这个数，不用自己算、不用硬编码
        match = re.search(
            r"strength vs JPY:\s*([+-]?\d+\.\d+)", __import__("sys").stdout.getvalue()
        )
        if not match:
            print("  ⚠️  未找到强弱差，使用strategy原始方向")
            correct_action = action
        else:
            gap = float(match.group(1))
            print(f"  [强弱差] strength vs JPY: {gap:.4f}")

            abs_gap = abs(gap)
            if abs_gap < STRENGTH_GAP_THRESHOLD:
                print(
                    f"  🛑 |gap|={abs_gap:.4f} < {STRENGTH_GAP_THRESHOLD} → 观望不开仓"
                )
                return
            elif gap > 0:
                correct_action = "BUY"
                print(f"  ✅ 非JPY更强 (gap>0) → {correct_action}")
            else:
                correct_action = "SELL"
                print(f"  ✅ JPY更强 (gap<0) → {correct_action}")

        # ─── 方向不一致 → 强制修正 + 反转止损止盈 ───
        if action != correct_action:
            print(f"  ⚠️  STRATEGY建议: {action} → ❌ 修正为: {correct_action}")
            signal_data["action"] = correct_action
            entry = signal_data["entry"]
            sl = signal_data["stop_loss"]
            tp = signal_data["take_profit"]
            # 方向反了 → SL/TP 互换
            signal_data["stop_loss"], signal_data["take_profit"] = tp, sl
            signal_data["reasoning"] = f"⚠️ 修正方向: gap={gap:.4f} → {correct_action}"
            action = correct_action
        else:
            print(f"  ✅ 方向一致: {action}")

        # ──────────────────────────────────────
        # 以下完全保留你原始代码，一行不改！
        # ──────────────────────────────────────

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
            confidence_score=0.85,
            stop_loss=signal_data["stop_loss"],
            take_profit=signal_data["take_profit"],
            reasoning=signal_data["reasoning"],
        )
        print(f"\n  ✅ FINAL SIGNAL: {action} {pair}")
        print(f"     Entry       : {signal_data['entry']}")
        print(f"     Stop Loss   : {signal.stop_loss}")
        print(f"     Take Profit : {signal.take_profit}")
        print(f"     R:R Ratio   : {signal_data['risk_reward']:.2f}")
        print(f"     Reason      : {signal.reasoning}")
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
    print("JPY STRENGTH TRADING BOT · 极简修正版")
    print("  直接用程序已算出的 strength vs JPY 判方向")
    print("=" * 60)
    print(f"  Strategy : Trade top pair if ≥2 valid JPY crosses qualify")
    print(
        f"  Risk     : Level {RISK_LEVEL} ({RISK_PROFILE[RISK_LEVEL]['units']:,} units per trade)"
    )
    print(f"  Interval : Every {CHECK_INTERVAL_MINUTES} minutes")
    print("  Press Ctrl+C to stop gracefully\n")
    run_cycle()
    # schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(run_cycle)
    # while True:
    #     schedule.run_pending()
    #     time.sleep(5)
