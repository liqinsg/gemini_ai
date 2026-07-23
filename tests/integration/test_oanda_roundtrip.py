@patch("utils.trading_core.close_position")
@patch("utils.trading_core.attach_sl_tp_to_open_trade")
@patch("utils.trading_core.importlib.import_module")
@patch("utils.trading_core.get_open_position")
@patch("utils.trading_core.oanda_client")
def test_open_and_close_trade(
    self,
    mock_oanda,
    mock_get_position,
    mock_import_module,
    mock_attach,
    mock_close,
):
    mock_get_position.return_value = None

    mock_close.return_value = True

    pricing_response = {
        "prices": [{
            "asks": [{"price": "1.1000"}],
            "bids": [{"price": "1.0998"}]
        }]
    }

    order_response = {
        "orderFillTransaction": {
            "id": "123"
        }
    }

    mock_oanda.request.side_effect = [
        MagicMock(response=pricing_response),
        MagicMock(response=order_response),
    ]

    def module_side_effect(name):
        mod = MagicMock()

        if name == "oandapyV20.endpoints.pricing":
            mod.PricingInfo.return_value = "PRICE_REQ"

        if name == "oandapyV20.endpoints.orders":
            mod.OrderCreate.return_value = "ORDER_REQ"

        return mod

    mock_import_module.side_effect = module_side_effect

    execute_market_trade(self.signal)

    mock_attach.assert_called_once()

    result = close_position("EUR_USD")

    mock_close.assert_called_once_with("EUR_USD")