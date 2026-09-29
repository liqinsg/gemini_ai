# custom_strategy_v1.py — Unified: V2 instrumentation + ML filter + ATR floor + Consensus guard
# import joblib
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from utils.range_detector import is_sideways
from utils import get_support_resistance

# from utils.signal_instrumentation import (
#     classify_volatility,
#     classify_price_location,
#     level_cluster_strength,
#     log_signal_observation,
#     log_executed_signal,
# )
import config as _config
import config_bot_v3 as _config_bot_v3
from config_bot_v3 import PIP_SIZE_BY_QUOTE
from config import (
    STRENGTH_PAIRS,
    SL_BUFFER_PIPS,
    SPREAD_PIPS,
    ENABLE_ATR_SLTP,
    ENABLE_BREAKOUT_CONFIRMATION,
    CHECK_INTERVAL_MINUTES,
    ENABLE_MACRO_PROTECTION,
    TP_PIPS,
    # ENABLE_VOLATILITY_NORMALIZED_DOMINANCE,
    # TRADE_PAIRS,
    # MIN_DOMINANCE_RATIO,
    # ENABLE_NEWS_FILTER,
    # ENABLE_EMA_TREND,
    # ENABLE_ATR_NORMALIZED_STRENGTH,
    # ENABLE_STRENGTH_ACCELERATION,
    # STRENGTH_ACCELERATION_WEIGHT,
    # BREAKOUT_CONFIRMATION_CLOSES,
    # GEMINI_API_KEY,
    # GEMINI_NEWS_MODEL,
    # GEMINI_NEWS_FALLBACK_MODEL,
    # NEWS_LOG_PATH,
    # NEWS_CURRENCIES,
    # REQUIRE_ALIGNED,
    # ALIGNMENT_THRESHOLD,
    # STRENGTH_GAP_THRESHOLD,
    # MIN_STRENGTH_SCORE,
    # STRENGTH_CUTOFF_RATIO,
    # MIN_VALID_PAIRS_TO_TRADE,
    # MIN_DOMINANT_PAIRS,
    # MIN_STRENGTH_PASSING_PAIRS,
    # DEBUG_SLTP,
    # ENABLE_RANGE_DETECTOR,
    # SKIP_SIDEWAYS_PAIRS,
    # TRADE_TOP_PAIRS,
    # SIGNAL_TIMEFRAMES,
    # ENABLE_ATR_MIN_FILTER,
    # ATR_MIN_RELATIVE_PCT,
)
from utils.strategy_helpers import (
    get_atr_with_volatility_context,
    build_strength_matrix,
    format_strength_ranking,
    check_ma5_cross,
    check_macd_histogram,
    get_live_prices,
    confirmed_breakout,
    NewsFilter,
    # get_candles,
    # _atr_from_candles,
    # get_dominance_normalizer,
    # get_pair_momentum,
    # get_ma5_position,
    # _ema,
    # get_ema_trend_position,
    # get_trend_position,
    # check_ma5_alignment,
    # get_slope_diagnostics,
    # get_previous_day_low,
    # get_previous_day_high,
)
from utils.ml_confirmation import ml_filter

# ==========================================
# Consensus / Dominance Guard state
# ==========================================
# _last_dominance_guard_triggered = False

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

USE_MACD = getattr(_config_bot_v3, "USE_MACD", True)


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
        # Group-level resonance overrides (for single-pair groups like CHF)
        min_strength_passing_pairs: int | None = None,
        min_dominant_pairs: int | None = None,
        min_valid_pairs_to_trade: int | None = None,
        # Group-level context (Part C regime-aware MA threshold):
        # `mc_regime` is set at group-dispatch time in scheduled_runner;
        # when non-None it lets strategy-internal checks relax or tighten
        # thresholds based on MC classification without leaking broker data.
        mc_regime: str | None = None,
    ):
        self.quote_ccy = quote_ccy.upper()
        self.pip = PIP_SIZE_BY_QUOTE.get(self.quote_ccy, 0.0001)
        self.mc_regime = mc_regime  # None/"CONSOLIDATION"/"NEUTRAL"/"STRONG_MOMENTUM"

        if trade_pairs is None:
            trade_pairs = [
                p for p in STRENGTH_PAIRS if p.endswith(f"_{self.quote_ccy}")
            ]
        self.trade_pairs = trade_pairs

        # --- Dominance ratio filter ---
        # Source order: explicit ctor arg → config_bot_v3 (v4 generic) → config.py → hardcoded default
        self.DOMINANCE_RATIO_ENABLED = (
            dominance_ratio_enabled
            if dominance_ratio_enabled is not None
            else getattr(
                _config_bot_v3,
                "DOMINANCE_RATIO_ENABLED",
                getattr(_config, "DOMINANCE_RATIO_ENABLED", True),
            )
        )
        self.DOMINANCE_RATIO_THRESHOLD = (
            dominance_ratio_threshold
            if dominance_ratio_threshold is not None
            else getattr(
                _config_bot_v3,
                "DOMINANCE_RATIO_THRESHOLD",
                getattr(_config, "DOMINANCE_RATIO_THRESHOLD", 2.0),
            )
        )
        self.GAP_SEPARATION_THRESHOLD = (
            gap_separation_threshold
            if gap_separation_threshold is not None
            else getattr(
                _config_bot_v3,
                "GAP_SEPARATION_THRESHOLD",
                getattr(_config, "GAP_SEPARATION_THRESHOLD", 1.3),
            )
        )
        self.DOMINANCE_OVERRIDE_ENABLED = (
            dominance_override_enabled
            if dominance_override_enabled is not None
            else getattr(
                _config_bot_v3,
                "DOMINANCE_OVERRIDE_ENABLED",
                getattr(_config, "DOMINANCE_OVERRIDE_ENABLED", True),
            )
        )
        self.DOMINANCE_OVERRIDE_THRESHOLD = (
            dominance_override_threshold
            if dominance_override_threshold is not None
            else getattr(
                _config_bot_v3,
                "DOMINANCE_OVERRIDE_THRESHOLD",
                getattr(_config, "DOMINANCE_OVERRIDE_THRESHOLD", 1.8),
            )
        )
        self.DOMINANCE_OVERRIDE_MEDIAN_FLOOR = getattr(
            _config_bot_v3, "DOMINANCE_OVERRIDE_MEDIAN_FLOOR", 0.15
        )

        # --- Core strategy thresholds (all from generic config) ---
        self.MIN_MARKET_STRENGTH = getattr(_config, "MIN_MARKET_STRENGTH", 0.03)
        self.FRONT_RUN_PIPS = getattr(_config, "FRONT_RUN_PIPS", 15)
        self.MACRO_PROTECTION_PIPS = getattr(_config, "MACRO_PROTECTION_PIPS", 10)
        self.MIN_RR = getattr(_config, "MIN_RR", 1.2)
        self.ATR_PERIOD = getattr(_config, "ATR_PERIOD", 14)
        self.ATR_HISTORY_LOOKBACK = getattr(_config, "ATR_HISTORY_LOOKBACK", 50)
        self.ATR_SL_MULTIPLIER_NORMAL = getattr(
            _config, "ATR_SL_MULTIPLIER_NORMAL", 2.2
        )
        self.ATR_SL_MULTIPLIER_HIGH_VOL = getattr(
            _config, "ATR_SL_MULTIPLIER_HIGH_VOL", 2.8
        )
        self.ATR_SL_MULTIPLIER_LOW_VOL = getattr(
            _config, "ATR_SL_MULTIPLIER_LOW_VOL", 1.8
        )
        self.ATR_RR_MULTIPLE = getattr(_config, "ATR_RR_MULTIPLE", 2.0)

        self.MIN_VALID_PAIRS = (
            min_valid_pairs_to_trade
            if min_valid_pairs_to_trade is not None
            else getattr(_config, "MIN_VALID_PAIRS_TO_TRADE", 1)
        )
        self.MIN_DOMINANT_PAIRS = (
            min_dominant_pairs
            if min_dominant_pairs is not None
            else getattr(_config, "MIN_DOMINANT_PAIRS", 1)
        )
        self.ALIGNMENT_REQUIRE_MAJORITY = getattr(
            _config_bot_v3, "ALIGNMENT_REQUIRE_MAJORITY", True
        )
        self.ALIGNMENT_THRESHOLD_MIN = getattr(
            _config_bot_v3, "ALIGNMENT_THRESHOLD_MIN", 2
        )
        self.TREND_ALIGNMENT_REQUIRED = (
            self.ALIGNMENT_THRESHOLD_MIN
            if self.ALIGNMENT_REQUIRE_MAJORITY
            else getattr(_config, "ALIGNMENT_THRESHOLD", 3)
        )
        self.TRADE_TOP_PAIRS = getattr(_config, "TRADE_TOP_PAIRS", 3)
        self.MIN_STRENGTH_PASSING_PAIRS = (
            min_strength_passing_pairs
            if min_strength_passing_pairs is not None
            else getattr(_config, "MIN_STRENGTH_PASSING_PAIRS", 2)
        )
        self.SKIP_SIDEWAYS_PAIRS = getattr(_config, "SKIP_SIDEWAYS_PAIRS", False)
        self.STRENGTH_GAP_THRESHOLD = getattr(_config, "STRENGTH_GAP_THRESHOLD", 1.5)
        self.MIN_STRENGTH_SCORE = getattr(_config, "MIN_STRENGTH_SCORE", 0.15)
        self.STRENGTH_CUTOFF_RATIO = getattr(_config, "STRENGTH_CUTOFF_RATIO", 0.4)

        # --- ATR min filter (injectable) ---
        self.ENABLE_ATR_MIN_FILTER = (
            enable_atr_min_filter
            if enable_atr_min_filter is not None
            else getattr(_config, "ENABLE_ATR_MIN_FILTER", True)
        )
        self.ATR_MIN_ABSOLUTE_PIPS = (
            atr_min_pips
            if atr_min_pips is not None
            else getattr(
                _config_bot_v3, "ATR_MIN_PIPS", getattr(_config, "ATR_MIN_PIPS", 6.0)
            )
        )
        self.ATR_MIN_ABSOLUTE = self.ATR_MIN_ABSOLUTE_PIPS * self.pip
        self.ATR_MIN_RELATIVE_PCT = (
            atr_min_relative_pct
            if atr_min_relative_pct is not None
            else getattr(_config, "ATR_MIN_RELATIVE_PCT", 0.045)
        )

        if not self.trade_pairs:
            print(
                f"[STRATEGY] WARNING: BaseCurrencyTrendStrategy(quote={self.quote_ccy}) has no trade pairs."
            )

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
            out: Dict[str, Dict[str, Any]] = {}
            for p, s in group_ranks.items():
                out[p] = {
                    "score": s,
                    "override": False,
                    "override_type": None,
                    "override_ratio": 0.0,
                }
            return out

        if n % 2 == 1:
            median = abs_scores[n // 2]
        else:
            median = (abs_scores[n // 2 - 1] + abs_scores[n // 2]) / 2

        top1 = abs_scores[-1]

        if median < 1e-6:
            best_pair = max(group_ranks, key=lambda p: abs(group_ranks[p]))
            print(
                f"  [DOMINANCE-{self.quote_ccy}] All gaps near zero → keep strongest: {best_pair}"
            )
            return {
                best_pair: {
                    "score": group_ranks[best_pair],
                    "override": False,
                    "override_type": None,
                    "override_ratio": 0.0,
                }
            }

        effective_median = max(median, self.DOMINANCE_OVERRIDE_MEDIAN_FLOOR)
        if median < self.DOMINANCE_OVERRIDE_MEDIAN_FLOOR:
            print(
                f"  [DOMINANCE-{self.quote_ccy}] median={median:.4f} < floor={self.DOMINANCE_OVERRIDE_MEDIAN_FLOOR} → "
                f"using floor for ratio calc (prevents noise-triggered OVERRIDE)"
            )

        # top_separation = top1 / effective_median — how much the leading pair
        # is pulling ahead of the (floor-clamped) median.  If ratio < 1.3 the
        # group is still clustered → no true DOMINANT leader and we enter the
        # TYPE-B CONSENSUS resonance branch below.
        top_separation = top1 / effective_median

        # Part B/C classification of OVERRIDE signals:
        #   TYPE-A DOMINANT  → top pair clearly dominates (top1/median ≥ GAP_SEPARATION_THRESHOLD=1.3)
        #                       → truly extreme signal.  Full privileges: MACD skip, etc.
        #   TYPE-B CONSENSUS → all pairs agree on direction, but no clear leader
        #                       (top1/median < 1.3) → weaker signal, must still pass MACD
        # We store the classification on each filtered entry so the downstream
        # generate_signals() branch can pick the right path without recalculating.
        result_inner: Dict[str, Dict[str, Any]] = {}

        # Resonance mode → check for group consensus
        if top_separation < self.GAP_SEPARATION_THRESHOLD:
            all_scores = list(group_ranks.values())
            all_same_sign = all(s > 0 for s in all_scores) or all(
                s < 0 for s in all_scores
            )

            if all_same_sign and self.DOMINANCE_OVERRIDE_ENABLED:
                sorted_pairs = sorted(
                    group_ranks.items(), key=lambda x: abs(x[1]), reverse=True
                )
                top_pair, top_score = sorted_pairs[0]
                print(
                    f"  [DOMINANCE-{self.quote_ccy}] RESONANCE + GROUP CONSENSUS: "
                    f"top1/median={top_separation:.2f}x, ALL {len(sorted_pairs)} pairs agree on sign → "
                    f"⚡ OVERRIDE[CONSENSUS] (TYPE-B, no true leader) — {top_pair} ({top_score:+.3f})"
                )
                result_inner[top_pair] = {
                    "score": top_score,
                    "override": True,
                    "override_type": "CONSENSUS",
                    "override_ratio": float(top_separation),
                }
                for pair, score in sorted_pairs[1:]:
                    result_inner[pair] = {
                        "score": score,
                        "override": False,
                        "override_type": None,
                        "override_ratio": 0.0,
                    }
                return result_inner

            print(
                f"  [DOMINANCE-{self.quote_ccy}] RESONANCE: top1/median={top_separation:.2f}x "
                f"< {self.GAP_SEPARATION_THRESHOLD}x → no filtering"
            )
            for p, s in group_ranks.items():
                result_inner[p] = {
                    "score": s,
                    "override": False,
                    "override_type": None,
                    "override_ratio": 0.0,
                }
            return result_inner

        threshold_ratio = self.DOMINANCE_RATIO_THRESHOLD
        override_ratio = (
            self.DOMINANCE_OVERRIDE_THRESHOLD
            if self.DOMINANCE_OVERRIDE_ENABLED
            else 999.0
        )

        for pair, score in group_ranks.items():
            ratio = abs(score) / effective_median
            if ratio >= override_ratio:
                print(
                    f"  ⚡ [OVERRIDE-{self.quote_ccy}] {pair}: ratio={ratio:.1f}x ≥ {override_ratio}x "
                    f"→ TYPE-A DOMINANT: skip MACD + full privileges"
                )
                result_inner[pair] = {
                    "score": score,
                    "override": True,
                    "override_type": "DOMINANT",
                    "override_ratio": float(ratio),
                }
            elif ratio >= threshold_ratio:
                print(
                    f"  ✓ [DOMINANCE-{self.quote_ccy}] {pair}: ratio={ratio:.1f}x ≥ {threshold_ratio}x "
                    f"→ proceed to checks"
                )
                result_inner[pair] = {
                    "score": score,
                    "override": False,
                    "override_type": None,
                    "override_ratio": float(ratio),
                }
            else:
                print(
                    f"  ✗ [DOMINANCE-{self.quote_ccy}] {pair}: ratio={ratio:.1f}x < {threshold_ratio}x "
                    f"→ filtered"
                )

        if not result_inner:
            print(
                f"  ⚠️ [DOMINANCE-{self.quote_ccy}] Nothing passed → fallback to all for resonance check"
            )
            fallback: Dict[str, Dict[str, Any]] = {}
            for p, s in group_ranks.items():
                fallback[p] = {
                    "score": s,
                    "override": False,
                    "override_type": None,
                    "override_ratio": 0.0,
                }
            return fallback

        return result_inner

    # -------------------------------------------------------
    # Core signal generation (override-aware)
    # -------------------------------------------------------
    def generate_signals(self, scores: dict) -> list[dict]:
        group_ranks = self.group_strength_rank(scores)

        if self.DOMINANCE_RATIO_ENABLED:
            filtered = self._apply_dominance_ratio_filter(group_ranks)
        else:
            filtered = {
                p: {"score": s, "override": False} for p, s in group_ranks.items()
            }

        _dominance_filtered_count = len(group_ranks) - len(filtered)

        # --- Part C — CHF-STRONG_MOMENTUM threshold relaxation ----------
        # Compute raw global gap (max-min) from the full cross-group
        # strength matrix passed by the caller.  Only relax MA consensus
        # when all 3 conditions hold: this QUOTE is CHF, MC regime =
        # STRONG_MOMENTUM, and raw global strength spread ≥ 1.8.
        _chf_relaxed = False
        if scores:
            try:
                _global_max = max(scores.values())
                _global_min = min(scores.values())
                _global_gap_raw = abs(_global_max - _global_min)
            except Exception:
                _global_gap_raw = 0.0
        else:
            _global_gap_raw = 0.0
        if (
            self.quote_ccy == "CHF"
            and self.mc_regime == "STRONG_MOMENTUM"
            and _global_gap_raw >= 1.8
        ):
            _chf_relaxed = True

        max_gap = max(abs(v["score"]) for v in filtered.values()) if filtered else 0.0
        if max_gap < self.MIN_MARKET_STRENGTH:
            print(
                f"  [STRATEGY-{self.quote_ccy}] Global gap ({max_gap:.4f}) below floor, using floor."
            )
            max_gap = self.MIN_MARKET_STRENGTH

        ranked_pairs = sorted(
            filtered.items(), key=lambda x: abs(x[1]["score"]), reverse=True
        )
        _ranked_str = " > ".join(f"{p}({v['score']:+.3f})" for p, v in ranked_pairs)
        print(f"\n  [{self.quote_ccy} cross strength] {_ranked_str}")
        if _chf_relaxed:
            print(
                "  📊 CHF STRONG_MOMENTUM — MA require_aligned lowered for this "
                f"group: 1.8→1.4 | global_gap={_global_gap_raw:.4f}"
            )
        print(
            f"\n[STRATEGY-{self.quote_ccy}] Checking pairs "
            f"(need ≥{self.TREND_ALIGNMENT_REQUIRED} aligned timeframes)..."
        )

        all_valid_signals = []
        strength_pass_count = 0
        _skip_reasons: dict[str, int] = {
            "strength_below_cutoff": 0,
            "news_risk": 0,
            "sideways_market": 0,
            "mixed_alignment": 0,
            "direction_mismatch": 0,
            "strength_below_min": 0,
            "ml_conflict": 0,
            "no_price_data": 0,
            "missing_sr": 0,
            "atr_unavailable": 0,
            "dominance_filtered": 0,
            "strength_pass_low": 0,
            "valid_pairs_low": 0,
            "direction_consensus_low": 0,
            "macd_conflict": 0,
        }

        for pair, info in ranked_pairs:
            strength_score = info["score"]
            is_override = bool(info.get("override"))
            override_type: str | None = info.get("override_type") if is_override else None
            override_ratio: float = float(info.get("override_ratio") or 0.0)

            print(
                f"\n  [{pair}] (strength vs {self.quote_ccy}: {strength_score:+.4f})"
                + (
                    f" | OVERRIDE[{override_type}] ratio={override_ratio:.2f}"
                    if is_override and override_type
                    else ""
                )
            )

            dynamic_cutoff = max_gap * self.STRENGTH_CUTOFF_RATIO
            if abs(strength_score) < dynamic_cutoff:
                print(
                    f"    → Skip: strength gap {abs(strength_score):.4f} below {dynamic_cutoff:.4f}"
                )
                _skip_reasons["strength_below_cutoff"] += 1
                continue

            strength_pass_count += 1

            # Part C — per-pair MA consensus threshold for CHF-STRONG:
            _local_req = 1.4 if _chf_relaxed else 1.8

            # ── OVERRIDE: split TYPE-A DOMINANT (skip MACD) vs TYPE-B CONSENSUS (keep MACD)
            if is_override:
                _is_a = override_type == "DOMINANT"
                print(
                    "    ⚡ OVERRIDE MODE "
                    f"[{override_type or 'UNCLASSIFIED'}]: MA Cross 3-TF filter "
                    f"(≥{_local_req:.1f} weighted votes, H4×2.0, lookback=4)"
                )
                if _chf_relaxed:
                    print(
                        f"    📊 CHF STRONG_MOMENTUM — require_aligned "
                        f"lowered 1.8→{_local_req:.1f}"
                    )
                direction = check_ma5_cross(
                    pair,
                    require_aligned=_local_req,
                    timeframes=["H4", "H1", "M30"],
                    cross_lookback=4,
                    cross_weight=1.0,
                    slope_weight=0.7,
                    tf_cross_weights={"H4": 2.0, "H1": 0.7, "M30": 1.0},
                )
                if direction is None:
                    print(
                        f"    → Skip OVERRIDE: MA Cross no consensus "
                        f"(need ≥{_local_req:.1f} weighted votes)"
                    )
                    _skip_reasons["mixed_alignment"] += 1
                    continue
                strength_direction = "BUY" if strength_score > 0 else "SELL"
                if strength_direction != direction:
                    print(
                        f"    → Skip OVERRIDE: direction mismatch — MA={direction}, "
                        f"Strength={strength_direction} ({strength_score:+.4f})"
                    )
                    _skip_reasons["direction_mismatch"] += 1
                    continue

                if _is_a:
                    # TYPE-A DOMINANT: MACD skips (extreme signal + clear leader)
                    print(f"    ⚡ OVERRIDE[DOMINANT]: skip MACD check (true leader)")
                else:
                    # TYPE-B CONSENSUS: *keep* MACD — no privileges beyond position-cap bypass.
                    print(
                        f"    ⚡ OVERRIDE[CONSENSUS] all aligned but dominance="
                        f"{override_ratio:.2f}<1.3 → keep MACD, rank by MA score"
                    )
                    if USE_MACD:
                        macd = check_macd_histogram(pair, timeframes=["H4"], verbose=False)
                        if macd and macd["per_tf"]:
                            h4_label = macd["per_tf"][0]["label"]
                            h4_delta = macd["per_tf"][0]["delta"]
                            h4_opposes = (
                                (strength_direction == "BUY" and h4_label == "BEARISH" and abs(h4_delta) >= MACD_MIN_DELTA_PCT) or
                                (strength_direction == "SELL" and h4_label == "BULLISH" and abs(h4_delta) >= MACD_MIN_DELTA_PCT)
                            )
                            if h4_opposes:
                                print(
                                    f"    → Skip OVERRIDE[CONSENSUS]: H4 MACD opposes — "
                                    f"{h4_label} (Δ={h4_delta:.6f}%)"
                                )
                                _skip_reasons["macd_conflict"] += 1
                                continue
                            print(
                                f"    → H4 MACD OK: {h4_label} (Δ={h4_delta:.6f}%)"
                            )
            else:
                # News filter
                should_avoid, news_reason = _news_filter.should_avoid_pair(pair)
                if should_avoid:
                    print(f"    → Skip: news risk - {news_reason}")
                    _skip_reasons["news_risk"] += 1
                    continue

                # Sideways filter
                if self.SKIP_SIDEWAYS_PAIRS:
                    sideways, reason, metrics = is_sideways(pair)
                    if sideways:
                        print(
                            f"    → Skip: sideways market — {reason} | Range: {metrics.get('range_pct', 'N/A')}%"
                        )
                        _skip_reasons["sideways_market"] += 1
                        continue

                # Trend alignment (MA Cross strict entry)
                direction = check_ma5_cross(
                    pair,
                    require_aligned=_local_req,
                    cross_lookback=4,
                    cross_weight=1.0,
                    slope_weight=0.7,
                    tf_cross_weights={"H4": 2.0, "H1": 0.7, "M30": 1.0},
                )
                if _chf_relaxed:
                    print(
                        f"    📊 CHF STRONG_MOMENTUM — require_aligned "
                        f"lowered 1.8→{_local_req:.1f}"
                    )
                if direction is None:
                    print(f"    → Skip: MA Cross no consensus")
                    _skip_reasons["mixed_alignment"] += 1
                    continue

                # Strength-direction alignment
                strength_direction = "BUY" if strength_score > 0 else "SELL"
                if strength_direction != direction:
                    print(
                        f"    → Skip: direction mismatch — MA={direction}, "
                        f"Strength={strength_direction} ({strength_score:+.4f})"
                    )
                    _skip_reasons["direction_mismatch"] += 1
                    continue

                # MACD histogram confirmation — 只看 H4 是否明确反对 MA 方向
                # H4 是趋势主导者；H1/M30 flicker 不应该 veto 高周期 MA Cross
                # 加 min delta 过滤噪声: MACD hist 微小波动(如 Δ=0.00001)不应该 veto
                if USE_MACD:
                    macd = check_macd_histogram(pair, timeframes=["H4"], verbose=True)
                    if macd and macd["per_tf"]:
                        h4_label = macd["per_tf"][0]["label"]
                        h4_delta = macd["per_tf"][0]["delta"]
                        h4_opposes = (
                            direction == "BUY" and h4_label == "EXPAND_DOWN"
                        ) or (direction == "SELL" and h4_label == "EXPAND_UP")
                        # 最小 delta 阈值: 0.0005 过滤几乎为零的噪声
                        if h4_opposes and abs(h4_delta) >= 0.0005:
                            print(
                                f"    → Skip: MACD H4 conflict — MA={direction}, "
                                f"H4_MACD_HIST={h4_label} (Δ={h4_delta:.5f})"
                            )
                            _skip_reasons["macd_conflict"] += 1
                            continue
                        elif h4_opposes and abs(h4_delta) < 0.0005:
                            print(
                                f"    → MACD H4 noise Δ={h4_delta:.5f} < 0.0005 → ignore"
                            )
                if abs(strength_score) < self.MIN_STRENGTH_SCORE:
                    print(
                        f"    → Skip: strength magnitude {abs(strength_score):.4f} < "
                        f"MIN {self.MIN_STRENGTH_SCORE}"
                    )
                    _skip_reasons["strength_below_min"] += 1
                    continue

                # ML confirmation (only for non-override)
                should_avoid_ml, ml_reason = ml_filter.should_avoid_pair(
                    pair, direction
                )
                if should_avoid_ml:
                    print(f"    → Skip: ML filter - {ml_reason}")
                    _skip_reasons["ml_conflict"] += 1
                    continue

            # ── Common path for both OVERRIDE and NORMAL: price/SL/TP ──
            prices = get_live_prices(pair)
            if prices is None:
                print("    → Skip: no live price data")
                _skip_reasons["no_price_data"] += 1
                continue

            daily_levels = get_support_resistance(
                pair, granularity="D", count=60, window=3, return_all_levels=True
            )
            weekly_levels = get_support_resistance(
                pair, granularity="W", count=52, window=2
            )
            if None in (
                daily_levels["support"],
                daily_levels["resistance"],
                weekly_levels["support"],
                weekly_levels["resistance"],
            ):
                print("    → Skip: missing S/R levels")
                _skip_reasons["missing_sr"] += 1
                continue

            if ENABLE_ATR_SLTP:
                atr, z_score = get_atr_with_volatility_context(
                    pair, self.ATR_PERIOD, self.ATR_HISTORY_LOOKBACK
                )
                if atr is None or atr <= 0:
                    print("    → Skip: ATR unavailable")
                    _skip_reasons["atr_unavailable"] += 1
                    continue

                if self.ENABLE_ATR_MIN_FILTER and not is_override:
                    _entry_ref = prices["ask"] if direction == "BUY" else prices["bid"]
                    _atr_rel_pct = (atr / _entry_ref) * 100
                    if (
                        atr < self.ATR_MIN_ABSOLUTE
                        or _atr_rel_pct < self.ATR_MIN_RELATIVE_PCT
                    ):
                        print(
                            f"    → Skip low-vol: ATR={atr:.4f} < {self.ATR_MIN_ABSOLUTE:.4f} | "
                            f"REL={_atr_rel_pct:.3f}% < {self.ATR_MIN_RELATIVE_PCT:.3f}%"
                        )
                        continue

                sl_multiplier = (
                    self.ATR_SL_MULTIPLIER_HIGH_VOL
                    if (z_score or 0) > 1
                    else (
                        self.ATR_SL_MULTIPLIER_LOW_VOL
                        if (z_score or 0) < -1
                        else self.ATR_SL_MULTIPLIER_NORMAL
                    )
                )
                # Part 5.4 — Anti-whipsaw SL guard for STRONG_MOMENTUM.
                # LOW_VOL (z_score < -1) would normally *tighten* SL to 1.8× ATR
                # (~0.82× of the 2.2 baseline).  During STRONG_MOMENTUM this
                # gets hit by whipsaws and the position gets self-swept out of
                # a strong trend early.  Force floor = NORMAL multiplier so
                # STRONG_MOMENTUM positions never get a tighter SL than the
                # default baseline; print an explicit banner for audit.
                if is_override:
                    _ovr_t = str(info.get("override_type") or "").upper()
                else:
                    _ovr_t = ""
                if self.mc_regime == "STRONG_MOMENTUM":
                    _before = sl_multiplier
                    sl_multiplier = max(
                        sl_multiplier, self.ATR_SL_MULTIPLIER_NORMAL
                    )
                    if abs(sl_multiplier - _before) > 1e-6:
                        print(
                            f"    [SL ADJUST] STRONG_MOMENTUM → no tighten "
                            f"(anti-whipsaw): mult {_before:.2f}→{sl_multiplier:.2f}"
                        )
                    else:
                        print(
                            f"    [SL ADJUST] STRONG_MOMENTUM → baseline already OK: "
                            f"mult={sl_multiplier:.2f}"
                        )
                # Note: TYPE-B CONSENSUS OVERRIDE *also* goes through the
                # z-score path above; TYPE-A DOMINANT gets same guard.  No
                # special privilege beyond STRONG_MOMENTUM gate already set.
                sl_distance = atr * sl_multiplier
                tp_distance = sl_distance * self.ATR_RR_MULTIPLE

                entry, sl, tp = (
                    (
                        prices["ask"],
                        round(prices["ask"] - sl_distance, 3),
                        round(prices["ask"] + tp_distance, 3),
                    )
                    if direction == "BUY"
                    else (
                        prices["bid"],
                        round(prices["bid"] + sl_distance, 3),
                        round(prices["bid"] - tp_distance, 3),
                    )
                )
                sl_reference = f"ATR x{sl_multiplier}"
                target_type = f"ATR x{sl_multiplier * self.ATR_RR_MULTIPLE:.2f}"

                if (
                    ENABLE_MACRO_PROTECTION
                    and direction == "BUY"
                    and entry
                    > weekly_levels["resistance"]
                    - self.MACRO_PROTECTION_PIPS * self.pip
                ):
                    print("    → Skip: too close to weekly resistance")
                    continue
                if (
                    ENABLE_MACRO_PROTECTION
                    and direction == "SELL"
                    and entry
                    < weekly_levels["support"] + self.MACRO_PROTECTION_PIPS * self.pip
                ):
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
                    sl = round(
                        daily_levels["support"]
                        - (SL_BUFFER_PIPS + SPREAD_PIPS) * self.pip,
                        3,
                    )
                    broke_out = (
                        confirmed_breakout(pair, daily_levels["resistance"], "above")
                        if ENABLE_BREAKOUT_CONFIRMATION
                        else entry > daily_levels["resistance"]
                    )
                    if broke_out and weekly_levels["resistance"] <= entry:
                        tp = round(entry + TP_PIPS * self.pip, 3)
                        target_type = "Fixed target (stale weekly level)"
                    else:
                        tp = round(
                            (
                                weekly_levels["resistance"]
                                if broke_out
                                else daily_levels["resistance"]
                            )
                            - self.FRONT_RUN_PIPS * self.pip,
                            3,
                        )
                        target_type = (
                            "Weekly Resistance" if broke_out else "Daily Resistance"
                        )
                    sl_reference = "Daily Support"
                else:
                    sl = round(
                        daily_levels["resistance"]
                        + (SL_BUFFER_PIPS + SPREAD_PIPS) * self.pip,
                        3,
                    )
                    broke_down = (
                        confirmed_breakout(pair, daily_levels["support"], "below")
                        if ENABLE_BREAKOUT_CONFIRMATION
                        else entry < daily_levels["support"]
                    )
                    if broke_down and weekly_levels["support"] >= entry:
                        tp = round(entry - TP_PIPS * self.pip, 3)
                        target_type = "Fixed target (stale weekly level)"
                    else:
                        tp = round(
                            (
                                weekly_levels["support"]
                                if broke_down
                                else daily_levels["support"]
                            )
                            + self.FRONT_RUN_PIPS * self.pip,
                            3,
                        )
                        target_type = (
                            "Weekly Support" if broke_down else "Daily Support"
                        )
                    sl_reference = "Daily Resistance"

            risk = abs(entry - sl)
            reward = abs(tp - entry)
            rr = reward / risk if risk > 0 else 0.0
            if rr < self.MIN_RR:
                print(f"    → Skip: R:R {rr:.2f} below {self.MIN_RR}")
                continue

            mode_tag = "[OVERRIDE]" if is_override else "[NORMAL]"
            print(f"    ✅ VALID {mode_tag}: {direction} {pair} | R:R {rr:.2f}")
            all_valid_signals.append(
                {
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
                    "override_type": (override_type if is_override else None),
                    "override_ratio": (override_ratio if is_override else 0.0),
                    "priority": "HIGH" if is_override else "NORMAL",
                }
            )

        # --- Part C (1) JPY QUOTE-ONLY Extremes-Only Gate ------------
        # JPY 独立策略：处于中间排名的 pair 不开仓；只保留「最强 (TOP)」
        # 和「最弱 (BOTTOM)」两个 pair。TOP 方向 = BUY (该货币 vs JPY 最强)；
        # BOTTOM 方向 = SELL (该货币 vs JPY 最弱)。注意：如果某一端的
        # 所有 pairs 排名都处于中间 (未达 strength_pass_count 阈值)，
        # 就跳过那一端。override 信号不受此 gate 限制 (保留最高权限)。
        if self.quote_ccy == "JPY":
            _jpy_normal = [s for s in all_valid_signals if not s.get("override_source")]
            _jpy_override = [s for s in all_valid_signals if s.get("override_source")]
            if _jpy_normal:
                _sorted_normal = sorted(
                    _jpy_normal,
                    key=lambda s: s["strength_score"],  # 分数高→该货币相对 JPY 越强
                )
                _weakest = _sorted_normal[0]   # 最小 (最负或最接近 0 负侧) → BOTTOM → SELL
                _strongest = _sorted_normal[-1]  # 最大 → TOP → BUY
                _kept = {_weakest["pair"], _strongest["pair"]}
                _filtered_out_jpy = [
                    s for s in _jpy_normal if s["pair"] not in _kept
                ]
                if _filtered_out_jpy:
                    print(
                        f"\n  [JPY EXTREMES-ONLY] middle-rank pairs filtered: "
                        f"{len(_filtered_out_jpy)} dropped (kept TOP={_strongest['pair']} "
                        f"BUY-score={_strongest['strength_score']:+.3f} / "
                        f"BOTTOM={_weakest['pair']} SELL-score={_weakest['strength_score']:+.3f})"
                    )
                    for s in _filtered_out_jpy:
                        print(
                            f"      ✗ JPY-MIDDLE dropped: {s['action']} {s['pair']} "
                            f"(score={s['strength_score']:+.3f})"
                        )
                else:
                    print(
                        f"\n  [JPY EXTREMES-ONLY] exactly 1-2 valid normal pairs → "
                        f"all kept (no middle-rank drop)"
                    )
                all_valid_signals = [
                    s for s in _jpy_normal if s["pair"] in _kept
                ] + _jpy_override
            else:
                print(
                    f"\n  [JPY EXTREMES-ONLY] no normal-signal pairs present → "
                    f"skip extremes gate (override count={len(_jpy_override)})"
                )
            # JPY 只有 2 个候选 (TOP/BOTTOM)，把 MIN_STRENGTH_PASSING_PAIRS
            # 的组内阈值降为 1 以匹配「只允许 1 端有信号」场景，否则两端都
            # 无法开仓。override 原本就 bypass 此检查，不影响。
            self.MIN_STRENGTH_PASSING_PAIRS = min(
                self.MIN_STRENGTH_PASSING_PAIRS, 1
            )

            # --- Part C (2) JPY EXTREME → OVERRIDE CHANNEL upgrade ------
            # JPY 独立策略的 TOP/BOTTOM 如果 abs(score) ≥ OVERRIDE threshold
            # (=1.8)，说明 JPY 端出现了极强单边走势 —— 这种情况下即使
            # 全局 MAX_POSITIONS 普通 2 名额已满，也要升级到 OVERRIDE 通道
            # (占用 1 个独立 OVERRIDE slot)，避免 JPY 大行情因为已有其他
            # 老仓位占着普通名额永远开不出来。
            _JPY_EXTREME_UPGRADE_THRESHOLD = (
                self.DOMINANCE_OVERRIDE_THRESHOLD if hasattr(
                    self, "DOMINANCE_OVERRIDE_THRESHOLD"
                ) else 1.8
            )
            _upgraded: list[dict] = []
            for s in all_valid_signals:
                if s.get("override_source"):
                    _upgraded.append(s)  # 原生 override 不动
                    continue
                _sc = float(s.get("strength_score") or 0.0)
                if abs(_sc) >= _JPY_EXTREME_UPGRADE_THRESHOLD:
                    # 判断 TOP (最强 BUY) / BOTTOM (最弱 SELL) 分类
                    if s["action"] == "BUY":
                        _cls = "JPY_EXTREME_TOP"
                    else:
                        _cls = "JPY_EXTREME_BOTTOM"
                    print(
                        f"\n  [JPY→OVERRIDE UPGRADE] {s['action']} {s['pair']} "
                        f"score={_sc:+.3f} ≥ {_JPY_EXTREME_UPGRADE_THRESHOLD} → "
                        f"promote to OVERRIDE channel (type={_cls}); "
                        f"bypasses general MAX_POSITIONS cap"
                    )
                    s2 = dict(s)
                    s2["override_source"] = "extreme_upgrade"
                    s2["override_type"] = _cls
                    s2["override_ratio"] = float("nan")  # 非 ratio 类型升级
                    s2["priority"] = "HIGH"
                    _upgraded.append(s2)
                else:
                    _upgraded.append(s)
            all_valid_signals = _upgraded

        # --- FINAL SELECTION ---
        valid_count = len(all_valid_signals)
        has_override = any(s.get("override_source") for s in all_valid_signals)
        override_kinds: dict[str, int] = {}
        override_kinds_total = 0
        for s in all_valid_signals:
            if not s.get("override_source"):
                continue
            k = s.get("override_type") or "UNCLASSIFIED"
            override_kinds[k] = override_kinds.get(k, 0) + 1
            override_kinds_total += 1
        if override_kinds_total:
            _parts = "/".join(f"{k}={v}" for k, v in sorted(override_kinds.items()))
            print(
                f"\n[STAT] OVERRIDE this cycle: {_parts} | "
                f"fired_cycle={override_kinds_total} (process rolling tally follows below)"
            )
        else:
            print(
                f"\n[STAT] OVERRIDE this cycle: NONE | fired_cycle=0"
            )

        print(
            f"\n[SELECTION-{self.quote_ccy}] Total valid: {valid_count} | "
            f"strength-passing: {strength_pass_count} | override={'YES' if has_override else 'no'}"
        )

        if has_override:
            print(
                "  ⚡ OVERRIDE present → bypassing MIN_STRENGTH_PASS / MIN_DOMINANT checks"
            )
        else:
            if strength_pass_count < self.MIN_STRENGTH_PASSING_PAIRS:
                _skip_reasons["strength_pass_low"] = strength_pass_count
                print(
                    f"  ❌ Only {strength_pass_count} pair(s) pass strength cutoff "
                    f"(need ≥ {self.MIN_STRENGTH_PASSING_PAIRS}) → NO TRADE"
                )
                self._print_no_signal_summary(_skip_reasons, _dominance_filtered_count)
                return []

        if valid_count < self.MIN_VALID_PAIRS:
            _skip_reasons["valid_pairs_low"] = valid_count
            print(
                f"  ❌ Only {valid_count} valid pair(s) — need ≥ {self.MIN_VALID_PAIRS} → NO TRADE"
            )
            self._print_no_signal_summary(_skip_reasons, _dominance_filtered_count)
            return []

        buy_count = sum(1 for s in all_valid_signals if s["action"] == "BUY")
        sell_count = sum(1 for s in all_valid_signals if s["action"] == "SELL")
        max_side = max(buy_count, sell_count)

        if not has_override and max_side < self.MIN_DOMINANT_PAIRS:
            _skip_reasons["direction_consensus_low"] = max_side
            print(
                f"  ❌ Directional consensus too thin: BUYs={buy_count} SELLs={sell_count} "
                f"(need ≥ {self.MIN_DOMINANT_PAIRS} same direction) → NO TRADE"
            )
            self._print_no_signal_summary(_skip_reasons, _dominance_filtered_count)
            return []

        top_pair = max(all_valid_signals, key=lambda x: abs(x["strength_score"]))
        print(
            f"  ✅ Selected (vs {self.quote_ccy}): {top_pair['action']} {top_pair['pair']} "
            f"({top_pair['strength_score']:+.4f})"
        )

        return all_valid_signals

    @staticmethod
    def _print_no_signal_summary(skip_reasons: dict, dominance_filtered: int) -> None:
        print(f"\n  ═══ NO SIGNAL SUMMARY ═══")
        _items = []
        if dominance_filtered:
            _items.append(f"DOMINANCE filtered {dominance_filtered} pairs")
        for reason_key, count in skip_reasons.items():
            if not count or count <= 0:
                continue
            _label_map = {
                "strength_below_cutoff": "strength_below_cutoff",
                "news_risk": "news_risk",
                "sideways_market": "sideways_market",
                "mixed_alignment": "mixed_alignment",
                "direction_mismatch": "direction_mismatch",
                "strength_below_min": "strength_below_min",
                "ml_conflict": "ml_conflict",
                "no_price_data": "no_price_data",
                "missing_sr": "missing_sr",
                "atr_unavailable": "atr_unavailable",
                "strength_pass_low": f"strength_pass_low({count}<MIN)",
                "valid_pairs_low": f"valid_pairs_low({count}<MIN)",
                "direction_consensus_low": f"direction_consensus_low({count}<MIN)",
            }
            _items.append(_label_map.get(reason_key, reason_key))
        if _items:
            for _item in _items:
                print(f"     ↳ {_item}")
        print(f"  ════════════════════════\n")

    def rules_description(self) -> str:
        _align_mode = (
            "MAJORITY (2/3)"
            if self.ALIGNMENT_REQUIRE_MAJORITY
            else f"STRICT ({self.TREND_ALIGNMENT_REQUIRED}/{self.TREND_ALIGNMENT_REQUIRED})"
        )
        return f"""
RULES SUMMARY — quote_ccy={self.quote_ccy}:
  • Trade pairs: {self.trade_pairs}
  • Min valid pairs: {self.MIN_VALID_PAIRS}
  • Directional consensus: ≥{self.MIN_DOMINANT_PAIRS} same direction
  • Resonance: ≥{self.MIN_STRENGTH_PASSING_PAIRS} pass strength cutoff
  • Timeframes aligned: {_align_mode}
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


def run_strategy(
    strategy: Strategy, scores: dict | None = None
) -> tuple[str, list[dict], float]:
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
