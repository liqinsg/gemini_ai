"""
utils/test_risk_integration.py
=========================

Phase 2 tests for risk_integration.py. Every OANDA call is mocked at
`oanda_client.request` — NO live network calls are made anywhere in this
file. These tests verify:

1. reconcile_with_oanda(): drops externally-closed units, syncs size drift,
   marks the cluster CLOSED when nothing survives.
2. apply_risk_action() -> UPDATE_SL: sends one TradeCRCDO per unit, with a
   stopLoss-only payload (per the documented take-profit-preservation contract).
3. apply_risk_action() -> PARTIAL_CLOSE/FULL_CLOSE: FIFO-allocates, sends
   TradeClose per affected unit, and syncs cluster state to the CONFIRMED
   executed size, not the requested size.
4. new_cluster_from_fill(): builds a cluster from actual fill data, not
   the planned signal data.
5. Error handling: a failed OANDA call raises RiskIntegrationError and
   does not leave the cluster in a partially-mutated state.

Run with: python3 -m pytest utils/test_risk_integration.py -v
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from oandapyV20.exceptions import V20Error
from utils.cluster_state_store import ClusterStateStore

from utils import risk_integration as ri
from utils.dynamic_risk_manager import ActionType, RiskAction, RiskStateEnum
from utils.pyramid_cluster import PyramidCluster


def _build_test_cluster(n_units: int = 1) -> PyramidCluster:
    """A cluster already past BE, with 1-3 units, each with a distinct trade_id."""
    cfg = ri.build_risk_config()
    cluster = PyramidCluster(
        initial_size=10000,
        entry_price=149.500,
        direction=1,
        atr_entry=0.25,
        entry_time=datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc),
        config=cfg,
        structural_sl_level=149.000,
        initial_trade_id="T-BASE",
    )
    cluster.update(price=150.20, atr_now=0.24, highest_high=150.20, lowest_low=149.40, hours_elapsed=4)
    if n_units >= 2:
        cluster.add_unit(5000, 150.20, datetime(2026, 8, 10, 4, 0, tzinfo=timezone.utc), 1e9, trade_id="T-ADD1")
    if n_units >= 3:
        cluster.add_unit(2500, 150.50, datetime(2026, 8, 10, 8, 0, tzinfo=timezone.utc), 1e9, trade_id="T-ADD2")
    return cluster


# ---------------------------------------------------------------------------
# 1. reconcile_with_oanda
# ---------------------------------------------------------------------------

class TestReconcileWithOanda(unittest.TestCase):
    @patch("utils.risk_integration.oanda_client")
    def test_all_units_still_open_no_change(self, mock_client):
        cluster = _build_test_cluster(n_units=2)
        mock_client.request.return_value = {
            "trades": [
                {"id": "T-BASE", "currentUnits": "10000"},
                {"id": "T-ADD1", "currentUnits": "5000"},
            ]
        }
        still_open = ri.reconcile_with_oanda(cluster, "USD_JPY")
        self.assertTrue(still_open)
        self.assertEqual(len(cluster.units), 2)
        self.assertEqual(cluster.total_size, 15000)

    @patch("utils.risk_integration.oanda_client")
    def test_externally_closed_unit_is_dropped(self, mock_client):
        cluster = _build_test_cluster(n_units=2)
        # T-ADD1 hit its TP at the broker and no longer appears in open trades.
        mock_client.request.return_value = {"trades": [{"id": "T-BASE", "currentUnits": "10000"}]}
        still_open = ri.reconcile_with_oanda(cluster, "USD_JPY")
        self.assertTrue(still_open)
        self.assertEqual(len(cluster.units), 1)
        self.assertEqual(cluster.units[0].trade_id, "T-BASE")

    @patch("utils.risk_integration.oanda_client")
    def test_size_drift_is_synced_to_oanda(self, mock_client):
        cluster = _build_test_cluster(n_units=1)
        # OANDA reports less than we think we have (e.g. a partial fill on close we
        # locally missed) — OANDA is always the source of truth for size.
        mock_client.request.return_value = {"trades": [{"id": "T-BASE", "currentUnits": "7500"}]}
        ri.reconcile_with_oanda(cluster, "USD_JPY")
        self.assertEqual(cluster.units[0].size, 7500)

    @patch("utils.risk_integration.oanda_client")
    def test_all_units_closed_marks_cluster_closed(self, mock_client):
        cluster = _build_test_cluster(n_units=1)
        mock_client.request.return_value = {"trades": []}  # nothing open anymore
        still_open = ri.reconcile_with_oanda(cluster, "USD_JPY")
        self.assertFalse(still_open)
        self.assertEqual(cluster.risk_manager.state, RiskStateEnum.CLOSED)
        self.assertEqual(len(cluster.units), 0)

    @patch("utils.risk_integration.oanda_client")
    def test_oanda_failure_raises_and_does_not_silently_proceed(self, mock_client):
        cluster = _build_test_cluster(n_units=1)
        mock_client.request.side_effect = V20Error(400, "simulated network failure")
        with self.assertRaises(ri.RiskIntegrationError):
            ri.reconcile_with_oanda(cluster, "USD_JPY")
        # Cluster must be untouched — a failed reconciliation must not be
        # treated as "nothing survived".
        self.assertEqual(len(cluster.units), 1)
        self.assertNotEqual(cluster.risk_manager.state, RiskStateEnum.CLOSED)


# ---------------------------------------------------------------------------
# 2. apply_risk_action -> UPDATE_SL
# ---------------------------------------------------------------------------

class TestApplyRiskActionUpdateSL(unittest.TestCase):
    @patch("utils.risk_integration.oanda_client")
    def test_sends_one_tradecrcdo_per_unit_stoploss_only_payload(self, mock_client):
        cluster = _build_test_cluster(n_units=3)
        mock_client.request.return_value = {}
        action = RiskAction(action=ActionType.UPDATE_SL, new_sl=150.000, state=RiskStateEnum.TRAILING_CHANDELIER)

        ri.apply_risk_action(cluster, "USD_JPY", action)

        self.assertEqual(mock_client.request.call_count, 3)
        sent_trade_ids = set()
        for call_args in mock_client.request.call_args_list:
            req = call_args[0][0]
            # oandapyV20 request objects expose their configured data payload
            payload = req.data
            self.assertIn("stopLoss", payload)
            self.assertNotIn("takeProfit", payload)  # must NOT touch TP — see module docstring
            self.assertEqual(payload["stopLoss"]["price"], "150.000")
            sent_trade_ids.add(req.trade_id if hasattr(req, "trade_id") else None)

    @patch("utils.risk_integration.oanda_client")
    def test_unit_with_no_trade_id_is_skipped_not_crashed(self, mock_client):
        cluster = _build_test_cluster(n_units=1)
        cluster.units[0].trade_id = None
        mock_client.request.return_value = {}
        action = RiskAction(action=ActionType.UPDATE_SL, new_sl=150.0, state=RiskStateEnum.TRAILING_CHANDELIER)
        ri.apply_risk_action(cluster, "USD_JPY", action)  # must not raise
        mock_client.request.assert_not_called()

    @patch("utils.risk_integration.oanda_client")
    def test_oanda_failure_raises_riskintegrationerror(self, mock_client):
        cluster = _build_test_cluster(n_units=1)
        mock_client.request.side_effect = V20Error(400, "simulated failure")
        action = RiskAction(action=ActionType.UPDATE_SL, new_sl=150.0, state=RiskStateEnum.TRAILING_CHANDELIER)
        with self.assertRaises(ri.RiskIntegrationError):
            ri.apply_risk_action(cluster, "USD_JPY", action)


# ---------------------------------------------------------------------------
# 3. apply_risk_action -> PARTIAL_CLOSE / FULL_CLOSE
# ---------------------------------------------------------------------------

class TestApplyRiskActionClose(unittest.TestCase):
    @patch("utils.risk_integration.oanda_client")
    def test_partial_close_fifo_and_syncs_confirmed_size(self, mock_client):
        cluster = _build_test_cluster(n_units=2)  # 10000 + 5000 = 15000 total
        # Requested close_size will be 7500 (50%); OANDA confirms a slightly
        # different actual fill (7480) — cluster state must reflect the CONFIRMED amount.
        mock_client.request.return_value = {"orderFillTransaction": {"units": "-7480"}}

        action = RiskAction(action=ActionType.PARTIAL_CLOSE, close_ratio=0.5, state=RiskStateEnum.TIME_DECAY_REDUCE)
        ri.apply_risk_action(cluster, "USD_JPY", action)

        # FIFO means only T-BASE (the oldest, 10000) should have been touched for a 7500 target.
        mock_client.request.assert_called_once()
        req = mock_client.request.call_args[0][0]
        self.assertEqual(req.data["units"], "7500")  # requested amount sent to OANDA

        # Cluster synced to the CONFIRMED 7480, not the requested 7500.
        self.assertEqual(cluster.units[0].size, 10000 - 7480)
        self.assertEqual(cluster.units[1].size, 5000)  # untouched, FIFO didn't reach it

    @patch("utils.risk_integration.oanda_client")
    def test_full_close_closes_every_unit_and_marks_cluster_closed(self, mock_client):
        cluster = _build_test_cluster(n_units=2)

        def fake_response(request):
            requested = abs(int(request.data["units"]))
            return {"orderFillTransaction": {"units": str(-requested)}}
        mock_client.request.side_effect = fake_response

        action = RiskAction(action=ActionType.FULL_CLOSE, close_ratio=1.0, state=RiskStateEnum.TIME_DECAY_EXIT)
        ri.apply_risk_action(cluster, "USD_JPY", action)

        self.assertEqual(mock_client.request.call_count, 2)  # both units closed
        self.assertEqual(len(cluster.units), 0)
        self.assertEqual(cluster.risk_manager.state, RiskStateEnum.CLOSED)

    @patch("utils.risk_integration.oanda_client")
    def test_close_failure_raises_and_does_not_apply_partial_progress(self, mock_client):
        """If the SECOND leg of a multi-unit close fails, the FIRST leg's OANDA
        call already executed (can't be undone), but apply_close() must not run
        with a half-confirmed instruction list — this test documents that the
        exception propagates before apply_close() is reached."""
        cluster = _build_test_cluster(n_units=2)
        mock_client.request.side_effect = [
            {"orderFillTransaction": {"units": "-10000"}},  # first unit closes fine
            V20Error(400, "simulated failure on second leg"),  # second unit fails
        ]
        action = RiskAction(action=ActionType.FULL_CLOSE, close_ratio=1.0, state=RiskStateEnum.TIME_DECAY_EXIT)
        with self.assertRaises(ri.RiskIntegrationError):
            ri.apply_risk_action(cluster, "USD_JPY", action)
        # cluster.units must be UNCHANGED (apply_close was never called) — this is
        # a known limitation documented in the module: a failure mid-close leaves
        # a real discrepancy between OANDA (leg 1 closed) and local state (both
        # still "open") until the next cycle's reconcile_with_oanda() catches it.
        self.assertEqual(len(cluster.units), 2)

    @patch("utils.risk_integration.oanda_client")
    def test_no_change_action_makes_no_oanda_calls(self, mock_client):
        cluster = _build_test_cluster(n_units=1)
        action = RiskAction(action=ActionType.NO_CHANGE, state=RiskStateEnum.TRAILING_CHANDELIER)
        ri.apply_risk_action(cluster, "USD_JPY", action)
        mock_client.request.assert_not_called()


# ---------------------------------------------------------------------------
# 4. new_cluster_from_fill
# ---------------------------------------------------------------------------

class TestNewClusterFromFill(unittest.TestCase):
    @patch("utils.risk_integration.get_atr_with_volatility_context")
    def test_builds_cluster_from_actual_fill_not_planned_signal(self, mock_atr):
        mock_atr.return_value = (0.30, 0.1)
        signal_data = {
            "pair": "USD_JPY", "action": "BUY",
            "entry": 149.500,  # PLANNED entry — must NOT be used
            "stop_loss": 149.000, "take_profit": 150.500,
        }
        # ACTUAL fill differs from planned entry due to slippage
        fill = {"status": "SUCCESS", "filled_price": "149.532", "units": "9980", "trade_id": "T-777"}

        cluster = ri.new_cluster_from_fill(signal_data, fill)

        self.assertEqual(cluster.risk_manager.entry_price_0, 149.532)  # ACTUAL fill price used
        self.assertEqual(cluster.units[0].size, 9980)  # ACTUAL filled units used
        self.assertEqual(cluster.units[0].trade_id, "T-777")
        self.assertEqual(cluster.direction, 1)

    @patch("utils.risk_integration.get_atr_with_volatility_context")
    def test_sell_signal_produces_short_direction(self, mock_atr):
        mock_atr.return_value = (0.30, 0.1)
        signal_data = {"pair": "EUR_JPY", "action": "SELL", "stop_loss": 165.000, "take_profit": 163.000}
        fill = {"filled_price": "164.500", "units": "10000", "trade_id": "T-888"}
        cluster = ri.new_cluster_from_fill(signal_data, fill)
        self.assertEqual(cluster.direction, -1)

    def test_missing_fill_field_raises_clearly(self):
        signal_data = {"pair": "USD_JPY", "action": "BUY", "stop_loss": 149.0}
        incomplete_fill = {"filled_price": "149.5", "units": "10000"}  # missing trade_id
        with self.assertRaises(ri.RiskIntegrationError) as ctx:
            ri.new_cluster_from_fill(signal_data, incomplete_fill)
        self.assertIn("trade_id", str(ctx.exception))

    @patch("utils.risk_integration.get_atr_with_volatility_context")
    def test_missing_atr_raises_clearly(self, mock_atr):
        mock_atr.return_value = (None, None)
        signal_data = {"pair": "USD_JPY", "action": "BUY", "stop_loss": 149.0}
        fill = {"filled_price": "149.5", "units": "10000", "trade_id": "T-1"}
        with self.assertRaises(ri.RiskIntegrationError):
            ri.new_cluster_from_fill(signal_data, fill)


# ---------------------------------------------------------------------------
# 5. fetch_market_context — including the get_candles() interface-mismatch
#    regression (real signature is (instrument, granularity, count) — NO
#    start/end kwargs; the original bug called it with start=entry_time).
# ---------------------------------------------------------------------------

class TestFetchMarketContext(unittest.TestCase):
    @patch("utils.risk_integration.get_candles")
    @patch("utils.risk_integration.get_atr_with_volatility_context")
    @patch("utils.risk_integration.get_latest_price")
    def test_calls_get_candles_with_real_signature_not_start_kwarg(self, mock_price, mock_atr, mock_candles):
        """Direct regression test for the reported TypeError: get_candles() must
        be called as (instrument, granularity, count) — positionally compatible
        with the real project-wide signature — never with a start= kwarg."""
        mock_price.return_value = 149.600
        mock_atr.return_value = (0.30, 0.1)
        mock_candles.return_value = []
        cluster = _build_test_cluster(n_units=1)

        ri.fetch_market_context("USD_JPY", cluster)

        mock_candles.assert_called_once()
        call = mock_candles.call_args
        self.assertNotIn("start", call.kwargs, "get_candles must never be called with start= — real signature has no such parameter")
        self.assertNotIn("end", call.kwargs)
        # Positional/keyword form must match (instrument, granularity, count) exactly.
        args, kwargs = call
        all_params = list(args) + list(kwargs.values())
        self.assertIn("USD_JPY", all_params)
        self.assertTrue(any(isinstance(v, int) for v in all_params), "count must be passed as a plain int")

    @patch("utils.risk_integration.get_candles")
    @patch("utils.risk_integration.get_atr_with_volatility_context")
    @patch("utils.risk_integration.get_latest_price")
    def test_extremes_include_live_price_even_if_candles_lag(self, mock_price, mock_atr, mock_candles):
        mock_price.return_value = 151.000  # price has moved beyond any completed candle
        mock_atr.return_value = (0.30, 0.1)
        mock_candles.return_value = [
            {"complete": True, "time": "2026-08-11T12:00:00.000000000Z", "mid": {"h": "150.200", "l": "149.800"}},
        ]
        cluster = _build_test_cluster(n_units=1)  # entry_time = 2026-08-10T00:00:00 UTC
        price, atr_now, hh, ll = ri.fetch_market_context("USD_JPY", cluster)
        self.assertEqual(price, 151.000)
        self.assertEqual(hh, 151.000)  # live price extends the high beyond the candle's 150.200
        self.assertEqual(ll, 149.800)

    @patch("utils.risk_integration.get_candles")
    @patch("utils.risk_integration.get_atr_with_volatility_context")
    @patch("utils.risk_integration.get_latest_price")
    def test_candles_before_entry_time_are_excluded(self, mock_price, mock_atr, mock_candles):
        """get_candles() returns the most-recent-N regardless of entry_time —
        some of those may predate entry and must NOT influence the Chandelier
        extremes (that would understate/overstate the true since-entry range)."""
        mock_price.return_value = 150.000
        mock_atr.return_value = (0.30, 0.1)
        mock_candles.return_value = [
            # Before entry_time (2026-08-10T00:00 UTC) — must be excluded.
            {"complete": True, "time": "2026-08-09T12:00:00.000000000Z", "mid": {"h": "999.000", "l": "1.000"}},
            # After entry_time — must be included.
            {"complete": True, "time": "2026-08-11T06:00:00.000000000Z", "mid": {"h": "150.500", "l": "149.900"}},
        ]
        cluster = _build_test_cluster(n_units=1)
        price, atr_now, hh, ll = ri.fetch_market_context("USD_JPY", cluster)
        # If the pre-entry candle leaked in, hh would be 999.0 — proves exclusion.
        self.assertEqual(hh, 150.500)
        self.assertEqual(ll, 149.900)

    @patch("utils.risk_integration.get_candles")
    @patch("utils.risk_integration.get_atr_with_volatility_context")
    @patch("utils.risk_integration.get_latest_price")
    def test_incomplete_candles_are_excluded(self, mock_price, mock_atr, mock_candles):
        mock_price.return_value = 150.000
        mock_atr.return_value = (0.30, 0.1)
        mock_candles.return_value = [
            {"complete": False, "time": "2026-08-11T06:00:00.000000000Z", "mid": {"h": "999.000", "l": "1.000"}},
        ]
        cluster = _build_test_cluster(n_units=1)
        price, atr_now, hh, ll = ri.fetch_market_context("USD_JPY", cluster)
        # The incomplete candle must be ignored entirely -> falls back to price-only extremes.
        self.assertEqual(hh, 150.000)
        self.assertEqual(ll, 150.000)

    @patch("utils.risk_integration.get_candles")
    @patch("utils.risk_integration.get_atr_with_volatility_context")
    @patch("utils.risk_integration.get_latest_price")
    def test_empty_candle_response_falls_back_to_price_without_raising(self, mock_price, mock_atr, mock_candles):
        """Insufficient/empty candle history (e.g. very recent entry, or thin
        broker history) must NOT crash — falls back to live price as the only
        known extreme, exactly as before this fix."""
        mock_price.return_value = 149.700
        mock_atr.return_value = (0.30, 0.1)
        mock_candles.return_value = []
        cluster = _build_test_cluster(n_units=1)

        price, atr_now, hh, ll = ri.fetch_market_context("USD_JPY", cluster)

        self.assertEqual(price, 149.700)
        self.assertEqual(hh, 149.700)
        self.assertEqual(ll, 149.700)

    @patch("utils.risk_integration.get_candles")
    @patch("utils.risk_integration.get_atr_with_volatility_context")
    @patch("utils.risk_integration.get_latest_price")
    def test_get_candles_exception_propagates_not_swallowed(self, mock_price, mock_atr, mock_candles):
        """A real fetch failure must raise RiskIntegrationError — never be
        treated the same as a legitimate empty response."""
        mock_price.return_value = 149.700
        mock_atr.return_value = (0.30, 0.1)
        mock_candles.side_effect = ConnectionError("simulated network failure")
        cluster = _build_test_cluster(n_units=1)

        with self.assertRaises(ri.RiskIntegrationError) as ctx:
            ri.fetch_market_context("USD_JPY", cluster)
        self.assertIn("get_candles failed", str(ctx.exception))

    @patch("utils.risk_integration.get_latest_price")
    def test_missing_price_raises_clearly(self, mock_price):
        mock_price.return_value = None
        cluster = _build_test_cluster(n_units=1)
        with self.assertRaises(ri.RiskIntegrationError):
            ri.fetch_market_context("USD_JPY", cluster)


class TestCandleCountEstimation(unittest.TestCase):
    """Unit tests for the count-from-elapsed-time bridge, isolated from any mocking."""

    def test_recent_entry_uses_minimum_count(self):
        entry_time = datetime.now(timezone.utc)
        count = ri._compute_candle_count_since(entry_time, "H1")
        self.assertGreaterEqual(count, ri._MIN_CANDLE_COUNT)

    def test_count_scales_with_elapsed_hours_for_h1(self):
        from datetime import timedelta
        entry_time = datetime.now(timezone.utc) - timedelta(hours=48)
        count = ri._compute_candle_count_since(entry_time, "H1")
        # ~48 H1 candles elapsed + buffer, comfortably within a small tolerance window.
        self.assertGreaterEqual(count, 48)
        self.assertLessEqual(count, 48 + ri._CANDLE_COUNT_BUFFER + 2)

    def test_count_is_clamped_to_oanda_max(self):
        from datetime import timedelta
        entry_time = datetime.now(timezone.utc) - timedelta(days=3000)  # absurdly long-open position
        count = ri._compute_candle_count_since(entry_time, "H1")
        self.assertEqual(count, ri._MAX_CANDLE_COUNT)

    def test_naive_entry_time_does_not_raise(self):
        """entry_time should always be tz-aware in this codebase, but a naive
        datetime must not crash count estimation — treated as UTC."""
        naive_entry = datetime(2026, 8, 10, 0, 0)  # no tzinfo
        count = ri._compute_candle_count_since(naive_entry, "H1")
        self.assertIsInstance(count, int)


class TestParseOandaCandleTime(unittest.TestCase):
    """Unit tests for the nanosecond-precision OANDA timestamp parser."""

    def test_parses_nanosecond_precision_with_trailing_z(self):
        dt = ri._parse_oanda_candle_time("2026-08-11T06:00:00.000000000Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.hour, 6)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_returns_none_on_garbage_input(self):
        self.assertIsNone(ri._parse_oanda_candle_time("not-a-timestamp"))

    def test_returns_none_on_missing_input(self):
        self.assertIsNone(ri._parse_oanda_candle_time(None))
        self.assertIsNone(ri._parse_oanda_candle_time(""))


# ---------------------------------------------------------------------------
# 6. manage_open_positions — regression tests for the "managed set retains an
#    instrument that was just confirmed closed" bug found in live logs:
#       [RISK] USD_JPY closed externally (TP/manual) — removed from managed state.
#       [RISK] Currently managing: ['USD_JPY']    <- contradicts the line above
# ---------------------------------------------------------------------------

class TestManageOpenPositions(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        # Point the module's store at an isolated temp file for this test.
        self._original_store = ri._store
        ri._store = ClusterStateStore(os.path.join(self.tmpdir, "open_clusters.json"))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        ri._store = self._original_store

    def _seed_cluster(self, instrument: str, n_units: int = 1) -> None:
        cluster = _build_test_cluster(n_units=n_units)
        ri.save_cluster_data(instrument, cluster.to_dict())

    def test_flag_off_returns_empty_list_and_touches_nothing(self):
        original_flag = ri.ENABLE_DYNAMIC_RISK_MANAGER
        ri.ENABLE_DYNAMIC_RISK_MANAGER = False
        try:
            self._seed_cluster("USD_JPY")
            result = ri.manage_open_positions()
            self.assertEqual(result, [])
        finally:
            ri.ENABLE_DYNAMIC_RISK_MANAGER = original_flag

    @patch("utils.risk_integration.oanda_client")
    def test_externally_closed_instrument_is_excluded_from_returned_list(self, mock_client):
        """DIRECT regression test for the reported bug: an instrument found
        closed at OANDA must NOT appear in the returned list — the exact
        scenario from the live log (USD_JPY closed externally)."""
        original_flag = ri.ENABLE_DYNAMIC_RISK_MANAGER
        ri.ENABLE_DYNAMIC_RISK_MANAGER = True
        try:
            self._seed_cluster("USD_JPY")
            mock_client.request.return_value = {"trades": []}  # nothing open at OANDA anymore

            result = ri.manage_open_positions()

            self.assertNotIn("USD_JPY", result, "closed instrument must not remain in the managed list")
            self.assertIsNone(ri.load_cluster_data("USD_JPY"), "state entry must be deleted")
        finally:
            ri.ENABLE_DYNAMIC_RISK_MANAGER = original_flag

    @patch("utils.risk_integration.get_candles")
    @patch("utils.risk_integration.get_atr_with_volatility_context")
    @patch("utils.risk_integration.get_latest_price")
    @patch("utils.risk_integration.oanda_client")
    def test_still_open_instrument_with_no_change_stays_in_returned_list(
        self, mock_client, mock_price, mock_atr, mock_candles
    ):
        original_flag = ri.ENABLE_DYNAMIC_RISK_MANAGER
        ri.ENABLE_DYNAMIC_RISK_MANAGER = True
        try:
            self._seed_cluster("USD_JPY")
            # Still open at OANDA, matching size, price hasn't moved enough for any action.
            mock_client.request.return_value = {"trades": [{"id": "T-BASE", "currentUnits": "10000"}]}
            mock_price.return_value = 150.30  # same as entry+small move, still < BE trigger
            mock_atr.return_value = (0.24, 0.1)
            mock_candles.return_value = []

            result = ri.manage_open_positions()

            self.assertIn("USD_JPY", result)
            self.assertIsNotNone(ri.load_cluster_data("USD_JPY"), "still-open position must remain saved")
        finally:
            ri.ENABLE_DYNAMIC_RISK_MANAGER = original_flag

    @patch("utils.risk_integration.get_candles")
    @patch("utils.risk_integration.get_atr_with_volatility_context")
    @patch("utils.risk_integration.get_latest_price")
    @patch("utils.risk_integration.oanda_client")
    def test_fully_closed_this_cycle_is_excluded_from_returned_list(
        self, mock_client, mock_price, mock_atr, mock_candles
    ):
        """The second (previously unfixed) instance of the same bug class: an
        instrument that THIS cycle's own time-decay FULL_CLOSE action closes
        must also not remain in the returned list."""
        original_flag = ri.ENABLE_DYNAMIC_RISK_MANAGER
        ri.ENABLE_DYNAMIC_RISK_MANAGER = True
        try:
            cfg = ri.build_risk_config()
            cfg.enable_time_stop = True
            cfg.time_exit_threshold = 0.01  # force an immediate full time-stop exit
            cluster = PyramidCluster(
                initial_size=10000, entry_price=149.500, direction=1, atr_entry=0.25,
                entry_time=datetime.now(timezone.utc) - timedelta(hours=100),
                config=cfg, structural_sl_level=149.000, initial_trade_id="T-BASE",
            )
            ri.save_cluster_data("USD_JPY", cluster.to_dict())

            mock_client.request.side_effect = [
                {"trades": [{"id": "T-BASE", "currentUnits": "10000"}]},  # reconcile: still open
                {"orderFillTransaction": {"units": "-10000"}},            # the FULL_CLOSE itself
            ]
            mock_price.return_value = 149.400  # below entry, well under 1R, forcing time-exit
            mock_atr.return_value = (0.05, 0.1)  # compressed ATR, satisfies vol_compression_frac check
            mock_candles.return_value = []

            result = ri.manage_open_positions()

            self.assertNotIn("USD_JPY", result)
            self.assertIsNone(ri.load_cluster_data("USD_JPY"))
        finally:
            ri.ENABLE_DYNAMIC_RISK_MANAGER = original_flag

    @patch("utils.risk_integration.oanda_client")
    def test_exception_mid_processing_keeps_instrument_in_returned_list(self, mock_client):
        """Ambiguous-state safety net: if something fails mid-cycle, the
        instrument's true state is unknown — it SHOULD stay in the returned
        list (unlike the two confirmed-closed cases above) so a same-cycle
        fresh entry isn't attempted on top of it."""
        original_flag = ri.ENABLE_DYNAMIC_RISK_MANAGER
        ri.ENABLE_DYNAMIC_RISK_MANAGER = True
        try:
            self._seed_cluster("USD_JPY")
            mock_client.request.side_effect = V20Error(500, "simulated OANDA outage")

            result = ri.manage_open_positions()

            self.assertIn("USD_JPY", result)
            self.assertIsNotNone(ri.load_cluster_data("USD_JPY"), "state must be left untouched on error")
        finally:
            ri.ENABLE_DYNAMIC_RISK_MANAGER = original_flag

    def test_no_managed_instruments_returns_empty_list(self):
        original_flag = ri.ENABLE_DYNAMIC_RISK_MANAGER
        ri.ENABLE_DYNAMIC_RISK_MANAGER = True
        try:
            self.assertEqual(ri.manage_open_positions(), [])
        finally:
            ri.ENABLE_DYNAMIC_RISK_MANAGER = original_flag


if __name__ == "__main__":
    unittest.main(verbosity=2)