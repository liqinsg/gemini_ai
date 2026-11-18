#!/usr/bin/env python3
"""Purpose: manually verify one explicitly specified OANDA Practice/demo
open-and-close round trip through TradingCore.execute_market_trade() and
TradingCore.close_trade_by_id(), the close method reached by the v31 runner's
ownership-gated close path.

Risk: when deliberately enabled, this script WILL submit one market order to
the OANDA Practice/demo environment and then attempt to close that exact newly
filled trade ID. It does not use the Live environment or real-money account.
Demo fills and prices may differ from Live. Simulated spread/slippage and a
residual demo position are still possible if the process stops or OANDA rejects
the close. It checks that the selected instrument has no existing demo trade
or pending order before entry and performs an offline SDK-construction
preflight before any order request. This file is outside pytest's test discovery
paths and has no action unless --practice, all explicit order parameters, and
the exact confirmation phrase are supplied.

On any uncertain or failed close, this script does NOT retry. Use the OANDA
platform to inspect only the reported trade ID and instrument; manually close
that exact residual trade if it is still open. Do not rerun the script to
clean it up.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CONFIRMATION = "I ACCEPT PRACTICE ORDER RISK"
INSTRUMENT_RE = re.compile(r"^[A-Z]{3}_[A-Z]{3}$")


def _positive_units(value: str) -> int:
    try:
        units = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("units must be a positive integer") from exc
    if units <= 0:
        raise argparse.ArgumentTypeError("units must be a positive integer")
    return units


def _positive_pips(value: str) -> float:
    try:
        pips = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("pip distance must be a positive number") from exc
    if not math.isfinite(pips) or pips <= 0:
        raise argparse.ArgumentTypeError("pip distance must be a positive number")
    return pips


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One explicitly scoped OANDA Practice/demo TradeClose smoke test."
    )
    parser.add_argument(
        "--practice",
        action="store_true",
        help="Required acknowledgement that the OANDA Practice/demo environment will be used.",
    )
    parser.add_argument(
        "--account-id", required=True, help="Explicit configured Practice/demo account ID."
    )
    parser.add_argument("--instrument", required=True, help="Explicit OANDA pair, e.g. EUR_USD.")
    parser.add_argument("--side", required=True, choices=("BUY", "SELL"))
    parser.add_argument("--units", required=True, type=_positive_units)
    parser.add_argument("--sl-pips", required=True, type=_positive_pips)
    parser.add_argument("--tp-pips", required=True, type=_positive_pips)
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


class _PreflightFakeClient:
    """In-memory sink used to validate SDK request construction without I/O."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def request(self, endpoint: Any) -> dict[str, Any]:
        self.requests.append(endpoint)
        return {"tradesClosed": [{"tradeID": "PREFLIGHT-TRADE", "units": "1"}]}


def _preflight_close_constructors(TradingCore: Any) -> tuple[bool, str]:
    """Exercise both close constructors against a fake client before any API I/O."""
    fake = _PreflightFakeClient()
    core = TradingCore(
        oanda_client=fake,
        oanda_account_id="PREFLIGHT-ACCOUNT",
        dry_run=False,
        market_closed=False,
    )
    full_ok, full_info = core.close_trade_by_id(
        "PREFLIGHT-TRADE", client_request_id="PREFLIGHT_FULL_CLOSE"
    )
    if not full_ok or len(fake.requests) != 1:
        return False, f"full-close request construction failed: {full_info}"

    partial_ok, partial_info = core.close_trade_partial(
        "PREFLIGHT-TRADE", units=1, client_request_id="PREFLIGHT_PARTIAL_CLOSE"
    )
    if not partial_ok or len(fake.requests) != 2:
        return False, f"partial-close request construction failed: {partial_info}"
    return True, "full and partial TradeClose requests constructed and sent to fake client only"


def main() -> int:
    args = _parse_args()

    # Fail closed before loading account configuration or constructing a client.
    if not args.practice or args.confirm != CONFIRMATION:
        print("REFUSED: Practice execution requires --practice and the exact --confirm phrase.")
        print("No OANDA client was created and no request was sent.")
        return 2

    instrument = args.instrument.strip().upper()
    if not INSTRUMENT_RE.fullmatch(instrument):
        print("REFUSED: --instrument must use the explicit BASE_QUOTE form, e.g. EUR_USD.")
        return 2
    try:
        from utils.trading_core_v2 import TradingCore

        preflight_ok, preflight_info = _preflight_close_constructors(TradingCore)
        if not preflight_ok:
            print(
                "REFUSED before API access: local TradeClose SDK preflight failed. "
                f"{preflight_info}"
            )
            print("No OANDA client was created and no network request was sent.")
            return 2
        print(f"Offline SDK preflight passed: {preflight_info}.")

        # config_oanda.py builds a module-level default client during import.
        # Pin this isolated process to Practice before importing it so even
        # that default client cannot select the Live environment.
        os.environ["OANDA_ENV"] = "practice"
        from config_oanda import get_oanda_profile

        # Use config_oanda.py's supported environment/client interface; do not
        # instantiate an independent API client in this script.
        profile = get_oanda_profile("practice")
        client = profile.get("oanda_client")
        configured_accounts = set(profile.get("account_ids") or [])
        if not client:
            print("REFUSED: OANDA Practice client is unavailable; check local environment configuration.")
            return 2
        if args.account_id not in configured_accounts:
            print("REFUSED: --account-id is not one of the configured Practice/demo account IDs.")
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
    try:
        from config_bot import PIP_SIZE_BY_QUOTE

        quote_ccy = instrument.rsplit("_", 1)[1]
        pip_size = float(PIP_SIZE_BY_QUOTE[quote_ccy])
    except (KeyError, IndexError, TypeError, ValueError):
        print(f"REFUSED: no configured pip size for {instrument}.")
        return 2

    # Read the current executable side from the configured Practice client;
    # use ask for BUY and bid for SELL, matching market-entry direction.
    bid, ask = core.get_bid_ask(instrument)
    entry_reference = ask if args.side == "BUY" else bid
    if entry_reference is None or not math.isfinite(entry_reference) or entry_reference <= 0:
        print(f"REFUSED: no valid Practice bid/ask available for {instrument}; no order submitted.")
        return 2

    if args.side == "BUY":
        stop_loss = entry_reference - args.sl_pips * pip_size
        take_profit = entry_reference + args.tp_pips * pip_size
    else:
        stop_loss = entry_reference + args.sl_pips * pip_size
        take_profit = entry_reference - args.tp_pips * pip_size
    if stop_loss <= 0 or take_profit <= 0:
        print("REFUSED: calculated stop-loss/take-profit price is not positive.")
        return 2
    stop_loss = float(core.format_price_for_instrument(stop_loss, instrument))
    take_profit = float(core.format_price_for_instrument(take_profit, instrument))

    client_extension_id = "MCS" + uuid.uuid4().hex[:24]
    client_request_id = "MCS_CLOSE_" + uuid.uuid4().hex[:20]
    print(
        f"Submitting the explicitly requested Practice/demo test: {args.side} {instrument}, "
        f"units={args.units}; reference={entry_reference}; SL={args.sl_pips} pips "
        f"({stop_loss}); TP={args.tp_pips} pips ({take_profit}). "
        "Account ID and credentials are not printed."
    )

    try:
        result = core.execute_market_trade(
            instrument=instrument,
            action=args.side,
            units=args.units,
            stop_loss=stop_loss,
            take_profit=take_profit,
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
