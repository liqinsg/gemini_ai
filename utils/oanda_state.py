"""Stateless OANDA runtime-state helpers for order entry."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Mapping

import oandapyV20.endpoints.orders as orders_ep
import oandapyV20.endpoints.trades as trades_ep


def _normalise_signal_time(value: Any) -> str:
    """Return a stable, OANDA-safe signal/bar identifier."""
    if isinstance(value, datetime):
        value = value.astimezone(timezone.utc).strftime("%Y%m%d_%H%M")
    value = str(value or "").strip()
    return "".join(char for char in value if char.isalnum() or char == "_")


def build_client_extensions(signal: Mapping[str, Any], strategy_tag: str = "jpy_strength") -> dict:
    """Build deterministic OANDA metadata for one signal/bar event."""
    instrument = str(signal.get("pair", signal.get("instrument", "UNKNOWN"))).upper()
    instrument = instrument.replace("/", "_")
    bar_time = _normalise_signal_time(
        signal.get("signal_time")
        or signal.get("bar_time")
        or signal.get("timestamp")
        or signal.get("time")
    )
    fingerprint_source = "|".join(
        str(signal.get(key, ""))
        for key in ("action", "entry", "stop_loss", "take_profit", "reasoning")
    )
    fingerprint = sha256(fingerprint_source.encode("utf-8")).hexdigest()[:12].upper()
    client_id = f"{strategy_tag.upper()}_{instrument}_{bar_time or fingerprint}"
    comment = (
        f"action={signal.get('action', '')}; bar={bar_time or 'unspecified'}; "
        f"signal={fingerprint}"
    )
    return {"id": client_id[:128], "tag": strategy_tag[:128], "comment": comment[:128]}


def get_open_trades_and_orders(api_client, account_id: str, instrument: str) -> tuple[list[dict], list[dict]]:
    """Fetch open trades and pending orders for one instrument directly from OANDA."""
    trades_response = api_client.request(
        trades_ep.TradesList(account_id, params={"instrument": instrument, "state": "OPEN"})
    )
    orders_response = api_client.request(
        orders_ep.OrderList(account_id, params={"instrument": instrument, "state": "PENDING"})
    )
    return trades_response.get("trades", []), orders_response.get("orders", [])


def has_open_trade_or_order(api_client, account_id: str, instrument: str) -> bool:
    """Return whether OANDA currently reports any active trade or pending order."""
    trades, orders = get_open_trades_and_orders(api_client, account_id, instrument)
    return bool(trades or orders)