"""
Emergency close/cancel.

Examples:
  python close_all.py
  python close_all.py USD_JPY
  python close_all.py GBP_USD --yes

No pair = close/cancel ALL instruments.
Pair = close/cancel only that instrument.
"""

import os
import argparse
from oandapyV20 import API
import oandapyV20.endpoints.positions as positions
import oandapyV20.endpoints.orders as orders

from config import (
    OANDA_ENV, OANDA_API_TOKEN, OANDA_ACCOUNT_ID
)
oanda_env = OANDA_ENV
oanda_client = API(
    access_token=os.getenv("OANDA_API_TOKEN"),
    environment=oanda_env,
)
account_id = os.getenv("OANDA_ACCOUNT_ID")


def cancel_pending_orders(instrument: str | None = None):
    req = orders.OrderList(accountID=account_id)
    oanda_client.request(req)

    pending_orders = req.response.get("orders", [])

    if instrument:
        pending_orders = [
            o for o in pending_orders
            if o.get("instrument") == instrument
        ]

    if not pending_orders:
        print("No pending orders to cancel.")
        return

    print(f"Found {len(pending_orders)} pending order(s) to cancel:\n")

    for order in pending_orders:
        order_id = order.get("id")
        pair = order.get("instrument")
        order_type = order.get("type")
        price = order.get("price")
        units = order.get("units")

        print(f"  Order {order_id} | {pair} | {order_type} | {units} @ {price}")

    print()

    for order in pending_orders:
        order_id = order.get("id")
        pair = order.get("instrument")

        try:
            req = orders.OrderCancel(
                accountID=account_id,
                orderID=order_id,
            )
            oanda_client.request(req)
            print(f"  CANCELED order {order_id} | {pair}")
        except Exception as e:
            print(f"  ERROR canceling order {order_id} | {pair}: {e}")


def close_open_positions(instrument: str | None = None):
    req = positions.OpenPositions(accountID=account_id)
    oanda_client.request(req)

    open_positions = req.response.get("positions", [])

    if instrument:
        open_positions = [
            p for p in open_positions
            if p.get("instrument") == instrument
        ]

    if not open_positions:
        print("No open positions to close.")
        return

    print(f"Found {len(open_positions)} open position(s) to close:\n")

    for p in open_positions:
        pair = p["instrument"]
        long_u = int(float(p.get("long", {}).get("units", 0)))
        short_u = int(float(p.get("short", {}).get("units", 0)))
        long_pl = p.get("long", {}).get("unrealizedPL", "0")
        short_pl = p.get("short", {}).get("unrealizedPL", "0")

        print(
            f"  {pair} | long: {long_u} units (P&L {long_pl}) | "
            f"short: {short_u} units (P&L {short_pl})"
        )

    print()

    for p in open_positions:
        pair = p["instrument"]
        long_u = int(float(p.get("long", {}).get("units", 0)))
        short_u = int(float(p.get("short", {}).get("units", 0)))

        payload = {}

        if long_u > 0:
            payload["longUnits"] = str(long_u)

        if short_u < 0:
            payload["shortUnits"] = str(abs(short_u))

        if not payload:
            print(f"  {pair}: nothing to close.")
            continue

        try:
            req = positions.PositionClose(
                accountID=account_id,
                instrument=pair,
                data=payload,
            )
            oanda_client.request(req)

            fills = []
            for side in ("longOrderFillTransaction", "shortOrderFillTransaction"):
                txn = req.response.get(side, {})
                if txn:
                    fills.append(
                        f"{txn.get('units')} units @ {txn.get('price')} "
                        f"(P&L {txn.get('pl')})"
                    )

            print(f"  CLOSED {pair}: {' | '.join(fills) if fills else 'done'}")

        except Exception as e:
            print(f"  ERROR closing {pair}: {e}")


def close_all(instrument: str | None = None, yes: bool = False):
    target = instrument or "ALL instruments"

    if not yes:
        confirm = input(
            f"Type 'CLOSE ALL' to close positions and cancel pending orders for {target}: "
        )
        if confirm.strip() != "CLOSE ALL":
            print("Aborted. Nothing changed.")
            return

    print(f"\n=== Cancel pending orders: {target} ===")
    cancel_pending_orders(instrument)

    print(f"\n=== Close open positions: {target} ===")
    close_open_positions(instrument)

    print("\nDone.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "instrument",
        nargs="?",
        help="Optional pair/instrument, e.g. USD_JPY. If omitted, closes/cancels all.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt.",
    )

    args = parser.parse_args()
    close_all(args.instrument, yes=args.yes)
    