"""
Examples:
  python close_all.py
"""

from oandapyV20 import API
import oandapyV20.endpoints.positions as positions
import oandapyV20.endpoints.orders as orders

from utils.utils import GREEN, RED, RESET

PROTECTED_ACCOUNT = "1"

from config_oanda import (
    OANDA_ENV,
    OANDA_API_TOKEN,
    OANDA_ACCOUNT_ID_1,
    OANDA_ACCOUNT_ID_2,
    OANDA_ACCOUNT_ID_3,
    OANDA_ACCOUNT_ID_4,
)

ACCOUNTS = {
    "1": OANDA_ACCOUNT_ID_1,
    "2": OANDA_ACCOUNT_ID_2,
    "3": OANDA_ACCOUNT_ID_3,
    "4": OANDA_ACCOUNT_ID_4,
}

oanda_client = API(
    access_token=OANDA_API_TOKEN,
    environment=OANDA_ENV,
)

def select_account():
    print("\n=== EMERGENCY CLOSE ALL ===\n")

    print(f"1: {OANDA_ACCOUNT_ID_1}")
    print(f"2: {OANDA_ACCOUNT_ID_2}")
    print(f"3: {OANDA_ACCOUNT_ID_3}")
    print(f"4: {OANDA_ACCOUNT_ID_4}")
    print("0: Exit")

    while True:
        choice = input("\nSelect option: ").strip()

        if choice == "0":
            return None

        if choice in ACCOUNTS:
            return ACCOUNTS[choice]

        print("Invalid selection.")

def close_all(account_id: str):
    print(f"\n=== Account: {account_id} | Cancel pending orders ===")
    cancel_pending_orders(account_id)

    print(f"\n=== Account: {account_id} | Close open positions ===")
    close_open_positions(account_id)

    print("\nDone.")
    
def cancel_pending_orders(account_id: str, instrument: str | None = None):
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


def close_open_positions(account_id: str, instrument: str | None = None):
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

def show_account_summary(account_id):
    try:
        req = positions.OpenPositions(accountID=account_id)
        oanda_client.request(req)

        pos = req.response.get("positions", [])

        if not pos:
            print("  Positions: None")
            return

        print("  Positions:")

        for p in pos:
            pair = p["instrument"]

            long_u = int(float(p.get("long", {}).get("units", 0)))
            short_u = int(float(p.get("short", {}).get("units", 0)))

            long_pl = float(p.get("long", {}).get("unrealizedPL", 0))
            short_pl = float(p.get("short", {}).get("unrealizedPL", 0))

            total_pl = long_pl + short_pl

            upl_text = (
                f"{GREEN}{total_pl:.2f}{RESET}"
                if total_pl >= 0
                else f"{RED}{total_pl:.2f}{RESET}"
            )
            
            print(
                f"    {pair:<10} "
                f"Long:{long_u:<8} "
                f"Short:{short_u:<8} "
                # f"P/L:{total_pl:.2f}"
                f"P/L:{upl_text}"
            )

    except Exception as e:
        print(f"  ERROR: {e}")
        
def show_menu():
    print("\n========== EMERGENCY CLOSE ALL ==========\n")

    print(f"1: {OANDA_ACCOUNT_ID_1} 🔒 PROTECTED")
    show_account_summary(OANDA_ACCOUNT_ID_1)
    print()

    print(f"2: {OANDA_ACCOUNT_ID_2}")
    show_account_summary(OANDA_ACCOUNT_ID_2)
    print()

    print(f"3: {OANDA_ACCOUNT_ID_3}")
    show_account_summary(OANDA_ACCOUNT_ID_3)
    print()

    print(f"4: {OANDA_ACCOUNT_ID_4}")
    show_account_summary(OANDA_ACCOUNT_ID_4)
    print()

    print("0: Exit")
        
if __name__ == "__main__":

    while True:
        show_menu()

        choice = input("Select option: ").strip()

        if choice == "0":
            print("Bye.")
            break

        if choice not in ACCOUNTS:
            print("Invalid selection.")
            continue

        if choice == PROTECTED_ACCOUNT:
            print(
                f"\n*** Account {ACCOUNTS[choice]} is PROTECTED. ***"
            )
            print("Viewing only. Close-all operation is disabled.")
            input("\nPress Enter to return to menu...")
            continue

        account_id = ACCOUNTS[choice]

        close_all(account_id)

        input("\nPress Enter to return to menu...")
