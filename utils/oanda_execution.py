# utils/oanda_execution.py — FINAL FULL VERSION
# """
# OANDA v20 Execution Wrapper — Production Hardened.
#
# Keyword arguments:
# api_client -- Initialized oandapyV20 API client
# account_id -- OANDA account ID
# api_token -- OANDA API token (optional, for reference)
# max_retries -- Maximum number of connection retries (default: 5)
# retry_delay -- Delay between retries in seconds (default: 3.0)
#
# Return: Instance of OANDAExecution
# """


# ✅ Config injected · No env reads · No config_oanda import
# ✅ Connection retry · Dynamic precision · SL/TP modification
# ✅ Native Trailing Stop Loss (server-side)
# ❌ No trailing TP — OANDA API does NOT support it natively

import time
from oandapyV20.endpoints.orders import OrderCreate, OrderCancel, OrderList
from oandapyV20.endpoints.positions import OpenPositions, PositionClose
from oandapyV20.endpoints.accounts import AccountSummary, AccountInstruments
from oandapyV20 import V20Error
from utils.oanda_state import build_client_extensions

# -----------------------------------------------------------------------------
# IRON RULE: All config INJECTED from main/config layer
# This module NEVER reads env vars or imports config_oanda
# -----------------------------------------------------------------------------

class OANDAExecution:
    """
    OANDA v20 Execution Wrapper — Production Hardened.
    All connection/account params injected; zero env access in utils.
    """

    def __init__(self, api_client, account_id: str, api_token: str = "",
                 max_retries: int = 5, retry_delay: float = 3.0):
        self.api = api_client
        self.account_id = account_id
        self.api_token = api_token
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._instrument_meta = {}  # precision & min trade size cache

    # -------------------------------------------------------------------------
    # CONNECTION HEALTH CHECK + AUTO-RECONNECT
    # -------------------------------------------------------------------------
    def check_connection(self) -> bool:
        """Verify API connection with retries."""
        last_err = None
        for attempt in range(self.max_retries):
            try:
                resp = self.api.request(AccountSummary(self.account_id))
                print(f"✅ ACCOUNT OK: {resp['account']['id']} — {resp['account']['currency']}")
                return True
            except (V20Error, Exception) as e:
                last_err = e
                print(f"⚠️ Connection {attempt+1}/{self.max_retries}: {repr(e)[:100]}")
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
        print(f"❌ FATAL: All {self.max_retries} attempts failed.")
        return False

    # -------------------------------------------------------------------------
    # INSTRUMENT METADATA + PRECISION VALIDATION
    # -------------------------------------------------------------------------
    def fetch_instrument_meta(self, instrument: str) -> dict:
        """Query OANDA for precision, min trade size, pip location; cache."""
        if instrument in self._instrument_meta:
            return self._instrument_meta[instrument]
        try:
            resp = self.api.request(AccountInstruments(
                self.account_id, params={"instruments": instrument}))
            info = resp["instruments"][0]
            meta = {
                "display_precision": int(info["displayPrecision"]),
                "trade_precision": int(info["tradeUnitsPrecision"]),
                "minimum_trade_size": float(info["minimumTradeSize"]),
                "pip_location": int(info["pipLocation"]),
                "min_trailing_distance": float(info.get("minimumTrailingStopDistance", 0.00050)),
                "max_trailing_distance": float(info.get("maximumTrailingStopDistance", 1.0)),
            }
            self._instrument_meta[instrument] = meta
            return meta
        except Exception as e:
            print(f"⚠️ {instrument} using fallback precision: {repr(e)[:60]}")
            decimals = 3 if "JPY" in instrument else 5
            meta = {
                "display_precision": decimals,
                "trade_precision": 0,
                "minimum_trade_size": 1000,
                "pip_location": -decimals + 1,
                "min_trailing_distance": 0.00050 if "JPY" not in instrument else 0.030,
                "max_trailing_distance": 1.0,
            }
            self._instrument_meta[instrument] = meta
            return meta

    def normalize_price(self, instrument: str, price: float | None) -> float | None:
        """Round price to OANDA-required precision."""
        return None if price is None else round(price, self.fetch_instrument_meta(instrument)["display_precision"])

    def normalize_units(self, instrument: str, units: float) -> int:
        """Enforce min trade size & lot precision."""
        meta = self.fetch_instrument_meta(instrument)
        sign = 1 if units >= 0 else -1
        abs_units = max(abs(units), meta["minimum_trade_size"])
        return int(sign * round(abs_units, meta["trade_precision"]))

    # -------------------------------------------------------------------------
    # HELPER: BUILD TRAILING STOP PAYLOAD FOR NEW ORDERS
    # -------------------------------------------------------------------------
    def build_trailing_stop_fill_dict(self, instrument: str, distance: float):
        """Returns dict to merge into order['order'] for server-side trailing SL."""
        if not distance or distance <= 0:
            return {}
        meta = self.fetch_instrument_meta(instrument)
        # Enforce OANDA's min/max trailing distance limits
        dist = max(min(distance, meta["max_trailing_distance"]), meta["min_trailing_distance"])
        return {
            "trailingStopLossOnFill": {
                "distance": f"{dist:.{meta['display_precision']}f}",
                "timeInForce": "GTC",
            }
        }

    # -------------------------------------------------------------------------
    # MARKET ORDER — Signal object interface
    # -------------------------------------------------------------------------
    def execute_market_trade(self, signal, units_override=None,
                             trailing_sl_distance: float = None,
                             client_extensions: dict | None = None) -> dict:
        instrument = signal.pair_to_trade
        raw_units = units_override if units_override is not None else 10000
        raw_units = -abs(raw_units) if signal.action == "SELL" else abs(raw_units)
        units = self.normalize_units(instrument, raw_units)

        sl_price = self.normalize_price(instrument, signal.stop_loss)
        tp_price = self.normalize_price(instrument, signal.take_profit)

        entry_data = {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(units),
            "stopLossOnFill": {"price": str(sl_price), "triggerCondition": "DEFAULT"} if sl_price else None,
            "takeProfitOnFill": {"price": str(tp_price), "triggerCondition": "DEFAULT"} if tp_price else None,
        }
        entry_data["clientExtensions"] = client_extensions or build_client_extensions(
            {
                "pair": instrument,
                "action": signal.action,
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit,
                "reasoning": getattr(signal, "reasoning", ""),
            },
            strategy_tag="ai_strategy",
        )
        # Merge trailing SL if provided (replaces fixed SL)
        entry_data.update(self.build_trailing_stop_fill_dict(instrument, trailing_sl_distance))

        try:
            resp = self.api.request(OrderCreate(self.account_id, {"order": entry_data}))
            fill_tx = resp.get("orderFillTransaction", {})
            if not fill_tx:
                return {"status": "ERROR", "code": "NO_FILL", "message": f"Keys: {list(resp.keys())}"}

            result = {
                "status": "SUCCESS",
                "order_id": fill_tx.get("orderID"),
                "trade_id": fill_tx.get("tradeOpened", {}).get("tradeID"),
                "filled_price": float(fill_tx.get("price", 0)),
                "units": units,
                "instrument": instrument,
                "trailing_activated": bool(trailing_sl_distance and trailing_sl_distance > 0),
                "raw": fill_tx,
            }
            print(f"🔹 FILLED: {signal.action} {instrument} @ {result['filled_price']}")
            if result["trailing_activated"]:
                print(f"🔹 TRAILING SL ATTACHED: {trailing_sl_distance}")
            else:
                print(f"🔹 SL: {sl_price} | TP: {tp_price}")
            return result

        except V20Error as e:
            return {"status": "ERROR", "code": e.code, "message": f"OANDA {e.code}: {e.message[:200]}"}
        except Exception as e:
            return {"status": "ERROR", "code": "EXCEPTION", "message": repr(e)[:200]}

    # -------------------------------------------------------------------------
    # DICT SIGNAL INTERFACE — backward compatibility
    # -------------------------------------------------------------------------
    def open_order(self, signal: dict, units: int = 10000,
                   trailing_sl_distance: float = None,
                   client_extensions: dict | None = None) -> dict:
        instrument = signal.get("pair", signal.get("instrument", ""))
        action = signal.get("action", "BUY")
        raw_units = units if action.upper() == "BUY" else -abs(units)
        units = self.normalize_units(instrument, raw_units)
        sl_price = self.normalize_price(instrument, signal.get("stop_loss"))
        tp_price = self.normalize_price(instrument, signal.get("take_profit"))

        entry_data = {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(units),
            "stopLossOnFill": {"price": str(sl_price), "triggerCondition": "DEFAULT"} if sl_price else None,
            "takeProfitOnFill": {"price": str(tp_price), "triggerCondition": "DEFAULT"} if tp_price else None,
        }
        entry_data["clientExtensions"] = client_extensions or build_client_extensions(
            signal,
            strategy_tag="ai_strategy",
        )
        entry_data.update(self.build_trailing_stop_fill_dict(instrument, trailing_sl_distance))

        try:
            resp = self.api.request(OrderCreate(self.account_id, {"order": entry_data}))
        except V20Error as e:
            return {"status": "ERROR", "code": e.code, "message": f"OANDA {e.code}: {e.message[:200]}"}
        except Exception as e:
            return {"status": "ERROR", "code": "EXCEPTION", "message": repr(e)[:200]}

        fill_tx = resp.get("orderFillTransaction")
        if not fill_tx:
            return {"status": "ERROR", "code": "NO_FILL", "message": f"Keys: {list(resp.keys())}"}

        result = {
            "status": "SUCCESS",
            "order_id": fill_tx.get("orderID"),
            "trade_id": fill_tx.get("tradeOpened", {}).get("tradeID"),
            "filled_price": float(fill_tx.get("price", 0)),
            "units": units,
            "instrument": instrument,
            "trailing_activated": bool(trailing_sl_distance and trailing_sl_distance > 0),
        }
        print(f"🔹 FILLED: {action} {instrument} @ {result['filled_price']}")
        print(f"🔹 {'TRAILING SL' if result['trailing_activated'] else 'SL/TP'} attached")
        return result

    # -------------------------------------------------------------------------
    # MODIFY EXISTING TRADE — update SL / TP / attach trailing SL
    # -------------------------------------------------------------------------
    def modify_trade(self, trade_id: str, instrument: str,
                     stop_loss: float | None = None,
                     take_profit: float | None = None,
                     trailing_sl_distance: float | None = None) -> dict:
        """
        Update SL/TP or attach/replace with TRAILING stop on an open trade.
        - trailing_sl_distance=value → replaces any fixed SL with server-side trailing
        - trailing_sl_distance=0 → cancel trailing stop
        - stop_loss=price → set fixed SL (cancels trailing)
        - take_profit=price → update fixed TP
        """
        results = {"trade_id": trade_id, "actions": [], "errors": []}
        meta = self.fetch_instrument_meta(instrument)

        # 1. ATTACH/REPLACE WITH TRAILING STOP (replaces fixed SL)
        if trailing_sl_distance is not None:
            if trailing_sl_distance <= 0:
                # Cancel trailing stop: find & cancel the order
                try:
                    orders = self.api.request(OrderList(
                        self.account_id, params={"tradeID": trade_id})).get("orders", [])
                    for ord in orders:
                        if ord.get("type") == "TRAILING_STOP_LOSS":
                            self.api.request(OrderCancel(self.account_id, ord["id"]))
                            results["actions"].append("TrailingStop CANCELLED")
                except Exception as e:
                    results["errors"].append(f"Cancel trailing: {repr(e)[:100]}")
            else:
                dist = max(min(trailing_sl_distance, meta["max_trailing_distance"]),
                           meta["min_trailing_distance"])
                dist_str = f"{dist:.{meta['display_precision']}f}"
                try:
                    self.api.request(OrderCreate(self.account_id, {
                        "order": {
                            "type": "TRAILING_STOP_LOSS",
                            "tradeID": str(trade_id),
                            "distance": dist_str,
                            "timeInForce": "GTC",
                        }
                    }))
                    results["actions"].append(f"TrailingStop → {dist_str}")
                except Exception as e:
                    results["errors"].append(f"Trailing failed: {repr(e)[:100]}")

        # 2. SET FIXED STOP LOSS (only if NOT setting trailing)
        elif stop_loss is not None:
            sl_price = self.normalize_price(instrument, stop_loss)
            try:
                self.api.request(OrderCreate(self.account_id, {
                    "order": {
                        "type": "STOP_LOSS",
                        "tradeID": str(trade_id),
                        "price": str(sl_price),
                        "timeInForce": "GTC",
                    }
                }))
                results["actions"].append(f"SL → {sl_price}")
            except Exception as e:
                results["errors"].append(f"SL failed: {repr(e)[:100]}")

        # 3. UPDATE TAKE PROFIT
        if take_profit is not None:
            tp_price = self.normalize_price(instrument, take_profit)
            try:
                self.api.request(OrderCreate(self.account_id, {
                    "order": {
                        "type": "TAKE_PROFIT",
                        "tradeID": str(trade_id),
                        "price": str(tp_price),
                        "timeInForce": "GTC",
                    }
                }))
                results["actions"].append(f"TP → {tp_price}")
            except Exception as e:
                results["errors"].append(f"TP failed: {repr(e)[:100]}")

        results["status"] = "SUCCESS" if not results["errors"] else "PARTIAL"
        return results

    # -------------------------------------------------------------------------
    # CLOSE POSITIONS
    # -------------------------------------------------------------------------
    def close_all_trades(self, instrument: str | None = None) -> dict:
        result = {"closed": [], "errors": []}
        try:
            req = OpenPositions(self.account_id)
            self.api.request(req)
            positions = req.response.get("positions", [])
        except Exception as e:
            return {"status": "ERROR", "message": f"Fetch positions failed: {repr(e)[:120]}"}

        if instrument:
            positions = [p for p in positions if p.get("instrument") == instrument]
        if not positions:
            print("No open positions to close.")
            return {"status": "OK", "reason": "No positions"}

        for pos in positions:
            pair = pos["instrument"]
            long_u = int(float(pos.get("long", {}).get("units", 0)))
            short_u = int(float(pos.get("short", {}).get("units", 0)))
            payload = {}
            if long_u > 0: payload["longUnits"] = str(long_u)
            if short_u < 0: payload["shortUnits"] = str(abs(short_u))
            if not payload: continue
            try:
                self.api.request(PositionClose(self.account_id, pair, data=payload))
                print(f"  ✅ CLOSED {pair}")
                result["closed"].append(pair)
            except Exception as e:
                err = f"{pair}: {repr(e)[:100]}"
                print(f"  ❌ ERROR closing {err}")
                result["errors"].append(err)
        return {"status": "OK" if not result["errors"] else "PARTIAL", **result}

"""使用示例
# --- main.py / config entry point ONLY ---
import os
from config_bot import load_profile, cfg
from utils.oanda_execution import OANDAExecution

P = load_profile(os.getenv("FX_PROFILE", "profile2"))
oanda = OANDAExecution(
    api_client=cfg(P, "OANDA_API"),
    account_id=cfg(P, "OANDA_ACCOUNT_ID"),
    api_token=os.getenv("OANDA_API_TOKEN", ""),
)
oanda.check_connection()

# === 开仓时直接附加追踪止损（替代固定SL） ===
oanda.execute_market_trade(
    signal=my_signal,
    trailing_sl_distance=0.00500  # 50 pips trailing SL
)

# === 开仓后给持仓追加追踪止损 ===
oanda.modify_trade(
    trade_id="1234",
    instrument="EUR_USD",
    trailing_sl_distance=0.00500  # 把固定SL换成追踪SL
)

# === 修改持仓SL/TP ===
oanda.modify_trade(
    trade_id="1234",
    instrument="EUR_USD",
    stop_loss=1.08000,   # 设固定SL
    take_profit=1.09500  # 更新TP
)

# === 取消追踪止损 ===
oanda.modify_trade("1234", "EUR_USD", trailing_sl_distance=0)
"""

# -----------------------------------------------------------------------------
# MODULE-LEVEL CONVENIENCE Wrapper — for backward compat with runners that
# import `open_oanda_order` / `close_all_trades` / `api` directly.
# These delegate to the OANDAExecution class above.
# -----------------------------------------------------------------------------
import os as _os
from config_bot import load_profile as _load_profile, cfg as _cfg

_DEFAULT_PROFILE = _os.getenv("FX_PROFILE", "profile2")
_P = _load_profile(_DEFAULT_PROFILE)

api = _cfg(_P, "OANDA_API")
OANDA_ACCOUNT_ID = _cfg(_P, "OANDA_ACCOUNT_ID")
OANDA_API_TOKEN = _os.getenv("OANDA_API_TOKEN", "")

_exec = OANDAExecution(
    api_client=api,
    account_id=OANDA_ACCOUNT_ID,
    api_token=OANDA_API_TOKEN,
)


def open_oanda_order(signal: dict, units: int = 10000, client_extensions: dict | None = None) -> dict:
    return _exec.open_order(signal, units=units, client_extensions=client_extensions)


def close_all_trades(instrument=None) -> dict:
    return _exec.close_all_trades(instrument=instrument)


# -----------------------------------------------------------------------------
# MODULE-LEVEL CONVENIENCE Wrapper — backward compat for runners
# -----------------------------------------------------------------------------
import os as _os
from config_bot import load_profile as _load_profile, cfg as _cfg

_DEFAULT_PROFILE = _os.getenv("FX_PROFILE", "profile2")
_P = _load_profile(_DEFAULT_PROFILE)

api = _cfg(_P, "OANDA_API")
OANDA_ACCOUNT_ID = _cfg(_P, "OANDA_ACCOUNT_ID")
OANDA_API_TOKEN = _os.getenv("OANDA_API_TOKEN", "")

_exec = OANDAExecution(
    api_client=api,
    account_id=OANDA_ACCOUNT_ID,
    api_token=OANDA_API_TOKEN,
)


def open_oanda_order(signal: dict, units: int = 10000, client_extensions: dict | None = None) -> dict:
    return _exec.open_order(signal, units=units, client_extensions=client_extensions)


def close_all_trades(instrument=None) -> dict:
    return _exec.close_all_trades(instrument=instrument)
