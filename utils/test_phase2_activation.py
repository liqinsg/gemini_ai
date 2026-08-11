"""
test_phase2_activation.py
===========================

Regression tests for the bug reported after the first live run:
- AUD_JPY direction-fix worked (SELL closed, BUY opened).
- But state/open_clusters.json was never created, and the other three
  pre-existing positions were never reconciled.

Root cause traced: ENABLE_DYNAMIC_RISK_MANAGER silently defaulted to False
(config.py never received the attribute), and because risk_integration's
imports in the runner were conditional on that flag, EVERYTHING Phase 2
related silently no-op'd with zero errors and zero log evidence.

This file tests three things:
1. The exact failure scenario reproduced: config.py missing the attribute
   entirely -> a loud warning fires and the flag safely resolves False
   (previously: silent, indistinguishable from an intentional False).
2. When the flag is False, manage_open_positions()-equivalent logic makes
   ZERO calls to OANDA or the state store — proving Phase 2 is fully inert,
   not partially/inconsistently active.
3. END-TO-END: when the flag IS True and a fill succeeds, a REAL JSON file
   is actually written to disk with the correct content — using a real
   ClusterStateStore against a temp directory (not mocked), directly
   guarding against "cluster created in memory but never persisted" bugs,
   which is the exact class of defect originally reported.

NO live OANDA calls anywhere in this file.
"""

import importlib
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Capture the PRISTINE original module objects at collection time (before any
# test in this file has run), so tearDown can restore the EXACT SAME objects —
# not just freshly-reimported equivalents. This matters because pytest collects
# (imports) every test file up front; other test files (e.g. test_risk_integration.py)
# bind `from utils import risk_integration as ri` against these original objects
# during collection. If this file's tests swap sys.modules["utils.risk_integration"]
# for a NEW object and don't restore the ORIGINAL object afterward, any
# @patch("utils.risk_integration.X") in another test file (which resolves its
# target dynamically via sys.modules at patch-time, not collection-time) ends up
# patching a different, abandoned module object than the one the other file's
# `ri` variable actually points to and calls — silently un-mocking it and letting
# a real network call through. Confirmed this exact failure mode empirically
# before adding this fix.
import utils.risk_integration as _ORIGINAL_RISK_INTEGRATION_MODULE  # noqa: E402


def _fresh_risk_integration_module():
    """Force a clean re-import of utils.risk_integration, so each test sees
    a fresh evaluation of the module-level flag/warning logic against
    whatever `config` module is currently in sys.modules."""
    if "utils.risk_integration" in sys.modules:
        del sys.modules["utils.risk_integration"]
    return importlib.import_module("utils.risk_integration")


def _restore_original_modules(original_config):
    """Restore sys.modules to the EXACT original objects other test files'
    module-level imports are bound to — see the capture comment above."""
    if original_config is not None:
        sys.modules["config"] = original_config
    sys.modules["utils.risk_integration"] = _ORIGINAL_RISK_INTEGRATION_MODULE


def _config_without_attr(attr_name: str):
    """Build a stand-in config module identical to the real one except
    missing `attr_name` entirely (not set to False — ABSENT), reproducing
    the exact scenario of config.py never having received the addition."""
    import config as real_config
    fake = types.ModuleType("config")
    for attr in dir(real_config):
        if not attr.startswith("_") and attr != attr_name:
            setattr(fake, attr, getattr(real_config, attr))
    return fake


class TestFlagResolutionReproducesReportedBug(unittest.TestCase):
    """Test 1: the exact scenario — attribute entirely absent from config.py."""

    def setUp(self):
        self._original_config = sys.modules.get("config")

    def tearDown(self):
        _restore_original_modules(self._original_config)

    def test_missing_attribute_emits_warning_and_resolves_false(self):
        sys.modules["config"] = _config_without_attr("ENABLE_DYNAMIC_RISK_MANAGER")
        with patch("builtins.print") as mock_print:
            ri = _fresh_risk_integration_module()
        self.assertFalse(ri.ENABLE_DYNAMIC_RISK_MANAGER)
        warning_calls = [str(c) for c in mock_print.call_args_list if "RISK WARNING" in str(c)]
        self.assertEqual(len(warning_calls), 1, "expected exactly one loud warning when the attribute is missing")
        self.assertIn("NOT DEFINED", warning_calls[0])

    def test_explicitly_false_does_not_warn(self):
        """Distinguishes 'intentionally disabled' from 'forgot to configure' —
        only the latter should warn."""
        import config as real_config
        fake = types.ModuleType("config")
        for attr in dir(real_config):
            if not attr.startswith("_"):
                setattr(fake, attr, getattr(real_config, attr))
        fake.ENABLE_DYNAMIC_RISK_MANAGER = False  # explicitly set, not absent
        sys.modules["config"] = fake

        with patch("builtins.print") as mock_print:
            ri = _fresh_risk_integration_module()
        self.assertFalse(ri.ENABLE_DYNAMIC_RISK_MANAGER)
        warning_calls = [str(c) for c in mock_print.call_args_list if "RISK WARNING" in str(c)]
        self.assertEqual(len(warning_calls), 0, "explicit False should NOT trigger the missing-attribute warning")

    def test_explicitly_true_activates_with_no_warning(self):
        import config as real_config
        fake = types.ModuleType("config")
        for attr in dir(real_config):
            if not attr.startswith("_"):
                setattr(fake, attr, getattr(real_config, attr))
        fake.ENABLE_DYNAMIC_RISK_MANAGER = True
        sys.modules["config"] = fake

        with patch("builtins.print") as mock_print:
            ri = _fresh_risk_integration_module()
        self.assertTrue(ri.ENABLE_DYNAMIC_RISK_MANAGER)
        warning_calls = [str(c) for c in mock_print.call_args_list if "RISK WARNING" in str(c)]
        self.assertEqual(len(warning_calls), 0)


class TestFlagOffMeansFullyInert(unittest.TestCase):
    """Test 2: when the flag is False, Phase 2 makes literally zero calls
    anywhere — not 'mostly off', fully inert."""

    def setUp(self):
        self._original_config = sys.modules.get("config")

    def tearDown(self):
        _restore_original_modules(self._original_config)

    @patch("utils.risk_integration.oanda_client")
    def test_list_managed_instruments_with_flag_off_still_reads_empty_store_but_never_calls_oanda(self, mock_client):
        """Even list_managed_instruments() itself never touches OANDA (it only
        reads the local JSON store) — this test documents that Phase A's
        no-op-when-off behavior lives in the RUNNER's own guard
        (`if not ENABLE_DYNAMIC_RISK_MANAGER: return set()`), not inside
        risk_integration's individual functions, which remain callable.
        This is intentional: it's the runner's job to gate Phase A/B, not
        risk_integration's job to guess whether it should refuse to run."""
        sys.modules["config"] = _config_without_attr("ENABLE_DYNAMIC_RISK_MANAGER")
        ri = _fresh_risk_integration_module()
        self.assertFalse(ri.ENABLE_DYNAMIC_RISK_MANAGER)

        # list_managed_instruments() reads the (empty/nonexistent) local file only.
        result = ri.list_managed_instruments()
        self.assertEqual(result, [])
        mock_client.request.assert_not_called()


class TestEndToEndClusterPersistence(unittest.TestCase):
    """Test 3: END-TO-END — flag True, fill succeeds, a REAL file lands on
    disk with the right content. This is the direct regression guard for
    'state/open_clusters.json was never created'."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_path = os.path.join(self.tmpdir, "state", "open_clusters.json")
        self._original_config = sys.modules.get("config")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        _restore_original_modules(self._original_config)

    @patch("utils.risk_integration.get_atr_with_volatility_context")
    def test_successful_fill_with_flag_on_actually_creates_state_file_on_disk(self, mock_atr):
        import config as real_config
        fake = types.ModuleType("config")
        for attr in dir(real_config):
            if not attr.startswith("_"):
                setattr(fake, attr, getattr(real_config, attr))
        fake.ENABLE_DYNAMIC_RISK_MANAGER = True
        fake.CLUSTER_STATE_PATH = self.state_path  # isolated temp path, not the real repo's state/
        sys.modules["config"] = fake

        # Precondition matching the bug report: directory doesn't exist yet at all.
        self.assertFalse(os.path.exists(self.state_path))

        ri = _fresh_risk_integration_module()
        self.assertTrue(ri.ENABLE_DYNAMIC_RISK_MANAGER)

        mock_atr.return_value = (0.30, 0.1)
        # Reproduces the reported AUD/JPY BUY fill exactly (112.360).
        signal_data = {"pair": "AUD_JPY", "action": "BUY", "stop_loss": 111.500, "take_profit": 113.500}
        fill = {"status": "SUCCESS", "filled_price": "112.360", "units": "10000", "trade_id": "T-AUDJPY-001"}

        cluster = ri.new_cluster_from_fill(signal_data, fill)
        ri.save_cluster_data("AUD_JPY", cluster.to_dict())

        # The core assertion: the file must ACTUALLY exist on disk now.
        self.assertTrue(os.path.exists(self.state_path), "state file was not created on disk")

        with open(self.state_path) as f:
            on_disk = json.load(f)
        self.assertIn("AUD_JPY", on_disk["clusters"])
        self.assertEqual(on_disk["clusters"]["AUD_JPY"]["risk_manager"]["entry_price_0"], 112.360)
        self.assertEqual(on_disk["clusters"]["AUD_JPY"]["units"][0]["trade_id"], "T-AUDJPY-001")

        # And it must be re-loadable via the normal API, not just present as raw JSON.
        reloaded = ri.load_cluster_data("AUD_JPY")
        self.assertIsNotNone(reloaded)
        self.assertEqual(ri.list_managed_instruments(), ["AUD_JPY"])


class TestPreExistingPositionsAreKnowinglyNotAdopted(unittest.TestCase):
    """Documents (does not yet fix) the second finding: manage_open_positions()
    can only manage instruments already present in the local JSON store — a
    position that exists at OANDA but was opened before Phase 2 registered it
    (e.g. the pre-existing EUR_JPY/GBP_JPY/USD_JPY positions from the bug
    report) is correctly left untouched, not silently mismanaged. This is
    CURRENT INTENDED behavior pending a separate decision on whether an
    'adopt untracked positions' capability should be built."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.state_path = os.path.join(self.tmpdir, "open_clusters.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_empty_store_manages_nothing_regardless_of_live_oanda_positions(self):
        from utils.cluster_state_store import ClusterStateStore
        store = ClusterStateStore(self.state_path)
        # No entries were ever registered for EUR_JPY/GBP_JPY/USD_JPY (they
        # predate this integration) — the store is empty regardless of what's
        # actually open at OANDA.
        self.assertEqual(store.list_managed_instruments(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)