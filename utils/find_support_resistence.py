import os
import importlib
from oandapyV20 import API
import oandapyV20.endpoints.instruments as instruments
from utils import oanda_client
from typing import Optional
from config import (
    OANDA_ENV, OANDA_API_TOKEN, OANDA_ACCOUNT_ID,
    TRADE_PAIRS, SIGNAL_TIMEFRAMES, SL_BUFFER_PIPS, SPREAD_PIPS
    # TP_PIPS, 
)

oanda_client = API(access_token=OANDA_API_TOKEN, environment=OANDA_ENV)

JPY_PIP = 0.01
MIN_MARKET_STRENGTH = 0.05
FRONT_RUN_PIPS = 15  # Outward buffer to secure fills before the exact level

STRENGTH_PAIRS = [
    "EUR_USD", "GBP_USD", "AUD_USD", "NZD_USD",
    "USD_CAD", "USD_CHF", "USD_JPY",
    "EUR_GBP", "EUR_JPY", "EUR_AUD", "EUR_CAD", "EUR_CHF",
    "GBP_JPY", "GBP_AUD", "GBP_CAD",
    "AUD_JPY", "AUD_CAD", "AUD_CHF",
    "NZD_JPY", "CAD_JPY", "CHF_JPY",
]

CURRENCIES = ["USD", "EUR", "GBP", "AUD", "NZD", "CAD", "CHF", "JPY"]

STRENGTH_TIMEFRAMES = {
    "H1": 1,
    "H4": 3,
    "H8": 6,
}
STRENGTH_CANDLE_COUNT = 10


# ==========================================
# HELPERS & STRUCTURAL S/R ENGINE (Your Code Merged)
# ==========================================
def get_candles(instrument: str, granularity: str, count: int) -> list:
    params = {"count": count, "granularity": granularity}
    try:
        req = instruments.InstrumentsCandles(instrument=instrument, params=params)
        oanda_client.request(req)
        return [c for c in req.response.get("candles", []) if c["complete"]]
    except Exception as e:
        print(f"  [STRATEGY] Candle fetch failed {instrument} {granularity}: {e}")
        return []


def get_support_resistance(instrument: str, granularity: str, count: int = 100, window: int = 3) -> dict:
    """
    Scans historical data to locate nearest structural swing levels relative to current price.
    """
    candles = get_candles(instrument, granularity, count)
    if not candles:
        return {"support": None, "resistance": None, "current_price": None}

    lows = [float(c["mid"]["l"]) for c in candles]
    highs = [float(c["mid"]["h"]) for c in candles]
    closes = [float(c["mid"]["c"]) for c in candles]
    current_price = closes[-1]

    supports = []
    resistances = []

    for i in range(window, len(candles) - window):
        low = lows[i]
        high = highs[i]

        if low == min(lows[i - window : i + window + 1]):
            supports.append(low)

        if high == max(highs[i - window : i + window + 1]):
            resistances.append(high)

    # Filter levels relative to where price is sitting right now
    support = max([s for s in supports if s <= current_price], default=min(lows))
    resistance = min([r for r in resistances if r >= current_price], default=max(highs))

    return {
        "support": support,
        "resistance": resistance,
        "current_price": current_price
    }


# ==========================================
# STEP 1: CURRENCY STRENGTH RANKING
# ==========================================
def jpy_strength_rank(scores: dict) -> dict[str, float]:
    jpy_score = scores.get("JPY", 0.0)
    return {
        pair: scores.get(pair.split("_")[0], 0.0) - jpy_score
        for pair in TRADE_PAIRS
    }


def get_pair_momentum(instrument: str, granularity: str, count: int) -> float | None:
    candles = get_candles(instrument, granularity, count + 1)
    if len(candles) < 2:
        return None
    oldest = float(candles[0]["mid"]["c"])
    latest = float(candles[-1]["mid"]["c"])
    return (latest - oldest) / oldest * 100


def build_strength_matrix() -> dict[str, float]:
    scores  = {c: 0.0 for c in CURRENCIES}
    samples = {c: 0   for c in CURRENCIES}

    for pair in STRENGTH_PAIRS:
        parts = pair.split("_")
        if len(parts) != 2:
            continue
        base, quote = parts
        if base not in CURRENCIES or quote not in CURRENCIES:
            continue

        for granularity, weight in STRENGTH_TIMEFRAMES.items():
            momentum = get_pair_momentum(pair, granularity, STRENGTH_CANDLE_COUNT)
            if momentum is None:
                continue
            scores[base]  += momentum * weight
            scores[quote] -= momentum * weight
            samples[base] += 1
            samples[quote]+= 1

    for c in CURRENCIES:
        if samples[c] > 0:
            scores[c] /= samples[c]

    return scores


def format_strength_ranking(scores: dict) -> str:
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    lines  = ["  Currency Strength Ranking:"]
    for i, (currency, score) in enumerate(ranked, 1):
        bar = "█" * min(int(abs(score) * 20), 40)
        direction = "▲" if score > 0 else "▼"
        lines.append(f"  {i}. {currency}: {score:+.4f} {direction} {bar}")
    gap = ranked[0][1] - ranked[-1][1]
    lines.append(f"\n  Score gap: {gap:.3f} "
                 f"({'STRONG' if gap > 1.5 else 'MODERATE' if gap > 0.5 else 'COILING'})")
    return "\n".join(lines)


# ==========================================
# STEP 2: MA5 MULTI-TIMEFRAME ALIGNMENT
# ==========================================
def get_ma5_position(instrument: str, granularity: str) -> str | None:
    candles = get_candles(instrument, granularity, count=10)
    if len(candles) < 6:
        return None
    closes       = [float(c["mid"]["c"]) for c in candles]
    ma5          = sum(closes[-5:]) / 5
    latest_close = closes[-1]
    return "above" if latest_close > ma5 else "below"


def get_live_prices(instrument: str) -> dict | None:
    try:
        pricing_module = __import__("oandapyV20.endpoints.pricing", fromlist=["PricingInfo"])
        req = pricing_module.PricingInfo(accountID=OANDA_ACCOUNT_ID, params={"instruments": instrument})
        oanda_client.request(req)
        prices = req.response["prices"][0]
        return {
            "ask": float(prices["asks"][0]["price"]),
            "bid": float(prices["bids"][0]["price"])
        }
    except Exception as e:
        print(f"    Price fetch failed for {instrument}: {e}")
        return None


# ==========================================
# MAIN ENTRY POINT
# ==========================================
def analyze_custom_strategy() -> str:
    print("[STRATEGY] Step 1 — Currency strength ranking...")
    scores = build_strength_matrix()
    strength_report = format_strength_ranking(scores)
    print(strength_report)

    jpy_ranks = jpy_strength_rank(scores)
    
    max_gap = max(abs(v) for v in jpy_ranks.values()) if jpy_ranks else 0.0
    if max_gap < MIN_MARKET_STRENGTH:
        print(f"  [STRATEGY] Global JPY gap ({max_gap:.4f}) below minimum floor. Enforcing {MIN_MARKET_STRENGTH}")
        max_gap = MIN_MARKET_STRENGTH

    ranked_pairs = sorted(jpy_ranks.items(), key=lambda x: abs(x[1]), reverse=True)
    print(f"\n  JPY cross priority (by absolute strength gap): {' > '.join(f'{p}({s:+.3f})' for p, s in ranked_pairs)}")

    print("\n[STRATEGY] Step 2 — MA5 alignment check...")
    signals = []

    for pair, strength_score in ranked_pairs:
        print(f"\n  [{pair}] (strength score: {strength_score:+.4f})")
        
        # Proportional Filter
        dynamic_cutoff = max_gap * 0.4
        if abs(strength_score) < dynamic_cutoff:
            print(f"    → Skip: strength gap {abs(strength_score):.4f} < {dynamic_cutoff:.4f} (40% of max gap)")
            continue

        direction = check_ma5_alignment(pair)
        if direction is None:
            print(f"    → No alignment (Mixed timeframes)")
            continue

        # Prevent contradictions
        if direction == "BUY" and strength_score < 0:
            print(f"    → Mixed Signal: Techs show BUY but Strength favors JPY strength. Skipping.")
            continue
        if direction == "SELL" and strength_score > 0:
            print(f"    → Mixed Signal: Techs show SELL but Strength favors Base strength. Skipping.")
            continue

        prices = get_live_prices(pair)
        if prices is None:
            continue

        # Fetch structural boundaries via your merged fractal algorithm
        daily_levels = get_support_resistance(pair, granularity="D", count=60, window=3)
        weekly_levels = get_support_resistance(pair, granularity="W", count=52, window=2)

        if daily_levels["support"] is None or weekly_levels["support"] is None:
            print(f"    → Structural tracking error. Skipping.")
            continue

        # --- ADDED TRACKING LINES TO SHOW YOUR CALCULATED S/R CEILINGS AND FLOORS ---
        print(f"    [D1 Levels] Support: {daily_levels['support']:.3f} | Resistance: {daily_levels['resistance']:.3f}")
        print(f"    [W1 Levels] Support: {weekly_levels['support']:.3f} | Resistance: {weekly_levels['resistance']:.3f}")

        if direction == "BUY":
            entry = prices["ask"]
            sl = round(daily_levels["support"] - (SL_BUFFER_PIPS + SPREAD_PIPS) * JPY_PIP, 3)
            # Take Profit handles your 15-pip inward front-running calculation:
            tp = round(daily_levels["resistance"] - (FRONT_RUN_PIPS * JPY_PIP), 3)
            
            # Macro Protection rule check
            if entry > (weekly_levels["resistance"] - (20 * JPY_PIP)):
                print(f"    → Abort BUY: Price is too close to major Weekly Structural Resistance ({weekly_levels['resistance']:.3f}).")
                continue
        else:  # SELL
            entry = prices["bid"]
            sl = round(daily_levels["resistance"] + (SL_BUFFER_PIPS + SPREAD_PIPS) * JPY_PIP, 3)
            # Take Profit handles your 15-pip inward front-running calculation:
            tp = round(daily_levels["support"] + (FRONT_RUN_PIPS * JPY_PIP), 3)
            
            # Macro Protection rule check
            if entry < (weekly_levels["support"] + (20 * JPY_PIP)):
                print(f"    → Abort SELL: Price is too close to major Weekly Structural Support ({weekly_levels['support']:.3f}).")
                continue

        # Risk-to-Reward Ratio Filter guarding execution
        risk_pips = abs(entry - sl) / JPY_PIP
        reward_pips = abs(tp - entry) / JPY_PIP
        if reward_pips < (risk_pips * 1.0):
            print(f"    → Skip: Poor R:R. Risk={risk_pips:.1f} pips vs Front-run Reward={reward_pips:.1f} pips.")
            continue

        print(f"    → SIGNAL: {direction} {pair} | Entry: {entry} | SL: {sl} | TP: {tp}")

        signals.append({
            "pair": pair, "action": direction, "entry": entry, "stop_loss": sl, "take_profit": tp,
            "strength_score": strength_score,
            "reasoning": f"All timeframes aligned {direction.lower()} MA5. Target set at dynamic daily structural barrier with front-running buffer."
        })

    best = max(signals, key=lambda s: abs(s["strength_score"])) if signals else None
    analyze_custom_strategy._last_signal = best

    # Build report presentation
    report = "=== JPY TREND STRATEGY REPORT ===\n\n" + strength_report + "\n\n=== MA5 ALIGNMENT RESULTS ===\n"
    if signals:
        for s in signals:
            report += f"  {s['pair']}: {s['action']} SIGNAL ✓ (strength {s['strength_score']:+.4f}) SL={s['stop_loss']} TP={s['take_profit']}\n"
        report += f"\nBest signal: {best['action']} {best['pair']}\nReasoning: {best['reasoning']}\n"
    else:
        report += "  No trade candidates cleared trend priority filters and structural validation fields. HOLD.\n"
    return report


analyze_custom_strategy._last_signal = None


def get_last_signal() -> dict | None:
    return analyze_custom_strategy._last_signal
