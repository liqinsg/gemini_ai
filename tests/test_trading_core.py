# tests/test_trading_core.py

import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from utils.trading_core import execute_market_trade


class TestExecuteMarketTrade(unittest.TestCase):

    def setUp(self):
        self.signal = SimpleNamespace(
            action="BUY",
            pair_to_trade="EUR_USD",
            stop_loss=1.08000,
            take_profit=1.12000,
            reasoning="Unit test trade"
        )

    # ----------------------------------
    # Test 1: None signal
    # ----------------------------------
    def test_none_signal(self):
        with patch("utils.trading_core.get_open_position") as mock_pos:
            execute_market_trade(None)

            mock_pos.assert_not_called()

    # ----------------------------------
    # Test 2: HOLD signal
    # ----------------------------------
    def test_hold_signal(self):
        signal = SimpleNamespace(action="HOLD")

        with patch("utils.trading_core.get_open_position") as mock_pos:
            execute_market_trade(signal)

            mock_pos.assert_not_called()

    # ----------------------------------
    # Test 3: Existing position
    # ----------------------------------
    @patch("utils.trading_core.get_open_position")
    def test_position_already_exists(self, mock_pos):
        mock_pos.return_value = {"instrument": "EUR_USD"}

        execute_market_trade(self.signal)

        mock_pos.assert_called_once_with("EUR_USD")

    # ----------------------------------
    # Test 4: Successful market order
    # ----------------------------------
    @patch("utils.trading_core.attach_sl_tp_to_open_trade")
    @patch("utils.trading_core.importlib.import_module")
    @patch("utils.trading_core.get_open_position")
    @patch("utils.trading_core.oanda_client")
    def test_successful_buy_order(
        self,
        mock_oanda,
        mock_get_position,
        mock_import_module,
        mock_attach
    ):
        mock_get_position.return_value = None

        # Pricing response
        pricing_response = {
            "prices": [
                {
                    "asks": [{"price": "1.10000"}],
                    "bids": [{"price": "1.09980"}]
                }
            ]
        }

        # Order create response
        order_response = {
            "orderFillTransaction": {
                "id": "123"
            }
        }

        mock_oanda.request.side_effect = [
            MagicMock(response=pricing_response),
            MagicMock(response=order_response)
        ]

        mock_orders_module = MagicMock()
        mock_orders_module.OrderCreate.return_value = "ORDER_REQ"

        def module_side_effect(name):
            if name == "oandapyV20.endpoints.pricing":
                mock_pricing = MagicMock()
                mock_pricing.PricingInfo.return_value = "PRICE_REQ"
                return mock_pricing

            if name == "oandapyV20.endpoints.orders":
                return mock_orders_module

            return MagicMock()

        mock_import_module.side_effect = module_side_effect

        execute_market_trade(self.signal)

        self.assertEqual(mock_oanda.request.call_count, 2)
        mock_attach.assert_called_once()

    # ----------------------------------
    # Test 5: Invalid BUY SL/TP
    # ----------------------------------
    @patch("utils.trading_core.importlib.import_module")
    @patch("utils.trading_core.get_open_position")
    @patch("utils.trading_core.oanda_client")
    def test_invalid_buy_sl_tp(
        self,
        mock_oanda,
        mock_get_position,
        mock_import_module
    ):
        mock_get_position.return_value = None

        bad_signal = SimpleNamespace(
            action="BUY",
            pair_to_trade="EUR_USD",
            stop_loss=1.10100,      # above ask
            take_profit=1.12000,
            reasoning="Bad trade"
        )

        pricing_response = {
            "prices": [
                {
                    "asks": [{"price": "1.10000"}],
                    "bids": [{"price": "1.09980"}]
                }
            ]
        }

        mock_oanda.request.return_value = MagicMock(
            response=pricing_response
        )

        pricing_mod = MagicMock()
        pricing_mod.PricingInfo.return_value = "PRICE_REQ"

        mock_import_module.return_value = pricing_mod

        execute_market_trade(bad_signal)

        # Only pricing check should happen
        self.assertEqual(mock_oanda.request.call_count, 1)

    # ----------------------------------
    # Test 6: Sell order units negative
    # ----------------------------------
    @patch("utils.trading_core.attach_sl_tp_to_open_trade")
    @patch("utils.trading_core.importlib.import_module")
    @patch("utils.trading_core.get_open_position")
    @patch("utils.trading_core.oanda_client")
    def test_sell_order_units(
        self,
        mock_oanda,
        mock_get_position,
        mock_import_module,
        mock_attach
    ):
        mock_get_position.return_value = None

        signal = SimpleNamespace(
            action="SELL",
            pair_to_trade="EUR_USD",
            stop_loss=1.1200,
            take_profit=1.0800,
            reasoning="Sell test"
        )

        pricing_response = {
            "prices": [
                {
                    "asks": [{"price": "1.1000"}],
                    "bids": [{"price": "1.0998"}]
                }
            ]
        }

        order_response = {
            "orderFillTransaction": {
                "id": "999"
            }
        }

        mock_oanda.request.side_effect = [
            MagicMock(response=pricing_response),
            MagicMock(response=order_response)
        ]

        mock_orders_mod = MagicMock()

        def module_side_effect(name):
            if name == "oandapyV20.endpoints.pricing":
                mod = MagicMock()
                mod.PricingInfo.return_value = "PRICE_REQ"
                return mod

            if name == "oandapyV20.endpoints.orders":
                mock_orders_mod.OrderCreate.return_value = "ORDER_REQ"
                return mock_orders_mod

            return MagicMock()

        mock_import_module.side_effect = module_side_effect

        execute_market_trade(signal)

        mock_attach.assert_called_once()


def close_position(instrument: str) -> bool:
    """
    Close an open position for a specific instrument.

    Returns:
        True  -> position closed successfully
        False -> no position found or error occurred
    """
    positions_mod = importlib.import_module(
        "oandapyV20.endpoints.positions"
    )

    try:
        # Find the position
        req = positions_mod.OpenPositions(
            accountID=OANDA_ACCOUNT_ID
        )
        oanda_client.request(req)

        position = next(
            (
                p
                for p in req.response.get("positions", [])
                if p.get("instrument") == instrument
            ),
            None,
        )

        if not position:
            print(f"[CLOSE] No open position for {instrument}")
            return False

        long_units = int(
            float(position.get("long", {}).get("units", 0))
        )

        short_units = int(
            float(position.get("short", {}).get("units", 0))
        )

        payload = {}

        if long_units > 0:
            payload["longUnits"] = str(long_units)

        if short_units < 0:
            payload["shortUnits"] = str(abs(short_units))

        if not payload:
            print(f"[CLOSE] Nothing to close for {instrument}")
            return False

        close_req = positions_mod.PositionClose(
            accountID=OANDA_ACCOUNT_ID,
            instrument=instrument,
            data=payload,
        )

        oanda_client.request(close_req)

        print(f"[CLOSE] Closed {instrument}")

        for side in (
            "longOrderFillTransaction",
            "shortOrderFillTransaction",
        ):
            txn = close_req.response.get(side, {})
            if txn:
                print(
                    f"[CLOSE] {txn.get('units')} units "
                    f"@ {txn.get('price')} "
                    f"(P&L {txn.get('pl')})"
                )

        return True

    except Exception as e:
        print(f"[CLOSE ERROR] {e}")
        return False

if __name__ == "__main__":
    unittest.main(verbosity=2)