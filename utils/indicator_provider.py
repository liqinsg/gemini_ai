import pandas as pd
import yfinance as yf

OANDA_TO_YAHOO = {
    "EUR_USD": "EURUSD=X",
    "GBP_USD": "GBPUSD=X",
    "AUD_USD": "AUDUSD=X",
    "NZD_USD": "NZDUSD=X",
    "USD_JPY": "USDJPY=X",
    "USD_CAD": "USDCAD=X",
    "USD_CHF": "USDCHF=X",
    "EUR_JPY": "EURJPY=X",
    "GBP_JPY": "GBPJPY=X",
    "AUD_JPY": "AUDJPY=X",
    "NZD_JPY": "NZDJPY=X",
    "CAD_JPY": "CADJPY=X",
    "CHF_JPY": "CHFJPY=X",
    "EUR_GBP": "EURGBP=X",
    "EUR_AUD": "EURAUD=X",
    "EUR_CAD": "EURCAD=X",
    "EUR_CHF": "EURCHF=X",
    "GBP_AUD": "GBPAUD=X",
    "GBP_CAD": "GBPCAD=X",
    "AUD_CAD": "AUDCAD=X",
    "AUD_CHF": "AUDCHF=X",
    "NZD_CAD": "NZDCAD=X",
}

INTERVAL_MAP = {
    "M15": "15m",
    "H1": "1h",
    "H4": "4h",
    "H8": "4h",
}


def translate_pair_to_yahoo(symbol: str) -> str | None:
    key = symbol.strip().upper()
    if key in OANDA_TO_YAHOO:
        return OANDA_TO_YAHOO[key]
    return key


def oanda_granularity_to_yf_interval(granularity: str) -> str:
    return INTERVAL_MAP.get(granularity.upper(), "1h")


def safe_column(df: pd.DataFrame, col_name: str) -> pd.Series:
    if isinstance(df.columns, pd.MultiIndex):
        matches = [c for c in df.columns if c[0] == col_name]
        if matches:
            return df[matches[0]]
    if col_name in df.columns:
        return df[col_name]
    raise KeyError(f"Column '{col_name}' not found in data")


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def compute_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period, min_periods=period).mean()


def compute_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "histogram": histogram})


def compute_indicators(df: pd.DataFrame) -> dict:
    close = safe_column(df, "Close")
    high = safe_column(df, "High")
    low = safe_column(df, "Low")

    indicators = {
        "sma5": close.rolling(window=5, min_periods=5).mean(),
        "sma10": close.rolling(window=10, min_periods=10).mean(),
        "ema10": close.ewm(span=10, adjust=False).mean(),
        "ema20": close.ewm(span=20, adjust=False).mean(),
        "rsi14": compute_rsi(close, 14),
        "atr14": compute_atr(high, low, close, 14),
    }
    indicators.update(compute_macd(close))
    return indicators


def fetch_price_data(symbol: str, period: str = "60d", interval: str = "1h") -> pd.DataFrame:
    yahoo_symbol = translate_pair_to_yahoo(symbol)
    if yahoo_symbol is None:
        raise ValueError(f"Unable to translate symbol to Yahoo Finance ticker: {symbol}")

    df = yf.download(yahoo_symbol, period=period, interval=interval, progress=False)
    if df.empty:
        raise RuntimeError(f"No market data returned for {symbol} ({yahoo_symbol})")
    return df


def summarize_indicator_data(symbol: str, period: str = "60d", interval: str = "1h") -> dict:
    df = fetch_price_data(symbol, period=period, interval=interval)
    indicators = compute_indicators(df)

    close_ser = safe_column(df, "Close")
    high_ser = safe_column(df, "High")
    low_ser = safe_column(df, "Low")
    latest = df.iloc[-1]

    return {
        "symbol": symbol,
        "interval": interval,
        "period": period,
        "current_close": float(round(latest[close_ser.name], 5)),
        "current_high": float(round(latest[high_ser.name], 5)),
        "current_low": float(round(latest[low_ser.name], 5)),
        "sma5": float(round(indicators["sma5"].iloc[-1], 5)),
        "sma10": float(round(indicators["sma10"].iloc[-1], 5)),
        "ema10": float(round(indicators["ema10"].iloc[-1], 5)),
        "ema20": float(round(indicators["ema20"].iloc[-1], 5)),
        "rsi14": float(round(indicators["rsi14"].iloc[-1], 2)),
        "atr14": float(round(indicators["atr14"].iloc[-1], 5)),
        "macd": float(round(indicators["macd"].iloc[-1], 5)),
        "macd_signal": float(round(indicators["signal"].iloc[-1], 5)),
        "macd_histogram": float(round(indicators["histogram"].iloc[-1], 5)),
        "range_high": float(round(high_ser.max(), 5)),
        "range_low": float(round(low_ser.min(), 5)),
        "previous_high": float(round(high_ser.iloc[-2], 5)),
        "previous_low": float(round(low_ser.iloc[-2], 5)),
    }
