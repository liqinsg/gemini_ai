"""Stage 1 regression tests — TOPUP cap ordering / args (behavior-preserving).

The existing suite does NOT exercise the Stage 1 reorder (MC regime / cap
computation moved BEFORE the TOPUP call). These tests close that gap:

  (a) call-order/args  — TOPUP runs AFTER the MC-regime/cap computation and
      receives THIS cycle's cross_net_cap / max_open_positions, not stale globals.
  (b) no-MC-pairs      — empty pair universe -> NO_MC_DATA / NEUTRAL defaults,
      and TOPUP is still invoked with those values.
  (c) basket parity    — the cap outputs (incl. the OVERRIDE/gap boost and the
      STRONG_GAP synthesis) match an independent reference oracle, and the
      module globals later consumed by _pick_global_basket equal what TOPUP got.
  (d) failure mode     — if the MC block raises, TOPUP is NOT called.

No broker/network access: every collaborator on the path is mocked, and the
cycle is unwound with a sentinel right after the TOPUP call site.
"""

import contextlib
import io
import os
import sys

import pytest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import scheduled_runner_v3 as runner  # noqa: E402


class _StopCycle(Exception):
    """Sentinel: unwind run_cycle immediately after the TOPUP call site."""


def _mk_groups(quote="JPY"):
    return {"JPY": {"quote_ccy": quote, "instruments": {"USD_JPY": {}}}}


def _oracle(global_mc, base, gap, strong_pos, neutral_pos, consol_pos):
    """Independent reference for the documented cap formula (review doc §三)."""
    if global_mc == "CONSOLIDATION":
        maxpos = consol_pos
    elif global_mc == "STRONG_MOMENTUM":
        maxpos = strong_pos
    else:
        maxpos = neutral_pos

    boost = False
    if global_mc != "CONSOLIDATION":
        if gap >= 1.8 or global_mc == "STRONG_MOMENTUM":
            boost = True
    cap = max(base, 3) if boost else base

    # Part D — STRONG_GAP synthetic label
    if global_mc == "NEUTRAL" and gap >= 1.8:
        cap = min(8, max(base, cap + 1))
    return cap, maxpos


def _drive(
    strength_pairs,
    groups,
    scores,
    regime,
    *,
    raise_regime=False,
    topup=None,
    prob_enabled=False,
):
    """Run run_cycle with the whole collaborator chain mocked.

    Returns (topup_mock, events). `events` records the ordering of the MC-regime
    computation vs the TOPUP call.
    """
    events = []
    if topup is None:
        topup = mock.MagicMock()

    def _topup_side_effect(*args, **kwargs):
        events.append("topup")
        return None

    topup.side_effect = _topup_side_effect

    def _regime_side_effect(pairs):
        events.append("mc_regime")
        if raise_regime:
            raise RuntimeError("mc regime boom")
        return regime

    patches = [
        mock.patch.object(
            runner, "_acquire_profile_lock", mock.MagicMock(return_value=None)
        ),
        mock.patch.object(
            runner, "_check_emergency_lock", mock.MagicMock(return_value=False)
        ),
        mock.patch.object(
            runner, "_resolve_effective_lots", mock.MagicMock(return_value=8)
        ),
        mock.patch.object(runner, "_resolve_min_lot", mock.MagicMock(return_value=1)),
        mock.patch.object(
            runner,
            "_env_or_config",
            mock.MagicMock(side_effect=lambda k, d=None, value_type=None: d),
        ),
        mock.patch.object(runner, "_cycle_cache_reset", mock.MagicMock()),
        mock.patch.object(runner, "_shadow_cycle_new", mock.MagicMock()),
        mock.patch.object(runner, "_safe_emit", mock.MagicMock()),
        mock.patch.object(runner, "_sltp_guardian", mock.MagicMock()),
        mock.patch.object(
            runner, "build_strength_matrix", mock.MagicMock(return_value=scores)
        ),
        mock.patch.object(
            runner, "format_strength_ranking", mock.MagicMock(return_value="")
        ),
        mock.patch.object(runner, "_print_dxy_reference", mock.MagicMock()),
        mock.patch.object(runner, "_run_composite_observation", mock.MagicMock()),
        mock.patch.object(
            runner, "print_live_jpy_index", mock.MagicMock(return_value=None)
        ),
        mock.patch.object(runner, "_maintain_group_positions", mock.MagicMock()),
        mock.patch.object(runner, "_weak_exit_scan_v31", mock.MagicMock()),
        mock.patch.object(runner, "_scale_out_scan_v31", mock.MagicMock()),
        mock.patch.object(runner, "_load_mc_cache", mock.MagicMock()),
        mock.patch.object(
            runner, "_load_prob_index", mock.MagicMock(return_value={})
        ),
        mock.patch.object(
            runner,
            "_get_global_mc_regime",
            mock.MagicMock(side_effect=_regime_side_effect),
        ),
        mock.patch.object(runner, "_mc_min_position_topup_scan_v31", topup),
        mock.patch.object(
            runner, "_print_mc_snapshot", mock.MagicMock(side_effect=_StopCycle)
        ),
        mock.patch.object(runner, "_strategy_groups", groups),
        mock.patch.object(
            runner._config_bot, "STRENGTH_PAIRS", strength_pairs, create=True
        ),
        mock.patch.object(
            runner._config,
            "PROB_ZONE_SIZING",
            {"ENABLED": prob_enabled},
            create=True,
        ),
    ]

    for p in patches:
        p.start()
    try:
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                runner.run_cycle(dry_run=True)
        except _StopCycle:
            pass
    finally:
        for p in reversed(patches):
            p.stop()
    return topup, events


def test_a_topup_gets_this_cycle_caps_not_stale_globals():
    # Deliberately wrong "previous cycle" values in the module globals.
    runner._cross_net_cap = 999
    runner._MAX_OPEN_POSITIONS = 999
    base = runner._CROSS_NET_CAP_BASE

    # gap 0.1 (< 1.8): the only boost source is regime == STRONG_MOMENTUM.
    scores = {"A": 0.1, "B": 0.2}
    topup, events = _drive(["USD_JPY"], _mk_groups(), scores, "STRONG_MOMENTUM")

    # Ordering: MC regime resolved BEFORE TOPUP.
    assert events == ["mc_regime", "topup"]
    assert topup.call_count == 1
    _, kwargs = topup.call_args
    assert kwargs["cross_net_cap"] == max(base, 3)
    assert kwargs["max_open_positions"] == runner.MC_STRONG_MOMENTUM_MAX_POSITIONS
    assert kwargs["cap_guard_mode"] == "off"
    # Proof the stale globals were NOT read.
    assert kwargs["cross_net_cap"] != 999
    assert kwargs["max_open_positions"] != 999


def test_b_no_mc_pairs_uses_defaults_and_still_calls_topup():
    runner._cross_net_cap = 999
    runner._MAX_OPEN_POSITIONS = 999

    topup, events = _drive([], _mk_groups(), {"A": 0.1, "B": 0.2}, "UNUSED")

    # Empty pair universe -> _get_global_mc_regime is not called at all.
    assert "mc_regime" not in events
    assert topup.call_count == 1
    _, kwargs = topup.call_args
    assert kwargs["cross_net_cap"] == runner._CROSS_NET_CAP_BASE
    assert kwargs["max_open_positions"] == runner.MC_NEUTRAL_MAX_POSITIONS


@pytest.mark.parametrize(
    "regime,scores",
    [
        ("NEUTRAL", {"A": 2.0, "B": -2.0}),  # gap 4.0 -> gap boost + synthesis
        ("NEUTRAL", {"A": 0.1, "B": 0.2}),  # gap 0.1 -> plain base
        ("CONSOLIDATION", {"A": 2.0, "B": -2.0}),
        ("STRONG_MOMENTUM", {"A": 0.1, "B": 0.2}),
    ],
)
def test_c_cap_outputs_match_reference_oracle_and_globals(regime, scores):
    runner._cross_net_cap = 123
    runner._MAX_OPEN_POSITIONS = 123

    topup, _ = _drive(["USD_JPY"], _mk_groups(), scores, regime)
    _, kwargs = topup.call_args

    gap = max(scores.values()) - min(scores.values())
    exp_cap, exp_max = _oracle(
        regime,
        runner._CROSS_NET_CAP_BASE,
        gap,
        runner.MC_STRONG_MOMENTUM_MAX_POSITIONS,
        runner.MC_NEUTRAL_MAX_POSITIONS,
        runner.MC_CONSOLIDATION_MAX_POSITIONS,
    )

    assert kwargs["cross_net_cap"] == exp_cap
    assert kwargs["max_open_positions"] == exp_max
    # The module globals later read by _pick_global_basket equal what TOPUP got.
    assert runner._cross_net_cap == exp_cap
    assert runner._MAX_OPEN_POSITIONS == exp_max


def test_d_mc_block_failure_skips_topup():
    runner._cross_net_cap = 123
    runner._MAX_OPEN_POSITIONS = 123
    topup = mock.MagicMock()

    with pytest.raises(RuntimeError):
        _drive(
            ["USD_JPY"],
            _mk_groups(),
            {"A": 0.1, "B": 0.2},
            "NEUTRAL",
            raise_regime=True,
            topup=topup,
        )

    # After the move, a raising MC block runs BEFORE TOPUP -> TOPUP never fires.
    assert topup.call_count == 0
