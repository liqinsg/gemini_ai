"""
Read-only local Monte Carlo loader + passive observation logger.

STRICT GUARDRAIL: Everything in this module is READ-ONLY / OBSERVATIONAL.
MC probabilities (p_up/p_down/classification) must never be wired into order
execution, position sizing, stop-loss, or trade-exit logic.
"""
import os
import json
import numpy as np
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

DEFAULT_RESULTS_DIR = Path(__file__).resolve().parent.parent / "daily_results"
OBSERVATIONS_LOG = Path(__file__).resolve().parent.parent / "logs" / "mc_observations.jsonl"
TRADING_DAYS_HORIZON = 5


def get_latest_mc_local(
    pair: str,
    day: bool = True,
    results_dir: Path = DEFAULT_RESULTS_DIR
) -> Optional[Dict[str, Any]]:
    """
    Loads the most recent Monte Carlo JSON result from the local daily_results directory.

    :param pair: Currency pair symbol (e.g., "EURUSD", "EURUSD=X", or "EUR_USD").
    :param day: True for Daily ('daily_mc'). H4 is retired; kept only for call-site compatibility.
    :param results_dir: Path object pointing to the results directory.
    :return: Parsed JSON dictionary or None if no matching file exists.
    """
    if not results_dir.exists():
        print(f"[MC LOCAL LOADER ERROR] Directory does not exist: {results_dir}")
        return None

    # Standardize currency pair name (e.g., "EURUSD=X" -> "EURUSD")
    safe_pair = pair.replace("=X", "").replace("=", "_").replace("_", "")
    tag = "daily" if day else "h4"
    pattern = f"{tag}_mc_{safe_pair}_*.json"

    # Find all matching files
    matching_files = list(results_dir.glob(pattern))

    if not matching_files:
        print(f"[MC LOCAL LOADER] No matching files found for {pair} (day={day}).")
        return None

    # Lexicographical sort on YYYYMMDD_HHMM guarantees the newest file is last
    latest_file = sorted(matching_files, key=lambda p: p.name)[-1]

    try:
        with open(latest_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[MC LOCAL LOADER ERROR] Failed to read {latest_file.name}: {e}")
        return None


def classify_p_up(p_up: Optional[float]) -> str:
    """Read-only observation bucket for p_up — informational labeling only, never a trade trigger."""
    if p_up is None:
        return "UNKNOWN"
    if p_up < 45:
        return "Bearish Bias"
    if p_up <= 55:
        return "Neutral / Noise"
    if p_up <= 60:
        return "Weak Confirmation"
    if p_up <= 65:
        return "Strong Confirmation"
    return "Extreme Deviation"


def log_mc_observation(mc_data: Dict[str, Any], log_path: Path = OBSERVATIONS_LOG) -> None:
    """Append a passive 5-day outcome-tracking record. Write-only; no execution code reads this file."""
    if not mc_data:
        return
    p_up = mc_data.get("p_up")
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pair": mc_data.get("pair"),
        "p_up": p_up,
        "p_down": mc_data.get("p_down"),
        "classification_bucket": classify_p_up(p_up),
        "forecast_start_price": mc_data.get("current_price"),
        "actual_price_5d_later": None,
        "is_actual_up": None,
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def resolve_pending_mc_observations(
    current_prices: Dict[str, Any],
    log_path: Path = OBSERVATIONS_LOG,
    trading_days_horizon: int = TRADING_DAYS_HORIZON,
) -> int:
    """
    Backfills actual_price_5d_later/is_actual_up on records whose horizon has elapsed.
    Purely a bookkeeping pass for later analysis — never consumed by execution logic.
    Returns the number of records resolved.
    """
    if not log_path.exists():
        return 0

    with open(log_path, "r", encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]

    today = datetime.now(timezone.utc).date()
    resolved = 0
    for rec in records:
        if rec.get("actual_price_5d_later") is not None:
            continue
        pair = rec.get("pair")
        if pair not in current_prices:
            continue
        forecast_date = datetime.fromisoformat(rec["timestamp"]).date()
        elapsed_trading_days = np.busday_count(forecast_date, today)
        if elapsed_trading_days < trading_days_horizon:
            continue
        actual_price = current_prices[pair]
        rec["actual_price_5d_later"] = actual_price
        rec["is_actual_up"] = bool(actual_price > rec["forecast_start_price"])
        resolved += 1

    if resolved:
        temp_path = log_path.with_suffix(f".tmp{os.getpid()}")
        with open(temp_path, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        temp_path.replace(log_path)

    return resolved
