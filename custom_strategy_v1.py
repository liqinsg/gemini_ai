# custom_strategy_v1.py — Unified: V2 instrumentation + ML filter + ATR floor + Consensus guard
import joblib
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from utils.range_detector import is_sideways
from utils import get_support_resistance, oanda_client
from utils.signal_instrumentation import (
    classify_volatility,
    classify_price_location,
    level_cluster_strength,
    log_signal_observation,
    log_executed_signal,
)
import config as _config
from config import (
    TRADE_PAIRS,
    SL_BUFFER_PIPS,
    SPREAD_PIPS,
    MIN_DOMINANCE_RATIO,
    ENABLE_VOLATILITY_NORMALIZED_DOMINANCE,
    ENABLE_ATR_SLTP,
    ENABLE_NEWS_FILTER,
    ENABLE_EMA_TREND,
    ENABLE_ATR_NORMALIZED_STRENGTH,
    ENABLE_STRENGTH_ACCELERATION,
    STRENGTH_ACCELERATION_WEIGHT,
    ENABLE_BREAKOUT_CONFIRMATION,
    BREAKOUT_CONFIRMATION_CLOSES,
    GEMINI_API_KEY,
    GEMINI_NEWS_MODEL,
    GEMINI_NEWS_FALLBACK_MODEL,
    NEWS_LOG_PATH,
    NEWS_CURRENCIES,
    REQUIRE_ALIGNED,
    ALIGNMENT_THRESHOLD,
    STRENGTH_GAP_THRESHOLD,
    MIN_STRENGTH_SCORE,
    STRENGTH_CUTOFF_RATIO,
    MIN_VALID_PAIRS_TO_TRADE,
    CHECK_INTERVAL_MINUTES,
    DEBUG_SLTP,
    ENABLE_MACRO_PROTECTION,
    TP_PIPS,
    ENABLE_RANGE_DETECTOR,
    SKIP_SIDEWAYS_PAIRS,
    TRADE_TOP_PAIRS,
    SIGNAL_TIMEFRAMES,
)
from utils.strategy_helpers import (
    get_candles,
    _atr_from_candles,
    get_atr_with_volatility_context,
    get_dominance_normalizer,
    get_pair_momentum,
    build_strength_matrix,
    format_strength_ranking,
    get_ma5_position,
    _ema,
    get_ema_trend_position,
    get_trend_position,
    check_ma5_alignment,
    get_slope_diagnostics,
    get_previous_day_low,
    get_previous_day_high,
    confirmed_breakout,
    get_live_prices,
    NewsFilter,
)
from utils.ml_confirmation import ml_filter

# # ==========================================
# # ATR MINIMUM FILTER — Low-volatility protection
# # Thresholds set LOOSE initially; tune upward if needed
# # ==========================================
# ENABLE_ATR_MINIMUM_FILTER = True
# ATR_MIN_ABSOLUTE = 0.060        # JPY pairs: ~0.6 pips floor
# ATR_MIN_RELATIVE_PCT = 0.045    # 0.045% of entry price floor

OANDA_ACCOUNT_ID = getattr(_config, "OANDA_ACCOUNT_ID", None) or os.getenv(
    "OANDA_ACCOUNT_ID"
)
if not OANDA_ACCOUNT_ID:
    print("[STRATEGY] WARNING: OANDA_ACCOUNT_ID not found in config.py or environment.")

JPY_TRADE_PAIRS = [p for p in TRADE_PAIRS if p.endswith("_JPY")]

def _signal_bar_time() -> str:
    now = datetime.now(timezone.utc)
    minute = now.minute - (now.minute % CHECK_INTERVAL_MINUTES)
    return now.replace(minute=minute, second=0, microsecond=0).isoformat()

if _dropped := [p for p in TRADE_PAIRS if not p.endswith("_JPY")]:
    print(
        f"[STRATEGY] WARNING: non-JPY pairs found in TRADE_PAIRS and will be IGNORED: {_dropped}"
    )

_news_filter = NewsFilter()

# ==========================================
# Consensus / Dominance Guard state
# ==========================================
_last_dominance_guard_triggered = False

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
# JPY TREND STRATEGY — Unified Edition
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

    MIN_VALID_PAIRS = MIN_VALID_PAIRS_TO_TRADE
    TREND_ALIGNMENT_REQUIRED = ALIGNMENT_THRESHOLD
    TRADE_TOP_PAIRS = TRADE_TOP_PAIRS
    SKIP_SIDEWAYS_PAIRS = SKIP_SIDEWAYS_PAIRS
    STRENGTH_GAP_THRESHOLD = STRENGTH_GAP_THRESHOLD
    MIN_STRENGTH_SCORE = MIN_STRENGTH_SCORE
    STRENGTH_CUTOFF_RATIO = STRENGTH_CUTOFF_RATIO

    def __init__(
        self,
        trade_pairs: list[str] | None = None,
        *,
        enable_atr_min_filter: bool = True,
        atr_min_absolute: float = 0.060,
        atr_min_relative_pct: float = 0.045,
    ):
        self.trade_pairs = trade_pairs if trade_pairs is not None else JPY_TRADE_PAIRS
        self.ENABLE_ATR_MIN_FILTER = enable_atr_min_filter
        self.ATR_MIN_ABSOLUTE = atr_min_absolute
        self.ATR_MIN_RELATIVE_PCT = atr_min_relative_pct
        if not self.trade_pairs:
            print("[STRATEGY] WARNING: JPYTrendStrategy has no trade pairs configured.")
                        
    def jpy_strength_rank(self, scores: dict) -> dict[str, float]:
        jpy_score = scores.get("JPY", 0.0)
        return {
            pair: scores.get(pair.split("_")[0], 0.0) - jpy_score
            for pair in self.trade_pairs
        }

    def generate_signals(self, scores: dict) -> list[dict]:
        global _last_dominance_guard_triggered
        _last_dominance_guard_triggered = False
        _news_filter.reset_cycle()

        cycle_id = datetime.now(timezone.utc).isoformat()
        pair_diagnostics: dict[str, dict] = {}

        jpy_ranks = self.jpy_strength_rank(scores)
        max_gap = max(abs(v) for v in jpy_ranks.values()) if jpy_ranks else 0.0
        if max_gap < self.MIN_MARKET_STRENGTH:
            print(f"  [STRATEGY] Global JPY gap ({max_gap:.4f}) below minimum floor.")
            max_gap = self.MIN_MARKET_STRENGTH

        ranked_pairs = sorted(jpy_ranks.items(), key=lambda x: abs(x[1]), reverse=True)
        print(
            f"\n  JPY cross strength ranking: {' > '.join(f'{p}({s:+.3f})' for p, s in ranked_pairs)}"
        )
        print(
            f"\n[STRATEGY] Checking pairs (need ≥{self.TREND_ALIGNMENT_REQUIRED} aligned timeframes)..."
        )

        all_valid_signals = []
        for pair, strength_score in ranked_pairs:
            print(f"\n  [{pair}] (strength vs JPY: {strength_score:+.4f})")

            # Strength cutoff
            dynamic_cutoff = max_gap * self.STRENGTH_CUTOFF_RATIO
            if abs(strength_score) < dynamic_cutoff:
                print(
                    f"    → Skip: strength gap {abs(strength_score):.4f} below {dynamic_cutoff:.4f}"
                )
                continue

            # News filter
            should_avoid, news_reason = _news_filter.should_avoid_pair(pair)
            if should_avoid:
                print(f"    → Skip: news risk - {news_reason}")
                continue

            # Sideways filter
            if self.SKIP_SIDEWAYS_PAIRS:
                sideways, reason, metrics = is_sideways(pair)
                if sideways:
                    print(
                        f"    → Skip: sideways market — {reason} | Range: {metrics.get('range_pct', 'N/A')}%"
                    )
                    continue
                else:
                    print(f"    → ✅ Trending market — {reason}")

            # Trend alignment
            direction = check_ma5_alignment(
                pair, require_aligned=self.TREND_ALIGNMENT_REQUIRED
            )
            if direction is None:
                print(
                    f"    → Skip: mixed alignment (need ≥{self.TREND_ALIGNMENT_REQUIRED} same)"
                )
                continue

            # Slope diagnostics (non-blocking instrumentation)
            try:
                slope_diag = get_slope_diagnostics(pair, SIGNAL_TIMEFRAMES, direction)
                pair_diagnostics.setdefault(pair, {})["slope"] = slope_diag
                log_signal_observation(
                    cycle_id=cycle_id, pair=pair, stage="alignment",
                    direction=direction, strength_score=strength_score, slope=slope_diag,
                )
            except Exception as _v2_err:
                print(f"    [V2-INSTRUMENTATION] slope diagnostics failed: {_v2_err}")

            # Strength-direction alignment
            if direction == "BUY" and strength_score < self.MIN_STRENGTH_SCORE:
                print(f"    → Skip: BUY strength {strength_score:+.4f} < MIN {self.MIN_STRENGTH_SCORE}")
                continue
            if direction == "SELL" and strength_score > -self.MIN_STRENGTH_SCORE:
                print(f"    → Skip: SELL strength {strength_score:+.4f} > MIN {-self.MIN_STRENGTH_SCORE}")
                continue

            # ✅ ML confirmation filter
            should_avoid_ml, ml_reason = ml_filter.should_avoid_pair(pair, direction)
            if should_avoid_ml:
                print(f"    → Skip: ML filter - {ml_reason}")
                continue

            # Price data
            prices = get_live_prices(pair)
            if prices is None:
                print("    → Skip: no live price data")
                continue

            daily_levels = get_support_resistance(
                pair, granularity="D", count=60, window=3, return_all_levels=True
            )
            weekly_levels = get_support_resistance(
                pair, granularity="W", count=52, window=2
            )
            if None in (
                daily_levels["support"], daily_levels["resistance"],
                weekly_levels["support"], weekly_levels["resistance"],
            ):
                print("    → Skip: missing S/R levels")
                continue

            # SL/TP calculation
            if ENABLE_ATR_SLTP:
                atr, z_score = get_atr_with_volatility_context(
                    pair, self.ATR_PERIOD, self.ATR_HISTORY_LOOKBACK
                )
                if atr is None or atr <= 0:
                    print("    → Skip: ATR unavailable")
                    continue
                
                # ==============================================
                # ATR MINIMUM VOLATILITY FILTER (injected params)
                # ==============================================
                if self.ENABLE_ATR_MIN_FILTER:
                    _entry_ref = prices["ask"] if direction == "BUY" else prices["bid"]
                    _atr_rel_pct = (atr / _entry_ref) * 100
                    if atr < self.ATR_MIN_ABSOLUTE or _atr_rel_pct < self.ATR_MIN_RELATIVE_PCT:
                        print(
                            f"    → 🚫 SKIP LOW-VOL: ATR={atr:.4f} < {self.ATR_MIN_ABSOLUTE:.4f} | "
                            f"REL={_atr_rel_pct:.3f}% < {self.ATR_MIN_RELATIVE_PCT:.3f}%"
                        )
                        continue
                    print(
                        f"    ✅ VOL-OK: ATR={atr:.4f}≥{self.ATR_MIN_ABSOLUTE:.4f} | "
                        f"REL={_atr_rel_pct:.3f}%≥{self.ATR_MIN_RELATIVE_PCT:.3f}%"
                    )
                # ==============================================


                sl_multiplier = (
                    self.ATR_SL_MULTIPLIER_HIGH_VOL if (z_score or 0) > 1 else
                    self.ATR_SL_MULTIPLIER_LOW_VOL if (z_score or 0) < -1 else
                    self.ATR_SL_MULTIPLIER_NORMAL
                )
                sl_distance = atr * sl_multiplier
                tp_distance = sl_distance * self.ATR_RR_MULTIPLE

                entry, sl, tp = (
                    (prices["ask"], round(prices["ask"] - sl_distance, 3),
                     round(prices["ask"] + tp_distance, 3))
                    if direction == "BUY" else
                    (prices["bid"], round(prices["bid"] + sl_distance, 3),
                     round(prices["bid"] - tp_distance, 3))
                )
                sl_reference = f"ATR x{sl_multiplier}"
                target_type = f"ATR x{sl_multiplier * self.ATR_RR_MULTIPLE:.2f}"

                # Volatility/price-location diagnostics (non-blocking)
                try:
                    vol_class = classify_volatility(z_score)
                    adverse_level = (daily_levels["resistance"] if direction == "BUY"
                                     else daily_levels["support"])
                    adverse_level_pool = (daily_levels.get("all_resistances", [])
                                          if direction == "BUY" else
                                          daily_levels.get("all_supports", []))
                    price_loc = classify_price_location(
                        entry=entry, adverse_level=adverse_level,
                        atr=atr, pip_size=self.JPY_PIP
                    )
                    price_loc["level_cluster_strength"] = level_cluster_strength(
                        adverse_level_pool, adverse_level, tolerance=10 * self.JPY_PIP
                    )
                    pair_diagnostics.setdefault(pair, {})["volatility"] = {
                        "class": vol_class, "z_score": z_score, "atr": atr,
                    }
                    pair_diagnostics[pair]["price_location"] = price_loc
                    log_signal_observation(
                        cycle_id=cycle_id, pair=pair, stage="volatility_and_price_location",
                        direction=direction, strength_score=strength_score,
                        volatility_class=vol_class, z_score=z_score,
                        atr=atr, price_location=price_loc,
                    )
                except Exception as _v2_err:
                    print(f"    [V2-INSTRUMENTATION] vol/price-location failed: {_v2_err}")

                # Weekly level protection
                if (ENABLE_MACRO_PROTECTION and direction == "BUY" and entry
                    > weekly_levels["resistance"] - self.MACRO_PROTECTION_PIPS * self.JPY_PIP):
                    print("    → Skip: too close to weekly resistance")
                    continue
                if (ENABLE_MACRO_PROTECTION and direction == "SELL" and entry
                    < weekly_levels["support"] + self.MACRO_PROTECTION_PIPS * self.JPY_PIP):
                    print("    → Skip: too close to weekly support")
                    continue

                if DEBUG_SLTP:
                    print(
                        f"    [DEBUG] entry={entry} sl={sl} tp={tp} "
                        f"daily_sup={daily_levels['support']} daily_res={daily_levels['resistance']} "
                        f"weekly_sup={weekly_levels['support']} weekly_res={weekly_levels['resistance']}"
                    )
                if direction == "BUY" and (tp <= entry or sl >= entry):
                    print("    → Skip: invalid SL/TP")
                    continue
                if direction == "SELL" and (tp >= entry or sl <= entry):
                    print("    → Skip: invalid SL/TP")
                    continue

            else:
                # Structural S/R mode
                if direction == "BUY":
                    entry = prices["ask"]
                    sl = round(
                        daily_levels["support"] - (SL_BUFFER_PIPS + SPREAD_PIPS) * self.JPY_PIP, 3
                    )
                    broke_out = (
                        confirmed_breakout(pair, daily_levels["resistance"], "above")
                        if ENABLE_BREAKOUT_CONFIRMATION else entry > daily_levels["resistance"]
                    )
                    if broke_out and weekly_levels["resistance"] <= entry:
                        tp = round(entry + TP_PIPS * self.JPY_PIP, 3)
                        target_type = "Fixed target (stale weekly level)"
                    else:
                        tp = round(
                            (weekly_levels["resistance"] if broke_out else daily_levels["resistance"])
                            - self.FRONT_RUN_PIPS * self.JPY_PIP, 3
                        )
                        target_type = "Weekly Resistance" if broke_out else "Daily Resistance"
                    sl_reference = "Daily Support"
                    if (ENABLE_MACRO_PROTECTION and entry
                        > weekly_levels["resistance"] - self.MACRO_PROTECTION_PIPS * self.JPY_PIP):
                        print("    → Skip: too close to weekly resistance")
                        continue
                    if tp <= entry or sl >= entry:
                        print("    → Skip: invalid SL/TP")
                        continue
                else:
                    entry = prices["bid"]
                    sl = round(
                        daily_levels["resistance"] + (SL_BUFFER_PIPS + SPREAD_PIPS) * self.JPY_PIP, 3
                    )
                    broke_down = (
                        confirmed_breakout(pair, daily_levels["support"], "below")
                        if ENABLE_BREAKOUT_CONFIRMATION else entry < daily_levels["support"]
                    )
                    if broke_down and weekly_levels["support"] >= entry:
                        tp = round(entry - TP_PIPS * self.JPY_PIP, 3)
                        target_type = "Fixed target (stale weekly level)"
                    else:
                        tp = round(
                            (weekly_levels["support"] if broke_down else daily_levels["support"])
                            + self.FRONT_RUN_PIPS * self.JPY_PIP, 3
                        )
                        target_type = "Weekly Support" if broke_down else "Daily Support"
                    sl_reference = "Daily Resistance"
                    if (ENABLE_MACRO_PROTECTION and entry
                        < weekly_levels["support"] + self.MACRO_PROTECTION_PIPS * self.JPY_PIP):
                        print("    → Skip: too close to weekly support")
                        continue
                    if tp >= entry or sl <= entry:
                        print("    → Skip: invalid SL/TP")
                        continue

            # Minimum R:R check
            risk = abs(entry - sl)
            reward = abs(tp - entry)
            rr = reward / risk if risk > 0 else 0.0
            if rr < self.MIN_RR:
                print(f"    → Skip: R:R {rr:.2f} below {self.MIN_RR}")
                continue

            print(f"    ✅ VALID: {direction} {pair} | R:R {rr:.2f}")
            all_valid_signals.append({
                "pair": pair,
                "action": direction,
                "bar_time": _signal_bar_time(),
                "entry": entry,
                "stop_loss": sl,
                "take_profit": tp,
                "strength_score": strength_score,
                "risk_reward": round(rr, 2),
                "reasoning": f"Aligned {direction} | SL={sl_reference} | TP={target_type}",
            })

        # --- FINAL SELECTION ---
        valid_count = len(all_valid_signals)
        print(f"\n[SELECTION] Total valid pairs: {valid_count}")

        if valid_count < self.MIN_VALID_PAIRS:
            print(
                f"  ❌ Only {valid_count} valid pair(s) — NEED AT LEAST {self.MIN_VALID_PAIRS} → NO TRADE"
            )
            _last_dominance_guard_triggered = True
            print("  [STRATEGY] Insufficient consensus — flagging for closure via runner.")
            return []

        top_pair = max(all_valid_signals, key=lambda x: abs(x["strength_score"]))
        label = "STRONGEST" if top_pair["strength_score"] > 0 else "WEAKEST"

        if len(all_valid_signals) > 1:
            weakest_pair = min(all_valid_signals, key=lambda x: abs(x["strength_score"]))
            print(
                f"  📊 [COMPARE] Strongest: {top_pair['action']} {top_pair['pair']} ({top_pair['strength_score']:+.4f})"
            )
            print(
                f"  📊 [COMPARE] Weakest:   {weakest_pair['action']} {weakest_pair['pair']} ({weakest_pair['strength_score']:+.4f})"
            )
        else:
            print("  📊 [COMPARE] Only 1 valid pair — no comparison possible")

        print(
            f"  ✅ Selected: {label} vs JPY → {top_pair['action']} {top_pair['pair']} ({top_pair['strength_score']:+.4f})"
        )

        # Log executed signal (non-blocking)
        try:
            log_executed_signal(
                cycle_id=cycle_id, pair=top_pair["pair"], direction=top_pair["action"],
                diagnostics=pair_diagnostics.get(top_pair["pair"], {}),
            )
        except Exception as _v2_err:
            print(f"  [V2-INSTRUMENTATION] executed-signal logging failed: {_v2_err}")

        return all_valid_signals

    def rules_description(self) -> str:
        news_status = "ON" if ENABLE_NEWS_FILTER else "OFF"
        trend_method = "EMA10/EMA20" if ENABLE_EMA_TREND else "MA5"
        sltp_method = "ATR-based" if ENABLE_ATR_SLTP else "Structural S/R"
        vol_filter = (
            f"ON (floor {self.ATR_MIN_ABSOLUTE} / {self.ATR_MIN_RELATIVE_PCT}%)"
            if self.ENABLE_ATR_MIN_FILTER else "OFF"
        )
        text = f"""
RULES SUMMARY:
  • Minimum valid pairs to trade: {self.MIN_VALID_PAIRS}
  • Timeframes aligned: {self.TREND_ALIGNMENT_REQUIRED}/{len(SIGNAL_TIMEFRAMES)}
  • Selection: STRONGEST strength gap
  • Trend filter: {trend_method}
  • SL/TP method: {sltp_method}
  • ATR minimum filter: {vol_filter}
  • News filter: {news_status}
  • Sideways filter: {"ON" if SKIP_SIDEWAYS_PAIRS else "OFF"}
  • Minimum R:R: {self.MIN_RR}
"""
        if ENABLE_NEWS_FILTER and _news_filter.degraded():
            text += "\n⚠️ NEWS FILTER TEMPORARILY UNAVAILABLE\n"
        return text

# ==========================================
# RUNNER & ENTRY POINT
# ==========================================
_active_strategy = JPYTrendStrategy()

def run_strategy(strategy: Strategy, scores: dict | None = None) -> tuple[str, list[dict], float]:
    print("[STRATEGY] Step 1 — Building currency strength matrix...")
    if scores is None:
        scores = build_strength_matrix()
    strength_report = format_strength_ranking(scores)
    print(strength_report)
    score_gap = (max(scores.values()) - min(scores.values())) if scores else 0.0
    signals = strategy.generate_signals(scores)
    report = "=== JPY STRENGTH TRADE STRATEGY REPORT ===\n\n"
    report += strength_report + "\n\n=== FINAL SIGNAL ===\n"
    if signals:
        s = signals[0]
        report += f"• {s['action']} {s['pair']} | Entry: {s['entry']} | SL: {s['stop_loss']} | TP: {s['take_profit']} | R:R={s['risk_reward']}\n"
    else:
        report += "• No qualifying signals — HOLD\n"
    report += strategy.rules_description()
    return report, signals, score_gap

def analyze_custom_strategy(scores: dict | None = None) -> str:
    report, signals, score_gap = run_strategy(_active_strategy, scores=scores)
    analyze_custom_strategy._last_signal = signals[0] if signals else None
    analyze_custom_strategy._last_valid_signals = signals
    analyze_custom_strategy._last_score_gap = score_gap
    return report

analyze_custom_strategy._last_signal = None
analyze_custom_strategy._last_valid_signals = []
analyze_custom_strategy._last_score_gap = 0.0

def get_last_signal() -> dict | None:
    return analyze_custom_strategy._last_signal

def get_last_score_gap() -> float:
    return getattr(analyze_custom_strategy, "_last_score_gap", 0.0)

def get_dominance_guard_status() -> bool:
    return getattr(analyze_custom_strategy, "_last_dominance_guard_triggered", False)

def get_top_signals(n: int = 2) -> list[dict]:
    valid = getattr(analyze_custom_strategy, "_last_valid_signals", []) or []
    sorted_valid = sorted(valid, key=lambda x: abs(x["strength_score"]), reverse=True)
    return sorted_valid[:n]