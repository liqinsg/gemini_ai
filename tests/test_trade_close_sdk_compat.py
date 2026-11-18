"""Offline regression tests for OANDA TradeClose request construction.

Purpose: exercise the same TradingCore full/partial close methods used by
scheduled_runner_v31.py, with the installed oandapyV20 endpoint classes and a
fake client. The tests verify the endpoint reaches the fake client's request
method and never contact OANDA.

Risk: no credentials are loaded, no network client is constructed, and no
account query, order, or position mutation is possible in these tests.

Run manually with:
    python -m pytest -q tests/test_trade_close_sdk_compat.py
"""

from __future__ import annotations

import unittest
from typing import Any

import oandapyV20.endpoints.trades as trades_endpoints

from utils.trading_core_v2 import TradingCore


class FakeOandaClient:
    """A request sink that cannot perform network I/O."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.requests: list[Any] = []

    def request(self, endpoint: Any) -> dict[str, Any]:
        self.requests.append(endpoint)
        return self.response


class TestTradeCloseSdkCompatibility(unittest.TestCase):
    ACCOUNT_ID = "UNIT-TEST-ACCOUNT"
    TRADE_ID = "UNIT-TEST-TRADE"
    CLIENT_REQUEST_ID = "UNIT_TEST_CLOSE_REQUEST"

    def _core(self, response: dict[str, Any]) -> tuple[TradingCore, FakeOandaClient]:
        client = FakeOandaClient(response)
        # Explicitly use a synthetic account ID and the fake client. No config
        # loader, credential lookup, or OANDA API client is involved.
        core = TradingCore(
            oanda_client=client,
            oanda_account_id=self.ACCOUNT_ID,
            dry_run=False,
            market_closed=False,
        )
        return core, client

    def test_full_close_builds_sdk_request_and_sends_it_to_fake_client(self) -> None:
        response = {
            "tradesClosed": [
                {
                    "tradeID": self.TRADE_ID,
                    "units": "1000",
                    "realizedPL": "1.25",
                    "price": "150.000",
                }
            ]
        }
        core, client = self._core(response)

        ok, info = core.close_trade_by_id(
            self.TRADE_ID,
            client_request_id=self.CLIENT_REQUEST_ID,
        )

        self.assertTrue(ok, info)
        self.assertEqual(len(client.requests), 1)
        request = client.requests[0]
        self.assertIsInstance(request, trades_endpoints.TradeClose)
        self.assertEqual(request.data, {"units": "ALL"})
        self.assertEqual(info.get("realizedPL"), "1.25")

    def test_partial_close_builds_sdk_request_and_sends_it_to_fake_client(self) -> None:
        response = {
            "tradesClosed": [
                {
                    "tradeID": self.TRADE_ID,
                    "units": "250",
                    "realizedPL": "0.20",
                    "price": "150.000",
                }
            ]
        }
        core, client = self._core(response)

        ok, info = core.close_trade_partial(
            self.TRADE_ID,
            units=250,
            client_request_id=self.CLIENT_REQUEST_ID,
        )

        self.assertTrue(ok, info)
        self.assertEqual(len(client.requests), 1)
        request = client.requests[0]
        self.assertIsInstance(request, trades_endpoints.TradeClose)
        self.assertEqual(request.data, {"units": "250"})
        self.assertEqual(info.get("realizedPL"), "0.20")


if __name__ == "__main__":
    unittest.main(verbosity=2)
