#!/usr/bin/env python3
"""
Unified data provider — automatically uses source set in config
"""
from config import DATA_SOURCE
from .yahoo_finance import get_candles as get_yahoo_candles, get_latest_price as get_yahoo_price
from .trading_core import get_candles as get_oanda_candles, get_latest_price as get_oanda_price


def get_candles(instrument: str, granularity: str = "D", count: int = 50, start=None, end=None):
    """Get candles using configured source"""
    if DATA_SOURCE == "OANDA_ONLY":
        return get_oanda_candles(instrument, granularity, count, start, end)
    elif DATA_SOURCE == "YAHOO_ONLY":
        return get_yahoo_candles(instrument, granularity, count, start, end)
    elif DATA_SOURCE == "OANDA_WITH_YAHOO_FALLBACK":
        data = get_oanda_candles(instrument, granularity, count, start, end)
        if not data:
            print(f"[INFO] OANDA returned no data for {instrument} {granularity} — falling back to Yahoo")
            data = get_yahoo_candles(instrument, granularity, count, start, end)
        return data
    else:
        raise ValueError(f"Invalid DATA_SOURCE: {DATA_SOURCE}")


def get_latest_price(instrument: str):
    """Get latest price using configured source"""
    if DATA_SOURCE == "OANDA_ONLY":
        return get_oanda_price(instrument)
    elif DATA_SOURCE == "YAHOO_ONLY":
        return get_yahoo_price(instrument)
    elif DATA_SOURCE == "OANDA_WITH_YAHOO_FALLBACK":
        price = get_oanda_price(instrument)
        if price is None:
            print(f"[INFO] OANDA price unavailable for {instrument} — falling back to Yahoo")
            price = get_yahoo_price(instrument)
        return price
    else:
        raise ValueError(f"Invalid DATA_SOURCE: {DATA_SOURCE}")