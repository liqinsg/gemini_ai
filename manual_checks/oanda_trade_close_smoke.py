#!/usr/bin/env python3
"""Purpose: manually verify one explicitly specified live OANDA open-and-close
round trip through TradingCore.execute_market_trade() and
TradingCore.close_trade_by_id(), the close method reached by the v31 runner's
ownership-gated close path.

Risk: when deliberately enabled, this script WILL submit one real market order
and then attempt to close that exact newly filled trade ID. It checks that the
selected instrument has no existing open trade or pending order before entry,
but market prices can move between order submission and fill. Even a quick
round trip can incur spread, slippage, commission, financing/margin effects,
and an unintended residual position if the fill response has no trade ID, the
process stops, OANDA rejects the close, or the close response is ambiguous.
Protective stop-loss/take-profit prices are required, but gaps and execution
conditions can still produce losses. This file is outside pytest's test
discovery paths and has no action unless --live, all explicit order parameters,
and the exact confirmation phrase are supplied.

On any uncertain or failed close, this script does NOT retry. Use the OANDA
platform to inspect only the reported trade ID and instrument; manually close
that exact residual trade if it is still open. Do not rerun the script to
clean it up.
"""

from __future__ import annotations

import argparse
import re
import sys
import uuid
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CONFIRMATION = "I ACCEPT LIVE ORDER RISK"
INSTRUMENT_RE = re.compile(r"^[A-Z]{3}_[A-Z]{3}$")


def _positive_units(value: str) -> int:
    try:
        units = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("units must be a positive integer") from exc
    if units <= 0:
        raise argparse.ArgumentTypeError("units must be a positive integer")
    return units


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One explicitly scoped live TradeClose compatibility smoke test."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Required acknowledgement that the configured live environment will be used.",
    )
    parser.add_argument("--account-id", required=True, help="Explicit configured live account ID.")
    parser.add_argument("--instrument", required=True, help="Explicit OANDA pair, e.g. EUR_USD.")
    parser.add_argument("--side", required=True, choices=("BUY", "SELL"))
    parser.add_argument("--units", required=True, type=_positive_units)
    parser.add_argument("--stop-loss", required=True, type=float)
    parser.add_argument("--take-profit", required=True, type=float)
    parser.add_argument(
        "--confirm",
        default="",
        help=f"Must exactly equal: {CONFIRMATION}",
    )
    return parser.parse_args()


def _has_same_instrument_exposure(client: Any, account_id: str, instrument: str) -> bool:
    """Read-only preflight; returns True for an open trade or pending order."""
    import oandapyV20.endpoints.orders as orders_endpoints
    import oandapyV20.endpoints.trades as trades_endpoints

    open_trades = client.request(trades_endpoints.OpenTrades(accountID=account_id))
    for trade in (open_trades or {}).get("trades", []):
        if trade.get("instrument") == instrument:
            return True

    pending_orders = client.request(orders_endpoints.OrdersPending(accountID=account_id))
    for order in (pending_orders or {}).get("orders", []):
        if order.get("instrument") == instrument:
            return True
    return False


def _closed_trade_matches(response: Any, trade_id: str) -> bool:
    if not isinstance(response, dict):
        return False
    closed = response.get("tradesClosed")
    if not isinstance(closed, list):
        return False
    return any(str(item.get("tradeID", "")) == trade_id for item in closed if isinstance(item, dict))


def main() -> int:
    args = _parse_args()

    # Fail closed before loading account configuration or constructing a client.
    if not args.live or args.confirm != CONFIRMATION:
        print("REFUSED: live execution requires --live and the exact --confirm phrase.")
        print("No OANDA client was created and no request was sent.")
        return 2

    instrument = args.instrument.strip().upper()
    if not INSTRUMENT_RE.fullmatch(instrument):
        print("REFUSED: --instrument must use the explicit BASE_QUOTE form, e.g. EUR_USD.")
        return 2
    if args.stop_loss <= 0 or args.take_profit <= 0:
        print("REFUSED: stop-loss and take-profit must be positive prices.")
        return 2
    if args.side == "BUY" and not args.stop_loss < args.take_profit:
        print("REFUSED: BUY requires stop-loss below take-profit.")
        return 2
    if args.side == "SELL" and not args.stop_loss > args.take_profit:
        print("REFUSED: SELL requires stop-loss above take-profit.")
        return 2

    try:
        from config_oanda import get_oanda_profile
        from utils.trading_core_v2 import TradingCore

        profile = get_oanda_profile("live")
        client = profile.get("oanda_client")
        configured_accounts = set(profile.get("account_ids") or [])
        if not client:
            print("REFUSED: live OANDA client is unavailable; check local environment configuration.")
            return 2
        if args.account_id not in configured_accounts:
            print("REFUSED: --account-id is not one of the configured live account IDs.")
            return 2
    except Exception as exc:
        print(f"REFUSED: local OANDA configuration could not be loaded ({type(exc).__name__}).")
        return 2

    try:
        if _has_same_instrument_exposure(client, args.account_id, instrument):
            print(
                f"REFUSED: {instrument} already has an open trade or pending order. "
                "No position was changed."
            )
            return 2
    except Exception as exc:
        print(
            "REFUSED: read-only preflight failed; no order was submitted "
            f"({type(exc).__name__})."
        )
        return 2

    core = TradingCore(
        oanda_client=client,
        oanda_account_id=args.account_id,
        dry_run=False,
        market_closed=False,
    )
    client_extension_id = "MCS" + uuid.uuid4().hex[:24]
    client_request_id = "MCS_CLOSE_" + uuid.uuid4().hex[:20]
    print(
        f"Submitting the explicitly requested live test: {args.side} {instrument}, "
        f"units={args.units}. Account ID and credentials are not printed."
    )

    try:
        result = core.execute_market_trade(
            instrument=instrument,
            action=args.side,
            units=args.units,
            stop_loss=args.stop_loss,
            take_profit=args.take_profit,
            dry_run=False,
            client_extensions={
                "id": client_extension_id,
                "tag": "MANUAL_TRADE_CLOSE_SMOKE",
                "comment": "One-off explicitly authorized close compatibility test",
            },
        )
    except Exception as exc:
        print(
            "ENTRY RESULT UNCERTAIN. Do not rerun. Check the OANDA platform for "
            f"a fill on {instrument}; error type: {type(exc).__name__}."
        )
        return 3

    if not isinstance(result, dict) or not result.get("trade_id"):
        print(
            "NO CLOSE REQUEST SENT: the entry did not return a confirmed trade ID. "
            f"Check the OANDA platform for {instrument} before taking any further action; "
            "do not rerun this script."
        )
        return 3

    trade_id = str(result["trade_id"])
    print(f"Entry fill returned trade ID {trade_id}; attempting exactly one close request.")
    try:
        close_ok, close_info = core.close_trade_by_id(
            trade_id,
            client_request_id=client_request_id,
        )
    except Exception as exc:
        print(
            f"CLOSE FAILED/UNCERTAIN for exact trade ID {trade_id} ({type(exc).__name__}). "
            f"Inspect {instrument} in OANDA and manually close only this trade ID if it remains open. "
            "No retry was attempted; do not rerun this script."
        )
        return 4

    response = close_info.get("full_response") if isinstance(close_info, dict) else None
    if not close_ok or not _closed_trade_matches(response, trade_id):
        print(
            f"CLOSE FAILED/UNCONFIRMED for exact trade ID {trade_id}. "
            f"Inspect {instrument} in OANDA immediately and manually close only this trade ID "
            "if it remains open. No retry was attempted; do not rerun this script."
        )
        return 4

    print(
        f"Close response confirms trade ID {trade_id} in tradesClosed. "
        "Verify in the OANDA platform that this exact ID is no longer open."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
