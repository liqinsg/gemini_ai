from types import SimpleNamespace

from utils.oanda_state import build_client_extensions, has_open_trade_or_order


def test_client_extensions_are_stable_for_the_same_signal_bar():
    signal = {
        "pair": "EUR_USD",
        "action": "BUY",
        "bar_time": "2026-09-12T10:00:00Z",
        "entry": 1.1,
        "stop_loss": 1.09,
        "take_profit": 1.12,
        "reasoning": "trend aligned",
    }

    first = build_client_extensions(signal, strategy_tag="jpy_strength")
    second = build_client_extensions(signal, strategy_tag="jpy_strength")

    assert first == second
    assert first["id"].startswith("JPY_STRENGTH_EUR_USD_20260912T100000Z")
    assert first["tag"] == "jpy_strength"
    assert "bar=20260912T100000Z" in first["comment"]


def test_open_trade_or_pending_order_blocks_entry():
    responses = iter(({"trades": []}, {"orders": [{"id": "pending-1"}]}))
    api_client = SimpleNamespace(request=lambda request: next(responses))

    assert has_open_trade_or_order(api_client, "account-1", "EUR_USD") is True