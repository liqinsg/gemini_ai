# fx_monte_carlo.py
"""
FX MONTE CARLO ENGINE — DAILY ONLY
✅ Usage:
   python fx_monte_carlo.py
✅ Market‑closed skip
✅ Consistent JSON output for trading bot (READ-ONLY — not used in execution/sizing/exit logic)
✅ Console + JSON output only (no Telegram, no OANDA)
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

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import config

def cfg(name, default):
    return getattr(config, name, default)

DEFAULT_PAIRS = [
    "EURUSD=X", "GBPUSD=X", "AUDUSD=X", "USDCHF=X", "NZDUSD=X", "USDCAD=X", "EURGBP=X",   "USDJPY=X", "EURJPY=X", "GBPJPY=X", "AUDJPY=X", "CADJPY=X", "CHFJPY=X", "NZDJPY=X",
    "GBPAUD=X", "EURCHF=X"
]
PAIRS = DEFAULT_PAIRS
SIMULATIONS = cfg("MC_SIMULATIONS", 5000)
CONFIDENCE = cfg("MC_CONFIDENCE", 0.90)
STUDENT_T_DF = cfg("MC_STUDENT_T_DF", 5)  # degrees of freedom for fat-tailed shocks
MAX_HISTORY_FILES = cfg("MC_MAX_HISTORY_FILES", 100)  # per-pair history retained on disk
RESULTS_DIR = BASE_DIR / "daily_results"
RESULTS_DIR.mkdir(exist_ok=True)

# ——— DAILY-ONLY PARAMS ———
YF_INTERVAL = "1d"
YF_PERIOD_FULL = "120d"
YF_PERIOD_RESAMPLE = "180d"
LOOKBACK = cfg("DAILY_LOOKBACK", 90)
FORECAST = cfg("DAILY_FORECAST", 5)
PERIODS_YEAR = 252
REPORT_TITLE = "FX DAILY MONTE CARLO UPDATE"

# ==========================================
# 🛡️ MARKET STATUS — FAST SCHEDULE EXIT (London TZ, zero API cost)
# ==========================================
def forex_market_closed():
    now = datetime.now(ZoneInfo("Europe/London"))
    wd = now.weekday()
    return (
        wd == 5                      # Saturday all-day
        or (wd == 6 and now.hour < 21)   # Sunday before 21:00 London
        or (wd == 4 and now.hour >= 21)  # Friday after 21:00 London
    )

if forex_market_closed():
    msg = "⏸️ FX Daily MC: Market closed — skipped"
    print(msg)
    raise SystemExit(0)

# ==========================================
# 📥 DATA FETCH — AUTO‑RESAMPLE FALLBACK
# ==========================================
def fetch_data(pair: str) -> pd.DataFrame:
    try:
        df = yf.download(pair, period=YF_PERIOD_FULL, interval=YF_INTERVAL, progress=False)
        if len(df) >= LOOKBACK:
            return df[["Open","High","Low","Close"]].dropna()
    except Exception:
        pass
    try:
        df = yf.download(pair, period=YF_PERIOD_RESAMPLE, interval="4h", progress=False)
        if df.empty:
            return pd.DataFrame()
        return df[["Open","High","Low","Close"]].resample(YF_INTERVAL).agg({
            "Open":"first", "High":"max", "Low":"min", "Close":"last"
        }).dropna()
    except Exception as e:
        print(f"❌ Data failed {pair}: {e}")
        return pd.DataFrame()

# ==========================================
# 🧠 UNIFIED PROBABILITY ENGINE — WARNING FIXED
# ==========================================
def run_mc(pair: str):
    df = fetch_data(pair)
    if len(df) < LOOKBACK:
        return None, False

    closes = df["Close"].values[-LOOKBACK:]
    # ✅ FIXED: extract scalar properly — no deprecation warning
    current = float(closes[-1].item())
    log_returns = np.log(closes[1:] / closes[:-1])

    drift = float(np.mean(log_returns) * PERIODS_YEAR)
    vol = float(np.std(log_returns) * np.sqrt(PERIODS_YEAR))
    dt = 1 / PERIODS_YEAR

    np.random.seed(42)
    # Fat-tailed shocks (Student-t) scaled back to unit variance to match sigma
    scale_factor = np.sqrt((STUDENT_T_DF - 2) / STUDENT_T_DF) if STUDENT_T_DF > 2 else 1.0
    t_shocks = np.random.standard_t(STUDENT_T_DF, size=(SIMULATIONS, FORECAST)) * scale_factor

    step_drift = drift/PERIODS_YEAR - 0.5 * (vol**2)/PERIODS_YEAR
    step_diffusion = (vol * np.sqrt(dt)) * t_shocks
    log_returns_matrix = step_drift + step_diffusion

    paths = np.empty((SIMULATIONS, FORECAST + 1))
    paths[:, 0] = current
    paths[:, 1:] = current * np.exp(np.cumsum(log_returns_matrix, axis=1))

    final = paths[:, -1]
    lower = float(np.percentile(final, (1 - CONFIDENCE)/2 * 100))
    upper = float(np.percentile(final, (1 + CONFIDENCE)/2 * 100))

    pct_changes = (final - current) / current
    var_95 = float(np.percentile(pct_changes, 5))
    cvar_95 = float(np.mean(pct_changes[pct_changes <= var_95]))

    percentile = round((np.sum(final <= current) / SIMULATIONS) * 100, 1)
    p_up = round((np.sum(final > current) / SIMULATIONS) * 100, 1)
    p_down = round(100 - p_up, 1)
    touch_upper = round((np.any(paths >= upper, axis=1).sum() / SIMULATIONS) * 100, 1)
    touch_lower = round((np.any(paths <= lower, axis=1).sum() / SIMULATIONS) * 100, 1)

    if percentile >= 85 and p_down > 55:
        regime = "🔴 OVERBOUGHT | Mean‑Reversion Risk"
    elif percentile <= 15 and p_up > 55:
        regime = "🟢 OVERSOLD | Bullish Reversal Chance"
    elif abs(drift) > vol * 0.7 and max(p_up, p_down) > 60:
        regime = "⚡ STRONG MOMENTUM"
    elif abs(p_up - p_down) < 4 and abs(drift) < vol * 0.3:
        regime = "⏳ CONSOLIDATION RANGE"
    else:
        regime = "🔹 NEUTRAL"

    dec = 3 if "JPY" in pair else 5
    return {
        "timeframe": "D",
        "pair": pair,
        "current_price": round(current, dec),
        "ann_drift_pct": round(drift * 100, 2),
        "ann_vol_pct": round(vol * 100, 2),
        "range_90": [round(lower, dec), round(upper, dec)],
        "percentile_rank": percentile,
        "p_up": p_up,
        "p_down": p_down,
        "p_up_pct": p_up,
        "p_down_pct": p_down,
        "touch_upper_pct": touch_upper,
        "touch_lower_pct": touch_lower,
        "var_95": round(var_95, 4),
        "cvar_95": round(cvar_95, 4),
        "expected_price": round(float(np.mean(final)), dec),
        "regime": regime,
        "lookback": LOOKBACK,
        "forecast": FORECAST,
        "simulations": SIMULATIONS,
        "generated_utc": datetime.now(timezone.utc).isoformat()
    }, True

# ==========================================
# 💾 ATOMIC SAVE + HISTORY CLEANUP
# ==========================================
def save_mc_result_safely(data: dict, target_file: Path, glob_pattern: str, max_files: int = MAX_HISTORY_FILES) -> None:
    """Atomically write JSON (safe under concurrent processes) and prune old history files."""
    target_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file = target_file.with_suffix(f".tmp{os.getpid()}")

    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    temp_file.replace(target_file)  # atomic on POSIX/NTFS — no partial reads for downstream loaders

    history_files = sorted(target_file.parent.glob(glob_pattern), key=os.path.getmtime)
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
    now_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    all_results = []
    print(f"🔬 Daily MC RUN — {now_str} UTC | Pairs: {len(PAIRS)}")
    for pair in PAIRS:
        print(f"🔄 Processing: {pair}")
        data, ok = run_mc(pair)
        if not ok:
            print(f"⚠️ Skipped {pair}")
            continue
        all_results.append(data)
        safe = pair.replace("=X","").replace("=","_")
        filename = f"daily_mc_{safe}_{now_str}.json"
        save_mc_result_safely(data, RESULTS_DIR / filename, glob_pattern=f"daily_mc_{safe}_*.json")
        print(f"✅ Saved → {filename}")
    print("✅ Run complete")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        err = f"❌ Daily MC Error: {e}"
        print(err)