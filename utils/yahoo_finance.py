#!/usr/bin/env python3
"""
Yahoo Finance data provider
Returns candles in the EXACT same format as OANDA
"""

import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Dict, Optional


def _oanda_format(candles_df: pd.DataFrame) -> List[Dict]:
    """
    Convert raw Yahoo DataFrame to OANDA candle structure:
    [
        {
            "complete": bool,
            "volume": int,
            "time": "2026-07-10T06:00:00.000000000Z",
            "mid": {
                "o": "string",
                "h": "string",
                "l": "string",
                "c": "string"
            }
        },
        ...
    ]
    """
    formatted = []
    for timestamp, row in candles_df.iterrows():
        time_str = timestamp.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
        formatted.append({
            "complete": True,
            "volume": int(row.get("Volume", 0)),
            "time": time_str,
            "mid": {
                "o": f"{float(row['Open']):.6f}",
                "h": f"{float(row['High']):.6f}",
                "l": f"{float(row['Low']):.6f}",
                "c": f"{float(row['Close']):.6f}"
            }
        })
    return formatted


def _to_yahoo_symbol(oanda_pair: str) -> str:
    """Convert OANDA-style pair (e.g. "GBP_JPY") to Yahoo symbol (e.g. "GBPJPY=X")"""
    return oanda_pair.replace("_", "") + "=X"


def get_candles(
    instrument: str,
    granularity: str = "D",
    count: int = 50,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None
) -> List[Dict]:
    """
    Get candles from Yahoo Finance — matches OANDA API parameters and output format
    """
    interval_map = {
        "M1": "1m",
        "M5": "5m",
        "M15": "15m",
        "M30": "30m",
        "H1": "60m",
        "H2": "60m",
        "H4": "240m",
        "D": "1d",
        "W": "1wk",
        "M": "1mo"
    }

    yahoo_interval = interval_map.get(granularity.upper(), "1d")
    symbol = _to_yahoo_symbol(instrument)

    try:
        if start and end:
            hist = yf.download(
                symbol,
                start=start,
                end=end,
                interval=yahoo_interval,
                progress=False,
                auto_adjust=False
            )
        else:
            period = "60d" if count <= 60 else "1y" if count <= 365 else "max"
            hist = yf.download(
                symbol,
                period=period,
                interval=yahoo_interval,
                progress=False,
                auto_adjust=False
            )
            hist = hist.tail(count)

        if hist.empty:
            return []

        hist = hist[["Open", "High", "Low", "Close", "Volume"]].astype(float)
        return _oanda_format(hist)

    except Exception as e:
        print(f"[YAHOO] Error fetching {instrument} ({granularity}): {str(e)}")
        return []


def get_latest_price(instrument: str) -> Optional[float]:
    """Get latest close price as float"""
    symbol = _to_yahoo_symbol(instrument)
    try:
        data = yf.Ticker(symbol).history(period="1d", interval="1m")
        return None if data.empty else float(data["Close"].iloc[-1])
    except Exception as e:
        print(f"[YAHOO] Error fetching latest price for {instrument}: {str(e)}")
        return None
