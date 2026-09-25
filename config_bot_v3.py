# config_bot.py — v7 · UNIFIED STRATEGY CONFIG
"""
ALL strategy/profile settings in ONE file.
OANDA API/connection → config_oanda.py (KEPT SEPARATE)

Purpose: Strategy & profile parameters ONLY.
Connection tokens/env → config_oanda.py (runtime config)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# ==========================================
# GLOBAL DEFAULTS — shared across profiles
# ==========================================
ALL_PAIRS = [
    "EURUSD=X",
    "GBPUSD=X",
    "EURJPY=X",
    "GBPJPY=X",
    "AUDUSD=X",
    "USDJPY=X",
    "GBPAUD=X",
    "USDCHF=X",
    "AUDJPY=X",
    "EURGBP=X",
    "NZDUSD=X",
    "CADJPY=X",
]
YAHOO_TO_OANDA = {
    "EURUSD=X": "EUR_USD",
    "GBPUSD=X": "GBP_USD",
    "EURJPY=X": "EUR_JPY",
    "GBPJPY=X": "GBP_JPY",
    "AUDUSD=X": "AUD_USD",
    "USDJPY=X": "USD_JPY",
    "GBPAUD=X": "GBP_AUD",
    "USDCHF=X": "USD_CHF",
    "AUDJPY=X": "AUD_JPY",
    "EURGBP=X": "EUR_GBP",
    "NZDUSD=X": "NZD_USD",
    "CADJPY=X": "CAD_JPY",
}

# Data / MC defaults
YF_INTERVAL = "4h"
YF_PERIOD_FULL = "30d"
YF_PERIOD_RESAMPLE = "60d"
YF_INTERVAL_D = "1d"
YF_PERIOD_FULL_D = "120d"
YF_PERIOD_RESAMPLE_D = "180d"

PERIODS_YEAR = 252
MC_BAND_PCT = 90
MC_MAX_AGE_HOURS = 24
SIMULATIONS = 5000
CONFIDENCE = MC_BAND_PCT / 100.0

ATR_PERIOD = 14
BASE_TP_PIPS = 50
EMA100_BUFFER_PIPS = 30
MIN_SL_PIPS = 35
MIN_SL_PIPS_JPY = MIN_SL_PIPS + 10

# ATR Minimum Volatility Filter (defaults — profiles may override)
ENABLE_ATR_MINIMUM_FILTER = True
ATR_MIN_PIPS = 6.0
ATR_MIN_RELATIVE_PCT = 0.045

DEBUG_MODE = False
NO_COOLDOWN = True
DEMO_LOT_SIZE = 10000
LIVE_LOT_SIZE = 1000

# Confluence / multi-TF
MULTI_TF_CONFLUENCE = False
CONFLUENCE_REQUIRED_TFS = 2

# Dynamic TP / Exit
TRAILING_TP = True
DYNAMIC_TP = False
TP_RAISE_THRESHOLD_PIPS = 15

# Lookback / Forecast
H4_LOOKBACK = 90
H4_FORECAST = 8
DAILY_LOOKBACK = 90
DAILY_FORECAST = 5

# Feature / Model
USE_ATR = True
USE_MACD = True
USE_RSI = True
USE_ADX = True
MODEL_TYPE = "xgboost"
TARGET_HORIZON = 6
TRAIN_LOOKBACK_BARS = 5000

# ==========================================
# v4 BASE-CURRENCY TREND STRATEGY PARAMETERS
# Generic — no JPY_ prefix. Consumed by BaseCurrencyTrendStrategy.
# ==========================================

# --- Core thresholds (same as config.py generic, unified) ---
ALIGNMENT_THRESHOLD = 3
ALIGNMENT_REQUIRE_MAJORITY = True
ALIGNMENT_THRESHOLD_MIN = 2
STRENGTH_GAP_THRESHOLD = 1.5
MIN_STRENGTH_SCORE = 0.15
STRENGTH_CUTOFF_RATIO = 0.4
MIN_MARKET_STRENGTH = 0.03
MIN_RR = 1.2
FRONT_RUN_PIPS = 15
MACRO_PROTECTION_PIPS = 10
MIN_VALID_PAIRS_TO_TRADE = 1
MIN_DOMINANT_PAIRS = 1
TRADE_TOP_PAIRS = 3
SKIP_SIDEWAYS_PAIRS = False
MIN_STRENGTH_PASSING_PAIRS = 2
MIN_QUALIFYING_PAIRS = 1
MIN_DOMINANCE_RATIO = 1.5

# --- Enable flags ---
ENABLE_ATR_SLTP = True
ENABLE_MACRO_PROTECTION = False
ENABLE_ATR_MIN_FILTER = True
ENABLE_NEWS_FILTER = False
ENABLE_VOLATILITY_NORMALIZED_DOMINANCE = False
ENABLE_STRENGTH_ACCELERATION = False
ENABLE_EMA_TREND = False
ENABLE_ATR_NORMALIZED_STRENGTH = False
ENABLE_BREAKOUT_CONFIRMATION = False
ENABLE_RANGE_DETECTOR = False

# --- ATR-based SL/TP (generic, no JPY_ prefix) ---
ATR_PERIOD = 14
ATR_HISTORY_LOOKBACK = 50
ATR_SL_MULTIPLIER_NORMAL = 2.2
ATR_SL_MULTIPLIER_HIGH_VOL = 2.8
ATR_SL_MULTIPLIER_LOW_VOL = 1.8
ATR_RR_MULTIPLE = 2.0

# --- ATR minimum volatility filter ---
ATR_MIN_PIPS = 6.0
ATR_MIN_RELATIVE_PCT = 0.045

# --- Strength matrix ---
STRENGTH_PAIRS = [
    "EUR_USD", "GBP_USD", "AUD_USD",
    "USD_JPY", "EUR_GBP", "EUR_JPY", "EUR_AUD",
    "GBP_JPY", "GBP_AUD", "AUD_JPY", "NZD_USD",
]
STRENGTH_TIMEFRAMES = {"H1": 1, "H4": 3, "H8": 6}
STRENGTH_FAST_LOOKBACK = 5
STRENGTH_SLOW_LOOKBACK = 20
STRENGTH_FAST_WEIGHT = 0.7
STRENGTH_SLOW_WEIGHT = 0.3
STRENGTH_ATR_PERIOD = 14
STRENGTH_ACCELERATION_WEIGHT = 0.5

# --- News filter ---
BREAKOUT_CONFIRMATION_CLOSES = 2
NEWS_LOG_PATH = "news_events.log"
NEWS_CURRENCIES = ["USD", "JPY", "EUR", "GBP"]
CURRENCIES = ["USD", "EUR", "GBP", "AUD", "JPY", "NZD"]

# --- Strength invalidation exit ---
ENABLE_STRATEGY_INVALIDATION_CLOSE = True
STRATEGY_INVALIDATION_MIN_GAP = 0.2
ENABLE_STRATEGY_INVALIDATION_RANK_CHECK = True
STRATEGY_INVALIDATION_TOP_TIER_FRACTION = 0.5
ENABLE_STRATEGY_INVALIDATION_PROPORTIONAL_CHECK = True
STRATEGY_INVALIDATION_PROPORTIONAL_FACTOR = 0.4
ENABLE_TECHNICAL_INVALIDATION_CLOSE = True
TECHNICAL_INVALIDATION_REQUIRE_ALIGNED = 2
ENABLE_MULTI_FACTOR_INVALIDATION = True
INVALIDATION_DETERIORATION_SCORE_THRESHOLD = 2.0
INVALIDATION_WEIGHT_GAP_ROBUSTNESS = 1.0
INVALIDATION_WEIGHT_RANK_TIER = 1.0
INVALIDATION_WEIGHT_PROPORTIONAL_CUTOFF = 1.0
INVALIDATION_WEIGHT_TECHNICAL_MIXED = 1.0
INVALIDATION_WEIGHT_TECHNICAL_OPPOSITE = 2.0

# --- Post-exit gate ---
POST_EXIT_GATE_ENABLED = True
POST_EXIT_GATE_SHADOW = False
POST_EXIT_TIER_MULTIPLIER = {"tier1": 1.00, "tier2": 1.10, "tier3": 1.20}
POST_EXIT_RANK_MULTIPLIER = {1: 1.00, 2: 1.00, 3: 1.03, 4: 1.07}
POST_EXIT_MC_REGIME_MULTIPLIER = {"STRONG_MOMENTUM": 0.97, "NEUTRAL": 1.00, "CONSOLIDATION": 1.05}
POST_EXIT_SIZE_MULTIPLIER = {"tier1": 1.00, "tier2": 0.85, "tier3": 0.70}
POST_EXIT_STRICT_WINDOW_HOURS = 24.0

# --- Dynamic risk management ---
DYNAMIC_RISK_TIMEFRAME = "H4"
TP_RATIO = 1.0
SL_RATIO = 1.5
RISK_ATR_MULTIPLIER_INIT = 2.0
RISK_BE_TRIGGER_R = 1.0
RISK_CHANDELIER_K_DEFAULT = 3.0
RISK_ENABLE_TIME_STOP = True
RISK_T_EXPECTED_HOURS = 24.0
RISK_TIME_REDUCE_THRESHOLD = 1.0
RISK_TIME_REDUCE_RATIO = 0.5
RISK_TIME_EXIT_THRESHOLD = 1.5
RISK_TIME_TIGHTEN_THRESHOLD = 1.5
RISK_VOL_COMPRESSION_FRAC = 0.6
RISK_MAX_SIZE_DECAY_RATIO = 0.7
RISK_EXTREME_LOOKBACK_GRANULARITY = "H1"

# --- MC regime ---
MC_REGIME_ENABLED = True
MC_REGIME_STRENGTH_HURDLE_CONSOLIDATION = 0.08
CROSS_STRENGTH_EXTREME_THRESHOLD = 2.0
MC_MAX_POSITIONS_NEUTRAL = 2
MC_MAX_POSITIONS_CONSOLIDATION = 1
MC_MAX_POSITIONS_AGGRESSIVE = 3
MC_TP_MULTIPLIER_NEUTRAL = 1.5
MC_TP_MULTIPLIER_CONSOLIDATION = 0.8
MC_TP_MULTIPLIER_AGGRESSIVE = 1.0
MC_EXIT_TIGHTNESS_NEUTRAL = 0.7
MC_EXIT_TIGHTNESS_CONSOLIDATION = 1.3
MC_EXIT_TIGHTNESS_AGGRESSIVE = 1.0
ENABLE_MC_BASKET_EXECUTION = True
MAX_NEW_ENTRIES_PER_CYCLE = 1
MC_DAILY_RUN_UTC_HOUR = 8
MC_WEEKLY_RUN_UTC_DAY = 1
MC_WEEKLY_RUN_UTC_HOUR = 8
MC_BULL_THRESHOLD = 0.55
MC_BEAR_THRESHOLD = 0.45
D_WINDOW_DAYS = 90
D_FORECAST_DAYS = 5
W_WINDOW_DAYS = 728
W_FORECAST_DAYS = 35

# ==========================================
# v4 DOMINANCE RATIO FILTER + OVERRIDE MODE
# ==========================================
DOMINANCE_RATIO_ENABLED = True
DOMINANCE_RATIO_THRESHOLD = 2.0
GAP_SEPARATION_THRESHOLD = 1.3
DOMINANCE_OVERRIDE_ENABLED = True
DOMINANCE_OVERRIDE_THRESHOLD = 2.4
DOMINANCE_OVERRIDE_MEDIAN_FLOOR = 0.15

# ==========================================
# v4 MC Direction Probability Conflict Check
# ==========================================
ENABLE_MC_CONFLICT_CHECK = True
ENABLE_MC_CONFLICT_BLOCK = False
MC_CONFLICT_PROB_THRESHOLD = 0.52
MC_CONFLICT_SEVERE_THRESHOLD = 0.58

# ==========================================
# v4 CROSS-GROUP MUTEX
# ==========================================
CROSS_GROUP_MUTEX_ENABLED = False
CROSS_GROUP_MUTEX_ACTION = "FLATTEN"

# ==========================================
# v4 STRATEGY GROUP DEFINITIONS
# ==========================================
STRATEGY_GROUPS = {
    "JPY": {"quote_ccy": "JPY", "tag_prefix": "JPY-STRENGTH"},
    "USD": {"quote_ccy": "USD", "tag_prefix": "USD-STRENGTH"},
}

# --- Pip sizes by quote currency ---
PIP_SIZE_BY_QUOTE = {
    "JPY": 0.01, "USD": 0.0001, "CHF": 0.0001,
    "GBP": 0.0001, "EUR": 0.0001, "AUD": 0.0001, "NZD": 0.0001,
}

# ==========================================
# 🔑 ACCOUNT IDs ONLY — reference config_oanda connection
# ==========================================
from config_oanda import api as OANDA_API
from config_oanda import (
    OANDA_ACCOUNT_ID_2 as OANDA_ACCOUNT_ID_PROFILE2,
    OANDA_ACCOUNT_ID_3 as OANDA_ACCOUNT_ID_PROFILE3,
    OANDA_ACCOUNT_ID_4 as OANDA_ACCOUNT_ID_PROFILE4,
)

# ==========================================
# ✅ 明确常量清单（禁止遍历 dir() 猜测合并）
# ==========================================
# 铁律：load_profile() 只能合并这些明确列出的 key，避免“扫一遍模块变量就塞进 P”的不可控行为。
_GLOBAL_CONSTANT_KEYS: tuple[str, ...] = (
    # pairs / mapping
    "ALL_PAIRS",
    "YAHOO_TO_OANDA",
    # yfinance
    "YF_INTERVAL",
    "YF_PERIOD_FULL",
    "YF_PERIOD_RESAMPLE",
    "YF_INTERVAL_D",
    "YF_PERIOD_FULL_D",
    "YF_PERIOD_RESAMPLE_D",
    # MC
    "PERIODS_YEAR",
    "MC_BAND_PCT",
    "MC_MAX_AGE_HOURS",
    "SIMULATIONS",
    "CONFIDENCE",
    # ATR / SLTP
    "ATR_PERIOD",
    "BASE_TP_PIPS",
    "EMA100_BUFFER_PIPS",
    "MIN_SL_PIPS",
    "MIN_SL_PIPS_JPY",
    # runtime flags
    "DEBUG_MODE",
    "NO_COOLDOWN",
    "DEMO_LOT_SIZE",
    "LIVE_LOT_SIZE",
    # confluence
    "MULTI_TF_CONFLUENCE",
    "CONFLUENCE_REQUIRED_TFS",
    # dynamic tp
    "TRAILING_TP",
    "DYNAMIC_TP",
    "TP_RAISE_THRESHOLD_PIPS",
    # lookback / forecast
    "H4_LOOKBACK",
    "H4_FORECAST",
    "DAILY_LOOKBACK",
    "DAILY_FORECAST",
    # feature / model
    "USE_ATR",
    "USE_MACD",
    "USE_RSI",
    "USE_ADX",
    "MODEL_TYPE",
    "TARGET_HORIZON",
    "TRAIN_LOOKBACK_BARS",
    # shared resources
    "D_STRATEGY_GROUPS",
    "EXCLUDE_CURRENCIES_GLOBAL",
    # ATR Minimum Volatility Filter
    "ENABLE_ATR_MINIMUM_FILTER",
    "ATR_MIN_PIPS",
    "ATR_MIN_RELATIVE_PCT",
)

# ==========================================
# 📊 PROFILE STRATEGY CONFIG — ALL IN ONE
# ==========================================
PROFILE_CFG = {
    "profile2": {
        "LABEL": "PROFILE2",
        "ACCOUNT_NAME": "Account 002",
        "OANDA_ACCOUNT_ID": OANDA_ACCOUNT_ID_PROFILE2,
        "COOLDOWN_FILE": "cooldown_profile2.json",
        "RESULTS_DIR": "daily_results_profile2",
        # ── Identity ──
        "MODE": "LEVEL10",
        "BASE_MIN_EDGE": 0.50,
        # ── Weights: S=35 R=20 A=15 X=20 M=10 ──
        "WEIGHT_STRENGTH": 0.35,
        "WEIGHT_RSI": 0.20,
        "WEIGHT_ADX": 0.15,
        "WEIGHT_XGB": 0.20,
        "WEIGHT_MC": 0.10,
        # ── Thresholds ──
        "MIN_CONVICTION_SCORE": 30.0,
        "MIN_SCORE_GAP": 0.10,
        "MAX_OPEN_POSITIONS": 3,
        "MAX_OPEN_PER_RUN": 1,
        "XGB_BULLISH_THRESHOLD": 0.52,
        "MC_BULLISH_THRESHOLD_PCT": 52.0,
        "MC_STRONG_THRESHOLD": 0.60,
        "REQUIRE_DIRECTION_CONSENSUS": True,
        "CONSENSUS_THRESHOLD": 2,
        "CONSENSUS_REQUIRED_VOTES": 2,
        "REQUIRE_STRONG_MOMENTUM": False,
        "ADX_SCALE_FACTOR": 2.0,
        # ── TREND FILTER: Profile2 = OFF ──
        "TREND_FILTER_ENABLED": False,
        "WEEK_EMA100_FILTER_ENABLED": False,
        "EMA_PERIOD_FAST": 20,
        "EMA_PERIOD_SLOW": 40,
        # ── TP/SL multipliers ──
        "TP_MULT": 2.0,
        "TP_STRONG_MULT": 2.5,
        "ATR_SL_MULT": 2.0,
        "ATR_TP_MULT": 2.5,
        # ── Dynamic Exit ──
        "USE_DYNAMIC_SL": 2,
        "DYNAMIC_SL_MULT": 1.5,
        "BE_TRIGGER_ATR_MULT": 2.5,  # 1.5 → 2.5 · 晚一点推保本，让利润先跑
        "TRAIL_TRIGGER_ATR_MULT": 3.5,  # 2.5 → 3.5 · 更大盈利才启动 trailing
        "TRAIL_ATR_MULT": 2.8,  # 1.5 → 2.8 · trailing 距离加宽，给回调留空间
        "MAX_HOLD_BARS": 24,  # 12 → 24 · 15m TF: 3h → 6h，单边行情更多时间
        # ── SL Strategy ──
        "SL_USE_ZONE_HIERARCHY": True,
        # ── Pair Selection ──
        "USE_TOP_PAIRS_ONLY": False,
        "TOP_PAIRS_COUNT": 4,
        "TOP_PAIRS_MIN_GAP": 0.25,
        # ── MC ──
        "SKIP_MC": False,
    },
    "profile3": {
        "LABEL": "PROFILE3",
        "ACCOUNT_NAME": "Account 003",
        "OANDA_ACCOUNT_ID": OANDA_ACCOUNT_ID_PROFILE3,
        "COOLDOWN_FILE": "cooldown_profile3.json",
        "RESULTS_DIR": "daily_results_profile3",
        # ── Identity ──
        "MODE": "LEVEL10",
        "BASE_MIN_EDGE": 0.50,
        # ── ATR Minimum Volatility Filter ──
        "ENABLE_ATR_MINIMUM_FILTER": True,
        "ATR_MIN_PIPS": 6.0,
        "ATR_MIN_RELATIVE_PCT": 0.045,
        # ── Weights: S=40 R=15 A=15 X=20 M=10 ──
        "WEIGHT_STRENGTH": 0.40,
        "WEIGHT_RSI": 0.15,
        "WEIGHT_ADX": 0.15,
        "WEIGHT_XGB": 0.20,
        "WEIGHT_MC": 0.10,
        # ── Thresholds ──
        "MIN_CONVICTION_SCORE": 20.0,
        "MIN_SCORE_GAP": 0.10,
        "MAX_OPEN_POSITIONS": 6,
        "MAX_OPEN_PER_RUN": 2,
        "XGB_BULLISH_THRESHOLD": 0.55,
        "MC_BULLISH_THRESHOLD_PCT": 55.0,
        "MC_STRONG_THRESHOLD": 0.55,
        "REQUIRE_DIRECTION_CONSENSUS": True,
        "CONSENSUS_THRESHOLD": 2,
        "CONSENSUS_REQUIRED_VOTES": 2,
        "REQUIRE_STRONG_MOMENTUM": False,
        "ADX_SCALE_FACTOR": 2.0,
        # ── TREND FILTER: Profile3 = ON + Weekly EMA100 ──
        "TREND_FILTER_ENABLED": True,
        "WEEK_EMA100_FILTER_ENABLED": True,
        "EMA_PERIOD_FAST": 40,
        "EMA_PERIOD_SLOW": 80,
        # ── TP/SL multipliers ──
        "TP_MULT": 2.5,
        "TP_STRONG_MULT": 3.0,
        "ATR_SL_MULT": 2.5,
        "ATR_TP_MULT": 3.0,
        # ── Dynamic Exit ──
        "USE_DYNAMIC_SL": 2,
        "DYNAMIC_SL_MULT": 1.5,
        "BE_TRIGGER_ATR_MULT": 1.5,
        "TRAIL_TRIGGER_ATR_MULT": 2.5,
        "TRAIL_ATR_MULT": 1.5,
        "MAX_HOLD_BARS": 12,
        # ── SL Strategy ──
        "SL_USE_ZONE_HIERARCHY": True,
        # ── Pair Selection ──
        "USE_TOP_PAIRS_ONLY": False,
        "TOP_PAIRS_COUNT": 4,
        "TOP_PAIRS_MIN_GAP": 0.25,
        # ── MC ──
        "SKIP_MC": False,
        "SL_ZONE_TRAILING": True,
    },
    # ✅ ─── Profile4 / Account004 · DEMO 全新独立 ───
    "profile4": {
        "LABEL": "PROFILE4",
        "ACCOUNT_NAME": "Account 004",
        "OANDA_ACCOUNT_ID": OANDA_ACCOUNT_ID_PROFILE4,
        "COOLDOWN_FILE": "cooldown_profile4.json",
        "RESULTS_DIR": "daily_results_profile4",
        # ── Identity ──
        "MODE": "LEVEL10",
        "BASE_MIN_EDGE": 0.50,
        "DEMO_LOT_SIZE": 5000,  # ✅ Added: Demo half-size
        # ── Weights: S=40 R=15 A=15 X=20 M=10 ──
        "WEIGHT_STRENGTH": 0.40,
        "WEIGHT_RSI": 0.15,
        "WEIGHT_ADX": 0.15,
        "WEIGHT_XGB": 0.20,
        "WEIGHT_MC": 0.10,
        # ── Thresholds (FINAL · 关 WEEKLY EMA100，留 TREND FILTER) ──
        "MIN_CONVICTION_SCORE": 15.0,  # 20.0 → 15.0 · 捞回擦边球
        "MIN_SCORE_GAP": 0.05,  # 0.10 → 0.05 · 低 gap 也能参与共识
        # ── Position Limits ──
        "MAX_OPEN_POSITIONS": 10,
        "MAX_OPEN_PER_RUN": 3,
        "MAX_OPEN_HIGH_VOL": 6,
        "MAX_OPEN_MID_VOL": 2,
        "MAX_OPEN_LOW_VOL": 2,
        # ── JPY 方向共识 ──
        "JPY_CONSENSUS_MIN": 2,
        "JPY_MAX_OPEN_PER_RUN": 2,
        # ── SL caps ──
        "MIN_SL_PIPS": 35,
        "MIN_SL_PIPS_JPY": 60,
        "SL_MAX_ALLOWED_PIPS": 200,
        "SL_MAX_ALLOWED_PIPS_JPY": 500,
        "XGB_BULLISH_THRESHOLD": 0.52,  # 0.55 → 0.52 · 减少 strength/XGB 分裂投票
        "MC_BULLISH_THRESHOLD_PCT": 52.0,  # 55.0 → 52.0 · MC 信号更平衡
        "MC_STRONG_THRESHOLD": 0.55,
        "REQUIRE_DIRECTION_CONSENSUS": True,
        "CONSENSUS_THRESHOLD": 2,
        "CONSENSUS_REQUIRED_VOTES": 2,
        "REQUIRE_STRONG_MOMENTUM": False,
        "ADX_SCALE_FACTOR": 2.0,
        # ── TREND FILTER: 关周 EMA100（最大瓶颈），留 EMA crossover（这版本紧要之处） ──
        "TREND_FILTER_ENABLED": True,
        "WEEK_EMA100_FILTER_ENABLED": False,  # True → False · 🔴 周一亚盘挡了 6+ 单
        "EMA_PERIOD_FAST": 15,  # 40 → 20 · 更敏捷
        "EMA_PERIOD_SLOW": 30,  # 80 → 40 · 减少滞后挡单
        # ── TP/SL multipliers ──
        "TP_MULT": 2.5,
        "TP_STRONG_MULT": 3.0,
        "ATR_SL_MULT": 2.5,
        "ATR_TP_MULT": 3.0,
        # ── Dynamic Exit ──
        "USE_DYNAMIC_SL": 2,
        "DYNAMIC_SL_MULT": 1.5,
        "BE_TRIGGER_ATR_MULT": 1.5,
        "TRAIL_TRIGGER_ATR_MULT": 2.5,
        "TRAIL_ATR_MULT": 1.5,
        "MAX_HOLD_BARS": 12,
        # ── PROFILE4 H4-ESCALE + TP LINK ──
        "USE_H4_ESCALE": True,
        "TP_LINK_SL": True,
        # ── SL Strategy ──
        "SL_USE_ZONE_HIERARCHY": True,
        # ── Pair Selection ──
        "USE_TOP_PAIRS_ONLY": False,
        "TOP_PAIRS_COUNT": 4,
        "TOP_PAIRS_MIN_GAP": 0.25,
        # ── MC ──
        "SKIP_MC": False,
    },
}

# ==========================================
# 🌐 全局共享资源 — 一处定义，多 profile 复用
# ==========================================

# ── D_STRATEGY_GROUPS — Daily 模式专属分组 ──
# 命中条件: instrument 名 == dict key（如 "GBP_AUD"）
# 消费端: DynamicPositionManager.update_all() → instrument_overrides.get(instrument)
# 字段说明（与消费端 key 严格对齐）：
#   bar_hours        — 每根 bar 小时数（日线=24）
#   max_hold         — 超过多少 bar 强制时间退出
#   sl_granularity   — Zone SL 重算时的 OANDA K 线粒度（H4/D/...）
#   confirm_on_close — True 时 SL 更新只在 D1 收盘后触发
D_STRATEGY_GROUPS = {
    # ── D1 Daily · GBP_AUD 高 beta ──
    "GBP_AUD": {
        "bar_hours": 24,
        "max_hold": 12,
        "sl_granularity": "D",
        "confirm_on_close": True,
    },
}

# ── EXCLUDE_CURRENCIES_GLOBAL — 默认要排除的货币代码 ──
EXCLUDE_CURRENCIES_GLOBAL = [
    # "NZD", "CAD", "CHF", "JPY",   # 需要时取消注释
]

# ==========================================
# 🔌 load_profile() — main app 的唯一入口
# ==========================================
# 用法:  P = load_profile("profile3")
#
# 内部做的事：
#   1. 取 PROFILE_CFG[name] 作为模板（深拷贝，不污染原模板）
#   2. merge 模块级全局常量（原来 cfg() 函数的第二层 fallback）
#   3. 注入全局共享资源（D_STRATEGY_GROUPS / EXCLUDE_CURRENCIES_GLOBAL）
#      — 哪些 profile 启用哪些资源，在这里集中声明
#   4. 返回一个完全独立的最终 dict
#
# main app 不需要知道 D_STRATEGY_GROUPS、PROFILE_CFG、cfg() 这些内部细节
def load_profile(profile_name: str) -> dict:
    import copy

    base_dir = Path(__file__).resolve().parent

    # ── Step 1: 取模板 + 深拷贝，不污染原 PROFILE_CFG ──
    template = PROFILE_CFG.get(profile_name, PROFILE_CFG["profile2"])
    final: dict[str, Any] = copy.deepcopy(template)

    # ── Step 2: merge 明确列出的模块级全局常量（禁止 dir() 猜测合并） ──
    for key in _GLOBAL_CONSTANT_KEYS:
        if key in final:
            continue
        final[key] = globals()[key]

    # ── Step 3: 注入全局共享资源（集中声明哪些 profile 启用哪些资源） ──
    if profile_name == "profile3":
        final["INSTRUMENT_OVERRIDES"] = D_STRATEGY_GROUPS
        final["EXCLUDE_CURRENCIES"] = list(EXCLUDE_CURRENCIES_GLOBAL)
    else:
        final["INSTRUMENT_OVERRIDES"] = {}
        final["EXCLUDE_CURRENCIES"] = []

    # ── Step 4: 注入外部客户端/连接（只在 config_bot 触碰 config_oanda） ──
    final["OANDA_API"] = OANDA_API

    # ── Step 5: 统一路径装配（避免各文件重复算 BASE_DIR / 拼路径） ──
    final["BASE_DIR"] = base_dir
    final["PROFILE_NAME"] = profile_name
    final["COOLDOWN_FILE_PATH"] = base_dir / final.get("COOLDOWN_FILE", f"cooldown_{profile_name}.json")
    final["RESULTS_DIR_PATH"] = base_dir / final.get("RESULTS_DIR", f"daily_results_{profile_name}")

    return final


def cfg(P: dict, key: str, default: Any = None) -> Any:
    """
    唯一读取入口：cfg(P, key)
    铁律：业务侧不允许直接 import 常量，不允许直接访问 config 层的模块变量。
    """
    if P is None:
        return default
    return P.get(key, default)