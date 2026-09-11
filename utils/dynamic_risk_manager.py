r"""
utils/dynamic_risk_manager.py
========================

Modular Dynamic SL/TP Risk Management Layer for swing/trend-following FX strategies.

Designed to plug into a position-management layer such as `custom_strategy_v1.py`
(JPY-cross trend strategy). Follows the same pattern as the rest of the codebase:
all behavior-changing logic is gated behind explicit config flags, defaulting to
conservative/off where relevant, so it can be integrated incrementally without
disturbing tested behavior.

State machine
-------------
    INIT -> BREAK_EVEN -> TRAILING_CHANDELIER -> {TIME_DECAY_REDUCE, TIME_DECAY_EXIT}
                                              \-> CLOSED (manual/TP/SL hit)

Transitions are one-directional except CLOSED, which is terminal. TIME_DECAY_REDUCE
and TRAILING_CHANDELIER can co-occur (a position can be in a tightened trail AND
have already had a partial time-based reduction) — `time_reduced` is tracked
independently of `state` for this reason.

Usage
-----
See `__main__` block at the bottom for a cycle-by-cycle example.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enums & structured outputs
# ---------------------------------------------------------------------------

class RiskStateEnum(Enum):
    """Position lifecycle states for the risk manager's internal state machine."""
    INIT = "INIT"                                  # Initial ATR-based SL, no BE yet
    BREAK_EVEN = "BREAK_EVEN"                       # SL moved to entry + buffer
    TRAILING_CHANDELIER = "TRAILING_CHANDELIER"     # Actively trailing via Chandelier Exit
    TIME_DECAY_REDUCE = "TIME_DECAY_REDUCE"         # Partial close issued due to stagnation
    TIME_DECAY_EXIT = "TIME_DECAY_EXIT"             # Full close issued due to thesis decay
    CLOSED = "CLOSED"                               # Position fully closed, manager inert


class ActionType(Enum):
    """Discrete actions the risk manager can instruct the execution layer to take."""
    NO_CHANGE = "NO_CHANGE"
    UPDATE_SL = "UPDATE_SL"
    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    FULL_CLOSE = "FULL_CLOSE"


@dataclass
class RiskAction:
    """
    Structured output returned by every `DynamicRiskManager.update()` call.

    Attributes:
        action: The instruction for the execution layer to carry out.
        new_sl: New stop-loss price, present only when action == UPDATE_SL
                (or accompanying a PARTIAL_CLOSE that also tightens SL).
        close_ratio: Fraction of the *current remaining* position to close,
                     in (0.0, 1.0]. 1.0 for FULL_CLOSE. Present only for
                     PARTIAL_CLOSE / FULL_CLOSE.
        reason: Human-readable explanation, safe to log verbatim.
        state: The RiskStateEnum the position is in *after* this update.
    """
    action: ActionType
    new_sl: Optional[float] = None
    close_ratio: Optional[float] = None
    reason: str = ""
    state: RiskStateEnum = RiskStateEnum.INIT


@dataclass
class RiskConfig:
    """
    Tunable parameters for the risk manager. Centralize per-strategy overrides
    here (e.g. one RiskConfig for trend setups, another for mean-reversion),
    mirroring the config.py pattern used elsewhere in the project.

    All new behavior is opt-in via the `enable_*` flags so this can be
    integrated into custom_strategy_v1.py incrementally.
    """
    # --- Initial SL sizing ---
    atr_multiplier_init: float = 2.0        # N in SL = entry - N*ATR (trend default)

    # --- Break-even ---
    be_trigger_r: float = 1.0               # Move to BE once profit >= this many R
    be_buffer_atr_frac: float = 0.05        # BE buffer = this fraction of current ATR (spread/slippage guard)

    # --- Chandelier trailing ---
    chandelier_lookback_note: str = "caller supplies rolling highest_high/lowest_low"
    chandelier_k_default: float = 3.0
    chandelier_k_tighten_at_2r: float = 2.0
    chandelier_k_tighten_at_1_5r: float = 2.5
    chandelier_k_time_decay_lock: float = 1.0   # aggressive lock-in once time-decayed but in profit

    # --- Time-decay / time-stop ---
    enable_time_stop: bool = True
    t_expected_hours: float = 48.0           # historical median hours-to-TP for this setup; calibrate per strategy
    time_reduce_threshold: float = 1.0      # t/T ratio to trigger partial reduce if under 1R
    time_reduce_ratio: float = 0.5          # fraction of position to close on time-reduce
    time_exit_threshold: float = 1.5        # t/T ratio to trigger full exit if still under 1R
    time_tighten_threshold: float = 1.5     # t/T ratio to force aggressive trail if position IS in profit
    vol_compression_frac: float = 0.6       # ATR_now < this * ATR_entry => volatility compressed (stagnation confirm)

    # --- Safety / edge cases ---
    min_sl_step_atr_frac: float = 0.02      # ignore SL updates smaller than this (avoid order-spam on noise)
    slippage_buffer_atr_frac: float = 0.03  # extra buffer added to BE/trail levels to absorb fill slippage

    def to_dict(self) -> dict:
        """Serialize to a plain dict of built-in types (safe for json.dumps)."""
        from dataclasses import asdict
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RiskConfig":
        """
        Reconstruct from a dict produced by `to_dict()`.

        Unknown/extra keys in `data` are ignored (forward-compat: a config
        saved by an older/newer version with extra fields won't crash this).
        Missing keys fall back to the dataclass defaults, so a state file
        saved before a new RiskConfig field was added still loads cleanly.
        """
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)


# ---------------------------------------------------------------------------
# Core risk manager
# ---------------------------------------------------------------------------

class DynamicRiskManager:
    """
    Per-position dynamic SL/TP and time-decay manager.

    One instance tracks exactly one open position (or one pyramided cluster
    sharing a unified SL). Instantiate at trade entry, call `update()` once
    per cycle (e.g. once per 15-minute cron tick) with fresh market
    data, and act on the returned RiskAction.

    Non-regressive guarantee: `current_sl` is monotonically favorable for the
    trade direction — it will never be moved back toward the entry/against
    the position once tightened, enforced in `_apply_sl_candidate`.
    """

    def __init__(
        self,
        entry_price: float,
        direction: int,
        atr_entry: float,
        entry_time: datetime,
        config: Optional[RiskConfig] = None,
        structural_sl_level: Optional[float] = None,
    ) -> None:
        """
        Args:
            entry_price: Fill price of the position.
            direction: +1 for long, -1 for short.
            atr_entry: ATR value at time of entry (defines the R-unit).
            entry_time: Timestamp of entry, used for time-decay calculations.
            config: RiskConfig instance; defaults applied if omitted.
            structural_sl_level: Optional structural invalidation level
                (e.g. last swing low/high). If provided, the initial SL is
                the *more conservative* of the ATR-based and structural level
                (never placed inside/through structure).

        Raises:
            ValueError: if direction is not +1 or -1, or atr_entry <= 0.
        """
        if direction not in (1, -1):
            raise ValueError(f"direction must be +1 (long) or -1 (short), got {direction}")
        if atr_entry <= 0:
            raise ValueError(f"atr_entry must be positive, got {atr_entry}")

        # entry_price_0 / r_unit_0 are the FIXED R-multiple anchor. R is a thesis-progress
        # signal calibrated against the *original* entry and initial ATR-based stop distance —
        # it must never be recomputed from a blended entry or the current SL after pyramiding
        # or partial closes, or every R-based threshold (BE, 1.5R/2R/3R tightening, time-decay)
        # silently starts meaning something different mid-trade. See architecture review notes.
        # Exposed only via read-only properties below — no setter is provided, by design.
        self._entry_price_0 = entry_price
        self.direction = direction
        self.atr_entry = atr_entry
        self.entry_time = entry_time
        self.cfg = config or RiskConfig()

        self.state: RiskStateEnum = RiskStateEnum.INIT
        self.chandelier_k: float = self.cfg.chandelier_k_default
        self.time_reduce_fired: bool = False  # has the ONE-TIME partial time-reduce already fired?

        self._current_sl: float = self._compute_initial_sl(structural_sl_level)
        self._r_unit_0: float = abs(self._entry_price_0 - self._current_sl)
        if self._r_unit_0 <= 0:
            raise ValueError("Computed R-unit is zero — check ATR/structural inputs.")

        self._last_action_reason: str = "Position initialized."

    # ------------------------------------------------------------------
    # Immutable R-anchor (read-only) properties
    # ------------------------------------------------------------------

    @property
    def entry_price_0(self) -> float:
        """The ORIGINAL base-unit entry price. Fixed for the life of the position/cluster."""
        return self._entry_price_0

    @property
    def r_unit_0(self) -> float:
        """The ORIGINAL R-unit (|entry_price_0 - initial SL|). Fixed for the life of the position/cluster."""
        return self._r_unit_0

    @property
    def current_sl(self) -> float:
        """Current stop-loss price. Mutates only via `_apply_sl_candidate` (non-regressive)."""
        return self._current_sl

    # ------------------------------------------------------------------
    # Initialization helpers
    # ------------------------------------------------------------------

    def _compute_initial_sl(self, structural_sl_level: Optional[float]) -> float:
        """ATR-based SL, clamped to never sit inside a supplied structural level."""
        raw_sl = self._entry_price_0 - self.direction * self.cfg.atr_multiplier_init * self.atr_entry
        if structural_sl_level is not None:
            if self.direction == 1:
                raw_sl = min(raw_sl, structural_sl_level)  # SL must be <= structural support
            else:
                raw_sl = max(raw_sl, structural_sl_level)  # SL must be >= structural resistance
        return raw_sl

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def unrealized_r(self, price: float) -> float:
        """
        Current unrealized profit expressed in multiples of the FIXED, original R-unit.
        Always measured against entry_price_0 / r_unit_0 — never against a blended entry
        or the current SL — so this number means the same thing at every point in the
        trade's life, including after pyramiding adds or partial closes.
        """
        return self.direction * (price - self._entry_price_0) / self._r_unit_0

    def hours_elapsed(self, current_time: datetime) -> float:
        """
        Wall-clock hours elapsed since entry (current_time - entry_time, in hours).
        This is independent of candle/bar timeframe — a 15-minute cron cycle
        checking an H4-timeframe entry both use the same wall-clock hour count.
        If you already track elapsed hours directly elsewhere, prefer passing
        `update(..., hours_elapsed=...)` instead, to avoid a redundant computation.
        """
        delta = current_time - self.entry_time
        return delta.total_seconds() / 3600.0

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
        Main entry point — call once per cycle (e.g. once per 15-minute cron
        invocation) with fresh market data.

        Args:
            price: Current price.
            atr_now: Current ATR reading (same period as used at entry, e.g. ATR(14)).
            highest_high: Rolling highest high since entry (for long Chandelier calc).
            lowest_low: Rolling lowest low since entry (for short Chandelier calc).
            current_time: Current timestamp. Required unless hours_elapsed is given.
            hours_elapsed: Direct wall-clock hours since entry. Takes precedence over
                          current_time-derived calculation if supplied — prefer
                          this if you already track it, to skip a redundant computation.

        Returns:
            RiskAction describing what the execution layer should do this cycle.

        Notes:
            - If the position is already CLOSED, always returns NO_CHANGE.
            - SL updates are suppressed if the candidate move is smaller than
              `min_sl_step_atr_frac * atr_now`, to avoid order-modify spam on
              insignificant noise.
        """
        if self.state == RiskStateEnum.CLOSED:
            return RiskAction(action=ActionType.NO_CHANGE, reason="Position already closed.", state=self.state)

        if hours_elapsed is None:
            if current_time is None:
                raise ValueError("Must supply either current_time or hours_elapsed.")
            hours_elapsed = self.hours_elapsed(current_time)

        r = self.unrealized_r(price)
        sl_candidate: Optional[float] = None
        action = ActionType.NO_CHANGE
        close_ratio: Optional[float] = None
        reason_parts = []

        # ---- 1. Break-even check ----
        if self.state == RiskStateEnum.INIT and r >= self.cfg.be_trigger_r:
            buffer = (self.cfg.be_buffer_atr_frac + self.cfg.slippage_buffer_atr_frac) * atr_now
            sl_candidate = self._entry_price_0 + self.direction * buffer
            self.state = RiskStateEnum.BREAK_EVEN
            reason_parts.append(f"BE triggered at {r:.2f}R.")

        # ---- 2. Chandelier trail (active once BE has been reached or passed) ----
        if self.state in (RiskStateEnum.BREAK_EVEN, RiskStateEnum.TRAILING_CHANDELIER,
                          RiskStateEnum.TIME_DECAY_REDUCE):
            if r >= 2.0:
                self.chandelier_k = min(self.chandelier_k, self.cfg.chandelier_k_tighten_at_2r)
            elif r >= 1.5:
                self.chandelier_k = min(self.chandelier_k, self.cfg.chandelier_k_tighten_at_1_5r)

            extreme = highest_high if self.direction == 1 else lowest_low
            chandelier_sl = extreme - self.direction * self.chandelier_k * atr_now
            sl_candidate = chandelier_sl if sl_candidate is None else (
                max(sl_candidate, chandelier_sl) if self.direction == 1 else min(sl_candidate, chandelier_sl)
            )
            if self.state == RiskStateEnum.BREAK_EVEN:
                self.state = RiskStateEnum.TRAILING_CHANDELIER
                reason_parts.append("Chandelier trailing engaged.")

        # ---- 3. Time-decay logic ----
        # Guard the whole block on state: once TIME_DECAY_EXIT (or CLOSED) has been reached,
        # no further time-decay action should fire — prevents a stale/lagging execution layer
        # from re-triggering time-decay logic on a position that's already being wound down.
        if self.cfg.enable_time_stop and self.state not in (RiskStateEnum.TIME_DECAY_EXIT, RiskStateEnum.CLOSED):
            t_frac = hours_elapsed / self.cfg.t_expected_hours
            vol_compressed = atr_now < self.cfg.vol_compression_frac * self.atr_entry

            # IMPORTANT: full-exit eligibility is evaluated independently of time_reduce_fired.
            # A prior partial reduce must NOT suppress a later full exit — otherwise a position
            # that got a 50% time-based haircut can stagnate forever afterward with no further
            # governance, which is exactly the "stuck trade" failure mode this framework exists
            # to prevent. Full exit takes priority over partial in the same bar (elif below).
            if t_frac > self.cfg.time_exit_threshold and r < 1.0 and vol_compressed:
                # Full time-stop exit: thesis decayed, price never delivered, vol dying confirms stagnation.
                action = ActionType.FULL_CLOSE
                close_ratio = 1.0
                self.state = RiskStateEnum.TIME_DECAY_EXIT
                reason_parts.append(
                    f"Time-stop FULL_CLOSE: t/T={t_frac:.2f} > {self.cfg.time_exit_threshold}, "
                    f"r={r:.2f}R < 1.0R, ATR compressed ({atr_now:.5f} < "
                    f"{self.cfg.vol_compression_frac}*{self.atr_entry:.5f})."
                )

            elif not self.time_reduce_fired and t_frac > self.cfg.time_reduce_threshold and r < 1.0:
                # Partial reduction fires ONCE: give it less time/size, tighten toward invalidation.
                # If stagnation continues afterward, the full-exit branch above can still fire later.
                action = ActionType.PARTIAL_CLOSE
                close_ratio = self.cfg.time_reduce_ratio
                self.time_reduce_fired = True
                self.state = RiskStateEnum.TIME_DECAY_REDUCE
                reason_parts.append(
                    f"Time-stop PARTIAL_CLOSE ({close_ratio:.0%}): t/T={t_frac:.2f} > "
                    f"{self.cfg.time_reduce_threshold}, r={r:.2f}R < 1.0R."
                )

            elif t_frac > self.cfg.time_tighten_threshold and r >= 1.0:
                # In profit but stalled beyond expected horizon: lock in aggressively, don't exit.
                if self.chandelier_k > self.cfg.chandelier_k_time_decay_lock:
                    self.chandelier_k = self.cfg.chandelier_k_time_decay_lock
                    extreme = highest_high if self.direction == 1 else lowest_low
                    tightened_sl = extreme - self.direction * self.chandelier_k * atr_now
                    sl_candidate = tightened_sl if sl_candidate is None else (
                        max(sl_candidate, tightened_sl) if self.direction == 1 else min(sl_candidate, tightened_sl)
                    )
                    reason_parts.append(
                        f"Stalled-in-profit tighten: t/T={t_frac:.2f}, r={r:.2f}R, chandelier_k -> "
                        f"{self.cfg.chandelier_k_time_decay_lock}."
                    )

        # ---- 4. Apply SL candidate (non-regressive, min-step filtered) ----
        if sl_candidate is not None and action == ActionType.NO_CHANGE:
            applied = self._apply_sl_candidate(sl_candidate, atr_now)
            if applied:
                action = ActionType.UPDATE_SL
        elif sl_candidate is not None and action == ActionType.PARTIAL_CLOSE:
            # A partial close can co-occur with an SL tightening in the same bar.
            self._apply_sl_candidate(sl_candidate, atr_now)

        reason = " ".join(reason_parts) if reason_parts else "No state change."
        self._last_action_reason = reason

        return RiskAction(
            action=action,
            new_sl=self.current_sl if action in (ActionType.UPDATE_SL, ActionType.PARTIAL_CLOSE) else None,
            close_ratio=close_ratio,
            reason=reason,
            state=self.state,
        )

    # ------------------------------------------------------------------
    # Internal safety helpers
    # ------------------------------------------------------------------

    def _apply_sl_candidate(self, candidate_sl: float, atr_now: float) -> bool:
        """
        Enforce non-regressive trailing: only move SL in the favorable direction,
        and only if the move exceeds the minimum noise-filtering step.

        Returns:
            True if current_sl was actually updated, False if suppressed
            (either non-favorable direction or below min-step threshold).
        """
        favorable = (candidate_sl > self._current_sl) if self.direction == 1 else (candidate_sl < self._current_sl)
        if not favorable:
            return False

        min_step = self.cfg.min_sl_step_atr_frac * atr_now
        if abs(candidate_sl - self._current_sl) < min_step:
            return False

        self._current_sl = candidate_sl
        return True

    def mark_closed(self) -> None:
        """Call when the position is fully closed externally (TP hit, manual close, etc.)."""
        self.state = RiskStateEnum.CLOSED

    def snapshot(self) -> dict:
        """Return a serializable dict of current manager state, for logging/persistence."""
        return {
            "state": self.state.value,
            "current_sl": self._current_sl,
            "chandelier_k": self.chandelier_k,
            "time_reduce_fired": self.time_reduce_fired,
            "entry_price_0": self._entry_price_0,
            "direction": self.direction,
            "r_unit_0": self._r_unit_0,
            "last_action_reason": self._last_action_reason,
        }

    # ------------------------------------------------------------------
    # Serialization (for cross-process persistence — see cluster_state_store.py)
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """
        Full serialization of everything needed to exactly reconstruct this
        manager's state via `from_dict()` — including the frozen R-anchor,
        current SL, state machine position, and the RiskConfig snapshot that
        was in effect when this position was opened (config is snapshotted at
        construction time and intentionally never re-read from live config.py
        on restore, so an in-flight trade isn't silently re-governed by
        different thresholds if global config changes later).

        All values are JSON-safe built-in types (datetime -> ISO 8601 string).
        """
        return {
            "entry_price_0": self._entry_price_0,
            "r_unit_0": self._r_unit_0,
            "current_sl": self._current_sl,
            "direction": self.direction,
            "atr_entry": self.atr_entry,
            "entry_time": self.entry_time.isoformat(),
            "state": self.state.value,
            "chandelier_k": self.chandelier_k,
            "time_reduce_fired": self.time_reduce_fired,
            "last_action_reason": self._last_action_reason,
            "config": self.cfg.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DynamicRiskManager":
        """
        Reconstruct a DynamicRiskManager from `to_dict()` output, restoring
        the EXACT prior state — including `current_sl` as it was last left,
        NOT recomputed from atr_entry/structural_sl_level as the normal
        `__init__` would. This deliberately bypasses `__init__`'s initial-SL
        computation: on restore, the position may have trailed far past
        where `__init__` would place it, and re-deriving `current_sl` from
        scratch would silently un-trail a stop that had already tightened.

        Args:
            data: Dict as produced by `to_dict()`.

        Returns:
            A DynamicRiskManager with `state`, `current_sl`, `entry_price_0`,
            `r_unit_0`, `chandelier_k`, and `time_reduce_fired` all restored
            exactly as saved.

        Raises:
            KeyError: if a required field is missing from `data`.
            ValueError: if `state` is not a recognized RiskStateEnum value.
        """
        obj = cls.__new__(cls)  # bypass __init__ entirely — see docstring
        obj._entry_price_0 = data["entry_price_0"]
        obj._r_unit_0 = data["r_unit_0"]
        obj._current_sl = data["current_sl"]
        obj.direction = data["direction"]
        obj.atr_entry = data["atr_entry"]
        obj.entry_time = datetime.fromisoformat(data["entry_time"])
        obj.state = RiskStateEnum(data["state"])
        obj.chandelier_k = data["chandelier_k"]
        obj.time_reduce_fired = data["time_reduce_fired"]
        obj._last_action_reason = data.get("last_action_reason", "Restored from saved state.")
        obj.cfg = RiskConfig.from_dict(data["config"])
        return obj


# ---------------------------------------------------------------------------
# Example usage — cycle-by-cycle simulation (e.g. one 15-min cron tick each)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import timedelta

    # --- Example: long AUD/JPY swing trade ---
    entry_time = datetime(2026, 8, 1, 0, 0)
    cfg = RiskConfig(
        atr_multiplier_init=2.0,
        be_trigger_r=1.0,
        t_expected_hours=48,     # e.g. ~2 days, calibrate from backtest history (hours-to-TP)
        enable_time_stop=True,
    )

    rm = DynamicRiskManager(
        entry_price=98.50,
        direction=1,             # long
        atr_entry=0.35,
        entry_time=entry_time,
        config=cfg,
        structural_sl_level=97.80,  # last swing low
    )

    print("Initial SL:", rm.current_sl, "| R-unit_0:", rm.r_unit_0)

    # Simulated cycle-by-cycle market data: (price, atr, highest_high, lowest_low, hours_since_entry)
    # This sequence is chosen to demonstrate the fixed time-decay bug: a partial reduce fires
    # first (t/T > 1.0, r < 1R), and stagnation THEN continues past t/T > 1.5 with r still < 1R
    # -> the full exit must still be reachable afterward (previously suppressed by time_reduced).
    simulated_cycles = [
        (98.70, 0.34, 98.70, 98.40, 4),
        (98.90, 0.33, 98.90, 98.40, 55),   # t/T=1.15, r=0.57R < 1.0R -> expect PARTIAL_CLOSE (time-reduce)
        (98.85, 0.30, 98.90, 98.40, 60),   # still stalled, t/T=1.25 -> time_reduce_fired blocks re-firing partial
        (98.80, 0.20, 98.90, 98.40, 75),   # t/T=1.56, r=0.43R < 1.0R, ATR compressed -> expect FULL_CLOSE now
    ]

    for price, atr_now, hh, ll, hours in simulated_cycles:
        result = rm.update(
            price=price,
            atr_now=atr_now,
            highest_high=hh,
            lowest_low=ll,
            hours_elapsed=hours,
        )
        print(
            f"t={hours:>3}h price={price:.2f} r={rm.unrealized_r(price):+.2f}R "
            f"-> action={result.action.value:<14} new_sl={result.new_sl} "
            f"close_ratio={result.close_ratio} state={result.state.value}"
        )
        if result.reason != "No state change.":
            print(f"        reason: {result.reason}")

    print("\nFinal snapshot:", rm.snapshot())