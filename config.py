# config.py
"""
Central configuration — edit this file to control all strategy behaviour.
Do not hardcode these values elsewhere in the codebase.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# OANDA connection
# ==========================================
OANDA_ENV = os.getenv("OANDA_ENV", "practice")
OANDA_API_TOKEN = os.getenv("OANDA_API_TOKEN", "")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")

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
SIGNAL_TIMEFRAMES = ["H4", "H1", "M30"]  # ["H4", "H1", "M30", "M15"] for testing now ignore M15
REQUIRE_ALIGNED = len(SIGNAL_TIMEFRAMES)

# TP / SL in pips (JPY pairs: 1 pip = 0.01)
TP_PIPS = 100                 # fixed take profit: entry + 100 pips
SL_BUFFER_PIPS = 20          # pips below today's daily low
SPREAD_PIPS = 3              # conservative spread buffer added to SL

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
MANUAL_STRATEGY = 1  # 1=TREND_COMBINED, 2=RANGE_REVERSION, 3=BREAKOUT_CONFIRM, 4=TREND_PULLBACK
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
# CURRENCIES = ["USD", "EUR", "GBP", "AUD", "JPY"]
# STRENGTH_PAIRS = [
#     "EUR_USD",
#     "GBP_USD",
#     "AUD_USD",
#     "NZD_USD",
#     "USD_CAD",
#     "USD_JPY",
#     "EUR_GBP",
#     "EUR_JPY",
#     "EUR_AUD",
#     "EUR_CAD",
#     "GBP_JPY",
#     "GBP_AUD",
#     "GBP_CAD",
#     "AUD_JPY",
#     "AUD_CAD",
#     "NZD_JPY",
#     "CAD_JPY",
# ]
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
    "AUD_CAD"
]

STRENGTH_TIMEFRAMES = {"H1": 1, "H4": 3, "H8": 6}

STRENGTH_FAST_LOOKBACK = 5   # bars
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


# Additions needed in config.py for Phase 2.
# All are read via getattr() with sensible defaults in risk_integration.py,
# so the runner will NOT crash if you don't add these yet — but
# stays dormant (byte-for-byte original behavior) until you explicitly add
# and set it to True.
# --- Master switch — everything in Phase 2 is dormant while this is False ---
ENABLE_DYNAMIC_RISK_MANAGER = True

# --- Where the cluster state JSON lives ---
CLUSTER_STATE_PATH = "state/open_clusters.json"

# --- RiskConfig defaults (snapshotted into each new cluster at entry time —
#     changing these later does NOT affect already-open positions) ---
RISK_ATR_MULTIPLIER_INIT = 2.0
RISK_BE_TRIGGER_R = 1.0
RISK_CHANDELIER_K_DEFAULT = 3.0
RISK_ENABLE_TIME_STOP = True
RISK_T_EXPECTED_HOURS = 24.0          # calibrate from your backtest's median hours-to-TP
RISK_TIME_REDUCE_THRESHOLD = 1.0
RISK_TIME_REDUCE_RATIO = 0.5
RISK_TIME_EXIT_THRESHOLD = 1.5
RISK_TIME_TIGHTEN_THRESHOLD = 1.5
RISK_VOL_COMPRESSION_FRAC = 0.6
RISK_MAX_SIZE_DECAY_RATIO = 0.7        # unused until pyramiding signals exist — harmless to add now

# --- Candle granularity for the Chandelier Exit's rolling high/low since entry ---
RISK_EXTREME_LOOKBACK_GRANULARITY = "H1"