# utils/calculate_currency_strength.py
import numpy as np
from config import CURRENCIES
from utils import get_candles


def calculate_atr(candles, period=14):
    """Calculate ATR from candle data, compatible with OANDA/Yahoo format"""
    if len(candles) < period + 1:
        return 0.0

    highs = np.array([float(c["mid"]["h"]) for c in candles])
    lows = np.array([float(c["mid"]["l"]) for c in candles])
    closes = np.array([float(c["mid"]["c"]) for c in candles])

    tr = np.zeros(len(candles))
    tr[0] = highs[0] - lows[0]
    for i in range(1, len(candles)):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )

    atr = np.mean(tr[-period:])
    return round(atr, 5)


def calculate_currency_strength(
    pairs,
    timeframes=["H1", "H4", "D"],
    weights=[1, 3, 6],
    lookback=20
):
    """
    Professional-grade Relative Currency Strength
    Standard industry implementation:
    - Multi-timeframe weighted
    - Logarithmic returns
    - ATR-normalized for volatility consistency
    - Z-score scaled for easy comparison
    """
    strength = {curr: [] for curr in CURRENCIES}

    for tf, weight in zip(timeframes, weights):
        for pair in pairs:
            if "_" not in pair:
                continue
            base_curr, quote_curr = pair.split("_")

            candles = get_candles(pair, granularity=tf, count=lookback)
            if not candles or len(candles) < 2:
                continue

            closes = np.array([float(c["mid"]["c"]) for c in candles])
            # Log return = more accurate than simple percentage
            log_return = np.log(closes[-1] / closes[0]) * 100

            # Normalize by ATR so high/low volatility pairs have comparable scores
            atr = calculate_atr(candles, period=14)
            if atr > 0:
                log_return = log_return / atr

            # Assign correct sign: base = positive, quote = negative
            strength[base_curr].append(log_return * weight)
            strength[quote_curr].append(-log_return * weight)

    # Average all weighted values
    final_scores = {}
    for curr, values in strength.items():
        final_scores[curr] = np.mean(values) if values else 0.0

    # Convert to Z-score: range ~ -3 to +3, easier to rank
    scores_array = np.array(list(final_scores.values()))
    mean_score = np.mean(scores_array)
    std_score = np.std(scores_array)

    if std_score == 0:
        return {curr: 0.0 for curr in final_scores}

    return {
        curr: round((score - mean_score) / std_score, 4)
        for curr, score in final_scores.items()
    }