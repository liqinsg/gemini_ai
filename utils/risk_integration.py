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

def fetch_market_context(instrument: str, cluster: PyramidCluster) -> tuple:
    """
    Gather the four inputs `DynamicRiskManager.update()` needs this cycle:
    current price, current ATR, and the rolling highest-high/lowest-low
    since the position's entry (for the Chandelier Exit calculation).

    Returns:
        (price, atr_now, highest_high, lowest_low)

    Raises:
        RiskIntegrationError: if price or ATR can't be obtained — a risk
                               decision must never be made on missing data.
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

    entry_time = cluster.risk_manager.entry_time
    candles = get_candles(instrument, granularity=EXTREME_LOOKBACK_GRANULARITY, start=entry_time)
    completed = [c for c in candles if c.get("complete", True)]

    if completed:
        highs = [float(c["mid"]["h"]) for c in completed]
        lows = [float(c["mid"]["l"]) for c in completed]
        highest_high, lowest_low = max(highs), min(lows)
    else:
        # No candle history since entry yet (e.g. entered this same cycle) — the
        # current price is the only known extreme so far.
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