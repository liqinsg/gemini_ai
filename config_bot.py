# config_bot.py — UNIFIED · v3 Master File
# All profiles + global constants + load_profile()
"""
Purpose: Strategy & profile parameters ONLY
Connection/OANDA keys → config_oanda.py
This is THE ONLY config file — do NOT maintain config_bot.py separately
"""
from __future__ import annotations
from pathlib import Path
from typing import Any

# ==========================================
# GLOBAL DEFAULTS — shared across ALL profiles
# ==========================================
ALL_PAIRS = [
    "EURUSD=X", "GBPUSD=X", "EURJPY=X", "GBPJPY=X", "AUDUSD=X",
    "USDJPY=X", "GBPAUD=X", "USDCHF=X", "AUDJPY=X", "EURGBP=X",
    "NZDUSD=X", "CADJPY=X",
]
YAHOO_TO_OANDA = {
    "EURUSD=X": "EUR_USD", "GBPUSD=X": "GBP_USD", "EURJPY=X": "EUR_JPY",
    "GBPJPY=X": "GBP_JPY", "AUDUSD=X": "AUD_USD", "USDJPY=X": "USD_JPY",
    "GBPAUD=X": "GBP_AUD", "USDCHF=X": "USD_CHF", "AUDJPY=X": "AUD_JPY",
    "EURGBP=X": "EUR_GBP", "NZDUSD=X": "NZD_USD", "CADJPY=X": "CAD_JPY",
}

# ── PIP Sizes — CRITICAL for import in strategy code ──
PIP_SIZE_BY_QUOTE = {
    "USD": 0.0001, "EUR": 0.0001, "GBP": 0.0001, "AUD": 0.0001,
    "NZD": 0.0001, "CAD": 0.0001, "CHF": 0.0001, "JPY": 0.01,
}

YF_INTERVAL = "4h"
YF_PERIOD_FULL = "30d"
YF_PERIOD_RESAMPLE = "60d"
PERIODS_YEAR = 252
MC_BAND_PCT = 90
MC_MAX_AGE_HOURS = 24
SIMULATIONS = 5000
CONFIDENCE = MC_BAND_PCT / 100.0
ATR_PERIOD = 14
BASE_TP_PIPS = 50
MIN_SL_PIPS = 35
MIN_SL_PIPS_JPY = MIN_SL_PIPS + 10
ENABLE_ATR_MINIMUM_FILTER = True
ATR_MIN_PIPS = 6.0
ATR_MIN_RELATIVE_PCT = 0.045
DEBUG_MODE = False
NO_COOLDOWN = True
DEMO_LOT_SIZE = 10000
LIVE_LOT_SIZE = 1000
USE_MACD = True

# ── MACD Per-Timeframe ──
MACD_TF_PARAMS: dict[str, dict[str, int]] = {
    "H4": {"fast": 12, "slow": 26, "signal": 9},
    "H1": {"fast": 12, "slow": 26, "signal": 9},
    "M30": {"fast": 12, "slow": 26, "signal": 9},
    "M15": {"fast": 12, "slow": 26, "signal": 9},
    "M5": {"fast": 12, "slow": 26, "signal": 9},
}

# ── STRATEGY GROUPS — CRITICAL for runner_v3 ──
STRATEGY_GROUPS = {
    "JPY": {"quote_ccy": "JPY", "tag_prefix": "JPY-STRENGTH"},
    "USD": {"quote_ccy": "USD", "tag_prefix": "USD-STRENGTH"},
    "CHF": {
        "quote_ccy": "CHF", "tag_prefix": "CHF-STRENGTH",
        "MIN_STRENGTH_PASSING_PAIRS": 1,
        "MIN_DOMINANT_PAIRS": 1,
    },
}

# ── Early Exit ──
EARLY_EXIT_MIN_HOLD_MINUTES = 180
EARLY_EXIT_OVERRIDE_MIN_HOLD_MINUTES = 10080
EARLY_EXIT_OVERRIDE_DISABLE_RUNNER_CLOSE = True
EARLY_EXIT_REQUIRE_MA_ALIGNED_NORMAL = 2.4
EARLY_EXIT_REQUIRE_MA_ALIGNED_OVERRIDE = 3.5
EARLY_EXIT_REQUIRE_MACD_AGREE_TF_COUNT = 2
EARLY_EXIT_STRENGTH_REVERSAL_MIN_ABS = 0.5
EARLY_EXIT_STRENGTH_REVERSAL_MIN_RANK_DROP = 2
EARLY_EXIT_ALLOW_AT_LOSS = False
EARLY_EXIT_TF_LIST_MA = ["H4", "H1", "M30"]
EARLY_EXIT_TF_LIST_MACD = ["H4", "H1", "M30"]
EARLY_EXIT_REQUIRE_H4_CONFIRM = True

# ── Dominance / Override ──
DOMINANCE_RATIO_ENABLED = True
DOMINANCE_RATIO_THRESHOLD = 1.3
DOMINANCE_OVERRIDE_ENABLED = True
DOMINANCE_OVERRIDE_THRESHOLD = 1.8
CROSS_MAX_NET_PER_CCY = 2

# ── Shared Resources ──
D_STRATEGY_GROUPS = STRATEGY_GROUPS
EXCLUDE_CURRENCIES_GLOBAL = []

# ==========================================
# ACCOUNT IDs — from config_oanda
# ==========================================
from config_oanda import api as OANDA_API
from config_oanda import (
    OANDA_ACCOUNT_ID_1 as OANDA_ACCOUNT_ID_PROFILE1,
    OANDA_ACCOUNT_ID_2 as OANDA_ACCOUNT_ID_PROFILE2,
    OANDA_ACCOUNT_ID_3 as OANDA_ACCOUNT_ID_PROFILE3,
    OANDA_ACCOUNT_ID_4 as OANDA_ACCOUNT_ID_PROFILE4,
)

# ==========================================
# Explicit merge keys — prevent wild-card dir() merge
# ==========================================
_GLOBAL_CONSTANT_KEYS: tuple[str, ...] = (
    "ALL_PAIRS", "YAHOO_TO_OANDA", "PIP_SIZE_BY_QUOTE",
    "YF_INTERVAL", "YF_PERIOD_FULL", "YF_PERIOD_RESAMPLE",
    "PERIODS_YEAR", "MC_BAND_PCT", "MC_MAX_AGE_HOURS", "SIMULATIONS", "CONFIDENCE",
    "ATR_PERIOD", "BASE_TP_PIPS", "MIN_SL_PIPS", "MIN_SL_PIPS_JPY",
    "DEBUG_MODE", "NO_COOLDOWN", "DEMO_LOT_SIZE", "LIVE_LOT_SIZE",
    "USE_MACD", "MACD_TF_PARAMS",
    "STRATEGY_GROUPS", "D_STRATEGY_GROUPS", "EXCLUDE_CURRENCIES_GLOBAL",
    "ENABLE_ATR_MINIMUM_FILTER", "ATR_MIN_PIPS", "ATR_MIN_RELATIVE_PCT",
    "EARLY_EXIT_MIN_HOLD_MINUTES", "EARLY_EXIT_OVERRIDE_MIN_HOLD_MINUTES",
    "EARLY_EXIT_OVERRIDE_DISABLE_RUNNER_CLOSE",
    "EARLY_EXIT_REQUIRE_MA_ALIGNED_NORMAL", "EARLY_EXIT_REQUIRE_MA_ALIGNED_OVERRIDE",
    "EARLY_EXIT_REQUIRE_MACD_AGREE_TF_COUNT", "EARLY_EXIT_STRENGTH_REVERSAL_MIN_ABS",
    "EARLY_EXIT_STRENGTH_REVERSAL_MIN_RANK_DROP", "EARLY_EXIT_ALLOW_AT_LOSS",
    "EARLY_EXIT_TF_LIST_MA", "EARLY_EXIT_TF_LIST_MACD", "EARLY_EXIT_REQUIRE_H4_CONFIRM",
    "DOMINANCE_RATIO_ENABLED", "DOMINANCE_RATIO_THRESHOLD",
    "DOMINANCE_OVERRIDE_ENABLED", "DOMINANCE_OVERRIDE_THRESHOLD",
    "CROSS_MAX_NET_PER_CCY",
)

# ==========================================
# PROFILES — ALL 4 DEFINED
# ==========================================
PROFILE_CFG = {
    # profile1 — Account001 · JPY LIVE
    "profile1": {
        "LABEL": "PROFILE1",
        "ACCOUNT_NAME": "Account 001",
        "OANDA_ACCOUNT_ID": OANDA_ACCOUNT_ID_PROFILE1,
        "COOLDOWN_FILE": "cooldown_profile1.json",
        "RESULTS_DIR": "daily_results_profile1",
        "MODE": "LEVEL10",
        "BASE_MIN_EDGE": 0.50,
        "quote_ccy": "JPY",
        "max_entries": 2,
        "max_positions": 2,
        "lot_size_live": 1,
        "min_hold_minutes": 180,
        "min_hold_override_minutes": 10080,
        "rr_min": 1.2,
        "atr_min_pips": 6.0,
        "use_macd": True,
        "trade_jpy_only": True,
        "jpy_weighted_pass_threshold": 0.8,
        "jpy_weighted_strong_min_count": 2,
        "jpy_weighted_ccy_min_abs_score": 0.1,
        "cross_max_net_per_ccy": 2,
        "override_max_positions": 3,
        "MAX_OPEN_POSITIONS": 2,
        "MAX_OPEN_PER_RUN": 1,
        "TREND_FILTER_ENABLED": False,
        "WEEK_EMA100_FILTER_ENABLED": False,
        "TP_MULT": 2.0,
        "ATR_SL_MULT": 2.0,
        "ATR_TP_MULT": 2.5,
        "BE_TRIGGER_ATR_MULT": 2.5,
        "TRAIL_TRIGGER_ATR_MULT": 3.5,
        "TRAIL_ATR_MULT": 2.8,
        "MAX_HOLD_BARS": 24,
        "WEIGHT_STRENGTH": 0.35,
        "WEIGHT_RSI": 0.20,
        "WEIGHT_ADX": 0.15,
        "WEIGHT_XGB": 0.20,
        "WEIGHT_MC": 0.10,
        "MIN_CONVICTION_SCORE": 30.0,
        "MIN_SCORE_GAP": 0.10,
        "XGB_BULLISH_THRESHOLD": 0.52,
        "MC_BULLISH_THRESHOLD_PCT": 52.0,
        "MC_STRONG_THRESHOLD": 0.60,
        "REQUIRE_DIRECTION_CONSENSUS": True,
        "CONSENSUS_THRESHOLD": 2,
        "CONSENSUS_REQUIRED_VOTES": 2,
        "EMA_PERIOD_FAST": 20,
        "EMA_PERIOD_SLOW": 40,
        "TP_STRONG_MULT": 2.5,
        "USE_DYNAMIC_SL": 2,
        "DYNAMIC_SL_MULT": 1.5,
        "SL_USE_ZONE_HIERARCHY": True,
        "USE_TOP_PAIRS_ONLY": False,
        "TOP_PAIRS_COUNT": 4,
        "TOP_PAIRS_MIN_GAP": 0.25,
        "SKIP_MC": False,
    },

    # profile2 — Account002 · Standard
    "profile2": {
        "LABEL": "PROFILE2",
        "ACCOUNT_NAME": "Account 002",
        "OANDA_ACCOUNT_ID": OANDA_ACCOUNT_ID_PROFILE2,
        "COOLDOWN_FILE": "cooldown_profile2.json",
        "RESULTS_DIR": "daily_results_profile2",
        "MODE": "LEVEL10",
        "BASE_MIN_EDGE": 0.50,
        "WEIGHT_STRENGTH": 0.35,
        "WEIGHT_RSI": 0.20,
        "WEIGHT_ADX": 0.15,
        "WEIGHT_XGB": 0.20,
        "WEIGHT_MC": 0.10,
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
        "TREND_FILTER_ENABLED": False,
        "WEEK_EMA100_FILTER_ENABLED": False,
        "EMA_PERIOD_FAST": 20,
        "EMA_PERIOD_SLOW": 40,
        "TP_MULT": 2.0,
        "TP_STRONG_MULT": 2.5,
        "ATR_SL_MULT": 2.0,
        "ATR_TP_MULT": 2.5,
        "USE_DYNAMIC_SL": 2,
        "DYNAMIC_SL_MULT": 1.5,
        "BE_TRIGGER_ATR_MULT": 2.5,
        "TRAIL_TRIGGER_ATR_MULT": 3.5,
        "TRAIL_ATR_MULT": 2.8,
        "MAX_HOLD_BARS": 24,
        "SL_USE_ZONE_HIERARCHY": True,
        "USE_TOP_PAIRS_ONLY": False,
        "TOP_PAIRS_COUNT": 4,
        "TOP_PAIRS_MIN_GAP": 0.25,
        "SKIP_MC": False,
    },

    # profile3 — Account003 · Conservative
    "profile3": {
        "LABEL": "PROFILE3",
        "ACCOUNT_NAME": "Account 003",
        "OANDA_ACCOUNT_ID": OANDA_ACCOUNT_ID_PROFILE3,
        "COOLDOWN_FILE": "cooldown_profile3.json",
        "RESULTS_DIR": "daily_results_profile3",
        "MODE": "LEVEL10",
        "BASE_MIN_EDGE": 0.50,
        "ENABLE_ATR_MINIMUM_FILTER": True,
        "ATR_MIN_PIPS": 6.0,
        "ATR_MIN_RELATIVE_PCT": 0.045,
        "WEIGHT_STRENGTH": 0.40,
        "WEIGHT_RSI": 0.15,
        "WEIGHT_ADX": 0.15,
        "WEIGHT_XGB": 0.20,
        "WEIGHT_MC": 0.10,
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
        "TREND_FILTER_ENABLED": True,
        "WEEK_EMA100_FILTER_ENABLED": True,
        "EMA_PERIOD_FAST": 40,
        "EMA_PERIOD_SLOW": 80,
        "TP_MULT": 2.5,
        "TP_STRONG_MULT": 3.0,
        "ATR_SL_MULT": 2.5,
        "ATR_TP_MULT": 3.0,
        "USE_DYNAMIC_SL": 2,
        "DYNAMIC_SL_MULT": 1.5,
        "BE_TRIGGER_ATR_MULT": 1.5,
        "TRAIL_TRIGGER_ATR_MULT": 2.5,
        "TRAIL_ATR_MULT": 1.5,
        "MAX_HOLD_BARS": 12,
        "SL_USE_ZONE_HIERARCHY": True,
        "USE_TOP_PAIRS_ONLY": False,
        "TOP_PAIRS_COUNT": 4,
        "TOP_PAIRS_MIN_GAP": 0.25,
        "SKIP_MC": False,
        "SL_ZONE_TRAILING": True,
    },

    # profile4 — Account004 · DEMO Aggressive
    "profile4": {
        "LABEL": "PROFILE4",
        "ACCOUNT_NAME": "Account 004",
        "OANDA_ACCOUNT_ID": OANDA_ACCOUNT_ID_PROFILE4,
        "COOLDOWN_FILE": "cooldown_profile4.json",
        "RESULTS_DIR": "daily_results_profile4",
        "MODE": "LEVEL10",
        "BASE_MIN_EDGE": 0.50,
        "DEMO_LOT_SIZE": 5000,
        "WEIGHT_STRENGTH": 0.40,
        "WEIGHT_RSI": 0.15,
        "WEIGHT_ADX": 0.15,
        "WEIGHT_XGB": 0.20,
        "WEIGHT_MC": 0.10,
        "MIN_CONVICTION_SCORE": 15.0,
        "MIN_SCORE_GAP": 0.05,
        "MAX_OPEN_POSITIONS": 10,
        "MAX_OPEN_PER_RUN": 3,
        "XGB_BULLISH_THRESHOLD": 0.52,
        "MC_BULLISH_THRESHOLD_PCT": 52.0,
        "MC_STRONG_THRESHOLD": 0.55,
        "REQUIRE_DIRECTION_CONSENSUS": True,
        "CONSENSUS_THRESHOLD": 2,
        "CONSENSUS_REQUIRED_VOTES": 2,
        "TREND_FILTER_ENABLED": True,
        "WEEK_EMA100_FILTER_ENABLED": False,
        "EMA_PERIOD_FAST": 15,
        "EMA_PERIOD_SLOW": 30,
        "TP_MULT": 2.5,
        "TP_STRONG_MULT": 3.0,
        "ATR_SL_MULT": 2.5,
        "ATR_TP_MULT": 3.0,
        "USE_DYNAMIC_SL": 2,
        "DYNAMIC_SL_MULT": 1.5,
        "BE_TRIGGER_ATR_MULT": 1.5,
        "TRAIL_TRIGGER_ATR_MULT": 2.5,
        "TRAIL_ATR_MULT": 1.5,
        "MAX_HOLD_BARS": 12,
        "SL_USE_ZONE_HIERARCHY": True,
        "USE_TOP_PAIRS_ONLY": False,
        "TOP_PAIRS_COUNT": 4,
        "TOP_PAIRS_MIN_GAP": 0.25,
        "SKIP_MC": False,
    },
}

# ==========================================
# load_profile() — ENTRY POINT
# ==========================================
def load_profile(profile_name: str) -> dict:
    import copy
    base_dir = Path(__file__).resolve().parent

    # ✅ Robust fallback — never KeyError
    template = PROFILE_CFG.get(profile_name)
    if template is None:
        for fb in ["profile2", "profile1", "profile3", "profile4"]:
            if fb in PROFILE_CFG:
                template = PROFILE_CFG[fb]
                print(f"[CONFIG] WARN: '{profile_name}' not found → fallback to '{fb}'")
                break
        else:
            raise RuntimeError(f"No valid profile — requested: {profile_name}")

    final: dict[str, Any] = copy.deepcopy(template)

    # Merge global constants
    for key in _GLOBAL_CONSTANT_KEYS:
        if key not in final and key in globals():
            final[key] = globals()[key]

    # Attach shared resources
    final["INSTRUMENT_OVERRIDES"] = D_STRATEGY_GROUPS
    final["EXCLUDE_CURRENCIES"] = list(EXCLUDE_CURRENCIES_GLOBAL)
    final["OANDA_API"] = OANDA_API
    final["BASE_DIR"] = base_dir
    final["PROFILE_NAME"] = profile_name
    final["COOLDOWN_FILE_PATH"] = base_dir / final.get("COOLDOWN_FILE", f"cooldown_{profile_name}.json")
    final["RESULTS_DIR_PATH"] = base_dir / final.get("RESULTS_DIR", f"daily_results_{profile_name}")
    return final

def cfg(P: dict, key: str, default: Any = None) -> Any:
    return P.get(key, default) if P else default