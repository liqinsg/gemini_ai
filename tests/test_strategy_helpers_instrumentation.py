import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from unittest.mock import patch

import utils.strategy_helpers as sh
import utils.find_support_resistence as fsr


def _candles(closes):
    """Build minimal fake OANDA candle dicts from a list of close prices."""
    return [{"mid": {"c": str(c), "h": str(c), "l": str(c)}, "complete": True} for c in closes]


# ---------------------------------------------------------------------------
# _ema_series
# ---------------------------------------------------------------------------

def test_ema_series_last_value_matches_ema():
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    series = sh._ema_series(values, period=5)
    assert series[-1] == pytest.approx(sh._ema(values, period=5))


def test_ema_series_empty_if_insufficient_data():
    assert sh._ema_series([1, 2], period=5) == []


def test_ema_series_length():
    values = list(range(1, 11))  # 10 values, period 5 -> 6 EMA points (indices 4..9)
    series = sh._ema_series(values, period=5)
    assert len(series) == 6


# ---------------------------------------------------------------------------
# get_slope_diagnostics -- pure diagnostic, must never affect check_ma5_alignment
# ---------------------------------------------------------------------------

def test_get_slope_diagnostics_strong_uptrend():
    rising_closes = [100 + i * 0.5 for i in range(10)]  # steadily rising

    with patch.object(sh, "get_candles", return_value=_candles(rising_closes)):
        result = sh.get_slope_diagnostics("USD_JPY", ["H4"], direction="BUY", k=3)

    assert result["per_timeframe"]["H4"]["label"] == "STRONG"
    assert result["against_count"] == 0
    assert result["combined_label"] == "STRONG"


def test_get_slope_diagnostics_against_for_sell_in_uptrend():
    rising_closes = [100 + i * 0.5 for i in range(10)]

    with patch.object(sh, "get_candles", return_value=_candles(rising_closes)):
        result = sh.get_slope_diagnostics("USD_JPY", ["H4"], direction="SELL", k=3)

    assert result["per_timeframe"]["H4"]["label"] == "AGAINST"
    assert result["against_count"] == 1


def test_get_slope_diagnostics_unknown_on_insufficient_candles():
    with patch.object(sh, "get_candles", return_value=_candles([100, 101])):
        result = sh.get_slope_diagnostics("USD_JPY", ["H4"], direction="BUY", k=3)
    assert result["per_timeframe"]["H4"]["label"] == "UNKNOWN"
    assert result["combined_label"] == "UNKNOWN"


def test_get_slope_diagnostics_never_raises_on_candle_fetch_error():
    def broken_get_candles(*a, **kw):
        raise RuntimeError("simulated OANDA failure")

    with patch.object(sh, "get_candles", side_effect=broken_get_candles):
        result = sh.get_slope_diagnostics("USD_JPY", ["H4", "H1"], direction="BUY", k=3)

    assert result["per_timeframe"]["H4"]["label"] == "UNKNOWN"
    assert result["per_timeframe"]["H1"]["label"] == "UNKNOWN"
    assert "error" in result["per_timeframe"]["H4"]


def test_get_slope_diagnostics_multi_timeframe_combined_label():
    rising_closes = [100 + i * 0.5 for i in range(10)]
    flat_closes = [100.0] * 10

    def fake_get_candles(instrument, tf, count):
        return _candles(rising_closes) if tf == "H4" else _candles(flat_closes)

    with patch.object(sh, "get_candles", side_effect=fake_get_candles):
        result = sh.get_slope_diagnostics("USD_JPY", ["H4", "H1"], direction="BUY", k=3)

    assert result["per_timeframe"]["H4"]["label"] == "STRONG"
    assert result["per_timeframe"]["H1"]["label"] == "FLAT"


def test_check_ma5_alignment_unaffected_by_new_functions():
    """
    Regression guard: check_ma5_alignment's own decision logic (the ONLY
    function whose output feeds the live entry decision) must be byte-for-
    byte behaviorally identical to before -- unaffected by the presence of
    _ema_series / get_slope_diagnostics elsewhere in the same module.
    """
    rising_closes = [100 + i * 0.5 for i in range(10)]
    with patch.object(sh, "get_candles", return_value=_candles(rising_closes)):
        direction = sh.check_ma5_alignment("USD_JPY", require_aligned=1)
    assert direction == "BUY"


# ---------------------------------------------------------------------------
# get_support_resistance's additive return_all_levels param
# ---------------------------------------------------------------------------

def _sr_candles(pattern):
    """pattern: list of (low, high, close) triples."""
    return [
        {"mid": {"l": str(l), "h": str(h), "c": str(c)}, "complete": True}
        for (l, h, c) in pattern
    ]


def _sample_sr_candles(n=20):
    # Oscillating pattern so the fractal scan finds multiple swing points.
    pattern = []
    for i in range(n):
        base = 100 + (i % 5)
        pattern.append((base - 0.5, base + 0.5, base))
    return _sr_candles(pattern)


def test_get_support_resistance_default_unchanged_shape():
    candles = _sample_sr_candles()
    with patch.object(fsr, "get_candles", return_value=candles):
        result = fsr.get_support_resistance("USD_JPY", "D", count=20, window=2)
    assert set(result.keys()) == {"support", "resistance", "current_price"}


def test_get_support_resistance_return_all_levels_adds_keys_only():
    candles = _sample_sr_candles()
    with patch.object(fsr, "get_candles", return_value=candles):
        default_result = fsr.get_support_resistance("USD_JPY", "D", count=20, window=2)
        extended_result = fsr.get_support_resistance(
            "USD_JPY", "D", count=20, window=2, return_all_levels=True
        )

    # The three original keys must have IDENTICAL values in both calls.
    for key in ("support", "resistance", "current_price"):
        assert default_result[key] == extended_result[key]

    assert set(extended_result.keys()) == {
        "support", "resistance", "current_price", "all_supports", "all_resistances"
    }
    assert isinstance(extended_result["all_supports"], list)
    assert isinstance(extended_result["all_resistances"], list)


def test_get_support_resistance_empty_candles_return_all_levels():
    with patch.object(fsr, "get_candles", return_value=[]):
        result = fsr.get_support_resistance(
            "USD_JPY", "D", count=20, window=2, return_all_levels=True
        )
    assert result == {
        "support": None, "resistance": None, "current_price": None,
        "all_supports": [], "all_resistances": [],
    }


def test_get_support_resistance_empty_candles_default_unchanged():
    with patch.object(fsr, "get_candles", return_value=[]):
        result = fsr.get_support_resistance("USD_JPY", "D", count=20, window=2)
    assert result == {"support": None, "resistance": None, "current_price": None}
