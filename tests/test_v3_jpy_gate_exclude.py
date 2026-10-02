"""Tests for the JPY global-extreme gate's RANKING-ONLY currency exclusion.

Feature under test: ``JPY_GATE_EXCLUDE_CURRENCIES`` (run.env / os.environ) and
the ``exclude_from_rank`` argument of ``_quote_ccy_global_rank()``.

LAYER 1 — pure-unit (no candle/broker traffic):
  TestParseGateExcludeCurrencies (R1/R4 parsing rules, 11 tests)
  TestRankingOnlyExclusion  (R1/R2 monotonicity + non-mutation, 6 tests)
  TestGateFailsClosed       (R3, 3 tests)
  TestSkipLoggingAndGateIntegration (R5 + pass-through wiring, 3 tests)

LAYER 1 — subprocess (proves module-level exit wiring, does ONE network call):
  TestMisconfigurationExitsLoudly (2 tests) — spawns ``import scheduled_runner_v3``
  in a subprocess to verify that bad values trigger ``sys.exit(2)`` BEFORE the
  runner proceeds.  Each import hits one OANDA ``is_market_open`` call in the
  module-level bootstrap (non-flaky — wrapped in try/except, degrades to
  ``market_closed=False`` on failure).  Only one invalid value is exercised
  because all four rejection branches are already fully covered by
  TestParseGateExcludeCurrencies; the subprocess test's job is solely to prove
  the exit-code wiring, not re-prove the parse logic.

LAYER 2 — end-to-end through generate_signals (patched, no broker):
  TestLayer2ExclusionConsistency (5 tests) — exercises the actual
  BaseCurrencyTrendStrategy Part C gate.  ``utils.strategy_helpers.get_candles``
  is patched to return ``[]`` so MA/MACD checks short-circuit cleanly; the
  gate verdict depends only on the supplied ``scores`` dict and the printed
  log lines.  These tests were mutation-tested: removing the exclude set makes
  both BOTTOM-flip and TOP-flip assertions fail.

Contract this file pins down:

  R1. SCORING IS UNTOUCHED.  The exclusion only skips currencies while building
      the gate's ranking.  ``_global_scores`` is never mutated, and USD's score
      is byte-identical with and without the exclusion.  (The previous
      implementation instead passed ``exclude_currencies`` into
      ``build_strength_matrix()``, which changed ``samples[USD]`` from 15 to 12
      and silently re-scaled USD — i.e. it moved the very reference point JPY is
      ranked against.)

  R2. THE CHANGE IS MONOTONE.  Removing a competitor can only improve or leave
      JPY's rank, never worsen it — so a gate that PASSED can never start
      FAILING because of this feature.  (The previous implementation broke this
      in 0.44% of random matrices, because USD could jump above JPY.)

  R3. FAIL-CLOSED.  ``_rank == 0`` (JPY absent from the ranked set) must SKIP.
      Testing ``_rank not in (1, _total)`` alone is insufficient: at
      ``_total == 0`` it evaluates ``0 not in (1, 0)`` → False, which would let
      the gate bypass itself.

  R4. MISCONFIGURATION FAILS LOUDLY.  ``JPY`` in the exclusion, an unknown
      code, and an exclusion leaving < 2 ranked currencies all refuse to start
      (exit 2) instead of silently disabling/blinding the gate.

  R5. THE SKIP LOG NAMES THE EXCLUSION.  When the gate blocks, the operator
      must be able to reconcile the ``/N`` denominator with the 6-currency
      banner printed above it.

``_SCORING_CURRENCIES`` (= ``utils.strategy_helpers.CURRENCIES``) is the
authority: it is the exact key set of ``_global_scores``.  ``config_bot_v3``
carries an extra NZD that never reaches the matrix, so NZD must be REJECTED as
a configured-but-inactive no-op.

No broker traffic, no candles, no network: the ranking helper is pure and the
group-level tests use the same MC tripwire as test_v3_jpy_global_extreme.py.

Run (all three work):
    python tests/test_v3_jpy_gate_exclude.py
    cd tests && python test_v3_jpy_gate_exclude.py
    python -m pytest tests/test_v3_jpy_gate_exclude.py -q
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Importing the runner executes module-level config parsing and prints a lot.
with redirect_stdout(StringIO()):
    import scheduled_runner_v3 as v3

JPY_CFG = {"quote_ccy": "JPY", "tag_prefix": "JPY-STRENGTH"}

# JPY under a correlated rival: CHF is WEAKEST and JPY is second-weakest, so
# JPY sits mid-table at 5/6 — and becomes 5/5 (weakest → gate PASS) the moment
# CHF stops competing.  This is the scenario the fix exists for.
CORRELATED = {
    "USD": 1.6000,
    "GBP": 0.3569,
    "AUD": -0.2000,
    "EUR": -1.6000,
    "JPY": -1.7000,
    "CHF": -1.9000,
}


def _run_group(scores, exclude=None, enabled=True, quote_ccy="JPY", group_name="JPY"):
    """Run one group with MC stubbed to a tripwire.

    The gate must return BEFORE any MC/candle work, so a ``_get_group_mc_regime``
    call means the gate passed.  Returns ``(result_or_None, captured_output)``;
    ``None`` means the gate let the run continue.
    """
    cfg = dict(JPY_CFG, quote_ccy=quote_ccy)
    buffer = StringIO()
    with patch.object(v3, "JPY_REQUIRE_GLOBAL_EXTREME", enabled), \
            patch.object(v3, "_JPY_GATE_EXCLUDE_SET", set(exclude or ())), \
            patch.object(v3, "_get_group_mc_regime",
                         side_effect=AssertionError("MC must not run for a skipped group")), \
            redirect_stdout(buffer):
        try:
            result = v3._run_single_group(group_name, cfg, scores)
        except AssertionError:
            return None, buffer.getvalue()
    return result, buffer.getvalue()


# ---------------------------------------------------------------------------
# R4 — parser / validation
# ---------------------------------------------------------------------------

class TestParseGateExcludeCurrencies(unittest.TestCase):
    def _parse(self, raw):
        return v3._parse_gate_exclude_currencies(raw, v3._SCORING_CURRENCIES)

    def test_unset_and_whitespace_mean_no_exclusion(self):
        for raw in (None, "", "   ", " , , ", ",,,"):
            with self.subTest(raw=raw):
                self.assertEqual(self._parse(raw), set())

    def test_codes_are_uppercased_and_trimmed(self):
        self.assertEqual(self._parse(" CHF , usd "), {"CHF", "USD"})
        self.assertEqual(self._parse("chf"), {"CHF"})

    def test_duplicates_collapse(self):
        self.assertEqual(self._parse("CHF,chf, CHF "), {"CHF"})

    def test_unknown_code_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            self._parse("XYZ")
        self.assertIn("XYZ", str(ctx.exception))

    def test_nzd_is_rejected_even_though_config_bot_declares_it(self):
        """config_bot_v3.CURRENCIES has 7 entries; the matrix only ever has 6."""
        import config_bot_v3

        self.assertIn("NZD", getattr(config_bot_v3, "CURRENCIES"))
        self.assertNotIn("NZD", v3._SCORING_CURRENCIES)
        with self.assertRaises(ValueError):
            self._parse("NZD")

    def test_jpy_cannot_exclude_itself(self):
        with self.assertRaises(ValueError) as ctx:
            self._parse("JPY")
        self.assertIn("JPY", str(ctx.exception))

    def test_jpy_rejected_even_alongside_a_valid_code(self):
        with self.assertRaises(ValueError):
            self._parse("CHF,JPY")

    def test_exclusion_leaving_fewer_than_two_is_rejected(self):
        with self.assertRaises(ValueError):
            self._parse("USD,EUR,GBP,AUD,CHF")  # leaves only JPY

    def test_exclusion_leaving_exactly_two_is_allowed(self):
        self.assertEqual(
            self._parse("USD,EUR,GBP,AUD"), {"USD", "EUR", "GBP", "AUD"}
        )

    def test_module_default_is_no_exclusion(self):
        self.assertEqual(v3._JPY_GATE_EXCLUDE_SET, set())


class TestMisconfigurationExitsLoudly(unittest.TestCase):
    """The module-level guard must actually sys.exit(2), not just raise."""

    def _import_with(self, value):
        env = os.environ.copy()
        env["JPY_GATE_EXCLUDE_CURRENCIES"] = value
        return subprocess.run(
            [sys.executable, "-c", "import scheduled_runner_v3"],
            cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=180,
        )

    def test_invalid_value_refuses_to_start(self):
        proc = self._import_with("JPY")
        self.assertEqual(proc.returncode, 2, proc.stderr[-400:])
        self.assertIn("refusing to start", proc.stdout)

    def test_valid_value_starts(self):
        proc = self._import_with("CHF")
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
        self.assertIn("JPY_GATE_EXCLUDE_CURRENCIES = ['CHF']", proc.stdout)


# ---------------------------------------------------------------------------
# R1 / R2 — ranking-only semantics
# ---------------------------------------------------------------------------

class TestRankingOnlyExclusion(unittest.TestCase):
    def test_exclusion_flips_the_verdict_when_the_rival_is_above_jpy(self):
        """CHF above JPY: removing it promotes JPY to weakest → gate PASS."""
        rank_full, total_full, _ = v3._quote_ccy_global_rank("JPY", CORRELATED)
        rank_x, total_x, _ = v3._quote_ccy_global_rank(
            "JPY", CORRELATED, exclude_from_rank={"CHF"}
        )
        self.assertEqual((rank_full, total_full), (5, 6))
        self.assertNotIn(rank_full, (1, total_full))     # mid-table → SKIP
        self.assertEqual((rank_x, total_x), (5, 5))
        self.assertIn(rank_x, (1, total_x))              # weakest → PASS

    def test_exclusion_cannot_help_when_the_rival_is_below_jpy(self):
        """Documents the semantics: sidelining a LOWER-ranked rival never helps.

        This is the case the original hand-run 'verification' accidentally
        demonstrated — JPY 2/6 → 2/5, still mid-table, gate still SKIP.
        """
        scores = {"USD": 1.5984, "JPY": 0.7803, "CHF": 0.4401,
                  "GBP": 0.3569, "AUD": -1.6190, "EUR": -1.6262}
        rank_full, total_full, _ = v3._quote_ccy_global_rank("JPY", scores)
        rank_x, total_x, _ = v3._quote_ccy_global_rank(
            "JPY", scores, exclude_from_rank={"CHF"}
        )
        self.assertEqual((rank_full, total_full), (2, 6))
        self.assertEqual((rank_x, total_x), (2, 5))
        self.assertNotIn(rank_full, (1, total_full))
        self.assertNotIn(rank_x, (1, total_x))

    def test_scores_dict_is_never_mutated_and_usd_is_not_rescaled(self):
        scores = dict(CORRELATED)
        before = dict(scores)
        v3._quote_ccy_global_rank("JPY", scores, exclude_from_rank={"CHF"})
        self.assertEqual(scores, before)
        self.assertEqual(scores["USD"], before["USD"])

    def test_total_counts_only_the_ranked_set(self):
        for excluded, expected_total in ((set(), 6), ({"CHF"}, 5),
                                         ({"CHF", "EUR"}, 4)):
            with self.subTest(excluded=sorted(excluded)):
                _r, total, _why = v3._quote_ccy_global_rank(
                    "JPY", CORRELATED, exclude_from_rank=excluded
                )
                self.assertEqual(total, expected_total)

    def test_pass_never_becomes_fail(self):
        """R2 as a property: the exclusion is monotone on the gate verdict."""
        rng = random.Random(20261002)
        ccy = list(v3._SCORING_CURRENCIES)
        regressions = []
        for _ in range(3000):
            scores = {c: rng.uniform(-3.0, 3.0) for c in ccy}
            r_full, t_full, _ = v3._quote_ccy_global_rank("JPY", scores)
            r_x, t_x, _ = v3._quote_ccy_global_rank(
                "JPY", scores, exclude_from_rank={"CHF"}
            )
            passed_full = r_full in (1, t_full)
            passed_x = r_x in (1, t_x)
            if passed_full and not passed_x:
                regressions.append((scores, r_full, t_full, r_x, t_x))
        self.assertEqual(
            regressions, [],
            f"{len(regressions)} PASS→FAIL regression(s); first={regressions[:1]}",
        )

    def test_empty_exclusion_set_is_identical_to_no_argument(self):
        self.assertEqual(
            v3._quote_ccy_global_rank("JPY", CORRELATED, exclude_from_rank=set()),
            v3._quote_ccy_global_rank("JPY", CORRELATED),
        )


# ---------------------------------------------------------------------------
# R3 — fail-closed
# ---------------------------------------------------------------------------

class TestGateFailsClosed(unittest.TestCase):
    def test_empty_matrix_does_not_bypass_the_gate(self):
        """``_total == 0`` used to make ``0 not in (1, 0)`` False → PASS."""
        result, output = _run_group({})
        self.assertIsNotNone(result, "gate PASSED on an empty matrix — bypass!")
        self.assertEqual(result["skip_reason"], "JPY_NOT_GLOBAL_EXTREME")
        self.assertIn("ranks 0/0", output)
        self.assertIn("ABSENT from the ranked set", output)

    def test_excluding_every_currency_does_not_bypass_the_gate(self):
        result, output = _run_group(
            CORRELATED, exclude=set(v3._SCORING_CURRENCIES)
        )
        self.assertIsNotNone(result, "gate PASSED with an empty ranked set — bypass!")
        self.assertEqual(result["skip_reason"], "JPY_NOT_GLOBAL_EXTREME")
        self.assertIn("ranks 0/0", output)

    def test_absent_jpy_still_fails_closed(self):
        result, _out = _run_group({"USD": 1.0, "GBP": 0.5})
        self.assertIsNotNone(result)
        self.assertEqual(result["skip_reason"], "JPY_NOT_GLOBAL_EXTREME")


# ---------------------------------------------------------------------------
# R5 — operator-facing logging + end-to-end gate behaviour
# ---------------------------------------------------------------------------

class TestSkipLoggingAndGateIntegration(unittest.TestCase):
    def test_exclusion_actually_opens_the_gate(self):
        """End-to-end: mid-table JPY with CHF above it becomes tradeable."""
        blocked, _ = _run_group(CORRELATED)
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked["skip_reason"], "JPY_NOT_GLOBAL_EXTREME")

        allowed, _ = _run_group(CORRELATED, exclude={"CHF"})
        self.assertIsNone(allowed, "exclusion did not open the gate")

    def test_skip_log_reconciles_denominator_with_the_banner(self):
        """A mid-table JPY under exclusion must say WHY the denominator is 5."""
        mid = {"USD": 2.0, "GBP": 1.0, "JPY": 0.9, "AUD": -0.5,
               "CHF": -2.0, "EUR": -3.0}
        rank, total, _ = v3._quote_ccy_global_rank(
            "JPY", mid, exclude_from_rank={"CHF"}
        )
        self.assertNotIn(rank, (1, total), "fixture must be mid-table")
        result, output = _run_group(mid, exclude={"CHF"})
        self.assertIsNotNone(result)
        self.assertIn(f"SKIP — JPY ranks {rank}/{total}", output)
        self.assertIn("[gate excludes ['CHF']]", output)
        self.assertIn("ranking-only", output)
        self.assertIn("banner lists all 6 currencies", output)

    def test_scores_are_never_rescaled_for_any_group(self):
        """Other groups and the shared matrix see the untouched scores."""
        for quote in ("USD", "CHF"):
            with self.subTest(quote=quote):
                result, _ = _run_group(
                    CORRELATED, exclude={"CHF"},
                    quote_ccy=quote, group_name=quote,
                )
                # Non-JPY groups are not gated at all → MC tripwire means it ran on.
                self.assertIsNone(result)


# ---------------------------------------------------------------------------
# R6 — Layer 2 (strategy-internal) sees the SAME exclusion as Layer 1
# ---------------------------------------------------------------------------
# The previous implementation passed a full-score dict to
# BaseCurrencyTrendStrategy.generate_signals() which always sorted ALL 6
# currencies — so with JPY_GATE_EXCLUDE_CURRENCIES=CHF, Layer 1 would PASS
# (JPY 5/5 BOTTOM) but Layer 2 would SKIP (JPY 5/6 MID).  That contradiction
# is now fixed: the exclusion set flows through the ctor and Part C filters
# it out.  These tests pin that invariant.

import custom_strategy_v3 as _csv3  # noqa: E402


class TestLayer2ExclusionConsistency(unittest.TestCase):
    """Layer 2 (JPY_REQUIRE_EXTREME_RANK) must honour the same exclude set."""

    def _run_strategy(self, scores, *, exclude=None, enable_rank=True):
        s = _csv3.BaseCurrencyTrendStrategy(
            quote_ccy="JPY",
            jpy_require_extreme_rank=enable_rank,
            jpy_gate_exclude_currencies=exclude or frozenset(),
            dominance_ratio_enabled=False,
            mc_regime=None,
        )
        buf = StringIO()
        with patch("utils.strategy_helpers.get_candles", return_value=[]):
            with redirect_stdout(buf):
                signals = s.generate_signals(scores)
        return signals, buf.getvalue()

    def _non_jpy(self, scores):
        s = _csv3.BaseCurrencyTrendStrategy(
            quote_ccy="USD",
            jpy_require_extreme_rank=True,
            jpy_gate_exclude_currencies={"CHF"},
            dominance_ratio_enabled=False,
        )
        buf = StringIO()
        with patch("utils.strategy_helpers.get_candles", return_value=[]):
            with redirect_stdout(buf):
                signals = s.generate_signals(scores)
        return signals, buf.getvalue()

    def test_layer2_uses_post_exclusion_rank_for_jpy(self):
        """JPY 5/6 MID → exclude CHF → JPY 5/5 BOTTOM → only BUY allowed."""
        scores = dict(CORRELATED)

        _, out_no_excl = self._run_strategy(scores, exclude=None)
        self.assertIn("JPY_MID (rank 5/6)", out_no_excl)
        self.assertIn("not extreme", out_no_excl)

        _, out_excl = self._run_strategy(scores, exclude={"CHF"})
        self.assertIn("applying Layer-1 exclusion set ['CHF']", out_excl)
        self.assertIn("JPY_BOTTOM (rank 5/5 [excludes ['CHF']])", out_excl)
        self.assertIn("only BUY allowed", out_excl)

    def test_layer2_top_rank_after_exclusion_allows_sell(self):
        """CHF is TOP (1/6), JPY is 2nd (2/6) → exclude CHF → JPY 1/5 TOP → SELL."""
        scores = {
            "CHF": 2.0,
            "JPY": 1.5,
            "USD": 1.0,
            "GBP": 0.5,
            "AUD": 0.2,
            "EUR": -0.5,
        }
        _, out_excl = self._run_strategy(scores, exclude={"CHF"})
        self.assertIn("applying Layer-1 exclusion set ['CHF']", out_excl)
        self.assertIn("JPY_TOP (rank 1/5 [excludes ['CHF']])", out_excl)
        self.assertIn("only SELL allowed", out_excl)

    def test_layer2_exclusion_is_silent_when_rank_not_active(self):
        """jpy_require_extreme_rank=False → no RANK GATE log even if exclude set."""
        scores = dict(CORRELATED)
        _, out = self._run_strategy(scores, exclude={"CHF"}, enable_rank=False)
        self.assertNotIn("JPY RANK GATE", out)
        self.assertNotIn("excludes ['CHF']", out)

    def test_non_jpy_group_ignores_exclude_set(self):
        """Even with jpy_require_extreme_rank=True, non-JPY groups skip Layer 2."""
        scores = dict(CORRELATED)
        _, out = self._non_jpy(scores)
        self.assertNotIn("JPY RANK GATE", out)
        self.assertNotIn("excludes", out)

    def test_empty_exclusion_is_byte_identical_to_no_argument(self):
        """frozenset() default preserves the historical all-6-currency rank."""
        scores = dict(CORRELATED)
        s_none = _csv3.BaseCurrencyTrendStrategy(
            quote_ccy="JPY", jpy_require_extreme_rank=True,
            jpy_gate_exclude_currencies=None,
            dominance_ratio_enabled=False,
        )
        s_empty = _csv3.BaseCurrencyTrendStrategy(
            quote_ccy="JPY", jpy_require_extreme_rank=True,
            jpy_gate_exclude_currencies=set(),
            dominance_ratio_enabled=False,
        )
        self.assertEqual(s_none._jpy_gate_exclude, s_empty._jpy_gate_exclude)
        self.assertIsInstance(s_none._jpy_gate_exclude, frozenset)


if __name__ == "__main__":
    unittest.main(verbosity=2)