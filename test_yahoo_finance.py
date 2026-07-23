#!/usr/bin/env python3
from config import DATA_SOURCE   # <-- ADD THIS IMPORT
from utils import get_candles, get_latest_price

print("=== TEST UNIFIED DATA PROVIDER ===")
print(f"Active source: {DATA_SOURCE}")

price = get_latest_price("GBP_JPY")
print(f"Latest GBP/JPY: {price:.4f}\n")

candles = get_candles(instrument="GBP_JPY", granularity="H1", count=3)
current_price = get_latest_price("GBP_JPY")

print(f"Fetched {len(candles)} candles:")
for c in candles:
    print(c)
