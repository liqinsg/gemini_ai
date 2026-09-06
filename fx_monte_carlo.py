# fx_monte_carlo_v1.py
"""
FX MONTE CARLO ENGINE — DAILY + H4 UNIFIED
✅ Usage:
   python fx_monte_carlo.py --timeframe D
   python fx_monte_carlo.py --timeframe H4
✅ Auto‑scales lookback / forecast / drift‑vol per timeframe
✅ Market‑closed skip per timeframe
✅ Consistent JSON output for trading bot
✅ Clean Telegram report
"""
import sys
import json
import argparse
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

import config
from config_oanda import OANDA_API_TOKEN, OANDA_ACCOUNT_ID, OANDA_ENV
from telegram_message import send_telegram_message
import oandapyV20

api = oandapyV20.API(access_token=OANDA_API_TOKEN, environment=OANDA_ENV)

# ==========================================
# ⚙️ ARG PARSE + TIMEFRAME CONFIG
# ==========================================
parser = argparse.ArgumentParser(description="FX Monte Carlo — Daily or H4")
parser.add_argument("--timeframe", choices=["D", "H4"], default="H4", help="Timeframe: D (Daily) / H4 (4‑Hour)")
args = parser.parse_args()
TF = args.timeframe

def cfg(name, default):
    return getattr(config, name, default)

PAIRS = cfg("DEFAULT_PAIRS", [
    "EURUSD=X", "GBPUSD=X", "EURJPY=X", "GBPJPY=X",
    "AUDUSD=X", "USDJPY=X", "GBPAUD=X", "USDCHF=X"
])
SIMULATIONS = cfg("MC_SIMULATIONS", 5000)
CONFIDENCE = cfg("MC_CONFIDENCE", 0.90)
RESULTS_DIR = BASE_DIR / "daily_results"
RESULTS_DIR.mkdir(exist_ok=True)

# ——— TIMEFRAME‑SPECIFIC PARAMS ———
if TF == "H4":
    YF_INTERVAL = "4h"
    YF_PERIOD_FULL = "30d"
    YF_PERIOD_RESAMPLE = "60d"
    LOOKBACK = cfg("H4_LOOKBACK", 90)
    FORECAST = cfg("H4_FORECAST", 8)
    PERIODS_YEAR = 252 * 6
    DT_SCALE = 6
    OANDA_GRANULARITY = "H4"
    REPORT_TITLE = "FX H4 MONTE CARLO UPDATE"
else:
    YF_INTERVAL = "1d"
    YF_PERIOD_FULL = "120d"
    YF_PERIOD_RESAMPLE = "180d"
    LOOKBACK = cfg("DAILY_LOOKBACK", 90)
    FORECAST = cfg("DAILY_FORECAST", 5)
    PERIODS_YEAR = 252
    DT_SCALE = 1
    OANDA_GRANULARITY = "D"
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
    msg = f"⏸️ FX {TF} MC: Market closed — skipped"
    print(msg)
    send_telegram_message(msg)
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
        fallback_interval = "1h" if TF == "H4" else "4h"
        df = yf.download(pair, period=YF_PERIOD_RESAMPLE, interval=fallback_interval, progress=False)
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
    dt = 1 / PERIODS_YEAR * DT_SCALE

    np.random.seed(42)
    paths = np.zeros((SIMULATIONS, FORECAST + 1))
    paths[:, 0] = current
    for t in range(1, FORECAST + 1):
        z = np.random.normal(0, 1, SIMULATIONS)
        paths[:, t] = paths[:, t-1] * np.exp(
            (drift/PERIODS_YEAR - 0.5 * (vol**2)/PERIODS_YEAR) + (vol * np.sqrt(dt)) * z
        )

    final = paths[:, -1]
    lower = float(np.percentile(final, (1 - CONFIDENCE)/2 * 100))
    upper = float(np.percentile(final, (1 + CONFIDENCE)/2 * 100))

    percentile = round((np.sum(final <= current) / SIMULATIONS) * 100, 1)
    p_up = round((np.sum(final > current) / SIMULATIONS) * 100, 1)
    p_down = round(100 - p_up, 1)
    touch_upper = round((np.any(paths >= upper, axis=1).sum() / SIMULATIONS) * 100, 1)
    touch_lower = round((np.any(paths <= lower, axis=1).sum() / SIMULATIONS) * 100, 1)

    if percentile >= 85 and p_down > 55:
        regime = f"🔴 {TF} OVERBOUGHT | Mean‑Reversion Risk"
    elif percentile <= 15 and p_up > 55:
        regime = f"🟢 {TF} OVERSOLD | Bullish Reversal Chance"
    elif abs(drift) > vol * 0.7 and max(p_up, p_down) > 60:
        regime = f"⚡ {TF} STRONG MOMENTUM"
    elif abs(p_up - p_down) < 4 and abs(drift) < vol * 0.3:
        regime = f"⏳ {TF} CONSOLIDATION RANGE"
    else:
        regime = f"🔹 {TF} NEUTRAL"

    dec = 3 if "JPY" in pair else 5
    return {
        "timeframe": TF,
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
        "regime": regime,
        "lookback": LOOKBACK,
        "forecast": FORECAST,
        "simulations": SIMULATIONS,
        "generated_utc": datetime.now(timezone.utc).isoformat()
    }, True

# ==========================================
# 📤 TELEGRAM REPORT
# ==========================================
def build_telegram(results: list) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"📊 **{REPORT_TITLE}**",
        f"📅 Generated: {now}",
        f"🔹 Timeframe: {TF} | Lookback: {LOOKBACK} | Forecast: {FORECAST} | Sims: {SIMULATIONS}", ""
    ]
    for r in results:
        dec = 3 if "JPY" in r["pair"] else 5
        lo, hi = r["range_90"]
        lines.extend([
            f"🔹 **{r['pair']}**",
            f"   💵 Last Close: `{r['current_price']}`",
            f"   📊 Percentile: `{r['percentile_rank']}%`",
            f"   🎯 UP: `{r['p_up_pct']}%` | DOWN: `{r['p_down_pct']}%`",
            f"   📏 90% Band: `{lo}` – `{hi}`",
            f"   🔍 Touch: Low `{r['touch_lower_pct']}%` | High `{r['touch_upper_pct']}%`",
            f"   {r['regime']}", ""
        ])
    return "\n".join(lines)

# ==========================================
# 🚀 MAIN RUN
# ==========================================
def main():
    now_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    all_results = []
    print(f"🔬 {TF} MC RUN — {now_str} UTC | Pairs: {len(PAIRS)}")
    for pair in PAIRS:
        print(f"🔄 Processing: {pair}")
        data, ok = run_mc(pair)
        if not ok:
            print(f"⚠️ Skipped {pair}")
            continue
        all_results.append(data)
        safe = pair.replace("=X","").replace("=","_")
        tag = "daily" if TF == "D" else "h4"
        with open(RESULTS_DIR / f"{tag}_mc_{safe}_{now_str}.json", "w") as f:
            json.dump(data, f, indent=2)
        print(f"✅ Saved → {tag}_mc_{safe}_{now_str}.json")
    if all_results:
        send_telegram_message(build_telegram(all_results))
        print("✅ Telegram report sent")
    print("✅ Run complete")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        err = f"❌ {TF} MC Error: {e}"
        print(err)
        send_telegram_message(err)