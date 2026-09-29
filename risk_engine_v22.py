import numpy as np
import math
import time
import json
import os
import uuid
from enum import Enum, auto
from dataclasses import dataclass, asdict
from typing import Dict, Any, Optional, Tuple, List
from datetime import datetime, timezone


class ExitDecision(Enum):
    HOLD = auto()
    EXIT_READY = auto()
    EXIT_PENDING = auto()
    INSUFFICIENT_DATA = auto()


class TradeState(Enum):
    EXISTS = auto()
    CLOSED = auto()
    UNKNOWN = auto()


class SLUpdateResult(Enum):
    UPDATED = auto()
    NO_CHANGE = auto()
    REJECTED_INVALID_DISTANCE = auto()
    FAILED_PROTECTION_MISSING = auto()
    BROKER_ERROR = auto()  # Broker API failure (not a distance violation)


class ReconcileStatus(Enum):
    SUCCESS_CLOSED = auto()
    ALREADY_CLOSED = auto()
    PARTIAL_FILL = auto()
    EXECUTION_REJECTED = auto()
    STATE_UNKNOWN = auto()
    NO_ACTION_TAKEN = auto()


@dataclass
class ReconciliationReport:
    status: ReconcileStatus
    trade_id: str
    remaining_units: float
    message: str


@dataclass
class PendingExitState:
    trade_id: str
    symbol: str
    side: str
    trigger_time_utc: str
    pending_duration_hours: float
    reason: str


class CompleteCandleFilter:
    @staticmethod
    def filter_completed_h1(candles: List[Dict[str, Any]]) -> List[float]:
        return [float(c["close"]) for c in candles if c.get("complete", False) is True]

    @staticmethod
    def filter_completed_daily(candles: List[Dict[str, Any]]) -> List[Dict[str, float]]:
        return [
            {"high": float(c["high"]), "low": float(c["low"]),
             "close": float(c["close"]), "time": str(c.get("time", ""))}
            for c in candles if c.get("complete", False) is True
        ]


class DailyBoundaryAligner:
    # Diagnostic counters (non-persistent, reset per process).
    # Used to quantify how often the "fails open" True branch is hit
    # before deciding whether to enforce alignment as a hard gate.
    empty_hit_count: int = 0
    parse_error_count: int = 0
    total_calls: int = 0

    @staticmethod
    def is_daily_close_aligned(candle_time_iso: str) -> bool:
        DailyBoundaryAligner.total_calls += 1
        if not candle_time_iso:
            DailyBoundaryAligner.empty_hit_count += 1
            # NOTE: currently FAILS-OPEN (True = no skip). For the first
            # observation period we only count occurrences; enforcement is
            # off.  Once a baseline is collected this will be revisited with
            # a REQUIRE_DAILY_ALIGNED flag.
            print(
                f"  [DIAG] DailyBoundaryAligner: empty candle time "
                f"(#{DailyBoundaryAligner.empty_hit_count}/"
                f"{DailyBoundaryAligner.total_calls} total) — "
                f"currently allows SL update; review before enforcing."
            )
            return True
        try:
            dt = datetime.fromisoformat(candle_time_iso.replace("Z", "+00:00"))
            return dt.hour in (21, 22) and (dt.minute < 6 or dt.minute >= 55)
        except ValueError:
            DailyBoundaryAligner.parse_error_count += 1
            print(
                f"  [DIAG] DailyBoundaryAligner: unparseable candle time "
                f"(#{DailyBoundaryAligner.parse_error_count}/"
                f"{DailyBoundaryAligner.total_calls} total): {candle_time_iso!r} "
                f"— currently allows SL update; review before enforcing."
            )
            return True


class StateStore:
    def __init__(self, storage_path: str = "./risk_state.json"):
        self.storage_path = storage_path

    def save_pending_exits(self, pending: Dict[str, PendingExitState]) -> None:
        data = {tid: asdict(s) for tid, s in pending.items()}
        temp = f"{self.storage_path}.tmp"
        with open(temp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(temp, self.storage_path)

    def load_pending_exits(self) -> Dict[str, PendingExitState]:
        if not os.path.exists(self.storage_path):
            return {}
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {tid: PendingExitState(**d) for tid, d in data.items()}
        except Exception as e:
            print(f"[StateStore] ⚠️ Recovery failed, start fresh: {e}")
            return {}


class PairMomentumEngine:
    def __init__(self, fast_period: int = 10, slow_period: int = 20):
        self.fast = fast_period
        self.slow = slow_period

    def calculate_ratios(self, closes: List[float]) -> List[float]:
        if len(closes) < self.slow:
            return []
        ratios = []
        for i in range(self.slow, len(closes) + 1):
            window = closes[i - self.slow: i]
            fast_ma = float(np.mean(window[-self.fast:]))
            slow_ma = float(np.mean(window))
            if slow_ma <= 0.0 or not math.isfinite(fast_ma) or not math.isfinite(slow_ma):
                # Bad ticks / missing data produce non-positive MA or NaN / inf.
                # A nan ratio will never cross the deadzone (both sides of
                # comparison short-circuit to false), which is the safe
                # fail-closed outcome: we don't trigger a false exit.
                ratios.append(float("nan"))
            else:
                ratios.append(((fast_ma / slow_ma) - 1.0) * 100.0)
        return ratios


class ExitPolicyEngine:
    def __init__(self, deadzone_pct: float = 0.15, max_pending_hours: float = 4.0):
        self.deadzone = deadzone_pct
        self.max_pending = max_pending_hours

    def evaluate_exit(
        self,
        side: str,
        ratio_series: List[float],
        is_in_event_window: bool = False,
        pending_duration_hours: float = 0.0
    ) -> Tuple[ExitDecision, str]:
        if len(ratio_series) < 2:
            return ExitDecision.INSUFFICIENT_DATA, "Need >= 2 ratio points"
        r_prev, r_curr = ratio_series[-2], ratio_series[-1]
        # Fail-closed on bad / insufficient data: NaN ratios from tick gaps or
        # non-positive MAs do NOT count as a reversal signal; we hold.
        if not math.isfinite(r_prev) or not math.isfinite(r_curr):
            return ExitDecision.HOLD, "HOLD: ratio corrupted (NaN / bad tick / short history)"
        signal = False
        reason = "HOLD"
        if side == "LONG":
            if r_prev < 0.0 and r_curr < -self.deadzone:
                signal = True
                reason = f"LONG_REVERSAL prev={r_prev:.3f}% curr={r_curr:.3f}%"
        elif side == "SHORT":
            if r_prev > 0.0 and r_curr > self.deadzone:
                signal = True
                reason = f"SHORT_REVERSAL prev={r_prev:.3f}% curr={r_curr:.3f}%"
        if signal:
            if is_in_event_window:
                if pending_duration_hours >= self.max_pending:
                    return ExitDecision.EXIT_READY, f"TIMEOUT_FORCED_EXIT: {reason}"
                return ExitDecision.EXIT_PENDING, f"EVENT_WINDOW_DEFERRED: {reason}"
            return ExitDecision.EXIT_READY, f"SIGNAL_CONFIRMED: {reason}"
        return ExitDecision.HOLD, "Trend intact — hold position"


class WilderATR:
    @staticmethod
    def calculate_atr14(candles: List[Dict[str, float]], period: int = 14) -> float:
        if len(candles) < 100:
            # Rather than raise ValueError and abort the whole SL loop for
            # every trade, surface the lack of data as a sentinel NaN so the
            # caller can fall back to NO_CHANGE with a structured log line.
            # A minimum of 100 bars (≈3+ months of daily data for each pair)
            # is required for Wilder smoothing to converge.
            return float("nan")
        high_vals = np.array([c["high"] for c in candles])
        low_vals = np.array([c["low"] for c in candles])
        close_vals = np.array([c["close"] for c in candles])
        tr = np.maximum(
            high_vals[1:] - low_vals[1:],
            np.maximum(np.abs(high_vals[1:] - close_vals[:-1]), np.abs(low_vals[1:] - close_vals[:-1]))
        )
        atr = float(np.mean(tr[:period]))
        for val in tr[period:]:
            atr = (atr * (period - 1) + val) / period
        return atr


class DailyTrailingProtection:
    def __init__(self, atr_multiplier: float = 1.5):
        self.atr_mult = atr_multiplier

    @staticmethod
    def _round_price(price: float, tick_size: float, side: str) -> float:
        prec = int(round(-math.log10(tick_size))) if tick_size < 1 else 0
        factor = 1.0 / tick_size
        if side == "LONG":
            val = math.floor(price * factor) / factor
        else:
            val = math.ceil(price * factor) / factor
        return round(val, prec)

    def validate_and_compute(
        self,
        side: str,
        current_broker_sl: Optional[float],
        current_bid: float,
        current_ask: float,
        daily_candles: List[Dict[str, float]],
        tick_size: float,
        min_stop_distance: float
    ) -> Tuple[SLUpdateResult, Optional[float], str]:
        if current_broker_sl is None:
            print(
                "🚨 [RISK] FAILED_PROTECTION_MISSING: trade has no SL attached. "
                "Expecting SL to be set by OANDA at entry; verify broker rejection."
            )
            return SLUpdateResult.FAILED_PROTECTION_MISSING, None, "No SL attached to trade"

        atr = WilderATR.calculate_atr14(daily_candles)
        if not math.isfinite(atr):
            return (
                SLUpdateResult.NO_CHANGE,
                current_broker_sl,
                f"Skip SL update: WilderATR needs ≥100 daily bars (got {len(daily_candles)})."
            )
        ref_price = daily_candles[-1]["close"]

        if side == "LONG":
            raw_candidate = ref_price - self.atr_mult * atr
            candidate_sl = self._round_price(raw_candidate, tick_size, "LONG")
            if candidate_sl <= current_broker_sl + tick_size:
                return SLUpdateResult.NO_CHANGE, current_broker_sl, "No meaningful tightening"
            if candidate_sl >= current_bid - min_stop_distance:
                return SLUpdateResult.REJECTED_INVALID_DISTANCE, current_broker_sl, "Violates Bid min-distance"
            return SLUpdateResult.UPDATED, candidate_sl, f"LONG_SL_UPDATED → {candidate_sl}"

        elif side == "SHORT":
            raw_candidate = ref_price + self.atr_mult * atr
            candidate_sl = self._round_price(raw_candidate, tick_size, "SHORT")
            if candidate_sl >= current_broker_sl - tick_size:
                return SLUpdateResult.NO_CHANGE, current_broker_sl, "No meaningful tightening"
            if candidate_sl <= current_ask + min_stop_distance:
                return SLUpdateResult.REJECTED_INVALID_DISTANCE, current_broker_sl, "Violates Ask min-distance"
            return SLUpdateResult.UPDATED, candidate_sl, f"SHORT_SL_UPDATED → {candidate_sl}"

        return SLUpdateResult.REJECTED_INVALID_DISTANCE, None, "Invalid side"


class ExecutionReconciler:
    def __init__(self, broker_adapter):
        self.broker = broker_adapter

    def reconcile_and_exit(
        self,
        trade_id: str,
        decision: ExitDecision,
        reason: str,
        client_request_id: str,
        max_retries: int = 3
    ) -> ReconciliationReport:
        if decision != ExitDecision.EXIT_READY:
            return ReconciliationReport(ReconcileStatus.NO_ACTION_TAKEN, trade_id, -1.0, f"Skipped: {decision}")

        state, units = self.broker.query_trade_state(trade_id)
        if state == TradeState.CLOSED:
            return ReconciliationReport(ReconcileStatus.ALREADY_CLOSED, trade_id, 0.0, "Already closed on broker")
        if state == TradeState.UNKNOWN:
            return ReconciliationReport(ReconcileStatus.STATE_UNKNOWN, trade_id, units, "Trade state UNKNOWN — NOT degraded")

        if not self.broker.send_close_order(trade_id, client_request_id):
            return ReconciliationReport(ReconcileStatus.EXECUTION_REJECTED, trade_id, units, "Close order rejected")

        backoff_delays = [0.5, 1.5, 3.0]
        # units_after is initialized to the pre-close position as a safe
        # default. If max_retries == 0 the loop body never executes and the
        # NameError / UnboundLocalError is avoided. If any retry runs, the
        # post-poll value overrides this initialiser.
        units_after = float(units) if units is not None else 0.0
        for attempt in range(max_retries):
            time.sleep(backoff_delays[min(attempt, len(backoff_delays) - 1)])
            state_after, units_after = self.broker.query_trade_state(trade_id)
            if state_after == TradeState.CLOSED or units_after == 0.0:
                return ReconciliationReport(ReconcileStatus.SUCCESS_CLOSED, trade_id, 0.0, f"Closed: {reason}")
            if state_after == TradeState.UNKNOWN:
                return ReconciliationReport(ReconcileStatus.STATE_UNKNOWN, trade_id, units_after, "State lost during poll")

        # PARTIAL_FILL: trade still has units after all retries. This should
        # be an extremely rare event on OANDA (full fills are the norm for
        # liquid FX at these sizes), so we always print a 🚨 diagnostic for
        # manual review, but do NOT attempt auto-reconciliation in-process —
        # the residual units become owner-ambiguous and need human review
        # before any further action is taken.
        print(
            f"🚨 [EXEC] PARTIAL_FILL trade={trade_id}: {units_after} units remain after "
            f"{max_retries} reconciliation retries. Residual position has no owner-tagged "
            f"action path. Do not allow the bot to re-enter on the same pair until resolved."
        )
        return ReconciliationReport(ReconcileStatus.PARTIAL_FILL, trade_id, units_after, f"Partial fill after {max_retries} retries")


class RiskManagementRunner:
    def __init__(self, broker_adapter, state_path: str = "./risk_state.json"):
        self.broker = broker_adapter
        self.state_store = StateStore(state_path)
        self.pending_exits: Dict[str, PendingExitState] = self.state_store.load_pending_exits()

        self.momentum = PairMomentumEngine(fast_period=10, slow_period=20)
        self.exit_policy = ExitPolicyEngine(deadzone_pct=0.15, max_pending_hours=4.0)
        self.sl_protection = DailyTrailingProtection(atr_multiplier=1.5)
        self.reconciler = ExecutionReconciler(broker_adapter)

    def prune_pending_exits(self, open_trade_ids: List[str]) -> int:
        """Remove pending entries for trades no longer open on the broker.

        This prevents the pending-exits map from accumulating stale entries
        for positions closed by broker SL/TP, manual intervention or
        unrecoverable partial fills.  Returns the number of entries removed.
        Should be called once per runner cycle, before per-trade processing.
        """
        open_set = set(open_trade_ids)
        stale_ids = [tid for tid in self.pending_exits.keys() if tid not in open_set]
        for tid in stale_ids:
            del self.pending_exits[tid]
        if stale_ids:
            self.state_store.save_pending_exits(self.pending_exits)
            print(
                f"  [RISK] pruned {len(stale_ids)} stale pending_exit entries: "
                f"{stale_ids}"
            )
        return len(stale_ids)

    def process_h1_bar(
        self,
        trade_id: str,
        symbol: str,
        side: str,
        raw_h1_candles: List[Dict[str, Any]],
        is_in_event_window: bool = False
    ) -> ReconciliationReport:
        closes = CompleteCandleFilter.filter_completed_h1(raw_h1_candles)

        pending_hours = 0.0
        if trade_id in self.pending_exits:
            then = datetime.fromisoformat(self.pending_exits[trade_id].trigger_time_utc)
            pending_hours = (datetime.now(timezone.utc) - then).total_seconds() / 3600.0

        ratios = self.momentum.calculate_ratios(closes)
        decision, reason = self.exit_policy.evaluate_exit(side, ratios, is_in_event_window, pending_hours)

        # NOTE on pending-exit lifecycle:
        #   EXIT_PENDING   → first occurrence creates entry; subsequent updates
        #                    refresh reason only when decision stays EXIT_PENDING.
        #   NOT EXIT_PENDING (HOLD) → signal disappeared → cancel pending immediately.
        #   EXIT_READY     → pending entry is KEPT ALIVE across the reconcile call.
        #                    We only delete it after the reconciler confirms the
        #                    trade is truly closed (SUCCESS_CLOSED / ALREADY_CLOSED).
        #                    If the close fails (REJECTED / PARTIAL / UNKNOWN) the
        #                    pending timer continues to accumulate across cycles,
        #                    so the 4h event-window timeout is not reset on failure.
        if decision == ExitDecision.EXIT_PENDING:
            if trade_id not in self.pending_exits:
                self.pending_exits[trade_id] = PendingExitState(
                    trade_id, symbol, side,
                    datetime.now(timezone.utc).isoformat(), 0.0, reason
                )
                self.state_store.save_pending_exits(self.pending_exits)
            else:
                # Refresh the human-readable reason so the state file reflects
                # the latest signal context; keep the original trigger time so
                # the pending clock does not reset on small signal updates.
                existing = self.pending_exits[trade_id]
                existing.reason = reason
                self.state_store.save_pending_exits(self.pending_exits)
        elif decision != ExitDecision.EXIT_READY and trade_id in self.pending_exits:
            # HOLD or INSUFFICIENT or the signal genuinely went away: cancel.
            del self.pending_exits[trade_id]
            self.state_store.save_pending_exits(self.pending_exits)

        if decision == ExitDecision.EXIT_READY:
            req_id = f"exit_{trade_id}_{uuid.uuid4().hex[:8]}"
            report = self.reconciler.reconcile_and_exit(trade_id, decision, reason, req_id)
            # Pending timer cleanup: only on confirmed closed states. All other
            # outcomes (REJECTED / PARTIAL_FILL / STATE_UNKNOWN) leave the
            # pending entry intact so the next cycle continues the 4h countdown
            # from where it left off.
            if report.status in (ReconcileStatus.SUCCESS_CLOSED, ReconcileStatus.ALREADY_CLOSED):
                if trade_id in self.pending_exits:
                    del self.pending_exits[trade_id]
                    self.state_store.save_pending_exits(self.pending_exits)
            else:
                print(
                    f"  [RISK] EXIT_READY close did not fully resolve "
                    f"(trade={trade_id}, status={report.status.name}). "
                    f"Retaining pending_exit; timer continues from "
                    f"{pending_hours:.2f}h."
                )
            return report

        return ReconciliationReport(ReconcileStatus.NO_ACTION_TAKEN, trade_id, -1.0, f"Hold: {reason}")

    def process_daily_bar(
        self,
        trade_id: str,
        symbol: str,
        side: str,
        current_broker_sl: Optional[float],
        current_bid: float,
        current_ask: float,
        raw_daily_candles: List[Dict[str, Any]]
    ) -> SLUpdateResult:
        candles = CompleteCandleFilter.filter_completed_daily(raw_daily_candles)

        if candles and not DailyBoundaryAligner.is_daily_close_aligned(candles[-1].get("time", "")):
            print(f"⚠️ [{symbol}] Daily candle time not aligned with OANDA 17:00 NY close — proceed with caution")

        tick_size, min_stop_dist = self.broker.get_instrument_spec(symbol)

        result, new_sl, msg = self.sl_protection.validate_and_compute(
            side, current_broker_sl, current_bid, current_ask,
            candles, tick_size, min_stop_dist
        )

        if result == SLUpdateResult.UPDATED and new_sl is not None:
            req_id = f"sl_{trade_id}_{uuid.uuid4().hex[:8]}"
            if self.broker.update_trade_sl(trade_id, new_sl, req_id):
                return SLUpdateResult.UPDATED
            # Broker rejection that isn't a validation-level distance problem:
            # surface as BROKER_ERROR so the audit log can distinguish from
            # "our candidate SL was inside the forbidden spread".
            print(
                f"  [RISK] BROKER_ERROR: update_trade_sl failed for {trade_id} "
                f"(candidate_sl={new_sl}). Treated as NO_CHANGE."
            )
            return SLUpdateResult.BROKER_ERROR

        return result
