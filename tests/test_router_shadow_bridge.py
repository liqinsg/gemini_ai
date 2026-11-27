"""Tests for ``router_shadow_bridge`` — Step 3a read-only Router shadow.

Scope — the bridge is the ONLY contact point between the (not-yet-wired)
GlobalPortfolioRouter and the live runner.  Its entire value rests on two
contracts:

  * it faithfully MAPS runner data into router inputs (pair/side/strength and,
    since Step 3a/B, real atr/spread/entry -> space_score/cost_score),
    flagging — never faking — the fields the runner signal does not carry yet
    (returns_series);
  * it is READ-ONLY and FAIL-SAFE: no orders, no runner-state mutation, and any
    internal error is swallowed (returns None) so a live cycle can never break.

These tests touch no broker, no network and no trading state.  The router and
the bridge are loaded straight from file (``importlib``) with no config stubs,
so the file is order-independent.
"""

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROUTER_PATH = ROOT / "global_portfolio_router_v1_1_5.py"
BRIDGE_PATH = ROOT / "router_shadow_bridge.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bridge():
    # Register the router under its real module name FIRST so the bridge's
    # top-level ``import global_portfolio_router_v1_1_5 as gpr`` resolves to the
    # file we just loaded (no reliance on sys.path / cwd).
    _load("global_portfolio_router_v1_1_5", ROUTER_PATH)
    return _load("router_shadow_bridge", BRIDGE_PATH)


# ---------------------------------------------------------------------------
# Helpers — the minimal shapes the runner hands to _pick_global_basket
# ---------------------------------------------------------------------------
def _sig(pair, action, score, atr=None, spread=None, entry=None):
    s = {"pair": pair, "action": action, "strength_score": score}
    # Only attach the 3a/B fields when given, so tests can exercise both the
    # "runner does not carry them yet" path and the real-data path.
    if atr is not None:
        s["atr"] = atr
    if spread is not None:
        s["spread"] = spread
    if entry is not None:
        s["entry"] = entry
    return s


def _group(name, sigs, tag_prefix="PFX"):
    return {"group_name": name, "signals": sigs, "cfg": {"tag_prefix": tag_prefix}}


# ---------------------------------------------------------------------------
# build_candidates — faithful mapping + explicit data_gaps
# ---------------------------------------------------------------------------
def test_build_candidates_maps_pairs_sides_and_scores(bridge):
    all_results = [
        _group("USD", [_sig("EUR_USD", "BUY", 0.5), _sig("GBP_USD", "SELL", 0.3)]),
        _group("JPY", [_sig("USD_JPY", "BUY", 0.9)]),
    ]
    cands, gaps = bridge.build_candidates(all_results)

    assert [c.pair for c in cands] == ["EUR/USD", "GBP/USD", "USD/JPY"]
    assert [c.side for c in cands] == ["LONG", "SHORT", "LONG"]
    assert [c.raw_score for c in cands] == [0.5, 0.3, 0.9]
    assert gaps == {"space_cost_missing": 3, "returns_missing": 3, "bad_signal": 0}


def test_build_candidates_uses_neutral_defaults_for_missing_fields(bridge):
    cands, _ = bridge.build_candidates([_group("USD", [_sig("EUR_USD", "BUY", 0.4)])])
    c = cands[0]
    # Neutral defaults for what the runner signal does not carry today.
    assert c.space_score == 0.0
    assert c.cost_score == 0.0
    assert len(c.returns_series) == 0
    assert c.atr == 0.0
    assert c.timeframe == "H1"


def test_build_candidates_skips_bad_signals_and_counts_them(bridge):
    all_results = [
        _group(
            "USD",
            [
                {"pair": "EUR_USD", "action": "HOLD", "strength_score": 1.0},  # bad action
                {"pair": "", "action": "BUY", "strength_score": 1.0},  # empty pair
                {"pair": "EUR/USD/USD", "action": "BUY", "strength_score": 1.0},  # bad pair
                _sig("EUR_USD", "BUY", 0.4),  # the only good one
            ],
        )
    ]
    cands, gaps = bridge.build_candidates(all_results)

    assert len(cands) == 1
    assert cands[0].pair == "EUR/USD"
    assert gaps["bad_signal"] == 3
    assert gaps["space_cost_missing"] == 1
    assert gaps["returns_missing"] == 1


def test_build_candidates_handles_none(bridge):
    cands, gaps = bridge.build_candidates(None)
    assert cands == []
    assert gaps == {"space_cost_missing": 0, "returns_missing": 0, "bad_signal": 0}


# --- Step 3a/B: real atr/spread/entry -> space_score / cost_score -----------
def test_build_candidates_computes_space_and_cost_from_live_data(bridge):
    all_results = [
        _group(
            "USD",
            [_sig("EUR_USD", "BUY", 0.5, atr=0.0010, spread=0.00012, entry=1.1000)],
        )
    ]
    cands, gaps = bridge.build_candidates(all_results)
    c = cands[0]

    assert c.atr == pytest.approx(0.0010)
    # space_score = clamp((atr/entry) / SPACE_REL_CAP, 0, 1)
    assert c.space_score == pytest.approx(
        0.0010 / 1.1000 / bridge.SPACE_REL_CAP, abs=1e-9
    )
    # cost_score delegates to the router's own live_cost_score (口径自洽)
    assert c.cost_score == pytest.approx(
        bridge.gpr.live_cost_score(0.00012, 0.0010), abs=1e-9
    )
    # atr present -> not a space/cost gap; returns_series still uncarried
    assert gaps["space_cost_missing"] == 0
    assert gaps["returns_missing"] == 1


def test_build_candidates_space_score_saturates_at_cap(bridge):
    # atr/entry = 1.0% > SPACE_REL_CAP (0.5%) -> clamped to 1.0
    all_results = [
        _group(
            "USD",
            [_sig("EUR_USD", "BUY", 0.5, atr=0.011, spread=0.0001, entry=1.1000)],
        )
    ]
    cands, _ = bridge.build_candidates(all_results)
    assert cands[0].space_score == 1.0


def test_build_candidates_atr_without_entry_keeps_cost_only(bridge):
    # atr present but entry missing -> space_score falls back, cost_score still real
    all_results = [
        _group("USD", [_sig("EUR_USD", "BUY", 0.5, atr=0.0010, spread=0.0001)])
    ]
    cands, gaps = bridge.build_candidates(all_results)
    c = cands[0]
    assert c.space_score == 0.0
    assert c.cost_score == pytest.approx(bridge.gpr.live_cost_score(0.0001, 0.0010))
    assert gaps["space_cost_missing"] == 0


# ---------------------------------------------------------------------------
# build_positions — runner (pair, +/-1) -> router Position
# ---------------------------------------------------------------------------
def test_build_positions_maps_pair_and_side(bridge):
    positions = bridge.build_positions({("EUR_USD", 1), ("USD_JPY", -1)})
    by_pair = {p.pair: p for p in positions}

    assert set(by_pair) == {"EUR/USD", "USD/JPY"}
    assert by_pair["EUR/USD"].side == "LONG"
    assert by_pair["USD/JPY"].side == "SHORT"
    assert all(p.risk_units == 1.0 for p in positions)


def test_build_positions_skips_malformed_items(bridge):
    positions = bridge.build_positions({("EUR_USD", 1), "not-a-tuple"})
    assert [p.pair for p in positions] == ["EUR/USD"]


def test_build_positions_handles_none(bridge):
    assert bridge.build_positions(None) == []


# ---------------------------------------------------------------------------
# run_shadow — comparison record + JSONL landing
# ---------------------------------------------------------------------------
def _basket_entry(pair, action):
    return {"signal": {"pair": pair, "action": action}, "group_name": "G"}


def test_run_shadow_agreed_true_writes_one_record(bridge, tmp_path):
    all_results = [_group("USD", [_sig("EUR_USD", "BUY", 0.9)])]
    basket = [_basket_entry("EUR_USD", "BUY")]

    rec = bridge.run_shadow(
        all_results,
        global_scores={"EUR": 1.0, "USD": -1.0},
        open_net={},
        held=set(),
        basket=basket,
        account="101-003-0000000-001",
        cycle_id="C1",
        log_dir=str(tmp_path),
    )

    assert rec is not None and rec["error"] is None
    assert rec["agreed"] is True
    assert rec["router"]["pair"] == "EUR/USD"
    assert rec["router"]["side"] == "LONG"
    assert rec["n_candidates"] == 1
    assert rec["n_open_positions"] == 0

    files = list(tmp_path.glob("router_shadow_*.jsonl"))
    assert len(files) == 1
    # Fixed name: date + account only — never PID/UUID (data must accumulate).
    assert re.fullmatch(r"router_shadow_\d{8}_101-003-0000000-001\.jsonl", files[0].name)
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["agreed"] is True


def test_run_shadow_agreed_false_when_basket_differs(bridge, tmp_path):
    all_results = [_group("USD", [_sig("EUR_USD", "BUY", 0.9)])]
    basket = [_basket_entry("USD_JPY", "SELL")]

    rec = bridge.run_shadow(
        all_results,
        global_scores={"EUR": 1.0, "USD": -1.0},
        open_net={},
        held=set(),
        basket=basket,
        account="a",
        cycle_id="C2",
        log_dir=str(tmp_path),
    )

    assert rec["agreed"] is False
    assert rec["router"]["pair"] == "EUR/USD"
    assert rec["basket"] == [{"pair": "USD/JPY", "side": "SHORT"}]


def test_run_shadow_is_read_only_on_inputs(bridge, tmp_path):
    all_results = [_group("USD", [_sig("EUR_USD", "BUY", 0.9)])]
    basket = [_basket_entry("EUR_USD", "BUY")]
    import copy

    all_results_before = copy.deepcopy(all_results)
    basket_before = copy.deepcopy(basket)

    bridge.run_shadow(
        all_results, {"EUR": 1.0, "USD": -1.0}, {}, set(), basket,
        account="a", cycle_id="C3", log_dir=str(tmp_path),
    )

    assert all_results == all_results_before
    assert basket == basket_before


def test_run_shadow_swallows_exception_and_logs_error(bridge, tmp_path):
    # A non-mapping ``global_scores`` makes ``dict(...)`` raise inside run_shadow.
    rec = bridge.run_shadow(
        [], object(), {}, set(), [], account="a", cycle_id="C4", log_dir=str(tmp_path)
    )

    assert rec is None  # swallowed: a shadow error must never propagate
    files = list(tmp_path.glob("router_shadow_*.jsonl"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert payload["error"]


def test_run_shadow_scoring_front_end_does_not_place_orders(bridge, tmp_path):
    # Sanity: the record exposes a router view but there is no order/action field
    # and the router decision is informational only.
    rec = bridge.run_shadow(
        [_group("USD", [_sig("EUR_USD", "BUY", 0.9)])],
        {"EUR": 1.0, "USD": -1.0}, {}, set(), [_basket_entry("EUR_USD", "BUY")],
        account="a", cycle_id="C5", log_dir=str(tmp_path),
    )
    assert "action" not in rec
    assert set(rec["router"]) >= {"pair", "side", "final_score", "alignment", "rejected_head"}
