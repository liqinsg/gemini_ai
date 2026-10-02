"""Fail-CLOSED guards in ``scheduled_runner_v3``.

Replaces the previous contents of this file, which loaded the long-archived
``scheduled_runner_v13.py``.  That module no longer exists in the repo root
(it lives in ``archives/``), so the old test raised ``FileNotFoundError`` at
collection and took the whole pytest run down with it instead of testing
anything.

The v13 architecture read broker state BEFORE signal analysis.  v3 does not —
its ``_build_open_exposure()`` call sits at the END of ``run_cycle`` (after the
strategy pipeline), so it cannot be unit-tested without a full network/broker
harness.  This file therefore pins the v3 guards that carry the SAME property
and ARE reachable cheaply:

  G1. A broker read failure must PROPAGATE out of ``_raw_open_trades()`` /
      ``_all_open_trades_snapshot()``.  Swallowing it into ``[]`` would mean
      "no open exposure", which silently disarms the net-exposure guard — the
      exact failure mode the fail-closed design exists to prevent.

  G2. ``_maintain_group_positions()`` must return BEFORE any bot-owned
      filtering or risk / early-exit work when that read fails.  Exits must
      never be *invented* from a state we could not read.

No broker traffic: the trading core and risk runner are replaced with mocks.
"""

from __future__ import annotations

import os
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import scheduled_runner_v3 as v3

JPY_CFG = {"quote_ccy": "JPY", "tag_prefix": "JPY-STRENGTH"}


class _BrokerDown(RuntimeError):
    """Stand-in for an OANDA read failure."""


class TestBrokerReadFailurePropagates(unittest.TestCase):
    """G1 — never degrade a failed state read into an empty position list."""

    def setUp(self):
        # Force the non-cached branch so the read really happens, and drop any
        # snapshot another test may have left behind.
        self._saved_market_closed = v3._MARKET_CLOSED_AT_STARTUP
        v3._MARKET_CLOSED_AT_STARTUP = False
        v3._ALL_OPEN_TRADES_CACHE = None

    def tearDown(self):
        v3._MARKET_CLOSED_AT_STARTUP = self._saved_market_closed
        v3._ALL_OPEN_TRADES_CACHE = None

    def test_raw_open_trades_reraises(self):
        with patch.object(
            v3._trading_core, "get_all_open_trades", side_effect=_BrokerDown("down")
        ):
            with self.assertRaises(_BrokerDown):
                v3._raw_open_trades(
                    v3._trading_core.get_all_open_trades, "get_all_open_trades"
                )

    def test_all_open_trades_snapshot_reraises_instead_of_returning_empty(self):
        with patch.object(
            v3._trading_core, "get_all_open_trades", side_effect=_BrokerDown("down")
        ):
            try:
                result = v3._all_open_trades_snapshot()
            except _BrokerDown:
                return
            self.fail(
                f"_all_open_trades_snapshot swallowed the failure and returned "
                f"{result!r} — a silent 'no exposure' reading"
            )

    def test_successful_read_is_returned_and_not_cached_while_market_is_open(self):
        payload = [{"instrument": "USD_JPY", "id": "T1"}]
        with patch.object(
            v3._trading_core, "get_all_open_trades", return_value=payload
        ) as fetch:
            self.assertEqual(v3._all_open_trades_snapshot(), payload)
            self.assertEqual(v3._all_open_trades_snapshot(), payload)
        # Market open → one call per invocation, no cross-call caching.
        self.assertEqual(fetch.call_count, 2)


class TestMaintainReturnsEarlyWhenStateUnavailable(unittest.TestCase):
    """G2 — no filtering / risk / early-exit work on an unreadable state."""

    def _run(self):
        buffer = StringIO()
        with patch.object(
            v3, "_all_open_trades_snapshot", side_effect=_BrokerDown("down")
        ), patch.object(
            v3, "is_bot_owned_trade", side_effect=AssertionError("filtering ran")
        ) as owned, patch.object(
            v3._risk_runner,
            "prune_pending_exits",
            side_effect=AssertionError("risk ran"),
        ) as prune, redirect_stdout(buffer):
            result = v3._maintain_group_positions("JPY", dict(JPY_CFG), dry_run=True)
        return result, buffer.getvalue(), owned, prune

    def test_returns_none_without_touching_filtering_or_risk(self):
        result, output, owned, prune = self._run()
        self.assertIsNone(result)
        self.assertFalse(owned.called, "bot-owned filtering ran on unreadable state")
        self.assertFalse(prune.called, "risk/early-exit ran on unreadable state")
        self.assertIn("Fetch failed", output)

    def test_failure_is_reported_with_the_group_name(self):
        _result, output, _owned, _prune = self._run()
        self.assertIn("[MAINTAIN JPY]", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
