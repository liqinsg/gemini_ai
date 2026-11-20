from utils.prob_zone_sizing import combine_size_caps
from utils.scale_out import (
    advance_stage,
    complete_stage_trade,
    get_stage_index,
    load_stage_state,
    register_stage_trade,
    save_stage_state,
)
from utils.oanda_state import closed_trade_was_take_profit


def test_independent_caps_use_minimum_not_product():
    total, caps = combine_size_caps(
        10_000,
        {"D_MC": 5_000, "W_MC": 5_000, "W_structure": 7_500},
    )
    assert total == 5_000
    assert caps == {
        "stage": 10_000,
        "D_MC": 5_000,
        "W_MC": 5_000,
        "W_structure": 7_500,
    }


def test_stage_advances_once_only_after_confirmed_tp(tmp_path):
    state_path = tmp_path / "stage.json"
    state = load_stage_state(state_path)
    assert register_stage_trade(state, "EUR_USD", "T-1", "BUY", 1.2)
    assert save_stage_state(state, state_path)

    state = load_stage_state(state_path)
    assert complete_stage_trade(state, "T-1", take_profit_confirmed=True) == 1
    assert get_stage_index(state, "EUR_USD") == 1
    assert save_stage_state(state, state_path)

    state = load_stage_state(state_path)
    assert complete_stage_trade(state, "T-1", take_profit_confirmed=True) is None
    assert get_stage_index(state, "EUR_USD") == 1


def test_non_tp_close_does_not_advance_stage():
    state = {}
    register_stage_trade(state, "EUR_USD", "T-2", "SELL", 1.1)
    assert complete_stage_trade(state, "T-2", take_profit_confirmed=False) is None
    assert get_stage_index(state, "EUR_USD") == 0


class _TransactionClient:
    def request(self, request):
        return {
            "transaction": {
                "type": "TAKE_PROFIT_ORDER",
                "tradeID": "T-3",
            }
        }


def test_closed_trade_requires_matching_take_profit_transaction():
    trade = {
        "id": "T-3",
        "closingTransactionIDs": ["100"],
    }
    assert closed_trade_was_take_profit(_TransactionClient(), "account", trade)

    trade["id"] = "T-other"
    assert not closed_trade_was_take_profit(_TransactionClient(), "account", trade)
