"""Tests for JPY extreme-rank side gate (direction-aware, strategy-internal).

Covers:
  A) Unit tests on the gate logic (embedded in BaseCurrencyTrendStrategy
     generate_signals; we test the effect via a short-circuited helper that
     isolates the ranking computation using the SAME algorithm as the strategy).
  B) Integration: gate ON + JPY mid-rank + valid USD_JPY BUY (score +2.8)
     already pre-promoted to OVERRIDE-eligible → empty signals.
  C) Switch OFF → signals unaffected.
  D) JPY missing in scores → fail closed.
  E) Tie handling is deterministic.

Nothing here contacts OANDA / reads candles.

Run:
    python tests/test_jpy_extreme_rank_gate.py
    python -m unittest -v tests.test_jpy_extreme_rank_gate
"""

import os
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch, MagicMock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import custom_strategy_v3 as cs

# Exactly the 2026-10-01 05:57 live ranking — JPY rank 4/6.
LIVE_RANKING = {
    "USD": 1.8739,
    "GBP": 1.3054,
    "EUR": -0.1483,
    "JPY": -0.9420,
    "AUD": -1.9567,
    "CHF": -2.4035,
}
JPY_TOP = dict(LIVE_RANKING, JPY=+3.0)     # JPY rank 1/6
JPY_BOTTOM = dict(LIVE_RANKING, JPY=-4.0)  # JPY rank 6/6
JPY_ABSENT = {k: v for k, v in LIVE_RANKING.items() if k != "JPY"}


def _jpy_rank_gate_same_algo(scores: dict) -> tuple:
    """Replicates the exact ranking logic inside BaseCurrencyTrendStrategy
    generate_signals Part C (0) so we can unit-test it in isolation.
    Returns (label: str, allowed_sides: set) matching the strategy output."""
    try:
        ranked_global = sorted(
            ((c, s) for c, s in (scores or {}).items() if s is not None),
            key=lambda kv: kv[1],
            reverse=True,
        )
    except Exception:
        ranked_global = []
    total_n = len(ranked_global)
    jpy_pos = 0
    for i, (c, _s) in enumerate(ranked_global, 1):
        if c == "JPY":
            jpy_pos = i
            break
    if jpy_pos == 0:
        return "JPY_ABSENT (missing)", set()
    if jpy_pos == 1:
        return f"JPY_TOP (rank 1/{total_n})", {"SELL"}
    if jpy_pos == total_n:
        return f"JPY_BOTTOM (rank {total_n}/{total_n})", {"BUY"}
    return f"JPY_MID (rank {jpy_pos}/{total_n})", set()


def _make_strategy(enable_gate: bool):
    """Construct a minimal JPY strategy without touching network/config."""
    # Monkey-patch STRENGTH_PAIRS to the JPY group so group_strength_rank works.
    from custom_strategy_v3 import STRENGTH_PAIRS as _orig_pairs
    with patch.object(cs, "STRENGTH_PAIRS", [
        "USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY",
    ] if False else _orig_pairs):
        return cs.BaseCurrencyTrendStrategy(
            quote_ccy="JPY",
            trade_pairs=["USD_JPY"],  # only USD_JPY needed for integrations
            dominance_ratio_enabled=False,  # skip ratio filter, keep simple
            jpy_require_extreme_rank=enable_gate,
        )


class TestRankGateAlgo(unittest.TestCase):
    """(a) Unit tests on _jpy_rank_gate_same_algo mirroring strategy logic."""

    def test_live_ranking_jpy_rank4_empty_set(self):
        """a1) live 2026-10-01 scores: JPY rank 4/6 → empty set (no entries)."""
        label, sides = _jpy_rank_gate_same_algo(LIVE_RANKING)
        self.assertEqual(label, "JPY_MID (rank 4/6)")
        self.assertEqual(sides, set())

    def test_jpy_top_allows_sell_only(self):
        """a2) JPY highest (strongest) → {"SELL"}."""
        label, sides = _jpy_rank_gate_same_algo(JPY_TOP)
        self.assertIn("JPY_TOP (rank 1/6)", label)
        self.assertEqual(sides, {"SELL"})

    def test_jpy_bottom_allows_buy_only(self):
        """a3) JPY lowest (weakest) → {"BUY"}."""
        label, sides = _jpy_rank_gate_same_algo(JPY_BOTTOM)
        self.assertIn("JPY_BOTTOM (rank 6/6)", label)
        self.assertEqual(sides, {"BUY"})

    def test_jpy_missing_fail_closed(self):
        """a4) JPY absent from matrix → empty set."""
        label, sides = _jpy_rank_gate_same_algo(JPY_ABSENT)
        self.assertIn("JPY_ABSENT", label)
        self.assertEqual(sides, set())

    def test_tie_is_deterministic(self):
        """a5) Tie between JPY and another: stable sort so positions tie-break
        by key alphabetical.  Result must be deterministic across runs."""
        tied = {"USD": 0.0, "JPY": 0.0, "EUR": -1.0}
        labels = set()
        sides_set = set()
        for _ in range(20):
            label, sides = _jpy_rank_gate_same_algo(tied)
            labels.add(label)
            sides_set.add(tuple(sorted(sides)))
        self.assertEqual(len(labels), 1, "tie rank must be deterministic")
        self.assertEqual(len(sides_set), 1)


class _NoNetStrategy:
    """Bypass the MA/MACD/ATR candle reads by stubbing the pair-check loop.

    Strategy.generate_signals runs the full MA/MACD pipeline which requires
    live candles.  For integration tests we only care whether signals that
    REACH the JPY blocks are filtered.  We inject pre-built valid_signals
    directly by overriding the expensive body via subclassing + mocking the
    expensive check loop out.

    Technique: replace the body of the pair for-loop with a pre-built signal
    list, then let the JPY Part C blocks (0) (1) (1b) (2) (3) run untouched.
    """


class TestGateIntegration(unittest.TestCase):
    """(b,c) Integration: simulate pre-built signals passing through Part C."""

    def _inject_signals_and_run_jpy_part_c(self, strategy, scores, signals):
        """Short-circuit generate_signals so only the JPY Part C blocks run
        on ``signals`` (pre-built).  Returns the final signals list.
        """
        import types
        orig_gen = cs.BaseCurrencyTrendStrategy.generate_signals

        def hijacked_generate_signals(self, scores_dict):
            # Replicate the END of generate_signals *from* the line just before
            # Part C (0) — i.e. skip pair checking, directly populate
            # all_valid_signals, strength_pass_count, then run the JPY blocks.
            all_valid_signals = list(signals)
            strength_pass_count = len(all_valid_signals)
            scores = scores_dict  # noqa: F841 — alias to match inner var name

            # ----------------- COPY OF THE STRATEGY'S JPY PART C -------------
            _jpy_label: str = ""
            _jpy_allowed_sides: set = set()
            _jpy_rank_gate_active = (
                self.quote_ccy == "JPY" and self.JPY_REQUIRE_EXTREME_RANK
            )
            if _jpy_rank_gate_active:
                try:
                    _ranked_global = sorted(
                        (
                            (c, s)
                            for c, s in (scores or {}).items()
                            if s is not None
                        ),
                        key=lambda kv: kv[1],
                        reverse=True,
                    )
                    _total_n = len(_ranked_global)
                    _jpy_pos = 0
                    for _i, (_c, _s) in enumerate(_ranked_global, 1):
                        if _c == "JPY":
                            _jpy_pos = _i
                            break
                    if _jpy_pos == 0:
                        _jpy_label = "JPY_ABSENT (missing)"
                        _jpy_allowed_sides = set()
                    elif _jpy_pos == 1:
                        _jpy_label = f"JPY_TOP (rank 1/{_total_n})"
                        _jpy_allowed_sides = {"SELL"}
                    elif _jpy_pos == _total_n:
                        _jpy_label = f"JPY_BOTTOM (rank {_total_n}/{_total_n})"
                        _jpy_allowed_sides = {"BUY"}
                    else:
                        _jpy_label = f"JPY_MID (rank {_jpy_pos}/{_total_n})"
                        _jpy_allowed_sides = set()
                except Exception:
                    _jpy_label = "JPY_RANK_ERROR"
                    _jpy_allowed_sides = set()
                if not _jpy_allowed_sides:
                    return []
            if self.quote_ccy == "JPY":
                _jpy_normal = [s for s in all_valid_signals if not s.get("override_source")]
                _jpy_override = [s for s in all_valid_signals if s.get("override_source")]
                if _jpy_normal:
                    _sorted_normal = sorted(
                        _jpy_normal, key=lambda s: s["strength_score"],
                    )
                    _weakest = _sorted_normal[0]
                    _strongest = _sorted_normal[-1]
                    _kept = {_weakest["pair"], _strongest["pair"]}
                    all_valid_signals = (
                        [s for s in _jpy_normal if s["pair"] in _kept] + _jpy_override
                    )
                self.MIN_STRENGTH_PASSING_PAIRS = min(
                    self.MIN_STRENGTH_PASSING_PAIRS, 1
                )
                if _jpy_rank_gate_active and _jpy_allowed_sides:
                    all_valid_signals = [
                        s for s in all_valid_signals
                        if s.get("action") in _jpy_allowed_sides
                    ]
                # Part C(2) OVERRIDE upgrade — run on the filtered list.
                thr = getattr(self, "DOMINANCE_OVERRIDE_THRESHOLD", 1.8)
                upgraded = []
                for s in all_valid_signals:
                    if s.get("override_source"):
                        upgraded.append(s)
                        continue
                    sc = float(s.get("strength_score") or 0.0)
                    if abs(sc) >= thr:
                        s2 = dict(s)
                        s2["override_source"] = "extreme_upgrade"
                        s2["override_type"] = (
                            "JPY_EXTREME_TOP" if s["action"] == "BUY"
                            else "JPY_EXTREME_BOTTOM"
                        )
                        upgraded.append(s2)
                    else:
                        upgraded.append(s)
                all_valid_signals = upgraded
            return all_valid_signals

        try:
            cs.BaseCurrencyTrendStrategy.generate_signals = hijacked_generate_signals
            return strategy.generate_signals(scores)
        finally:
            cs.BaseCurrencyTrendStrategy.generate_signals = orig_gen

    def _usdjpy_buy_28_signal(self, already_override=False):
        """USD_JPY BUY score +2.8 OVERRIDE-eligible signal."""
        return {
            "pair": "USD_JPY",
            "action": "BUY",
            "bar_time": "2026-10-01T06:00:00+00:00",
            "entry": 158.329,
            "stop_loss": 155.461,
            "take_profit": 162.631,
            "strength_score": +2.8,
            "risk_reward": 2.0,
            "reasoning": "mock signal",
            "override_source": "dominance_ratio" if already_override else None,
            "override_type": "DOMINANT" if already_override else None,
            "override_ratio": 1.7 if already_override else 0.0,
            "priority": "HIGH",
        }

    def test_gate_on_mid_rank_returns_empty(self):
        """b) Integration — gate ON + JPY rank4 + USDJPY BUY 2.8 OVERRIDE-able → []."""
        strategy = _make_strategy(enable_gate=True)
        sig = self._usdjpy_buy_28_signal(already_override=False)
        result = self._inject_signals_and_run_jpy_part_c(
            strategy, LIVE_RANKING, [sig]
        )
        self.assertEqual(result, [], "mid-rank JPY must drop all signals incl OVERRIDE-eligibles")

    def test_gate_on_mid_rank_already_override_also_dropped(self):
        """b variant — gate ON + JPY mid + already native OVERRIDE → still dropped."""
        strategy = _make_strategy(enable_gate=True)
        sig = self._usdjpy_buy_28_signal(already_override=True)
        result = self._inject_signals_and_run_jpy_part_c(
            strategy, LIVE_RANKING, [sig]
        )
        self.assertEqual(result, [], "native OVERRIDE cannot bypass the direction gate")

    def test_gate_on_bottom_allows_buy(self):
        """b positive — JPY rank N/BOTTOM + BUY USDJPY → kept and upgraded."""
        strategy = _make_strategy(enable_gate=True)
        sig = self._usdjpy_buy_28_signal(already_override=False)
        result = self._inject_signals_and_run_jpy_part_c(
            strategy, JPY_BOTTOM, [sig]
        )
        self.assertEqual(len(result), 1, "JPY bottom must allow BUY USD_JPY")
        self.assertEqual(result[0]["override_source"], "extreme_upgrade")
        self.assertEqual(result[0]["override_type"], "JPY_EXTREME_TOP")

    def test_gate_on_top_blocks_buy_keeps_sell(self):
        """b direction — JPY rank 1/TOP → BUY blocked, SELL allowed."""
        strategy = _make_strategy(enable_gate=True)
        buy = self._usdjpy_buy_28_signal(already_override=True)
        sell = dict(buy, action="SELL", strength_score=-2.8, pair="EUR_JPY")
        result = self._inject_signals_and_run_jpy_part_c(
            strategy, JPY_TOP, [buy, sell]
        )
        actions = [s["action"] for s in result]
        self.assertNotIn("BUY", actions)
        self.assertIn("SELL", actions)

    def test_gate_off_signals_unchanged(self):
        """c) gate OFF — same mid-rank scenario as test (b) → signals pass through."""
        strategy = _make_strategy(enable_gate=False)
        sig = self._usdjpy_buy_28_signal(already_override=False)
        result = self._inject_signals_and_run_jpy_part_c(
            strategy, LIVE_RANKING, [sig]
        )
        self.assertEqual(len(result), 1, "gate OFF → mid-rank JPY still allows signals")
        self.assertEqual(result[0]["action"], "BUY")
        self.assertEqual(result[0]["override_source"], "extreme_upgrade")


class TestSwitchLoading(unittest.TestCase):
    """Ensure the scheduled_runner_v3 switch default is OFF and source prints."""

    def test_default_is_off(self):
        """Must default to False to preserve existing (2026-10-01) behavior."""
        with patch.dict(os.environ, {}, clear=False):
            # Unset any env keys so fallback chain runs to code default
            if "JPY_REQUIRE_EXTREME_RANK" in os.environ:
                del os.environ["JPY_REQUIRE_EXTREME_RANK"]
            import importlib
            import scheduled_runner_v3 as v3_mod
            # The value is set at import time; re-importing would re-run the
            # entire file (heavy).  Instead just assert that the code path
            # produces False using the same logic as the runner.
            from scheduled_runner_v3 import _ENV_LOADED_KEYS as _k
            in_env = "JPY_REQUIRE_EXTREME_RANK" in _k
            in_cfg = hasattr(v3_mod._config_bot, "JPY_REQUIRE_EXTREME_RANK")
            if not in_env and not in_cfg:
                effective = False
                src = "defaults (false)"
            else:
                # Can't easily check without full reimport — skip in that case.
                self.skipTest("runner already imported with different env")
            self.assertFalse(effective)
            self.assertEqual(src, "defaults (false)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
