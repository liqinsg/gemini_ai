import utils.oanda_execution as oe


def test_open_oanda_order_reports_missing_fill_transaction(monkeypatch):
    """A non-fill response should report a clear error instead of crashing on a missing key."""
    signal = {
        "pair": "USD_JPY",
        "action": "BUY",
        "stop_loss": 162.10,
        "take_profit": 162.70,
    }

    response = {
        "orderCreateTransaction": {
            "id": "123",
            "type": "ORDER_CREATE",
            "instrument": "USD_JPY",
            "time": "2026-08-17T00:00:00Z",
        },
        "lastTransactionID": "123",
    }

    monkeypatch.setattr(oe, "OANDA_ACCOUNT_ID", "123-456-789")
    monkeypatch.setattr(oe, "OANDA_API_TOKEN", "fake-token")
    monkeypatch.setattr(oe.api, "request", lambda req: response)

    result = oe.open_oanda_order(signal, units=10000)

    assert result["status"] == "ERROR"
    lower = result["message"].lower()
    assert "missing" in lower or "orderfilltransaction" in lower
