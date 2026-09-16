"""Regression tests for the v1.4.4 SL/TP guardian numeric contract.

Bug covered
-----------
``_validate_and_repair_sltp()`` fed the *instrument-formatted string* calculated
SL/TP into ``_sltp_decision()`` / ``_audit_side()``, which subtract that value
from OANDA's *float* current SL/TP price.  Every cycle therefore aborted with::

    TypeError: unsupported operand type(s) for -: 'str' and 'float'

The calculated SL/TP must stay instrument-rounded but NUMERIC.

All OANDA access is mocked: no order is ever created, modified or closed.

Run:
    python -m unittest -v tests.test_v144_sltp_guardian
"""

import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import scheduled_runner_v144 as runner

TAG = "JPY-STRENGTH_EUR_JPY_BUY_20260916"


def _trade(trade_id, instrument, units, tag=None, sl=None, tp=None):
    """An OANDA open-trade record; prices are STRINGS, exactly as OANDA sends them."""
    return {
        "id": trade_id,
        "instrument": instrument,
        "currentUnits": str(units),
        "clientExtensions": {"tag": tag} if tag else {},
        "stopLossOrder": {"price": sl} if sl is not None else {},
        "takeProfitOrder": {"price": tp} if tp is not None else {},
    }


class _FakeOpenTrades:
    kind = "open_trades"

    def __init__(self, accountID=None):
        self.response = {"trades": []}


class _FakeTradeDetails:
    kind = "details"

    def __init__(self, accountID=None, tradeID=None):
        self.accountID = accountID
        self.tradeID = tradeID


class TestSltpDecisionNumericContract(unittest.TestCase):
    """The decision helper must work on numbers (bug: str - float crash)."""

    def test_delta_is_numeric_and_triggers_update(self):
        decision, delta = runner._sltp_decision(209.339, 209.039)
        self.assertEqual(decision, "UPDATE_REQUIRED")
        self.assertIsInstance(delta, float)
        self.assertAlmostEqual(delta, -0.3, places=6)

    def test_no_current_price_requests_update(self):
        self.assertEqual(runner._sltp_decision(None, 209.039), ("UPDATE_REQUIRED", None))

    def test_sub_precision_noise_is_not_an_update(self):
        decision, _ = runner._sltp_decision(209.339, 209.3390000001)
        self.assertEqual(decision, "NO_CHANGE")


class TestGuardianNumericRegression(unittest.TestCase):
    """End-to-end (mocked) guardian behaviour for strategy trades."""

    ACCOUNT = "TEST-ACCOUNT"

    def _run(self, trades, details_for_call, dry_run):
        """Run the guardian with a fully mocked OANDA client.

        ``details_for_call(call_number, trade_id)`` returns the TradeDetails body.
        Returns (report, attach_mock, captured_stdout).
        """

        open_trades = _FakeOpenTrades()
        open_trades.response = {"trades": trades}
        trades_mod = SimpleNamespace(
            OpenTrades=lambda accountID=None, **kwargs: open_trades,
            TradeDetails=_FakeTradeDetails,
        )

        client = MagicMock()
        calls = {"details": 0}

        def _request(endpoint):
            if getattr(endpoint, "kind", None) != "details":
                return None
            calls["details"] += 1
            return details_for_call(calls["details"], endpoint.tradeID)

        client.request.side_effect = _request

        report = runner._new_cycle_report()
        attach = MagicMock(return_value=True)
        buffer = StringIO()

        with patch.object(runner, "trades_mod", trades_mod), patch.object(
            runner, "oanda_client", client
        ), patch.object(runner._config, "OANDA_ACCOUNT_ID", self.ACCOUNT), patch.object(
            runner, "attach_sl_tp_to_open_trade", attach
        ), redirect_stdout(buffer):
            runner._validate_and_repair_sltp(report, dry_run=dry_run)

        return report, attach, buffer.getvalue()

    def test_guardian_does_not_crash_on_string_sltp_prices(self):
        """The exact production failure: OANDA reports SL/TP prices as strings."""
        trades = [_trade("1951", "EUR_JPY", 1000, tag=TAG, sl="178.762", tp="179.762")]

        def details(_call, _trade_id):
            return {
                "trade": {
                    "price": "179.275",
                    "stopLossOrder": {"id": "SL-1", "price": "178.762"},
                    "takeProfitOrder": {"id": "TP-1", "price": "179.762"},
                }
            }

        report, attach, output = self._run(trades, details, dry_run=False)

        self.assertEqual(report["trades_scanned"], 1)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["sl_requests"], 0)
        self.assertEqual(report["tp_requests"], 0)
        attach.assert_not_called()
        self.assertEqual(output.count("DECISION=DRM_MANAGED"), 2)

        # Deltas must be rendered as numbers, not raw strings.
        deltas = [
            line.split("=", 1)[1]
            for line in output.splitlines()
            if line.strip().startswith("DELTA=")
        ]
        self.assertEqual(len(deltas), 2)
        for value in deltas:
            float(value)

    def test_missing_sltp_in_dry_run_is_reported_but_not_sent(self):
        trades = [_trade("1999", "USD_JPY", 10000, tag=TAG)]

        report, attach, output = self._run(
            trades, lambda _call, _tid: {"trade": {"price": "155.419"}}, dry_run=True
        )

        self.assertEqual(report["trades_scanned"], 1)
        self.assertEqual(report["sl_required"], 1)
        self.assertEqual(report["tp_required"], 1)
        self.assertEqual(report["sl_requests"], 0)
        self.assertEqual(report["tp_requests"], 0)
        attach.assert_not_called()
        self.assertIn("SKIPPED_DRYRUN", output)

    def test_missing_sltp_is_repaired_with_numeric_prices(self):
        instrument = "USD_JPY"
        entry = 155.419
        pip = getattr(runner._config, "JPY_PIP", 0.01)
        expected_sl = float(
            runner.format_price_for_instrument(entry - runner.SL_PIPS * pip, instrument)
        )
        expected_tp = float(
            runner.format_price_for_instrument(
                entry + runner.TP_PIPS * pip * runner.TP_RATIO, instrument
            )
        )
        trades = [_trade("2000", instrument, 10000, tag=TAG)]

        def details(call_number, _trade_id):
            if call_number == 1:
                return {"trade": {"price": str(entry)}}
            return {
                "trade": {
                    "price": str(entry),
                    "stopLossOrder": {
                        "id": "SL-9",
                        "price": runner.format_price_for_instrument(
                            expected_sl, instrument
                        ),
                    },
                    "takeProfitOrder": {
                        "id": "TP-9",
                        "price": runner.format_price_for_instrument(
                            expected_tp, instrument
                        ),
                    },
                }
            }

        report, attach, _output = self._run(trades, details, dry_run=False)

        attach.assert_called_once()
        signal = attach.call_args.args[0]
        self.assertIsInstance(signal.stop_loss, float)
        self.assertIsInstance(signal.take_profit, float)
        self.assertAlmostEqual(signal.stop_loss, expected_sl, places=6)
        self.assertAlmostEqual(signal.take_profit, expected_tp, places=6)
        self.assertEqual(report["sl_requests"], 1)
        self.assertEqual(report["tp_requests"], 1)
        self.assertEqual(report["confirmed"], 1)
        self.assertEqual(report["failed"], 0)


if __name__ == "__main__":
    unittest.main()
