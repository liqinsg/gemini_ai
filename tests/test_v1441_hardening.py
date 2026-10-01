"""Regression tests for the v1.4.5 hardening pass on ``scheduled_runner_v1441``.

Mirrors ``tests/test_v144_hardening.py`` for the v1441 (TradingCore) runner: the
same defensive helpers plus an end-to-end mocked dry-run cycle proving that a
malformed trade, a partial signal or a zero risk distance can no longer abort it.

Nothing here contacts OANDA and no trading decision is changed.

Run (all three work):
    python tests/test_v1441_hardening.py
    cd tests && python test_v1441_hardening.py
    python -m unittest -v tests.test_v1441_hardening
"""

import os
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Running this file as a script puts *this* directory on sys.path, not the
# project root, so `import scheduled_runner_v1441` would fail (same convention as
# the other tests in this package).
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import scheduled_runner_v1441 as runner


class TestValueCoercion(unittest.TestCase):
    def test_to_float_or_none_accepts_strings_and_numbers(self):
        self.assertEqual(runner._to_float_or_none("178.762"), 178.762)
        self.assertEqual(runner._to_float_or_none(178), 178.0)
        self.assertEqual(runner._to_float_or_none("-3.5"), -3.5)

    def test_to_float_or_none_treats_absent_and_bad_values_as_none(self):
        for value in (None, "", "n/a", {}, []):
            self.assertIsNone(runner._to_float_or_none(value), msg=repr(value))

    def test_abs_strength_score_tolerates_bad_payloads(self):
        self.assertEqual(runner._abs_strength_score({"strength_score": -1.25}), 1.25)
        self.assertEqual(runner._abs_strength_score({"strength_score": None}), 0.0)
        self.assertEqual(runner._abs_strength_score({}), 0.0)
        self.assertEqual(runner._abs_strength_score(None), 0.0)
        self.assertEqual(runner._abs_strength_score({"strength_score": "oops"}), 0.0)
        self.assertEqual(
            runner._abs_strength_score(SimpleNamespace(strength_score=-0.5)), 0.5
        )


class TestPositionUnits(unittest.TestCase):
    def test_reads_both_legs(self):
        position = {"instrument": "USD_JPY", "long": {"units": "10000"}, "short": {"units": "-2000"}}
        self.assertEqual(runner._position_units(position, "long"), 10000)
        self.assertEqual(runner._position_units(position, "short"), -2000)

    def test_null_and_missing_legs_are_flat(self):
        for position in (
            {"long": None, "short": None},
            {"long": {}, "short": {}},
            {"long": {"units": ""}, "short": {"units": "bad"}},
            {"long": "10000"},
            {},
        ):
            self.assertEqual(runner._position_units(position, "long"), 0)
            self.assertEqual(runner._position_units(position, "short"), 0)


class TestConfirmationNumericTolerance(unittest.TestCase):
    """OANDA echoes prices as strings; a numeric echo is still a confirmation."""

    def _fmt(self, value, instrument="EUR_JPY"):
        return runner.TradingCore.format_price_for_instrument(value, instrument)

    def test_numeric_echo_is_confirmed(self):
        calculated = 178.762
        self.assertEqual(
            runner._confirmation_result(
                {"id": "SL-1", "price": float(self._fmt(calculated))}, calculated, "EUR_JPY"
            ),
            ("CONFIRMED", "SL-1"),
        )

    def test_string_echo_is_still_confirmed(self):
        calculated = 178.762
        self.assertEqual(
            runner._confirmation_result(
                {"id": "SL-1", "price": self._fmt(calculated)}, calculated, "EUR_JPY"
            ),
            ("CONFIRMED", "SL-1"),
        )

    def test_mismatch_or_missing_order_is_not_confirmed(self):
        self.assertEqual(
            runner._confirmation_result({"id": "SL-1", "price": "1.0"}, 178.762, "EUR_JPY"),
            ("NOT_CONFIRMED", "SL-1"),
        )
        self.assertEqual(
            runner._confirmation_result({"price": "178.762"}, 178.762, "EUR_JPY"),
            ("NOT_CONFIRMED", None),
        )


class TestSignalShapeGuard(unittest.TestCase):
    def test_complete_signal_has_no_missing_fields(self):
        signal = {"pair": "USD_JPY", "action": "BUY", "entry": 1.0,
                  "stop_loss": 0.9, "take_profit": 1.2}
        self.assertEqual(runner._missing_signal_fields(signal), [])

    def test_partial_or_non_dict_signal_is_reported(self):
        self.assertEqual(
            runner._missing_signal_fields({"pair": "USD_JPY", "action": "BUY"}),
            ["entry", "stop_loss", "take_profit"],
        )
        self.assertEqual(
            runner._missing_signal_fields({"pair": "USD_JPY", "action": None,
                                           "entry": 1.0, "stop_loss": 0.9,
                                           "take_profit": 1.2}),
            ["action"],
        )
        self.assertEqual(runner._missing_signal_fields(None), list(runner._REQUIRED_SIGNAL_FIELDS))


class TestTpMultiplierGuard(unittest.TestCase):
    def test_scales_tp_and_risk_reward_for_buy(self):
        candidate = {"pair": "USD_JPY", "action": "BUY", "entry": 155.0,
                     "stop_loss": 154.0, "take_profit": 157.0}
        runner._apply_tp_multiplier(candidate, 1.5)
        self.assertEqual(candidate["take_profit"], 156.5)
        self.assertEqual(candidate["risk_reward"], 1.5)

    def test_scales_tp_for_sell_in_the_opposite_direction(self):
        candidate = {"pair": "USD_JPY", "action": "SELL", "entry": 155.0,
                     "stop_loss": 156.0, "take_profit": 153.0}
        runner._apply_tp_multiplier(candidate, 2.0)
        self.assertEqual(candidate["take_profit"], 153.0)
        self.assertEqual(candidate["risk_reward"], 2.0)

    def test_zero_risk_distance_keeps_strategy_tp(self):
        """The old code raised ZeroDivisionError here and killed the whole cycle."""
        candidate = {"pair": "USD_JPY", "action": "BUY", "entry": 155.0,
                     "stop_loss": 155.0, "take_profit": 157.0}
        with redirect_stdout(StringIO()) as buffer:
            runner._apply_tp_multiplier(candidate, 1.5)
        self.assertEqual(candidate["take_profit"], 157.0)
        self.assertNotIn("risk_reward", candidate)
        self.assertIn("zero/invalid", buffer.getvalue())

    def test_missing_or_unparseable_prices_do_not_raise(self):
        for candidate in ({"action": "BUY"}, {"action": "BUY", "entry": "x", "stop_loss": None}):
            runner._apply_tp_multiplier(candidate, 1.5)


class TestCycleReportIsDefensive(unittest.TestCase):
    def test_report_renders_without_env_or_account(self):
        report = runner._new_cycle_report()
        with patch.object(runner, "_trading_core", SimpleNamespace()), patch.object(
            runner, "_oanda_profile", {}
        ):
            with redirect_stdout(StringIO()) as buffer:
                runner._print_full_cycle_report(report, {"units": 1}, dry_run=False)
        output = buffer.getvalue()
        self.assertIn("MODE: UNKNOWN", output)
        self.assertIn("ACCOUNT: unknown", output)

    def test_incomplete_report_does_not_raise(self):
        """The report runs in a finally block; a KeyError there would mask the cycle."""
        with redirect_stdout(StringIO()) as buffer:
            runner._print_full_cycle_report({}, {}, dry_run=True)
        self.assertIn("could not render cycle report", buffer.getvalue())


def _good_signal(**overrides):
    signal = {"pair": "USD_JPY", "action": "BUY", "entry": 155.0, "stop_loss": 154.0,
              "take_profit": 157.0, "strength_score": -1.0, "risk_reward": 2.0,
              "reasoning": "unit-test"}
    signal.update(overrides)
    return signal


class TestRunCycleDryRunSmoke(unittest.TestCase):
    """End-to-end (fully mocked, dry-run) coverage of the main cycle path."""

    def _run_cycle(self, signal, tp_mult=1.5, guardian_raises=False):
        core = SimpleNamespace(oanda_account_id="TEST-ACCOUNT",
                               get_all_open_trades=lambda: [])
        if guardian_raises:
            def _boom(*args, **kwargs):
                raise ValueError("malformed trade record")
            guardian = _boom
        else:
            guardian = lambda *a, **k: None
        patches = [
            patch.object(runner, "_trading_core", core),
            patch.object(runner, "_print_mc_snapshot", lambda: None),
            patch.object(runner, "_validate_and_repair_sltp", guardian),
            patch.object(runner, "_is_emergency_lock_active_v144", lambda: False),
            patch.object(runner, "with_retry", MagicMock()),
            patch.object(runner, "get_last_signal", lambda: signal),
            patch.object(runner, "_get_mc_for_pair", lambda *a, **k: {"regime": "NEUTRAL"}),
            patch.object(runner, "_regime_params",
                         lambda mode: {"max_positions": 1, "tp_multiplier": tp_mult,
                                       "exit_tightness": 1.0}),
            patch.object(runner, "_check_pair_level_strategy_position",
                         lambda pair, action: (True, "no JPY-STRENGTH position on pair")),
            patch.object(runner._config, "ENABLE_MC_BASKET_EXECUTION", False),
            patch.object(runner._config, "POST_EXIT_GATE_ENABLED", False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        buffer = StringIO()
        with redirect_stdout(buffer):
            runner.run_cycle(dry_run=True)
        return buffer.getvalue()

    def test_dry_run_cycle_scales_tp_and_reports_dry_run(self):
        output = self._run_cycle(_good_signal())
        self.assertIn("TP×1.5 →", output)
        self.assertIn("[DRY RUN] Signal validated — no order sent.", output)
        self.assertIn("FULL CYCLE REPORT", output)

    def test_dry_run_cycle_survives_zero_risk_distance(self):
        """Old behaviour: ZeroDivisionError aborted the cycle before the report."""
        output = self._run_cycle(_good_signal(stop_loss=155.0))
        self.assertIn("risk distance is zero/invalid", output)
        self.assertIn("[DRY RUN] Signal validated — no order sent.", output)

    def test_incomplete_signal_holds_without_crashing(self):
        output = self._run_cycle({"pair": "USD_JPY", "action": "BUY"})
        self.assertIn("Incomplete strategy signal", output)
        self.assertIn("Core reason: Incomplete strategy signal", output)
        self.assertNotIn("Traceback", output)

    def test_guardian_exception_does_not_abort_the_cycle(self):
        """The guardian call sits before the cycle try-block; it must not kill entries."""
        output = self._run_cycle(_good_signal(), guardian_raises=True)
        self.assertIn("scan aborted (non-fatal)", output)
        self.assertIn("[DRY RUN] Signal validated — no order sent.", output)
        self.assertIn("FULL CYCLE REPORT", output)


if __name__ == "__main__":
    unittest.main()
