"""Regression tests for two live-path fixes:

1. The pending-order half of the duplicate-entry gate never ran.
   ``oandapyV20`` names the endpoint ``OrdersPending`` (v3/accounts/{id}/pendingOrders);
   ``PendingOrders`` does not exist in 0.7.2, so every call raised AttributeError
   and the callers swallowed it — a resting duplicate order was never blocked.
   Fixed in ``utils/trading_core_v2.py`` and ``scheduled_runner_v144.py``.

2. ``TradingCore.close_position`` did ``position.get("long", {}).get("units")``.
   A ``null``/non-dict leg raised AttributeError inside the broad try/except,
   which turned a real close into a silent ``False`` (and in the emergency
   close-all, an aborted enumeration).

Nothing here contacts OANDA.

Run (all three work):
    python tests/test_trading_core_v2_hardening.py
    cd tests && python test_trading_core_v2_hardening.py
    python -m unittest -v tests.test_trading_core_v2_hardening
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Running this file as a script puts *this* directory on sys.path, not the
# project root, so the repo imports would fail (same convention as the other
# tests in this package).
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import oandapyV20.endpoints.orders as orders_ep

from utils.trading_core_v2 import TradingCore, _position_leg_units

ACCOUNT = "TEST-ACCOUNT"


class _FakeOpenTrades:
    """Stands in for oandapyV20.endpoints.trades.OpenTrades in the v144 runner."""

    kind = "open_trades"

    def __init__(self, accountID=None, **kwargs):
        self.response = {"trades": []}


class TestPendingOrdersEndpoint(unittest.TestCase):
    """The endpoint class must exist and the orders must come back parsed."""

    def _core(self, orders):
        client = MagicMock()

        def _request(endpoint):
            endpoint.response = {"orders": orders}
            return endpoint.response

        client.request.side_effect = _request
        return TradingCore(client, ACCOUNT), client

    def test_get_pending_orders_returns_orders(self):
        orders = [{"id": "501", "instrument": "USD_JPY", "tag": "JPY-STRENGTH_USD_JPY_BUY_20261001"}]
        core, _client = self._core(orders)
        self.assertEqual(core.get_pending_orders(), orders)

    def test_get_pending_orders_uses_the_real_pending_endpoint(self):
        """``PendingOrders`` does not exist in oandapyV20 0.7.2 — this pins the name."""
        self.assertTrue(hasattr(orders_ep, "OrdersPending"))
        core, client = self._core([])
        core.get_pending_orders()
        endpoint = client.request.call_args[0][0]
        self.assertIsInstance(endpoint, orders_ep.OrdersPending)

    def test_empty_response_is_an_empty_list(self):
        core, _client = self._core([])
        self.assertEqual(core.get_pending_orders(), [])


class TestPositionLegUnits(unittest.TestCase):
    def test_well_formed_legs(self):
        position = {"long": {"units": "10000"}, "short": {"units": "-2000"}}
        self.assertEqual(_position_leg_units(position, "long"), 10000)
        self.assertEqual(_position_leg_units(position, "short"), -2000)

    def test_null_and_missing_legs_are_flat(self):
        for position in (
            {"long": None, "short": None},
            {"long": {}, "short": {}},
            {"long": {"units": ""}, "short": {"units": "bad"}},
            {"long": "10000"},
            {},
        ):
            self.assertEqual(_position_leg_units(position, "long"), 0)
            self.assertEqual(_position_leg_units(position, "short"), 0)


class TestClosePositionSurvivesNullLeg(unittest.TestCase):
    """Pre-fix: AttributeError -> caught -> returns False on a real close."""

    def _core_with_position(self, position):
        client = MagicMock()

        def _request(endpoint):
            endpoint.response = {"positions": [position]}
            return endpoint.response

        client.request.side_effect = _request
        return TradingCore(client, ACCOUNT), client

    def test_null_short_leg_with_live_long_leg_still_closes(self):
        core, client = self._core_with_position(
            {
                "instrument": "USD_JPY",
                "long": {"units": "1000"},
                "short": None,
            }
        )
        self.assertTrue(core.close_position("USD_JPY"))
        close_request = client.request.call_args[0][0]
        self.assertEqual(close_request.data, {"longUnits": "1000"})

    def test_both_legs_null_is_treated_as_flat(self):
        core, _client = self._core_with_position(
            {"instrument": "USD_JPY", "long": None, "short": None}
        )
        # Empty payload is a no-op close; the point is that it must not crash.
        self.assertIsInstance(core.close_position("USD_JPY"), bool)


class TestV1441PendingOrderGate(unittest.TestCase):
    """scheduled_runner_v1441 delegates to TradingCore.get_pending_orders()."""

    def setUp(self):
        import scheduled_runner_v1441 as runner

        self.runner = runner
        self.root = runner.BOT_OWNED_TAG_ROOT + runner._BOT_TAG_SEP

    def _check(self, pending):
        core = SimpleNamespace(
            get_all_open_trades=lambda: [],
            get_pending_orders=lambda: pending,
        )
        with patch.object(self.runner, "_trading_core", core):
            return self.runner._check_pair_level_strategy_position("USD_JPY", "BUY")

    def test_root_wrapped_strategy_pending_order_blocks(self):
        pending = [{"instrument": "USD_JPY",
                    "tag": f"{self.root}JPY-STRENGTH_USD_JPY_BUY_20261001"}]
        allowed, reason = self._check(pending)
        self.assertFalse(allowed)
        self.assertEqual(reason, "pending order exists → pair blocked")

    def test_legacy_unwrapped_strategy_pending_order_blocks(self):
        pending = [{"instrument": "USD_JPY",
                    "tag": "JPY-STRENGTH_USD_JPY_BUY_20261001"}]
        allowed, _reason = self._check(pending)
        self.assertFalse(allowed)

    def test_foreign_pending_order_does_not_block(self):
        pending = [{"instrument": "USD_JPY", "tag": "SOMEONE-ELSE_MANUAL"}]
        allowed, _reason = self._check(pending)
        self.assertTrue(allowed)

    def test_no_pending_order_does_not_block(self):
        allowed, reason = self._check([])
        self.assertTrue(allowed)
        self.assertEqual(reason, "no JPY-STRENGTH position on pair")

    def test_other_instrument_does_not_block(self):
        pending = [{"instrument": "EUR_JPY",
                    "tag": f"{self.root}JPY-STRENGTH_EUR_JPY_BUY_20261001"}]
        allowed, _reason = self._check(pending)
        self.assertTrue(allowed)


class TestV144PendingOrderGate(unittest.TestCase):
    """scheduled_runner_v144 calls OrdersPending inline — same fix, same gate."""

    def setUp(self):
        import scheduled_runner_v144 as runner

        self.runner = runner

    def _check(self, pending):
        client = MagicMock()

        def _request(endpoint):
            if getattr(endpoint, "kind", None) == "open_trades":
                return endpoint.response
            endpoint.response = {"orders": pending}
            return endpoint.response

        client.request.side_effect = _request
        with patch.object(self.runner, "trades_mod", SimpleNamespace(OpenTrades=_FakeOpenTrades)), \
                patch.object(self.runner, "oanda_client", client), \
                patch.object(self.runner._config, "OANDA_ACCOUNT_ID", ACCOUNT):
            return self.runner._check_pair_level_strategy_position("USD_JPY", "BUY")

    def test_strategy_pending_order_blocks(self):
        pending = [{"instrument": "USD_JPY",
                    "tag": "JPY-STRENGTH_USD_JPY_BUY_20261001"}]
        allowed, reason = self._check(pending)
        self.assertFalse(allowed)
        self.assertEqual(reason, "pending order exists → pair blocked")

    def test_foreign_pending_order_does_not_block(self):
        self.assertTrue(self._check([{"instrument": "USD_JPY", "tag": "MANUAL"}])[0])

    def test_no_pending_order_does_not_block(self):
        self.assertTrue(self._check([])[0])


if __name__ == "__main__":
    unittest.main()
