import os
from oandapyV20 import API
import oandapyV20.endpoints.instruments as instruments
import pandas as pd

# Dynamically respect OANDA_ENV; defaults to 'practice' for safety.
# (Matches the pattern used in custom_strategy.py — do not hardcode "live" here.)
oanda_env = os.getenv("OANDA_ENV", "practice")
oanda_client = API(access_token=os.getenv("OANDA_API_TOKEN"), environment=oanda_env)


def get_gbpjpy_technicals() -> str:
    # 1. Fetch the last 20 4-hour candles for GBP/JPY
    params = {
        "count": 20,
        "granularity": "H4"  # 4-Hour intervals are great for swing/macro signals
    }
    request = instruments.InstrumentsCandles(instrument="GBP_JPY", params=params)

    # Send the request through the authenticated client
    oanda_client.request(request)
    candles = request.response.get('candles', [])

    # 2. Parse into a Pandas DataFrame for technical calculations
    data = []
    for c in candles:
        if c['complete']:
            data.append({
                "time": c['time'],
                "close": float(c['mid']['c']),
                "volume": int(c['volume'])
            })

    df = pd.DataFrame(data)

    if df.empty or len(df) < 5:
        return "Pair: GBP/JPY\nInsufficient candle data to compute technicals."

    # 3. Calculate Simple Technicals (e.g., a simple 5-period moving average)
    df['MA_5'] = df['close'].rolling(window=5).mean()
    latest = df.iloc[-1]

    # 4. Format a concise string summary for downstream analysis
    summary = f"""
    Pair: GBP/JPY
    Current 4H Close: {latest['close']}
    5-Period MA: {latest['MA_5']:.2f}
    Recent Trend: {'Bullish (Above MA)' if latest['close'] > latest['MA_5'] else 'Bearish (Below MA)'}
    Volume: {latest['volume']}
    """
    return summary