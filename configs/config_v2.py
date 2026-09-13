"""
JPY Strength Trading Bot — Business Static Config
==================================================
⚠️  ONLY store constants, paths, preset tables here.
    DO NOT compute, lookup, or merge parameters in this file.
    All rule merging & lookup → config_loader.py
"""

# ==========================================
# EXISTING CONFIG — KEEP ALL YOUR ORIGINAL VALUES UNCHANGED
# ==========================================
# (Keep all your existing constants below:
#  CHECK_INTERVAL_MINUTES, RISK_LEVEL, RISK_PROFILE,
#  MIN_VALID_PAIRS_TO_TRADE, TRADE_PAIRS,
#  MC_REGIME_ENABLED, MC_REGIME_STRENGTH_HURDLE_CONSOLIDATION,
#  POST_EXIT_GATE_ENABLED, POST_EXIT_GATE_SHADOW,
#  ALIGNMENT_THRESHOLD, DYNAMIC_RISK_TIMEFRAME, TP_RATIO, SL_RATIO,
#  MC_MAX_POSITIONS_*, MC_TP_MULTIPLIER_*, MC_EXIT_TIGHTNESS_*,
#  MIN_MARKET_STRENGTH, MIN_DOMINANCE_RATIO, etc.)

# ========== ↓↓↓ ALL YOUR EXISTING LINES REMAIN HERE ↓↓↓ ==========

# ==========================================
# ✅ NEW: Regime Trading Presets — RFC v2.0 (lookup table only, no logic)
# ==========================================
SAFE_ZONE_HALF_WIDTH = 0.38   # Conservative band: center ± coeff × half_range

# Regime → trading parameters table
REGIME_PRESET = {
    "NEUTRAL": {
        "align_min": 2,        # Min aligned timeframes to qualify
        "tp_mult": 1.0,        # TP multiplier
        "sl_mult": 1.0,        # SL multiplier
        "max_count": 1,        # Max open positions allowed
    },
    "CONSOLIDATION": {
        "align_min": 3,
        "tp_mult": 0.8,
        "sl_mult": 1.2,
        "max_count": 1,
    },
    "STRONG_MOMENTUM": {
        "align_min": 2,
        "tp_mult": 1.3,
        "sl_mult": 0.9,
        "max_count": 2,
    },
}

# Override rules when price is OUTSIDE the safe zone
EDGE_OVERRIDE = {
    "align_min": 3,
    "tp_mult": 0.8,
    "sl_mult": 1.0,
    "max_count": 1,
}

# Path to MC result files (JSON)
MC_RESULT_PATH = "../dailymc_results/daily"