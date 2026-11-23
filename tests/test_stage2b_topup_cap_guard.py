"""Stage 2b — MC-TOPUP cap-guard focused tests (off | shadow | enforce).

Covers the Stage 2b prompt item 6 matrix:

  • per-mode semantics (off / shadow / enforce)
  • cap logs only for fully qualified candidates
  • cap boundary: equality with the cap is allowed
  • two pairs cannot cumulatively exceed one currency's cap in a sequential scan
  • multi-trade-per-pair is counted individually (one per broker trade)
  • unmatched-prefix and top-level-tag trades are excluded
  • dry-run mutates no simulated state
  • shadow refresh uses the post-fill state for the next pair
  • shadow refresh failure never blocks or changes the order flow
  • off mode performs no extra broker reads

Netting reality (Owner-confirmed account fill mode = netting):

  Under netting a TOPUP MERGES units into the existing trade. Neither the trade
  count nor the sign-based per-currency net changes. Therefore the cap guard can
  only block when exposure is ALREADY at or over a cap; it never bounds ladder
  depth or units. Tests below exercise both:

    - netting-realistic MERGE fills (fill_mode="merge" on the fake broker);
    - new-trade fills, which exercise the reconciliation MECHANISM only and do
      NOT represent netting behavior (see the docstrings that say so).

Run:
    python -m pytest tests/test_stage2b_topup_cap_guard.py -q
    (or)  python -m unittest -v tests.test_stage2b_topup_cap_guard
"""

import os
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import scheduled_runner_v3 as runner

ROOT_TAG = "GEMINIAIBOT_V3::"
# A tag_prefix that exists in config_bot_v3.STRATEGY_GROUPS, so the trade is
# "bot-owned AND matches a strategy group" (i.e. an eligible candidate).
STRAT_TAG = f"{ROOT_TAG}JPY-STRENGTH"


def _trade(tid, instr, units, sl="1.00000", tp="2.00000", tag=None, top_level_tag=None):
    t = {
        "id": str(tid),
        "instrument": instr,
        "currentUnits": str(units),
        "clientExtensions": {"tag": tag if tag is not None else f"{STRAT_TAG}_{instr}_BUY_20261120"},
        "stopLossOrder": {"price": sl},
        "takeProfitOrder": {"price": tp},
    }
    if top_level_tag is not None:
        t["tag"] = top_level_tag
    return t


class _FakeCore:
    """Records execute_market_trade calls (never touches the network).

    ``on_order(instrument, action, units)`` — optional hook invoked after each
    recorded order, used to model how the broker state evolves after a fill.
    """

    def __init__(self, on_order=None):
        self.orders = []
        self._on_order = on_order

    def execute_market_trade(
        self, instrument, action, units, stop_loss, take_profit, *, dry_run=False,
        client_extensions=None,
    ):
        self.orders.append(
            {
                "instrument": instrument,
                "action": action,
                "units": units,
                "sl": stop_loss,
                "tp": take_profit,
                "ce": client_extensions,
            }
        )
        if self._on_order is not None:
            self._on_order(instrument, action, units)
        return {"trade_id": str(len(self.orders)), "filled_price": "1.0"}


class _Broker:
    """Minimal evolving broker with a configurable fill mode.

    fill_mode="merge"     — NETTING: the submitted units are added to the
                            existing trade of that instrument; NO new trade is
                            created. Count and sign-based net do not change.
    fill_mode="new_trade" — hedging-style: every fill appends a new trade.
                            (Mechanism-only; not how a netting account behaves.)
    """

    def __init__(self, trades, fill_mode="merge"):
        self.trades = [dict(t) for t in trades]
        self.fill_mode = fill_mode
        self.snapshots = 0
        self._next_id = 1000 + len(self.trades)

    def snapshot(self):
        self.snapshots += 1
        return [dict(t) for t in self.trades]

    def apply_fill(self, instrument, action, units):
        signed = float(units) if action == "BUY" else -float(units)
        if self.fill_mode == "merge":
            for t in self.trades:
                if t["instrument"] == instrument:
                    t["currentUnits"] = str(float(t["currentUnits"]) + signed)
                    return
        # new_trade, or a merge with no pre-existing trade of that instrument
        self._next_id += 1
        self.trades.append(_trade(self._next_id, instrument, signed))



class _Recorder:
    """Captures _mc_topup_cap_guard_shadow_log(record, summary) calls."""

    def __init__(self):
        self.calls = []  # list of (record_dict, summary_str)

    def __call__(self, record, summary):
        self.calls.append((dict(record), summary))

    def events(self):
        return [r.get("event") for r, _ in self.calls]

    def records(self, event=None):
        return [r for r, _ in self.calls if event is None or r.get("event") == event]


class TestResolveSwitch(unittest.TestCase):
    """item 1: validated switch; invalid -> fall back off with a loud warning."""

    def _resolve(self, raw):
        with patch.object(runner, "_env_or_config", lambda k, d=None, value_type=None: raw):
            buf = StringIO()
            with redirect_stdout(buf):
                mode = runner._resolve_mc_topup_cap_guard()
        return mode, buf.getvalue()

    def test_default_off(self):
        mode, _ = self._resolve("off")
        self.assertEqual(mode, "off")

    def test_shadow(self):
        mode, _ = self._resolve("shadow")
        self.assertEqual(mode, "shadow")

    def test_enforce_case_insensitive(self):
        mode, _ = self._resolve("ENFORCE")
        self.assertEqual(mode, "enforce")

    def test_invalid_falls_back_off_with_warning(self):
        mode, out = self._resolve("banana")
        self.assertEqual(mode, "off")
        self.assertIn("WARNING", out)
        self.assertIn("banana", out)


class _Base(unittest.TestCase):
    def _drive(
        self,
        trades,
        *,
        mode="off",
        cross_net_cap=None,
        max_open_positions=None,
        snapshot_seq=None,
        snapshot_raises_at=None,
        dry_run=False,
        prob_fn=None,
        regime="STRONG_MOMENTUM",
        min_lot=1,
        max_lots=4,
        broker=None,
        ce_spy=None,
    ):
        """Drive the real scan with mocked collaborators.

        ``snapshot_seq`` — successive broker snapshots; the last one repeats.
        ``snapshot_raises_at`` — 0-based snapshot call index that raises.
        ``broker`` — an evolving ``_Broker``; when given, ``trades`` is ignored,
        every order mutates the broker per its fill_mode, and each snapshot read
        returns the broker's current state.
        ``ce_spy`` — list that captures the ``strategy_tag`` argument passed to
        ``build_client_extensions`` for each order.
        """
        core = _FakeCore(on_order=(broker.apply_fill if broker is not None else None))
        rec = _Recorder()

        if broker is not None:
            snap_mock = MagicMock(side_effect=broker.snapshot)
        else:
            if snapshot_seq is None:
                snapshot_seq = [trades]
            seq = [list(s) for s in snapshot_seq]
            snap_mock = MagicMock()

            def _side():
                idx = snap_mock.call_count - 1
                if snapshot_raises_at is not None and idx == snapshot_raises_at:
                    raise RuntimeError("broker read failed")
                return list(seq[min(idx, len(seq) - 1)])

            snap_mock.side_effect = _side

        if ce_spy is not None:

            def _ce(*a, **k):
                ce_spy.append(k.get("strategy_tag"))
                return {}

        else:

            def _ce(*a, **k):
                return {}

        patches = [
            patch.object(runner, "_MC_MIN_POS_ENABLED", True),
            patch.object(runner, "_MC_MIN_LOT", min_lot),
            patch.object(runner, "_EFFECTIVE_LOTS", max_lots, create=True),
            patch.object(runner, "_trading_core", core),
            patch.object(runner, "_all_open_trades_snapshot", snap_mock),
            patch.object(runner, "_get_mc_item", lambda pair: {"regime": regime}),
            patch.object(
                runner,
                "_fetch_mc_direction_prob",
                prob_fn or (lambda pair: {"p_up": 0.7, "p_down": 0.3}),
            ),
            patch.object(runner, "build_client_extensions", _ce),
            patch.object(runner, "_mc_topup_cap_guard_shadow_log", rec),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        buf = StringIO()
        with redirect_stdout(buf):
            runner._mc_min_position_topup_scan_v31(
                dry_run,
                cross_net_cap=cross_net_cap,
                max_open_positions=max_open_positions,
                cap_guard_mode=mode,
            )
        return core, rec, snap_mock, buf.getvalue()


class TestPerModeSemantics(_Base):
    def test_off_no_logs_no_extra_reads_order_placed(self):
        """item: off — no behavior change, no cap logs, no extra broker reads."""
        core, rec, snap, _ = self._drive(
            [_trade(1, "EUR_JPY", 1)],
            mode="off",
            cross_net_cap=0,  # would block if the guard were active
            max_open_positions=0,
        )
        self.assertEqual(len(core.orders), 1)
        self.assertEqual(rec.calls, [])
        self.assertEqual(snap.call_count, 1)

    def test_shadow_logs_but_does_not_block(self):
        """item: shadow computes would-block and logs, but never blocks."""
        core, rec, snap, _ = self._drive(
            [_trade(1, "EUR_JPY", 1)],
            mode="shadow",
            cross_net_cap=1,  # EUR: 1->2, JPY: -1->-2 => both exceed 1
        )
        self.assertEqual(len(core.orders), 1)  # NOT blocked
        guards = rec.records("MC_TOPUP_CAP_GUARD")
        self.assertEqual(len(guards), 1)
        self.assertTrue(guards[0]["would_block"])
        self.assertEqual(guards[0]["reason"], "NET_CAP")
        # shadow also performed the post-fill refresh read
        self.assertEqual(snap.call_count, 2)

    def test_enforce_blocks_over_cap(self):
        """item: enforce blocks when a currency's net cap would be exceeded."""
        core, rec, _, out = self._drive(
            [_trade(1, "EUR_JPY", 1)],
            mode="enforce",
            cross_net_cap=1,  # new EUR=2 > 1
        )
        self.assertEqual(core.orders, [])
        guards = rec.records("MC_TOPUP_CAP_GUARD")
        self.assertEqual(len(guards), 1)
        self.assertTrue(guards[0]["would_block"])
        self.assertIn("BLOCK", out)

    def test_enforce_allows_when_within_cap(self):
        core, rec, _, _ = self._drive(
            [_trade(1, "EUR_JPY", 1)],
            mode="enforce",
            cross_net_cap=2,  # new EUR=2<=2, new JPY=-2<=2
        )
        self.assertEqual(len(core.orders), 1)
        self.assertFalse(rec.records("MC_TOPUP_CAP_GUARD")[0]["would_block"])


class TestCapBoundary(_Base):
    def test_equality_with_cap_is_allowed(self):
        """item: boundary — equality with the cap is allowed (not > cap)."""
        core, rec, _, _ = self._drive(
            [_trade(1, "EUR_JPY", 1)],
            mode="enforce",
            cross_net_cap=2,
        )
        self.assertEqual(len(core.orders), 1)
        g = rec.records("MC_TOPUP_CAP_GUARD")[0]
        self.assertFalse(g["would_block"])
        self.assertEqual(g["reason"], "OK")

    def test_max_open_positions_equality_allowed(self):
        core, rec, _, _ = self._drive(
            [_trade(1, "EUR_JPY", 1)],
            mode="enforce",
            max_open_positions=2,  # runtime count 1 -> new 2 == 2 allowed
        )
        self.assertEqual(len(core.orders), 1)
        self.assertFalse(rec.records("MC_TOPUP_CAP_GUARD")[0]["would_block"])


class TestQualifiedCandidatesOnly(_Base):
    def test_logs_only_for_qualified_candidates(self):
        """item: no cap log for candidates that fail an earlier gate."""
        def prob(pair):
            if pair == "GBP_JPY":
                return {"p_up": 0.2, "p_down": 0.8}  # BUY holding => mismatch
            return {"p_up": 0.8, "p_down": 0.2}

        trades = [
            _trade(1, "EUR_JPY", 1),   # qualified
            _trade(2, "GBP_JPY", 1),   # direction mismatch -> no cap eval
            _trade(3, "CHF_JPY", 4),   # already at STRONG target -> no cap eval
        ]
        core, rec, _, _ = self._drive(
            trades, mode="shadow", cross_net_cap=5, max_open_positions=9,
            prob_fn=prob, max_lots=4,
        )
        instruments = {r["instrument"] for r in rec.records("MC_TOPUP_CAP_GUARD")}
        self.assertEqual(instruments, {"EUR_JPY"})
        # GBP_JPY (mismatch) and CHF_JPY (at cap) never ordered
        self.assertEqual([o["instrument"] for o in core.orders], ["EUR_JPY"])


class TestCumulativeCap(_Base):
    def test_two_pairs_cannot_cumulatively_exceed_one_currency_cap(self):
        """Reconciliation MECHANISM test — NOT netting behavior.

        This uses a NEW-trade fill (hedging-style) only to prove the guard reads
        the refreshed broker state between pairs. On a netting account a merged
        fill changes neither count nor sign-based net, so this scenario does not
        occur there (see TestNettingMerge).

        Initial: EUR_USD +1, GBP_USD +1 -> USD net = -2, cap = 3.
        Pair 1 (EUR_USD) fills -> broker now shows a NEW EUR_USD trade
        (USD net = -3); using that refreshed state, Pair 2 (GBP_USD) would push
        USD to -4 > 3 and must be blocked under enforce.
        """
        initial = [_trade(1, "EUR_USD", 1), _trade(2, "GBP_USD", 1)]
        after_fill = initial + [_trade(3, "EUR_USD", 1)]  # new trade observed

        core, rec, _, _ = self._drive(
            initial,
            mode="enforce",
            cross_net_cap=3,
            snapshot_seq=[initial, after_fill],
        )
        # Only the first pair is submitted; the second is blocked.
        self.assertEqual([o["instrument"] for o in core.orders], ["EUR_USD"])
        guards = rec.records("MC_TOPUP_CAP_GUARD")
        self.assertEqual(len(guards), 2)
        first, second = guards
        self.assertFalse(first["would_block"])              # EUR_USD allowed
        self.assertTrue(second["would_block"])              # GBP_USD blocked
        self.assertEqual(second["instrument"], "GBP_USD")
        self.assertEqual(second["reason"], "NET_CAP")
        # Second pair evaluated against the REFRESHED runtime state (count 3).
        self.assertEqual(second["runtime_count"], 3)

    def test_without_refresh_second_pair_would_not_block(self):
        """Reconciliation MECHANISM control — NOT netting behavior.

        If the post-fill state were ignored, USD would stay -2 and the second
        pair would (wrongly) pass. This pins that the refresh matters.
        """
        initial = [_trade(1, "EUR_USD", 1), _trade(2, "GBP_USD", 1)]
        # Same as above but the post-fill snapshot is identical (no new trade):
        # USD stays -2 -> second pair passes. Demonstrates net-cap depends on
        # the reconciled broker state, not an assumed +1.
        core, rec, _, _ = self._drive(
            initial,
            mode="enforce",
            cross_net_cap=3,
            snapshot_seq=[initial, list(initial)],
        )
        self.assertEqual([o["instrument"] for o in core.orders], ["EUR_USD", "GBP_USD"])
        for g in rec.records("MC_TOPUP_CAP_GUARD"):
            self.assertFalse(g["would_block"])


class TestMultiTradePerPair(_Base):
    def test_multi_trade_per_pair_counts_individually(self):
        """item: count is one per broker trade; two long trades on the same pair = 2."""
        trades = [_trade(1, "EUR_JPY", 1), _trade(2, "EUR_JPY", 1)]
        core, rec, _, _ = self._drive(
            trades, mode="shadow", max_open_positions=2, cross_net_cap=None,
        )
        guards = rec.records("MC_TOPUP_CAP_GUARD")
        self.assertEqual(len(guards), 1)                    # one pair entry
        self.assertEqual(guards[0]["runtime_count"], 2)     # counted per trade
        self.assertTrue(guards[0]["would_block"])           # 2 + 1 = 3 > 2
        self.assertEqual(guards[0]["reason"], "MAX_OPEN_POSITIONS")


class TestOwnershipFiltering(_Base):
    def test_unmatched_prefix_and_top_level_tag_excluded(self):
        """item: exposure only counts bot-root + known strategy prefix via
        clientExtensions.tag (no top-level `tag` fallback)."""
        good = _trade(1, "EUR_JPY", 1)
        unmatched_prefix = _trade(
            2, "GBP_JPY", 1, tag=f"{ROOT_TAG}NONSENSE_PREFIX_GBP_JPY_BUY",
        )
        # bot-root tag present ONLY at the top level (clientExtensions empty)
        top_level_only = _trade(
            3, "AUD_JPY", 1, tag="", top_level_tag=f"{STRAT_TAG}_AUD_JPY_BUY",
        )
        trades = [good, unmatched_prefix, top_level_only]

        core, rec, _, _ = self._drive(
            trades, mode="shadow", cross_net_cap=5, max_open_positions=9,
        )
        guards = rec.records("MC_TOPUP_CAP_GUARD")
        self.assertEqual({r["instrument"] for r in guards}, {"EUR_JPY"})
        self.assertEqual(guards[0]["runtime_count"], 1)
        self.assertEqual([o["instrument"] for o in core.orders], ["EUR_JPY"])

    def test_manual_trade_excluded(self):
        core, rec, _, _ = self._drive(
            [_trade(1, "EUR_JPY", 1, tag="MANUAL_TAG")],
            mode="shadow", cross_net_cap=5,
        )
        self.assertEqual(core.orders, [])
        self.assertEqual(rec.calls, [])


class TestDryRunNoMutation(_Base):
    def test_dry_run_mutates_no_simulated_state(self):
        """item: dry-run never advances the simulated runtime state."""
        trades = [_trade(1, "EUR_JPY", 1), _trade(2, "GBP_JPY", 1)]
        core, rec, snap, _ = self._drive(
            trades, mode="shadow", max_open_positions=2, dry_run=True,
        )
        self.assertEqual(core.orders, [])                    # nothing submitted
        self.assertEqual(snap.call_count, 1)                 # no refresh read
        guards = rec.records("MC_TOPUP_CAP_GUARD")
        self.assertEqual(len(guards), 2)
        # Both pairs see the SAME unchanged runtime_count (2): a phantom +1
        # would have made the second pair report 3.
        self.assertEqual({g["runtime_count"] for g in guards}, {2})
        self.assertNotIn("MC_TOPUP_CAP_GUARD_REFRESH", rec.events())


class TestShadowRefresh(_Base):
    def test_refresh_uses_post_fill_state_for_next_pair(self):
        """item: a successful fill's reconciled state feeds the next pair.

        MECHANISM test — uses a NEW-trade fill to make the refreshed state
        observably differ. Netting merge fills are covered in TestNettingMerge.
        """
        initial = [_trade(1, "EUR_JPY", 1), _trade(2, "GBP_JPY", 1)]
        after_fill = initial + [_trade(3, "EUR_JPY", 1)]  # new trade observed
        core, rec, snap, _ = self._drive(
            initial,
            mode="shadow",
            max_open_positions=3,
            snapshot_seq=[initial, after_fill],
        )
        # Both pairs still submit (shadow never blocks): 1 initial read + one
        # post-fill refresh per successful order = 3 reads.
        self.assertEqual([o["instrument"] for o in core.orders], ["EUR_JPY", "GBP_JPY"])
        self.assertEqual(snap.call_count, 3)
        guards = rec.records("MC_TOPUP_CAP_GUARD")
        self.assertEqual(len(guards), 2)
        self.assertEqual(guards[0]["runtime_count"], 2)       # pre-fill
        self.assertEqual(guards[1]["runtime_count"], 3)       # post-fill (refreshed)
        self.assertTrue(guards[1]["would_block"])             # 3 + 1 = 4 > 3

        # The first refresh record carries the expected/observed detail.
        ref = rec.records("MC_TOPUP_CAP_GUARD_REFRESH")
        self.assertEqual(len(ref), 2)
        self.assertEqual(ref[0]["instrument"], "EUR_JPY")
        self.assertEqual(ref[0]["fill_kind"], "new_trade")
        self.assertTrue(ref[0]["matched"])
        self.assertEqual(ref[0]["submitted_units"], 1)

    def test_refresh_failure_never_blocks_or_changes_flow(self):
        """item: shadow refresh failure stops shadow eval only — never blocks."""
        trades = [_trade(1, "EUR_JPY", 1), _trade(2, "GBP_JPY", 1)]
        core, rec, snap, _ = self._drive(
            trades,
            mode="shadow",
            cross_net_cap=5,
            max_open_positions=9,
            snapshot_raises_at=1,  # the post-fill refresh read fails
        )
        # Both orders still submitted (no blocking of live flow).
        self.assertEqual([o["instrument"] for o in core.orders], ["EUR_JPY", "GBP_JPY"])
        self.assertIn("MC_TOPUP_CAP_GUARD_REFRESH_FAIL", rec.events())
        # Shadow eval stopped: no second CAP_GUARD evaluation for GBP_JPY.
        guard_instruments = [g["instrument"] for g in rec.records("MC_TOPUP_CAP_GUARD")]
        self.assertEqual(guard_instruments, ["EUR_JPY"])

    def test_enforce_refresh_failure_is_fail_closed(self):
        """enforce must not continue laddering on an unknown post-fill state."""
        trades = [_trade(1, "EUR_JPY", 1), _trade(2, "GBP_JPY", 1)]
        core, rec, _, out = self._drive(
            trades,
            mode="enforce",
            cross_net_cap=5,
            max_open_positions=9,
            snapshot_raises_at=1,
        )
        self.assertEqual([o["instrument"] for o in core.orders], ["EUR_JPY"])
        self.assertIn("MC_TOPUP_CAP_GUARD_REFRESH_FAIL", rec.events())


class TestNettingMerge(_Base):
    """Netting-realistic: a TOPUP MERGES units; count and sign-net do not move.

    Because neither the eligible trade count nor the sign-based per-currency net
    changes on a merged fill, the guard can only block when exposure is ALREADY
    at or over a cap. It never bounds ladder depth or units.
    """

    def test_merged_fill_logs_merged_and_count_unchanged(self):
        """item 1a: refresh shows fill_kind=merged, matched, count unchanged."""
        broker = _Broker([_trade(1, "EUR_JPY", 1)], fill_mode="merge")
        core, rec, _, _ = self._drive(
            None, broker=broker, mode="shadow", cross_net_cap=5, max_open_positions=9,
        )
        self.assertEqual(len(core.orders), 1)  # shadow still submits
        ref = rec.records("MC_TOPUP_CAP_GUARD_REFRESH")
        self.assertEqual(len(ref), 1)
        self.assertEqual(ref[0]["fill_kind"], "merged")
        self.assertTrue(ref[0]["matched"])
        self.assertEqual(ref[0]["observed_count_before"], ref[0]["observed_count_after"])

    def test_merged_fill_leaves_runtime_state_unchanged_for_next_pair(self):
        """item 1b: next pair is evaluated against the SAME runtime net/count."""
        broker = _Broker(
            [_trade(1, "EUR_JPY", 1), _trade(2, "GBP_JPY", 1)], fill_mode="merge",
        )
        core, rec, _, _ = self._drive(
            None, broker=broker, mode="shadow", cross_net_cap=9, max_open_positions=9,
        )
        self.assertEqual([o["instrument"] for o in core.orders], ["EUR_JPY", "GBP_JPY"])
        guards = rec.records("MC_TOPUP_CAP_GUARD")
        self.assertEqual(len(guards), 2)
        # A merged fill adds no trade and does not change any sign:
        self.assertEqual(guards[0]["runtime_count"], guards[1]["runtime_count"])
        self.assertEqual(guards[0]["runtime_net"], guards[1]["runtime_net"])
        self.assertEqual(guards[1]["runtime_count"], 2)

    def test_ladder_escalation_not_bounded_under_netting(self):
        """item 1c: with caps NOT already reached, every rung is allowed."""
        broker = _Broker([_trade(1, "EUR_JPY", 1)], fill_mode="merge")
        orders = []
        guards = []
        for _scan in range(3):  # 1 -> 2 -> 4 ; third scan is already at target
            core, rec, _, _ = self._drive(
                None, broker=broker, mode="enforce",
                cross_net_cap=2, max_open_positions=8,
            )
            orders += core.orders
            guards += rec.records("MC_TOPUP_CAP_GUARD")
        # 1->2 then 2->4; at the STRONG target (4) the third scan does nothing.
        self.assertEqual([o["units"] for o in orders], [1, 2])
        self.assertEqual([o["action"] for o in orders], ["BUY", "BUY"])
        # No rung was ever blocked, and the runtime count never grew (merged).
        self.assertTrue(guards)
        self.assertTrue(all(not g["would_block"] for g in guards))
        self.assertEqual({g["runtime_count"] for g in guards}, {1})

    def test_block_only_when_count_already_at_cap(self):
        """item 1d: count already at max_open_positions -> block."""
        # count = 2 (two existing trades), cap = 2 ; a further +1 would be 3 > 2.
        broker = _Broker(
            [_trade(1, "EUR_JPY", 1), _trade(2, "GBP_JPY", 1)], fill_mode="merge",
        )
        core, rec, _, _ = self._drive(
            None, broker=broker, mode="enforce",
            cross_net_cap=9, max_open_positions=2,
        )
        self.assertEqual(core.orders, [])  # blocked: count already at cap
        reasons = {g["reason"] for g in rec.records("MC_TOPUP_CAP_GUARD")}
        self.assertEqual(reasons, {"MAX_OPEN_POSITIONS"})

    def test_block_only_when_currency_net_already_at_cap(self):
        """item 1d: a currency's net already at the cap -> block on the same side."""
        # EUR net = +1 == cap ; a further EUR-long add would push it to +2 > 1.
        broker = _Broker([_trade(1, "EUR_JPY", 1)], fill_mode="merge")
        core, rec, _, _ = self._drive(
            None, broker=broker, mode="enforce",
            cross_net_cap=1, max_open_positions=9,
        )
        self.assertEqual(core.orders, [])
        g = rec.records("MC_TOPUP_CAP_GUARD")[0]
        self.assertTrue(g["would_block"])
        self.assertEqual(g["reason"], "NET_CAP")

    def test_merge_fill_allowed_below_cap(self):
        """item 1d control: exposure BELOW the cap -> merged fill still allowed."""
        broker = _Broker([_trade(1, "EUR_JPY", 1)], fill_mode="merge")
        core, rec, _, _ = self._drive(
            None, broker=broker, mode="enforce",
            cross_net_cap=3, max_open_positions=9,
        )
        self.assertEqual(len(core.orders), 1)
        self.assertFalse(rec.records("MC_TOPUP_CAP_GUARD")[0]["would_block"])


class TestOffModeBehaviorChanges(_Base):
    """Stage 2b changes off-mode behavior in three ways; pin each one.

    These are OFF-mode behavior changes versus the pre-Stage-2b "off" (see the
    [RISKS] section of the Stage 2b-FIX report).
    """

    def test_off_population_requires_strategy_prefix(self):
        """item 2a: candidates = bot-owned AND clientExtensions.tag contains a
        configured strategy prefix (SUBSTRING). A bot-root tag with an unknown
        prefix is NOT a candidate, even in off mode."""
        good = _trade(1, "EUR_JPY", 1)
        unmatched_prefix = _trade(
            2, "GBP_JPY", 1, tag=f"{ROOT_TAG}NONSENSE_PREFIX_GBP_JPY_BUY",
        )
        core, _, _, _ = self._drive(
            [good, unmatched_prefix], mode="off", cross_net_cap=9, max_open_positions=9,
        )
        self.assertEqual([o["instrument"] for o in core.orders], ["EUR_JPY"])

    def test_off_substring_prefix_matches_mid_string(self):
        """item 2a: matching is SUBSTRING containment (`p in raw_tag`), not startswith."""
        mid = _trade(1, "EUR_JPY", 1, tag=f"{ROOT_TAG}WEIRD_JPY-STRENGTH_SUFFIX_X")
        core, _, _, _ = self._drive(
            [mid], mode="off", cross_net_cap=9, max_open_positions=9,
        )
        self.assertEqual([o["instrument"] for o in core.orders], ["EUR_JPY"])

    def test_off_malformed_eligible_record_aborts_whole_scan(self):
        """item 2b: a malformed ELIGIBLE record aborts the WHOLE scan (off too).

        Pre-Stage-2b this record was skipped and the rest of the scan continued.
        """
        bad = _trade(1, "EUR_JPY", 1)
        bad["currentUnits"] = "not-a-number"  # eligible but malformed
        good = _trade(2, "GBP_JPY", 1)
        core, rec, _, out = self._drive(
            [bad, good], mode="off", cross_net_cap=9, max_open_positions=9,
        )
        self.assertEqual(core.orders, [])   # whole scan aborted -> good not topped up
        self.assertEqual(rec.calls, [])     # off logs nothing
        self.assertIn("中止", out)           # fail-closed abort message printed

    def test_off_top_level_tag_fallback_removed(self):
        """item 2c: the top-level `tag` fallback for raw_tag is gone.

        A record whose only bot-root tag lives at the TOP level (clientExtensions
        empty) is not eligible, so it is neither topped up nor a raw_tag source.
        """
        t = _trade(1, "EUR_JPY", 1, tag="", top_level_tag=f"{STRAT_TAG}_EUR_JPY_BUY")
        core, _, _, _ = self._drive(
            [t], mode="off", cross_net_cap=9, max_open_positions=9,
        )
        self.assertEqual(core.orders, [])

    def test_off_raw_tag_comes_from_client_extensions_not_top_level(self):
        """item 2c: raw_tag derives from clientExtensions.tag, never from a
        (possibly misleading) top-level tag, for an eligible trade."""
        ce_seen = []
        t = _trade(1, "EUR_JPY", 1, top_level_tag=f"{ROOT_TAG}TOP_LEVEL_PREFIX_LIE")
        core, _, _, _ = self._drive(
            [t], mode="off", cross_net_cap=9, max_open_positions=9, ce_spy=ce_seen,
        )
        self.assertEqual(len(core.orders), 1)
        self.assertEqual(len(ce_seen), 1)
        self.assertIn("JPY-STRENGTH", ce_seen[0])
        self.assertNotIn("TOP_LEVEL_PREFIX_LIE", ce_seen[0])


class TestInitFailClosed(_Base):
    def test_malformed_eligible_record_aborts_scan(self):
        """item: init failure (malformed eligible record) aborts the whole scan."""
        bad = _trade(1, "EUR_JPY", 1)
        bad["currentUnits"] = "not-a-number"  # eligible but malformed
        core, rec, _, _ = self._drive([bad], mode="shadow", cross_net_cap=5)
        self.assertEqual(core.orders, [])
        self.assertEqual(rec.calls, [])


if __name__ == "__main__":
    unittest.main()
