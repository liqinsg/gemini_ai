"""
Stop order placement helpers — S4 strategy
-------------------------------------------
All entries use STOP orders (not LIMIT) so the trade only fills
when price confirms the move through the level, not before.

BUY  STOP: entry ABOVE current price → fills on upward breakout/bounce
SELL STOP: entry BELOW current price → fills on downward breakdown/bounce
"""

import importlib
from datetime import datetime, timezone, timedelta
from oandapyV20 import API
from config import OANDA_API_TOKEN, OANDA_ENV, OANDA_ACCOUNT_ID
from trading_core format_price_for_instrument

try:
    orders_module = importlib.import_module("oandapyV20.endpoints.orders")
except Exception:
    orders_module = None

oanda_client = API(access_token=OANDA_API_TOKEN, environment=OANDA_ENV)


def _gtd_time(minutes: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(minutes=minutes)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def place_stop_entry_order(
    pair: str,
    action: str,
    units: int,
    entry: float,
    sl: float,
    tp: float,
    expiry_minutes: int = 1440,
    label: str = "S4",
) -> str | None:
    """
    Places a single STOP order.
    Returns order ID on success, None on failure.
    """
    if orders_module is None:
        print(f"[{label}] oandapyV20 orders not available.")
        return None

    order_units = units if action == "BUY" else -units
    gtd = _gtd_time(expiry_minutes)

    payload = {
        "order": {
            "instrument":   pair,
            "units":        str(order_units),
            "price":        format_price_for_instrument(entry, pair),
            "type":         "STOP",
            "timeInForce":  "GTD",
            "gtdTime":      gtd,
            "positionFill": "DEFAULT",
            "stopLossOnFill":   {"price": format_price_for_instrument(sl,  pair)},
            "takeProfitOnFill": {"price": format_price_for_instrument(tp,  pair)},
            "clientExtensions": {
                "comment": f"{label} {action} stop @ {entry}"[:128],
                "tag":     f"{label.lower()}-stop"
            }
        }
    }

    try:
        req = orders_module.OrderCreate(accountID=OANDA_ACCOUNT_ID, data=payload)
        oanda_client.request(req)
        oid = req.response.get("orderCreateTransaction", {}).get("id")
        print(f"  [{label}] {action} STOP placed: {pair} @ {entry} "
              f"SL={sl} TP={tp} | id={oid} | expires={gtd}")
        return oid
    except Exception as e:
        print(f"  [{label}] {action} STOP failed for {pair} @ {entry}: {e}")
        return None


def place_dual_stop_orders(
    pair: str,
    action: str,
    units: int,
    support: float,
    resistance: float,
    sl: float,
    tp: float,
    expiry_minutes: int = 1440,
) -> list[str]:
    """
    Places TWO STOP orders for a trending pair — covers both scenarios:

    For BUY direction (bullish):
      1. S4-BOUNCE: STOP BUY just above SUPPORT
         → fills if price dips to support and bounces back up (pullback entry)
      2. S4-BREAK:  STOP BUY just above RESISTANCE
         → fills if price breaks above resistance (breakout entry)

    For SELL direction (bearish):
      1. S4-BOUNCE: STOP SELL just below RESISTANCE
         → fills if price pops to resistance and drops (pullback entry)
      2. S4-BREAK:  STOP SELL just below SUPPORT
         → fills if price breaks below support (breakdown entry)

    Returns list of placed order IDs.
    Note: if BOTH fire, you'll hold two positions. Cancel one manually
    from the OANDA HUB once the first fills, or reduce units by half.
    """
    dp  = 3 if pair.endswith("_JPY") else 5
    pip = 0.01 if pair.endswith("_JPY") else 0.0001
    buf = pip * 10   # 3-pip confirmation buffer

    order_ids = []

    if action == "BUY":
        orders_to_place = [
            ("S4-BOUNCE", "BUY", round(support    + buf, dp)),
            ("S4-BREAK",  "BUY", round(resistance + buf, dp)),
        ]
    else:
        orders_to_place = [
            ("S4-BOUNCE", "SELL", round(resistance - buf, dp)),
            ("S4-BREAK",  "SELL", round(support    - buf, dp)),
        ]

    print(f"\n  [S4] Dual stop orders for {pair} ({action}):")
    print(f"    Support: {support} | Resistance: {resistance}")

    for label, direction, entry in orders_to_place:
        oid = place_stop_entry_order(
            pair, direction, units, entry, sl, tp,
            expiry_minutes=expiry_minutes,
            label=label
        )
        if oid:
            order_ids.append(oid)

    print(f"  [S4] {len(order_ids)}/2 stop orders placed for {pair}")
    return order_ids