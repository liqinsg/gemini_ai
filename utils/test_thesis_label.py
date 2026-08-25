"""Focused regression coverage for executed-signal thesis labels."""

import json
import unittest
from unittest.mock import patch

from utils.signal_instrumentation import log_executed_signal


class TestThesisLabel(unittest.TestCase):
    def _captured_record(self, **kwargs):
        with patch("utils.signal_instrumentation._append_jsonl") as mock_append:
            log_executed_signal(**kwargs)
            args, _ = mock_append.call_args
            return args[1]

    def test_thesis_label_full_diagnostics(self):
        record = self._captured_record(
            cycle_id="2026-08-25T00:00:00+00:00",
            pair="EUR_JPY",
            direction="BUY",
            diagnostics={
                "slope": {"combined_label": "STRONG"},
                "volatility": {"class": "EXTREME"},
            },
        )
        self.assertEqual(record["thesis_label"], "BUY_STRONG_EXTREME")

    def test_thesis_label_missing_slope(self):
        record = self._captured_record(
            cycle_id="c1",
            pair="USD_JPY",
            direction="SELL",
            diagnostics={"volatility": {"class": "NORMAL"}},
        )
        self.assertEqual(record["thesis_label"], "SELL_UNKNOWN_NORMAL")

    def test_thesis_label_missing_volatility(self):
        record = self._captured_record(
            cycle_id="c1",
            pair="USD_JPY",
            direction="SELL",
            diagnostics={"slope": {"combined_label": "WEAK"}},
        )
        self.assertEqual(record["thesis_label"], "SELL_WEAK_UNKNOWN")

    def test_thesis_label_empty_diagnostics(self):
        record = self._captured_record(
            cycle_id="c1",
            pair="GBP_JPY",
            direction="BUY",
            diagnostics={},
        )
        self.assertEqual(record["thesis_label"], "BUY_UNKNOWN_UNKNOWN")

    def test_thesis_label_none_diagnostics_values(self):
        record = self._captured_record(
            cycle_id="c1",
            pair="AUD_JPY",
            direction="BUY",
            diagnostics={"slope": None, "volatility": None},
        )
        self.assertEqual(record["thesis_label"], "BUY_UNKNOWN_UNKNOWN")

    def test_existing_fields_unaffected(self):
        diagnostics = {"slope": {"combined_label": "STRONG"}}
        record = self._captured_record(
            cycle_id="c42",
            pair="EUR_JPY",
            direction="BUY",
            diagnostics=diagnostics,
        )
        self.assertEqual(record["log_type"], "signal_executed")
        self.assertEqual(record["cycle_id"], "c42")
        self.assertEqual(record["pair"], "EUR_JPY")
        self.assertEqual(record["direction"], "BUY")
        self.assertEqual(record["diagnostics"], diagnostics)


if __name__ == "__main__":
    unittest.main()
