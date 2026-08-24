# utils/thesis_state_store.py
"""
code 2 from chatGPT
utils/thesis_state_store.py
============================

JSON-backed persistence for Phase 2 thesis-observation state (snapshot +
confirmation counter), across cron-triggered process invocations —
mirroring the proven conventions of utils/cluster_state_store.py, but
COMPLETELY INDEPENDENT of it. There is no shared write path, no shared
lock file, and no import of ClusterStateStore here. This is deliberate:
a bug in the passive Phase 2 observation layer must never be able to
touch cluster/risk-management persistence.

Design notes (mirroring cluster_state_store.py's documented philosophy)
------------------------------------------------------------------------
- One JSON file holds thesis-observation state for ALL currently-observed
  instruments, keyed by OANDA instrument string (e.g. "GBP_JPY").
- `schema_version` is written into every save and checked on every load.
- A file lock (via `filelock`, same package already used by
  ClusterStateStore) guards every load-modify-save cycle against
  overlapping cron invocations. This is a SEPARATE lock file from
  ClusterStateStore's — no shared resource contention path exists
  between the two.
- Missing file or malformed JSON are both treated as "no thesis state
  yet" rather than raised as errors. Corruption is logged loudly and
  the bad file is preserved (renamed, not deleted) alongside a fresh
  empty state — nothing is silently destroyed.
- A lock-acquisition timeout is caught and treated as "skip thesis
  observation this cycle" — it must never propagate into the calling
  risk-management loop.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from typing import Optional

from filelock import FileLock, Timeout

SCHEMA_VERSION = 1

DEFAULT_STATE_PATH = os.environ.get(
    "THESIS_STATE_PATH", "state/thesis_observation_state.json"
)
DEFAULT_LOCK_TIMEOUT_SECONDS = 30


class ThesisStateStoreError(Exception):
    """Raised only for lock-acquisition timeouts — callers MUST catch this
    and degrade to 'skip observation this cycle', never let it propagate
    into manage_open_positions()."""


def _empty_state() -> dict:
    return {"schema_version": SCHEMA_VERSION, "instruments": {}}


def _lock_path(state_path: str) -> str:
    return state_path + ".lock"


def _quarantine_corrupt_file(state_path: str) -> None:
    if not os.path.exists(state_path):
        return
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    quarantined = f"{state_path}.corrupt.{ts}"
    try:
        shutil.copy2(state_path, quarantined)
        print(f"[THESIS-STATE] Corrupt state file preserved at {quarantined}")
    except Exception as e:
        print(f"[THESIS-STATE] Failed to quarantine corrupt state file: {e}")


def load_thesis_state(state_path: str = DEFAULT_STATE_PATH) -> dict:
    """
    Load thesis-observation state from disk. NEVER RAISES for a missing or
    malformed file — both degrade to an empty state. A malformed file is
    preserved (not deleted) before being treated as empty.
    """
    if not os.path.exists(state_path):
        return _empty_state()
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "instruments" not in data:
            raise ValueError("thesis state file missing required 'instruments' key")
        if data.get("schema_version") != SCHEMA_VERSION:
            print(f"[THESIS-STATE] Unexpected schema_version in {state_path}; "
                  f"starting fresh rather than guessing at migration.")
            return _empty_state()
        return data
    except Exception as e:
        print(f"[THESIS-STATE] Malformed state file at {state_path}: {e}")
        _quarantine_corrupt_file(state_path)
        return _empty_state()


def save_thesis_state(state: dict, state_path: str = DEFAULT_STATE_PATH) -> None:
    """
    Atomic write: write to a temp file in the same directory, then
    os.replace() — same mechanism already proven in cluster_state_store.py.
    NEVER RAISES to the caller; logs and returns on failure (a failed save
    just means next cycle re-derives from whatever was last saved, exactly
    the same tolerance ClusterStateStore already has for its own writes).
    """
    try:
        dirname = os.path.dirname(state_path) or "."
        os.makedirs(dirname, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=dirname,
            prefix=".thesis_state_",
            suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, default=str, indent=2, sort_keys=True)
            os.replace(tmp_path, state_path)
        except Exception:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            raise
    except Exception as e:
        print(f"[THESIS-STATE] Failed to save state to {state_path}: {e}")


class thesis_state_session:
    """
    Context manager: acquires the thesis-state lock, loads state, yields it
    for in-place mutation, saves on clean exit. A lock-acquisition timeout
    raises ThesisStateStoreError, which callers MUST catch and treat as
    "skip thesis observation this cycle" (never let it propagate).

    Usage:
        try:
            with thesis_state_session() as state:
                ... mutate state["instruments"][...] ...
        except ThesisStateStoreError:
            print("[THESIS-STATE] lock timeout — skipping observation this cycle")
    """

    def __init__(
        self,
        state_path: str = DEFAULT_STATE_PATH,
        lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS
    ):
        self.state_path = state_path
        self.lock = FileLock(
            _lock_path(state_path),
            timeout=lock_timeout_seconds
        )
        self.state: Optional[dict] = None

    def __enter__(self) -> dict:
        try:
            self.lock.acquire()
        except Timeout as e:
            raise ThesisStateStoreError(
                f"Timed out acquiring thesis-state lock at "
                f"{_lock_path(self.state_path)}"
            ) from e
        self.state = load_thesis_state(self.state_path)
        return self.state

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None and self.state is not None:
                save_thesis_state(self.state, self.state_path)
        finally:
            self.lock.release()
        return False


def cleanup_orphaned_instruments(state: dict, managed_instruments) -> None:
    """
    Remove any instrument entry from `state["instruments"]` that is not in
    the current cycle's managed-instrument set. Mutates `state` in place.
    This is the defensive orphan sweep — applied on every load/cycle,
    independent of whether a close was explicitly detected elsewhere.
    """
    managed_set = set(managed_instruments)
    stale = [
        inst
        for inst in state.get("instruments", {})
        if inst not in managed_set
    ]
    for inst in stale:
        del state["instruments"][inst]