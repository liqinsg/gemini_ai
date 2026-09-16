"""Stateless OANDA runtime-state helpers for order entry and duplicate prevention.

Everything here is derived from the broker's own live state — open trades,
pending orders, and closed-trade history — so idempotency and post-exit
context never depend on a local `state/` directory.
"""
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


def build_client_extensions(
    signal: Mapping[str, Any],
    strategy_tag: str = "jpy_strength",
    *,
    bar_time: Any = None,
) -> dict:
    """Build deterministic OANDA metadata for one signal/bar event."""
    instrument = str(signal.get("pair", signal.get("instrument", "UNKNOWN"))).upper()
    instrument = instrument.replace("/", "_")
    bar_time = _normalise_signal_time(
        bar_time
        or signal.get("signal_time")
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
    return (
        get_open_trades(api_client, account_id, instrument=instrument),
        get_pending_orders(api_client, account_id, instrument=instrument),
    )


def has_open_trade_or_order(api_client, account_id: str, instrument: str) -> bool:
    """Return whether OANDA currently reports any active trade or pending order."""
    trades, orders = get_open_trades_and_orders(api_client, account_id, instrument)
    return bool(trades or orders)


# ===========================================================================
# OANDA-AUTHORITATIVE IDEMPOTENCY (no local state files)
#
# Duplicate-entry prevention is decided EXCLUSIVELY from the broker's own live
# view of the account: open trades + pending orders, matched by the strategy
# tag stamped into the order's clientExtensions. Nothing is read from or
# written to a local `state/` directory — a stale, lost, or copied file can
# therefore never cause a duplicate entry, nor suppress a legitimate one.
# ===========================================================================

# Canonical prefix stamped on this strategy's orders (matches the runners'
# STRATEGY_TAG_PREFIX). Matching is dash/underscore-insensitive, so both
# "jpy_strength" (older runners) and "JPY-STRENGTH_GBP_JPY_BUY_20260914"
# (v1.4.4+ runners) are recognised as the same strategy.
STRATEGY_TAG_PREFIX = "JPY-STRENGTH"
OPEN_TRADE_STATE = "OPEN"
PENDING_ORDER_STATE = "PENDING"


def normalize_strategy_tag(tag: Any) -> str:
    """Normalise a strategy tag for tolerant comparison (case/dash/underscore)."""
    return str(tag or "").strip().upper().replace("_", "-")


def strategy_tag_of(record: Mapping[str, Any]) -> str:
    """Read the strategy tag from an OANDA trade/order payload."""
    extensions = record.get("clientExtensions") or {}
    return str(record.get("tag") or extensions.get("tag") or "")


def is_strategy_record(record: Mapping[str, Any], strategy_tag: str = STRATEGY_TAG_PREFIX) -> bool:
    """Return True when `record` was opened by this strategy (tag prefix match)."""
    tagged = normalize_strategy_tag(strategy_tag_of(record))
    wanted = normalize_strategy_tag(strategy_tag)
    return bool(tagged) and tagged.startswith(wanted)


def _units_of(record: Mapping[str, Any]) -> float:
    """Signed units of an OANDA trade payload (currentUnits preferred, else initial)."""
    raw = record.get("currentUnits")
    if raw is None:
        raw = record.get("initialUnits")
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def get_open_trades(api_client, account_id: str, instrument: str | None = None) -> list[dict]:
    """Fetch OPEN trades from OANDA (account-wide, or filtered to one instrument)."""
    params: dict[str, Any] = {"state": OPEN_TRADE_STATE}
    if instrument:
        params["instrument"] = instrument
    response = api_client.request(trades_ep.TradesList(account_id, params=params))
    return response.get("trades", [])


def get_pending_orders(api_client, account_id: str, instrument: str | None = None) -> list[dict]:
    """Fetch PENDING orders from OANDA (account-wide, or filtered to one instrument)."""
    params: dict[str, Any] = {"state": PENDING_ORDER_STATE}
    if instrument:
        params["instrument"] = instrument
    response = api_client.request(orders_ep.OrderList(account_id, params=params))
    return response.get("orders", [])


def check_pair_level_strategy_position(
    api_client,
    account_id: str,
    instrument: str,
    side: str,
    strategy_tag: str = STRATEGY_TAG_PREFIX,
) -> tuple[bool, str]:
    """Broker-authoritative duplicate-entry gate for one instrument/cycle.

    Blocks when OANDA already reports an open trade opened by this strategy on
    `instrument` (same-direction duplicate, or the prohibited opposite-direction
    dual position), or a still-pending strategy order for it. Any query failure
    fails CLOSED — a position whose state could not be verified must never be
    stacked on.

    Returns:
        (allowed, reason) — allowed is False when the entry must be skipped.
    """
    side = str(side or "").upper()
    try:
        open_trades = get_open_trades(api_client, account_id, instrument=instrument)
    except Exception as exc:
        return False, f"open-trade query failed (fail closed): {exc}"

    for trade in open_trades:
        if not is_strategy_record(trade, strategy_tag):
            continue
        current_side = "BUY" if _units_of(trade) > 0 else "SELL"
        if current_side == side:
            return False, f"same-direction duplicate (trade_id={trade.get('id')})"
        return False, (
            "pair-level protection; opposite-direction dual position prohibited "
            f"(trade_id={trade.get('id')})"
        )

    try:
        pending_orders = get_pending_orders(api_client, account_id, instrument=instrument)
    except Exception as exc:
        return False, f"pending-order query failed (fail closed): {exc}"

    for order in pending_orders:
        if is_strategy_record(order, strategy_tag):
            return False, f"strategy order already in flight (order_id={order.get('id')})"

    return True, "no strategy trade or pending order on instrument"


# ---------------------------------------------------------------------------
# Post-exit history — derived from OANDA's CLOSED trades, never a local file
# ---------------------------------------------------------------------------

CLOSED_TRADE_STATE = "CLOSED"
CLOSED_TRADE_HISTORY_COUNT = 50
PROFIT_CLOSE_REASON = "PROFIT_TAKE"   # maps to tier1 in utils.post_exit_gate
LOSS_CLOSE_REASON = "STOP_LOSS"       # maps to tier3 in utils.post_exit_gate


def parse_oanda_time(value: Any) -> datetime | None:
    """Parse an OANDA RFC3339 timestamp (nanosecond precision, trailing 'Z')."""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    head, dot, remainder = text.partition(".")
    if dot:
        offset = ""
        fraction = remainder
        for index, char in enumerate(remainder):
            if char in "+-" and index > 0:
                fraction, offset = remainder[:index], remainder[index:]
                break
        text = f"{head}.{fraction[:6]}{offset}" if fraction[:6] else f"{head}{offset}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def get_closed_trades(
    api_client,
    account_id: str,
    instrument: str,
    count: int = CLOSED_TRADE_HISTORY_COUNT,
) -> list[dict]:
    """Fetch recently CLOSED trades for one instrument directly from OANDA."""
    response = api_client.request(
        trades_ep.TradesList(
            account_id,
            params={"instrument": instrument, "state": CLOSED_TRADE_STATE, "count": count},
        )
    )
    return response.get("trades", [])


def build_post_exit_context(closed_trades: list[dict], now: datetime | None = None) -> dict:
    """Derive post-exit context from OANDA closed trades (newest first).

    Classification is broker-truth based: a trade that closed with positive
    realizedPL counts as a profit exit (tier1), otherwise a loss exit (tier3) —
    the same tier vocabulary `utils.post_exit_gate` already consumes. The sign
    of realizedPL is also the only win/loss definition used for the consecutive
    failure streak, so no separate local ledger has to be maintained.
    """
    now = now or datetime.now(timezone.utc)
    context: dict[str, Any] = {
        "closed_at": None,
        "close_reason": None,
        "tier": None,
        "elapsed_hours": 0.0,
        "consecutive_failures": 0,
        "realized_pl": 0.0,
        "source": "oanda_closed_trades",
    }
    if not closed_trades:
        return context

    def _close_key(trade: Mapping[str, Any]) -> datetime:
        return parse_oanda_time(trade.get("closeTime")) or datetime.min.replace(tzinfo=timezone.utc)

    ordered = sorted(closed_trades, key=_close_key, reverse=True)
    latest = ordered[0]
    try:
        realized_pl = float(latest.get("realizedPL") or 0.0)
    except (TypeError, ValueError):
        realized_pl = 0.0

    is_profit = realized_pl > 0
    close_dt = parse_oanda_time(latest.get("closeTime"))
    context.update(
        closed_at=close_dt.isoformat() if close_dt else None,
        close_reason=PROFIT_CLOSE_REASON if is_profit else LOSS_CLOSE_REASON,
        tier="tier1" if is_profit else "tier3",
        realized_pl=realized_pl,
    )
    if close_dt:
        context["elapsed_hours"] = max((now - close_dt).total_seconds() / 3600.0, 0.0)

    streak = 0
    for trade in ordered:
        try:
            pl = float(trade.get("realizedPL") or 0.0)
        except (TypeError, ValueError):
            pl = 0.0
        if pl < 0:
            streak += 1
        else:
            break
    context["consecutive_failures"] = streak
    return context