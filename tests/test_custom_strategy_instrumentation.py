"""
Behavioral-equivalence tests for the V2 Section 4.1 instrumentation patch
applied to custom_strategy_v1.py.

Strategy: mock every external data source (get_live_prices, get_support_resistance
already patched via strategy_helpers/find_support_resistence mocks,
check_ma5_alignment, get_atr_with_volatility_context) so generate_signals()
runs entirely deterministically, then assert:
  1. The exact same trade is selected as would be selected by the
     unmodified decision logic (computed independently in this test file).
  2. The returned signal dict has EXACTLY the same keys as before
     instrumentation (no new keys leaked into what feeds TradeSignal()).
  3. Instrumentation logging occurred (population + executed-signal logs
     were written) without altering the above.
  4. A logging failure inside the instrumentation blocks does not prevent
     a valid trade from being returned.
"""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

import custom_strategy_v1 as cs


EXPECTED_SIGNAL_KEYS = {
    "pair", "action", "entry", "stop_loss", "take_profit",
    "strength_score", "risk_reward", "reasoning",
}


@pytest.fixture
def temp_signal_log(tmp_path, monkeypatch):
    path = str(tmp_path / "signals.jsonl")
    monkeypatch.setattr("utils.signal_instrumentation.SIGNAL_OBSERVATION_LOG_PATH", path)
    return path


def _base_mocks(monkeypatch):
    """
    Sets up a single, deterministic scenario: USD_JPY is the only pair
    with a large enough strength gap and full BUY alignment; every other
    pair is deliberately given a near-zero strength score so it's cut by
    the strength-gap filter early (keeps the scenario simple and
    unambiguous about which pair MUST win).
    """
    strategy = cs.JPYTrendStrategy(trade_pairs=["USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY"])

    scores = {"USD": 0.20, "JPY": 0.0, "EUR": 0.0, "GBP": 0.0, "AUD": 0.0}
    # jpy_strength_rank: USD_JPY -> 0.20, others -> 0.0 (below any positive cutoff)

    monkeypatch.setattr(cs, "check_ma5_alignment", lambda pair, require_aligned: "BUY" if pair == "USD_JPY" else None)
    monkeypatch.setattr(cs, "get_live_prices", lambda pair: {"ask": 150.100, "bid": 150.080})
    monkeypatch.setattr(
        cs, "get_support_resistance",
        lambda pair, granularity, count, window, return_all_levels=False: (
            {
                "support": 149.000, "resistance": 151.000, "current_price": 150.090,
                **({"all_supports": [149.000, 148.500], "all_resistances": [151.000, 151.800]}
                   if return_all_levels else {}),
            }
        ),
    )
    monkeypatch.setattr(cs, "get_atr_with_volatility_context", lambda pair, period, lookback: (0.300, 0.5))
    monkeypatch.setattr(
        cs, "get_slope_diagnostics",
        lambda pair, timeframes, direction: {
            "k": 3, "ema_period": 5,
            "per_timeframe": {"H4": {"label": "STRONG"}},
            "combined_label": "STRONG", "against_count": 0,
        },
    )

    return strategy, scores


def test_selected_trade_unchanged_by_instrumentation(monkeypatch, temp_signal_log):
    strategy, scores = _base_mocks(monkeypatch)

    signals = strategy.generate_signals(scores)

    assert len(signals) == 1
    top = signals[0]
    assert top["pair"] == "USD_JPY"
    assert top["action"] == "BUY"

    # Hand-computed expectation, independent of the instrumentation code path:
    # ATR path: sl_multiplier = NORMAL (z_score=0.5 is between -1 and 1) = 2.2
    # sl_distance = 0.300 * 2.2 = 0.66 ; tp_distance = 0.66 * 2.0 = 1.32
    # entry = ask = 150.100 (BUY)
    expected_entry = 150.100
    expected_sl = round(150.100 - 0.66, 3)
    expected_tp = round(150.100 + 1.32, 3)
    assert top["entry"] == pytest.approx(expected_entry)
    assert top["stop_loss"] == pytest.approx(expected_sl)
    assert top["take_profit"] == pytest.approx(expected_tp)

    expected_rr = round(abs(expected_tp - expected_entry) / abs(expected_entry - expected_sl), 2)
    assert top["risk_reward"] == pytest.approx(expected_rr)


def test_returned_signal_dict_has_unchanged_keys(monkeypatch, temp_signal_log):
    """
    Critical safety check: instrumentation must NOT leak new keys into the
    dict that scheduled_runner_v1.3.py reads from and that (in the
    non-dynamic-risk-manager path) could theoretically be unpacked into
    TradeSignal(). Exactly the original 8 keys, nothing more.
    """
    strategy, scores = _base_mocks(monkeypatch)
    signals = strategy.generate_signals(scores)
    assert set(signals[0].keys()) == EXPECTED_SIGNAL_KEYS


def test_instrumentation_logs_were_written(monkeypatch, temp_signal_log):
    strategy, scores = _base_mocks(monkeypatch)
    strategy.generate_signals(scores)

    with open(temp_signal_log) as f:
        lines = [json.loads(l) for l in f.readlines()]

    log_types = [l["log_type"] for l in lines]
    assert "signal_observation" in log_types
    assert "signal_executed" in log_types

    executed = [l for l in lines if l["log_type"] == "signal_executed"][0]
    assert executed["pair"] == "USD_JPY"
    assert executed["direction"] == "BUY"
    assert "slope" in executed["diagnostics"]
    assert "volatility" in executed["diagnostics"]
    assert "price_location" in executed["diagnostics"]
    assert executed["diagnostics"]["price_location"]["level_cluster_strength"] >= 1


def test_trade_still_returned_if_slope_diagnostics_raises(monkeypatch, temp_signal_log):
    """
    If get_slope_diagnostics itself somehow raises (bypassing its own
    internal per-timeframe try/except), the outer try/except in
    generate_signals() must still allow the trade through unaffected.
    """
    strategy, scores = _base_mocks(monkeypatch)

    def broken_slope_diagnostics(*a, **kw):
        raise RuntimeError("simulated instrumentation bug")

    monkeypatch.setattr(cs, "get_slope_diagnostics", broken_slope_diagnostics)

    signals = strategy.generate_signals(scores)
    assert len(signals) == 1
    assert signals[0]["pair"] == "USD_JPY"
    assert signals[0]["action"] == "BUY"


def test_trade_still_returned_if_log_signal_observation_raises(monkeypatch, temp_signal_log):
    strategy, scores = _base_mocks(monkeypatch)

    def broken_log(*a, **kw):
        raise RuntimeError("simulated logging backend failure")

    monkeypatch.setattr(cs, "log_signal_observation", broken_log)
    monkeypatch.setattr(cs, "log_executed_signal", broken_log)

    signals = strategy.generate_signals(scores)
    assert len(signals) == 1
    assert signals[0]["pair"] == "USD_JPY"


def test_no_valid_signal_case_unaffected(monkeypatch, temp_signal_log):
    """When nothing qualifies, behavior (empty list) must be unchanged."""
    strategy, scores = _base_mocks(monkeypatch)
    monkeypatch.setattr(cs, "check_ma5_alignment", lambda pair, require_aligned: None)

    signals = strategy.generate_signals(scores)
    assert signals == []
