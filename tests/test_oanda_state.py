import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import config
import pytest
from oandapyV20.exceptions import V20Error

from utils.oanda_state import build_client_extensions, has_open_trade_or_order
from utils.schemas import TradeSignal
import utils.trading_core as trading_core


# The cron runner is the live entry point; it must block duplicates from OANDA's
# own state (utils.oanda_state), never from a local `state/` file.
RUNNER_PATH = Path(__file__).resolve().parents[1] / "scheduled_runner_v13.py"
STRATEGY_TAG = "JPY-STRENGTH_GBP_JPY_BUY_20260914"


def _load_scheduler(monkeypatch):
    """Load the versioned runner with deterministic, offline test settings."""
    monkeypatch.setattr(config, "OANDA_ACCOUNT_ID", "test-account")
    monkeypatch.setattr(config, "POST_EXIT_GATE_ENABLED", False)
    monkeypatch.setattr(config, "MC_REGIME_ENABLED", False)
    monkeypatch.setattr(config, "ENABLE_MC_BASKET_EXECUTION", False)
    spec = importlib.util.spec_from_file_location("scheduled_runner_v13_test", RUNNER_PATH)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    # Observational post-exit shadow gate: keep it from touching the network/logs.
    monkeypatch.setattr(runner, "POST_EXIT_SHADOW_MODE", False)
    return runner


def _tagged(records):
    """Stamp canned OANDA payloads with this strategy's tag."""
    return [{"clientExtensions": {"tag": STRATEGY_TAG}, **record} for record in records]


def _candidate():
    return {
        "pair": "GBP_JPY",
        "action": "BUY",
        "bar_time": "2026-09-12T10:00:00+00:00",
        "entry": 204.50,
        "stop_loss": 203.80,
        "take_profit": 206.00,
        "strength_score": 0.2,
        "risk_reward": 2.0,
        "reasoning": "test signal",
    }


def _configure_single_candidate_cycle(monkeypatch, runner, candidate):
    monkeypatch.setattr(runner, "with_retry", lambda fn, **_kwargs: fn())
    monkeypatch.setattr(runner, "analyze_custom_strategy", lambda *_a, **_k: "fake report")
    monkeypatch.setattr(runner, "get_last_signal", lambda: candidate)
    monkeypatch.setattr(runner, "get_latest_mc_local", lambda **_kwargs: None)


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

    first = build_client_extensions(
        signal,
        strategy_tag="jpy_strength",
        bar_time="2026-09-12T10:00:00Z",
    )
    second = build_client_extensions(
        signal,
        strategy_tag="jpy_strength",
        bar_time="2026-09-12T10:00:00Z",
    )

    assert first == second
    assert first["id"].startswith("JPY_STRENGTH_EUR_USD_20260912T100000Z")
    assert first["tag"] == "jpy_strength"
    assert "bar=20260912T100000Z" in first["comment"]


def test_open_trade_or_pending_order_blocks_entry():
    responses = iter(({"trades": []}, {"orders": [{"id": "pending-1"}]}))
    api_client = SimpleNamespace(request=lambda request: next(responses))

    assert has_open_trade_or_order(api_client, "account-1", "EUR_USD") is True


def test_open_trade_on_oanda_makes_scheduler_skip_execution(monkeypatch, capsys):
    """A strategy trade already open at OANDA must block this cycle's entry."""
    runner = _load_scheduler(monkeypatch)
    candidate = _candidate()
    _configure_single_candidate_cycle(monkeypatch, runner, candidate)
    oanda_client = SimpleNamespace(
        request=MagicMock(side_effect=(
            {"trades": _tagged([{"id": "trade-1", "currentUnits": "10000"}])},
            {"orders": []},
        ))
    )
    execute_market_trade = MagicMock()
    monkeypatch.setattr(runner, "oanda_client", oanda_client)
    monkeypatch.setattr(runner, "execute_market_trade", execute_market_trade)

    runner.run_cycle()

    execute_market_trade.assert_not_called()
    assert "BLOCKED by OANDA idempotency guard" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("trades", "orders", "expected_queries"),
    [
        ([{"id": "trade-1", "currentUnits": "10000"}], [], 1),
        ([], [{"id": "order-1"}], 2),
    ],
    ids=["open-trade", "pending-order"],
)
def test_existing_oanda_state_never_calls_execute_market_trade(
    monkeypatch, trades, orders, expected_queries
):
    runner = _load_scheduler(monkeypatch)
    candidate = _candidate()
    _configure_single_candidate_cycle(monkeypatch, runner, candidate)
    oanda_client = SimpleNamespace(
        request=MagicMock(side_effect=({"trades": _tagged(trades)}, {"orders": _tagged(orders)}))
    )
    execute_market_trade = MagicMock()
    monkeypatch.setattr(runner, "oanda_client", oanda_client)
    monkeypatch.setattr(runner, "execute_market_trade", execute_market_trade)

    runner.run_cycle()

    execute_market_trade.assert_not_called()
    assert oanda_client.request.call_count == expected_queries


def test_foreign_open_trade_does_not_block_our_entry(monkeypatch, capsys):
    """A manual / other-strategy position on the pair must not stop this bot
    from entering — the tag is what distinguishes 'ours' from 'not ours'."""
    runner = _load_scheduler(monkeypatch)
    candidate = _candidate()
    _configure_single_candidate_cycle(monkeypatch, runner, candidate)
    oanda_client = SimpleNamespace(
        request=MagicMock(side_effect=(
            {"trades": [{"id": "manual-1", "currentUnits": "10000",
                         "clientExtensions": {"tag": "MANUAL-TRADE"}}]},
            {"orders": []},
        ))
    )
    execute_market_trade = MagicMock(return_value=True)
    monkeypatch.setattr(runner, "oanda_client", oanda_client)
    monkeypatch.setattr(runner, "execute_market_trade", execute_market_trade)
    monkeypatch.setattr(runner, "resolve_and_prepare_entry", lambda *_a, **_k: None)

    runner.run_cycle()

    assert "[IDEMPOTENCY] GBP_JPY BUY allowed" in capsys.readouterr().out
    execute_market_trade.assert_called_once()


def test_duplicate_client_extension_rejection_does_not_retry_order(monkeypatch):
    signal = TradeSignal(
        pair_to_trade="USD_JPY",
        action="BUY",
        confidence_score=0.85,
        stop_loss=149.5,
        take_profit=151.0,
        reasoning="duplicate rejection test",
    )
    price_response = {
        "prices": [{"asks": [{"price": "150.000"}], "bids": [{"price": "149.990"}]}]
    }
    duplicate_error = V20Error(
        400,
        "CLIENT_EXTENSIONS_ID_ALREADY_IN_USE: clientExtensions.id already exists",
    )
    api_client = MagicMock()
    api_client.request.side_effect = [price_response, duplicate_error]
    pricing_module = MagicMock()
    pricing_module.PricingInfo.return_value = "pricing-request"
    orders_module = MagicMock()
    orders_module.OrderCreate.return_value = "order-request"

    def import_module(name):
        return pricing_module if name.endswith("pricing") else orders_module

    monkeypatch.setattr(trading_core, "get_open_position", lambda _pair: None)
    monkeypatch.setattr(trading_core, "oanda_client", api_client)
    monkeypatch.setattr(trading_core.importlib, "import_module", import_module)

    assert trading_core.execute_market_trade(signal) is False
    orders_module.OrderCreate.assert_called_once()
    assert api_client.request.call_count == 2