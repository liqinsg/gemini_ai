"""
Emergency close-all tool for all OANDA accounts across demo + live.

Examples:
  python close_all_v1.py              # show all demo + live accounts
  python close_all_v1.py --demo       # demo accounts only
  python close_all_v1.py --live       # live accounts only
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from oandapyV20 import API
import oandapyV20.endpoints.positions as positions
import oandapyV20.endpoints.orders as orders

from utils.utils import GREEN, RED, RESET
from config_oanda import get_oanda_profile


@dataclass
class AccountEntry:
    index: int
    env: str
    env_label: str
    client: API
    account_id: str
    is_primary: bool


def _collect_accounts(env_filter: str | None) -> list[AccountEntry]:
    envs: list[tuple[str, str]] = []
    if env_filter is None or env_filter in ("demo", "practice"):
        envs.append(("demo", "DEMO"))
    if env_filter is None or env_filter == "live":
        envs.append(("live", "LIVE"))

    entries: list[AccountEntry] = []
    counter = 1

    for env_override, label in envs:
        profile = get_oanda_profile(env_override=env_override)
        client: API = profile["api"] or API(
            access_token=profile["token"], environment=profile["env"]
        )
        for idx, acc_id in enumerate(profile["account_ids"]):
            entries.append(
                AccountEntry(
                    index=counter,
                    env=profile["env"],
                    env_label=label,
                    client=client,
                    account_id=acc_id,
                    is_primary=(idx == 0),
                )
            )
            counter += 1

    return entries


def show_account_summary(client: API, account_id: str):
    try:
        req = positions.OpenPositions(accountID=account_id)
        client.request(req)

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
                f"P/L:{upl_text}"
            )

    except Exception as e:
        print(f"  ERROR: {e}")


def show_menu(entries: list[AccountEntry]):
    print("\n========== EMERGENCY CLOSE ALL ==========\n")

    for e in entries:
        env_tag = (
            f"{GREEN}[{e.env_label}]{RESET}"
            if e.env_label == "DEMO"
            else f"{RED}[{e.env_label}]{RESET}"
        )
        prot_tag = "🔒 PROTECTED" if e.is_primary else ""
        print(f"{e.index}: {env_tag} {e.account_id} {prot_tag}".rstrip())
        show_account_summary(e.client, e.account_id)
        print()

    print("0: Exit")


def close_all(entry: AccountEntry):
    print(
        f"\n=== [{entry.env_label}] {entry.account_id} "
        f"| Cancel pending orders ==="
    )
    cancel_pending_orders(entry.client, entry.account_id)

    print(
        f"\n=== [{entry.env_label}] {entry.account_id} "
        f"| Close open positions ==="
    )
    close_open_positions(entry.client, entry.account_id)

    print("\nDone.")


def cancel_pending_orders(client: API, account_id: str, instrument: str | None = None):
    req = orders.OrderList(accountID=account_id)
    client.request(req)

    pending_orders = req.response.get("orders", [])

    if instrument:
        pending_orders = [
            o for o in pending_orders if o.get("instrument") == instrument
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
            client.request(req)
            print(f"  CANCELED order {order_id} | {pair}")
        except Exception as e:
            print(f"  ERROR canceling order {order_id} | {pair}: {e}")


def close_open_positions(client: API, account_id: str, instrument: str | None = None):
    req = positions.OpenPositions(accountID=account_id)
    client.request(req)

    open_positions = req.response.get("positions", [])

    if instrument:
        open_positions = [
            p for p in open_positions if p.get("instrument") == instrument
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
            client.request(req)

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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Emergency close-all tool")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--demo",
        action="store_true",
        help="Show demo/practice accounts only",
    )
    group.add_argument(
        "--live",
        action="store_true",
        help="Show live accounts only",
    )
    args = parser.parse_args()

    env_filter: str | None = None
    if args.demo:
        env_filter = "demo"
    elif args.live:
        env_filter = "live"

    entries = _collect_accounts(env_filter)

    if not entries:
        print("❌ No accounts available. Check run.env or .env")
        sys.exit(1)

    demo_count = sum(1 for e in entries if e.env_label == "DEMO")
    live_count = sum(1 for e in entries if e.env_label == "LIVE")
    filter_label = (
        "(all)" if env_filter is None else env_filter.upper()
    )
    print(
        f"🌍 Scope: {filter_label} | "
        f"DEMO: {demo_count} | LIVE: {live_count} | "
        f"Total: {len(entries)}"
    )

    while True:
        show_menu(entries)

        choice = input("Select option: ").strip()

        if choice == "0":
            print("Bye.")
            break

        try:
            selected_idx = int(choice)
        except ValueError:
            print("Invalid selection.")
            continue

        matching = [e for e in entries if e.index == selected_idx]
        if not matching:
            print("Invalid selection.")
            continue

        entry = matching[0]

        if entry.is_primary:
            print(
                f"\n*** [{entry.env_label}] {entry.account_id} is PROTECTED. ***"
            )
            print("Viewing only. Close-all operation is disabled.")
            input("\nPress Enter to return to menu...")
            continue

        close_all(entry)

        input("\nPress Enter to return to menu...")