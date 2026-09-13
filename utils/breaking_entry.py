"""Breaking-entry helpers: detect prolonged ranges and place stop-entry orders.

Self-contained to avoid circular imports. Uses OANDA REST endpoints directly.

Strategy: when a pair has been consolidating inside a tight range for
BREAKOUT_DURATION_HOURS, place a BUY STOP above range high and a SELL STOP
below range low with ATR-based SL/TP. Whichever fires first is the breakout
entry; cancel the other manually or it expires GTC.

width_pct is a RATIO (not a percentage): 0.005 = 0.5% of price.
On GBP/JPY ~215, that is ~107 pips — a tight but realistic consolidation box.
"""
import importlib
from oandapyV20 import API
from config import OANDA_ACCOUNT_ID, OANDA_ENV, OANDA_API_TOKEN

try:
    orders            = importlib.import_module("oandapyV20.endpoints.orders")
    instruments_module = importlib.import_module("oandapyV20.endpoints.instruments")
except Exception:
    orders             = None
    instruments_module = None

oanda_client = API(access_token=OANDA_API_TOKEN, environment=OANDA_ENV)


# Canonical price formatter — duplicated from main.py intentionally
# to keep this module self-contained and avoid circular imports.
def format_price_for_instrument(price, instrument: str) -> str:
    try:
        numeric_price = float(price)
    except (TypeError, ValueError):
        return str(price)
    return f"{numeric_price:.3f}" if instrument.endswith("_JPY") else f"{numeric_price:.5f}"


def get_open_units(instrument: str) -> tuple[float, float]:
    """Returns (long_units, short_units) for an instrument, (0, 0) if flat or error."""
    try:
        positions_module = importlib.import_module("oandapyV20.endpoints.positions")
        req = positions_module.OpenPositions(accountID=OANDA_ACCOUNT_ID)
        oanda_client.request(req)
        for pos in req.response.get("positions", []):
            if pos.get("instrument") == instrument:
                return (
                    float(pos.get("long",  {}).get("units", 0)),
                    float(pos.get("short", {}).get("units", 0)),
                )
    except Exception as e:
        print(f"[BREAKENTRY] Could not check open positions: {e}")
    return 0.0, 0.0


def detect_protracted_range(
    instrument: str,
    granularity: str  = "H1",
    duration_hours: int = 72,
    width_pct: float    = 0.005,   # ratio, not percent: 0.005 = 0.5%
) -> dict | None:
    """
    Returns range metrics if the pair has been consolidating tightly,
    otherwise None.

    Keys in returned dict:
      is_ranged, range_high, range_low, range_width, mean_price, atr
    """
    if instruments_module is None:
        print("[BREAKENTRY] oandapyV20 instruments endpoint not available.")
        return None

    try:
        params  = {"count": duration_hours + 2, "granularity": granularity}
        request = instruments_module.InstrumentsCandles(instrument=instrument, params=params)
        oanda_client.request(request)
        candles = [c for c in request.response.get("candles", []) if c.get("complete")]
        if len(candles) < duration_hours:
            return None

        highs  = [float(c["mid"]["h"]) for c in candles]
        lows   = [float(c["mid"]["l"]) for c in candles]
        closes = [float(c["mid"]["c"]) for c in candles]

        range_high  = max(highs)
        range_low   = min(lows)
        range_width = range_high - range_low
        mean_price  = sum(closes) / len(closes)

        # ATR over the same period for SL/TP sizing
        trs = [
            max(highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i]  - closes[i-1]))
            for i in range(1, len(candles))
        ]
        atr = sum(trs[-14:]) / min(14, len(trs)) if trs else range_width

        is_ranged = (range_width / mean_price) <= width_pct

        return {
            "is_ranged":   is_ranged,
            "range_high":  range_high,
            "range_low":   range_low,
            "range_width": range_width,
            "mean_price":  mean_price,
            "atr":         atr,
        }
    except Exception as e:
        print(f"[BREAKENTRY] Range detection failed for {instrument}: {e}")
        return None


def place_breakout_stop_orders(
    instrument: str,
    units: int,
    duration_hours: int = 72,
    width_pct: float    = 0.005,
) -> list[str]:
    """
    Places BUY STOP above range high and SELL STOP below range low
    when the pair has been in a protracted tight consolidation.

    SL for BUY  = range_low  - ATR * 0.5  (below the consolidation)
    TP for BUY  = entry      + range_width * 2  (2× breakout projection)
    (mirrored for SELL)

    Returns list of created order IDs (may be empty).
    """
    if orders is None:
        print("[BREAKENTRY] oandapyV20 orders endpoint not available.")
        return []

    # Guard: skip if already holding this instrument
    long_u, short_u = get_open_units(instrument)
    if long_u != 0 or short_u != 0:
        print(f"[BREAKENTRY] Open position exists for {instrument} "
              f"(long={long_u}, short={short_u}). Skipping stop orders.")
        return []

    info = detect_protracted_range(
        instrument, "H1",
        duration_hours=duration_hours,
        width_pct=width_pct
    )

    if not info:
        print(f"[BREAKENTRY] Could not fetch range data for {instrument}.")
        return []

    if not info["is_ranged"]:
        rw_pct = info["range_width"] / info["mean_price"] * 100
        print(f"[BREAKENTRY] {instrument} not in protracted range "
              f"({rw_pct:.2f}% width > {width_pct*100:.2f}% threshold). Skipping.")
        return []

    top     = info["range_high"]
    bottom  = info["range_low"]
    atr     = info["atr"]
    rw      = info["range_width"]
    dp      = 3 if instrument.endswith("_JPY") else 5

    # Entry: just outside range boundaries
    buy_entry  = round(top    + atr * 0.1, dp)
    sell_entry = round(bottom - atr * 0.1, dp)

    # SL: beyond the opposite side of the range
    buy_sl  = round(bottom - atr * 0.5, dp)
    sell_sl = round(top    + atr * 0.5, dp)

    # TP: 2× range width projection from entry
    buy_tp  = round(buy_entry  + rw * 2, dp)
    sell_tp = round(sell_entry - rw * 2, dp)

    print(f"[BREAKENTRY] {instrument} | {duration_hours}h range "
          f"{bottom:.5f}–{top:.5f} ({rw/info['mean_price']*100:.3f}% width)")
    print(f"  BUY  STOP: entry={buy_entry}  SL={buy_sl}  TP={buy_tp}")
    print(f"  SELL STOP: entry={sell_entry} SL={sell_sl} TP={sell_tp}")

    account_id = OANDA_ACCOUNT_ID
    order_ids  = []

    for direction, entry, sl, tp, order_units in [
        ("BUY",  buy_entry,  buy_sl,  buy_tp,   units),
        ("SELL", sell_entry, sell_sl, sell_tp,  -units),
    ]:
        payload = {
            "order": {
                "instrument":   instrument,
                "units":        str(order_units),
                "price":        format_price_for_instrument(entry, instrument),
                "type":         "STOP",
                "timeInForce":  "GTC",
                "positionFill": "DEFAULT",
                "stopLossOnFill": {
                    "price": format_price_for_instrument(sl, instrument)
                },
                "takeProfitOnFill": {
                    "price": format_price_for_instrument(tp, instrument)
                },
                "clientExtensions": {
                    "comment": f"S4 breakout {direction} @ {entry}"[:128],
                    "tag":     "breaking-entry"
                }
            }
        }
        try:
            req = orders.OrderCreate(accountID=account_id, data=payload)
            oanda_client.request(req)
            txn = req.response.get("orderCreateTransaction", {})
            oid = txn.get("id") or req.response.get("relatedTransactionIDs", [None])[0]
            print(f"[BREAKENTRY] {direction} STOP placed | ID {oid}")
            if oid:
                order_ids.append(oid)
        except Exception as e:
            print(f"[BREAKENTRY] {direction} STOP failed: {e}")

    return order_ids