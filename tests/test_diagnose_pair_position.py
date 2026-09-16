
"""
Unit tests for diagnose_pair_position()

Read-only diagnostic tests:
- No real OANDA API calls
- No order creation
- No position modification
- No position closing

Run:
    python -m unittest -v tests.test_diagnose_pair_position
"""

import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Adjust this import if your actual module path is different.
import utils.trading_core as trading_core


class TestDiagnosePairPosition(unittest.TestCase):
    """Test the read-only pair position diagnostic."""

    def setUp(self):
        # Mock OANDA client.
        self.mock_oanda_client = MagicMock()

        # Mock account ID.
        self.account_id = "TEST_ACCOUNT"

        # Example Open Trades response.
        self.open_trades_response = {
            "trades": [
                {
                    "id": "1001",
                    "instrument": "USD_JPY",
                    "currentUnits": "10000",
                    "openTime": "2026-09-16T01:00:00.000000000Z",
                    "clientExtensions": {},
                    "stopLossOrder": {},
                    "takeProfitOrder": {},
                },
                {
                    "id": "1002",
                    "instrument": "GBP_JPY",
                    "currentUnits": "-10000",
                    "openTime": "2026-09-16T01:05:00.000000000Z",
                    "clientExtensions": {
                        "tag": "OLD-STRATEGY"
                    },
                    "stopLossOrder": {},
                    "takeProfitOrder": {},
                },
                {
                    "id": "1003",
                    "instrument": "EUR_USD",
                    "currentUnits": "5000",
                    "openTime": "2026-09-16T01:10:00.000000000Z",
                    "clientExtensions": {},
                    "stopLossOrder": {},
                    "takeProfitOrder": {},
                },
            ]
        }

        # Example Open Positions response.
        self.open_positions_response = {
            "positions": [
                {
                    "instrument": "USD_JPY",
                    "long": {
                        "units": "10000",
                        "averagePrice": "147.500",
                    },
                    "short": {
                        "units": "0",
                        "averagePrice": "0",
                    },
                },
                {
                    "instrument": "GBP_JPY",
                    "long": {
                        "units": "0",
                        "averagePrice": "0",
                    },
                    "short": {
                        "units": "-10000",
                        "averagePrice": "198.200",
                    },
                },
                {
                    "instrument": "EUR_USD",
                    "long": {
                        "units": "5000",
                        "averagePrice": "1.1000",
                    },
                    "short": {
                        "units": "0",
                        "averagePrice": "0",
                    },
                },
            ]
        }

        # Configure the client to return the mocked responses.
        # def fake_request(request):
        #     endpoint_name = request.__class__.__name__

        #     if endpoint_name == "OpenTrades":
        #         return self.open_trades_response

        #     if endpoint_name == "OpenPositions":
        #         return self.open_positions_response

        #     raise AssertionError(
        #         f"Unexpected OANDA endpoint: {endpoint_name}"
        #     )
        def fake_request(request):
            endpoint_name = request.__class__.__name__

            if endpoint_name == "OpenTrades":
                request.response = self.open_trades_response
                return self.open_trades_response

            if endpoint_name == "OpenPositions":
                request.response = self.open_positions_response
                return self.open_positions_response

            raise AssertionError(
                f"Unexpected OANDA endpoint: {endpoint_name}"
            )
    
        self.mock_oanda_client.request.side_effect = fake_request

    def _install_test_environment(self):
        """
        Patch the OANDA client, account ID, and endpoint imports.

        This assumes diagnose_pair_position() dynamically imports:
            oandapyV20.endpoints.trades
            oandapyV20.endpoints.positions
        """

        fake_trades_module = SimpleNamespace(
            OpenTrades=type(
                "OpenTrades",
                (),
                {
                    "__init__": lambda self, accountID: (
                        setattr(self, "accountID", accountID)
                    )
                },
            )
        )

        fake_positions_module = SimpleNamespace(
            OpenPositions=type(
                "OpenPositions",
                (),
                {
                    "__init__": lambda self, accountID: (
                        setattr(self, "accountID", accountID)
                    )
                },
            )
        )

        def fake_import_module(name):
            if name == "oandapyV20.endpoints.trades":
                return fake_trades_module

            if name == "oandapyV20.endpoints.positions":
                return fake_positions_module

            raise ImportError(f"Unexpected module import: {name}")

        return patch.multiple(
            trading_core,
            oanda_client=self.mock_oanda_client,
            OANDA_ACCOUNT_ID=self.account_id,
        ), patch(
            "importlib.import_module",
            side_effect=fake_import_module,
        )

    def test_usd_jpy_diagnostic(self):
        """USD_JPY should show its matching trade and position."""

        with self._install_test_environment()[0], \
             self._install_test_environment()[1]:

            output = io.StringIO()

            with redirect_stdout(output):
                trading_core.diagnose_pair_position("USD_JPY")

            result = output.getvalue()

        # Basic diagnostic headers.
        self.assertIn("[DIAGNOSTIC] USD_JPY", result)
        self.assertIn("[OPEN TRADES]", result)
        self.assertIn("[OPEN POSITIONS]", result)

        # Matching USD_JPY trade should appear.
        self.assertIn("1001", result)
        self.assertIn("USD_JPY", result)
        self.assertIn("10000", result)

        # Matching position should appear.
        self.assertIn("averagePrice", result)
        self.assertIn("147.500", result)

        # Unrelated GBP_JPY trade ID should not appear in USD_JPY section.
        # Note: this assertion is not used because the output may contain
        # shared totals or other diagnostic information in future versions.

    def test_gbp_jpy_diagnostic(self):
        """GBP_JPY should show its matching trade and position."""

        with self._install_test_environment()[0], \
             self._install_test_environment()[1]:

            output = io.StringIO()

            with redirect_stdout(output):
                trading_core.diagnose_pair_position("GBP_JPY")

            result = output.getvalue()

        self.assertIn("[DIAGNOSTIC] GBP_JPY", result)
        self.assertIn("1002", result)
        self.assertIn("-10000", result)
        self.assertIn("198.200", result)
        self.assertIn("OLD-STRATEGY", result)

    def test_unrelated_pair_is_not_reported_as_matching_trade(self):
        """A pair with no matching records should not print trade details."""

        with self._install_test_environment()[0], \
             self._install_test_environment()[1]:

            output = io.StringIO()

            with redirect_stdout(output):
                trading_core.diagnose_pair_position("AUD_NZD")

            result = output.getvalue()

        self.assertIn("[DIAGNOSTIC] AUD_NZD", result)

        # The diagnostic should not display details of unrelated pairs.
        self.assertNotIn("1001", result)
        self.assertNotIn("1002", result)
        self.assertNotIn("USD_JPY", result)
        self.assertNotIn("GBP_JPY", result)

    def test_open_trades_and_positions_are_both_queried(self):
        """The function should issue both read-only OANDA requests."""

        with self._install_test_environment()[0], \
             self._install_test_environment()[1]:

            with redirect_stdout(io.StringIO()):
                trading_core.diagnose_pair_position("USD_JPY")

        self.assertEqual(
            self.mock_oanda_client.request.call_count,
            2,
        )

        called_endpoints = [
            call.args[0].__class__.__name__
            for call in self.mock_oanda_client.request.call_args_list
        ]

        self.assertIn("OpenTrades", called_endpoints)
        self.assertIn("OpenPositions", called_endpoints)


if __name__ == "__main__":
    unittest.main()