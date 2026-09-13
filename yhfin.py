import argparse
import json
import math
import sys

import numpy as np
from utils import *
# from indicator_provider import summarize_indicator_data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch symbol data and compute local indicators.")
    parser.add_argument("symbol", nargs="?", default="SPY", help="Ticker symbol to fetch (default SPY)")
    parser.add_argument("--period", default="60d", help="History lookback period for yfinance")
    parser.add_argument("--interval", default="1d", help="Data interval for yfinance")
    return parser.parse_args()


def _sanitize(obj):
    """Recursively replace NaN/Infinity with None and coerce numpy scalars to native types."""
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if hasattr(obj, "isoformat"):  # pandas.Timestamp, datetime
        return obj.isoformat()
    return obj


if __name__ == "__main__":
    args = parse_args()
    try:
        result = summarize_indicator_data(args.symbol, period=args.period, interval=args.interval)
        clean = _sanitize(result)
        print(json.dumps(clean, allow_nan=False))
    except Exception as e:
        print(json.dumps({"error": str(e), "symbol": args.symbol}))
        sys.exit(1)