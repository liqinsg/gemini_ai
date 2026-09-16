import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from utils import trading_core


class FakePricingInfo:
    def __init__(self, account_id, params):
        self.kind = "PricingInfo"
        self.params = params


class FakeOrderCreate:
    def __init__(self, account_id, payload):
        self.kind = "OrderCreate"
        self.payload = payload


class FakeTradeDetails:
    def __init__(self, account_id, trade_id):
        self.kind = "TradeDetails"
        self.trade_id = trade_id


class TestTradeClientExtensions(unittest.TestCase):

    def test_market_order_sets_trade_client_extensions(self):
        signal = SimpleNamespace(
            action="BUY",
            pair_to_trade="USD_JPY",
            stop_loss=151.257,
            take_profit=161.399,
            reasoning="unit-test",
        )

        client_extensions = {
            "id": "JPY-STRENGTH_USD_JPY_TEST",
            "tag": "JPY-STRENGTH_USD_JPY_BUY",
            "comment": "unit test",
        }

        captured = {}

        def fake_import_module(name):
            if name == "oandapyV20.endpoints.pricing":
                return SimpleNamespace(PricingInfo=FakePricingInfo)

            if name == "oandapyV20.endpoints.orders":
                return SimpleNamespace(OrderCreate=FakeOrderCreate)

            if name == "oandapyV20.endpoints.positions":
                class FakeOpenPositions:
                    kind = "OpenPositions"

                    def __init__(self, accountID=None, **kwargs):
                        self.response = {"positions": []}

                return SimpleNamespace(OpenPositions=FakeOpenPositions)

            if name == "oandapyV20.endpoints.trades":
                return SimpleNamespace(TradeDetails=FakeTradeDetails)

            raise AssertionError(f"Unexpected import: {name}")

        def fake_request(endpoint):
            if endpoint.kind == "OpenPositions":
                return {"positions": []}

            if endpoint.kind == "PricingInfo":
                return {
                    "prices": [{
                        "asks": [{"price": "155.318"}],
                        "bids": [{"price": "155.317"}],
                    }]
                }

            if endpoint.kind == "OrderCreate":
                captured["payload"] = endpoint.payload

                return {
                    "orderFillTransaction": {
                        "id": "TEST-ORDER",
                        "price": "155.318",
                        "tradeOpened": {
                            "tradeID": "TEST-TRADE"
                        },
                    }
                }

            if endpoint.kind == "TradeDetails":
                return {
                    "trade": {
                        "id": endpoint.trade_id,
                        "stopLossOrder": {"id": "TEST-SL"},
                        "takeProfitOrder": {"id": "TEST-TP"},
                    }
                }

            raise AssertionError(f"Unexpected endpoint: {endpoint.kind}")

        with patch.object(
            trading_core.importlib,
            "import_module",
            side_effect=fake_import_module,
        ), patch.object(
            trading_core.oanda_client,
            "request",
            side_effect=fake_request,
        ), patch.object(
            trading_core,
            "attach_sl_tp_to_open_trade",
            return_value=True,
        ):

            result = trading_core.execute_market_trade(
                signal,
                units_override=10000,
                dry_run=False,
                client_extensions=client_extensions,
            )

        self.assertTrue(result)

        self.assertIn("payload", captured)

        order = captured["payload"]["order"]

        # Order metadata must exist.
        self.assertEqual(
            order["clientExtensions"],
            client_extensions,
        )

        # Trade metadata must ALSO exist.
        self.assertEqual(
            order["tradeClientExtensions"],
            client_extensions,
        )

        # Both keys must carry the SAME client_extensions object (no copy that
        # could silently drop id/tag/comment on the resulting OANDA trade).
        self.assertIs(
            order["clientExtensions"],
            order["tradeClientExtensions"],
        )

        # The metadata fields the strategy owns must reach the wire verbatim.
        self.assertEqual(order["tradeClientExtensions"]["id"], client_extensions["id"])
        self.assertEqual(order["tradeClientExtensions"]["tag"], client_extensions["tag"])
        self.assertEqual(
            order["tradeClientExtensions"]["comment"],
            client_extensions["comment"],
        )
        self.assertEqual(order["tradeClientExtensions"]["tag"], "JPY-STRENGTH_USD_JPY_BUY")
        self.assertTrue(
            order["tradeClientExtensions"]["tag"].startswith("JPY-STRENGTH_")
        )


if __name__ == "__main__":
    unittest.main()
