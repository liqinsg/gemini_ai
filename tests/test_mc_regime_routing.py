"""MC-regime routing for ``scheduled_runner_v3``.

Replaces the previous contents of this file, which imported the archived
``scheduled_runner_v1_4`` module.  That module is not in the repo root (it
lives in ``archives/``), so collection failed with ``ModuleNotFoundError`` and
the entire pytest run aborted before running anything else.

The v1.4 file was 543 lines of monkeypatching v1.4-specific internals.  The
part that still describes live behaviour is the PURE regime mapping, which v3
keeps with identical semantics — so that coverage is carried over here
verbatim, and the v1.4-only scaffolding is dropped.

Covered (all pure functions, zero network / zero orders):
  * ``_clean_mc_regime``   — string normalisation (emoji, case, ordering, junk)
  * ``_regime_policy``     — regime → execution policy ("cautious"/"aggressive"/"normal")
  * ``_get_group_mc_regime`` — majority vote across a group, NO_MC_DATA fallback
  * ``_get_global_mc_regime`` — delegates to the same majority vote

Run:  pytest tests/test_mc_regime_routing.py -v
"""

from __future__ import annotations

import os
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

with redirect_stdout(StringIO()):  # module-level runner bootstrap is chatty
    import scheduled_runner_v3 as v3


class TestCleanMcRegime(unittest.TestCase):
    def test_empty_and_missing_map_to_neutral(self):
        for raw in (None, "", "   ", 0):
            with self.subTest(raw=raw):
                self.assertEqual(v3._clean_mc_regime(raw), "NEUTRAL")

    def test_known_regimes_are_extracted_from_noisy_labels(self):
        cases = [
            ("⏳ D CONSOLIDATION RANGE", "CONSOLIDATION"),
            ("consolidation", "CONSOLIDATION"),
            ("⚡ H4 STRONG MOMENTUM", "STRONG_MOMENTUM"),
            ("🔹 D NEUTRAL", "NEUTRAL"),
            ("n/a", "NEUTRAL"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(v3._clean_mc_regime(raw), expected)

    def test_bare_momentum_defaults_to_strong_momentum(self):
        self.assertEqual(v3._clean_mc_regime("MOMENTUM ONLY"), "STRONG_MOMENTUM")


class TestRegimePolicyMapping(unittest.TestCase):
    """Ported verbatim from the v1.4 suite — v3 keeps the same semantics."""

    CASES = [
        ("", "normal"),
        (None, "normal"),
        ("N/A", "normal"),
        ("NO_LOCAL_MC_DATA", "normal"),
        ("⏳ D CONSOLIDATION RANGE", "cautious"),
        ("💤 consolidation range", "cautious"),          # lowercase
        ("⚡ H4 STRONG MOMENTUM", "aggressive"),
        ("⚡ STRONG MOMENTUM H4", "aggressive"),          # reversed order
        ("STRONG MOMENTUM ONLY", "aggressive"),
        ("🔹 D NEUTRAL", "normal"),
    ]

    def test_regime_policy_mapping(self):
        for regime, expected in self.CASES:
            with self.subTest(regime=regime):
                self.assertEqual(v3._regime_policy(regime), expected)

    def test_policy_is_always_one_of_the_three_modes(self):
        for regime in ("", None, "N/A", "CONSOLIDATION", "STRONG_MOMENTUM",
                       "NEUTRAL", "garbage value"):
            with self.subTest(regime=regime):
                self.assertIn(v3._regime_policy(regime),
                              ("cautious", "aggressive", "normal"))


class TestGroupRegimeMajorityVote(unittest.TestCase):
    PAIRS = ["USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY"]

    def _with_regimes(self, mapping):
        """Patch the MC lookup so each pair reports ``mapping[pair]`` (None → no data)."""
        def _fake_item(pair):
            regime = mapping.get(pair)
            return None if regime is None else {"regime": regime}
        return patch.object(v3, "_get_mc_item", side_effect=_fake_item)

    def test_no_data_at_all_falls_back_to_no_mc_data(self):
        with self._with_regimes({p: None for p in self.PAIRS}):
            self.assertEqual(v3._get_group_mc_regime(self.PAIRS), "NO_MC_DATA")

    def test_empty_pair_list_falls_back_to_no_mc_data(self):
        self.assertEqual(v3._get_group_mc_regime([]), "NO_MC_DATA")

    def test_majority_wins(self):
        with self._with_regimes({
            "USD_JPY": "⚡ H4 STRONG MOMENTUM",
            "EUR_JPY": "STRONG MOMENTUM ONLY",
            "GBP_JPY": "🔹 D NEUTRAL",
            "AUD_JPY": "STRONG MOMENTUM",
        }):
            self.assertEqual(v3._get_group_mc_regime(self.PAIRS), "STRONG_MOMENTUM")

    def test_pairs_without_mc_data_are_skipped_not_counted_as_neutral(self):
        # 2 × CONSOLIDATION vs 1 × NEUTRAL, with one pair missing entirely.
        with self._with_regimes({
            "USD_JPY": "⏳ D CONSOLIDATION RANGE",
            "EUR_JPY": "consolidation",
            "GBP_JPY": "NEUTRAL",
            "AUD_JPY": None,
        }):
            self.assertEqual(v3._get_group_mc_regime(self.PAIRS), "CONSOLIDATION")

    def test_labels_are_normalised_before_voting(self):
        # Raw labels differ but all normalise to NEUTRAL → NEUTRAL, not a tie.
        with self._with_regimes({
            "USD_JPY": "🔹 D NEUTRAL",
            "EUR_JPY": "n/a",
            "GBP_JPY": "neutral",
            "AUD_JPY": "NEUTRAL",
        }):
            self.assertEqual(v3._get_group_mc_regime(self.PAIRS), "NEUTRAL")


class TestGlobalRegimeDelegatesToGroupVote(unittest.TestCase):
    def test_global_uses_the_same_majority_logic(self):
        pairs = ["USD_JPY", "EUR_JPY", "GBP_JPY"]

        def _fake_item(pair):
            return {"regime": "CONSOLIDATION"} if pair != "GBP_JPY" else {"regime": "NEUTRAL"}

        with patch.object(v3, "_get_mc_item", side_effect=_fake_item):
            self.assertEqual(v3._get_global_mc_regime(pairs), "CONSOLIDATION")

    def test_global_no_data_falls_back_to_no_mc_data(self):
        with patch.object(v3, "_get_mc_item", return_value=None):
            self.assertEqual(v3._get_global_mc_regime(["USD_JPY"]), "NO_MC_DATA")


if __name__ == "__main__":
    unittest.main(verbosity=2)
