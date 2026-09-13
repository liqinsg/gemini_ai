# ml_forex_strategy.py
import warnings
warnings.simplefilter("ignore", FutureWarning)
import importlib
import pandas as pd
import numpy as np
import yfinance as yf
import talib
from sklearn.svm import SVC
from sklearn.calibration import CalibratedClassifierCV
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.model_selection import TimeSeriesSplit, GridSearchCV
from sklearn.base import BaseEstimator, TransformerMixin
from datetime import datetime
import sys
import os
import joblib

# --------------------------
# 🔧 FORCE DEMO / PRACTICE MODE ONLY
# --------------------------
PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from config import OANDA_ENV
if OANDA_ENV.lower() != "practice":
    print("="*70)
    print("❌ ABORTED: REAL ACCOUNT DETECTED!")
    print("⚠️  This script is locked to DEMO / PRACTICE account only.")
    print("👉 Set DEMO_MODE=True and OANDA_ENV='practice' in config.py first.")
    print("="*70)
    sys.exit(1)
print("✅ ✅ ✅ RUNNING IN DEMO / PRACTICE MODE — NO REAL FUNDS AT RISK ✅ ✅ ✅")

# Import your existing modules
from utils.oanda_execution import open_oanda_order, close_all_trades, api
from telegram_message import send_telegram_message


# ----------------------
# ⚙️ CONFIG SETTINGS
# ----------------------
RUN_BACKTEST = True
PLOT_EQUITY_CURVE = False
PLOT_PRICE_WITH_SIGNALS = False

# Risk Management
RISK_PER_TRADE_PCT = 1.0
REWARD_RATIO = 1.5
SL_MULTIPLIER = 1.0
TP_MULTIPLIER = 1.5
SIGNAL_THRESHOLD = 0.55
DEFAULT_TRADE_UNITS = 1000  # Small safe demo units

# Trading Settings
FOREX_PAIR = "USDJPY=X"
START_DATE = "2020-01-01"
END_DATE = datetime.now().strftime("%Y-%m-%d")
INITIAL_CAPITAL = 10000

# Log file
LOG_FILE = os.path.join(os.path.dirname(__file__), "forex_signals.log")


# ----------------------
# 1. Feature Engineering
# ----------------------
class FeatureEngineer(BaseEstimator, TransformerMixin):
    def __init__(self, timeperiod=14):
        self.timeperiod = timeperiod

    def fit(self, X, y=None):
        return self

    def transform(self, df):
        df_features = df.copy()
        close = np.asarray(df_features['Close']).ravel()
        high = np.asarray(df_features['High']).ravel()
        low = np.asarray(df_features['Low']).ravel()
        volume = np.asarray(df_features['Volume']).ravel()

        df_features['MA20'] = talib.SMA(close, timeperiod=20)
        df_features['ATR'] = talib.ATR(high, low, close, timeperiod=14)
        df_features['RSI'] = talib.RSI(close, timeperiod=self.timeperiod)
        df_features['CCI'] = talib.CCI(high, low, close, timeperiod=20)

        df_features['PriceChange'] = np.append(np.nan, np.diff(close) / close[:-1])
        df_features['VolChange'] = np.append(np.nan, np.diff(volume) / volume[:-1])

        for lag in [1, 2, 3]:
            df_features[f'RSI_lag{lag}'] = df_features['RSI'].shift(lag)
            df_features[f'VolChange_lag{lag}'] = df_features['VolChange'].shift(lag)

        feature_cols = [
            'MA20', 'ATR', 'RSI', 'CCI', 'PriceChange', 'VolChange',
            'RSI_lag1', 'RSI_lag2', 'RSI_lag3',
            'VolChange_lag1', 'VolChange_lag2', 'VolChange_lag3'
        ]
        return df_features[feature_cols]


# ----------------------
# 2. Data Preparation
# ----------------------
def prepare_forex_dataset(ticker, start, end):
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Fetching data for {ticker}...")
    try:
        df = yf.download(ticker, start=start, end=end, progress=False)
        if df.empty:
            print("❌ No data retrieved.")
            return None, None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[['Open', 'High', 'Low', 'Close', 'Volume']].copy()
        df.sort_index(inplace=True)
        df['Volume'] = df['Volume'].replace(0, 1e-6)
    except Exception as e:
        print(f"❌ Error fetching data: {e}")
        return None, None

    close = np.asarray(df['Close']).ravel()
    future_return = pd.Series(close).pct_change(3).shift(-3).values
    df['future_return'] = future_return
    df['label'] = np.where(future_return > 0, 1, 0)

    df_clean = df.dropna(subset=['future_return', 'label']).copy()
    if df_clean.empty:
        print("❌ No valid rows after cleaning.")
        return None, None

    y = df_clean['label'].astype(int)
    X_raw = df_clean.drop(columns=['label', 'future_return'])
    print(f"✅ Cleaned shapes: X={X_raw.shape}, y={y.shape}")
    return X_raw, y


# ----------------------
# 3. Risk & Signal Functions
# ----------------------
def generate_signals(model, X_features, threshold=0.55):
    proba = model.predict_proba(X_features)[:, 1]
    signal = np.zeros(len(proba))
    signal[proba > threshold] = 1
    signal[proba < (1 - threshold)] = -1
    return pd.Series(signal, index=X_features.index, name='signal')


def calculate_risk_levels(entry_price, atr, direction="long"):
    sl_distance = atr * SL_MULTIPLIER
    tp_distance = sl_distance * REWARD_RATIO
    if direction.lower() == "long":
        return {
            "entry": round(entry_price, 4),
            "stop_loss": round(entry_price - sl_distance, 4),
            "take_profit": round(entry_price + tp_distance, 4),
            "risk_pips": round(sl_distance * 100, 2)
        }
    else:
        return {
            "entry": round(entry_price, 4),
            "stop_loss": round(entry_price + sl_distance, 4),
            "take_profit": round(entry_price - tp_distance, 4),
            "risk_pips": round(sl_distance * 100, 2)
        }


def calculate_position_size(capital, risk_pct, entry, sl):
    risk_amt = capital * (risk_pct / 100)
    risk_per_unit = abs(entry - sl)
    return round(risk_amt / risk_per_unit, 2) if risk_per_unit != 0 else 0


def get_live_trade_plan(ticker, model, fe, capital=10000, threshold=0.55):
    try:
        data = yf.download(ticker, period="60d", interval="1d", progress=False)
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)
        data = data[["Open", "High", "Low", "Close", "Volume"]].copy()
        data["Volume"] = data["Volume"].replace(0, 1e-6)

        features = fe.transform(data).dropna()
        if features.empty:
            return "❌ *Not enough data to generate trade plan*"

        latest = features.tail(1)
        prob_up = model.predict_proba(latest)[0, 1]
        entry = float(data["Close"].iloc[-1])
        atr = float(latest["ATR"].iloc[0])

        if prob_up > threshold:
            risk = calculate_risk_levels(entry, atr, "long")
            dir_text = "📈 *BUY / LONG*"
            direction = "BUY"
        elif prob_up < (1 - threshold):
            risk = calculate_risk_levels(entry, atr, "short")
            dir_text = "📉 *SELL / SHORT*"
            direction = "SELL"
        else:
            return f"""
📊 *{ticker} TRADE PLAN*
Date: {datetime.now().strftime('%Y-%m-%d')}
Probability Up: {prob_up:.1%}
Action: ⏸️ *HOLD / NO TRADE*
Reason: Probability within neutral range
"""

        size = calculate_position_size(capital, RISK_PER_TRADE_PCT, risk["entry"], risk["stop_loss"])
        return f"""
📊 *{ticker} TRADE PLAN*
Date: {datetime.now().strftime('%Y-%m-%d')}
--------------------------------
Probability Up: {prob_up:.1%}
Direction: {dir_text}
Entry Price: `{risk['entry']}`
Stop-Loss: `{risk['stop_loss']}` ({risk['risk_pips']} pips risk)
Take-Profit: `{risk['take_profit']}`
Risk/Reward Ratio: 1 : {REWARD_RATIO}
Capital: `${capital:,.2f}`
Risk per Trade: {RISK_PER_TRADE_PCT}% = `${(capital * RISK_PER_TRADE_PCT / 100):.2f}`
Position Size: {size} units
Potential Profit: `${(size * abs(risk['take_profit'] - risk['entry'])):.2f}`
""", direction, risk

    except Exception as e:
        return f"❌ *Error generating trade plan:* {str(e)}", None, None


# ----------------------
# 4. Backtest Function
# ----------------------
def backtest(df_price, signals, initial_capital=10000, fee_pct=0.0002):
    data = df_price.copy()
    data['signal'] = signals
    data['position'] = data['signal'].shift(1).fillna(0)
    data['returns'] = data['Close'].pct_change().fillna(0)
    data['trade'] = data['position'].diff().abs()
    data['net_returns'] = data['position'] * data['returns'] - data['trade'] * fee_pct
    data['equity'] = initial_capital * (1 + data['net_returns']).cumprod()
    data.loc[data.index[0], 'equity'] = initial_capital

    total_return = (data['equity'].iloc[-1] / initial_capital) - 1
    buy_hold_return = (data['Close'].iloc[-1] / data['Close'].iloc[0]) - 1
    drawdown = (data['equity'] / data['equity'].cummax()) - 1
    max_drawdown = drawdown.min()
    num_trades = int(data['trade'].sum())
    win_rate = (data['net_returns'][data['net_returns'] != 0] > 0).mean() if num_trades > 0 else 0

    return {
        'Initial Capital': f"${initial_capital:,.2f}",
        'Final Equity': f"${data['equity'].iloc[-1]:,.2f}",
        'Strategy Return': f"{total_return:.2%}",
        'Buy & Hold': f"{buy_hold_return:.2%}",
        'Max Drawdown': f"{max_drawdown:.2%}",
        'Total Trades': num_trades,
        'Win Rate': f"{win_rate:.2%}"
    }


# ----------------------
# 5. Execute DEMO Order + Notify
# ----------------------
def execute_and_notify_demo_trade(direction: str, risk: dict):
    """Execute order on DEMO account using your proven oanda_execution.py"""
    pair = "USD_JPY"  # Matches OANDA format

    # Build signal dict exactly as open_oanda_order expects
    signal = {
        "pair": pair,
        "action": direction,
        "stop_loss": risk["stop_loss"],
        "take_profit": risk["take_profit"]
    }

    print(f"\n🚀 EXECUTING DEMO {direction} ORDER FOR {pair}...")
    print(f"   Entry: {risk['entry']} | SL: {risk['stop_loss']} | TP: {risk['take_profit']}")

    # Send order (removed invalid set_sl/set_tp params)
    result = open_oanda_order(signal, units=DEFAULT_TRADE_UNITS)

    # Send Telegram alert
    if result["status"] == "SUCCESS":
        alert = f"""
✅ *DEMO TRADE EXECUTED*
🔹 Account: PRACTICE / DEMO
🔹 Pair: {pair}
🔹 Direction: {direction}
🔹 Entry: `{result.get('filled_price', risk['entry'])}`
🔹 SL: `{risk['stop_loss']}`
🔹 TP: `{risk['take_profit']}`
🔹 Units: {DEFAULT_TRADE_UNITS}
🔹 Order ID: `{result.get('order_id', 'N/A')}`
🔹 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
    else:
        alert = f"""
❌ *DEMO TRADE FAILED*
🔹 Pair: {pair}
🔹 Direction: {direction}
🔹 Error: {result.get('message', 'Unknown error')}
"""
    print(alert)
    send_telegram_message(alert)
    return result


# ----------------------
# 6. Main Run
# ----------------------
def run_strategy():
    print("\n=========================================")
    print(f"⏰ Running Forex Strategy @ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=========================================")

    X_raw, y = prepare_forex_dataset(FOREX_PAIR, START_DATE, END_DATE)
    if X_raw is None or y is None or X_raw.empty or y.empty:
        msg = "❌ Strategy failed: Could not load valid data"
        print(msg)
        send_telegram_message(msg)
        return

    # Tune model with CalibratedClassifierCV
    best_score = -np.inf
    best_params = {}
    base_svm = SVC(class_weight='balanced', random_state=42)
    calibrated_svm = CalibratedClassifierCV(base_svm, ensemble=False)
    model = Pipeline([
        ('scaler', StandardScaler()),
        ('svm', calibrated_svm)
    ])
    param_grid = {'svm__estimator__C': [0.1, 1, 10], 'svm__estimator__kernel': ['linear', 'rbf']}
    tscv = TimeSeriesSplit(n_splits=5)

    for tp in [10, 14, 20]:
        fe = FeatureEngineer(timeperiod=tp)
        Xf = fe.transform(X_raw)
        comb = pd.concat([Xf, y], axis=1).dropna()
        if comb.empty:
            continue
        Xc, yc = comb.drop(columns=['label']), comb['label']
        grid = GridSearchCV(model, param_grid, cv=tscv, scoring='f1', n_jobs=-1, verbose=0)
        grid.fit(Xc, yc)
        print(f"TP={tp} | F1={grid.best_score_:.4f}")
        if grid.best_score_ > best_score:
            best_score = grid.best_score_
            best_params = {**grid.best_params_, 'rsi_timeperiod': tp}

    if not best_params:
        msg = "❌ Strategy failed: No valid model parameters found"
        print(msg)
        send_telegram_message(msg)
        return

    # Train final calibrated model
    best_tp = best_params['rsi_timeperiod']
    svm_args = {k.replace('svm__estimator__', ''): v for k, v in best_params.items() if k.startswith('svm__estimator__')}
    fe_final = FeatureEngineer(timeperiod=best_tp)
    X_final = fe_final.transform(X_raw).dropna()
    data_final = pd.concat([X_final, y.reindex(X_final.index)], axis=1).dropna()
    Xm, ym = data_final.drop(columns=['label']), data_final['label']

    final_base = SVC(class_weight='balanced', random_state=42, **svm_args)
    final_calibrated = CalibratedClassifierCV(final_base, ensemble=False)
    final_model = Pipeline([
        ('scaler', StandardScaler()),
        ('svm', final_calibrated)
    ])
    final_model.fit(Xm, ym)

    # Backtest
    if RUN_BACKTEST:
        signals = generate_signals(final_model, Xm, SIGNAL_THRESHOLD)
        backtest_metrics = backtest(X_raw.loc[Xm.index], signals, INITIAL_CAPITAL)
        print("\n=== BACKTEST RESULTS ===")
        for k, v in backtest_metrics.items():
            print(f"{k}: {v}")

    # Get today's signal
    trade_plan, direction, risk = get_live_trade_plan(FOREX_PAIR, final_model, fe_final, INITIAL_CAPITAL, SIGNAL_THRESHOLD)
    print("\n" + trade_plan)

    # Save to log
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n[{datetime.now()}]\n{trade_plan}\n{'-'*50}\n")

    # Save trained model for merged strategy
    save_trained_model(final_model, fe_final)

    # Send plan to Telegram
    send_telegram_message(trade_plan)

    # --------------------------
    # 🚀 EXECUTE DEMO ORDER IF VALID SIGNAL
    # --------------------------
    if direction in ["BUY", "SELL"] and risk:
        execute_and_notify_demo_trade(direction, risk)
    else:
        print("\n⏸️ No valid BUY/SELL signal — no order executed.")

    return trade_plan


# ----------------------
# ✅ MERGE HELPER FUNCTIONS
# ----------------------
def save_trained_model(model, fe, path: str = "ml_trade_model.pkl"):
    """Save trained model + feature engineer for later use"""
    try:
        joblib.dump({"model": model, "feature_engineer": fe}, path)
        print(f"✅ ML model saved to {path}")
    except Exception as e:
        print(f"⚠️ Failed to save ML model: {e}")


def load_trained_model(path: str = "ml_trade_model.pkl"):
    """Load saved model and feature engineer"""
    try:
        data = joblib.load(path)
        print(f"✅ ML model loaded from {path}")
        return data["model"], data["feature_engineer"]
    except Exception as e:
        print(f"⚠️ Could not load ML model: {e}")
        return None, None


def get_live_features_for_pair(pair_symbol: str, fe, lookback_days: int = 60):
    """Get latest features for any JPY pair"""
    try:
        # Convert OANDA format like AUD_JPY → Yahoo format AUDJPY=X
        yf_symbol = pair_symbol.replace("_", "") + "=X"
        data = yf.download(yf_symbol, period=f"{lookback_days}d", interval="1d", progress=False)
        
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)
        if data.empty or len(data) < 30:
            return None

        data = data[["Open", "High", "Low", "Close", "Volume"]].copy()
        data["Volume"] = data["Volume"].replace(0, 1e-6)
        features = fe.transform(data).dropna()
        return None if features.empty else features.tail(1)
    except Exception as e:
        print(f"⚠️ Error getting features for {pair_symbol}: {e}")
        return None


def ml_filter(pair: str, direction: str, model=None, fe=None, min_confidence=0.50):
    """Return (should_skip, reason) based on ML confidence"""
    try:
        if model is None or fe is None:
            return False, "ML not available"
        
        features = get_live_features_for_pair(pair, fe)
        if features is None:
            return False, "Insufficient data"

        prob_up = model.predict_proba(features)[0, 1]
        confidence = prob_up if direction == "BUY" else (1 - prob_up)

        if confidence < min_confidence:
            return True, f"confidence {confidence:.1%} < {min_confidence:.0%}"
        return False, f"confidence {confidence:.1%}"
    except Exception as e:
        return True, f"ML check failed: {str(e)}"


def is_ml_weighted_dominance_enabled():
    """Simple helper for config flag"""
    return False


if __name__ == "__main__":
    run_strategy()