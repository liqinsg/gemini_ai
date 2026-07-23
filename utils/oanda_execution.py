import sys
import os

# Add project root to import path so config.py is found
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Now your normal imports work perfectly
import oandapyV20
import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.trades as trades
from oandapyV20.exceptions import V20Error
from typing import Dict, Optional
from config import OANDA_ACCOUNT_ID, OANDA_ENV, OANDA_API_TOKEN, DEMO_MODE

api = oandapyV20.API(
    access_token=OANDA_API_TOKEN,
    environment=OANDA_ENV
)

def open_oanda_order(signal: Dict, units: Optional[float] = None) -> Dict:
    """
    Open a market order on OANDA from your strategy signal dict.
    Expected keys: pair, action, stop_loss, take_profit
    """
    if not OANDA_ACCOUNT_ID or not OANDA_API_TOKEN:
        return {"status": "ERROR", "message": "Missing OANDA credentials"}

    # Parse signal data
    pair_raw = signal.get("pair")
    if not pair_raw:
        return {"status": "ERROR", "message": "Signal missing 'pair'"}

    pair = pair_raw.replace("_", "/")  # e.g., EUR_JPY -> EUR/JPY

    action = signal.get("action")
    if action not in {"BUY", "SELL"}:
        return {"status": "ERROR", "message": "Signal 'action' must be BUY or SELL"}

    sl_raw = signal.get("stop_loss")
    tp_raw = signal.get("take_profit")
    if sl_raw is None or tp_raw is None:
        return {"status": "ERROR", "message": "Signal missing stop_loss or take_profit"}

    # Ensure numeric
    try:
        sl = float(sl_raw)
        tp = float(tp_raw)
    except (TypeError, ValueError):
        return {"status": "ERROR", "message": "stop_loss/take_profit must be numeric"}

    default_units = 10000
    position_units = default_units if units is None else units
    position_units = float(position_units)

    # Direction
    if action == "SELL":
        position_units = -abs(position_units)

    order_payload = {
        "order": {
            "type": "MARKET",
            "instrument": pair,
            "units": str(int(position_units)),  # OANDA expects integer units as string
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {
                "price": str(round(sl, 3)),
                "timeInForce": "GTC",
            },
            "takeProfitOnFill": {
                "price": str(round(tp, 3)),
                "timeInForce": "GTC",
            },
        }
    }

    try:
        print(f"\n[OANDA EXEC] Sending {action} order for {pair}...")
        print(f"  Units: {abs(int(position_units))} | SL: {sl:.3f} | TP: {tp:.3f}")

        request = orders.OrderCreate(OANDA_ACCOUNT_ID, data=order_payload)
        response = api.request(request)

        fill = response["orderFillTransaction"]
        # ✅ Safe access — handle missing fields gracefully
        result = {
            "status": "SUCCESS",
            "order_id": fill.get("id", "unknown"),
            "filled_price": fill.get("price", "unknown"),
            "instrument": fill.get("instrument", pair),
            "units": fill.get("units", str(int(position_units))),
            "sl_set": fill.get("stopLossOnFill", {}).get("price", "NOT_SET"),
            "tp_set": fill.get("takeProfitOnFill", {}).get("price", "NOT_SET"),
            "time": fill.get("time", "unknown"),
        }

        print(f"[OANDA EXEC] ✅ Order filled: {result['order_id']} @ {result['filled_price']}")
        return result

    except V20Error as e:
        error_msg = f"OANDA API Error: {e}"
        print(f"[OANDA EXEC] ❌ {error_msg}")
        return {"status": "ERROR", "message": error_msg}
    except Exception as e:
        # ✅ Even if parsing fails, check if order actually succeeded first
        print(f"[OANDA EXEC] ⚠️ Parsing error — checking if order filled anyway...")
        try:
            # Quick check: look for recent open trade
            from oandapyV20.endpoints.trades import TradesList
            trades_resp = api.request(TradesList(OANDA_ACCOUNT_ID))
            recent = [t for t in trades_resp.get("trades", []) if t["instrument"] == pair]
            if recent:
                print(f"[OANDA EXEC] ✅ Order DID fill — found trade {recent[0]['id']}")
                return {
                    "status": "SUCCESS",
                    "order_id": recent[0]["id"],
                    "filled_price": recent[0]["price"],
                    "instrument": recent[0]["instrument"],
                    "units": recent[0]["currentUnits"],
                    "sl_set": "CHECK_API",
                    "tp_set": "CHECK_API",
                    "time": recent[0]["time"],
                }
        except:
            pass
        error_msg = f"Unexpected Error: {str(e)}"
        print(f"[OANDA EXEC] ❌ {error_msg}")
        return {"status": "ERROR", "message": error_msg}

def close_all_trades() -> Dict:
    """Helper: Close all open trades on your account"""
    try:
        request = trades.TradesList(OANDA_ACCOUNT_ID)
        open_trades = api.request(request).get("trades", [])
        if not open_trades:
            return {"status": "INFO", "message": "No open trades found"}

        results = []
        for trade in open_trades:
            req = trades.TradeClose(OANDA_ACCOUNT_ID, tradeID=trade["id"])
            api.request(req)
            results.append({"trade_id": trade["id"], "closed": True})
            print(f"[CLEANUP] Closed trade ID: {trade['id']}")

        return {"status": "SUCCESS", "closed_trades": results}

    except Exception as e:
        return {"status": "ERROR", "message": str(e)}


# ----------------------
# 🧪 TEST MAIN FUNCTION
# ----------------------
def main():
    print("=" * 50)
    print("🧪 TESTING OANDA ORDER OPENING (DEMO ACCOUNT)")
    print("=" * 50)

    # --------------------------
    # 1. Demo test signal
    # --------------------------
    # Matches exactly what your strategy generates
    test_signal = {
        "pair": "USD_JPY",          # Will auto-convert to USD/JPY
        "action": "BUY",            # Use "SELL" to test short
        "stop_loss": 162.10,        # Example SL below entry
        "take_profit": 162.70       # Example TP above entry
    }

    # Optional: override units here (default = 10000)
    test_units = None

    # --------------------------
    # 2. Run order open test
    # --------------------------
    print("\n📤 Sending test order...")
    open_result = open_oanda_order(test_signal, units=test_units)

    print("\n📋 Open Order Result:")
    for k, v in open_result.items():
        print(f"  {k}: {v}")

    # --------------------------
    # 3. Optional: Auto-close test trade (uncomment to use)
    # --------------------------
    # print("\n🧹 Closing all trades for cleanup...")
    # close_result = close_all_trades()
    # print("\n📋 Close Result:")
    # for k, v in close_result.items():
    #     print(f"  {k}: {v}")

    print("\n✅ Test run complete!")


if __name__ == "__main__":
    main()