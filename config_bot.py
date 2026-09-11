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

DEBUG_MODE = False
NO_COOLDOWN = True
DEFAULT_LOT_SIZE = 10000

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
    "DEFAULT_LOT_SIZE",
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
        "DEFAULT_LOT_SIZE": 5000,  # ✅ Added: Demo half-size
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
