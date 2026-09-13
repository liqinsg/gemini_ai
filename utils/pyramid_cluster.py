"""
utils/pyramid_cluster.py
===================

Multi-unit position (pyramiding) wrapper around a single shared
`DynamicRiskManager`. Manages safe scaling-in for a trend trade that's
already working, while keeping one unified stop-loss and one enforced
aggregate-risk ceiling across all units.

Design summary
--------------
- A `PyramidCluster` holds a list of `PositionUnit`s (size, entry price,
  entry time) plus exactly one `DynamicRiskManager` instance, which owns
  the shared SL and the state machine (INIT -> BREAK_EVEN -> ...).
- Adding a unit is gated by `can_add_unit()` / `add_unit()`, which enforce:
    1. The cluster must already be secured at BREAK_EVEN or better.
    2. Sizing decay: each new unit <= `max_size_decay_ratio` * previous unit.
    3. Aggregate risk across ALL units (existing + proposed) must stay
       within a caller-supplied `max_allowed_risk` ceiling.
- On a successful add, the blended (size-weighted) entry price is
  recomputed and pushed into the shared `DynamicRiskManager`, along with
  a recomputed R-unit (distance from blended entry to the *current* shared
  SL) — the trailing/BE/time-decay state machine itself is untouched by
  the add (it doesn't reset to INIT; a secured cluster stays secured).

Depends on `dynamic_risk_manager.py` living in the same package/directory.
"""

from __future__ import annotations

import os
import sys

# Bootstrap sys.path the same way oanda_execution.py does, so this module
# works both when run directly (`python utils/pyramid_cluster.py`, which
# only puts utils/ itself on sys.path) and when imported as part of the
# package (`from utils.pyramid_cluster import ...`, which needs the
# PROJECT ROOT on sys.path so `utils` resolves as a package).
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, List, Optional

from utils.dynamic_risk_manager import (
    ActionType,
    DynamicRiskManager,
    RiskAction,
    RiskConfig,
    RiskStateEnum,
)


# ---------------------------------------------------------------------------
# Enums & structured outputs
# ---------------------------------------------------------------------------

class AddUnitStatus(Enum):
    """Result codes for a proposed pyramid add."""
    OK = "OK"
    REJECTED_NOT_SECURED = "REJECTED_NOT_SECURED"           # base position not yet at BE+
    REJECTED_SIZE_TOO_LARGE = "REJECTED_SIZE_TOO_LARGE"     # violates sizing-decay rule
    REJECTED_RISK_EXCEEDED = "REJECTED_RISK_EXCEEDED"       # would breach aggregate risk cap
    REJECTED_CLUSTER_CLOSED = "REJECTED_CLUSTER_CLOSED"     # cluster already closed/time-exited


@dataclass
class AddUnitCheck:
    """
    Result of `can_add_unit()` / `add_unit()`.

    Attributes:
        status: Outcome of the guardrail checks.
        reason: Human-readable explanation, safe to log.
        projected_total_risk: Aggregate risk if this add were applied
                               (present for RISK_EXCEEDED and successful OK checks).
        max_allowed_risk: The ceiling that was checked against.
    """
    status: AddUnitStatus
    reason: str
    projected_total_risk: Optional[float] = None
    max_allowed_risk: Optional[float] = None


@dataclass
class PositionUnit:
    """A single fill within a pyramided cluster."""
    size: float
    entry_price: float
    entry_time: datetime
    trade_id: Optional[str] = None  # OANDA trade ID for this specific fill/ticket — needed
                                     # because SL-modify and close calls are per-trade-ID, not
                                     # per-position, even when several units share one instrument.

    def to_dict(self) -> dict:
        """Serialize to a plain, JSON-safe dict."""
        return {
            "size": self.size,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time.isoformat(),
            "trade_id": self.trade_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PositionUnit":
        """Reconstruct from `to_dict()` output."""
        return cls(
            size=data["size"],
            entry_price=data["entry_price"],
            entry_time=datetime.fromisoformat(data["entry_time"]),
            trade_id=data.get("trade_id"),
        )


@dataclass
class ClusterMetrics:
    """Snapshot of cluster-wide metrics, for logging/dashboards."""
    unit_count: int
    unit_sizes: List[float]
    total_size: float
    blended_entry: float
    shared_sl: float
    total_risk: float
    state: RiskStateEnum


class CloseAllocationMethod(Enum):
    """How a cluster-level close_ratio maps to specific unit tickets."""
    FIFO = "FIFO"        # Close oldest units first — matches OANDA's default FIFO rule
    PRO_RATA = "PRO_RATA"  # Trim every unit proportionally by the same ratio


@dataclass
class UnitCloseInstruction:
    """
    One line item telling the execution layer how much of a specific unit
    to close. `unit_index` refers to the unit's position in `cluster.units`
    *at the time `close_allocation()` was called* — pass the same list back
    into `apply_close()` promptly, before any other add/close mutates order.
    `trade_id` is included so the execution layer knows exactly which OANDA
    trade ticket to send the close request to.
    """
    unit_index: int
    entry_price: float
    entry_time: datetime
    trade_id: Optional[str]
    original_size: float
    close_size: float
    remaining_size: float


# Default risk calculator: (unit_size, price_distance_to_sl) -> risk in "notional price-units".
# This is a placeholder — in a real account-risk check, replace with a calculator that
# converts to account-currency risk (e.g. via OANDA's pip value / margin calc for the
# instrument), since JPY-cross pip values depend on account currency and current USD/JPY rate.
DEFAULT_RISK_CALCULATOR: Callable[[float, float], float] = lambda size, distance: size * distance


# ---------------------------------------------------------------------------
# PyramidCluster
# ---------------------------------------------------------------------------

class PyramidCluster:
    """
    Manages a pyramided cluster of same-direction units sharing one
    `DynamicRiskManager`. Use this instead of a bare `DynamicRiskManager`
    whenever a strategy may scale into a winning position.
    """

    def __init__(
        self,
        initial_size: float,
        entry_price: float,
        direction: int,
        atr_entry: float,
        entry_time: datetime,
        config: Optional[RiskConfig] = None,
        structural_sl_level: Optional[float] = None,
        max_size_decay_ratio: float = 0.7,
        risk_calculator: Optional[Callable[[float, float], float]] = None,
        initial_trade_id: Optional[str] = None,
    ) -> None:
        """
        Args:
            initial_size: Size of the first (base) unit, e.g. 10_000 units.
            entry_price: Fill price of the first unit.
            direction: +1 long, -1 short — fixed for the life of the cluster.
            atr_entry: ATR at time of the base entry (defines the initial R-unit).
            entry_time: Timestamp of the base entry.
            config: RiskConfig passed straight through to the shared DynamicRiskManager.
            structural_sl_level: Optional structural invalidation level for the initial SL.
            max_size_decay_ratio: Each new unit must be <= this fraction of the immediately
                                   preceding unit's size (e.g. 0.7 => each add is at most 70%
                                   of the prior unit).
            risk_calculator: Callable(size, price_distance) -> risk value, used to enforce
                              the aggregate risk ceiling. Defaults to a raw notional
                              (size * distance) placeholder — supply a proper
                              account-currency conversion for live use. NOT persisted by
                              `to_dict()` (see note there) — re-supply it on every restore.
            initial_trade_id: OANDA trade ID for the base unit's fill, if already known
                               (e.g. constructing immediately after a live fill).
        """
        self.max_size_decay_ratio = max_size_decay_ratio
        self.risk_calculator = risk_calculator or DEFAULT_RISK_CALCULATOR

        self.units: List[PositionUnit] = [
            PositionUnit(
                size=initial_size,
                entry_price=entry_price,
                entry_time=entry_time,
                trade_id=initial_trade_id,
            )
        ]
        self.risk_manager = DynamicRiskManager(
            entry_price=entry_price,
            direction=direction,
            atr_entry=atr_entry,
            entry_time=entry_time,
            config=config,
            structural_sl_level=structural_sl_level,
        )

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    @property
    def direction(self) -> int:
        """
        Trade direction (+1/-1). Read through to the shared DynamicRiskManager
        rather than keeping a second copy — direction is canonically owned by
        the risk manager, since it's fixed at construction and used directly
        in its R-multiple / SL-favorability calculations.
        """
        return self.risk_manager.direction

    @property
    def total_size(self) -> float:
        """Sum of all open unit sizes."""
        return sum(u.size for u in self.units)

    @property
    def blended_entry(self) -> float:
        """Size-weighted average entry price across all units."""
        total = self.total_size
        return sum(u.size * u.entry_price for u in self.units) / total

    def _secured_states(self) -> set:
        """States in which the base position is considered 'secured' (BE or better)."""
        return {
            RiskStateEnum.BREAK_EVEN,
            RiskStateEnum.TRAILING_CHANDELIER,
            RiskStateEnum.TIME_DECAY_REDUCE,
        }

    def _projected_total_risk(self, projected_units: List[PositionUnit], shared_sl: float) -> float:
        """Aggregate risk = sum over units of risk_calculator(size, |entry - shared_sl|)."""
        return sum(
            self.risk_calculator(u.size, abs(u.entry_price - shared_sl))
            for u in projected_units
        )

    # ------------------------------------------------------------------
    # Guardrail checks
    # ------------------------------------------------------------------

    def can_add_unit(
        self,
        new_size: float,
        new_entry_price: float,
        max_allowed_risk: float,
    ) -> AddUnitCheck:
        """
        Check (without mutating state) whether a proposed add is allowed.

        Args:
            new_size: Proposed size of the new unit.
            new_entry_price: Proposed fill price of the new unit.
            max_allowed_risk: Risk ceiling in the same units produced by
                               `self.risk_calculator` (e.g. account currency,
                               if you supplied an account-aware calculator).
                               Typically `account_equity * risk_pct_max`.

        Returns:
            AddUnitCheck with status OK if all guardrails pass, or the
            first-violated REJECTED_* status otherwise (checked in order:
            cluster state -> secured -> sizing decay -> aggregate risk).
        """
        if self.risk_manager.state in (RiskStateEnum.CLOSED, RiskStateEnum.TIME_DECAY_EXIT):
            return AddUnitCheck(
                status=AddUnitStatus.REJECTED_CLUSTER_CLOSED,
                reason=f"Cluster is in terminal state {self.risk_manager.state.value}; cannot add.",
            )

        if self.risk_manager.state not in self._secured_states():
            return AddUnitCheck(
                status=AddUnitStatus.REJECTED_NOT_SECURED,
                reason=(
                    f"Base position not yet secured (state={self.risk_manager.state.value}); "
                    "SL must reach BREAK_EVEN or better before pyramiding."
                ),
            )

        last_unit_size = self.units[-1].size
        max_new_size = self.max_size_decay_ratio * last_unit_size
        if new_size > max_new_size:
            return AddUnitCheck(
                status=AddUnitStatus.REJECTED_SIZE_TOO_LARGE,
                reason=(
                    f"Proposed size {new_size} exceeds sizing-decay cap "
                    f"({self.max_size_decay_ratio:.0%} of last unit {last_unit_size} = {max_new_size})."
                ),
            )

        shared_sl = self.risk_manager.current_sl
        projected_units = self.units + [
            PositionUnit(size=new_size, entry_price=new_entry_price, entry_time=datetime.min)
        ]
        projected_risk = self._projected_total_risk(projected_units, shared_sl)

        if projected_risk > max_allowed_risk:
            return AddUnitCheck(
                status=AddUnitStatus.REJECTED_RISK_EXCEEDED,
                reason=(
                    f"Projected aggregate risk {projected_risk:.4f} would exceed "
                    f"max_allowed_risk {max_allowed_risk:.4f}."
                ),
                projected_total_risk=projected_risk,
                max_allowed_risk=max_allowed_risk,
            )

        return AddUnitCheck(
            status=AddUnitStatus.OK,
            reason="All guardrails passed.",
            projected_total_risk=projected_risk,
            max_allowed_risk=max_allowed_risk,
        )

    # ------------------------------------------------------------------
    # Mutating operation
    # ------------------------------------------------------------------

    def add_unit(
        self,
        new_size: float,
        new_entry_price: float,
        entry_time: datetime,
        max_allowed_risk: float,
        trade_id: Optional[str] = None,
    ) -> AddUnitCheck:
        """
        Attempt to add a new unit to the cluster. Re-runs `can_add_unit()`
        internally; only mutates state if the check passes.

        On success:
            - Appends the new PositionUnit. `self.blended_entry` (an accounting-
              only, size-weighted average) updates automatically since it's a
              derived property over `self.units`.

        IMPORTANT — R-anchor is NEVER touched here. The shared
        `DynamicRiskManager`'s `entry_price_0` / `r_unit_0` stay fixed at the
        ORIGINAL base-unit values for the life of the cluster. R is a
        thesis-progress signal (has price moved far enough relative to the
        original ATR-calibrated plan?), not a cost-basis figure — rebasing it
        to a blended entry on every add would silently change what every
        R-based threshold (BE, 1.5R/2R/3R tightening, time-decay) means
        mid-trade, and can even flip R negative immediately after a de-risking
        action. See architecture review notes for the numeric example.
        Use `self.blended_entry` / `metrics().total_risk` for true cost-basis
        and aggregate-risk reporting instead — those are separate concerns
        from R and are computed fresh from `self.units` whenever you ask.

        Args:
            new_size: Size of the new unit.
            new_entry_price: Fill price of the new unit.
            entry_time: Timestamp of this add.
            max_allowed_risk: Risk ceiling, see `can_add_unit`.
            trade_id: OANDA trade ID for this new fill, if already known.

        Returns:
            The AddUnitCheck describing the outcome (OK or the rejection reason).
            Callers should inspect `.status` before assuming the add happened.
        """
        check = self.can_add_unit(new_size, new_entry_price, max_allowed_risk)
        if check.status != AddUnitStatus.OK:
            return check

        self.units.append(
            PositionUnit(size=new_size, entry_price=new_entry_price, entry_time=entry_time, trade_id=trade_id)
        )
        return check

    # ------------------------------------------------------------------
    # Pass-through per-cycle update
    # ------------------------------------------------------------------

    def update(
        self,
        price: float,
        atr_now: float,
        highest_high: float,
        lowest_low: float,
        current_time: Optional[datetime] = None,
        hours_elapsed: Optional[float] = None,
    ) -> RiskAction:
        """
        Pass-through to the shared DynamicRiskManager's per-cycle update.
        A PARTIAL_CLOSE / FULL_CLOSE action applies proportionally across
        all units in the cluster — the caller's execution layer is
        responsible for actually splitting the close across broker tickets;
        `close_ratio` in the returned RiskAction is relative to whatever
        `remaining_ratio` currently represents for the whole cluster.
        """
        return self.risk_manager.update(
            price=price,
            atr_now=atr_now,
            highest_high=highest_high,
            lowest_low=lowest_low,
            current_time=current_time,
            hours_elapsed=hours_elapsed,
        )

    def mark_closed(self) -> None:
        """Mark the entire cluster as closed (delegates to the shared risk manager)."""
        self.risk_manager.mark_closed()

    # ------------------------------------------------------------------
    # Close allocation (PARTIAL_CLOSE / FULL_CLOSE fan-out)
    # ------------------------------------------------------------------

    def close_allocation(
        self,
        close_ratio: float,
        method: CloseAllocationMethod = CloseAllocationMethod.FIFO,
    ) -> List[UnitCloseInstruction]:
        """
        Translate a cluster-level `close_ratio` (as returned in a RiskAction
        from `update()`) into concrete per-unit close instructions.

        This method is read-only — it does NOT mutate `self.units`. Call it,
        send the resulting tickets to your broker, and once fills are
        confirmed, call `apply_close()` with the same instructions (or a
        filtered subset, if some legs partially failed) to sync internal state.

        Args:
            close_ratio: Fraction of the cluster's current total size to
                         close, in (0.0, 1.0]. Typically taken directly from
                         `RiskAction.close_ratio`.
            method: FIFO closes oldest units first, which matches OANDA's
                    mandatory FIFO closing rule for retail US-style accounts
                    and is the safest default. PRO_RATA trims every unit by
                    the same ratio instead — use this only if your broker/
                    account mode (e.g. MT5 hedging) allows closing arbitrary
                    tickets and you specifically want to preserve the
                    blended-entry profile rather than the newest/oldest cost basis.

        Returns:
            List of UnitCloseInstruction, one per currently-open unit
            (close_size will be 0.0 for units unaffected by a FIFO partial
            close that a smaller ratio doesn't reach).

        Raises:
            ValueError: if close_ratio is not in (0.0, 1.0].
        """
        if not (0.0 < close_ratio <= 1.0):
            raise ValueError(f"close_ratio must be in (0.0, 1.0], got {close_ratio}")

        instructions: List[UnitCloseInstruction] = []
        target_close_size = self.total_size * close_ratio

        if method == CloseAllocationMethod.FIFO:
            remaining_to_close = target_close_size
            for idx, u in enumerate(self.units):
                close_size = min(u.size, max(remaining_to_close, 0.0))
                remaining_to_close -= close_size
                instructions.append(
                    UnitCloseInstruction(
                        unit_index=idx,
                        entry_price=u.entry_price,
                        entry_time=u.entry_time,
                        trade_id=u.trade_id,
                        original_size=u.size,
                        close_size=close_size,
                        remaining_size=u.size - close_size,
                    )
                )
        elif method == CloseAllocationMethod.PRO_RATA:
            for idx, u in enumerate(self.units):
                close_size = u.size * close_ratio
                instructions.append(
                    UnitCloseInstruction(
                        unit_index=idx,
                        entry_price=u.entry_price,
                        entry_time=u.entry_time,
                        trade_id=u.trade_id,
                        original_size=u.size,
                        close_size=close_size,
                        remaining_size=u.size - close_size,
                    )
                )
        else:
            raise ValueError(f"Unknown CloseAllocationMethod: {method}")

        return instructions

    def apply_close(self, instructions: List[UnitCloseInstruction]) -> None:
        """
        Sync internal unit sizes after `close_allocation()`'s instructions
        have actually been executed at the broker.

        Units whose `remaining_size` rounds to ~0 are dropped entirely. If
        every unit is fully closed, the underlying `DynamicRiskManager` is
        marked CLOSED.

        IMPORTANT — R-anchor is NEVER touched here, same rationale as
        `add_unit()`: a partial close is a sizing/accounting event, not a
        change to the original thesis's reference point. `entry_price_0` /
        `r_unit_0` on the shared risk manager stay exactly as set at
        construction; only `self.units` (and therefore `blended_entry` /
        `metrics().total_risk`) changes here.

        Args:
            instructions: The list returned by `close_allocation()` — pass
                          it back unmodified (or with `close_size` adjusted
                          down for any leg that only partially filled).

        Raises:
            ValueError: if `instructions` doesn't match the current unit
                        count/order (guards against a stale call after
                        another add/close has already mutated `self.units`),
                        or if any instruction's `trade_id` doesn't match the
                        corresponding unit's `trade_id` (guards against a
                        same-length but misaligned/reordered instruction list).
        """
        if len(instructions) != len(self.units):
            raise ValueError(
                f"Instruction count ({len(instructions)}) doesn't match current unit "
                f"count ({len(self.units)}) — cluster state has changed since "
                "close_allocation() was called. Re-run close_allocation() first."
            )

        surviving_units: List[PositionUnit] = []
        epsilon = 1e-9
        for instr, unit in zip(instructions, self.units):
            if instr.trade_id != unit.trade_id:
                raise ValueError(
                    f"apply_close() instruction/unit mismatch at position {instr.unit_index}: "
                    f"instruction trade_id={instr.trade_id!r} does not match the current unit's "
                    f"trade_id={unit.trade_id!r}. A same-length instruction list can still be "
                    "misaligned if it was reordered, filtered, or built against a stale "
                    "self.units snapshot after another add/close ran in between. Re-run "
                    "close_allocation() against the CURRENT cluster and retry."
                )
            new_size = instr.remaining_size
            if new_size > epsilon:
                surviving_units.append(
                    PositionUnit(
                        size=new_size,
                        entry_price=unit.entry_price,
                        entry_time=unit.entry_time,
                        trade_id=unit.trade_id,
                    )
                )

        self.units = surviving_units

        if not self.units:
            self.risk_manager.mark_closed()

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def metrics(self) -> ClusterMetrics:
        """Return a snapshot of current cluster-wide metrics."""
        shared_sl = self.risk_manager.current_sl
        total_risk = self._projected_total_risk(self.units, shared_sl)
        return ClusterMetrics(
            unit_count=len(self.units),
            unit_sizes=[u.size for u in self.units],
            total_size=self.total_size,
            blended_entry=self.blended_entry,
            shared_sl=shared_sl,
            total_risk=total_risk,
            state=self.risk_manager.state,
        )

    # ------------------------------------------------------------------
    # Serialization (for cross-process persistence — see cluster_state_store.py)
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """
        Full serialization of the cluster: every unit (including trade_id)
        plus the complete shared DynamicRiskManager state. Does NOT include
        `risk_calculator` — it's a live callable (typically dependent on a
        current FX conversion rate) that can't be meaningfully frozen into
        JSON; re-supply it via `from_dict(..., risk_calculator=...)` on
        every restore instead of expecting it to round-trip.
        """
        return {
            "units": [u.to_dict() for u in self.units],
            "max_size_decay_ratio": self.max_size_decay_ratio,
            "risk_manager": self.risk_manager.to_dict(),
        }

    @classmethod
    def from_dict(
        cls,
        data: dict,
        risk_calculator: Optional[Callable[[float, float], float]] = None,
    ) -> "PyramidCluster":
        """
        Reconstruct a PyramidCluster from `to_dict()` output.

        Args:
            data: Dict as produced by `to_dict()`.
            risk_calculator: Must be re-supplied explicitly (not persisted —
                              see `to_dict()` docstring). Defaults to
                              `DEFAULT_RISK_CALCULATOR` (raw notional) if
                              omitted, same as a fresh `__init__` would.

        Returns:
            A PyramidCluster with `units` (including trade_id) and the
            shared `risk_manager` restored exactly as saved.

        Raises:
            KeyError: if a required field is missing from `data`.
        """
        obj = cls.__new__(cls)  # bypass __init__ — it forces a single base unit and a
                                # freshly-computed initial SL, neither of which apply on restore
        obj.max_size_decay_ratio = data["max_size_decay_ratio"]
        obj.risk_calculator = risk_calculator or DEFAULT_RISK_CALCULATOR
        obj.units = [PositionUnit.from_dict(u) for u in data["units"]]
        obj.risk_manager = DynamicRiskManager.from_dict(data["risk_manager"])
        return obj


# ---------------------------------------------------------------------------
# Example usage — base entry + two safe pyramiding adds
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    entry_time = datetime(2026, 8, 1, 0, 0)
    cfg = RiskConfig(atr_multiplier_init=2.0, be_trigger_r=1.0, t_expected_hours=48)

    # Account-currency-aware risk calculator for this example: assume JPY-quoted pair,
    # a fixed illustrative USD/JPY conversion, and that `size` is in AUD (base currency)
    # units. In production, replace with your actual pip-value / margin calculation.
    USDJPY_APPROX = 150.0

    def jpy_cross_risk_calculator(size: float, price_distance: float) -> float:
        # size (AUD units) * price_distance (JPY per AUD) = risk in JPY; convert to USD.
        risk_jpy = size * price_distance
        return risk_jpy / USDJPY_APPROX

    account_equity_usd = 50_000.0
    risk_pct_max = 0.01  # 1% of equity per cluster
    max_allowed_risk = account_equity_usd * risk_pct_max
    print(f"Max allowed aggregate risk: ${max_allowed_risk:.2f}\n")

    # --- Step 1: base entry, long AUD/JPY, 10,000 units ---
    cluster = PyramidCluster(
        initial_size=10_000,
        entry_price=98.50,
        direction=1,
        atr_entry=0.35,
        entry_time=entry_time,
        config=cfg,
        structural_sl_level=97.80,
        max_size_decay_ratio=0.7,
        risk_calculator=jpy_cross_risk_calculator,
    )
    print("Base entry:", cluster.metrics())

    # --- Attempt an early add BEFORE break-even is reached: should be rejected ---
    early_check = cluster.can_add_unit(new_size=5_000, new_entry_price=98.70, max_allowed_risk=max_allowed_risk)
    print("\nEarly add attempt (pre-BE):", early_check.status.value, "-", early_check.reason)

    # --- Push the trade to +1R so BE triggers ---
    result = cluster.update(price=99.30, atr_now=0.34, highest_high=99.30, lowest_low=98.40, hours_elapsed=8)
    print("\nAfter update to price=99.30:", result.action.value, result.reason, "| state:", result.state.value)
    print("Cluster metrics:", cluster.metrics())

    # --- Step 2: first safe add (5,000 units = 50% of base 10,000) ---
    add1 = cluster.add_unit(
        new_size=5_000,
        new_entry_price=99.30,
        entry_time=datetime(2026, 8, 1, 8, 0),
        max_allowed_risk=max_allowed_risk,
    )
    print("\nAdd #1 (5,000 units @ 99.30):", add1.status.value, "-", add1.reason)
    print("Cluster metrics after add #1:", cluster.metrics())

    # --- Advance further, trail engages, then attempt a second add ---
    result2 = cluster.update(price=99.80, atr_now=0.32, highest_high=99.80, lowest_low=98.40, hours_elapsed=18)
    print("\nAfter update to price=99.80:", result2.action.value, result2.reason, "| state:", result2.state.value)
    print("Cluster metrics:", cluster.metrics())

    # --- Step 3: second safe add (2,500 units = 50% of prior add's 5,000) ---
    add2 = cluster.add_unit(
        new_size=2_500,
        new_entry_price=99.80,
        entry_time=datetime(2026, 8, 1, 18, 0),
        max_allowed_risk=max_allowed_risk,
    )
    print("\nAdd #2 (2,500 units @ 99.80):", add2.status.value, "-", add2.reason)
    print("Cluster metrics after add #2:", cluster.metrics())

    # --- Attempt an oversized third add: should be rejected on sizing-decay ---
    oversized_check = cluster.can_add_unit(new_size=2_000, new_entry_price=99.90, max_allowed_risk=max_allowed_risk)
    print("\nOversized add attempt (2,000 > 70% of 2,500=1,750):", oversized_check.status.value, "-", oversized_check.reason)

    print("\nCluster state before stagnation:")
    print(cluster.metrics())

    # --- Simulate the trade stalling well past T with the cluster still under 1R,
    #     forcing the shared risk manager to issue a PARTIAL_CLOSE ---
    print("\n--- Simulating stagnation -> time-decay PARTIAL_CLOSE ---")
    stall_result = cluster.update(
        price=99.00, atr_now=0.30, highest_high=99.80, lowest_low=98.40, hours_elapsed=60
    )
    print(f"update() -> action={stall_result.action.value} close_ratio={stall_result.close_ratio} "
          f"reason={stall_result.reason}")

    if stall_result.action == ActionType.PARTIAL_CLOSE:
        # Step A: ask the cluster how to fan the close out across tickets (FIFO = OANDA default)
        instructions = cluster.close_allocation(stall_result.close_ratio, method=CloseAllocationMethod.FIFO)
        print("\nFIFO close_allocation() instructions:")
        for instr in instructions:
            print(
                f"  unit[{instr.unit_index}] entry={instr.entry_price} "
                f"orig_size={instr.original_size} close_size={instr.close_size} "
                f"remaining_size={instr.remaining_size}"
            )

        # Step B: (in production) send `close_size` orders to the broker for each unit here,
        # confirm fills, then sync internal state:
        cluster.apply_close(instructions)
        print("\nCluster metrics after apply_close():")
        print(cluster.metrics())
        # This is the architecture-review fix in action: R stays anchored to the ORIGINAL
        # base entry (98.50) and initial R-unit, not the post-close blended entry (99.33).
        # Without the fix, this would read negative right after a de-risking action.
        print(
            f"R immediately after the de-risking close, at price=99.00: "
            f"{cluster.risk_manager.unrealized_r(99.00):+.2f}R (anchored to entry_price_0="
            f"{cluster.risk_manager.entry_price_0}, NOT blended_entry={cluster.blended_entry:.2f})"
        )

    print("\nFinal cluster state:")
    print(cluster.metrics())