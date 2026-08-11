"""
utils/cluster_state_store.py
=======================

JSON-backed persistence for `PyramidCluster` state across cron-triggered
process invocations. `scheduled_runner_v1.1.py` is invoked fresh by cron
every CHECK_INTERVAL_MINUTES with no long-running process in between, so
any in-memory `PyramidCluster`/`DynamicRiskManager` object would otherwise
vanish at the end of every cycle. This module is the disk-backed bridge.

Design notes
------------
- One JSON file holds ALL currently-managed clusters, keyed by OANDA
  instrument (e.g. "USD_JPY") — not by trade ID, since a cluster can span
  several trade IDs (base unit + pyramid adds) under one logical position.
- `schema_version` is written into every save and checked on every load,
  so a future format change can be migrated deliberately rather than
  silently misread.
- A file lock (via the `filelock` package) wraps every load-modify-save
  cycle, guarding against two overlapping cron invocations (e.g. one run
  still finishing retries when the next one fires) corrupting the file or
  racing on the same instrument.
- Missing file, empty file, or malformed JSON are all treated as "no
  managed positions yet" rather than raised as errors — this is a
  correctness-critical choice: a corrupted state file must never be
  allowed to crash the runner into leaving a live position unmanaged.
  Corruption IS logged loudly, and the bad file is preserved alongside a
  fresh one, so nothing is silently lost.

Usage
-----
    store = ClusterStateStore("state/open_clusters.json")

    with store.locked() as state:               # read-WRITE: auto-saves on clean exit
        cluster = restore_cluster(state["clusters"]["USD_JPY"])
        ... mutate cluster ...
        state["clusters"]["USD_JPY"] = cluster.to_dict()
        # no explicit save call needed — locked() saves automatically here

Or, for single-instrument operations, the convenience wrappers
`load_cluster_dict(instrument)` (read-only, does NOT rewrite the file) /
`save_cluster_dict(instrument, data)` / `delete_cluster(instrument)` each
take the lock for just that one operation.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from filelock import FileLock, Timeout

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

DEFAULT_LOCK_TIMEOUT_SECONDS = 30  # fail loudly rather than hang forever if something is stuck


class ClusterStateStoreError(Exception):
    """Raised for unrecoverable state-store problems (e.g. lock timeout)."""


def _empty_state() -> dict:
    return {"schema_version": SCHEMA_VERSION, "clusters": {}}


class ClusterStateStore:
    """
    JSON-backed store for open `PyramidCluster` state, keyed by instrument.
    Thread/process-safe for the cron-invoked, one-process-per-cycle model
    via a companion `.lock` file (using `filelock`, which uses OS-level
    file locking and works across separate processes, not just threads).
    """

    def __init__(self, path: str, lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS) -> None:
        """
        Args:
            path: Path to the JSON state file (e.g. "state/open_clusters.json").
                  The parent directory is created if it doesn't exist.
            lock_timeout_seconds: How long to wait for the lock before giving
                                   up. Deliberately finite — better to fail a
                                   cron cycle loudly (log + retry next time)
                                   than hang the process indefinitely.
        """
        self.path = os.path.abspath(path)
        self.lock_path = self.path + ".lock"
        self.lock_timeout_seconds = lock_timeout_seconds
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)

    # ------------------------------------------------------------------
    # Low-level load/save (NOT locked on their own — use `locked()` or the
    # convenience wrappers below, which wrap these correctly)
    # ------------------------------------------------------------------

    def _load_unlocked(self) -> dict:
        """
        Read and parse the state file. Missing file, empty file, or invalid
        JSON all safely return a fresh empty state rather than raising —
        see module docstring for the rationale. On any of those conditions,
        a corrupted/unreadable existing file is preserved as a `.corrupt.<ts>`
        sibling for forensics instead of being silently overwritten.
        """
        if not os.path.exists(self.path):
            return _empty_state()

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = f.read()
        except OSError as e:
            logger.error("cluster_state_store: could not read %s (%s) — treating as empty.", self.path, e)
            return _empty_state()

        if not raw.strip():
            logger.warning("cluster_state_store: %s is empty — treating as empty state.", self.path)
            return _empty_state()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            self._quarantine_corrupt_file(raw, reason=f"JSONDecodeError: {e}")
            return _empty_state()

        if not isinstance(data, dict) or "clusters" not in data:
            self._quarantine_corrupt_file(raw, reason="missing 'clusters' key or not a dict")
            return _empty_state()

        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            # Forward-compat placeholder: no migrations exist yet (schema_version
            # has never changed), so treat any mismatch as suspicious and quarantine
            # rather than guess at a migration path.
            self._quarantine_corrupt_file(raw, reason=f"unexpected schema_version={version!r}")
            return _empty_state()

        return data

    def _quarantine_corrupt_file(self, raw_content: str, reason: str) -> None:
        """Preserve an unreadable/corrupt state file for forensics instead of losing it silently."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        quarantine_path = f"{self.path}.corrupt.{ts}"
        try:
            with open(quarantine_path, "w", encoding="utf-8") as f:
                f.write(raw_content)
            logger.error(
                "cluster_state_store: %s is corrupt (%s). Original content preserved at %s. "
                "Treating as empty state — ALL managed positions will be re-discovered via "
                "OANDA reconciliation on the next cycle, not lost, but review the quarantined "
                "file before assuming that's sufficient.",
                self.path, reason, quarantine_path,
            )
        except OSError as e:
            logger.error(
                "cluster_state_store: %s is corrupt (%s), AND quarantine copy failed (%s). "
                "Original content follows in this log line: %r",
                self.path, reason, e, raw_content,
            )

    def _save_unlocked(self, state: dict) -> None:
        """
        Write state atomically: write to a temp file in the same directory,
        then os.replace() it over the target. This avoids ever leaving a
        half-written, truncated JSON file behind if the process is killed
        mid-write (e.g. cron timeout, OOM kill).
        """
        state = dict(state)
        state["schema_version"] = SCHEMA_VERSION

        dir_name = os.path.dirname(self.path) or "."
        fd, tmp_path = tempfile.mkstemp(prefix=".cluster_state_", dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

    # ------------------------------------------------------------------
    # Locked access
    # ------------------------------------------------------------------

    @contextmanager
    def locked(self) -> Iterator[dict]:
        """
        Context manager: acquires the cross-process file lock, loads the
        current state, yields it for the caller to read/mutate, and — if
        the `with` block exits without an exception — saves it back.

        Use this ONLY when you intend to mutate the state. For a pure read,
        use `read_locked()` instead — it takes the same lock but skips the
        write, avoiding an unnecessary atomic-write + fsync on every read
        (this matters once something calls a read path every cron cycle).

        On exception inside the `with` block, the state is NOT saved (so a
        crash mid-update doesn't persist a half-applied mutation), and the
        exception propagates after the lock is released.

        Raises:
            ClusterStateStoreError: if the lock can't be acquired within
                                      `lock_timeout_seconds` (another process
                                      is presumably still holding it).
        """
        lock = FileLock(self.lock_path, timeout=self.lock_timeout_seconds)
        try:
            with lock:
                state = self._load_unlocked()
                yield state
                self._save_unlocked(state)
        except Timeout as e:
            raise ClusterStateStoreError(
                f"Could not acquire lock {self.lock_path} within {self.lock_timeout_seconds}s "
                "— another process may be stuck holding it. Skipping this cycle's state update."
            ) from e

    @contextmanager
    def read_locked(self) -> Iterator[dict]:
        """
        Read-only counterpart to `locked()`: still takes the cross-process
        lock (so a read can't observe a half-written state from a concurrent
        writer), but never calls `_save_unlocked()` — no temp file, no
        fsync, no rename, and the file's mtime is untouched by a pure read.

        Mutating `state` inside this block has no effect beyond the `with`
        block — nothing is persisted. Use `locked()` instead if you intend
        to write.

        Raises:
            ClusterStateStoreError: same as `locked()`.
        """
        lock = FileLock(self.lock_path, timeout=self.lock_timeout_seconds)
        try:
            with lock:
                state = self._load_unlocked()
                yield state
        except Timeout as e:
            raise ClusterStateStoreError(
                f"Could not acquire lock {self.lock_path} within {self.lock_timeout_seconds}s "
                "— another process may be stuck holding it. Skipping this cycle's read."
            ) from e

    # ------------------------------------------------------------------
    # Convenience single-instrument wrappers
    # ------------------------------------------------------------------

    def load_cluster_dict(self, instrument: str) -> Optional[dict]:
        """Return the raw cluster dict for `instrument`, or None if not currently managed.
        Read-only — does not rewrite the state file."""
        with self.read_locked() as state:
            return state["clusters"].get(instrument)

    def save_cluster_dict(self, instrument: str, cluster_data: dict) -> None:
        """Upsert the cluster dict for `instrument`."""
        with self.locked() as state:
            state["clusters"][instrument] = cluster_data

    def delete_cluster(self, instrument: str) -> bool:
        """
        Remove `instrument` from the store (e.g. after a FULL_CLOSE or once
        reconciliation finds it's no longer open at the broker).

        Returns:
            True if an entry existed and was removed, False if it wasn't
            present (not an error — deleting something already gone is a no-op).
        """
        with self.locked() as state:
            existed = instrument in state["clusters"]
            state["clusters"].pop(instrument, None)
            return existed

    def list_managed_instruments(self) -> list:
        """Return the list of instruments currently tracked in the store.
        Read-only — does not rewrite the state file."""
        with self.read_locked() as state:
            return list(state["clusters"].keys())