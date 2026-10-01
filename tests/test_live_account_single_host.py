"""Tests for LIVE_ACCOUNT_SINGLE_HOST guard.

Scenarios (all mocked, no OANDA/network):
  1) recent bot-owned entry present (< 2 min) → detected True + info populated
  2) no recent entry → False
  3) old entry (1 hour ago) → False
  4) is_bot_owned_trade returns False for manual trade → ignored even if recent
  5) _all_open_trades_snapshot() raises → fail-closed (True)
  6) openTime unparseable → ignored (not counted as recent)
  7) guard switch OFF (integration) → _execute_single_signal proceeds unblocked
  8) guard switch ON + recent detected → _execute_single_signal returns False
       with reason SINGLE_HOST_RECENT_ENTRY

Run:
    python tests/test_live_account_single_host.py
    python -m unittest -v tests.test_live_account_single_host
"""

import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _make_trade(trade_id: str, instrument: str, openTime_dt: datetime,
                bot_owned: bool = True):
    """Build a trade dict that looks enough like an OANDA open-trade object."""
    from scheduled_runner_v3 import _parse_oanda_openTime  # noqa: F401
    # Format like OANDA: RFC3339 nanosecond Z
    s = openTime_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.")
    s += f"{openTime_dt.microsecond:06d}000Z"
    t = {
        "id": trade_id,
        "instrument": instrument,
        "openTime": s,
        "currentUnits": "1",
        "clientExtensions": {
            "id": f"test-{trade_id}",
            "tag": "GEMINIAIBOT_V3::JPY-STRENGTH_USD_JPY_BUY_20261001",
            "comment": "",
        },
    }
    # The is_bot_owned_trade check in scheduled_runner v3 is via
    # utils.utils.is_bot_owned_trade.  We'll mock it explicitly below.
    return t, bot_owned


class TestRecentEntryDetector(unittest.TestCase):
    """Unit tests on _single_host_recent_entry_detected()."""

    def _call(self, trades_with_ownership, window_min=2):
        """trades_with_ownership: list of (trade_obj, is_bot_owned_bool)."""
        import scheduled_runner_v3 as v3
        # IMPORTANT: import is_bot_owned_trade *from* utils directly so we can
        # patch the lookup at source (the scheduled_runner_v3 name binding is
        # set at import time so patching that module's attribute is correct,
        # but in tests we do both to be safe).
        import utils.utils as _u

        trade_objs = [t for t, _owned in trades_with_ownership]

        def _fake_snapshot():
            return list(trade_objs)

        def _fake_is_bot_owned(t):
            for tt, owned in trades_with_ownership:
                # Compare by identity first, then by id field
                if tt is t:
                    return owned
                if (
                    isinstance(tt, dict) and isinstance(t, dict)
                    and tt.get("id") is not None
                    and tt.get("id") == t.get("id")
                ):
                    return owned
            return False

        with patch.object(v3, "_all_open_trades_snapshot", _fake_snapshot), \
             patch.object(v3, "is_bot_owned_trade", _fake_is_bot_owned), \
             patch.object(_u, "is_bot_owned_trade", _fake_is_bot_owned):
            return v3._single_host_recent_entry_detected(window_min=window_min)

    def test_recent_entry_detected(self):
        now = datetime.now(timezone.utc)
        t, own = _make_trade("1337", "USD_JPY", now - timedelta(seconds=45))
        detected, info = self._call([(t, True)])
        self.assertTrue(detected, "entry 45s ago must be detected")
        self.assertIn("T1337 USD_JPY", info or "")
        self.assertIn("age=", info or "")

    def test_no_recent_entry_empty_snapshot(self):
        detected, info = self._call([])
        self.assertFalse(detected)
        self.assertIsNone(info)

    def test_old_entry_not_counted(self):
        now = datetime.now(timezone.utc)
        t, own = _make_trade("1338", "GBP_JPY", now - timedelta(hours=1))
        detected, info = self._call([(t, True)])
        self.assertFalse(detected, "1h-old entry must not be recent")
        self.assertIsNone(info)

    def test_manual_trade_recent_but_ignored(self):
        """Even a very recent manual trade (not bot-owned) must be ignored."""
        now = datetime.now(timezone.utc)
        t, _ = _make_trade("9999", "EUR_USD", now - timedelta(seconds=10))
        detected, info = self._call([(t, False)])
        self.assertFalse(detected, "manual trade must not trigger guard")
        self.assertIsNone(info)

    def test_snapshot_error_fail_closed(self):
        import scheduled_runner_v3 as v3

        def boom():
            raise RuntimeError("OANDA API timeout")

        with patch.object(v3, "_all_open_trades_snapshot", boom):
            detected, info = v3._single_host_recent_entry_detected(window_min=2)
        self.assertTrue(detected, "snapshot error must fail closed")
        self.assertIn("snapshot-failed", info or "")

    def test_unparseable_opentime_skipped(self):
        import scheduled_runner_v3 as v3

        bogus = {"id": "X", "instrument": "USD_JPY", "openTime": "not-a-date",
                 "clientExtensions": {"tag": "GEMINIAIBOT_V3::..."}}
        with patch.object(v3, "_all_open_trades_snapshot",
                          return_value=[bogus]), \
             patch.object(v3, "is_bot_owned_trade", return_value=True):
            detected, info = v3._single_host_recent_entry_detected(window_min=2)
        self.assertFalse(detected, "unparseable openTime must be skipped")


class TestExecuteSignalIntegration(unittest.TestCase):
    """Integration: LIVE_ACCOUNT_SINGLE_HOST inside _execute_single_signal()."""

    def _build_entry(self, action="BUY", pair="USD_JPY", is_override=False):
        sig = {
            "pair": pair,
            "action": action,
            "entry": 158.0,
            "stop_loss": 155.0,
            "take_profit": 162.0,
            "strength_score": 2.8,
            "risk_reward": 2.0,
            "override_source": "extreme_upgrade" if is_override else None,
            "override_type": "JPY_EXTREME_TOP" if is_override else None,
        }
        return {
            "signal": sig,
            "tag_prefix": "JPY-STRENGTH",
            "group_name": "JPY",
        }

    def test_guard_off_proceeds(self):
        """Switch OFF → the function proceeds past the guard line.

        We run with dry_run=True so the function returns True before any
        broker interaction.  This lets us prove that the guard did not
        return early (i.e. the guard was bypassed because switch=False).
        """
        import scheduled_runner_v3 as v3

        entry = self._build_entry()
        with patch.object(v3, "SINGLE_HOST_GUARD_ENABLED", False), \
             patch.object(v3, "OVERRIDE_MAX_PER_CYCLE", 1), \
             patch.object(v3, "OVERRIDE_MAX_OPEN", 3), \
             patch.object(v3, "_count_open_override_trades", return_value=0), \
             patch.object(v3, "_all_open_trades_snapshot", return_value=[]):
            ok, reason = v3._execute_single_signal(
                entry, dry_run=True,
                cycle_override_issued_before=0,
                global_scores={"USD": 1.8, "JPY": -0.9},
            )
        self.assertTrue(ok, "guard OFF + dry_run=True must proceed → ok=True")
        self.assertIsNone(reason)

    def test_guard_on_recent_blocks_entry(self):
        import scheduled_runner_v3 as v3

        entry = self._build_entry()
        now = datetime.now(timezone.utc)
        recent_trade = {
            "id": "1000",
            "instrument": "USD_JPY",
            "openTime": (now - timedelta(seconds=30)).strftime(
                "%Y-%m-%dT%H:%M:%S.000000000Z"
            ),
            "clientExtensions": {"tag": "GEMINIAIBOT_V3::..."},
        }
        with patch.object(v3, "SINGLE_HOST_GUARD_ENABLED", True), \
             patch.object(v3, "SINGLE_HOST_WINDOW_MIN", 2):
            def snap():
                return [recent_trade]
            def is_bot(t):
                return True
            with patch.object(v3, "_all_open_trades_snapshot", snap), \
                 patch.object(v3, "is_bot_owned_trade", is_bot), \
                 redirect_stdout(StringIO()) as out:
                ok, reason = v3._execute_single_signal(
                    entry, dry_run=False,
                    cycle_override_issued_before=0,
                    global_scores={},
                )
                out_str = out.getvalue()
        self.assertFalse(ok)
        self.assertEqual(reason, "SINGLE_HOST_RECENT_ENTRY")
        self.assertIn("[LOCK]", out_str)
        self.assertIn("recent entry detected", out_str)

    def test_guard_on_check_error_fail_closed(self):
        import scheduled_runner_v3 as v3

        entry = self._build_entry()
        with patch.object(v3, "SINGLE_HOST_GUARD_ENABLED", True), \
             patch.object(
                 v3, "_single_host_recent_entry_detected",
                 side_effect=RuntimeError("kaboom"),
             ), redirect_stdout(StringIO()) as out:
            ok, reason = v3._execute_single_signal(
                entry, dry_run=False,
                cycle_override_issued_before=0,
                global_scores={},
            )
            out_str = out.getvalue()
        self.assertFalse(ok)
        self.assertEqual(reason, "SINGLE_HOST_CHECK_ERR")
        self.assertIn("[LOCK]", out_str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
