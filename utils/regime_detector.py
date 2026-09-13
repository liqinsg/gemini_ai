"""
Market Regime Detector
----------------------
Classifies the current market as TRENDING, RANGING, or BREAKOUT_WATCH
using ADX (Average Directional Index) computed from OANDA H4 candles.

ADX measures trend strength regardless of direction:
  > ADX_TREND_THRESHOLD (25)    → TRENDING   → use Trend Combined strategy
  > ADX_BREAKOUT_THRESHOLD (20) → BREAKOUT_WATCH → use Breakout Confirm strategy
  < ADX_BREAKOUT_THRESHOLD (20) → RANGING    → use Range Reversion strategy

Also computes +DI and -DI to determine trend direction (bullish/bearish),
and range width as a % of price to quantify how tight the range is.
"""

import os
from oandapyV20 import API
import oandapyV20.endpoints.instruments as instruments_ep
from retry import with_retry
from config import (
    OANDA_API_TOKEN, OANDA_ENV,
    ADX_TREND_THRESHOLD, ADX_BREAKOUT_THRESHOLD,
    ATR_GRANULARITY, ATR_CANDLE_COUNT
)

REGIME_TRENDING  = "TRENDING"
REGIME_RANGING   = "RANGING"
REGIME_BREAKOUT  = "BREAKOUT_WATCH"

oanda_client = API(access_token=OANDA_API_TOKEN, environment=OANDA_ENV)


def compute_adx(pair: str, period: int = 14) -> dict | None:
    """
    Computes ADX, +DI, -DI, and range metrics from H4 candles.
    Returns a dict with all regime indicators, or None on failure.
    """
    params = {"count": ATR_CANDLE_COUNT + period + 5, "granularity": ATR_GRANULARITY}
    try:
        def _fetch():
            r = instruments_ep.InstrumentsCandles(instrument=pair, params=params)
            oanda_client.request(r)
            return [c for c in r.response.get("candles", []) if c["complete"]]
        candles = with_retry(_fetch, max_attempts=3, delay=5, label=f"regime_candles_{pair}")
        if len(candles) < period + 5:
            return None

        highs  = [float(c["mid"]["h"]) for c in candles]
        lows   = [float(c["mid"]["l"]) for c in candles]
        closes = [float(c["mid"]["c"]) for c in candles]

        # True Range
        trs, plus_dms, minus_dms = [], [], []
        for i in range(1, len(candles)):
            h, l, pc = highs[i], lows[i], closes[i-1]
            tr = max(h - l, abs(h - pc), abs(l - pc))
            trs.append(tr)
            up_move   = highs[i]  - highs[i-1]
            down_move = lows[i-1] - lows[i]
            plus_dms.append(up_move   if up_move   > down_move and up_move   > 0 else 0)
            minus_dms.append(down_move if down_move > up_move   and down_move > 0 else 0)

        # Wilder RMA smoothing: seed = simple average of first n values,
        # then each subsequent value = prev * (n-1)/n + current * (1/n).
        # This correctly keeps the smoothed value bounded to the input range.
        def wilder_rma(values, n):
            if len(values) < n:
                return []
            result = [sum(values[:n]) / n]  # seed
            for v in values[n:]:
                result.append(result[-1] * (n - 1) / n + v / n)
            return result

        atr14   = wilder_rma(trs,       period)
        plus14  = wilder_rma(plus_dms,  period)
        minus14 = wilder_rma(minus_dms, period)

        if not atr14:
            return None

        # +DI / -DI: scale directional movement by ATR
        plus_di  = [100 * p / a if a > 0 else 0 for p, a in zip(plus14,  atr14)]
        minus_di = [100 * m / a if a > 0 else 0 for m, a in zip(minus14, atr14)]

        # DX: divergence between +DI and -DI, 0-100 scale
        dx_vals = [
            100 * abs(p - m) / (p + m) if (p + m) > 0 else 0
            for p, m in zip(plus_di, minus_di)
        ]

        # ADX = Wilder RMA of DX — smooth over another period
        adx_vals = wilder_rma(dx_vals, period)
        if not adx_vals:
            return None

        adx = round(min(adx_vals[-1], 100), 2)  # clamp to [0,100] as sanity check
        pdi = plus_di[-1]
        mdi = minus_di[-1]

        # Range metrics (recent ATR_CANDLE_COUNT candles)
        recent_highs = highs[-ATR_CANDLE_COUNT:]
        recent_lows  = lows[-ATR_CANDLE_COUNT:]
        range_high   = max(recent_highs)
        range_low    = min(recent_lows)
        mid_price    = closes[-1]
        range_width_pct = (range_high - range_low) / mid_price * 100

        return {
            "adx":             round(adx, 2),
            "plus_di":         round(pdi, 2),
            "minus_di":        round(mdi, 2),
            "trend_direction": "BULLISH" if pdi > mdi else "BEARISH",
            "range_high":      round(range_high, 5),
            "range_low":       round(range_low,  5),
            "range_width_pct": round(range_width_pct, 3),
            "current_price":   round(mid_price, 5),
            "atr":             round(atr14[-1], 5),
        }

    except Exception as e:
        print(f"  [REGIME] ADX computation failed for {pair}: {e}")
        return None


def detect_regime(pair: str) -> tuple[str, dict]:
    """
    Returns (regime_label, metrics_dict).
    regime_label is one of: TRENDING, RANGING, BREAKOUT_WATCH
    """
    metrics = compute_adx(pair)
    if metrics is None:
        print(f"  [REGIME] Could not compute ADX for {pair}. Defaulting to RANGING.")
        return REGIME_RANGING, {}

    adx = metrics["adx"]
    pdi = metrics["plus_di"]
    mdi = metrics["minus_di"]

    if adx >= ADX_TREND_THRESHOLD:
        regime = REGIME_TRENDING
    elif adx >= ADX_BREAKOUT_THRESHOLD:
        regime = REGIME_BREAKOUT
    else:
        regime = REGIME_RANGING

    print(f"\n  [REGIME] {pair}")
    print(f"    ADX={adx:.1f} | +DI={pdi:.1f} | -DI={mdi:.1f} | "
          f"Direction={metrics['trend_direction']}")
    print(f"    Range: {metrics['range_low']} – {metrics['range_high']} "
          f"({metrics['range_width_pct']:.2f}% width)")
    print(f"    Regime → {regime}")

    return regime, metrics


def get_regime_context_for_llm(pair: str, regime: str, metrics: dict) -> str:
    """Formats regime data as a string for the LLM meta-selector prompt."""
    if not metrics:
        return f"Regime detection unavailable for {pair}. Treat as RANGING."
    return f"""
=== MARKET REGIME ANALYSIS: {pair} ===
Regime        : {regime}
ADX           : {metrics.get('adx', 'N/A')} (trend strength; >25=trending, <20=ranging)
+DI / -DI     : {metrics.get('plus_di', 'N/A')} / {metrics.get('minus_di', 'N/A')}
Direction     : {metrics.get('trend_direction', 'N/A')}
Range         : {metrics.get('range_low', 'N/A')} – {metrics.get('range_high', 'N/A')}
Range width   : {metrics.get('range_width_pct', 'N/A')}% of price
Current price : {metrics.get('current_price', 'N/A')}
ATR (H4)      : {metrics.get('atr', 'N/A')}
"""