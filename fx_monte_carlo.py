# fx_monte_carlo.py
# updated: 2024-08-01 with weekly MC
# MC daily (90 days)
# MC weekly (104 weeks)
"""
FX MONTE CARLO ENGINE — DAILY + WEEKLY
=======================================

Usage:
    python fx_monte_carlo.py

Timeframes:
    D = Daily MC (90 days)
    W = Weekly MC (104 weeks)

Design:
    - D: short-term / tactical probability context
    - W: medium-term regime / directional confirmation
    - READ-ONLY: NOT used directly in execution, sizing, or exit logic
    - No H4 fallback / no timeframe resampling
    - Market-closed skip
    - Atomic JSON output
    - MC observation logging + pending observation resolution
    - Console + JSON output only
"""

import os
import sys
import json
import numpy as np
import pandas as pd
import yfinance as yf

from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path


# ==========================================
# PATH / IMPORTS
# ==========================================

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import config

from utils.mc_loader_local import (
    log_mc_observation,
    resolve_pending_mc_observations,
)


# ==========================================
# CONFIG HELPERS
# ==========================================

def cfg(name, default):
    return getattr(config, name, default)


# ==========================================
# PAIRS
# ==========================================

DEFAULT_PAIRS = [
    "EURUSD=X",
    "GBPUSD=X",
    "AUDUSD=X",
    "USDCHF=X",
    "NZDUSD=X",
    "USDCAD=X",
    "EURGBP=X",

    "USDJPY=X",
    "EURJPY=X",
    "GBPJPY=X",
    "AUDJPY=X",
    "CADJPY=X",
    "CHFJPY=X",
    "NZDJPY=X",

    "GBPAUD=X",
    "EURCHF=X",
]

PAIRS = DEFAULT_PAIRS


# ==========================================
# MC GLOBAL PARAMETERS
# ==========================================

SIMULATIONS = cfg("MC_SIMULATIONS", 5000)
CONFIDENCE = cfg("MC_CONFIDENCE", 0.90)

# Student-t degrees of freedom for fat-tailed shocks
STUDENT_T_DF = cfg("MC_STUDENT_T_DF", 5)

# Keep maximum history files per pair/timeframe
MAX_HISTORY_FILES = cfg("MC_MAX_HISTORY_FILES", 100)


# ==========================================
# TIMEFRAME CONFIGURATION
# ==========================================

# Daily:
#   90 trading days ≈ 4.5 calendar months
#   Forecast = 5 trading days
#
# Weekly:
#   104 weeks ≈ 2 years
#   Forecast = 5 weeks
#
# W is deliberately longer than D because weekly data
# otherwise has too few observations for stable volatility
# and drift estimation.

DAILY_LOOKBACK = cfg("DAILY_LOOKBACK", 90)
DAILY_FORECAST = cfg("DAILY_FORECAST", 5)

WEEKLY_LOOKBACK = cfg("WEEKLY_LOOKBACK", 104)
WEEKLY_FORECAST = cfg("WEEKLY_FORECAST", 5)


TIMEFRAME_CONFIG = {
    "D": {
        "yf_interval": "1d",
        "yf_period": "1y",
        "lookback": DAILY_LOOKBACK,
        "forecast": DAILY_FORECAST,
        "periods_year": 252,
    },
    "W": {
        "yf_interval": "1wk",
        "yf_period": "5y",
        "lookback": WEEKLY_LOOKBACK,
        "forecast": WEEKLY_FORECAST,
        "periods_year": 52,
    },
}


# Run both by default.
# Can be overridden in config.py with:
#
# MC_TIMEFRAMES = ["D"]
# MC_TIMEFRAMES = ["W"]
# MC_TIMEFRAMES = ["D", "W"]

TIMEFRAMES = cfg("MC_TIMEFRAMES", ["D", "W"])


# ==========================================
# OUTPUT
# ==========================================

RESULTS_DIR = BASE_DIR / "daily_results"
RESULTS_DIR.mkdir(exist_ok=True)

REPORT_TITLE = "FX MONTE CARLO UPDATE"


# ==========================================
# 🛡️ MARKET STATUS
# ==========================================

def forex_market_closed():
    """
    FX market schedule check using London timezone.

    Saturday:
        closed all day

    Sunday:
        closed before 21:00 London

    Friday:
        closed from 21:00 London
    """

    now = datetime.now(ZoneInfo("Europe/London"))
    wd = now.weekday()

    return (
        wd == 5
        or (wd == 6 and now.hour < 21)
        or (wd == 4 and now.hour >= 21)
    )


if forex_market_closed():
    msg = "⏸️ FX MC: Market closed — skipped"
    print(msg)
    raise SystemExit(0)


# ==========================================
# 📥 DATA FETCH
# ==========================================

def fetch_data(pair: str, timeframe: str) -> pd.DataFrame:
    """
    Fetch native Yahoo Finance data for the requested timeframe.

    IMPORTANT:
        No H4 fallback.
        No resampling.
        No cross-timeframe substitution.

    If native data is insufficient, return an empty DataFrame.
    """

    if timeframe not in TIMEFRAME_CONFIG:
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    tf = TIMEFRAME_CONFIG[timeframe]

    interval = tf["yf_interval"]
    period = tf["yf_period"]
    lookback = tf["lookback"]

    try:
        df = yf.download(
            pair,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=False,
        )

        if df.empty:
            print(
                f"⚠️ No data {pair} [{timeframe}] "
                f"(interval={interval}, period={period})"
            )
            return pd.DataFrame()

        # yfinance can occasionally return MultiIndex columns.
        if isinstance(df.columns, pd.MultiIndex):
            try:
                df.columns = df.columns.get_level_values(0)
            except Exception:
                pass

        required = ["Open", "High", "Low", "Close"]

        missing = [c for c in required if c not in df.columns]

        if missing:
            print(
                f"⚠️ Missing columns {pair} [{timeframe}]: {missing}"
            )
            return pd.DataFrame()

        df = df[required].dropna()

        if len(df) < lookback:
            print(
                f"⚠️ Insufficient native {timeframe} data for {pair}: "
                f"{len(df)} < {lookback}"
            )
            return pd.DataFrame()

        return df

    except Exception as e:
        print(
            f"❌ Data failed {pair} [{timeframe}]: {e}"
        )
        return pd.DataFrame()


# ==========================================
# 🧠 MONTE CARLO ENGINE
# ==========================================

def run_mc(pair: str, timeframe: str):
    """
    Run Monte Carlo simulation for one pair/timeframe.

    Returns:
        (result_dict, True)
        or
        (None, False)
    """

    if timeframe not in TIMEFRAME_CONFIG:
        return None, False

    tf = TIMEFRAME_CONFIG[timeframe]

    lookback = tf["lookback"]
    forecast = tf["forecast"]
    periods_year = tf["periods_year"]

    df = fetch_data(pair, timeframe)

    if len(df) < lookback:
        return None, False

    closes = df["Close"].values[-lookback:]

    # Extract scalar safely.
    current = float(closes[-1].item())

    # Guard against invalid prices.
    if current <= 0:
        return None, False

    log_returns = np.log(closes[1:] / closes[:-1])

    if len(log_returns) < 2:
        return None, False

    # ======================================
    # DRIFT / VOLATILITY
    # ======================================

    drift = float(
        np.mean(log_returns) * periods_year
    )

    vol = float(
        np.std(log_returns) * np.sqrt(periods_year)
    )

    dt = 1 / periods_year

    # ======================================
    # STUDENT-T SHOCKS
    # ======================================

    # Reproducible simulation.
    np.random.seed(42)

    # Student-t variance:
    # df / (df - 2)
    #
    # Scale back to unit variance so that
    # volatility remains comparable to sigma.

    scale_factor = (
        np.sqrt(
            (STUDENT_T_DF - 2)
            / STUDENT_T_DF
        )
        if STUDENT_T_DF > 2
        else 1.0
    )

    t_shocks = (
        np.random.standard_t(
            STUDENT_T_DF,
            size=(SIMULATIONS, forecast),
        )
        * scale_factor
    )

    # ======================================
    # PATH GENERATION
    # ======================================

    step_drift = (
        drift / periods_year
        - 0.5 * (vol ** 2) / periods_year
    )

    step_diffusion = (
        vol
        * np.sqrt(dt)
        * t_shocks
    )

    log_returns_matrix = (
        step_drift
        + step_diffusion
    )

    paths = np.empty(
        (SIMULATIONS, forecast + 1)
    )

    paths[:, 0] = current

    paths[:, 1:] = (
        current
        * np.exp(
            np.cumsum(
                log_returns_matrix,
                axis=1,
            )
        )
    )

    final = paths[:, -1]

    # ======================================
    # CONFIDENCE RANGE
    # ======================================

    lower = float(
        np.percentile(
            final,
            (1 - CONFIDENCE) / 2 * 100,
        )
    )

    upper = float(
        np.percentile(
            final,
            (1 + CONFIDENCE) / 2 * 100,
        )
    )

    # ======================================
    # RETURN DISTRIBUTION
    # ======================================

    pct_changes = (
        final - current
    ) / current

    var_95 = float(
        np.percentile(
            pct_changes,
            5,
        )
    )

    tail = pct_changes[
        pct_changes <= var_95
    ]

    cvar_95 = float(
        np.mean(tail)
    ) if len(tail) else var_95

    # ======================================
    # PROBABILITIES
    # ======================================

    percentile = round(
        (
            np.sum(final <= current)
            / SIMULATIONS
        ) * 100,
        1,
    )

    p_up = round(
        (
            np.sum(final > current)
            / SIMULATIONS
        ) * 100,
        1,
    )

    p_down = round(
        100 - p_up,
        1,
    )

    # ======================================
    # TOUCH PROBABILITIES
    # ======================================

    touch_upper = round(
        (
            np.any(
                paths >= upper,
                axis=1,
            ).sum()
            / SIMULATIONS
        ) * 100,
        1,
    )

    touch_lower = round(
        (
            np.any(
                paths <= lower,
                axis=1,
            ).sum()
            / SIMULATIONS
        ) * 100,
        1,
    )

    # ======================================
    # REGIME CLASSIFICATION
    # ======================================

    if percentile >= 85 and p_down > 55:
        regime = (
            "🔴 OVERBOUGHT | "
            "Mean-Reversion Risk"
        )

    elif percentile <= 15 and p_up > 55:
        regime = (
            "🟢 OVERSOLD | "
            "Bullish Reversal Chance"
        )

    elif (
        abs(drift) > vol * 0.7
        and max(p_up, p_down) > 60
    ):
        regime = "⚡ STRONG MOMENTUM"

    elif (
        abs(p_up - p_down) < 4
        and abs(drift) < vol * 0.3
    ):
        regime = "⏳ CONSOLIDATION RANGE"

    else:
        regime = "🔹 NEUTRAL"

    # ======================================
    # PRICE DECIMAL
    # ======================================

    dec = 3 if "JPY" in pair else 5

    # ======================================
    # RESULT
    # ======================================

    return {
        "timeframe": timeframe,
        "pair": pair,

        "current_price": round(
            current,
            dec,
        ),

        "ann_drift_pct": round(
            drift * 100,
            2,
        ),

        "ann_vol_pct": round(
            vol * 100,
            2,
        ),

        "range_90": [
            round(lower, dec),
            round(upper, dec),
        ],

        "percentile_rank": percentile,

        "p_up": p_up,
        "p_down": p_down,

        # Backward-compatible fields
        "p_up_pct": p_up,
        "p_down_pct": p_down,

        "touch_upper_pct": touch_upper,
        "touch_lower_pct": touch_lower,

        "var_95": round(
            var_95,
            4,
        ),

        "cvar_95": round(
            cvar_95,
            4,
        ),

        "expected_price": round(
            float(np.mean(final)),
            dec,
        ),

        "regime": regime,

        "lookback": lookback,
        "forecast": forecast,
        "simulations": SIMULATIONS,

        "generated_utc": (
            datetime.now(timezone.utc)
            .isoformat()
        ),
    }, True


# ==========================================
# 💾 ATOMIC SAVE + HISTORY CLEANUP
# ==========================================

def save_mc_result_safely(
    data: dict,
    target_file: Path,
    glob_pattern: str,
    max_files: int = MAX_HISTORY_FILES,
) -> None:
    """
    Atomically write JSON and prune old history.
    """

    target_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temp_file = target_file.with_suffix(
        f".tmp{os.getpid()}"
    )

    with open(
        temp_file,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            data,
            f,
            indent=2,
            ensure_ascii=False,
        )

    temp_file.replace(target_file)

    history_files = sorted(
        target_file.parent.glob(glob_pattern),
        key=os.path.getmtime,
    )

    if len(history_files) > max_files:
        for old_file in history_files[:-max_files]:
            try:
                old_file.unlink()
            except OSError:
                pass


# ==========================================
# 🚀 MAIN RUN
# ==========================================

def main():

    now_str = datetime.now(
        timezone.utc
    ).strftime("%Y%m%d_%H%M")

    all_results = []
    current_prices = {}

    print(
        f"🔬 {REPORT_TITLE} — "
        f"{now_str} UTC"
    )

    print(
        f"📈 Timeframes: {', '.join(TIMEFRAMES)}"
    )

    print(
        f"🔹 Pairs: {len(PAIRS)}"
    )

    for timeframe in TIMEFRAMES:

        if timeframe not in TIMEFRAME_CONFIG:
            print(
                f"⚠️ Unsupported timeframe: "
                f"{timeframe} — skipped"
            )
            continue

        tf = TIMEFRAME_CONFIG[timeframe]

        print("")
        print("=" * 60)
        print(
            f"📊 TIMEFRAME: {timeframe}"
        )
        print(
            f"   Lookback: {tf['lookback']}"
        )
        print(
            f"   Forecast: {tf['forecast']}"
        )
        print(
            f"   Interval: {tf['yf_interval']}"
        )
        print("=" * 60)

        for pair in PAIRS:

            print(
                f"🔄 Processing: "
                f"{pair} [{timeframe}]"
            )

            data, ok = run_mc(
                pair,
                timeframe,
            )

            if not ok:
                print(
                    f"⚠️ Skipped "
                    f"{pair} [{timeframe}]"
                )
                continue

            all_results.append(data)

            current_prices[pair] = (
                data["current_price"]
            )

            safe = (
                pair
                .replace("=X", "")
                .replace("=", "_")
            )

            filename = (
                f"mc_{timeframe}_{safe}_"
                f"{now_str}.json"
            )

            save_mc_result_safely(
                data,
                RESULTS_DIR / filename,
                glob_pattern=(
                    f"mc_{timeframe}_{safe}_*.json"
                ),
            )

            # Observation layer.
            # MC remains READ-ONLY from the
            # trading execution perspective.
            log_mc_observation(data)

            print(
                f"✅ Saved → {filename}"
            )

    # ======================================
    # RESOLVE PENDING OBSERVATIONS
    # ======================================
    if resolved := (resolve_pending_mc_observations(current_prices)):
        print(
            f"📊 Resolved {resolved} "
            f"pending MC observation(s)"
        )

    print("")
    print(
        f"✅ Run complete — "
        f"{len(all_results)} MC results"
    )


# ==========================================
# ENTRY POINT
# ==========================================

if __name__ == "__main__":

    try:
        main()

    except Exception as e:

        err = (
            f"❌ MC Error: {e}"
        )

        print(err)
