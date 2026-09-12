# config.py
"""
Central configuration — edit this file to control all strategy behaviour.
Do not hardcode these values elsewhere in the codebase.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env", override=False)
load_dotenv(PROJECT_ROOT / "run.env", override=True)


def _environment_value(base_name: str, default: str = "") -> str:
    """Return the active environment's value, falling back to the generic key."""
    suffix = "LIVE" if OANDA_ENV.lower() in {"live", "real"} else "DEMO"
    return os.getenv(f"{base_name}_{suffix}", os.getenv(base_name, default))


# ==========================================
# OANDA connection
# ==========================================
OANDA_ENV = os.getenv("OANDA_ENV", "practice")
OANDA_API_TOKEN = _environment_value("OANDA_API_TOKEN")
OANDA_ACCOUNT_ID = _environment_value("OANDA_ACCOUNT_ID")
OANDA_ACCOUNT_ID_1 = _environment_value("OANDA_ACCOUNT_ID_1")
OANDA_ACCOUNT_ID_2 = _environment_value("OANDA_ACCOUNT_ID_2")
OANDA_ACCOUNT_ID_3 = _environment_value("OANDA_ACCOUNT_ID_3")
OANDA_ACCOUNT_ID_4 = _environment_value("OANDA_ACCOUNT_ID_4")
# ==========================================
# Scheduler
# ==========================================
CHECK_INTERVAL_MINUTES = 15  # 15 min matches the fastest signal timeframe (M15)
# ==========================================
# ACTIVE STRATEGY SETTINGS (simple runner)
# Simple MA5 multi-timeframe trend strategy on JPY pairs.
# ==========================================
# Pairs to actually trade (direct orders placed here)
TRADE_PAIRS = [
    "USD_JPY",
    "EUR_JPY",
    "GBP_JPY",
    "AUD_JPY",
]
# Timeframes that must ALL be above MA5 for an entry signal
SIGNAL_TIMEFRAMES = [
    "H4",
    "H1",
    "M30",
]  # ["H4", "H1", "M30", "M15"] for testing now ignore M15
REQUIRE_ALIGNED = len(SIGNAL_TIMEFRAMES)
# TP / SL in pips (JPY pairs: 1 pip = 0.01)
TP_PIPS = 100  # fixed take profit: entry + 100 pips
SL_BUFFER_PIPS = 20  # pips below today's daily low
SPREAD_PIPS = 3  # conservative spread buffer added to SL
# NOTE: if your code imports SL_PIPS (as your earlier traceback showed),
# add this alias (it does not rename anything else).
SL_PIPS = SL_BUFFER_PIPS
MIN_RR = 1.2
# ==========================================
# RISK LEVEL (1 = safest, 10 = most aggressive)
# Controls position size per trade.
# ==========================================
RISK_LEVEL = 10
RISK_PROFILE = {
    1: {"units": 1000, "min_confidence": 0.90},
    2: {"units": 2000, "min_confidence": 0.85},
    3: {"units": 3000, "min_confidence": 0.80},
    4: {"units": 4000, "min_confidence": 0.75},
    5: {"units": 5000, "min_confidence": 0.70},
    6: {"units": 6000, "min_confidence": 0.65},
    7: {"units": 7000, "min_confidence": 0.60},
    8: {"units": 8000, "min_confidence": 0.55},
    9: {"units": 9000, "min_confidence": 0.50},
    10: {"units": 10000, "min_confidence": 0.40},
}
# ==========================================
# AI / GEMINI (disabled for simple strategy)
# Set True to re-enable LLM validation when needed.
# ==========================================
USE_GEMINI_AI = True
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_NEWS_MODEL = "gemini-3.5-flash"
GEMINI_NEWS_FALLBACK_MODEL = "gemini-flash-lite-latest"
# ==========================================
# ADVANCED STRATEGY SETTINGS (full scheduled_runner.py)
# Safe to ignore when running scheduled_runner_simple.py.
# ==========================================
# Meta selector
META_MODE = "MANUAL"  # e.g. "MANUAL"
MANUAL_STRATEGY = (
    1  # 1=TREND_COMBINED, 2=RANGE_REVERSION, 3=BREAKOUT_CONFIRM, 4=TREND_PULLBACK
)
STRATEGY_PULLBACK = 4
# ADX regime thresholds
ADX_TREND_THRESHOLD = 25
ADX_BREAKOUT_THRESHOLD = 20
# ATR-based SL/TP (used by full runner, not simple runner)
ATR_GRANULARITY = "D"
ATR_CANDLE_COUNT = 20
ATR_MULTIPLIER_SL = 0.75
ATR_MULTIPLIER_TP = 2.0
# Range strategy (full runner)
RANGE_LOOKBACK = 20
RANGE_TP_RATIO = 0.6
RANGE_SL_RATIO = 0.25
# Breaking / coiling entry
BREAKOUT_DURATION_HOURS = 4
BREAKOUT_WIDTH_PCT = 2.0
# Test mode — forces a specific pair regardless of signal
FORCE_TEST_PAIR = False
TEST_PAIR = None  # e.g. "USD_JPY"
# Order expiry for pending limit/stop orders
EXPIRE_AFTER = 1440  # minutes (1 day)
# ==========================================
# JPY TREND STRATEGY (custom_strategy.py) — single source of truth
# ==========================================
# Minimum number of candidate JPY-cross pairs that must independently pass
# every filter in a single cycle before ANY trade is taken.
MIN_QUALIFYING_PAIRS = 1  # or 2-3
# Candidate currencies / pairs universe
CURRENCIES = ["USD", "EUR", "GBP", "AUD", "NZD", "CAD", "JPY"]
STRENGTH_PAIRS = [
    "EUR_USD",
    "GBP_USD",
    "AUD_USD",
    "USD_CAD",
    "USD_JPY",
    "EUR_GBP",
    "EUR_JPY",
    "EUR_AUD",
    "EUR_CAD",
    "GBP_JPY",
    "GBP_AUD",
    "GBP_CAD",
    "AUD_JPY",
    "AUD_CAD",
]
STRENGTH_TIMEFRAMES = {"H1": 1, "H4": 3, "H8": 6}
STRENGTH_FAST_LOOKBACK = 5  # bars
STRENGTH_SLOW_LOOKBACK = 20  # bars
STRENGTH_FAST_WEIGHT = 0.7
STRENGTH_SLOW_WEIGHT = 0.3
ENABLE_STRENGTH_ACCELERATION = False
STRENGTH_ACCELERATION_WEIGHT = 0.5
STRENGTH_ATR_PERIOD = 14
ENABLE_EMA_TREND = False
ENABLE_ATR_NORMALIZED_STRENGTH = False
ENABLE_BREAKOUT_CONFIRMATION = False
BREAKOUT_CONFIRMATION_CLOSES = 2
ENABLE_ATR_SLTP = True
# --- News filter ---
ENABLE_NEWS_FILTER = False
NEWS_LOG_PATH = "news_events.log"
NEWS_CURRENCIES = ["USD", "JPY", "EUR", "GBP"]
# --- Signal corroboration (directional consensus across JPY crosses) ---
MIN_DOMINANCE_RATIO = 1.5
ENABLE_VOLATILITY_NORMALIZED_DOMINANCE = False
DOMINANCE_ATR_PERIOD = 14
# --- JPYTrendStrategy trade parameters ---
JPY_PIP = 0.01
MIN_MARKET_STRENGTH = 0.03
FRONT_RUN_PIPS = 15
MIN_RR = 1.2
# --- JPYTrendStrategy ATR-based SL/TP (only used when ENABLE_ATR_SLTP=True) ---
JPY_ATR_PERIOD = 14
JPY_ATR_HISTORY_LOOKBACK = 50
JPY_ATR_SL_MULTIPLIER_NORMAL = 2.2
JPY_ATR_SL_MULTIPLIER_HIGH_VOL = 2.8
JPY_ATR_SL_MULTIPLIER_LOW_VOL = 1.8
JPY_ATR_RR_MULTIPLE = 2.0  # TP distance = SL distance * this
# ==========================================
# DATA PROVIDER SETTINGS
# ==========================================
DATA_SOURCE = "OANDA_WITH_YAHOO_FALLBACK"
# ==========================================
# ML Confirmation Layer
# ==========================================
ENABLE_ML_CONFIRMATION = False
ML_MIN_CONFIDENCE = 0.50
ML_MODEL_PATH = "ml_trade_model.pkl"
ENABLE_ML_WEIGHTED_DOMINANCE = False
ML_RETRAIN_HOURS = 24
ML_TRAIN_GRANULARITY = "H1"
ML_TRAIN_CANDLE_COUNT = 3000
ML_HOLDOUT_FRACTION = 0.2
ML_LABEL_HORIZON = 3
ML_MIN_HOLDOUT_F1 = 0.0
# ==========================================
# SIDEWAYS / RANGE DETECTION (final optimized values)
# ==========================================
ENABLE_RANGE_DETECTOR = False
RANGE_DETECT_LOOKBACK_DAYS = 3
RANGE_DETECT_MAX_RANGE_PCT = 2.0
RANGE_DETECT_MIN_VOL_RATIO = 0.6
SKIP_SIDEWAYS_PAIRS = False
# --- Weekly protection / entry gating ---
MACRO_PROTECTION_PIPS = 10
MIN_VALID_PAIRS_TO_TRADE = 1
DEBUG_SLTP = True  # print raw entry/sl/tp/S-R values before the validity check; flip off once diagnosed
# --- Allow single strong pair & only trade top pair(s) ---
TRADE_TOP_PAIRS = 1  # Always trade only single strongest/weakest pair per cycle
# --- ML CONFIRMATION (MERGED MODE) ---
# (kept as the final/authoritative block)
ENABLE_ML_CONFIRMATION = False
ML_MIN_CONFIDENCE = 0.50
ML_TRAIN_PAIR = "USDJPY=X"
DEMO_MODE = False
# --------------------------
# STRENGTH & TREND SETTINGS
# --------------------------
MIN_STRENGTH_GAP = 1.2
SIDEWAYS_LOOKBACK_DAYS = 7
MIN_LONG_TREND_ANGLE = 15
BREAKOUT_LOOKBACK_DAYS = 30
MAX_SIDEWAYS_RANGE_PCT = 1.8
BREAKOUT_THRESHOLD_PCT = 0.3
MACRO_PROTECTION_PIPS = 10
# set False to disable the weekly-resistance/support proximity check entirely
ENABLE_MACRO_PROTECTION = False
DEFAULT_PAIRS = [
    # 歐美與主要貨幣
    "EURUSD=X",
    "GBPUSD=X",
    "AUDUSD=X",
    "USDCHF=X",
    "NZDUSD=X",
    "USDCAD=X",
    "EURGBP=X",
    # 日圓交叉盤 (JPY Crosses)
    "USDJPY=X",
    "EURJPY=X",
    "GBPJPY=X",
    "AUDJPY=X",
    "CADJPY=X",
    "CHFJPY=X",
    "NZDJPY=X",
    # 其他交叉盤
    "GBPAUD=X",
    "EURCHF=X",
]
# Additions needed in config.py for Phase 2.
# All are read via getattr() with sensible defaults in risk_integration.py,
# so the runner will NOT crash if you don't add these yet — but
# stays dormant (byte-for-byte original behavior) until you explicitly add
# and set it to True.
# Deprecated stateful risk manager. The active runner is stateless and uses
# OANDA as its sole runtime trading-state source of truth.
ENABLE_DYNAMIC_RISK_MANAGER = False
# --- RiskConfig defaults (snapshotted into each new cluster at entry time —
#     changing these later does NOT affect already-open positions) ---
# Tuning guide: see docs/exit_tuning_cheatsheet.md (section D) before editing.
RISK_ATR_MULTIPLIER_INIT = 2.0
RISK_BE_TRIGGER_R = 1.0
RISK_CHANDELIER_K_DEFAULT = 3.0
RISK_ENABLE_TIME_STOP = True
RISK_T_EXPECTED_HOURS = 24.0  # calibrate from your backtest's median hours-to-TP
RISK_TIME_REDUCE_THRESHOLD = 1.0
RISK_TIME_REDUCE_RATIO = 0.5
RISK_TIME_EXIT_THRESHOLD = 1.5
RISK_TIME_TIGHTEN_THRESHOLD = 1.5
RISK_VOL_COMPRESSION_FRAC = 0.6
RISK_MAX_SIZE_DECAY_RATIO = (
    0.7  # unused until pyramiding signals exist — harmless to add now
)
# --- Candle granularity for the Chandelier Exit's rolling high/low since entry ---
RISK_EXTREME_LOOKBACK_GRANULARITY = "H1"
# --- Strategy-Driven Exit: Strength Invalidation Close ---
# If a managed position's held direction no longer matches the base currency's
# strength vs JPY (per the freshly computed Currency Strength Matrix), close it
# immediately instead of waiting for SL/Chandelier/time-decay to catch up.
# NOTE: unlike RISK_* above, all invalidation params (sections A-B in
# docs/exit_tuning_cheatsheet.md) are read LIVE every cycle — edits here
# affect ALREADY-OPEN positions on the next run.
ENABLE_STRATEGY_INVALIDATION_CLOSE = True
# The base-vs-JPY strength gap must still exceed this in the trade's favor —
# a merely non-negative, decaying edge is NOT enough (prevents holding a
# stubborn, mediocre/lagging trade waiting for a full sign-flip).
STRATEGY_INVALIDATION_MIN_GAP = 0.2  # 0.15, 0.5
# Additionally require the base currency to still sit in the favorable half
# of the absolute strength ranking (top half for LONG, bottom half for SHORT).
ENABLE_STRATEGY_INVALIDATION_RANK_CHECK = True
STRATEGY_INVALIDATION_TOP_TIER_FRACTION = 0.5
# Additionally require the gap to still clear the SAME dynamic "entry-grade"
# cutoff (max_gap * factor) that JPYTrendStrategy applies to fresh entries —
# a position that wouldn't qualify for a new entry this cycle is closed
# rather than held on the static floor above alone.
ENABLE_STRATEGY_INVALIDATION_PROPORTIONAL_CHECK = True
STRATEGY_INVALIDATION_PROPORTIONAL_FACTOR = 0.4
# --- Strategy-Driven Exit: Technical (MA5 alignment) Invalidation Close ---
# Automates closing a position whose MA5 multi-timeframe alignment (the same
# check used at entry) has turned mixed or fully opposite — the bot closes it
# itself instead of requiring a manual close_all_trades() intervention.
ENABLE_TECHNICAL_INVALIDATION_CLOSE = True
# Defaults to REQUIRE_ALIGNED (same bar as entry) if left unset.
# Set to 2 (of 3 timeframes: H4/H1/M30) to tolerate a single-timeframe pullback
# (e.g. M30 dipping below MA5) without closing the position.
TECHNICAL_INVALIDATION_REQUIRE_ALIGNED = 2
# --- Global Invalidation Sweep (kill switch) ---
# Legacy setting for the retired stateful risk manager. The active runner uses
# OANDA's live trade and order state before every entry.
ENABLE_GLOBAL_INVALIDATION_SWEEP = True
# --- Multi-Factor Deterioration Invalidation (combined scoring) ---
# Sums the weight of EVERY currently-failing factor above (fundamental
# strength + technical alignment) instead of stopping at the first one
# found — several merely-borderline factors (e.g. a thin-but-still-passing
# strength gap AND a newly-mixed MA5 alignment) can combine and close a
# position that no single hard check alone would have flagged yet. With the
# default weights/threshold below, this reproduces "any single factor
# closes" by default — raise the threshold to require multiple factors, or
# tune individual weights to make specific factors dominate the score.
ENABLE_MULTI_FACTOR_INVALIDATION = True
# 2.0 = require at least TWO failing factors (multi-factor consensus) before
# closing — a single MA wobble or gap jitter alone no longer closes a trade.
INVALIDATION_DETERIORATION_SCORE_THRESHOLD = 2.0
INVALIDATION_WEIGHT_GAP_ROBUSTNESS = 1.0
INVALIDATION_WEIGHT_RANK_TIER = 1.0
INVALIDATION_WEIGHT_PROPORTIONAL_CUTOFF = 1.0
INVALIDATION_WEIGHT_TECHNICAL_MIXED = 1.0
INVALIDATION_WEIGHT_TECHNICAL_OPPOSITE = 2.0
# ==========================================
# POST-EXIT GATE — Adaptive Threshold Engine
# ==========================================
# LIVE mode: gate participates in actual demo/live decisions.
# Never overrides hard risk-manager controls — existing safety remains authoritative.
POST_EXIT_GATE_ENABLED = True
POST_EXIT_GATE_SHADOW = False
POST_EXIT_TIER_MULTIPLIER = {
    "tier1": 1.00,
    "tier2": 1.10,
    "tier3": 1.20,
}
POST_EXIT_RANK_MULTIPLIER = {
    1: 1.00,
    2: 1.00,
    3: 1.03,
    4: 1.07,
}
POST_EXIT_MC_REGIME_MULTIPLIER = {
    "STRONG_MOMENTUM": 0.97,
    "NEUTRAL": 1.00,
    "CONSOLIDATION": 1.05,
}
POST_EXIT_SIZE_MULTIPLIER = {
    "tier1": 1.00,
    "tier2": 0.85,
    "tier3": 0.70,
}
POST_EXIT_STRICT_WINDOW_HOURS = 24.0
# ==========================================
# CENTRALIZED THRESHOLDS — Tune ONLY in config.py
# ==========================================
# ALIGNMENT_THRESHOLD: Min timeframes that must agree on direction.
#   Default 3 = H4+H1+M30 all aligned (strict, current behavior).
#   Relaxed 2 = tolerate 1 mixed, qualify anyway.
ALIGNMENT_THRESHOLD = 3
# DYNAMIC_RISK_TIMEFRAME: Which candle timeframe drives MA5 crossover exit.
#   "H4" = slow exit (current, more pullback, more tail).
#   "H1" = faster exit, less pullback, trend reverses → exit immediately.
DYNAMIC_RISK_TIMEFRAME = "H4"
# TP_RATIO / SL_RATIO: Profit-Taking / Stop-Loss multipliers (× risk distance).
#   TP_RATIO default 1.0 = 1R profit (current).  Relaxed 1.8 = let profits run.
#   SL_RATIO default 1.5 = 1.5R stop (final defense).  Usually keep as-is.
#   Note: MC-regime-specific TP multipliers (MC_TP_MULTIPLIER_*) override this
#         on a per-cycle basis. This is the global FALLBACK when no regime match.
TP_RATIO = 1.0
SL_RATIO = 1.5
# ==========================================
# TUNING CHEAT SHEET (edit the 4 values above, NOT here)
# ==========================================
# Mode A — Strict (default):
#   ALIGNMENT_THRESHOLD = 3
#   DYNAMIC_RISK_TIMEFRAME = "H4"
#   TP_RATIO = 1.0
#   SL_RATIO = 1.5
#
# Mode B — More signals:
#   ALIGNMENT_THRESHOLD = 2
#   DYNAMIC_RISK_TIMEFRAME = "H4"
#   TP_RATIO = 1.0
#   SL_RATIO = 1.5
#
# Mode C — Faster exit + let profits run (RECOMMENDED):
#   ALIGNMENT_THRESHOLD = 2
#   DYNAMIC_RISK_TIMEFRAME = "H1"
#   TP_RATIO = 1.8
#   SL_RATIO = 1.5
#
# Mode D — Aggressive:
#   ALIGNMENT_THRESHOLD = 2
#   DYNAMIC_RISK_TIMEFRAME = "H1"
#   TP_RATIO = 2.0
#   SL_RATIO = 1.5
# ==========================================
# Legacy shadow-only settings (preserved for backward compatibility, no longer used for live decisions)
POST_EXIT_SHADOW_MODE = True
POST_EXIT_HALF_LIFE_HOURS = 6.0
POST_EXIT_RULES = {
    "tier1": {"baseline": 1.00, "m_reason": 1.00},
    "tier2": {"baseline": 1.05, "m_reason": 1.10},
    "tier3": {"baseline": 1.15, "m_reason": 1.25},
}
# ==========================================
# MC REGIME-AWARE TRADING — v2 参数矩阵
# ===========================
MC_REGIME_ENABLED = True
# CONSOLIDATION 模式下, 候选信号的 |strength_score| 必须 ≥ 此门槛才放行。
MC_REGIME_STRENGTH_HURDLE_CONSOLIDATION = 0.08
# --- 每 regime 的最大候选开仓数 ---
# CONSOLIDATION: 严格限 1 个 (即使多个合格也只选 top1)
# NEUTRAL:       最多 3 个 (取 top N + 方向兼容过滤)
# AGGRESSIVE:    最多 4 个 (全部方向一致才开)
MC_MAX_POSITIONS_NEUTRAL = 3
MC_MAX_POSITIONS_CONSOLIDATION = 1
MC_MAX_POSITIONS_AGGRESSIVE = 4
# --- 止盈倍率 (take_profit = entry ± |entry - SL| * TP_MULTIPLIER) ---
# NEUTRAL:       ×1.5 放大盈利目标
# CONSOLIDATION: ×0.8 保守止盈, 快进快出
# AGGRESSIVE:    ×1.0 保持默认
MC_TP_MULTIPLIER_NEUTRAL = 1.5
MC_TP_MULTIPLIER_CONSOLIDATION = 0.8
MC_TP_MULTIPLIER_AGGRESSIVE = 1.0
# --- 主动平仓紧密度 (<1 宽松, >1 收紧, 1.0 默认) ---
# NEUTRAL:       0.7 更宽松持有
# CONSOLIDATION: 1.3 收紧条件, 提前离场
# AGGRESSIVE:    1.0 保持默认 (当前 risk layer 尚未对接, 仅日志追踪)
MC_EXIT_TIGHTNESS_NEUTRAL = 0.7
MC_EXIT_TIGHTNESS_CONSOLIDATION = 1.3
MC_EXIT_TIGHTNESS_AGGRESSIVE = 1.0
# --- Basket execution vs single-pair (v1.3 compatible) ---
# False (default): 始终只执行 strength ranking 的 top1 pair, 忽略 max_pos / candidate pool.
#   这是 v1.3 的行为 —— 一次信号只开一单, 永不产生 EUR_JPY + GBP_JPY + USD_JPY 式的
#   同向相关 basket. 安全, 适合实盘.
# True: 启用 v1.4 basket loop, 按 MC regime 的 max_pos 取 top N 方向兼容 pair,
#   逐个实盘下单. 实验性功能, 默认关闭.
ENABLE_MC_BASKET_EXECUTION = False

# ==========================================
# MC-REGIME → Dynamic-Risk exit_tightness threading
# ==========================================
# If a fill is confirmed at OANDA but PyramidCluster registration then fails,
# the position is LIVE but UNMANAGED (native broker SL/TP only). Log that
# failure loudly via utils/logging_utils (pair, fill price, exception) ON TOP
# of the runner's stdout prints — the silent fail-open that produced the
# stacked same-direction AUD/JPY shorts must not recur quietly.
# Default True (loud by default, opt-out only).
ENABLE_CLUSTER_LOUD_LOG_ON_FILL_FAILURE = True

# ==========================================
# MONTE CARLO — DAILY + WEEKLY 双周期
# Phase A: Observation Only — 仅记录, 不控制入场
# ==========================================
# — Scheduler (UTC 0) —
MC_DAILY_RUN_UTC_HOUR    = 8     # Daily MC: every day at 08:00 UTC
MC_WEEKLY_RUN_UTC_DAY    = 1     # Weekly MC: every Monday
MC_WEEKLY_RUN_UTC_HOUR   = 8     #            at 08:00 UTC

# — Probability Direction Thresholds —
MC_BULL_THRESHOLD        = 0.55  # >55% → Bullish
MC_BEAR_THRESHOLD        = 0.45  # <45% → Bearish
# 0.45 ~ 0.55 → Neutral

# — Daily MC (D: short-term / timing) —
D_WINDOW_DAYS            = 90    # Lookback window
D_FORECAST_DAYS          = 5     # Forecast horizon

# — Weekly MC (W: medium-term / regime context) —
W_WINDOW_DAYS            = 728   # ~104 weeks lookback
W_FORECAST_DAYS          = 35    # ~5 weeks forecast horizon

# — JPY Cross-Market Regime Consistency (W dimension) —
JPY_REGIME_PAIRS         = ["USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY"]
JPY_REGIME_STRONG_COUNT  = 3     # ≥3 same direction = STRONG regime

# — Alignment Observation Schema —
# Phase A: labels & logs only. NO trading / NO filtering.
# Scoring rules:
#   W+D both bullish → +2 / STRONG_BULL
#   W+D both bearish → +2 / STRONG_BEAR
#   One matches + one neutral → +1 / MODERATE
#   Opposite directions →  0 / CONFLICT
#   Any neutral alone →    0 / NEUTRAL
MC_ALIGNMENT_LOG_FIELDS = [
    "timestamp_utc", "pair",
    "strategy_direction",
    "D_p_up", "D_p_down", "D_regime",
    "W_p_up", "W_p_down", "W_regime",
    "mc_internal_alignment",
    "mc_strategy_alignment",
    "jpy_regime_weekly",
    "outcome", "realized_pnl",
    "max_favorable_pips", "max_adverse_pips",
]