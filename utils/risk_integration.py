"""
risk_integration.py
====================

Phase 2 integration layer bridging `PyramidCluster` / `DynamicRiskManager`
to live OANDA calls and the JSON `ClusterStateStore`, for use from
`scheduled_runner_v1.1.py`'s cron-invoked `run_cycle()`.

Scope of this phase
--------------------
This module MANAGES existing open positions each cron cycle: break-even,
Chandelier trailing, and time-decay partial/full close. It does NOT wire up
pyramiding adds — `custom_strategy_v1.py` never generates a "scale into an
existing position" signal today (it only ever proposes a fresh entry, and
skips pairs where a position is already open). `PyramidCluster.add_unit()`
therefore has no caller yet in this integration; adding pyramid-signal
generation is a separate, later piece of work, not a gap in this phase.

Design principles carried over from the architecture review
-------------------------------------------------------------
- R stays anchored to the ORIGINAL base-unit entry (`entry_price_0`/`r_unit_0`),
  never recomputed from a blended entry — enforced inside DynamicRiskManager/
  PyramidCluster already; this module never touches those fields directly.
- RiskConfig is snapshotted at cluster-creation time (`build_risk_config()`)
  and persisted with the cluster, not re-read from `config.py` on restore —
  an in-flight trade isn't silently re-governed by a later config change.
- OANDA is always the source of truth for size/existence. `reconcile_with_oanda()`
  runs before any risk decision every cycle, so decisions are never made
  against stale local state.

NOT YET LIVE-VERIFIED (flag before deploying)
------------------------------------------------
- The OANDA TradeCRCDO "omit takeProfit = unmodified" contract is confirmed
  against OANDA's official docs (developer.oanda.com/rest-live-v20/trade-ep/)
  but has a known Java-SDK GitHub issue reporting inconsistent behavior for
  the analogous null-vs-omit semantics. Verify on a demo account before
  relying on it against a live account.
- This module has never made a live network call — everything below has
  been written against the documented OANDA v20 API contract and the
  patterns already used in `oanda_execution.py`/`trading_core.py`, and
  tested here only via mocked `oanda_client.request()` calls.

V2 SECTION 4.1 INSTRUMENTATION (ADDITIVE ONLY — see module-level NOTE below)
------------------------------------------------------------------------------
Two small, additive logging calls were added inside `manage_open_positions()`,
at the two points this module already detects that a managed position has
closed (either externally, via `reconcile_with_oanda`, or by this cycle's
own risk action reaching RiskStateEnum.CLOSED). Both calls:
  - are wrapped in their own try/except so a logging failure can NEVER
    interrupt position management,
  - are placed strictly AFTER the existing close-detection logic has
    already run, so they never influence whether/how a position closes,
  - read data that already exists on the (already-restored) `cluster`
    object — no new OANDA API call is made, and no existing OANDA call's
    parameters, ordering, or return-value handling changes.
See `utils/signal_instrumentation.log_trade_outcome()` for what's recorded,
including an explicit note on why `close_price` is an approximation rather
than an exact OANDA fill price (getting the exact fill price would require
a NEW OANDA API call — e.g. TradeDetails on the closed trade ID — which is
intentionally NOT added here; see the implementation report for why this
was flagged rather than improvised).
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Dict, List, Optional

import oandapyV20.endpoints.trades as trades_ep
from oandapyV20.exceptions import V20Error

import config as _config
from config import OANDA_ACCOUNT_ID
from utils.trading_core import oanda_client, format_price_for_instrument, get_latest_price
from utils.strategy_helpers import get_atr_with_volatility_context, get_candles
from utils.dynamic_risk_manager import ActionType, RiskAction, RiskConfig, RiskStateEnum
from utils.pyramid_cluster import CloseAllocationMethod, PyramidCluster
from utils.cluster_state_store import ClusterStateStore, ClusterStateStoreError
from utils.signal_instrumentation import log_trade_outcome  # V2 4.1 — additive only

logger = logging.getLogger(__name__)

# --- Single source of truth for the flag, with a LOUD warning if it's simply
#     absent from config.py — this is the exact defect that caused Phase 2 to
#     silently no-op for an entire session with zero errors or log evidence.
#     "Not defined" and "explicitly False" are now distinguishable, and any
#     other module (e.g. the runner) should import ENABLE_DYNAMIC_RISK_MANAGER
#     FROM HERE rather than re-deriving it with its own getattr() — a second,
#     independent resolution of the same flag is exactly how this kind of
#     silent-divergence bug creeps back in. ---
def _resolve_enable_flag() -> bool:
    if not hasattr(_config, "ENABLE_DYNAMIC_RISK_MANAGER"):
        print(
            "[RISK WARNING] config.ENABLE_DYNAMIC_RISK_MANAGER is NOT DEFINED in config.py "
            "— defaulting to DISABLED. Dynamic risk management, cluster persistence, and "
            "position reconciliation are ALL INACTIVE this run. If this is unintentional, "
            "add ENABLE_DYNAMIC_RISK_MANAGER = True to config.py (see config_ADDITIONS_NEEDED.py)."
        )
        return False
    return bool(_config.ENABLE_DYNAMIC_RISK_MANAGER)


ENABLE_DYNAMIC_RISK_MANAGER = _resolve_enable_flag()
CLUSTER_STATE_PATH = getattr(_config, "CLUSTER_STATE_PATH", "state/open_clusters.json")

_store = ClusterStateStore(CLUSTER_STATE_PATH)

# Granularity used to compute the rolling highest-high/lowest-low since entry
# for the Chandelier Exit calculation. H1 is a reasonable default resolution
# regardless of which timeframe the entry signal itself was generated on —
# it's fine-grained enough not to understate the true extreme, and cheap
# enough to fetch every 15-minute cycle without hitting rate limits.
EXTREME_LOOKBACK_GRANULARITY = getattr(_config, "RISK_EXTREME_LOOKBACK_GRANULARITY", "H1")


class RiskIntegrationError(Exception):
    """Raised for unrecoverable errors in this integration layer (e.g. OANDA call failures)."""


# ---------------------------------------------------------------------------
# Config snapshot
# ---------------------------------------------------------------------------

def build_risk_config() -> RiskConfig:
    """
    Snapshot a RiskConfig from current global config.py values. Call this
    ONLY when creating a brand-new cluster — never on restore, where the
    persisted config snapshot (via `DynamicRiskManager.from_dict`) must be
    used instead so an in-flight trade's thresholds don't silently change
    if config.py is edited later.
    """
    return RiskConfig(
        atr_multiplier_init=getattr(_config, "RISK_ATR_MULTIPLIER_INIT", 2.0),
        be_trigger_r=getattr(_config, "RISK_BE_TRIGGER_R", 1.0),
        chandelier_k_default=getattr(_config, "RISK_CHANDELIER_K_DEFAULT", 3.0),
        enable_time_stop=getattr(_config, "RISK_ENABLE_TIME_STOP", True),
        t_expected_hours=getattr(_config, "RISK_T_EXPECTED_HOURS", 24.0),
        time_reduce_threshold=getattr(_config, "RISK_TIME_REDUCE_THRESHOLD", 1.0),
        time_reduce_ratio=getattr(_config, "RISK_TIME_REDUCE_RATIO", 0.5),
        time_exit_threshold=getattr(_config, "RISK_TIME_EXIT_THRESHOLD", 1.5),
        time_tighten_threshold=getattr(_config, "RISK_TIME_TIGHTEN_THRESHOLD", 1.5),
        vol_compression_frac=getattr(_config, "RISK_VOL_COMPRESSION_FRAC", 0.6),
    )


def default_risk_calculator(size: float, price_distance: float) -> float:
    """
    Placeholder aggregate-risk calculator: raw notional (size * distance),
    NOT account-currency risk. Since this integration phase never calls
    `add_unit()` (no pyramiding signal exists yet), this is currently unused
    in practice — kept here as the documented default for when that changes.
    """
    return size * price_distance


# ---------------------------------------------------------------------------
# State store pass-through (thin wrappers so callers only import this module)
# ---------------------------------------------------------------------------

def list_managed_instruments() -> List[str]:
    return _store.list_managed_instruments()


def load_cluster_data(instrument: str) -> Optional[dict]:
    return _store.load_cluster_dict(instrument)


def save_cluster_data(instrument: str, data: dict) -> None:
    _store.save_cluster_dict(instrument, data)


def delete_cluster_data(instrument: str) -> bool:
    return _store.delete_cluster(instrument)


def restore_cluster(cluster_data: dict) -> PyramidCluster:
    """Reconstruct a PyramidCluster from stored state. risk_calculator is
    intentionally re-supplied fresh here rather than persisted — see
    PyramidCluster.from_dict's docstring."""
    return PyramidCluster.from_dict(cluster_data, risk_calculator=default_risk_calculator)


# ---------------------------------------------------------------------------
# Reconciliation — OANDA is always the source of truth
# ---------------------------------------------------------------------------

def reconcile_with_oanda(cluster: PyramidCluster, instrument: str) -> bool:
    """
    Sync `cluster.units` against OANDA's actual open trades for `instrument`
    BEFORE any risk decision is made this cycle. A unit whose trade_id is no
    longer open at the broker (TP hit, manual close, margin closeout, etc.)
    is dropped; a unit whose size has drifted from OANDA's reported size is
    corrected to match OANDA (broker is always the source of truth for size).

    Args:
        cluster: The restored cluster to reconcile in place.
        instrument: OANDA instrument string, e.g. "USD_JPY".

    Returns:
        True if the cluster still has at least one open unit after
        reconciliation. False if every unit is gone — in that case
        `cluster.mark_closed()` has already been called, and the caller
        should delete this instrument's state entry.

    Raises:
        RiskIntegrationError: if the OANDA call itself fails (network,
                               auth, etc.) — deliberately does NOT swallow
                               this, since making a risk decision against
                               data we couldn't actually verify is worse
                               than skipping the cycle and retrying next time.
    """
    try:
        resp = oanda_client.request(
            trades_ep.TradesList(OANDA_ACCOUNT_ID, params={"instrument": instrument, "state": "OPEN"})
        )
    except V20Error as e:
        raise RiskIntegrationError(f"reconcile_with_oanda: failed to fetch open trades for {instrument}: {e}") from e

    live_by_id: Dict[str, float] = {t["id"]: abs(float(t["currentUnits"])) for t in resp.get("trades", [])}

    surviving_units = []
    for unit in cluster.units:
        if unit.trade_id not in live_by_id:
            logger.info(
                "[RECONCILE] %s unit trade_id=%s no longer open at OANDA — removing from cluster.",
                instrument, unit.trade_id,
            )
            continue
        live_size = live_by_id[unit.trade_id]
        if live_size != unit.size:
            logger.info(
                "[RECONCILE] %s unit trade_id=%s size drift: local=%s oanda=%s — syncing to OANDA.",
                instrument, unit.trade_id, unit.size, live_size,
            )
            unit.size = live_size
        surviving_units.append(unit)

    cluster.units = surviving_units

    if not cluster.units:
        cluster.mark_closed()
        return False
    return True


# ---------------------------------------------------------------------------
# Market context
# ---------------------------------------------------------------------------

# Approximate hours-per-candle for each granularity, used ONLY to translate
# "elapsed time since entry" into a `count` for get_candles() — which supports
# a most-recent-N-candles fetch, not a date-range fetch (see fetch_market_context).
_GRANULARITY_HOURS = {
    "S5": 5 / 3600, "S10": 10 / 3600, "S15": 15 / 3600, "S30": 30 / 3600,
    "M1": 1 / 60, "M2": 2 / 60, "M4": 4 / 60, "M5": 5 / 60, "M10": 10 / 60,
    "M15": 15 / 60, "M30": 30 / 60,
    "H1": 1.0, "H2": 2.0, "H3": 3.0, "H4": 4.0, "H6": 6.0, "H8": 8.0, "H12": 12.0,
    "D": 24.0, "W": 24.0 * 7,
}

# OANDA's documented per-request candle count ceiling.
_MAX_CANDLE_COUNT = 5000
# Always request at least this many, even for a same-cycle entry, so there's at
# least the current forming candle to look at.
_MIN_CANDLE_COUNT = 2
# Extra candles requested beyond the pure elapsed-time estimate, to absorb
# partial/forming bars and any minor clock skew between entry_time and now.
_CANDLE_COUNT_BUFFER = 3


def _ensure_tz_aware_utc(dt: datetime) -> datetime:
    """Treat a naive datetime as UTC (defensive — entry_time should always be
    tz-aware in this codebase, but comparing naive vs. aware datetimes raises
    TypeError, and that's a worse failure mode than assuming UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _compute_candle_count_since(entry_time: datetime, granularity: str) -> int:
    """
    How many of the MOST RECENT candles to request so that, after filtering
    by timestamp, the result covers everything from entry_time to now.
    get_candles() only supports `count` (most-recent-N) — no start/end — so
    this is the bridge: estimate candles-since-entry from elapsed wall-clock
    time, pad it, and clamp to OANDA's valid range.
    """
    entry_time = _ensure_tz_aware_utc(entry_time)
    hours_per_candle = _GRANULARITY_HOURS.get(granularity, 1.0)
    elapsed_hours = max((datetime.now(timezone.utc) - entry_time).total_seconds() / 3600.0, 0.0)
    estimated = int(elapsed_hours / hours_per_candle) + _CANDLE_COUNT_BUFFER
    return max(_MIN_CANDLE_COUNT, min(estimated, _MAX_CANDLE_COUNT))


def _parse_oanda_candle_time(raw: str) -> Optional[datetime]:
    """
    Parse an OANDA candle timestamp, e.g. "2026-08-10T03:15:00.000000000Z"
    (nanosecond precision, trailing Z) into a tz-aware UTC datetime. Python's
    datetime can only parse up to microsecond precision, so the fractional
    part is truncated to 6 digits first. Returns None on any parse failure
    rather than raising — a single malformed timestamp in a candle list
    should not abort the whole market-context fetch; that candle is just
    excluded from the extreme calculation.
    """
    if not raw:
        return None
    try:
        text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        if "." in text:
            head, rest = text.split(".", 1)
            for i, ch in enumerate(rest):
                if ch in "+-":
                    frac, tz_part = rest[:i], rest[i:]
                    break
            else:
                frac, tz_part = rest, ""
            text = f"{head}.{frac[:6].ljust(6, '0')}{tz_part}"
        return datetime.fromisoformat(text)
    except (ValueError, AttributeError, IndexError):
        return None


def fetch_market_context(instrument: str, cluster: PyramidCluster) -> tuple:
    """
    Gather the four inputs `DynamicRiskManager.update()` needs this cycle:
    current price, current ATR, and the rolling highest-high/lowest-low
    since the position's entry (for the Chandelier Exit calculation).

    IMPORTANT: the project-wide `get_candles(instrument, granularity, count)`
    does NOT support a `start`/`end` date-range fetch — it only returns the
    most recent `count` candles. This function bridges that: it estimates how
    many recent candles covers the time since `cluster.risk_manager.entry_time`,
    fetches that many, then filters to only the ones at/after entry_time
    before computing the extremes. `get_candles()` itself is untouched.

    Returns:
        (price, atr_now, highest_high, lowest_low)

    Raises:
        RiskIntegrationError: if price, ATR, or the candle fetch itself fails
                               outright (an exception from get_candles is
                               never swallowed) — a risk decision must never
                               be made on data we couldn't actually obtain.
                               An EMPTY (but successfully returned) candle
                               list is NOT an error — see the fallback below.
    """
    price = get_latest_price(instrument)
    if price is None:
        raise RiskIntegrationError(f"fetch_market_context: no live price available for {instrument}")

    atr_now, _z = get_atr_with_volatility_context(
        instrument,
        getattr(_config, "JPY_ATR_PERIOD", 14),
        getattr(_config, "JPY_ATR_HISTORY_LOOKBACK", 100),
    )
    if atr_now is None or atr_now <= 0:
        raise RiskIntegrationError(f"fetch_market_context: ATR unavailable for {instrument}")

    entry_time = _ensure_tz_aware_utc(cluster.risk_manager.entry_time)
    count = _compute_candle_count_since(entry_time, EXTREME_LOOKBACK_GRANULARITY)

    try:
        candles = get_candles(instrument, EXTREME_LOOKBACK_GRANULARITY, count)
    except Exception as e:
        # A real fetch failure (network, bad instrument, etc.) must propagate —
        # never silently treated as "no candles since entry".
        raise RiskIntegrationError(f"fetch_market_context: get_candles failed for {instrument}: {e}") from e

    since_entry = []
    for c in candles or []:
        if not c.get("complete", True):
            continue
        candle_time = _parse_oanda_candle_time(c.get("time"))
        if candle_time is not None and candle_time >= entry_time:
            since_entry.append(c)

    if since_entry:
        highs = [float(c["mid"]["h"]) for c in since_entry]
        lows = [float(c["mid"]["l"]) for c in since_entry]
        highest_high, lowest_low = max(highs), min(lows)
    else:
        # Legitimate, expected case (not an error): a same-cycle entry has no
        # completed candle yet, OR the broker returned fewer/older candles than
        # requested (e.g. thin history). Fall back to the live price as the
        # only known extreme so far. Logged (not silent) since it's still
        # worth visibility into how often this fallback is actually hit.
        logger.warning(
            "[RISK] %s: no completed candles found at/after entry_time=%s "
            "(requested count=%d, got %d raw candles back) — falling back to "
            "live price as the only known extreme so far.",
            instrument, entry_time.isoformat(), count, len(candles or []),
        )
        highest_high = lowest_low = price

    # Always fold the live price into the extremes — candles may lag the current tick.
    highest_high = max(highest_high, price)
    lowest_low = min(lowest_low, price)

    return price, atr_now, highest_high, lowest_low


# ---------------------------------------------------------------------------
# Applying a RiskAction to live OANDA orders
# ---------------------------------------------------------------------------

def apply_risk_action(cluster: PyramidCluster, instrument: str, action: RiskAction) -> None:
    """
    Execute the RiskAction returned by `cluster.update()` against OANDA,
    and sync `cluster`'s in-memory state to match what actually executed.

    Args:
        cluster: The cluster `action` was computed for. Mutated in place
                 (via `apply_close()`) on PARTIAL_CLOSE/FULL_CLOSE.
        instrument: OANDA instrument string.
        action: The RiskAction from `cluster.update()`.

    Raises:
        RiskIntegrationError: if any OANDA call fails. Deliberately does not
                               retry internally — the caller (run_cycle) should
                               leave the cluster's PREVIOUS saved state in place
                               (don't save on exception) so next cycle retries
                               against the same known-good state.
    """
    if action.action == ActionType.NO_CHANGE:
        return

    if action.action == ActionType.UPDATE_SL:
        _update_sl_for_all_units(cluster, action.new_sl, instrument)
        return

    if action.action in (ActionType.PARTIAL_CLOSE, ActionType.FULL_CLOSE):
        _execute_close(cluster, instrument, action.close_ratio)
        return

    raise RiskIntegrationError(f"apply_risk_action: unhandled action type {action.action}")


def _update_sl_for_all_units(cluster: PyramidCluster, new_sl: float, instrument: str) -> None:
    """
    SL is logically shared across the cluster, but OANDA attaches stop orders
    per trade ID — so one UPDATE_SL action means one TradeCRCDO call per unit.

    Per OANDA's official v20 docs (developer.oanda.com/rest-live-v20/trade-ep/):
    omitting `takeProfit` from the payload leaves the existing take-profit
    order UNMODIFIED (only an explicit `takeProfit: null` cancels it) — so
    this payload intentionally contains ONLY `stopLoss`. NOT YET VERIFIED
    against a live/demo account — see module docstring.
    """
    price_str = format_price_for_instrument(new_sl, instrument)
    payload = {"stopLoss": {"price": price_str, "timeInForce": "GTC"}}

    for unit in cluster.units:
        if not unit.trade_id:
            logger.warning("[RISK] %s unit with no trade_id — cannot update SL, skipping.", instrument)
            continue
        try:
            oanda_client.request(trades_ep.TradeCRCDO(OANDA_ACCOUNT_ID, unit.trade_id, data=payload))
            logger.info("[RISK] %s trade_id=%s SL updated to %s", instrument, unit.trade_id, price_str)
        except V20Error as e:
            raise RiskIntegrationError(
                f"_update_sl_for_all_units: failed to update SL for {instrument} trade_id={unit.trade_id}: {e}"
            ) from e


def _execute_close(cluster: PyramidCluster, instrument: str, close_ratio: float) -> None:
    """
    FIFO-allocate the close across units, send one TradeClose per affected
    trade ID, confirm the ACTUAL executed size from each response (not the
    requested size), then sync cluster state via `apply_close()` using only
    what was confirmed to have actually executed.
    """
    instructions = cluster.close_allocation(close_ratio, method=CloseAllocationMethod.FIFO)
    confirmed = []

    for instr in instructions:
        if instr.close_size <= 0:
            confirmed.append(instr)
            continue
        if not instr.trade_id:
            raise RiskIntegrationError(
                f"_execute_close: {instrument} unit at index {instr.unit_index} has no trade_id "
                f"but close_size={instr.close_size} > 0 — cannot close an untracked ticket."
            )
        try:
            resp = oanda_client.request(
                trades_ep.TradeClose(
                    OANDA_ACCOUNT_ID,
                    tradeID=instr.trade_id,
                    data={"units": str(int(round(instr.close_size)))},
                )
            )
        except V20Error as e:
            raise RiskIntegrationError(
                f"_execute_close: failed to close {instr.close_size} units of "
                f"{instrument} trade_id={instr.trade_id}: {e}"
            ) from e

        fill = resp.get("orderFillTransaction", {})
        actual_closed = abs(float(fill.get("units", instr.close_size)))
        logger.info(
            "[RISK] %s trade_id=%s requested_close=%s actual_close=%s",
            instrument, instr.trade_id, instr.close_size, actual_closed,
        )
        confirmed.append(
            replace(
                instr,
                close_size=actual_closed,
                remaining_size=instr.original_size - actual_closed,
            )
        )

    cluster.apply_close(confirmed)


# ---------------------------------------------------------------------------
# New cluster creation from a live fill
# ---------------------------------------------------------------------------

def new_cluster_from_fill(signal_data: dict, fill: dict) -> PyramidCluster:
    """
    Build a fresh PyramidCluster from an ACTUAL OANDA fill — never from the
    strategy's planned/requested entry price or size, since slippage means
    those can differ from what actually executed.

    Args:
        signal_data: The dict from `get_last_signal()` (pair, action,
                     stop_loss, etc.) — used only for `stop_loss` (as the
                     structural SL floor) and `pair`/`action` for direction.
        fill: The dict returned by `open_oanda_order()` on SUCCESS. Must
              contain "filled_price", "units", and "trade_id".

    Returns:
        A new PyramidCluster with its RiskConfig freshly snapshotted from
        current config.py (see `build_risk_config()`).

    Raises:
        RiskIntegrationError: if `fill` is missing required fields, or if
                               ATR can't be fetched for the new position.
    """
    for required_key in ("filled_price", "units", "trade_id"):
        if required_key not in fill:
            raise RiskIntegrationError(f"new_cluster_from_fill: fill missing required key {required_key!r}: {fill}")

    pair = signal_data["pair"]
    direction = 1 if signal_data["action"] == "BUY" else -1

    atr_now, _z = get_atr_with_volatility_context(
        pair,
        getattr(_config, "JPY_ATR_PERIOD", 14),
        getattr(_config, "JPY_ATR_HISTORY_LOOKBACK", 100),
    )
    if atr_now is None or atr_now <= 0:
        raise RiskIntegrationError(f"new_cluster_from_fill: ATR unavailable for {pair} at entry")

    filled_price = float(fill["filled_price"])
    filled_units = abs(float(fill["units"]))

    cluster = PyramidCluster(
        initial_size=filled_units,
        entry_price=filled_price,
        direction=direction,
        atr_entry=atr_now,
        entry_time=datetime.now(timezone.utc),
        config=build_risk_config(),
        structural_sl_level=float(signal_data["stop_loss"]),
        max_size_decay_ratio=getattr(_config, "RISK_MAX_SIZE_DECAY_RATIO", 0.7),
        risk_calculator=default_risk_calculator,
        initial_trade_id=str(fill["trade_id"]),
    )
    return cluster


# ---------------------------------------------------------------------------
# V2 SECTION 4.1 — ADDITIVE outcome-logging helper
# ---------------------------------------------------------------------------

def _log_closure_outcome(
    cluster: PyramidCluster,
    instrument: str,
    approx_close_price: Optional[float],
    close_price_source: str,
    close_reason: str,
) -> None:
    """
    ADDITIVE ONLY (V2 Section 4.1 instrumentation). Writes one outcome
    record via `signal_instrumentation.log_trade_outcome()`. Called from two
    places in `manage_open_positions()`, both AFTER the pre-existing
    close-detection logic has already run — this function never influences
    whether a position is treated as closed, and never makes an OANDA call.

    `cluster.risk_manager` retains `entry_price_0`/`r_unit_0`/`direction`
    even after `mark_closed()` (confirmed: `mark_closed()` only sets
    `state`, per dynamic_risk_manager.py — it does not clear the R-anchor),
    so this is safe to call in both the "closed externally" and "closed by
    own action" branches without needing to snapshot anything before
    reconciliation mutates `cluster.units`.

    Wrapped in try/except at the call site (not here) so a failure here is
    visible per-call-site with call-site-specific context in the log line.
    """
    realized_r = None
    if approx_close_price is not None:
        realized_r = cluster.risk_manager.unrealized_r(approx_close_price)

    log_trade_outcome(
        instrument=instrument,
        direction=cluster.risk_manager.direction,
        entry_price_0=cluster.risk_manager.entry_price_0,
        r_unit_0=cluster.risk_manager.r_unit_0,
        close_price=approx_close_price,
        close_price_source=close_price_source,
        realized_r=realized_r,
        close_reason=close_reason,
        final_state=cluster.risk_manager.state.value,
    )


# ---------------------------------------------------------------------------
# Phase A orchestration — manage every existing risk-managed position
# ---------------------------------------------------------------------------

def manage_open_positions() -> List[str]:
    """
    Revisit every currently risk-managed instrument — reconcile against
    OANDA, compute this cycle's RiskAction, and execute it (SL update /
    partial close / full close). No-op, returns [], if the flag is off.

    Lives here (not in the runner) because it has no runner-specific
    dependencies — everything it calls already lives in this module — which
    makes it directly unit-testable without needing the runner's heavier
    external dependencies (custom_strategy_v1, retry, etc.).

    Returns:
        List of instruments that are STILL under active management as of
        the end of this cycle — used by the caller to avoid attempting a
        fresh entry on a pair the risk layer is already handling.

        IMPORTANT membership semantics — this is a fix for a real bug found
        in live logs: an instrument found closed at OANDA, or one this
        cycle's own action fully closed, is POSITIVELY KNOWN to be flat and
        must NOT appear in the returned list, since a same-cycle fresh entry
        on it is legitimate and should not be blocked. An instrument left
        managed after an exception (state genuinely UNKNOWN this cycle) DOES
        stay in the returned list — erring toward not double-entering on top
        of a position whose true state couldn't be verified this cycle is
        the safer default.
    """
    if not ENABLE_DYNAMIC_RISK_MANAGER:
        return []

    still_managed: List[str] = []
    try:
        instruments = list_managed_instruments()
    except Exception as e:
        print(f"  [RISK ERROR] Could not list managed instruments: {e}")
        print("  → Skipping position management this cycle, will retry next cycle.")
        return still_managed

    for instrument in instruments:
        is_still_managed = False
        try:
            cluster_data = load_cluster_data(instrument)
            if cluster_data is None:
                continue  # deleted between list and load (e.g. by a previous iteration) — genuinely gone

            cluster = restore_cluster(cluster_data)

            still_open = reconcile_with_oanda(cluster, instrument)
            if not still_open:
                # --- V2 4.1 ADDITIVE: outcome log for an externally-closed
                # position. Best-effort approximate close price (see
                # _log_closure_outcome docstring / signal_instrumentation
                # module docstring for why this is an approximation, not an
                # exact fill price). Never allowed to affect control flow.
                try:
                    approx_price = get_latest_price(instrument)
                    _log_closure_outcome(
                        cluster,
                        instrument,
                        approx_close_price=approx_price,
                        close_price_source=(
                            "latest_price_at_detection (approximate — exact OANDA "
                            "fill price not fetched; see module docstring)"
                        ),
                        close_reason="closed_externally_or_by_broker "
                                     "(TP/SL/manual — detected via reconciliation)",
                    )
                except Exception as log_err:
                    logger.warning(
                        "[V2-LOG] Failed to log trade outcome for %s (external close): %s",
                        instrument, log_err,
                    )
                # --- end V2 4.1 addition ---

                delete_cluster_data(instrument)
                print(f"  [RISK] {instrument} closed externally (TP/manual) — removed from managed state.")
                # is_still_managed stays False — this instrument is confirmed flat.
            else:
                price, atr_now, hh, ll = fetch_market_context(instrument, cluster)
                action = cluster.update(price, atr_now, hh, ll, current_time=datetime.now(timezone.utc))

                print(
                    f"  [RISK] {instrument} price={price} r={cluster.risk_manager.unrealized_r(price):+.2f}R "
                    f"-> {action.action.value} (state={action.state.value})"
                )
                if action.reason and action.reason != "No state change.":
                    print(f"         {action.reason}")

                apply_risk_action(cluster, instrument, action)

                if cluster.risk_manager.state == RiskStateEnum.CLOSED:
                    # --- V2 4.1 ADDITIVE: outcome log for a position closed
                    # by this cycle's own risk action (FULL_CLOSE). `price`
                    # is the same evaluation price already fetched this
                    # cycle via fetch_market_context (no new fetch) and is
                    # a closer approximation to the actual close than the
                    # external-close branch's post-hoc price lookup.
                    try:
                        _log_closure_outcome(
                            cluster,
                            instrument,
                            approx_close_price=price,
                            close_price_source=(
                                "cycle_evaluation_price (approximate — exact OANDA "
                                "fill price not fetched; see module docstring)"
                            ),
                            close_reason=f"closed_by_own_risk_action (last_action={action.action.value})",
                        )
                    except Exception as log_err:
                        logger.warning(
                            "[V2-LOG] Failed to log trade outcome for %s (full close): %s",
                            instrument, log_err,
                        )
                    # --- end V2 4.1 addition ---

                    delete_cluster_data(instrument)
                    print(f"  [RISK] {instrument} fully closed this cycle — removed from managed state.")
                    # is_still_managed stays False — confirmed flat as of this cycle's own action.
                else:
                    save_cluster_data(instrument, cluster.to_dict())
                    is_still_managed = True

        except Exception as e:
            import traceback
            print(f"  [RISK ERROR] {instrument}: {e}")
            traceback.print_exc()
            # Deliberately do NOT delete or save state on error — leave the LAST
            # KNOWN GOOD state in place and retry fresh next cycle. State is
            # AMBIGUOUS here (we don't know if it's still open), so err toward
            # still-managed rather than risk a duplicate entry this cycle.
            is_still_managed = True

        if is_still_managed:
            still_managed.append(instrument)

    return still_managed
