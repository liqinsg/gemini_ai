# fx_monte_carlo.py
# updated: strict YAML configuration loading + ticker suffix formatting
"""
FX MONTE CARLO ENGINE — DAILY & WEEKLY
======================================

Usage:
    python fx_monte_carlo.py D
    python fx_monte_carlo.py W
    python fx_monte_carlo.py -d
    python fx_monte_carlo.py -w
"""

import os
import sys
import json
import argparse
import yaml
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

try:
    from utils.mc_loader_local import (
        log_mc_observation,
        resolve_pending_mc_observations,
    )
except ImportError:
    log_mc_observation = None
    resolve_pending_mc_observations = None

try:
    from telegram_message import send_telegram_message
except ImportError:
    send_telegram_message = None


# ==========================================
# 📄 YAML DATA LOADING (STRICT)
# ==========================================

YAML_FILE = BASE_DIR / "mc_data.yml"

if not YAML_FILE.is_file():
    print(f"❌ Configuration file not found: {YAML_FILE}")
    sys.exit(1)

try:
    with open(YAML_FILE, "r", encoding="utf-8") as f:
        mc_config = yaml.safe_load(f)
    if not isinstance(mc_config, dict):
        raise ValueError("YAML root must be a dictionary.")
except Exception as e:
    print(f"❌ Failed to parse {YAML_FILE}: {e}")
    sys.exit(1)

# Extract parameters strictly from YAML
try:
    RAW_PAIRS = mc_config["pairs"]
    PAIRS = [p if p.endswith("=X") else f"{p}=X" for p in RAW_PAIRS]
    SIMULATIONS = int(mc_config["simulations"])
    CONFIDENCE = float(mc_config["confidence"])
    STUDENT_T_DF = int(mc_config["student_t_df"])
    DAILY_LOOKBACK = int(mc_config["daily_lookback"])
    DAILY_FORECAST = int(mc_config["daily_forecast"])
    WEEKLY_LOOKBACK = int(mc_config["weekly_lookback"])
    WEEKLY_FORECAST = int(mc_config["weekly_forecast"])
except KeyError as e:
    print(f"❌ Missing required parameter in {YAML_FILE}: {e}")
    sys.exit(1)
except Exception as e:
    print(f"❌ Invalid value in {YAML_FILE}: {e}")
    sys.exit(1)

MAX_HISTORY_FILES = mc_config.get("max_history_files", 100)


# ==========================================
# ⚙️ ARG PARSE
# ==========================================

parser = argparse.ArgumentParser(
    description="FX Monte Carlo Engine",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog="""Examples:
  python fx_monte_carlo.py D
  python fx_monte_carlo.py W
  python fx_monte_carlo.py -d
  python fx_monte_carlo.py -w
""",
)

tf_group = parser.add_argument_group("Timeframe Selection (Required)")
tf_group.add_argument(
    "timeframe_pos",
    nargs="?",
    choices=["D", "W", "d", "w"],
    help="Timeframe positional argument: D (Daily) or W (Weekly)",
)
tf_group.add_argument(
    "-d",
    "--daily",
    action="store_true",
    help="Execute Daily timeframe",
)
tf_group.add_argument(
    "-w",
    "--weekly",
    action="store_true",
    help="Execute Weekly timeframe",
)

args = parser.parse_args()

# Resolve timeframe input
TF = None
if args.timeframe_pos:
    TF = args.timeframe_pos.upper()
elif args.daily:
    TF = "D"
elif args.weekly:
    TF = "W"

if not TF:
    parser.print_help()
    sys.exit(1)


# ==========================================
# TIMEFRAME & DIRECTORY ROUTING
# ==========================================

TIMEFRAME_CONFIG = {
    "D": {
        "yf_interval": "1d",
        "yf_period": "1y",
        "lookback": DAILY_LOOKBACK,
        "forecast": DAILY_FORECAST,
        "periods_year": 252,
        "results_dir": BASE_DIR / "mc_daily_results",
        "report_title": "FX DAILY MONTE CARLO UPDATE",
    },
    "W": {
        "yf_interval": "1wk",
        "yf_period": "5y",
        "lookback": WEEKLY_LOOKBACK,
        "forecast": WEEKLY_FORECAST,
        "periods_year": 52,
        "results_dir": BASE_DIR / "mc_weekly_results",
        "report_title": "FX WEEKLY MONTE CARLO UPDATE",
    },
}

TF_CFG = TIMEFRAME_CONFIG[TF]
RESULTS_DIR = TF_CFG["results_dir"]
RESULTS_DIR.mkdir(exist_ok=True, parents=True)


# ==========================================
# 🛡️ MARKET STATUS
# ==========================================

def forex_market_closed():
    now = datetime.now(ZoneInfo("Europe/London"))
    wd = now.weekday()
    return (
        wd == 5
        or (wd == 6 and now.hour < 21)
        or (wd == 4 and now.hour >= 21)
    )


if forex_market_closed():
    msg = f"⏸️ FX {TF} MC: Market closed — skipped"
    print(msg)
    if send_telegram_message:
        send_telegram_message(msg)
    raise SystemExit(0)


# ==========================================
# 📥 DATA FETCH
# ==========================================

def fetch_data(pair: str) -> pd.DataFrame:
    interval = TF_CFG["yf_interval"]
    period = TF_CFG["yf_period"]
    lookback = TF_CFG["lookback"]

    try:
        df = yf.download(
            pair,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=False,
        )

        if df.empty:
            return pd.DataFrame()

        if isinstance(df.columns, pd.MultiIndex):
            try:
                df.columns = df.columns.get_level_values(0)
            except Exception:
                pass

        required = ["Open", "High", "Low", "Close"]
        if not all(c in df.columns for c in required):
            return pd.DataFrame()

        df = df[required].dropna()
        if len(df) < lookback:
            return pd.DataFrame()

        return df

    except Exception as e:
        print(f"❌ Data failed {pair} [{TF}]: {e}")
        return pd.DataFrame()


# ==========================================
# 🧠 MONTE CARLO ENGINE
# ==========================================

def run_mc(pair: str):
    lookback = TF_CFG["lookback"]
    forecast = TF_CFG["forecast"]
    periods_year = TF_CFG["periods_year"]

    df = fetch_data(pair)
    if len(df) < lookback:
        return None, False

    closes = df["Close"].values[-lookback:]
    current = float(closes[-1].item())

    if current <= 0:
        return None, False

    log_returns = np.log(closes[1:] / closes[:-1])
    if len(log_returns) < 2:
        return None, False

    drift = float(np.mean(log_returns) * periods_year)
    vol = float(np.std(log_returns) * np.sqrt(periods_year))
    dt = 1 / periods_year

    np.random.seed(42)
    scale_factor = (
        np.sqrt((STUDENT_T_DF - 2) / STUDENT_T_DF)
        if STUDENT_T_DF > 2
        else 1.0
    )

    t_shocks = (
        np.random.standard_t(STUDENT_T_DF, size=(SIMULATIONS, forecast))
        * scale_factor
    )

    step_drift = (drift / periods_year) - 0.5 * (vol**2) / periods_year
    step_diffusion = vol * np.sqrt(dt) * t_shocks
    log_returns_matrix = step_drift + step_diffusion

    paths = np.empty((SIMULATIONS, forecast + 1))
    paths[:, 0] = current
    paths[:, 1:] = current * np.exp(np.cumsum(log_returns_matrix, axis=1))

    final = paths[:, -1]
    lower = float(np.percentile(final, (1 - CONFIDENCE) / 2 * 100))
    upper = float(np.percentile(final, (1 + CONFIDENCE) / 2 * 100))

    pct_changes = (final - current) / current
    var_95 = float(np.percentile(pct_changes, 5))
    tail = pct_changes[pct_changes <= var_95]
    cvar_95 = float(np.mean(tail)) if len(tail) else var_95

    percentile = round((np.sum(final <= current) / SIMULATIONS) * 100, 1)
    p_up = round((np.sum(final > current) / SIMULATIONS) * 100, 1)
    p_down = round(100 - p_up, 1)

    touch_upper = round(
        (np.any(paths >= upper, axis=1).sum() / SIMULATIONS) * 100, 1
    )
    touch_lower = round(
        (np.any(paths <= lower, axis=1).sum() / SIMULATIONS) * 100, 1
    )

    if percentile >= 85 and p_down > 55:
        regime = f"🔴 {TF} OVERBOUGHT | Mean-Reversion Risk"
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
        "var_95": round(var_95, 4),
        "cvar_95": round(cvar_95, 4),
        "expected_price": round(float(np.mean(final)), dec),
        "regime": regime,
        "lookback": lookback,
        "forecast": forecast,
        "simulations": SIMULATIONS,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
    }, True


# ==========================================
# 💾 UNIFIED ATOMIC SAVE
# ==========================================

def save_batch_mc_safely(
    data_payload: dict,
    target_file: Path,
    glob_pattern: str,
    max_files: int = MAX_HISTORY_FILES,
) -> None:
    target_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file = target_file.with_suffix(f".tmp{os.getpid()}")

    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(data_payload, f, indent=2, ensure_ascii=False)

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
# 📤 TELEGRAM REPORT BUILDER
# ==========================================

def build_telegram(results: list) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"📊 **{TF_CFG['report_title']}**",
        f"📅 Generated: {now}",
        f"🔹 Timeframe: {TF} | Lookback: {TF_CFG['lookback']} | Forecast: {TF_CFG['forecast']} | Sims: {SIMULATIONS}",
        "",
    ]
    for r in results:
        lo, hi = r["range_90"]
        lines.extend([
            f"🔹 **{r['pair']}**",
            f"   💵 Last Close: `{r['current_price']}`",
            f"   📊 Percentile: `{r['percentile_rank']}%`",
            f"   🎯 UP: `{r['p_up_pct']}%` | DOWN: `{r['p_down_pct']}%`",
            f"   📏 90% Band: `{lo}` – `{hi}`",
            f"   🔍 Touch: Low `{r['touch_lower_pct']}%` | High `{r['touch_upper_pct']}%`",
            f"   {r['regime']}",
            "",
        ])
    return "\n".join(lines)


def split_telegram_report(report: str, max_length: int = 3500) -> list[str]:
    """Split a report into Telegram-safe chunks without breaking pair blocks."""
    sections = report.split("\n\n")
    chunks = []
    current = ""

    for section in sections:
        if current:
            candidate = f"{current}\n\n{section}"
        else:
            candidate = section
        if current and len(candidate) > max_length:
            chunks.append(current)
            current = section
        else:
            current = candidate

    if current:
        chunks.append(current)

    return chunks


# ==========================================
# 🚀 MAIN RUN
# ==========================================

def main():
    now_dt = datetime.now(timezone.utc)
    now_str = now_dt.strftime("%Y%m%d_%H%M")

    all_results = []
    pairs_results_map = {}
    current_prices = {}

    print(f"🔬 {TF_CFG['report_title']} — {now_str} UTC | Pairs: {len(PAIRS)}")

    for pair in PAIRS:
        print(f"🔄 Processing: {pair} [{TF}]")
        data, ok = run_mc(pair)

        if not ok:
            print(f"⚠️ Skipped {pair} [{TF}]")
            continue

        all_results.append(data)
        pairs_results_map[pair] = data
        current_prices[pair] = data["current_price"]

        if log_mc_observation:
            log_mc_observation(data)

    if not all_results:
        print("⚠️ No valid pair results generated.")
        return

    batch_payload = {
        "metadata": {
            "timeframe": TF,
            "generated_utc": now_dt.isoformat(),
            "total_pairs": len(all_results),
            "simulations": SIMULATIONS,
            "lookback": TF_CFG["lookback"],
            "forecast": TF_CFG["forecast"],
        },
        "results": pairs_results_map,
    }

    unified_filename = f"mc_{TF}_all_pairs_{now_str}.json"
    save_batch_mc_safely(
        batch_payload,
        RESULTS_DIR / unified_filename,
        glob_pattern=f"mc_{TF}_all_pairs_*.json",
    )
    print(f"✅ Saved unified batch → {RESULTS_DIR / unified_filename}")

    if resolve_pending_mc_observations:
        if resolved := resolve_pending_mc_observations(current_prices):
            print(f"📊 Resolved {resolved} pending MC observation(s)")

    if send_telegram_message and all_results:
        report_chunks = split_telegram_report(build_telegram(all_results))
        sent = all(send_telegram_message(chunk) for chunk in report_chunks)
        if sent:
            print(f"✅ Telegram report sent ({len(report_chunks)} message(s))")
        else:
            print("❌ Telegram report failed")

    print(f"✅ Run complete — {len(all_results)} pairs processed.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        err = f"❌ {TF} MC Error: {e}"
        print(err)
        if send_telegram_message:
            send_telegram_message(err)