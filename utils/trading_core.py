# utils/trading_core.py
"""
Core trading utilities: OANDA client, order execution, price formatting, Gemini helpers
"""
from datetime import datetime
import json
import importlib
from oandapyV20 import API
from google import genai
from google.genai import types
import oandapyV20.endpoints.instruments as instruments
import oandapyV20.endpoints.pricing as pricing
from config import OANDA_ACCOUNT_ID


from config import (
    OANDA_ENV,
    OANDA_API_TOKEN,
    OANDA_ACCOUNT_ID,
    GEMINI_API_KEY,
    GEMINI_NEWS_MODEL,
    USE_GEMINI_AI
)


# --------------------------
# Initialize Clients
# --------------------------
oanda_client = API(access_token=OANDA_API_TOKEN, environment=OANDA_ENV)
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if USE_GEMINI_AI else None


# --------------------------
# Basic Helpers
# --------------------------
def format_price_for_instrument(price, instrument: str) -> str:
    try:
        numeric_price = float(price)
    except (TypeError, ValueError):
        return str(price)
    return f"{numeric_price:.3f}" if instrument.endswith("_JPY") else f"{numeric_price:.5f}"


def get_open_position(instrument: str):
    positions_module = importlib.import_module("oandapyV20.endpoints.positions")
    req = positions_module.OpenPositions(accountID=OANDA_ACCOUNT_ID)
    oanda_client.request(req)
    return next((p for p in req.response.get("positions", []) if p.get("instrument") == instrument), None)


def attach_sl_tp_to_open_trade(signal, instrument: str | None = None) -> bool:
    instrument = instrument or signal.pair_to_trade
    position = get_open_position(instrument)
    if not position:
        print(f"[EXEC] No open position for {instrument}")
        return False
    trade_ids = []
    for side in (position.get("long", {}), position.get("short", {})):
        trade_ids.extend(side.get("tradeIDs", []))
    if not trade_ids:
        return False
    trade_id = trade_ids[0]
    trades_mod = importlib.import_module("oandapyV20.endpoints.trades")
    payload = {
        "stopLoss": {"price": format_price_for_instrument(signal.stop_loss, instrument), "timeInForce": "GTC"},
        "takeProfit": {"price": format_price_for_instrument(signal.take_profit, instrument), "timeInForce": "GTC"}
    }
    try:
        oanda_client.request(trades_mod.TradeCRCDO(OANDA_ACCOUNT_ID, trade_id, data=payload))
        print(f"[EXEC] SL/TP attached to {instrument}")
        return True
    except Exception as e:
        print(f"[EXEC ERROR] {e}")
        return False


def verify_sl_tp_on_trade(trade_id: str, instrument: str) -> None:
    trades_mod = importlib.import_module("oandapyV20.endpoints.trades")
    try:
        resp = oanda_client.request(trades_mod.TradeDetails(OANDA_ACCOUNT_ID, trade_id)).response
        sl = resp["trade"].get("stopLossOrder", {})
        tp = resp["trade"].get("takeProfitOrder", {})
        print(f"[VERIFY] SL={sl.get('price')}, TP={tp.get('price')}") if sl or tp else print("[VERIFY] No SL/TP found")
    except Exception as e:
        print(f"[VERIFY ERROR] {e}")


def get_recent_range(instrument: str, granularity: str = "H1", lookback: int = 20) -> tuple[float, float, float] | None:
    inst_mod = importlib.import_module("oandapyV20.endpoints.instruments")
    try:
        resp = oanda_client.request(inst_mod.InstrumentsCandles(instrument, params={"count": lookback+1, "granularity": granularity})).response
        candles = [c for c in resp["candles"] if c["complete"]]
        if len(candles) < lookback:
            return None
        highs = [float(c["mid"]["h"]) for c in candles[:-1]]
        lows = [float(c["mid"]["l"]) for c in candles[:-1]]
        return max(highs), min(lows), float(candles[-1]["mid"]["c"])
    except Exception as e:
        print(f"[RANGE ERROR] {e}")
        return None


def execute_market_trade(signal, units_override=None):
    if not signal or signal.action == "HOLD":
        print("[EXEC] No action")
        return
    if get_open_position(signal.pair_to_trade):
        print("[EXEC] Already have position")
        return
    pricing_mod = importlib.import_module("oandapyV20.endpoints.pricing")
    try:
        resp = oanda_client.request(pricing_mod.PricingInfo(OANDA_ACCOUNT_ID, {"instruments": signal.pair_to_trade})).response
        ask = float(resp["prices"][0]["asks"][0]["price"])
        bid = float(resp["prices"][0]["bids"][0]["price"])
        if (signal.action == "BUY" and (signal.stop_loss >= ask or signal.take_profit <= ask)) or \
           (signal.action == "SELL" and (signal.stop_loss <= bid or signal.take_profit >= bid)):
            print("[EXEC] Invalid SL/TP")
            return
    except Exception as e:
        print(f"[PRICE CHECK] {e}")
    units = (units_override or 10000) if signal.action == "BUY" else -(units_override or 10000)
    orders_mod = importlib.import_module("oandapyV20.endpoints.orders")
    payload = {
        "order": {
            "units": str(units),
            "instrument": signal.pair_to_trade,
            "timeInForce": "FOK",
            "type": "MARKET",
            "stopLossOnFill": {"price": format_price_for_instrument(signal.stop_loss, signal.pair_to_trade)},
            "takeProfitOnFill": {"price": format_price_for_instrument(signal.take_profit, signal.pair_to_trade)},
            "clientExtensions": {"comment": signal.reasoning[:128], "tag": "ai-strategy"}
        }
    }
    try:
        resp = oanda_client.request(orders_mod.OrderCreate(OANDA_ACCOUNT_ID, payload)).response
        if "orderFillTransaction" in resp:
            print(f"[EXEC] Filled {signal.action} {signal.pair_to_trade}")
            attach_sl_tp_to_open_trade(signal)
            return True
    except Exception as e:
        print(f"[EXEC ERROR] {e}")
    return False

# --------------------------
# Gemini Enhancements
# --------------------------


def get_latest_news_sentiment() -> str:
    if not USE_GEMINI_AI or not gemini_client:
        return "Gemini disabled"
    try:
        res = gemini_client.models.generate_content(
            model=GEMINI_NEWS_MODEL,
            contents="Summarize today's major FX/macro news: JPY, USD, EUR, GBP drivers + next 24h risk events.",
            config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())], temperature=0.1)
        )
        return res.text.strip()
    except Exception as e:
        print(f"[SENTIMENT ERROR] {e}")
        return "No sentiment available"


def validate_signal_with_fundamentals(signal: dict, sentiment: str) -> tuple[bool, str]:
    if not USE_GEMINI_AI:
        return True, "Gemini disabled"
    prompt = f"""
    TECHNICAL: {signal['action']} {signal['pair']} | SL={signal['stop_loss']} TP={signal['take_profit']}
    MACRO: {sentiment}
    Return JSON: {{"approve": true/false, "reason": "..."}}
    """
    try:
        res = gemini_client.models.generate_content(model=GEMINI_NEWS_MODEL, contents=prompt, temperature=0.0)
        data = json.loads(res.text.strip("`json \n"))
        return data.get("approve", True), data.get("reason", "")
    except Exception as e:
        print(f"[VALIDATION ERROR] {e}")
        return True, "Validation skipped"


def get_news_risk_bias(pair: str) -> dict:
    if not USE_GEMINI_AI:
        return {"impact": 0, "bias": "NEUTRAL"}
    prompt = f"Search high-impact events for {pair} next 24h. Return JSON: {{\"impact\":0-3, \"bias\":\"BUY/SELL/NEUTRAL\"}}"
    try:
        res = gemini_client.models.generate_content(model=GEMINI_NEWS_MODEL, contents=prompt, config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]))
        return json.loads(res.text.strip("`json \n"))
    except Exception:
        return {"impact": 0, "bias": "NEUTRAL"}


def get_ensemble_consensus(prompt: str):
    # ✅ Import HERE only — breaks circular dependency
    from models_ensemble import get_gemini_decision, get_qwen_decision, get_deepseek_decision, use_qwen, use_deepseek

    signals = []
    try:
        signals.append(("gemini", get_gemini_decision(prompt)))
    except Exception as e:
        print(f"[ENSEMBLE] Gemini: {e}")
    if use_qwen:
        try:
            signals.append(("qwen", get_qwen_decision(prompt)))
        except Exception as e:
            print(f"[ENSEMBLE] Qwen: {e}")
    if use_deepseek:
        try:
            signals.append(("deepseek", get_deepseek_decision(prompt)))
        except Exception as e:
            print(f"[ENSEMBLE] DeepSeek: {e}")
    if not signals:
        return None, signals
    if len({s.pair_to_trade for _, s in signals}) != 1:
        return None, signals
    actions = [s.action for _, s in signals]
    best = max(set(actions), key=actions.count)
    return next(s for _, s in signals if s.action == best), signals


def run_trading_cycle():
    from custom_strategy import analyze_custom_strategy, get_last_signal
    print("\n=== START TRADING CYCLE ===")
    try:
        report = analyze_custom_strategy()
        signal = get_last_signal()
        if not signal:
            print("[STRATEGY] No signal — HOLD")
            return
        sentiment = get_latest_news_sentiment()
        news = get_news_risk_bias(signal["pair"])
        if news["impact"] >= 2:
            print("[NEWS RISK] High impact — HOLD")
            return
        ok, reason = validate_signal_with_fundamentals(signal, sentiment)
        if not ok:
            print(f"[GEMINI REJECT] {reason}")
            return
        final_signal, _ = get_ensemble_consensus(f"{report}\n{sentiment}")
        if final_signal:
            execute_market_trade(final_signal)
    except Exception as e:
        print(f"[CYCLE ERROR] {e}")


def get_candles(
    instrument: str,
    granularity: str = "D",
    count: int = 50,
    start: datetime | None = None,
    end: datetime | None = None
) -> list:
    """Fetch historical candles from OANDA API, matches unified format"""
    params = {"granularity": granularity, "count": count}
    if start:
        params["from"] = start.isoformat()
    if end:
        params["to"] = end.isoformat()

    try:
        req = instruments.InstrumentsCandles(instrument=instrument, params=params)
        resp = oanda_client.request(req)
        return resp.get("candles", [])
    except Exception as e:
        print(f"[OANDA] Error fetching candles: {str(e)}")
        return []


def get_latest_price(instrument: str) -> float | None:
    """Get latest mid price from OANDA"""
    try:
        req = pricing.PricingInfo(
            accountID=OANDA_ACCOUNT_ID,
            params={"instruments": instrument}
        )
        resp = oanda_client.request(req)
        prices = resp.get("prices", [])
        if not prices:
            return None
        bid = float(prices[0]["bids"][0]["price"])
        ask = float(prices[0]["asks"][0]["price"])
        return round((bid + ask) / 2, 5)
    except Exception as e:
        print(f"[OANDA] Error fetching price: {str(e)}")
        return None

def close_position(instrument: str) -> bool:
    positions_mod = importlib.import_module(
        "oandapyV20.endpoints.positions"
    )

    try:
        pos_req = positions_mod.OpenPositions(
            accountID=OANDA_ACCOUNT_ID
        )
        oanda_client.request(pos_req)

        position = next(
            (
                p
                for p in pos_req.response.get("positions", [])
                if p.get("instrument") == instrument
            ),
            None,
        )

        if not position:
            print(f"[CLOSE] No open position for {instrument}")
            return False

        long_units = int(
            float(position.get("long", {}).get("units", 0))
        )

        short_units = int(
            float(position.get("short", {}).get("units", 0))
        )

        payload = {}

        if long_units > 0:
            payload["longUnits"] = str(long_units)

        if short_units < 0:
            payload["shortUnits"] = str(abs(short_units))

        req = positions_mod.PositionClose(
            accountID=OANDA_ACCOUNT_ID,
            instrument=instrument,
            data=payload,
        )

        oanda_client.request(req)

        print(f"[CLOSE] Closed {instrument}")
        return True

    except Exception as e:
        print(f"[CLOSE ERROR] {e}")
        return False