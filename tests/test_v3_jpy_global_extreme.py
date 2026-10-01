"""Regression tests for the JPY global-extreme entry gate (scheduled_runner_v3).

Rule: the JPY group may open NEW entries only while JPY is either the STRONGEST
or the WEAKEST currency in the global strength ranking.  A mid-table JPY (the
2026-10-01 05:57 run had JPY 4th of 6) must produce no JPY signals.

Scope is entry-side only: the guardian / risk / early-exit path runs in
``_maintain_group_positions()`` before ``_run_single_group()``, so exits are
never blocked by this gate.  That is asserted here too.

Nothing here contacts OANDA or reads candles.

Run (all three work):
    python tests/test_v3_jpy_global_extreme.py
    cd tests && python test_v3_jpy_global_extreme.py
    python -m unittest -v tests.test_v3_jpy_global_extreme
"""

import os
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

# Running this file as a script puts *this* directory on sys.path, not the
# project root, so the repo imports would fail (same convention as the other
# tests in this package).
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import scheduled_runner_v3 as v3

# Exactly the ranking from the 2026-10-01 05:57 live run: JPY is 4th of 6.
LIVE_RANKING = {
    "USD": 1.8739,
    "GBP": 1.3054,
    "EUR": -0.1483,
    "JPY": -0.9420,
    "AUD": -1.9567,
    "CHF": -2.4035,
}
JPY_TOP = dict(LIVE_RANKING, JPY=+3.0)      # JPY strongest
JPY_BOTTOM = dict(LIVE_RANKING, JPY=-4.0)   # JPY weakest

JPY_CFG = {"quote_ccy": "JPY", "tag_prefix": "JPY-STRENGTH"}


class TestGlobalRankHelper(unittest.TestCase):
    def test_rank_matches_the_printed_banner(self):
        self.assertEqual(v3._quote_ccy_global_rank("JPY", LIVE_RANKING),
                         (4, 6, "mid-table"))
        self.assertEqual(v3._quote_ccy_global_rank("USD", LIVE_RANKING),
                         (1, 6, "strongest currency"))
        self.assertEqual(v3._quote_ccy_global_rank("CHF", LIVE_RANKING),
                         (6, 6, "weakest currency"))

    def test_absent_currency_is_rank_zero(self):
        rank, total, reason = v3._quote_ccy_global_rank("NZD", LIVE_RANKING)
        self.assertEqual((rank, total), (0, 6))
        self.assertIn("absent", reason)

    def test_empty_matrix_does_not_raise(self):
        self.assertEqual(v3._quote_ccy_global_rank("JPY", {}), (0, 0, "absent from global matrix"))


class TestJpyGroupGate(unittest.TestCase):
    def _run_group(self, scores, enabled=True, quote_ccy="JPY", group_name="JPY"):
        """Run the group with MC+strategy stubbed out.

        ``_get_group_mc_regime`` is a tripwire: the skip path must return before
        touching MC or candles at all.
        """
        cfg = dict(JPY_CFG, quote_ccy=quote_ccy)
        buffer = StringIO()
        with patch.object(v3, "JPY_REQUIRE_GLOBAL_EXTREME", enabled), \
                patch.object(v3, "_get_group_mc_regime",
                             side_effect=AssertionError("MC must not run for a skipped group")), \
                redirect_stdout(buffer):
            return v3._run_single_group(group_name, cfg, scores), buffer.getvalue()

    def test_mid_table_jpy_is_skipped_before_any_mc_work(self):
        result, output = self._run_group(LIVE_RANKING)
        self.assertEqual(result["signals"], [])
        self.assertEqual(result["skip_reason"], "JPY_NOT_GLOBAL_EXTREME")
        self.assertIn("SKIP — JPY ranks 4/6", output)
        self.assertIn("JPY GLOBAL-EXTREME", output)

    def test_top_ranked_jpy_passes_the_gate(self):
        """Past the gate the run continues — and then the MC tripwire fires."""
        with self.assertRaises(AssertionError):
            self._run_group(JPY_TOP)

    def test_bottom_ranked_jpy_passes_the_gate(self):
        with self.assertRaises(AssertionError):
            self._run_group(JPY_BOTTOM)

    def test_gate_disabled_restores_old_behaviour(self):
        with self.assertRaises(AssertionError):
            self._run_group(LIVE_RANKING, enabled=False)

    def test_non_jpy_group_is_unaffected(self):
        """A mid-table CHF group must not be gated by the JPY rule."""
        with self.assertRaises(AssertionError):
            self._run_group(LIVE_RANKING, quote_ccy="CHF", group_name="CHF")


class TestGateFlagDefault(unittest.TestCase):
    def test_defaults_to_enabled(self):
        self.assertTrue(v3.JPY_REQUIRE_GLOBAL_EXTREME)

    def test_rank_zero_is_treated_as_not_extreme(self):
        """Absent JPY in the matrix must fail safe (skip), not pass."""
        cfg = dict(JPY_CFG)
        buffer = StringIO()
        with patch.object(v3, "JPY_REQUIRE_GLOBAL_EXTREME", True), \
                patch.object(v3, "_get_group_mc_regime",
                             side_effect=AssertionError("must not run")), \
                redirect_stdout(buffer):
            res = v3._run_single_group("JPY", cfg, {"USD": 1.0, "GBP": 0.5})
        self.assertEqual(res["skip_reason"], "JPY_NOT_GLOBAL_EXTREME")
        self.assertIn("ranks 0/2", buffer.getvalue())


class TestExitsAreNotGated(unittest.TestCase):
    """The gate lives in the entry path only; maintenance runs before it."""

    def test_maintain_group_positions_is_called_before_group_strategy(self):
        src = open(v3.__file__, encoding="utf-8").read()
        self.assertLess(
            src.index("_maintain_group_positions(gname, gcfg, dry_run, _global_scores)"),
            src.index("result = _run_single_group(gname, gcfg, _global_scores)"),
        )


if __name__ == "__main__":
    unittest.main()
