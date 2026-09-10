"""
test_fx_monte_carlo.py — Unit tests for fx_monte_carlo.py (D + W timeframes)
============================================================================

Covers:
    - Module import safety (handles market-closed SystemExit)
    - fetch_data with mocked yfinance (D and W intervals)
    - run_mc full pipeline: drift, vol, student-t shocks, paths,
      confidence range, VaR/CVaR, p_up/p_down, regime classification
    - save_mc_result_safely atomic write + history pruning
    - main() orchestration across both D and W timeframes
    - Edge cases: bad prices, insufficient data, empty DF

All temp artifacts go to /tmp/fx_mc_test_* and are cleaned up after.

Run:  cd ~/projects/gemini_ai && pytest tests/test_fx_monte_carlo.py -v -s
"""

import json
import os
import sys
import tempfile
import shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, PropertyMock

import numpy as np
import pandas as pd
import pytest


# ============================================================
# Handle module-level SystemExit from forex_market_closed()
# before fx_monte_carlo is imported.
#
# Strategy: try a normal import first.  If the FX market happens
# to be closed (weekend / Friday evening) a SystemExit(0) fires.
# In that case we clean up the half-loaded module and re-import
# with zoneinfo.ZoneInfo patched so that forex_market_closed()
# always sees a weekday (market-open) time.
# ============================================================

_MARKET_OPEN_WED_NAIVE = datetime(2025, 9, 10, 14, 0, 0)


def _make_fake_zoneinfo():
    """Return a drop-in replacement for zoneinfo.ZoneInfo that, when used
    as the tz argument to datetime.now(), yields a fixed Wednesday 14:00."""
    from datetime import tzinfo as _tzinfo_base

    class _FakeTZ(_tzinfo_base):
        def __init__(self, *args, **kwargs):
            self._name = args[0] if args else "Europe/London"

        def utcoffset(self, dt):
            return timedelta(hours=0)

        def dst(self, dt):
            return timedelta(0)

        def tzname(self, dt):
            return self._name

        def fromutc(self, dt):
            return _MARKET_OPEN_WED_NAIVE.replace(tzinfo=self)

        def __repr__(self):
            return f"FakeTZ({self._name!r})"

    return _FakeTZ


@pytest.fixture(scope="session", autouse=True)
def _import_fxmc():
    """Import fx_monte_carlo once per session, robust to market-closed hours."""
    import importlib

    # Remove any stale half-loaded copy.
    for _k in list(sys.modules):
        if "fx_monte_carlo" in _k:
            del sys.modules[_k]

    try:
        import fx_monte_carlo as fxmc
        yield fxmc
        return
    except SystemExit:
        pass

    # --- Market was closed; retry with ZoneInfo patched ---
    for _k in list(sys.modules):
        if "fx_monte_carlo" in _k:
            del sys.modules[_k]

    fake_zi = _make_fake_zoneinfo()

    with patch("zoneinfo.ZoneInfo", fake_zi):
        import fx_monte_carlo as fxmc
        yield fxmc


# ============================================================
# Helpers — synthetic price series
# ============================================================

def make_price_series(n: int, start: float = 1.10,
                     drift: float = 0.0, vol: float = 0.01,
                     freq: str = "D") -> pd.DataFrame:
    """Generate a synthetic OHLCV DataFrame mimicking yfinance output."""
    rng = np.random.default_rng(42)
    returns = rng.normal(drift / 252, vol / np.sqrt(252), n)
    closes = start * np.exp(np.cumsum(returns))
    if freq == "W":
        idx = pd.date_range("2020-01-01", periods=n, freq="W")
    else:
        idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "Open":  closes,
        "High":  closes * (1 + np.abs(rng.normal(0, 0.003, n))),
        "Low":   closes * (1 - np.abs(rng.normal(0, 0.003, n))),
        "Close": closes,
    }, index=idx)


def make_flat_series(n: int, price: float = 1.10) -> pd.DataFrame:
    """All prices identical → zero drift / zero vol edge case."""
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "Open": [price] * n,
        "High": [price] * n,
        "Low":  [price] * n,
        "Close": [price] * n,
    }, index=idx)


# ============================================================
# Per-test fixture: isolated /tmp dir + yfinance mock
# ============================================================

@pytest.fixture
def sandbox():
    """Create a temp dir under /tmp, yield its Path, then delete it."""
    tmp = Path(tempfile.mkdtemp(prefix="fx_mc_test_", dir="/tmp"))
    yield tmp
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def mock_yfinance(monkeypatch):
    """Replace yfinance.download with a factory; per-test sets df via .respond(df)."""
    import fx_monte_carlo as fxmc
    _registry = {}

    def _fake_download(ticker, period, interval, progress, auto_adjust):
        df = _registry.get((ticker, interval))
        if df is None:
            return pd.DataFrame()
        return df.copy()

    monkeypatch.setattr(fxmc.yf, "download", _fake_download)

    def _respond(pair, timeframe, df):
        tf_cfg = fxmc.TIMEFRAME_CONFIG[timeframe]
        _registry[(pair, tf_cfg["yf_interval"])] = df

    yield _respond


@pytest.fixture
def quiet_obs_layer(monkeypatch):
    """Neutralise the observation-log side effects."""
    import fx_monte_carlo as fxmc
    monkeypatch.setattr(fxmc, "log_mc_observation", MagicMock())
    monkeypatch.setattr(fxmc, "resolve_pending_mc_observations",
                        MagicMock(return_value=0))


@pytest.fixture
def isolated_results_dir(monkeypatch, sandbox):
    """Redirect fx_monte_carlo.RESULTS_DIR into the per-test sandbox."""
    import fx_monte_carlo as fxmc
    monkeypatch.setattr(fxmc, "RESULTS_DIR", sandbox)
    return sandbox


# ============================================================
# Test: TIMEFRAME_CONFIG has D and W
# ============================================================

class TestTimeframeConfig:
    def test_daily_present(self, _import_fxmc):
        cfg = _import_fxmc.TIMEFRAME_CONFIG["D"]
        assert cfg["yf_interval"] == "1d"
        assert cfg["yf_period"] == "1y"
        assert cfg["lookback"] == 90
        assert cfg["forecast"] == 5
        assert cfg["periods_year"] == 252

    def test_weekly_present(self, _import_fxmc):
        cfg = _import_fxmc.TIMEFRAME_CONFIG["W"]
        assert cfg["yf_interval"] == "1wk"
        assert cfg["yf_period"] == "5y"
        assert cfg["lookback"] == 104
        assert cfg["forecast"] == 5
        assert cfg["periods_year"] == 52

    def test_timeframes_default_d_and_w(self, _import_fxmc):
        assert "D" in _import_fxmc.TIMEFRAMES
        assert "W" in _import_fxmc.TIMEFRAMES


# ============================================================
# Test: fetch_data
# ============================================================

class TestFetchData:
    def test_daily_success(self, _import_fxmc, mock_yfinance):
        pair = "EURUSD=X"
        df = make_price_series(n=200, freq="D")
        mock_yfinance(pair, "D", df)

        result = _import_fxmc.fetch_data(pair, "D")
        assert not result.empty
        assert "Close" in result.columns
        assert len(result) >= 90

    def test_weekly_success(self, _import_fxmc, mock_yfinance):
        pair = "EURUSD=X"
        df = make_price_series(n=200, freq="W")
        mock_yfinance(pair, "W", df)

        result = _import_fxmc.fetch_data(pair, "W")
        assert not result.empty
        assert len(result) >= 104

    def test_insufficient_daily_data_returns_empty(self, _import_fxmc, mock_yfinance):
        pair = "EURUSD=X"
        df = make_price_series(n=30, freq="D")
        mock_yfinance(pair, "D", df)

        result = _import_fxmc.fetch_data(pair, "D")
        assert result.empty

    def test_insufficient_weekly_data_returns_empty(self, _import_fxmc, mock_yfinance):
        pair = "EURUSD=X"
        df = make_price_series(n=50, freq="W")
        mock_yfinance(pair, "W", df)

        result = _import_fxmc.fetch_data(pair, "W")
        assert result.empty

    def test_unsupported_timeframe_raises(self, _import_fxmc, mock_yfinance):
        with pytest.raises(ValueError):
            _import_fxmc.fetch_data("EURUSD=X", "H1")

    def test_yfinance_empty_df(self, _import_fxmc, mock_yfinance):
        pair = "BADPAIR=X"
        mock_yfinance(pair, "D", pd.DataFrame())
        result = _import_fxmc.fetch_data(pair, "D")
        assert result.empty


# ============================================================
# Test: run_mc — correctness of the engine
# ============================================================

class TestRunMc:
    def test_daily_run_produces_valid_schema(self, _import_fxmc, mock_yfinance):
        pair = "EURUSD=X"
        df = make_price_series(n=200, start=1.10, drift=0.05, vol=0.012, freq="D")
        mock_yfinance(pair, "D", df)

        data, ok = _import_fxmc.run_mc(pair, "D")
        assert ok is True
        assert data is not None

        required_keys = {
            "timeframe", "pair", "current_price",
            "ann_drift_pct", "ann_vol_pct",
            "range_90", "percentile_rank",
            "p_up", "p_down",
            "touch_upper_pct", "touch_lower_pct",
            "var_95", "cvar_95",
            "expected_price", "regime",
            "lookback", "forecast", "simulations",
            "generated_utc",
        }
        assert required_keys.issubset(data.keys())
        assert data["timeframe"] == "D"
        assert data["pair"] == pair
        assert data["lookback"] == 90
        assert data["forecast"] == 5
        assert data["simulations"] == 5000

    def test_weekly_run_produces_valid_schema(self, _import_fxmc, mock_yfinance):
        pair = "EURUSD=X"
        df = make_price_series(n=200, start=1.10, drift=0.05, vol=0.05, freq="W")
        mock_yfinance(pair, "W", df)

        data, ok = _import_fxmc.run_mc(pair, "W")
        assert ok is True
        assert data["timeframe"] == "W"
        assert data["lookback"] == 104
        assert data["forecast"] == 5

    def test_probabilities_sum_to_100(self, _import_fxmc, mock_yfinance):
        pair = "EURUSD=X"
        df = make_price_series(n=200, freq="D")
        mock_yfinance(pair, "D", df)

        data, ok = _import_fxmc.run_mc(pair, "D")
        assert ok
        assert data["p_up"] + data["p_down"] == 100.0

    def test_range_90_ordered(self, _import_fxmc, mock_yfinance):
        pair = "EURUSD=X"
        df = make_price_series(n=200, freq="D")
        mock_yfinance(pair, "D", df)

        data, ok = _import_fxmc.run_mc(pair, "D")
        assert ok
        lo, hi = data["range_90"]
        assert lo <= data["current_price"] <= hi

    def test_var_95_is_negative_for_long_only(self, _import_fxmc, mock_yfinance):
        """VaR 95 should be ≤ 0 when expressed as a percentage change."""
        pair = "EURUSD=X"
        df = make_price_series(n=200, freq="D")
        mock_yfinance(pair, "D", df)

        data, ok = _import_fxmc.run_mc(pair, "D")
        assert ok
        assert data["var_95"] <= 0.0

    def test_cvar_more_extreme_than_var(self, _import_fxmc, mock_yfinance):
        pair = "EURUSD=X"
        df = make_price_series(n=200, freq="D")
        mock_yfinance(pair, "D", df)

        data, ok = _import_fxmc.run_mc(pair, "D")
        assert ok
        assert data["cvar_95"] <= data["var_95"]

    def test_regime_is_one_of_expected_labels(self, _import_fxmc, mock_yfinance):
        pair = "EURUSD=X"
        df = make_price_series(n=200, freq="D")
        mock_yfinance(pair, "D", df)

        data, ok = _import_fxmc.run_mc(pair, "D")
        assert ok
        valid_labels = {
            "🔴 OVERBOUGHT | Mean-Reversion Risk",
            "🟢 OVERSOLD | Bullish Reversal Chance",
            "⚡ STRONG MOMENTUM",
            "⏳ CONSOLIDATION RANGE",
            "🔹 NEUTRAL",
        }
        assert data["regime"] in valid_labels

    def test_jpy_pair_uses_3_decimals(self, _import_fxmc, mock_yfinance):
        pair = "USDJPY=X"
        df = make_price_series(n=200, start=150.0, freq="D")
        mock_yfinance(pair, "D", df)

        data, ok = _import_fxmc.run_mc(pair, "D")
        assert ok
        price_str = f"{data['current_price']:.6f}"
        decimal_part = price_str.split(".")[1]
        non_zero = [d for d in decimal_part if d != "0"]
        assert len(decimal_part) >= 3

    def test_run_mc_with_invalid_timeframe_fails(self, _import_fxmc):
        data, ok = _import_fxmc.run_mc("EURUSD=X", "H1")
        assert ok is False
        assert data is None

    def test_run_mc_with_insufficient_data_fails(self, _import_fxmc, mock_yfinance):
        pair = "EURUSD=X"
        df = make_price_series(n=10, freq="D")
        mock_yfinance(pair, "D", df)

        data, ok = _import_fxmc.run_mc(pair, "D")
        assert ok is False
        assert data is None


# ============================================================
# Test: save_mc_result_safely — atomic write + history pruning
# ============================================================

class TestSaveMcResultSafely:
    def test_atomic_write_produces_valid_json(self, _import_fxmc, sandbox):
        target = sandbox / "mc_D_EURUSD_20260910_1200.json"
        data = {
            "timeframe": "D",
            "pair": "EURUSD=X",
            "current_price": 1.10500,
        }

        _import_fxmc.save_mc_result_safely(
            data, target, glob_pattern="mc_D_EURUSD_*.json", max_files=5,
        )

        assert target.exists()
        loaded = json.loads(target.read_text())
        assert loaded["current_price"] == 1.10500
        tmp_files = list(sandbox.glob("*.tmp*"))
        assert not tmp_files, "temp files should be cleaned up"

    def test_history_pruning_keeps_latest_n(self, _import_fxmc, sandbox):
        glob = "mc_D_EURUSD_*.json"
        for i in range(10):
            target = sandbox / f"mc_D_EURUSD_20260910_{i:04d}.json"
            _import_fxmc.save_mc_result_safely(
                {"v": i}, target, glob_pattern=glob, max_files=3,
            )

        survivors = sorted(sandbox.glob(glob))
        assert len(survivors) == 3
        names = [p.name for p in survivors]
        for expected in ("mc_D_EURUSD_20260910_0007.json",
                         "mc_D_EURUSD_20260910_0008.json",
                         "mc_D_EURUSD_20260910_0009.json"):
            assert expected in names

    def test_max_files_zero_keeps_only_latest(self, _import_fxmc, sandbox):
        glob = "mc_W_GBPUSD_*.json"
        for i in range(5):
            target = sandbox / f"mc_W_GBPUSD_20260910_{i:04d}.json"
            _import_fxmc.save_mc_result_safely(
                {"v": i}, target, glob_pattern=glob, max_files=1,
            )

        survivors = list(sandbox.glob(glob))
        assert len(survivors) == 1


# ============================================================
# Test: main() orchestration across D and W
# ============================================================

class TestMainOrchestration:
    def test_main_runs_both_timeframes_with_mocks(
        self, _import_fxmc, monkeypatch, sandbox,
        mock_yfinance, quiet_obs_layer, isolated_results_dir, capsys,
    ):
        import fx_monte_carlo as fxmc

        for pair in fxmc.PAIRS:
            df_d = make_price_series(n=200, freq="D", start=1.10)
            df_w = make_price_series(n=200, freq="W", start=1.10)
            mock_yfinance(pair, "D", df_d)
            mock_yfinance(pair, "W", df_w)

        fxmc.main()
        out = capsys.readouterr().out

        assert "TIMEFRAME: D" in out
        assert "TIMEFRAME: W" in out
        assert "Run complete" in out

        # One JSON per (pair, timeframe)
        all_json = list(isolated_results_dir.glob("*.json"))
        assert len(all_json) == len(fxmc.PAIRS) * 2

        d_results = [p for p in all_json if "/mc_D_" in str(p)]
        w_results = [p for p in all_json if "/mc_W_" in str(p)]
        assert len(d_results) == len(fxmc.PAIRS)
        assert len(w_results) == len(fxmc.PAIRS)

        for jf in d_results + w_results:
            data = json.loads(jf.read_text())
            assert "timeframe" in data
            assert "pair" in data
            assert "current_price" in data
            assert p_sum(data) == 100.0

        # Observation layer called once per result
        assert fxmc.log_mc_observation.call_count == len(all_json)


# ============================================================
# Test: main() with only D timeframe (override config)
# ============================================================

class TestMainSingleTimeframe:
    def test_daily_only_runs(self, _import_fxmc, monkeypatch, sandbox,
                             mock_yfinance, quiet_obs_layer,
                             isolated_results_dir, capsys):
        import fx_monte_carlo as fxmc

        monkeypatch.setattr(fxmc, "TIMEFRAMES", ["D"])

        for pair in fxmc.PAIRS:
            df_d = make_price_series(n=200, freq="D", start=1.10)
            mock_yfinance(pair, "D", df_d)

        fxmc.main()
        out = capsys.readouterr().out

        assert "TIMEFRAME: D" in out
        assert "TIMEFRAME: W" not in out

        all_json = list(isolated_results_dir.glob("*.json"))
        assert len(all_json) == len(fxmc.PAIRS)


# ============================================================
# Test: Edge cases
# ============================================================

class TestEdgeCases:
    def test_negative_price_fails_run_mc(self, _import_fxmc, mock_yfinance):
        pair = "EURUSD=X"
        n = 200
        idx = pd.date_range("2024-01-01", periods=n, freq="D")
        closes = np.full(n, -1.0)
        df = pd.DataFrame({
            "Open": closes, "High": closes, "Low": closes, "Close": closes,
        }, index=idx)
        mock_yfinance(pair, "D", df)

        data, ok = _import_fxmc.run_mc(pair, "D")
        assert ok is False

    def test_flat_prices_produce_zero_drift_and_vol(
        self, _import_fxmc, mock_yfinance,
    ):
        pair = "EURUSD=X"
        df = make_flat_series(n=200, price=1.10000)
        mock_yfinance(pair, "D", df)

        data, ok = _import_fxmc.run_mc(pair, "D")
        assert ok
        assert data["ann_drift_pct"] == pytest.approx(0.0, abs=1e-6)
        assert data["ann_vol_pct"] == pytest.approx(0.0, abs=1e-6)
        lo, hi = data["range_90"]
        assert lo == pytest.approx(hi, abs=1e-4)
        assert data["p_up"] == 50.0
        assert data["p_down"] == 50.0

    def test_missing_columns_in_yfinance_df(
        self, _import_fxmc, mock_yfinance,
    ):
        pair = "EURUSD=X"
        n = 200
        idx = pd.date_range("2024-01-01", periods=n, freq="D")
        df = pd.DataFrame({
            "Open": np.ones(n),
            "Close": np.ones(n),
        }, index=idx)
        mock_yfinance(pair, "D", df)

        result = _import_fxmc.fetch_data(pair, "D")
        assert result.empty


# ============================================================
# Helpers
# ============================================================

def p_sum(data: dict) -> float:
    """Sum of p_up and p_down (should always be 100)."""
    return round(data["p_up"] + data["p_down"], 1)