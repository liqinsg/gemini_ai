"""
utils/test_position_direction.py
============================

Regression tests for the direction-aware entry fix (see position_direction.py
docstring for the bug this addresses — a real AUD/JPY SHORT-vs-BUY-signal
conflict found in the first live scheduled-runner cycle).

Required coverage per the fix request:
1. Same-direction existing position -> no new entry.
2. Opposite-direction existing position -> close it, then allow the new signal.
3. No existing position -> normal entry.

Plus: the HEDGED edge case, and closure failure handling — both surfaced by
actually reading get_open_position()'s/close_position()'s real shape rather
than assuming a simplified one.

NO live OANDA calls — get_open_position/close_position are mocked throughout.
"""

import os
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from utils.position_direction import (
    PositionDecision,
    PositionDirectionError,
    get_position_direction,
    resolve_signal_vs_position,
    resolve_and_prepare_entry,
)


def _position(long_units="0", short_units="0"):
    """Build a fake get_open_position() return value, matching OANDA's real shape."""
    return {"instrument": "AUD_JPY", "long": {"units": long_units}, "short": {"units": short_units}}


# ---------------------------------------------------------------------------
# get_position_direction() — parsing the raw OANDA shape correctly
# ---------------------------------------------------------------------------

class TestGetPositionDirection(unittest.TestCase):
    @patch("utils.position_direction.get_open_position")
    def test_no_position_returns_none(self, mock_get):
        mock_get.return_value = None
        self.assertIsNone(get_position_direction("AUD_JPY"))

    @patch("utils.position_direction.get_open_position")
    def test_long_units_returns_buy(self, mock_get):
        mock_get.return_value = _position(long_units="10000", short_units="0")
        self.assertEqual(get_position_direction("AUD_JPY"), "BUY")

    @patch("utils.position_direction.get_open_position")
    def test_short_units_returns_sell(self, mock_get):
        # This is the EXACT shape from the screenshot's AUD/JPY SHORT position.
        mock_get.return_value = _position(long_units="0", short_units="-10000")
        self.assertEqual(get_position_direction("AUD_JPY"), "SELL")

    @patch("utils.position_direction.get_open_position")
    def test_both_sides_open_returns_hedged(self, mock_get):
        mock_get.return_value = _position(long_units="10000", short_units="-5000")
        self.assertEqual(get_position_direction("AUD_JPY"), "HEDGED")

    @patch("utils.position_direction.get_open_position")
    def test_zero_units_both_sides_returns_none(self, mock_get):
        # Some APIs return a stale zero-unit entry rather than omitting it — must not
        # be misread as an open position.
        mock_get.return_value = _position(long_units="0", short_units="0")
        self.assertIsNone(get_position_direction("AUD_JPY"))


# ---------------------------------------------------------------------------
# resolve_signal_vs_position() — pure decision logic (no I/O)
# ---------------------------------------------------------------------------

class TestResolveSignalVsPosition(unittest.TestCase):
    # --- Requirement 1: same direction -> no new entry ---
    def test_buy_signal_existing_buy_skips(self):
        self.assertEqual(resolve_signal_vs_position("BUY", "BUY"), PositionDecision.SKIP_SAME_DIRECTION)

    def test_sell_signal_existing_sell_skips(self):
        self.assertEqual(resolve_signal_vs_position("SELL", "SELL"), PositionDecision.SKIP_SAME_DIRECTION)

    # --- Requirement 2: opposite direction -> close then enter ---
    def test_buy_signal_existing_sell_closes_then_enters(self):
        # This is exactly the AUD/JPY screenshot scenario: SHORT open, BUY signal.
        self.assertEqual(resolve_signal_vs_position("BUY", "SELL"), PositionDecision.CLOSE_THEN_ENTER)

    def test_sell_signal_existing_buy_closes_then_enters(self):
        self.assertEqual(resolve_signal_vs_position("SELL", "BUY"), PositionDecision.CLOSE_THEN_ENTER)

    # --- Requirement 3: no existing position -> normal entry ---
    def test_buy_signal_no_position_enters(self):
        self.assertEqual(resolve_signal_vs_position("BUY", None), PositionDecision.ENTER)

    def test_sell_signal_no_position_enters(self):
        self.assertEqual(resolve_signal_vs_position("SELL", None), PositionDecision.ENTER)

    # --- Edge case surfaced during design: hedged (both sides open) ---
    def test_hedged_position_is_skipped_regardless_of_signal(self):
        self.assertEqual(resolve_signal_vs_position("BUY", "HEDGED"), PositionDecision.SKIP_HEDGED)
        self.assertEqual(resolve_signal_vs_position("SELL", "HEDGED"), PositionDecision.SKIP_HEDGED)


# ---------------------------------------------------------------------------
# resolve_and_prepare_entry() — full orchestration, including the actual close call
# ---------------------------------------------------------------------------

class TestResolveAndPrepareEntry(unittest.TestCase):
    @patch("utils.position_direction.close_position")
    @patch("utils.position_direction.get_open_position")
    def test_no_position_enters_without_calling_close(self, mock_get, mock_close):
        mock_get.return_value = None
        decision = resolve_and_prepare_entry("AUD_JPY", "BUY")
        self.assertEqual(decision, PositionDecision.ENTER)
        mock_close.assert_not_called()

    @patch("utils.position_direction.close_position")
    @patch("utils.position_direction.get_open_position")
    def test_same_direction_skips_without_calling_close(self, mock_get, mock_close):
        mock_get.return_value = _position(long_units="10000", short_units="0")
        decision = resolve_and_prepare_entry("AUD_JPY", "BUY")
        self.assertEqual(decision, PositionDecision.SKIP_SAME_DIRECTION)
        mock_close.assert_not_called()

    @patch("utils.position_direction.close_position")
    @patch("utils.position_direction.get_open_position")
    def test_opposite_direction_closes_then_returns_close_then_enter(self, mock_get, mock_close):
        # Reproduces the screenshot exactly: SHORT AUD/JPY open, BUY signal arrives.
        mock_get.return_value = _position(long_units="0", short_units="-10000")
        mock_close.return_value = True

        decision = resolve_and_prepare_entry("AUD_JPY", "BUY")

        self.assertEqual(decision, PositionDecision.CLOSE_THEN_ENTER)
        mock_close.assert_called_once_with("AUD_JPY")

    @patch("utils.position_direction.close_position")
    @patch("utils.position_direction.get_open_position")
    def test_close_failure_raises_and_entry_must_not_proceed(self, mock_get, mock_close):
        mock_get.return_value = _position(long_units="0", short_units="-10000")
        mock_close.return_value = False  # OANDA close call failed

        with self.assertRaises(PositionDirectionError) as ctx:
            resolve_and_prepare_entry("AUD_JPY", "BUY")
        self.assertIn("AUD_JPY", str(ctx.exception))
        mock_close.assert_called_once_with("AUD_JPY")

    @patch("utils.position_direction.close_position")
    @patch("utils.position_direction.get_open_position")
    def test_hedged_position_skips_without_calling_close(self, mock_get, mock_close):
        mock_get.return_value = _position(long_units="10000", short_units="-5000")
        decision = resolve_and_prepare_entry("AUD_JPY", "BUY")
        self.assertEqual(decision, PositionDecision.SKIP_HEDGED)
        mock_close.assert_not_called()

    @patch("utils.position_direction.close_position")
    @patch("utils.position_direction.get_open_position")
    def test_eurjpy_same_scenario_class_as_audjpy(self, mock_get, mock_close):
        """Explicitly covers the 'same situation exists for other pairs such as
        EUR/JPY' note — proves the fix is instrument-agnostic, not AUD/JPY-specific."""
        mock_get.return_value = {"instrument": "EUR_JPY", "long": {"units": "0"}, "short": {"units": "-10000"}}
        mock_close.return_value = True
        decision = resolve_and_prepare_entry("EUR_JPY", "BUY")
        self.assertEqual(decision, PositionDecision.CLOSE_THEN_ENTER)
        mock_close.assert_called_once_with("EUR_JPY")


if __name__ == "__main__":
    unittest.main(verbosity=2)