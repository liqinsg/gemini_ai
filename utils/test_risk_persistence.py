"""
utils/test_risk_persistence.py
=========================

Phase 1 tests for the risk-management persistence layer:

1. DynamicRiskManager: serialize -> deserialize preserves all risk state.
2. PyramidCluster with multiple units survives serialization.
3. Trade IDs survive serialization.
4. ClusterStateStore handles a missing/corrupt state file safely.
5. Concurrent access is protected by the file lock.

Run from the PROJECT ROOT (same convention as test_risk_integration.py):
    python3 -m pytest utils/test_risk_persistence.py -v
"""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime

# Bootstrap sys.path to the project root the same way pyramid_cluster.py and
# test_risk_integration.py do, so this file works regardless of the current
# working directory it's invoked from (project root vs. inside utils/).
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from utils.dynamic_risk_manager import ActionType, DynamicRiskManager, RiskConfig, RiskStateEnum
from utils.pyramid_cluster import CloseAllocationMethod, PositionUnit, PyramidCluster
from utils.cluster_state_store import ClusterStateStore, ClusterStateStoreError, SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 1. DynamicRiskManager round-trip
# ---------------------------------------------------------------------------

class TestDynamicRiskManagerRoundTrip(unittest.TestCase):
    def _build_and_advance(self) -> DynamicRiskManager:
        """Build a manager and push it through BE + Chandelier engagement,
        so the serialized state isn't just the freshly-constructed default."""
        cfg = RiskConfig(atr_multiplier_init=2.0, be_trigger_r=1.0, t_expected_hours=48)
        rm = DynamicRiskManager(
            entry_price=98.50,
            direction=1,
            atr_entry=0.35,
            entry_time=datetime(2026, 8, 1, 0, 0),
            config=cfg,
            structural_sl_level=97.80,
        )
        rm.update(price=99.30, atr_now=0.34, highest_high=99.30, lowest_low=98.40, hours_elapsed=8)
        rm.update(price=99.60, atr_now=0.32, highest_high=99.60, lowest_low=98.40, hours_elapsed=20)
        return rm

    def test_round_trip_preserves_all_fields(self):
        rm = self._build_and_advance()
        data = rm.to_dict()
        restored = DynamicRiskManager.from_dict(data)

        self.assertEqual(restored.entry_price_0, rm.entry_price_0)
        self.assertEqual(restored.r_unit_0, rm.r_unit_0)
        self.assertEqual(restored.current_sl, rm.current_sl)
        self.assertEqual(restored.direction, rm.direction)
        self.assertEqual(restored.atr_entry, rm.atr_entry)
        self.assertEqual(restored.entry_time, rm.entry_time)
        self.assertEqual(restored.state, rm.state)
        self.assertEqual(restored.chandelier_k, rm.chandelier_k)
        self.assertEqual(restored.time_reduce_fired, rm.time_reduce_fired)
        # Config values must also round-trip, since thresholds are snapshotted at
        # construction and must not silently change on restore if config.py changes later.
        self.assertEqual(restored.cfg.atr_multiplier_init, rm.cfg.atr_multiplier_init)
        self.assertEqual(restored.cfg.t_expected_hours, rm.cfg.t_expected_hours)
        self.assertEqual(restored.cfg.chandelier_k_default, rm.cfg.chandelier_k_default)

        self.assertEqual(restored.state, RiskStateEnum.TRAILING_CHANDELIER)

    def test_round_trip_is_json_safe(self):
        """to_dict() output must survive an actual json.dumps/loads cycle, not just be a dict."""
        rm = self._build_and_advance()
        data = rm.to_dict()
        json_str = json.dumps(data)
        reparsed = json.loads(json_str)
        restored = DynamicRiskManager.from_dict(reparsed)
        self.assertEqual(restored.current_sl, rm.current_sl)
        self.assertEqual(restored.state, rm.state)

    def test_restored_manager_continues_behaving_identically(self):
        """A restored manager fed the same subsequent bar must produce the same
        RiskAction as the original would — proves restore isn't just cosmetic."""
        rm = self._build_and_advance()
        restored = DynamicRiskManager.from_dict(rm.to_dict())

        action_original = rm.update(price=99.55, atr_now=0.30, highest_high=99.60, lowest_low=98.40, hours_elapsed=30)
        action_restored = restored.update(price=99.55, atr_now=0.30, highest_high=99.60, lowest_low=98.40, hours_elapsed=30)

        self.assertEqual(action_original.action, action_restored.action)
        self.assertEqual(action_original.new_sl, action_restored.new_sl)
        self.assertEqual(action_original.state, action_restored.state)

    def test_restore_does_not_reset_to_init_or_recompute_sl_from_atr(self):
        """Regression guard: from_dict must NOT re-derive current_sl the way
        __init__ would (from atr_entry/structural_sl_level) — it must use the
        exact trailed value that was saved, even though atr_entry is also stored."""
        rm = self._build_and_advance()
        naive_init_sl = rm.entry_price_0 - rm.direction * rm.cfg.atr_multiplier_init * rm.atr_entry
        self.assertNotEqual(rm.current_sl, naive_init_sl, "test setup should have trailed the SL past its initial value")

        restored = DynamicRiskManager.from_dict(rm.to_dict())
        self.assertEqual(restored.current_sl, rm.current_sl)
        self.assertNotEqual(restored.current_sl, naive_init_sl)


# ---------------------------------------------------------------------------
# 2 & 3. PyramidCluster multi-unit round-trip, including trade_id
# ---------------------------------------------------------------------------

class TestPyramidClusterRoundTrip(unittest.TestCase):
    def _build_pyramided_cluster(self) -> PyramidCluster:
        cfg = RiskConfig(atr_multiplier_init=2.0, be_trigger_r=1.0, t_expected_hours=48)
        cluster = PyramidCluster(
            initial_size=10_000,
            entry_price=98.50,
            direction=1,
            atr_entry=0.35,
            entry_time=datetime(2026, 8, 1, 0, 0),
            config=cfg,
            structural_sl_level=97.80,
            max_size_decay_ratio=0.7,
            initial_trade_id="TRADE-BASE-001",
        )
        # push to BE so pyramiding is allowed
        cluster.update(price=99.30, atr_now=0.34, highest_high=99.30, lowest_low=98.40, hours_elapsed=8)
        max_allowed_risk = 10_000_000  # generous cap, not the focus of this test
        check1 = cluster.add_unit(
            new_size=5_000, new_entry_price=99.30,
            entry_time=datetime(2026, 8, 1, 8, 0),
            max_allowed_risk=max_allowed_risk, trade_id="TRADE-ADD-002",
        )
        assert check1.status.value == "OK", check1.reason
        cluster.update(price=99.80, atr_now=0.32, highest_high=99.80, lowest_low=98.40, hours_elapsed=18)
        check2 = cluster.add_unit(
            new_size=2_500, new_entry_price=99.80,
            entry_time=datetime(2026, 8, 1, 18, 0),
            max_allowed_risk=max_allowed_risk, trade_id="TRADE-ADD-003",
        )
        assert check2.status.value == "OK", check2.reason
        return cluster

    def test_multi_unit_cluster_round_trip(self):
        cluster = self._build_pyramided_cluster()
        data = cluster.to_dict()
        restored = PyramidCluster.from_dict(data)

        self.assertEqual(len(restored.units), 3)
        self.assertEqual(restored.total_size, cluster.total_size)
        self.assertEqual(restored.blended_entry, cluster.blended_entry)
        self.assertEqual(restored.max_size_decay_ratio, cluster.max_size_decay_ratio)
        self.assertEqual(restored.risk_manager.current_sl, cluster.risk_manager.current_sl)
        self.assertEqual(restored.risk_manager.state, cluster.risk_manager.state)
        # R-anchor must still be the ORIGINAL base entry, not blended
        self.assertEqual(restored.risk_manager.entry_price_0, 98.50)

    def test_trade_ids_survive_serialization(self):
        cluster = self._build_pyramided_cluster()
        original_ids = [u.trade_id for u in cluster.units]
        self.assertEqual(original_ids, ["TRADE-BASE-001", "TRADE-ADD-002", "TRADE-ADD-003"])

        restored = PyramidCluster.from_dict(cluster.to_dict())
        restored_ids = [u.trade_id for u in restored.units]
        self.assertEqual(restored_ids, original_ids)

    def test_trade_ids_survive_json_round_trip(self):
        """Not just dict-in/dict-out — through an actual json.dumps/loads."""
        cluster = self._build_pyramided_cluster()
        json_str = json.dumps(cluster.to_dict())
        reparsed = json.loads(json_str)
        restored = PyramidCluster.from_dict(reparsed)
        self.assertEqual([u.trade_id for u in restored.units], ["TRADE-BASE-001", "TRADE-ADD-002", "TRADE-ADD-003"])

    def test_close_allocation_after_restore_uses_correct_trade_ids(self):
        """The whole point of persisting trade_id: a FIFO close computed on a
        RESTORED cluster must still map back to the right OANDA trade tickets."""
        cluster = self._build_pyramided_cluster()
        restored = PyramidCluster.from_dict(cluster.to_dict())

        instructions = restored.close_allocation(0.5, method=CloseAllocationMethod.FIFO)
        self.assertEqual(instructions[0].trade_id, "TRADE-BASE-001")
        self.assertGreater(instructions[0].close_size, 0)  # FIFO drains the oldest unit first
        self.assertEqual(instructions[1].trade_id, "TRADE-ADD-002")
        self.assertEqual(instructions[2].trade_id, "TRADE-ADD-003")

    def test_apply_close_rejects_trade_id_mismatch(self):
        """Regression guard for the review's flagged gap: a same-length instruction
        list that's been reordered/misaligned relative to self.units must be
        rejected loudly, not silently misapplied to the wrong trade ticket."""
        cluster = self._build_pyramided_cluster()
        instructions = cluster.close_allocation(0.5, method=CloseAllocationMethod.FIFO)

        # Swap two instructions' trade_ids to simulate a misaligned/reordered list
        # (same length, same total, but no longer positionally matching self.units).
        tampered = list(instructions)
        tampered[0], tampered[1] = tampered[1], tampered[0]

        with self.assertRaises(ValueError) as ctx:
            cluster.apply_close(tampered)
        self.assertIn("trade_id", str(ctx.exception))
        # Cluster state must be untouched after the rejected call.
        self.assertEqual(len(cluster.units), 3)
        self.assertEqual([u.trade_id for u in cluster.units], ["TRADE-BASE-001", "TRADE-ADD-002", "TRADE-ADD-003"])

    def test_restored_cluster_continues_behaving_identically(self):
        cluster = self._build_pyramided_cluster()
        restored = PyramidCluster.from_dict(cluster.to_dict())

        action_original = cluster.update(price=99.00, atr_now=0.30, highest_high=99.80, lowest_low=98.40, hours_elapsed=60)
        action_restored = restored.update(price=99.00, atr_now=0.30, highest_high=99.80, lowest_low=98.40, hours_elapsed=60)

        self.assertEqual(action_original.action, action_restored.action)
        self.assertEqual(action_original.close_ratio, action_restored.close_ratio)

    def test_risk_calculator_not_persisted_but_restorable(self):
        """risk_calculator is a live callable and must NOT be expected in the
        serialized dict; from_dict must accept a fresh one without erroring,
        and default sensibly if none is supplied."""
        cluster = self._build_pyramided_cluster()
        data = cluster.to_dict()
        self.assertNotIn("risk_calculator", data)

        custom_calc = lambda size, distance: size * distance * 2  # arbitrary distinct calculator
        restored = PyramidCluster.from_dict(data, risk_calculator=custom_calc)
        self.assertIs(restored.risk_calculator, custom_calc)

        restored_default = PyramidCluster.from_dict(data)
        self.assertIsNotNone(restored_default.risk_calculator)


# ---------------------------------------------------------------------------
# 4. ClusterStateStore: missing / corrupt file handling
# ---------------------------------------------------------------------------

class TestClusterStateStoreCorruption(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_path = os.path.join(self.tmpdir, "open_clusters.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_missing_file_returns_empty_state_without_error(self):
        store = ClusterStateStore(self.state_path)
        self.assertFalse(os.path.exists(self.state_path))
        with store.locked() as state:
            self.assertEqual(state["clusters"], {})
            self.assertEqual(state["schema_version"], SCHEMA_VERSION)

    def test_load_on_missing_file_does_not_create_file_until_save(self):
        store = ClusterStateStore(self.state_path)
        # locked() always saves on clean exit, so the file WILL exist after — verify content instead
        with store.locked() as state:
            pass
        self.assertTrue(os.path.exists(self.state_path))
        with open(self.state_path) as f:
            data = json.load(f)
        self.assertEqual(data["clusters"], {})

    def test_empty_file_treated_as_empty_state(self):
        with open(self.state_path, "w") as f:
            f.write("")
        store = ClusterStateStore(self.state_path)
        with store.locked() as state:
            self.assertEqual(state["clusters"], {})

    def test_malformed_json_is_quarantined_and_treated_as_empty(self):
        with open(self.state_path, "w") as f:
            f.write("{not valid json!!!")
        store = ClusterStateStore(self.state_path)

        with store.locked() as state:
            self.assertEqual(state["clusters"], {})

        quarantine_files = [f for f in os.listdir(self.tmpdir) if ".corrupt." in f]
        self.assertEqual(len(quarantine_files), 1)
        with open(os.path.join(self.tmpdir, quarantine_files[0])) as f:
            self.assertEqual(f.read(), "{not valid json!!!")

    def test_wrong_schema_version_is_quarantined(self):
        with open(self.state_path, "w") as f:
            json.dump({"schema_version": 999, "clusters": {"USD_JPY": {}}}, f)
        store = ClusterStateStore(self.state_path)

        with store.locked() as state:
            self.assertEqual(state["clusters"], {})

        quarantine_files = [f for f in os.listdir(self.tmpdir) if ".corrupt." in f]
        self.assertEqual(len(quarantine_files), 1)

    def test_missing_clusters_key_is_quarantined(self):
        with open(self.state_path, "w") as f:
            json.dump({"schema_version": SCHEMA_VERSION, "unrelated": True}, f)
        store = ClusterStateStore(self.state_path)
        with store.locked() as state:
            self.assertEqual(state["clusters"], {})

    def test_save_and_reload_round_trip_via_store(self):
        cfg = RiskConfig()
        rm = DynamicRiskManager(
            entry_price=149.5, direction=1, atr_entry=0.2,
            entry_time=datetime(2026, 8, 10, 3, 15), config=cfg, structural_sl_level=149.0,
        )
        cluster = PyramidCluster.__new__(PyramidCluster)
        cluster.max_size_decay_ratio = 0.7
        cluster.risk_calculator = lambda s, d: s * d
        cluster.units = [PositionUnit(size=10000, entry_price=149.5, entry_time=datetime(2026, 8, 10, 3, 15), trade_id="T1")]
        cluster.risk_manager = rm

        store = ClusterStateStore(self.state_path)
        store.save_cluster_dict("USD_JPY", cluster.to_dict())

        loaded = store.load_cluster_dict("USD_JPY")
        self.assertIsNotNone(loaded)
        restored = PyramidCluster.from_dict(loaded)
        self.assertEqual(restored.units[0].trade_id, "T1")
        self.assertEqual(restored.risk_manager.entry_price_0, 149.5)

        self.assertIsNone(store.load_cluster_dict("EUR_JPY"))  # untouched instrument

    def test_delete_cluster(self):
        store = ClusterStateStore(self.state_path)
        store.save_cluster_dict("USD_JPY", {"dummy": True})
        self.assertTrue(store.delete_cluster("USD_JPY"))
        self.assertIsNone(store.load_cluster_dict("USD_JPY"))
        self.assertFalse(store.delete_cluster("USD_JPY"))  # already gone -> False, not an error

    def test_atomic_save_leaves_no_tmp_file_behind(self):
        store = ClusterStateStore(self.state_path)
        store.save_cluster_dict("USD_JPY", {"dummy": True})
        leftover_tmp = [f for f in os.listdir(self.tmpdir) if f.startswith(".cluster_state_")]
        self.assertEqual(leftover_tmp, [])

    def test_read_operations_do_not_rewrite_the_state_file(self):
        """Regression guard: load_cluster_dict()/list_managed_instruments() must
        use the read-only lock path and NOT trigger an atomic write — a read
        should never change the file's mtime or trigger unnecessary disk I/O,
        since Phase 2 will call these every 15-minute cron cycle."""
        store = ClusterStateStore(self.state_path)
        store.save_cluster_dict("USD_JPY", {"dummy": True})  # establishes the file

        mtime_before = os.path.getmtime(self.state_path)
        time.sleep(0.05)  # ensure a rewrite (if it happened) would be detectable via mtime

        _ = store.load_cluster_dict("USD_JPY")
        _ = store.load_cluster_dict("EUR_JPY")  # miss case too
        _ = store.list_managed_instruments()

        mtime_after = os.path.getmtime(self.state_path)
        self.assertEqual(mtime_before, mtime_after, "A read-only call rewrote the state file")

    def test_read_locked_yields_state_without_persisting_mutations(self):
        """Mutating the dict yielded by read_locked() must have no effect —
        it's explicitly documented as not persisting."""
        store = ClusterStateStore(self.state_path)
        store.save_cluster_dict("USD_JPY", {"dummy": True})

        with store.read_locked() as state:
            state["clusters"]["SHOULD_NOT_PERSIST"] = {"x": 1}

        with store.read_locked() as state:
            self.assertNotIn("SHOULD_NOT_PERSIST", state["clusters"])


# ---------------------------------------------------------------------------
# 5. Concurrent access protected by the file lock
# ---------------------------------------------------------------------------

class TestConcurrentAccess(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_path = os.path.join(self.tmpdir, "open_clusters.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_concurrent_writers_do_not_corrupt_or_lose_updates(self):
        """
        Spin up several threads, each opening its OWN ClusterStateStore instance
        pointed at the same file (simulating separate cron-invoked processes,
        which is the real scenario — not separate threads of one process, but
        the file lock behaves the same way for both), each writing a distinct
        instrument key. If the lock works, all N entries survive; if it
        didn't, some writes would be lost to a lost-update race or the file
        would end up corrupt.
        """
        n_writers = 12
        barrier = threading.Barrier(n_writers)
        errors = []

        def writer(i: int):
            try:
                barrier.wait()  # maximize actual overlap
                store = ClusterStateStore(self.state_path, lock_timeout_seconds=10)
                store.save_cluster_dict(f"PAIR_{i}", {"seq": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(n_writers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        self.assertEqual(errors, [], f"Unexpected errors during concurrent writes: {errors}")

        final_store = ClusterStateStore(self.state_path)
        managed = set(final_store.list_managed_instruments())
        expected = {f"PAIR_{i}" for i in range(n_writers)}
        self.assertEqual(managed, expected, "Some concurrent writes were lost — lock did not serialize access correctly")

        # File must still be valid, parseable JSON (no interleaved/truncated writes)
        with open(self.state_path) as f:
            data = json.load(f)
        self.assertEqual(len(data["clusters"]), n_writers)

    def test_lock_timeout_raises_clear_error_when_lock_held(self):
        """If the lock is already held (simulated by holding it directly),
        a second store with a short timeout must fail fast and clearly,
        not hang or silently proceed unsafely."""
        from filelock import FileLock

        store = ClusterStateStore(self.state_path, lock_timeout_seconds=1)
        blocking_lock = FileLock(store.lock_path)
        blocking_lock.acquire()
        try:
            with self.assertRaises(ClusterStateStoreError):
                with store.locked() as state:
                    pass  # should never get here — lock is held by blocking_lock
        finally:
            blocking_lock.release()

    def test_read_modify_write_is_atomic_across_threads(self):
        """
        A stronger version of the corruption test: each writer LOADS the
        current state, adds its own key to whatever's already there, and
        saves — this is the real load-modify-save pattern run_cycle() will
        use. If locking is broken, concurrent read-modify-write cycles would
        lose each other's additions (classic lost-update race).
        """
        n_writers = 8
        barrier = threading.Barrier(n_writers)
        errors = []

        def read_modify_write(i: int):
            try:
                barrier.wait()
                store = ClusterStateStore(self.state_path, lock_timeout_seconds=10)
                with store.locked() as state:
                    state["clusters"][f"RMW_{i}"] = {"seq": i}
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=read_modify_write, args=(i,)) for i in range(n_writers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        self.assertEqual(errors, [])
        final_store = ClusterStateStore(self.state_path)
        managed = set(final_store.list_managed_instruments())
        self.assertEqual(managed, {f"RMW_{i}" for i in range(n_writers)})


if __name__ == "__main__":
    unittest.main(verbosity=2)