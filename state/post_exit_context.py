"""
PostExitContext — READ-ONLY tracker of past trade outcomes per instrument.

STRICT GUARDRAIL:
- Only reads v2_trade_outcomes.jsonl (never writes it).
- Never touches open_clusters.json or any live position/cluster state.
- All state is computed in-memory from the outcome log on each call.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import config as _config

# Default path mirrors signal_instrumentation.py convention
TRADE_OUTCOME_LOG_PATH = os.environ.get(
    "V2_TRADE_OUTCOME_LOG_PATH", "logs/v2_trade_outcomes.jsonl"
)
CLUSTER_STATE_PATH = Path(
    os.environ.get("CLUSTER_STATE_PATH", "state/open_clusters.json")
)
POST_EXIT_STRICT_WINDOW_HOURS = float(
    getattr(_config, "POST_EXIT_STRICT_WINDOW_HOURS", 24.0)
)
_TIER_BY_REASON = {
    "TP": "tier1",
    "PROFIT_TAKE": "tier1",
    "HWM_LOCK": "tier1",
    "TECHNICAL_INVALIDATION": "tier2",
    "STRATEGY_INVALIDATION": "tier3",
    "SWEEP": "tier3",
    "STOP_LOSS": "tier3",
}
_TIER_RANK = {"unknown": 0, "tier1": 1, "tier2": 2, "tier3": 3}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PostExitTracker:
    """
    Lightweight, read-only tracker that scans the trade-outcome log to
    produce per-instrument post-exit context:
      - closed_at          : ISO timestamp of the most recent exit
      - close_reason       : reason string of the most recent exit
      - consecutive_failures : count of consecutive losing exits
    """

    def __init__(self, log_path: str = TRADE_OUTCOME_LOG_PATH):
        self.log_path = Path(log_path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _is_losing_outcome(record: Dict[str, Any]) -> bool:
        """A trade is considered a 'failure' for streak counting if realized_r < 0."""
        realized_r = record.get("realized_r")
        return realized_r is not None and realized_r < 0

    def _read_outcomes(self) -> List[Dict[str, Any]]:
        """Read all trade_outcome lines from the JSONL file."""
        if not self.log_path.exists():
            return []
        records: List[Dict[str, Any]] = []
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        if rec.get("log_type") == "trade_outcome":
                            records.append(rec)
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            print(f"[POST_EXIT_CONTEXT] Failed to read outcome log: {e}")
        return records

    def _read_cluster_state(self) -> Dict[str, Any]:
        try:
            with open(CLUSTER_STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
            return state if isinstance(state, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def record_exit(
        self,
        instrument: str,
        reason: str,
        closed_at: Optional[str] = None,
    ) -> None:
        """Persist post-exit state beside cluster records with an atomic replace."""
        state = self._read_cluster_state()
        state.setdefault("clusters", {})
        state.setdefault("post_exit_states", {})
        reason_key = reason.upper().split(":", 1)[0].strip()
        incoming_tier = _TIER_BY_REASON.get(reason_key, "unknown")
        previous = state["post_exit_states"].get(instrument)
        previous_tier = previous.get("tier", "unknown") if previous else "unknown"
        if previous and _TIER_RANK.get(previous_tier, 0) > _TIER_RANK[incoming_tier]:
            exit_state = dict(previous)
            exit_state["is_active"] = True
        else:
            exit_state = {
                "instrument": instrument,
                "closed_at": closed_at or _utcnow_iso(),
                "close_reason": reason,
                "tier": incoming_tier,
                "is_active": True,
            }
        state["post_exit_states"][instrument] = exit_state
        CLUSTER_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix="open_clusters.", dir=CLUSTER_STATE_PATH.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, CLUSTER_STATE_PATH)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_context(self, instrument: str) -> Dict[str, Any]:
        """
        Return post-exit context for a single instrument.

        Returns
        -------
        dict with keys:
            instrument              : str
            closed_at               : Optional[str]  ISO timestamp
            close_reason            : Optional[str]
            consecutive_failures    : int
            elapsed_hours           : float  (0.0 if no prior exit)
        """
        state = self._read_cluster_state()
        persisted = state.get("post_exit_states", {}).get(instrument)
        records = self._read_outcomes()

        # Filter to this instrument, newest first
        inst_records = [
            r for r in records if r.get("instrument") == instrument
        ]
        inst_records.sort(
            key=lambda r: r.get("timestamp_utc", ""), reverse=True
        )

        if not inst_records and persisted:
            closed_at = persisted.get("closed_at")
            elapsed_hours = 0.0
            try:
                elapsed_hours = max(
                    0.0,
                    (
                        datetime.now(timezone.utc)
                        - datetime.fromisoformat(closed_at)
                    ).total_seconds()
                    / 3600.0,
                )
            except (TypeError, ValueError):
                pass
            return {
                "instrument": instrument,
                "closed_at": closed_at,
                "close_reason": persisted.get("close_reason"),
                "tier": persisted.get("tier", "unknown"),
                "is_active": elapsed_hours <= POST_EXIT_STRICT_WINDOW_HOURS,
                "consecutive_failures": 0,
                "elapsed_hours": round(elapsed_hours, 4),
            }

        if not inst_records:
            return {
                "instrument": instrument,
                "closed_at": None,
                "close_reason": None,
                "consecutive_failures": 0,
                "elapsed_hours": 0.0,
            }

        latest = inst_records[0]
        closed_at = latest.get("timestamp_utc")
        close_reason = latest.get("close_reason")

        # Count consecutive failures walking backwards from latest
        consecutive_failures = 0
        for r in inst_records:
            if self._is_losing_outcome(r):
                consecutive_failures += 1
            else:
                break

        # Elapsed hours since closed_at
        elapsed_hours = 0.0
        if closed_at:
            try:
                closed_dt = datetime.fromisoformat(closed_at)
                now_dt = datetime.now(timezone.utc)
                elapsed = (now_dt - closed_dt).total_seconds() / 3600.0
                elapsed_hours = max(0.0, elapsed)
            except Exception:
                elapsed_hours = 0.0

        return {
            "instrument": instrument,
            "closed_at": closed_at,
            "close_reason": close_reason,
            "is_active": elapsed_hours <= POST_EXIT_STRICT_WINDOW_HOURS,
            "consecutive_failures": consecutive_failures,
            "elapsed_hours": round(elapsed_hours, 4),
        }