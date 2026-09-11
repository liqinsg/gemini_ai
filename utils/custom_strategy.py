import os
from abc import ABC, abstractmethod
from ml_confirmation import ml_filter, is_ml_weighted_dominance_enabled
from range_detector import is_sideways
from find_support_resistence import get_support_resistance, oanda_client
import config as _config
from config import (
    TRADE_PAIRS, SL_BUFFER_PIPS, SPREAD_PIPS,
    MIN_QUALIFYING_PAIRS, MIN_DOMINANCE_RATIO,
    ENABLE_VOLATILITY_NORMALIZED_DOMINANCE,
    ENABLE_ATR_SLTP, ENABLE_NEWS_FILTER, ENABLE_EMA_TREND,
    ENABLE_ATR_NORMALIZED_STRENGTH, ENABLE_STRENGTH_ACCELERATION,
    STRENGTH_ACCELERATION_WEIGHT, ENABLE_BREAKOUT_CONFIRMATION,
    BREAKOUT_CONFIRMATION_CLOSES,
    GEMINI_API_KEY, GEMINI_NEWS_MODEL, GEMINI_NEWS_FALLBACK_MODEL,
    NEWS_LOG_PATH, NEWS_CURRENCIES,
    # ✅ Explicit config imports
    REQUIRE_ALIGNED,
    MIN_VALID_PAIRS_TO_TRADE,
    ENABLE_RANGE_DETECTOR,
    SKIP_SIDEWAYS_PAIRS,
    TRADE_TOP_PAIRS,
    ENABLE_MACRO_PROTECTION
)
from trading_core import get_latest_news_sentiment, get_news_risk_bias, validate_signal_with_fundamentals, get_ensemble_consensus, execute_market_trade

from utils.strategy_helpers import (
    get_candles, _atr_from_candles, get_atr_with_volatility_context,
    get_dominance_normalizer, get_pair_momentum, build_strength_matrix,
    format_strength_ranking, get_ma5_position, _ema, get_ema_trend_position,
    get_trend_position, check_ma5_alignment, get_previous_day_low,
    get_previous_day_high, confirmed_breakout, get_live_prices,
    NewsFilter
)

# --- TOGGLE: Set to False to disable sideways skipping entirely ---
# SKIP_SIDEWAYS_PAIRS = True  # ✅ Toggle here!


def check_sideways_and_skip(instrument: str) -> tuple[bool, str]:
    """
    Check if pair is sideways; returns (should_skip: bool, reason: str)
    Respects the global SKIP_SIDEWAYS_PAIRS toggle.
    """
    if not SKIP_SIDEWAYS_PAIRS:
        return False, "Sideways skip disabled by toggle"

    sideways, reason, metrics = is_sideways(instrument)
    if sideways:
        print(f"⚠️  SKIP {instrument}: {reason} | Metrics: {metrics}")
        return True, reason
    else:
        print(f"✅ TRADE {instrument}: {reason} | Metrics: {metrics}")
        return False, reason


OANDA_ACCOUNT_ID = getattr(_config, "OANDA_ACCOUNT_ID", None) or os.getenv("OANDA_ACCOUNT_ID")
if not OANDA_ACCOUNT_ID:
    print("[STRATEGY] WARNING: OANDA_ACCOUNT_ID not found in config.py or environment.")

JPY_TRADE_PAIRS = [p for p in TRADE_PAIRS if p.endswith("_JPY")]
if _dropped := [p for p in TRADE_PAIRS if not p.endswith("_JPY")]:
    print(f"[STRATEGY] WARNING: non-JPY pairs found in TRADE_PAIRS and will be IGNORED: {_dropped}")

_news_filter = NewsFilter()


# ==========================================
# STRATEGY INTERFACE
# ==========================================
class Strategy(ABC):
    @abstractmethod
    def generate_signals(self, scores: dict) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
    def rules_description(self) -> str:
        raise NotImplementedError


# ==========================================
# JPY TREND STRATEGY
# ==========================================
class JPYTrendStrategy(Strategy):
    JPY_PIP = _config.JPY_PIP
    MIN_MARKET_STRENGTH = _config.MIN_MARKET_STRENGTH
    FRONT_RUN_PIPS = _config.FRONT_RUN_PIPS
    MACRO_PROTECTION_PIPS = _config.MACRO_PROTECTION_PIPS
    MIN_RR = _config.MIN_RR

    ATR_PERIOD = _config.JPY_ATR_PERIOD
    ATR_HISTORY_LOOKBACK = _config.JPY_ATR_HISTORY_LOOKBACK
    ATR_SL_MULTIPLIER_NORMAL = _config.JPY_ATR_SL_MULTIPLIER_NORMAL
    ATR_SL_MULTIPLIER_HIGH_VOL = _config.JPY_ATR_SL_MULTIPLIER_HIGH_VOL
    ATR_SL_MULTIPLIER_LOW_VOL = _config.JPY_ATR_SL_MULTIPLIER_LOW_VOL
    ATR_RR_MULTIPLE = _config.JPY_ATR_RR_MULTIPLE

    # --- Pull all rules from config ---
    MIN_VALID_PAIRS = MIN_VALID_PAIRS_TO_TRADE  # Require consensus across multiple pairs
    TREND_ALIGNMENT_REQUIRED = REQUIRE_ALIGNED  # How many timeframes must agree
    TRADE_TOP_PAIRS = TRADE_TOP_PAIRS           # Always trade only single strongest/weakest
    SKIP_SIDEWAYS_PAIRS = SKIP_SIDEWAYS_PAIRS   # Toggle to skip sideways pairs

    def __init__(self, trade_pairs: list[str] | None = None):
        self.trade_pairs = trade_pairs if trade_pairs is not None else JPY_TRADE_PAIRS
        if not self.trade_pairs:
            print("[STRATEGY] WARNING: JPYTrendStrategy has no trade pairs configured.")

    def jpy_strength_rank(self, scores: dict) -> dict[str, float]:
        jpy_score = scores.get("JPY", 0.0)
        return {
            pair: scores.get(pair.split("_")[0], 0.0) - jpy_score
            for pair in self.trade_pairs
        }

    def generate_signals(self, scores: dict) -> list[dict]:
        _news_filter.reset_cycle()
        jpy_ranks = self.jpy_strength_rank(scores)

        max_gap = max(abs(v) for v in jpy_ranks.values()) if jpy_ranks else 0.0
        if max_gap < self.MIN_MARKET_STRENGTH:
            print(f"  [STRATEGY] Global JPY gap ({max_gap:.4f}) below minimum floor.")
            max_gap = self.MIN_MARKET_STRENGTH

        ranked_pairs = sorted(jpy_ranks.items(), key=lambda x: abs(x[1]), reverse=True)
        print(f"\n  JPY cross strength ranking: {' > '.join(f'{p}({s:+.3f})' for p, s in ranked_pairs)}")
        print(f"\n[STRATEGY] Checking pairs (need ≥{self.TREND_ALIGNMENT_REQUIRED} aligned timeframes)...")
        all_valid_signals = []

        for pair, strength_score in ranked_pairs:
            print(f"\n  [{pair}] (strength vs JPY: {strength_score:+.4f})")
            # Basic strength cutoff
            dynamic_cutoff = max_gap * 0.4
            if abs(strength_score) < dynamic_cutoff:
                print(f"    → Skip: strength gap {abs(strength_score):.4f} below {dynamic_cutoff:.4f}")
                continue

            # News filter
            should_avoid, news_reason = _news_filter.should_avoid_pair(pair)
            if should_avoid:
                print(f"    → Skip: news risk - {news_reason}")
                continue

            # ✅ SIDEWAYS FILTER — BEST SPOT
            if self.SKIP_SIDEWAYS_PAIRS:
                sideways, reason, metrics = is_sideways(pair)
                if sideways:
                    print(f"    → Skip: sideways market — {reason} | Range: {metrics.get('range_pct', 'N/A')}%")
                    continue

            # Trend alignment check — uses your config value
            direction = check_ma5_alignment(pair, require_aligned=self.TREND_ALIGNMENT_REQUIRED)
            if direction is None:
                print(f"    → Skip: mixed alignment (need ≥{self.TREND_ALIGNMENT_REQUIRED} same)")
                continue

            # Match strength to direction
            if direction == "BUY" and strength_score < 0:
                print("    → Skip: tech BUY but base is weaker than JPY")
                continue
            if direction == "SELL" and strength_score > 0:
                print("    → Skip: tech SELL but base is stronger than JPY")
                continue

            # ML filter
            should_avoid_ml, ml_reason = ml_filter.should_avoid_pair(pair, direction)
            if should_avoid_ml:
                print(f"    → Skip: ML filter - {ml_reason}")
                continue

            # Get prices & levels
            prices = get_live_prices(pair)
            if prices is None:
                print("    → Skip: no live price data")
                continue

            daily_levels = get_support_resistance(pair, granularity="D", count=60, window=3)
            weekly_levels = get_support_resistance(pair, granularity="W", count=52, window=2)

            if None in (daily_levels["support"], daily_levels["resistance"],
                        weekly_levels["support"], weekly_levels["resistance"]):
                print(f"    → Skip: missing S/R levels")
                continue

            # Calculate SL/TP
            if ENABLE_ATR_SLTP:
                atr, z_score = get_atr_with_volatility_context(pair, self.ATR_PERIOD, self.ATR_HISTORY_LOOKBACK)
                if atr is None or atr <= 0:
                    print("    → Skip: ATR unavailable")
                    continue

                sl_multiplier = self.ATR_SL_MULTIPLIER_HIGH_VOL if (z_score or 0) > 1 else \
                                self.ATR_SL_MULTIPLIER_LOW_VOL if (z_score or 0) < -1 else \
                                self.ATR_SL_MULTIPLIER_NORMAL

                sl_distance = atr * sl_multiplier
                tp_distance = sl_distance * self.ATR_RR_MULTIPLE

                if direction == "BUY":
                    entry, sl, tp = prices["ask"], round(prices["ask"] - sl_distance, 3), round(prices["ask"] + tp_distance, 3)
                else:
                    entry, sl, tp = prices["bid"], round(prices["bid"] + sl_distance, 3), round(prices["bid"] - tp_distance, 3)

                sl_reference = f"ATR x{sl_multiplier}"
                target_type = f"ATR x{sl_multiplier * self.ATR_RR_MULTIPLE:.2f}"

                # Weekly level protection
                if direction == "BUY" and entry > weekly_levels["resistance"] - self.MACRO_PROTECTION_PIPS * self.JPY_PIP:
                    print("    → Skip: too close to weekly resistance")
                    continue
                if direction == "SELL" and entry < weekly_levels["support"] + self.MACRO_PROTECTION_PIPS * self.JPY_PIP:
                    print("    → Skip: too close to weekly support")
                    continue

            elif direction == "BUY":
                entry = prices["ask"]
                sl = round(daily_levels["support"] - (SL_BUFFER_PIPS + SPREAD_PIPS) * self.JPY_PIP, 3)
                broke_out = confirmed_breakout(pair, daily_levels["resistance"], "above") if ENABLE_BREAKOUT_CONFIRMATION else entry > daily_levels["resistance"]
                tp = round((weekly_levels["resistance"] if broke_out else daily_levels["resistance"]) - self.FRONT_RUN_PIPS * self.JPY_PIP, 3)
                sl_reference = "Daily Support"
                target_type = "Weekly Resistance" if broke_out else "Daily Resistance"

                if ENABLE_MACRO_PROTECTION and entry > weekly_levels["resistance"] - self.MACRO_PROTECTION_PIPS * self.JPY_PIP:
                    print("    → Skip: too close to weekly resistance")
                    continue
                
                if tp <= entry or sl >= entry:
                    print("    → Skip: invalid SL/TP")
                    continue

            else: # SELL
                entry = prices["bid"]
                sl = round(daily_levels["resistance"] + (SL_BUFFER_PIPS + SPREAD_PIPS) * self.JPY_PIP, 3)
                broke_down = confirmed_breakout(pair, daily_levels["support"], "below") if ENABLE_BREAKOUT_CONFIRMATION else entry < daily_levels["support"]
                tp = round((weekly_levels["support"] if broke_down else daily_levels["support"]) + self.FRONT_RUN_PIPS * self.JPY_PIP, 3)
                sl_reference = "Daily Resistance"
                target_type = "Weekly Support" if broke_down else "Daily Support"

                if entry < weekly_levels["support"] + self.MACRO_PROTECTION_PIPS * self.JPY_PIP:
                    print("    → Skip: too close to weekly support")
                    continue
                if tp >= entry or sl <= entry:
                    print("    → Skip: invalid SL/TP")
                    continue

            # Check minimum risk-reward
            risk = abs(entry - sl)
            reward = abs(tp - entry)
            rr = reward / risk if risk > 0 else 0.0
            if rr < self.MIN_RR:
                print(f"    → Skip: R:R {rr:.2f} below {self.MIN_RR}")
                continue

            # ✅ Pair passes ALL basic conditions
            print(f"    ✅ VALID: {direction} {pair} | R:R {rr:.2f}")
            all_valid_signals.append({
                "pair": pair,
                "action": direction,
                "entry": entry,
                "stop_loss": sl,
                "take_profit": tp,
                "strength_score": strength_score,
                "risk_reward": round(rr, 2),
                "reasoning": f"Aligned {direction} | SL={sl_reference} | TP={target_type}"
            })

        # --- FINAL SELECTION ---
        valid_count = len(all_valid_signals)
        print(f"\n[SELECTION] Total valid pairs: {valid_count}")

        # Rule: Require consensus across multiple pairs
        if valid_count < self.MIN_VALID_PAIRS:
            print(f"  ❌ Only {valid_count} valid pair(s) — NEED AT LEAST {self.MIN_VALID_PAIRS} → NO TRADE")
            return []

        # Pick ONLY the single top pair with largest strength gap vs JPY
        top_pair = max(all_valid_signals, key=lambda x: abs(x["strength_score"]))
        label = "STRONGEST" if top_pair["strength_score"] > 0 else "WEAKEST"

        print(f"  ✅ Selected: {label} vs JPY → {top_pair['action']} {top_pair['pair']} ({top_pair['strength_score']:+.4f})")
        return [top_pair]

    def rules_description(self) -> str:
        news_status = "ON" if ENABLE_NEWS_FILTER else "OFF"
        trend_method = "EMA10/EMA20" if ENABLE_EMA_TREND else "MA5"
        sltp_method = "ATR-based" if ENABLE_ATR_SLTP else "Structural S/R"

        text = f"""
RULES SUMMARY:
  • Minimum valid pairs to trade: {self.MIN_VALID_PAIRS}
  • Timeframes required to align: {self.TREND_ALIGNMENT_REQUIRED}/4
  • Trade only: single pair with largest strength gap vs JPY
  • Trend filter: {trend_method}
  • SL/TP method: {sltp_method}
  • News filter: {news_status}
  • Minimum R:R: {self.MIN_RR}
"""
        if ENABLE_NEWS_FILTER and _news_filter.degraded():
            text += "\n⚠️ NEWS FILTER TEMPORARILY UNAVAILABLE\n"
        return text


# ==========================================
# RUNNER & ENTRY POINT
# ==========================================
def run_strategy(strategy: Strategy) -> tuple[str, list[dict]]:
    print("[STRATEGY] Step 1 — Building currency strength matrix...")
    scores = build_strength_matrix()
    strength_report = format_strength_ranking(scores)
    print(strength_report)

    signals = strategy.generate_signals(scores)

    report = "=== JPY STRENGTH TRADE STRATEGY REPORT ===\n\n"
    report += strength_report + "\n\n=== FINAL SIGNAL ===\n"
    if signals:
        s = signals[0]
        report += f"• {s['action']} {s['pair']} | Entry: {s['entry']} | SL: {s['stop_loss']} | TP: {s['take_profit']} | R:R={s['risk_reward']}\n"
    else:
        report += "• No qualifying signals — HOLD\n"

    report += strategy.rules_description()
    return report, signals


_active_strategy = JPYTrendStrategy()


def analyze_custom_strategy() -> str:
    report, signals = run_strategy(_active_strategy)
    analyze_custom_strategy._last_signal = signals[0] if signals else None
    return report


analyze_custom_strategy._last_signal = None


def get_last_signal() -> dict | None:
    return analyze_custom_strategy._last_signal

def run_trading_cycle():
    from custom_strategy import analyze_custom_strategy, get_last_signal

    print("\n=== START TRADING CYCLE ===")
    try:
        report = analyze_custom_strategy()
        signal = get_last_signal()
        if not signal:
            print("[STRATEGY] No signal — HOLD")
            return
        sentiment = get_latest_news_sentiment()
        news = get_news_risk_bias(signal["pair"])
        if news["impact"] >= 2:
            print("[NEWS RISK] High impact — HOLD")
            return
        ok, reason = validate_signal_with_fundamentals(signal, sentiment)
        if not ok:
            print(f"[GEMINI REJECT] {reason}")
            return
        final_signal, _ = get_ensemble_consensus(f"{report}\n{sentiment}")
        if final_signal:
            execute_market_trade(final_signal)
    except Exception as e:
        print(f"[CYCLE ERROR] {e}")
