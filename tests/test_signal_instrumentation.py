import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from utils.signal_instrumentation import (
    classify_slope,
    classify_volatility,
    classify_price_location,
    level_cluster_strength,
    log_signal_observation,
    log_executed_signal,
    log_trade_outcome,
    SlopeClass,
    VolatilityClass,
    ProximityClass,
)


# ---------------------------------------------------------------------------
# classify_slope
# ---------------------------------------------------------------------------

def test_slope_strong_buy():
    r = classify_slope(ema_now=100.5, ema_past=100.0, direction="BUY")
    assert r["label"] == SlopeClass.STRONG.value
    assert r["raw_delta"] == pytest.approx(0.5)


def test_slope_weak_buy():
    # 0.05% move -> below STRONG threshold (0.1%) but above FLAT band (0.01%)
    r = classify_slope(ema_now=100.05, ema_past=100.0, direction="BUY")
    assert r["label"] == SlopeClass.WEAK.value


def test_slope_flat_buy():
    r = classify_slope(ema_now=100.005, ema_past=100.0, direction="BUY")
    assert r["label"] == SlopeClass.FLAT.value


def test_slope_against_buy():
    r = classify_slope(ema_now=99.5, ema_past=100.0, direction="BUY")
    assert r["label"] == SlopeClass.AGAINST.value


def test_slope_sell_is_mirrored():
    # For SELL, EMA falling is STRONG (moving toward signal direction)
    r = classify_slope(ema_now=99.5, ema_past=100.0, direction="SELL")
    assert r["label"] == SlopeClass.STRONG.value
    # For SELL, EMA rising is AGAINST
    r2 = classify_slope(ema_now=100.5, ema_past=100.0, direction="SELL")
    assert r2["label"] == SlopeClass.AGAINST.value


def test_slope_unknown_on_missing_input():
    assert classify_slope(None, 100.0, "BUY")["label"] == SlopeClass.UNKNOWN.value
    assert classify_slope(100.0, None, "BUY")["label"] == SlopeClass.UNKNOWN.value
    assert classify_slope(100.0, 0.0, "BUY")["label"] == SlopeClass.UNKNOWN.value


# ---------------------------------------------------------------------------
# classify_volatility
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "z,expected",
    [
        (None, VolatilityClass.UNKNOWN.value),
        (-2.0, VolatilityClass.COMPRESSED.value),
        (-1.5000001, VolatilityClass.COMPRESSED.value),
        (-1.4, VolatilityClass.NORMAL.value),
        (0.0, VolatilityClass.NORMAL.value),
        (1.0, VolatilityClass.NORMAL.value),      # boundary: not > 1.0
        (1.0001, VolatilityClass.ELEVATED.value),
        (2.0, VolatilityClass.ELEVATED.value),    # boundary: not > 2.0
        (2.0001, VolatilityClass.EXTREME.value),
        (5.0, VolatilityClass.EXTREME.value),
    ],
)
def test_classify_volatility(z, expected):
    assert classify_volatility(z) == expected


# ---------------------------------------------------------------------------
# classify_price_location
# ---------------------------------------------------------------------------

def test_price_location_near():
    r = classify_price_location(entry=100.0, adverse_level=100.1, atr=0.5)
    # distance=0.1, distance_atr=0.2 -> NEAR (< 0.5)
    assert r["label"] == ProximityClass.NEAR.value
    assert r["distance_atr"] == pytest.approx(0.2)


def test_price_location_moderate():
    r = classify_price_location(entry=100.0, adverse_level=100.5, atr=0.5)
    # distance=0.5, distance_atr=1.0 -> MODERATE (0.5 <= x < 1.5)
    assert r["label"] == ProximityClass.MODERATE.value


def test_price_location_clear():
    r = classify_price_location(entry=100.0, adverse_level=102.0, atr=0.5)
    # distance=2.0, distance_atr=4.0 -> CLEAR
    assert r["label"] == ProximityClass.CLEAR.value


def test_price_location_unknown_without_atr():
    r = classify_price_location(entry=100.0, adverse_level=100.1, atr=None)
    assert r["label"] == ProximityClass.UNKNOWN.value
    assert r["distance_raw"] == pytest.approx(0.1)


def test_price_location_unknown_on_missing_input():
    assert classify_price_location(None, 100.0)["label"] == ProximityClass.UNKNOWN.value
    assert classify_price_location(100.0, None)["label"] == ProximityClass.UNKNOWN.value


def test_price_location_pips_reported_independently_of_atr():
    r = classify_price_location(entry=100.00, adverse_level=100.10, atr=0.5, pip_size=0.01)
    assert r["distance_pips"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# level_cluster_strength
# ---------------------------------------------------------------------------

def test_level_cluster_strength_counts_within_tolerance():
    levels = [100.0, 100.02, 100.5, 105.0]
    assert level_cluster_strength(levels, target_level=100.0, tolerance=0.05) == 2
    assert level_cluster_strength(levels, target_level=105.0, tolerance=0.05) == 1


def test_level_cluster_strength_empty_inputs():
    assert level_cluster_strength([], 100.0, 0.05) == 0
    assert level_cluster_strength(None, 100.0, 0.05) == 0
    assert level_cluster_strength([100.0], None, 0.05) == 0


# ---------------------------------------------------------------------------
# Logging (uses env-var-overridden paths into a temp dir)
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_log_paths(tmp_path, monkeypatch):
    sig_path = str(tmp_path / "signals.jsonl")
    outcome_path = str(tmp_path / "outcomes.jsonl")
    monkeypatch.setattr(
        "utils.signal_instrumentation.SIGNAL_OBSERVATION_LOG_PATH", sig_path
    )
    monkeypatch.setattr(
        "utils.signal_instrumentation.TRADE_OUTCOME_LOG_PATH", outcome_path
    )
    return sig_path, outcome_path


def test_log_signal_observation_writes_valid_json_line(temp_log_paths):
    sig_path, _ = temp_log_paths
    ok = log_signal_observation(
        cycle_id="2026-08-14T00:00:00Z",
        pair="USD_JPY",
        stage="alignment",
        direction="BUY",
        strength_score=0.05,
        slope={"label": "STRONG"},
    )
    assert ok is True
    with open(sig_path) as f:
        lines = f.readlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["log_type"] == "signal_observation"
    assert record["pair"] == "USD_JPY"
    assert record["stage"] == "alignment"
    assert record["slope"]["label"] == "STRONG"


def test_log_executed_signal_writes_valid_json_line(temp_log_paths):
    sig_path, _ = temp_log_paths
    ok = log_executed_signal(
        cycle_id="cyc1", pair="EUR_JPY", direction="SELL", diagnostics={"foo": "bar"}
    )
    assert ok is True
    record = json.loads(open(sig_path).readline())
    assert record["log_type"] == "signal_executed"
    assert record["diagnostics"] == {"foo": "bar"}


def test_log_trade_outcome_writes_valid_json_line(temp_log_paths):
    _, outcome_path = temp_log_paths
    ok = log_trade_outcome(
        instrument="GBP_JPY",
        direction=1,
        entry_price_0=195.0,
        r_unit_0=0.5,
        close_price=195.6,
        close_price_source="cycle_evaluation_price",
        realized_r=1.2,
        close_reason="closed_by_own_risk_action",
        final_state="CLOSED",
    )
    assert ok is True
    record = json.loads(open(outcome_path).readline())
    assert record["log_type"] == "trade_outcome"
    assert record["realized_r"] == 1.2
    assert record["close_price_source"] == "cycle_evaluation_price"


def test_multiple_log_calls_append_dont_overwrite(temp_log_paths):
    sig_path, _ = temp_log_paths
    log_signal_observation(cycle_id="c1", pair="USD_JPY", stage="alignment")
    log_signal_observation(cycle_id="c1", pair="EUR_JPY", stage="alignment")
    with open(sig_path) as f:
        lines = f.readlines()
    assert len(lines) == 2


def test_logging_failure_never_raises(monkeypatch, tmp_path):
    # Point the log at a path where the "directory" is actually a file,
    # guaranteeing os.makedirs()/open() fail -- must return False, not raise.
    blocking_file = tmp_path / "not_a_dir"
    blocking_file.write_text("x")
    bad_path = str(blocking_file / "sub" / "log.jsonl")
    monkeypatch.setattr("utils.signal_instrumentation.SIGNAL_OBSERVATION_LOG_PATH", bad_path)
    ok = log_signal_observation(cycle_id="c1", pair="USD_JPY", stage="alignment")
    assert ok is False  # failed, but did not raise
