# custom_strategy_v1.py — Unified: V2 instrumentation + ML filter + ATR floor + Consensus guard
import joblib
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from utils.range_detector import is_sideways
from utils import get_support_resistance
from utils.signal_instrumentation import (
    classify_volatility,
    classify_price_location,
    level_cluster_strength,
    log_signal_observation,
    log_executed_signal,
)
import config as _config
import config_bot_v3 as _config_bot_v3
from config_bot_v3 import PIP_SIZE_BY_QUOTE
from config import (
    TRADE_PAIRS,
    STRENGTH_PAIRS,
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
    MIN_DOMINANT_PAIRS,
    MIN_STRENGTH_PASSING_PAIRS,
    CHECK_INTERVAL_MINUTES,
    DEBUG_SLTP,
    ENABLE_MACRO_PROTECTION,
    TP_PIPS,
    ENABLE_RANGE_DETECTOR,
    SKIP_SIDEWAYS_PAIRS,
    TRADE_TOP_PAIRS,
    SIGNAL_TIMEFRAMES,
    ENABLE_ATR_MIN_FILTER,
    ATR_MIN_PIPS,
    ATR_MIN_RELATIVE_PCT,
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

# ==========================================
# Consensus / Dominance Guard state
# ==========================================
_last_dominance_guard_triggered = False

OANDA_ACCOUNT_ID = getattr(_config, "OANDA_ACCOUNT_ID", None) or os.getenv(
    "OANDA_ACCOUNT_ID"
)
if not OANDA_ACCOUNT_ID:
    print("[STRATEGY] WARNING: OANDA_ACCOUNT_ID not found in config.py or environment.")

def _signal_bar_time() -> str:
    now = datetime.now(timezone.utc)
    minute = now.minute - (now.minute % CHECK_INTERVAL_MINUTES)
    return now.replace(minute=minute, second=0, microsecond=0).isoformat()

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
# BASE-CURRENCY TREND STRATEGY — Generic v4
# Parameterized by quote_ccy → supports JPY, USD, ... any group
# ==========================================
class BaseCurrencyTrendStrategy(Strategy):
    """
    Generic strength-trend strategy for any quote-currency group.
    Replaces hardcoded JPYTrendStrategy; same logic, parametric.

    Usage:
        s = BaseCurrencyTrendStrategy(quote_ccy="JPY")
        s = BaseCurrencyTrendStrategy(quote_ccy="USD")
        signals = s.generate_signals(scores)
    """

    def __init__(
        self,
        quote_ccy: str = "JPY",
        trade_pairs: list[str] | None = None,
        *,
        # Dominance ratio params
        dominance_ratio_enabled: bool | None = None,
        dominance_ratio_threshold: float | None = None,
        gap_separation_threshold: float | None = None,
        dominance_override_enabled: bool | None = None,
        dominance_override_threshold: float | None = None,
        # ATR filter params
        enable_atr_min_filter: bool | None = None,
        atr_min_pips: float | None = None,
        atr_min_relative_pct: float | None = None,
    ):
        self.quote_ccy = quote_ccy.upper()
        self.pip = PIP_SIZE_BY_QUOTE.get(self.quote_ccy, 0.0001)

        if trade_pairs is None:
            trade_pairs = [p for p in STRENGTH_PAIRS if p.endswith(f"_{self.quote_ccy}")]
        self.trade_pairs = trade_pairs

        # --- Dominance ratio filter ---
        # Source order: explicit ctor arg → config_bot_v3 (v4 generic) → config.py → hardcoded default
        self.DOMINANCE_RATIO_ENABLED = (
            dominance_ratio_enabled if dominance_ratio_enabled is not None
            else getattr(_config_bot_v3, "DOMINANCE_RATIO_ENABLED", getattr(_config, "DOMINANCE_RATIO_ENABLED", True))
        )
        self.DOMINANCE_RATIO_THRESHOLD = (
            dominance_ratio_threshold if dominance_ratio_threshold is not None
            else getattr(_config_bot_v3, "DOMINANCE_RATIO_THRESHOLD", getattr(_config, "DOMINANCE_RATIO_THRESHOLD", 2.0))
        )
        self.GAP_SEPARATION_THRESHOLD = (
            gap_separation_threshold if gap_separation_threshold is not None
            else getattr(_config_bot_v3, "GAP_SEPARATION_THRESHOLD", getattr(_config, "GAP_SEPARATION_THRESHOLD", 1.3))
        )
        self.DOMINANCE_OVERRIDE_ENABLED = (
            dominance_override_enabled if dominance_override_enabled is not None
            else getattr(_config_bot_v3, "DOMINANCE_OVERRIDE_ENABLED", getattr(_config, "DOMINANCE_OVERRIDE_ENABLED", True))
        )
        self.DOMINANCE_OVERRIDE_THRESHOLD = (
            dominance_override_threshold if dominance_override_threshold is not None
            else getattr(_config_bot_v3, "DOMINANCE_OVERRIDE_THRESHOLD", getattr(_config, "DOMINANCE_OVERRIDE_THRESHOLD", 2.4))
        )

        # --- Core strategy thresholds (all from generic config) ---
        self.MIN_MARKET_STRENGTH = getattr(_config, "MIN_MARKET_STRENGTH", 0.03)
        self.FRONT_RUN_PIPS = getattr(_config, "FRONT_RUN_PIPS", 15)
        self.MACRO_PROTECTION_PIPS = getattr(_config, "MACRO_PROTECTION_PIPS", 10)
        self.MIN_RR = getattr(_config, "MIN_RR", 1.2)
        self.ATR_PERIOD = getattr(_config, "ATR_PERIOD", 14)
        self.ATR_HISTORY_LOOKBACK = getattr(_config, "ATR_HISTORY_LOOKBACK", 50)
        self.ATR_SL_MULTIPLIER_NORMAL = getattr(_config, "ATR_SL_MULTIPLIER_NORMAL", 2.2)
        self.ATR_SL_MULTIPLIER_HIGH_VOL = getattr(_config, "ATR_SL_MULTIPLIER_HIGH_VOL", 2.8)
        self.ATR_SL_MULTIPLIER_LOW_VOL = getattr(_config, "ATR_SL_MULTIPLIER_LOW_VOL", 1.8)
        self.ATR_RR_MULTIPLE = getattr(_config, "ATR_RR_MULTIPLE", 2.0)

        self.MIN_VALID_PAIRS = getattr(_config, "MIN_VALID_PAIRS_TO_TRADE", 1)
        self.MIN_DOMINANT_PAIRS = getattr(_config, "MIN_DOMINANT_PAIRS", 1)
        self.TREND_ALIGNMENT_REQUIRED = getattr(_config, "ALIGNMENT_THRESHOLD", 3)
        self.TRADE_TOP_PAIRS = getattr(_config, "TRADE_TOP_PAIRS", 3)
        self.MIN_STRENGTH_PASSING_PAIRS = getattr(_config, "MIN_STRENGTH_PASSING_PAIRS", 2)
        self.SKIP_SIDEWAYS_PAIRS = getattr(_config, "SKIP_SIDEWAYS_PAIRS", False)
        self.STRENGTH_GAP_THRESHOLD = getattr(_config, "STRENGTH_GAP_THRESHOLD", 1.5)
        self.MIN_STRENGTH_SCORE = getattr(_config, "MIN_STRENGTH_SCORE", 0.15)
        self.STRENGTH_CUTOFF_RATIO = getattr(_config, "STRENGTH_CUTOFF_RATIO", 0.4)

        # --- ATR min filter (injectable) ---
        self.ENABLE_ATR_MIN_FILTER = (
            enable_atr_min_filter if enable_atr_min_filter is not None
            else getattr(_config, "ENABLE_ATR_MIN_FILTER", True)
        )
        self.ATR_MIN_ABSOLUTE_PIPS = (
            atr_min_pips if atr_min_pips is not None
            else getattr(_config_bot_v3, "ATR_MIN_PIPS", getattr(_config, "ATR_MIN_PIPS", 6.0))
        )
        self.ATR_MIN_ABSOLUTE = self.ATR_MIN_ABSOLUTE_PIPS * self.pip
        self.ATR_MIN_RELATIVE_PCT = (
            atr_min_relative_pct if atr_min_relative_pct is not None
            else getattr(_config, "ATR_MIN_RELATIVE_PCT", 0.045)
        )

        if not self.trade_pairs:
            print(f"[STRATEGY] WARNING: BaseCurrencyTrendStrategy(quote={self.quote_ccy}) has no trade pairs.")

    # -------------------------------------------------------
    # Group strength rank — base_ccy vs quote_ccy
    # -------------------------------------------------------
    def group_strength_rank(self, scores: dict) -> dict[str, float]:
        quote_score = scores.get(self.quote_ccy, 0.0)
        return {
            pair: scores.get(pair.split("_")[0], 0.0) - quote_score
            for pair in self.trade_pairs
        }

    # -------------------------------------------------------
    # Dominance ratio filter (LEADER + OVERRIDE modes)
    # -------------------------------------------------------
    def _apply_dominance_ratio_filter(self, group_ranks: dict) -> dict:
        abs_scores = sorted(abs(v) for v in group_ranks.values())
        n = len(abs_scores)
        if n <= 1:
            return {p: {"score": s, "override": False} for p, s in group_ranks.items()}

        if n % 2 == 1:
            median = abs_scores[n // 2]
        else:
            median = (abs_scores[n // 2 - 1] + abs_scores[n // 2]) / 2

        top1 = abs_scores[-1]

        if median < 1e-6:
            best_pair = max(group_ranks, key=lambda p: abs(group_ranks[p]))
            print(f"  [DOMINANCE-{self.quote_ccy}] All gaps near zero → keep strongest: {best_pair}")
            return {best_pair: {"score": group_ranks[best_pair], "override": False}}

        top_separation = top1 / median

        # Resonance mode → check for group consensus
        if top_separation < self.GAP_SEPARATION_THRESHOLD:
            all_scores = list(group_ranks.values())
            all_same_sign = all(s > 0 for s in all_scores) or all(s < 0 for s in all_scores)

            if all_same_sign and self.DOMINANCE_OVERRIDE_ENABLED:
                sorted_pairs = sorted(group_ranks.items(), key=lambda x: abs(x[1]), reverse=True)
                top_pair, top_score = sorted_pairs[0]
                print(
                    f"  [DOMINANCE-{self.quote_ccy}] RESONANCE + GROUP CONSENSUS: "
                    f"top1/median={top_separation:.2f}x, ALL {len(sorted_pairs)} pairs agree on sign → "
                    f"⚡ TOP PAIR OVERRIDE: {top_pair} ({top_score:+.3f})"
                )
                result = {top_pair: {"score": top_score, "override": True}}
                for pair, score in sorted_pairs[1:]:
                    result[pair] = {"score": score, "override": False}
                return result

            print(
                f"  [DOMINANCE-{self.quote_ccy}] RESONANCE: top1/median={top_separation:.2f}x "
                f"< {self.GAP_SEPARATION_THRESHOLD}x → no filtering"
            )
            return {p: {"score": s, "override": False} for p, s in group_ranks.items()}

        threshold_ratio = self.DOMINANCE_RATIO_THRESHOLD
        override_ratio = self.DOMINANCE_OVERRIDE_THRESHOLD if self.DOMINANCE_OVERRIDE_ENABLED else 999.0

        result = {}
        for pair, score in group_ranks.items():
            ratio = abs(score) / median
            if ratio >= override_ratio:
                print(
                    f"  ⚡ [OVERRIDE-{self.quote_ccy}] {pair}: ratio={ratio:.1f}x ≥ {override_ratio}x "
                    f"→ SKIP ALL TRADITIONAL FILTERS"
                )
                result[pair] = {"score": score, "override": True}
            elif ratio >= threshold_ratio:
                print(
                    f"  ✓ [DOMINANCE-{self.quote_ccy}] {pair}: ratio={ratio:.1f}x ≥ {threshold_ratio}x "
                    f"→ proceed to checks"
                )
                result[pair] = {"score": score, "override": False}
            else:
                print(
                    f"  ✗ [DOMINANCE-{self.quote_ccy}] {pair}: ratio={ratio:.1f}x < {threshold_ratio}x "
                    f"→ filtered"
                )

        if not result:
            print(f"  ⚠️ [DOMINANCE-{self.quote_ccy}] Nothing passed → fallback to all for resonance check")
            return {p: {"score": s, "override": False} for p, s in group_ranks.items()}

        return result

    # -------------------------------------------------------
    # Core signal generation (override-aware)
    # -------------------------------------------------------
    def generate_signals(self, scores: dict) -> list[dict]:
        group_ranks = self.group_strength_rank(scores)

        if self.DOMINANCE_RATIO_ENABLED:
            filtered = self._apply_dominance_ratio_filter(group_ranks)
        else:
            filtered = {p: {"score": s, "override": False} for p, s in group_ranks.items()}

        max_gap = max(abs(v["score"]) for v in filtered.values()) if filtered else 0.0
        if max_gap < self.MIN_MARKET_STRENGTH:
            print(f"  [STRATEGY-{self.quote_ccy}] Global gap ({max_gap:.4f}) below floor, using floor.")
            max_gap = self.MIN_MARKET_STRENGTH

        ranked_pairs = sorted(filtered.items(), key=lambda x: abs(x[1]["score"]), reverse=True)
        _ranked_str = " > ".join(
            f"{p}({v['score']:+.3f})" for p, v in ranked_pairs
        )
        print(f"\n  [{self.quote_ccy} cross strength] {_ranked_str}")
        print(
            f"\n[STRATEGY-{self.quote_ccy}] Checking pairs "
            f"(need ≥{self.TREND_ALIGNMENT_REQUIRED} aligned timeframes)..."
        )

        all_valid_signals = []
        strength_pass_count = 0

        for pair, info in ranked_pairs:
            strength_score = info["score"]
            is_override = info["override"]

            print(f"\n  [{pair}] (strength vs {self.quote_ccy}: {strength_score:+.4f})")

            dynamic_cutoff = max_gap * self.STRENGTH_CUTOFF_RATIO
            if abs(strength_score) < dynamic_cutoff:
                print(
                    f"    → Skip: strength gap {abs(strength_score):.4f} below {dynamic_cutoff:.4f}"
                )
                continue

            strength_pass_count += 1

            # ── OVERRIDE: skip all traditional filters ──
            if is_override:
                print(f"    ⚡ OVERRIDE MODE: bypassing MA/strength direction checks")
                direction = "BUY" if strength_score > 0 else "SELL"
            else:
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

                # Trend alignment
                direction = check_ma5_alignment(
                    pair, require_aligned=self.TREND_ALIGNMENT_REQUIRED
                )
                if direction is None:
                    print(
                        f"    → Skip: mixed alignment (need ≥{self.TREND_ALIGNMENT_REQUIRED} same)"
                    )
                    continue

                # Strength-direction alignment
                strength_direction = "BUY" if strength_score > 0 else "SELL"
                if strength_direction != direction:
                    print(
                        f"    → Skip: direction mismatch — MA={direction}, "
                        f"Strength={strength_direction} ({strength_score:+.4f})"
                    )
                    continue
                if abs(strength_score) < self.MIN_STRENGTH_SCORE:
                    print(
                        f"    → Skip: strength magnitude {abs(strength_score):.4f} < "
                        f"MIN {self.MIN_STRENGTH_SCORE}"
                    )
                    continue

                # ML confirmation (only for non-override)
                should_avoid_ml, ml_reason = ml_filter.should_avoid_pair(pair, direction)
                if should_avoid_ml:
                    print(f"    → Skip: ML filter - {ml_reason}")
                    continue

            # ── Common path for both OVERRIDE and NORMAL: price/SL/TP ──
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

            if ENABLE_ATR_SLTP:
                atr, z_score = get_atr_with_volatility_context(
                    pair, self.ATR_PERIOD, self.ATR_HISTORY_LOOKBACK
                )
                if atr is None or atr <= 0:
                    print("    → Skip: ATR unavailable")
                    continue

                if self.ENABLE_ATR_MIN_FILTER and not is_override:
                    _entry_ref = prices["ask"] if direction == "BUY" else prices["bid"]
                    _atr_rel_pct = (atr / _entry_ref) * 100
                    if atr < self.ATR_MIN_ABSOLUTE or _atr_rel_pct < self.ATR_MIN_RELATIVE_PCT:
                        print(
                            f"    → Skip low-vol: ATR={atr:.4f} < {self.ATR_MIN_ABSOLUTE:.4f} | "
                            f"REL={_atr_rel_pct:.3f}% < {self.ATR_MIN_RELATIVE_PCT:.3f}%"
                        )
                        continue

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

                if ENABLE_MACRO_PROTECTION and direction == "BUY" and entry \
                    > weekly_levels["resistance"] - self.MACRO_PROTECTION_PIPS * self.pip:
                    print("    → Skip: too close to weekly resistance")
                    continue
                if ENABLE_MACRO_PROTECTION and direction == "SELL" and entry \
                    < weekly_levels["support"] + self.MACRO_PROTECTION_PIPS * self.pip:
                    print("    → Skip: too close to weekly support")
                    continue

                if direction == "BUY" and (tp <= entry or sl >= entry):
                    print("    → Skip: invalid SL/TP")
                    continue
                if direction == "SELL" and (tp >= entry or sl <= entry):
                    print("    → Skip: invalid SL/TP")
                    continue
            else:
                entry = prices["ask"] if direction == "BUY" else prices["bid"]
                if direction == "BUY":
                    sl = round(daily_levels["support"] - (SL_BUFFER_PIPS + SPREAD_PIPS) * self.pip, 3)
                    broke_out = (
                        confirmed_breakout(pair, daily_levels["resistance"], "above")
                        if ENABLE_BREAKOUT_CONFIRMATION else entry > daily_levels["resistance"]
                    )
                    if broke_out and weekly_levels["resistance"] <= entry:
                        tp = round(entry + TP_PIPS * self.pip, 3)
                        target_type = "Fixed target (stale weekly level)"
                    else:
                        tp = round(
                            (weekly_levels["resistance"] if broke_out else daily_levels["resistance"])
                            - self.FRONT_RUN_PIPS * self.pip, 3
                        )
                        target_type = "Weekly Resistance" if broke_out else "Daily Resistance"
                    sl_reference = "Daily Support"
                else:
                    sl = round(daily_levels["resistance"] + (SL_BUFFER_PIPS + SPREAD_PIPS) * self.pip, 3)
                    broke_down = (
                        confirmed_breakout(pair, daily_levels["support"], "below")
                        if ENABLE_BREAKOUT_CONFIRMATION else entry < daily_levels["support"]
                    )
                    if broke_down and weekly_levels["support"] >= entry:
                        tp = round(entry - TP_PIPS * self.pip, 3)
                        target_type = "Fixed target (stale weekly level)"
                    else:
                        tp = round(
                            (weekly_levels["support"] if broke_down else daily_levels["support"])
                            + self.FRONT_RUN_PIPS * self.pip, 3
                        )
                        target_type = "Weekly Support" if broke_down else "Daily Support"
                    sl_reference = "Daily Resistance"

            risk = abs(entry - sl)
            reward = abs(tp - entry)
            rr = reward / risk if risk > 0 else 0.0
            if rr < self.MIN_RR:
                print(f"    → Skip: R:R {rr:.2f} below {self.MIN_RR}")
                continue

            mode_tag = "[OVERRIDE]" if is_override else "[NORMAL]"
            print(f"    ✅ VALID {mode_tag}: {direction} {pair} | R:R {rr:.2f}")
            all_valid_signals.append({
                "pair": pair,
                "action": direction,
                "bar_time": _signal_bar_time(),
                "entry": entry,
                "stop_loss": sl,
                "take_profit": tp,
                "strength_score": strength_score,
                "risk_reward": round(rr, 2),
                "reasoning": f"{mode_tag} Aligned {direction} | SL={sl_reference} | TP={target_type}",
                "override_source": "dominance_ratio" if is_override else None,
                "priority": "HIGH" if is_override else "NORMAL",
            })

        # --- FINAL SELECTION ---
        valid_count = len(all_valid_signals)
        has_override = any(s.get("override_source") for s in all_valid_signals)
        print(
            f"\n[SELECTION-{self.quote_ccy}] Total valid: {valid_count} | "
            f"strength-passing: {strength_pass_count} | override={'YES' if has_override else 'no'}"
        )

        if has_override:
            print("  ⚡ OVERRIDE present → bypassing MIN_STRENGTH_PASS / MIN_DOMINANT checks")
        else:
            if strength_pass_count < self.MIN_STRENGTH_PASSING_PAIRS:
                print(
                    f"  ❌ Only {strength_pass_count} pair(s) pass strength cutoff "
                    f"(need ≥ {self.MIN_STRENGTH_PASSING_PAIRS}) → NO TRADE"
                )
                return []

        if valid_count < self.MIN_VALID_PAIRS:
            print(
                f"  ❌ Only {valid_count} valid pair(s) — need ≥ {self.MIN_VALID_PAIRS} → NO TRADE"
            )
            return []

        buy_count = sum(1 for s in all_valid_signals if s["action"] == "BUY")
        sell_count = sum(1 for s in all_valid_signals if s["action"] == "SELL")
        max_side = max(buy_count, sell_count)

        if not has_override and max_side < self.MIN_DOMINANT_PAIRS:
            print(
                f"  ❌ Directional consensus too thin: BUYs={buy_count} SELLs={sell_count} "
                f"(need ≥ {self.MIN_DOMINANT_PAIRS} same direction) → NO TRADE"
            )
            return []

        top_pair = max(all_valid_signals, key=lambda x: abs(x["strength_score"]))
        print(
            f"  ✅ Selected (vs {self.quote_ccy}): {top_pair['action']} {top_pair['pair']} "
            f"({top_pair['strength_score']:+.4f})"
        )

        return all_valid_signals

    def rules_description(self) -> str:
        return f"""
RULES SUMMARY — quote_ccy={self.quote_ccy}:
  • Trade pairs: {self.trade_pairs}
  • Min valid pairs: {self.MIN_VALID_PAIRS}
  • Directional consensus: ≥{self.MIN_DOMINANT_PAIRS} same direction
  • Resonance: ≥{self.MIN_STRENGTH_PASSING_PAIRS} pass strength cutoff
  • Timeframes aligned: {self.TREND_ALIGNMENT_REQUIRED}
  • Dominance ratio: enabled={self.DOMINANCE_RATIO_ENABLED}, ratio≥{self.DOMINANCE_RATIO_THRESHOLD}
  • Override mode: enabled={self.DOMINANCE_OVERRIDE_ENABLED}, ratio≥{self.DOMINANCE_OVERRIDE_THRESHOLD}
  • Pip size: {self.pip}
"""


# ==========================================
# JPY TREND STRATEGY — Compatibility wrapper
# ==========================================
class JPYTrendStrategy(BaseCurrencyTrendStrategy):
    def __init__(self, trade_pairs=None, **kwargs):
        super().__init__(quote_ccy="JPY", trade_pairs=trade_pairs, **kwargs)


# ==========================================
# RUNNER & ENTRY POINT
# ==========================================
_active_strategy = BaseCurrencyTrendStrategy(quote_ccy="JPY")

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