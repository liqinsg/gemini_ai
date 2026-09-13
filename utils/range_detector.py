from utils.strategy_helpers import get_candles, _atr_from_candles, _ema
from typing import Tuple
import sys
from pathlib import Path
import config as _config
sys.path.insert(0, str(Path(__file__).parent.parent))

"""
Range / Sideways Market Detector
=================================
Standalone module to identify flat/sideways pairs.
Run directly: `python utils/range_detector.py` or `python -m utils.range_detector`
"""


def is_sideways(
    instrument: str,
    lookback_days: int = _config.RANGE_DETECT_LOOKBACK_DAYS,
    max_range_pct: float = _config.RANGE_DETECT_MAX_RANGE_PCT,
    min_volatility_ratio: float = _config.RANGE_DETECT_MIN_VOL_RATIO,
    ma_band_threshold: float = 0.5
) -> Tuple[bool, str, dict]:
    """
    Check if pair is in sideways/range-bound condition.
    Returns: (is_sideways: bool, reason: str, metrics: dict)
    """
    metrics = {}
    try:
        # Get enough daily candles for all checks
        candles = get_candles(instrument, "D", count=45)
        if len(candles) < lookback_days + 20:
            return False, "Not enough candle data", metrics

        # --- Extract price data ---
        highs = [float(c["mid"]["h"]) for c in candles[-lookback_days:]]
        lows = [float(c["mid"]["l"]) for c in candles[-lookback_days:]]
        closes = [float(c["mid"]["c"]) for c in candles]
        current_price = closes[-1]

        # --- 1. Total price range check ---
        period_high = max(highs)
        period_low = min(lows)
        range_pct = ((period_high - period_low) / period_low) * 100
        metrics["range_pct"] = round(range_pct, 2)
        metrics["period_high"] = round(period_high, 3)
        metrics["period_low"] = round(period_low, 3)

        if range_pct < max_range_pct:
            reason = f"Sideways: only {range_pct:.2f}% range in {lookback_days} days"
            return True, reason, metrics

        # --- 2. Volatility check (ATR vs historical) ---
        atr_current = _atr_from_candles(candles[-15:], period=14)
        atr_historical = _atr_from_candles(candles[-40:-15], period=14)
        if isinstance(atr_current, float) and isinstance(atr_historical, float) and atr_historical > 0:
            vol_ratio = atr_current / atr_historical
            metrics["volatility_ratio"] = round(vol_ratio, 2)
            if vol_ratio < min_volatility_ratio:
                reason = f"Sideways: low volatility ({vol_ratio:.2f}x average)"
                return True, reason, metrics

        # --- 3. MA band check ---
        try:
            ma20_series = _ema(closes, 20)
            if isinstance(ma20_series, list) and len(ma20_series) >= 20:
                ma20 = ma20_series[-1]
            else:
                ma20 = float(ma20_series)

            if ma20 == 0:
                ma20 = current_price

            ma_deviation_pct = abs(current_price - ma20) / ma20 * 100
            metrics["ma20_deviation_pct"] = round(ma_deviation_pct, 2)
            if ma_deviation_pct < ma_band_threshold:
                reason = f"Sideways: stuck within {ma_deviation_pct:.2f}% of MA20"
                return True, reason, metrics
        except Exception:
            pass  # Skip MA check if calculation fails

        return False, "Trending: sufficient movement", metrics

    except Exception as e:
        return False, f"Range check failed: {str(e)}", metrics


def main():
    """Quick test runner — run directly to check all JPY pairs"""
    test_pairs = ["USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY", "CAD_JPY", "NZD_JPY"]
    print("=== RANGE DETECTOR TEST ===\n")

    for pair in test_pairs:
        print(f"Testing {pair}...")
        sideways, reason, metrics = is_sideways(pair)
        status = "🔴 SIDEWAYS" if sideways else "🟢 TRENDING"
        print(f"  {status} | {reason}")
        if metrics:
            print(f"  Metrics: {metrics}")
        print()


if __name__ == "__main__":
    main()
