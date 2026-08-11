"""
utils/position_direction.py
======================

Fixes a real bug found in the first live scheduled-runner cycle: the
original entry-check only asked "is ANY position open for this instrument?"
and skipped entry either way — including when the existing position was in
the OPPOSITE direction from the new signal (e.g. holding SHORT AUD/JPY while
the strategy now signals BUY). That leaves a stale, wrong-direction position
open indefinitely instead of reversing into the new signal.

This module adds direction-awareness: BUY vs BUY (or SELL vs SELL) still
skips as before; BUY vs SELL (or SELL vs BUY) closes the opposite position
first, then lets entry proceed.

Scope
-----
This is intentionally independent of the dynamic risk manager / PyramidCluster
machinery (Phase 2). It sits at the same point in `run_cycle()` the original
"Already holding position... Skipping" check occupied, and runs regardless of
`ENABLE_DYNAMIC_RISK_MANAGER`. When that flag IS on, `run_cycle()` already
returns early for any pair currently tracked in the cluster state store
(`pair in managed_instruments`) — so by the time this module's logic runs,
the pair is guaranteed to be untracked by the risk layer: either the flag is
off entirely, or it's a leftover/manual/pre-Phase-2 position the risk layer
never took ownership of (exactly the AUD/JPY situation in the screenshot).
There is therefore no cluster-state conflict to reconcile here.

`custom_strategy_v1.py` is untouched — this module only consumes its output
(`signal_data["action"]`), never influences signal generation.
"""

from __future__ import annotations

import os
import sys
from enum import Enum
from typing import Optional

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from utils.trading_core import get_open_position, close_position


class PositionDecision(Enum):
    """What to do this cycle, given a new signal and the current OANDA position."""
    ENTER = "ENTER"                          # no existing position -> open normally
    SKIP_SAME_DIRECTION = "SKIP_SAME_DIRECTION"  # existing position already matches the signal
    CLOSE_THEN_ENTER = "CLOSE_THEN_ENTER"    # opposite direction -> close it, then enter
    SKIP_HEDGED = "SKIP_HEDGED"              # both long AND short open simultaneously — ambiguous


class PositionDirectionError(Exception):
    """Raised when resolving or closing a position fails unrecoverably."""


def get_position_direction(pair: str) -> Optional[str]:
    """
    Query OANDA for the current position direction on `pair`.

    Returns:
        "BUY" if a long position is open, "SELL" if short, "HEDGED" if both
        long and short units are simultaneously non-zero (not expected for
        this non-hedging swing-trend strategy, but handled explicitly rather
        than silently guessing), or None if flat.
    """
    existing = get_open_position(pair)
    if not existing:
        return None

    long_units = float(existing.get("long", {}).get("units", 0) or 0)
    short_units = float(existing.get("short", {}).get("units", 0) or 0)

    if long_units != 0 and short_units != 0:
        return "HEDGED"
    if long_units != 0:
        return "BUY"
    if short_units != 0:
        return "SELL"
    return None


def resolve_signal_vs_position(signal_action: str, existing_direction: Optional[str]) -> PositionDecision:
    """
    Pure decision logic — no I/O, fully unit-testable without mocking OANDA.

    Args:
        signal_action: "BUY" or "SELL", from the strategy's signal.
        existing_direction: Return value of `get_position_direction()`.

    Returns:
        The PositionDecision to act on.
    """
    if existing_direction is None:
        return PositionDecision.ENTER
    if existing_direction == "HEDGED":
        return PositionDecision.SKIP_HEDGED
    if existing_direction == signal_action:
        return PositionDecision.SKIP_SAME_DIRECTION
    return PositionDecision.CLOSE_THEN_ENTER


def resolve_and_prepare_entry(pair: str, signal_action: str) -> PositionDecision:
    """
    Full orchestration for one instrument/cycle: check the live OANDA
    position, decide what to do, and if the decision is CLOSE_THEN_ENTER,
    actually close the opposite position before returning.

    Args:
        pair: OANDA instrument string, e.g. "AUD_JPY".
        signal_action: "BUY" or "SELL" from the current signal.

    Returns:
        The PositionDecision that was resolved. If CLOSE_THEN_ENTER is
        returned, the opposite position has ALREADY been closed — the
        caller can proceed straight to opening the new entry. If the close
        itself failed, this raises rather than returning a decision the
        caller might act on incorrectly.

    Raises:
        PositionDirectionError: if closing the opposite position fails.
                                 Entry must NOT proceed in that case — the
                                 caller should abort this cycle and retry
                                 next time rather than risk stacking a new
                                 position on top of one that failed to close.
    """
    existing_direction = get_position_direction(pair)
    decision = resolve_signal_vs_position(signal_action, existing_direction)

    if decision == PositionDecision.CLOSE_THEN_ENTER:
        closed = close_position(pair)
        if not closed:
            raise PositionDirectionError(
                f"{pair}: failed to close existing {existing_direction} position before "
                f"opening new {signal_action} entry — aborting entry this cycle for safety."
            )

    return decision