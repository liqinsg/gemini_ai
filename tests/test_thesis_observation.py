# tests/test_thesis_observation.py
"""
Tests for the CORRECTED utils/thesis_observation.py + utils/thesis_state_store.py
(Architecture vNext Phase 2 Correction).

Structure:
  A. classify_thesis() pure-function correctness
  B. Zero-strength-score semantics
  C. Shared strength-matrix call-count proof
  D. Missing/corrupt persistence file safety
  E. Orphan cleanup
  F. Passivity re-proof
  G. REAL SUBPROCESS-BOUNDARY TESTS
"""
import json
import os
import sys
import textwrap
import subprocess
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from utils.thesis_observation import (
    ThesisState,
    SnapshotKind,
    classify_thesis,
    compute_generic_strength_score,
    observe_thesis,
    safe_build_strength_matrix_once,
    reset_observation_state,
)
from utils.thesis_state_store import (
    load_thesis_state,
    save_thesis_state,
    thesis_state_session,
    cleanup_orphaned_instruments,
    ThesisStateStoreError,
)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    log_path = str(tmp_path / "thesis_obs_test.jsonl")
    state_path = str(tmp_path / "thesis_state_test.json")
    monkeypatch.setattr("utils.thesis_observation.THESIS_OBSERVATION_LOG_PATH", log_path)
    reset_observation_state()
    yield {"log_path": log_path, "state_path": state_path, "tmp_path": str(tmp_path)}


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _mock_features(alignment_label):
    return (
        patch("utils.strategy_helpers.check_ma5_alignment", return_value=alignment_label),
        patch("utils.strategy_helpers.get_slope_diagnostics", return_value={"combined": "AGAINST"}),
        patch("utils.strategy_helpers.get_atr_with_volatility_context", return_value=(0.5, 1.0)),
    )


def test_intact_buy():
    r = classify_thesis(1, 0.5, 0.6, "BUY", "BUY")
    assert r["state"] == ThesisState.THESIS_INTACT.value


def test_intact_sell():
    r = classify_thesis(-1, -0.5, -0.7, "SELL", "SELL")
    assert r["state"] == ThesisState.THESIS_INTACT.value


def test_invalidated_buy():
    r = classify_thesis(1, 0.5, -0.6, "BUY", "SELL")
    assert r["state"] == ThesisState.THESIS_INVALIDATED.value


def test_invalidated_sell():
    r = classify_thesis(-1, -0.5, 0.6, "SELL", "BUY")
    assert r["state"] == ThesisState.THESIS_INVALIDATED.value


def test_deteriorating_mixed():
    r = classify_thesis(1, 0.5, 0.4, "BUY", "SELL")
    assert r["state"] == ThesisState.THESIS_DETERIORATING.value


def test_unknown_when_both_unavailable():
    r = classify_thesis(1, 0.5, None, "BUY", None)
    assert r["state"] == ThesisState.UNKNOWN.value
    assert r["state"] not in (
        ThesisState.THESIS_INTACT.value,
        ThesisState.THESIS_DETERIORATING.value,
        ThesisState.THESIS_INVALIDATED.value,
        ThesisState.UNCLASSIFIED.value,
    )


def test_generic_strength_score_symmetry_and_genericity():
    matrix = {"GBP": 1.0, "JPY": -0.5, "USD": -2.0, "EUR": 1.5}
    assert compute_generic_strength_score("GBP_JPY", matrix) == pytest.approx(1.5)
    assert compute_generic_strength_score("EUR_USD", matrix) == pytest.approx(3.5)
    assert compute_generic_strength_score("USD_JPY", matrix) == pytest.approx(-1.5)


def test_generic_strength_score_none_matrix():
    assert compute_generic_strength_score("GBP_JPY", None) is None


def test_zero_strength_score_is_neutral_for_buy():
    r = classify_thesis(1, 0.5, 0.0, "BUY", "BUY")
    assert r["state"] == ThesisState.THESIS_INTACT.value


def test_zero_strength_score_is_neutral_for_sell():
    r = classify_thesis(-1, -0.5, 0.0, "SELL", "SELL")
    assert r["state"] == ThesisState.THESIS_INTACT.value


def test_zero_strength_score_does_not_count_as_deterioration_alone():
    r_zero = classify_thesis(1, 0.5, 0.0, "BUY", None)
    r_none = classify_thesis(1, 0.5, None, "BUY", None)
    assert r_zero["state"] == r_none["state"] == ThesisState.UNKNOWN.value


def test_shared_matrix_computed_once_across_multiple_instruments(isolate):
    call_counter = {"count": 0}

    def _counting_matrix():
        call_counter["count"] += 1
        return {"GBP": 1.0, "JPY": 0.0, "USD": -1.0, "EUR": 0.5}

    p1, p2, p3 = _mock_features("BUY")
    with patch("utils.strategy_helpers.build_strength_matrix", side_effect=_counting_matrix), p1, p2, p3:
        matrix, err = safe_build_strength_matrix_once()
        assert err is None
        assert call_counter["count"] == 1

        with thesis_state_session(state_path=isolate["state_path"]) as state:
            for instrument in ("GBP_JPY", "EUR_JPY", "USD_JPY", "AUD_JPY"):
                observe_thesis(
                    state=state, instrument=instrument, direction_sign=1,
                    entry_price_0=190.0, entry_time="2026-08-20T00:00:00+00:00",
                    current_price=190.0, unrealized_r=0.0, cycle_id="c0",
                    strength_matrix=matrix,
                )

    assert call_counter["count"] == 1


def test_shared_matrix_failure_degrades_gracefully_without_retry(isolate):
    call_counter = {"count": 0}

    def _failing_matrix():
        call_counter["count"] += 1
        raise RuntimeError("OANDA candle fetch failed")

    p1, p2, p3 = _mock_features("BUY")
    with patch("utils.strategy_helpers.build_strength_matrix", side_effect=_failing_matrix), p1, p2, p3:
        matrix, err = safe_build_strength_matrix_once()
        assert matrix is None
        assert err is not None
        assert call_counter["count"] == 1

        with thesis_state_session(state_path=isolate["state_path"]) as state:
            for instrument in ("GBP_JPY", "EUR_JPY", "USD_JPY"):
                observe_thesis(
                    state=state, instrument=instrument, direction_sign=1,
                    entry_price_0=190.0, entry_time="2026-08-20T00:00:00+00:00",
                    current_price=190.0, unrealized_r=0.0, cycle_id="c0",
                    strength_matrix=matrix,
                )

    assert call_counter["count"] == 1
    records = _read_jsonl(isolate["log_path"])
    snap = next(
        r for r in records
        if r["log_type"] == "thesis_snapshot"
        and r["instrument"] == "GBP_JPY"
    )
    assert snap["strength_score"] is None
    assert "unavailable" in snap["strength_score_unavailable_reason"]


def test_missing_state_file_is_empty_state(tmp_path):
    path = str(tmp_path / "does_not_exist.json")
    state = load_thesis_state(path)
    assert state == {"schema_version": 1, "instruments": {}}


def test_corrupt_state_file_is_quarantined_and_replaced(tmp_path):
    path = str(tmp_path / "corrupt.json")
    with open(path, "w") as f:
        f.write("{not valid json!!!")

    state = load_thesis_state(path)
    assert state == {"schema_version": 1, "instruments": {}}

    quarantined = [
        f for f in os.listdir(tmp_path)
        if f.startswith("corrupt.json.corrupt.")
    ]
    assert len(quarantined) == 1

    with open(os.path.join(tmp_path, quarantined[0])) as f:
        assert f.read() == "{not valid json!!!"


def test_observe_thesis_survives_corrupt_state_via_session(tmp_path):
    path = str(tmp_path / "corrupt2.json")
    with open(path, "w") as f:
        f.write("not json at all")

    p1, p2, p3 = _mock_features("BUY")
    with p1, p2, p3:
        with thesis_state_session(state_path=path) as state:
            observe_thesis(
                state=state,
                instrument="GBP_JPY",
                direction_sign=1,
                entry_price_0=190.0,
                entry_time="2026-08-20T00:00:00+00:00",
                current_price=190.0,
                unrealized_r=0.0,
                cycle_id="c0",
                strength_matrix={"GBP": 1.0, "JPY": 0.0},
            )

    reloaded = load_thesis_state(path)
    assert "GBP_JPY" in reloaded["instruments"]
    assert (
        reloaded["instruments"]["GBP_JPY"]["confirmation"]["last_classification"]
        == ThesisState.UNCLASSIFIED.value
    )


def test_orphan_cleanup_removes_unmanaged_instrument():
    state = {
        "schema_version": 1,
        "instruments": {
            "GBP_JPY": {"snapshot": {}, "confirmation": {}},
            "USD_JPY": {"snapshot": {}, "confirmation": {}},
        },
    }

    cleanup_orphaned_instruments(state, ["GBP_JPY"])

    assert "GBP_JPY" in state["instruments"]
    assert "USD_JPY" not in state["instruments"]


def test_same_instrument_new_thesis_does_not_inherit_old_snapshot(isolate):
    p1, p2, p3 = _mock_features("BUY")

    with p1, p2, p3:
        with thesis_state_session(state_path=isolate["state_path"]) as state:
            observe_thesis(
                state=state,
                instrument="USD_JPY",
                direction_sign=1,
                entry_price_0=150.0,
                entry_time="2026-08-20T00:00:00+00:00",
                current_price=150.0,
                unrealized_r=0.0,
                cycle_id="cA",
                strength_matrix={"USD": 1.0, "JPY": 0.0},
            )

    with thesis_state_session(state_path=isolate["state_path"]) as state:
        cleanup_orphaned_instruments(state, [])

    with p1, p2, p3:
        with thesis_state_session(state_path=isolate["state_path"]) as state:
            assert "USD_JPY" not in state["instruments"]

            observe_thesis(
                state=state,
                instrument="USD_JPY",
                direction_sign=-1,
                entry_price_0=160.0,
                entry_time="2026-08-20T05:00:00+00:00",
                current_price=160.0,
                unrealized_r=0.0,
                cycle_id="cB",
                strength_matrix={"USD": -1.0, "JPY": 0.0},
            )

    final = load_thesis_state(isolate["state_path"])

    assert final["instruments"]["USD_JPY"]["snapshot"]["entry_price_0"] == 160.0
    assert (
        final["instruments"]["USD_JPY"]["confirmation"]["last_classification"]
        == ThesisState.UNCLASSIFIED.value
    )


def _build_real_cluster():
    from utils.pyramid_cluster import PyramidCluster

    cluster = PyramidCluster(
        initial_size=10000,
        entry_price=190.000,
        direction=1,
        atr_entry=0.300,
        entry_time=datetime(
            2026, 8, 20, 0, 0, 0, tzinfo=timezone.utc
        ),
        initial_trade_id="T-TEST-001",
    )
    return cluster


def test_observe_thesis_does_not_mutate_risk_manager_state(isolate):
    cluster = _build_real_cluster()
    rm = cluster.risk_manager

    before = {
        "state": rm.state,
        "entry_time": rm.entry_time,
        "entry_price_0": rm.entry_price_0,
        "r_unit_0": rm.r_unit_0,
        "chandelier_k": rm.chandelier_k,
        "time_reduce_fired": rm.time_reduce_fired,
        "current_sl": rm.current_sl,
        "direction": rm.direction,
    }

    unit_count_before = len(cluster.units)

    p1, p2, p3 = _mock_features("SELL")

    with p1, p2, p3:
        with thesis_state_session(state_path=isolate["state_path"]) as state:
            observe_thesis(
                state=state,
                instrument="GBP_JPY",
                direction_sign=rm.direction,
                entry_price_0=rm.entry_price_0,
                entry_time=str(rm.entry_time),
                current_price=189.0,
                unrealized_r=rm.unrealized_r(189.0),
                cycle_id="c0",
                strength_matrix={"GBP": -1.0, "JPY": 0.0},
            )

    assert rm.state == before["state"]
    assert rm.entry_time == before["entry_time"]
    assert rm.entry_price_0 == before["entry_price_0"]
    assert rm.r_unit_0 == before["r_unit_0"]
    assert rm.chandelier_k == before["chandelier_k"]
    assert rm.time_reduce_fired == before["time_reduce_fired"]
    assert rm.current_sl == before["current_sl"]
    assert rm.direction == before["direction"]
    assert len(cluster.units) == unit_count_before


def test_observe_thesis_never_calls_oanda_execution_functions(isolate):
    cluster = _build_real_cluster()
    rm = cluster.risk_manager

    def _boom(*a, **kw):
        raise AssertionError(
            "observe_thesis reached an OANDA execution function — FORBIDDEN"
        )

    p1, p2, p3 = _mock_features("SELL")

    with p1, p2, p3, \
         patch("utils.trading_core.close_position", side_effect=_boom), \
         patch("utils.risk_integration.apply_risk_action", side_effect=_boom):
        with thesis_state_session(state_path=isolate["state_path"]) as state:
            observe_thesis(
                state=state,
                instrument="GBP_JPY",
                direction_sign=rm.direction,
                entry_price_0=rm.entry_price_0,
                entry_time=str(rm.entry_time),
                current_price=150.0,
                unrealized_r=rm.unrealized_r(150.0),
                cycle_id="c0",
                strength_matrix={"GBP": -5.0, "JPY": 0.0},
            )


def test_observe_thesis_return_value_is_none():
    p1, p2, p3 = _mock_features("BUY")

    with p1, p2, p3:
        result = observe_thesis(
            state={"schema_version": 1, "instruments": {}},
            instrument="GBP_JPY",
            direction_sign=1,
            entry_price_0=190.0,
            entry_time="t",
            current_price=190.0,
            unrealized_r=0.0,
            cycle_id="c0",
            strength_matrix={"GBP": 1.0, "JPY": 0.0},
        )

    assert result is None


_CYCLE_SCRIPT = textwrap.dedent("""
    import sys, json
    sys.path.insert(0, {repo_root!r})

    from unittest.mock import patch
    from utils.thesis_state_store import thesis_state_session
    from utils.thesis_observation import observe_thesis

    with patch("utils.strategy_helpers.check_ma5_alignment", return_value={alignment!r}), \\
         patch("utils.strategy_helpers.get_slope_diagnostics", return_value={{"combined": "AGAINST"}}), \\
         patch("utils.strategy_helpers.get_atr_with_volatility_context", return_value=(0.5, 1.0)):

        with thesis_state_session(state_path={state_path!r}) as state:
            observe_thesis(
                state=state,
                instrument={instrument!r},
                direction_sign={direction_sign!r},
                entry_price_0={entry_price_0!r},
                entry_time={entry_time!r},
                current_price={current_price!r},
                unrealized_r={unrealized_r!r},
                cycle_id={cycle_id!r},
                strength_matrix={{
                    "GBP": {strength!r},
                    "JPY": 0.0,
                    "USD": {strength!r}
                }},
            )

    with open({state_path!r}) as f:
        print(json.dumps(json.load(f)))
""")


def _subprocess_env(extra=None):
    env = os.environ.copy()
    env.setdefault("GEMINI_API_KEY", "dummy-test-key-not-real")
    env.setdefault("OANDA_ACCOUNT_ID", "dummy-account")
    env.setdefault("OANDA_API_TOKEN", "dummy-token")
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    if extra:
        env.update(extra)
    return env


def _run_cycle_subprocess(
    state_path,
    instrument,
    cycle_id,
    strength,
    alignment,
    direction_sign=1,
    entry_price_0=190.0,
    entry_time="2026-08-20T00:00:00+00:00",
    current_price=190.0,
    unrealized_r=0.0,
):
    script = _CYCLE_SCRIPT.format(
        repo_root=REPO_ROOT,
        state_path=state_path,
        instrument=instrument,
        cycle_id=cycle_id,
        strength=strength,
        alignment=alignment,
        direction_sign=direction_sign,
        entry_price_0=entry_price_0,
        entry_time=entry_time,
        current_price=current_price,
        unrealized_r=unrealized_r,
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"subprocess failed:\n"
        f"STDOUT:{result.stdout}\n"
        f"STDERR:{result.stderr}"
    )

    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.subprocess
def test_1_first_observation_via_real_subprocess(tmp_path):
    state_path = str(tmp_path / "sub_state.json")

    state = _run_cycle_subprocess(
        state_path,
        "GBP_JPY",
        "cycle-A",
        strength=1.0,
        alignment="BUY",
    )

    conf = state["instruments"]["GBP_JPY"]["confirmation"]

    assert conf["consecutive_deteriorating"] == 0
    assert conf["last_classification"] == "UNCLASSIFIED"
    assert conf["last_classification"] != "THESIS_INTACT"
    assert "snapshot" in state["instruments"]["GBP_JPY"]


@pytest.mark.subprocess
def test_2_snapshot_survives_real_process_restart(tmp_path):
    state_path = str(tmp_path / "sub_state.json")

    state_a = _run_cycle_subprocess(
        state_path,
        "GBP_JPY",
        "cycle-A",
        strength=1.0,
        alignment="BUY",
    )

    snap_a = state_a["instruments"]["GBP_JPY"]["snapshot"]

    state_b = _run_cycle_subprocess(
        state_path,
        "GBP_JPY",
        "cycle-B",
        strength=-1.0,
        alignment="SELL",
    )

    snap_b = state_b["instruments"]["GBP_JPY"]["snapshot"]

    assert snap_b["strength_score"] == snap_a["strength_score"] == pytest.approx(1.0)
    assert (
        snap_b["first_observed_cycle_id"]
        == snap_a["first_observed_cycle_id"]
        == "cycle-A"
    )


@pytest.mark.subprocess
def test_3_genuine_cross_cycle_comparison_not_self_comparison(tmp_path):
    state_path = str(tmp_path / "sub_state.json")

    _run_cycle_subprocess(
        state_path,
        "GBP_JPY",
        "cycle-A",
        strength=1.0,
        alignment="BUY",
    )

    state_b = _run_cycle_subprocess(
        state_path,
        "GBP_JPY",
        "cycle-B",
        strength=-1.0,
        alignment="SELL",
    )

    snap = state_b["instruments"]["GBP_JPY"]["snapshot"]
    conf = state_b["instruments"]["GBP_JPY"]["confirmation"]

    assert snap["first_observed_cycle_id"] == "cycle-A"
    assert conf["last_updated_cycle_id"] == "cycle-B"
    assert snap["first_observed_cycle_id"] != conf["last_updated_cycle_id"]
    assert conf["last_classification"] == "THESIS_INVALIDATED"


@pytest.mark.subprocess
def test_4_confirmation_reaches_3_across_four_subprocesses(tmp_path):
    state_path = str(tmp_path / "sub_state.json")

    _run_cycle_subprocess(
        state_path, "GBP_JPY", "cycle-A",
        strength=1.0, alignment="BUY"
    )

    _run_cycle_subprocess(
        state_path, "GBP_JPY", "cycle-B",
        strength=-1.0, alignment="SELL"
    )

    _run_cycle_subprocess(
        state_path, "GBP_JPY", "cycle-C",
        strength=-1.0, alignment="SELL"
    )

    state_d = _run_cycle_subprocess(
        state_path, "GBP_JPY", "cycle-D",
        strength=-1.0, alignment="SELL"
    )

    conf = state_d["instruments"]["GBP_JPY"]["confirmation"]

    assert conf["consecutive_deteriorating"] == 3
    assert conf["last_classification"] == "CONFIRMED_DETERIORATION"


@pytest.mark.subprocess
def test_5_snapshot_immutable_across_subprocesses(tmp_path):
    state_path = str(tmp_path / "sub_state.json")

    state_a = _run_cycle_subprocess(
        state_path,
        "GBP_JPY",
        "cycle-A",
        strength=1.0,
        alignment="BUY",
    )

    baseline_strength = (
        state_a["instruments"]["GBP_JPY"]["snapshot"]["strength_score"]
    )

    for cycle, strength in [
        ("cycle-B", -1.0),
        ("cycle-C", 0.3),
        ("cycle-D", -2.5),
    ]:
        state_n = _run_cycle_subprocess(
            state_path,
            "GBP_JPY",
            cycle,
            strength=strength,
            alignment="SELL",
        )

        assert (
            state_n["instruments"]["GBP_JPY"]["snapshot"]["strength_score"]
            == baseline_strength
        )


@pytest.mark.subprocess
def test_6_corrupt_state_file_real_subprocess(tmp_path):
    state_path = str(tmp_path / "sub_state.json")

    with open(state_path, "w") as f:
        f.write("{{{not valid json")

    state = _run_cycle_subprocess(
        state_path,
        "GBP_JPY",
        "cycle-A",
        strength=1.0,
        alignment="BUY",
    )

    assert "GBP_JPY" in state["instruments"]

    quarantined = [
        f for f in os.listdir(tmp_path)
        if "corrupt" in f
    ]

    assert len(quarantined) == 1


@pytest.mark.subprocess
def test_7_missing_state_file_real_subprocess(tmp_path):
    state_path = str(tmp_path / "does_not_exist_yet.json")

    assert not os.path.exists(state_path)

    state = _run_cycle_subprocess(
        state_path,
        "GBP_JPY",
        "cycle-A",
        strength=1.0,
        alignment="BUY",
    )

    assert (
        state["instruments"]["GBP_JPY"]["confirmation"]["last_classification"]
        == "UNCLASSIFIED"
    )


@pytest.mark.subprocess
def test_8_orphan_cleanup_real_subprocess(tmp_path):
    state_path = str(tmp_path / "sub_state.json")

    _run_cycle_subprocess(
        state_path,
        "GBP_JPY",
        "cycle-A",
        strength=1.0,
        alignment="BUY",
    )

    cleanup_script = textwrap.dedent(f"""
        import sys, json
        sys.path.insert(0, {REPO_ROOT!r})

        from utils.thesis_state_store import (
            thesis_state_session,
            cleanup_orphaned_instruments,
        )

        with thesis_state_session(state_path={state_path!r}) as state:
            cleanup_orphaned_instruments(state, [])

        with open({state_path!r}) as f:
            print(json.dumps(json.load(f)))
    """)

    result = subprocess.run(
        [sys.executable, "-c", cleanup_script],
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr

    state = json.loads(result.stdout.strip().splitlines()[-1])

    assert "GBP_JPY" not in state["instruments"]


@pytest.mark.subprocess
def test_9_same_instrument_new_thesis_real_subprocess(tmp_path):
    state_path = str(tmp_path / "sub_state.json")

    _run_cycle_subprocess(
        state_path,
        "USD_JPY",
        "cycle-A",
        strength=1.0,
        alignment="BUY",
        entry_price_0=150.0,
    )

    cleanup_script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {REPO_ROOT!r})

        from utils.thesis_state_store import (
            thesis_state_session,
            cleanup_orphaned_instruments,
        )

        with thesis_state_session(state_path={state_path!r}) as state:
            cleanup_orphaned_instruments(state, [])
    """)

    subprocess.run(
        [sys.executable, "-c", cleanup_script],
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )

    state_b = _run_cycle_subprocess(
        state_path,
        "USD_JPY",
        "cycle-B",
        strength=-1.0,
        alignment="SELL",
        direction_sign=-1,
        entry_price_0=160.0,
    )

    snap_b = state_b["instruments"]["USD_JPY"]["snapshot"]
    conf_b = state_b["instruments"]["USD_JPY"]["confirmation"]

    assert snap_b["entry_price_0"] == 160.0
    assert snap_b["first_observed_cycle_id"] == "cycle-B"
    assert conf_b["last_classification"] == "UNCLASSIFIED"