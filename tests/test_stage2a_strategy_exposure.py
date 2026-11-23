"""Stage 2a tests — shared `_strategy_exposure` ownership/exposure helper.

Stage 2a unifies the ownership + exposure bookkeeping that used to live in two
separate places, so the live entry paths cannot drift apart:

  • `_build_open_exposure()`  (basket/decision view: net exposure + held set)
  • `_execute_single_signal()` (execution view: strategy-open count + same-pair)

Both now delegate to `_strategy_exposure(trades) -> (net, count, eligible)`.

Reference oracles below are VERBATIM copies of the pre-refactor loops; the tests
assert helper output == oracle output on hand-crafted edge cases and on seeded
random trade lists. No broker, network, or trading state is touched.
"""

import os
import random
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import scheduled_runner_v3 as runner  # noqa: E402

_ROOT = "GEMINIAIBOT_V3::"


# ---------------------------------------------------------------------------
# Reference oracles — verbatim pre-refactor logic (do NOT "improve" these)
# ---------------------------------------------------------------------------
def _oracle_build_open_exposure(trades):
    """Copy of the pre-refactor `_build_open_exposure` body."""
    net = {}
    held = set()
    _prefixes = {cfg["tag_prefix"] for cfg in runner._strategy_groups.values()}
    for t in trades:
        if not runner.is_bot_owned_trade(t):
            continue
        tag = t.get("clientExtensions", {}).get("tag", "") or ""
        raw_tag = runner._extract_raw_tag(tag)
        if not any(p in raw_tag for p in _prefixes):
            continue
        inst = t["instrument"]
        base, quote = inst.split("_")
        units = float(t.get("currentUnits", 0))
        s = 1 if units > 0 else -1
        net[base] = net.get(base, 0) + s
        net[quote] = net.get(quote, 0) - s
        held.add((inst, s))
    return net, held


def _oracle_execute_scan(trades, pair):
    """Copy of the pre-refactor `_execute_single_signal` scan loop."""
    _prefixes = {cfg["tag_prefix"] for cfg in runner._strategy_groups.values()}
    _strategy_open = 0
    _bot_has_this_pair = False
    for t in trades:
        if not runner.is_bot_owned_trade(t):
            continue
        tag = t.get("clientExtensions", {}).get("tag", "") or ""
        raw_tag = runner._extract_raw_tag(tag)
        if any(p in raw_tag for p in _prefixes):
            _strategy_open += 1
            if t.get("instrument") == pair:
                _bot_has_this_pair = True
    return _strategy_open, _bot_has_this_pair


# ---------------------------------------------------------------------------
# Fixtures — deterministic trade lists covering every documented edge case
# ---------------------------------------------------------------------------
_HANDCRAFTED = [
    # (1) bot-owned, prefix at the START
    {
        "id": "1",
        "instrument": "USD_JPY",
        "currentUnits": "1000",
        "clientExtensions": {"tag": f"{_ROOT}JPY-STRENGTH_USD_JPY_BUY_1"},
    },
    # (2) bot-owned, prefix appears MID-STRING (substring case, not startswith)
    {
        "id": "2",
        "instrument": "EUR_JPY",
        "currentUnits": "-2000",
        "clientExtensions": {"tag": f"{_ROOT}XX-JPY-STRENGTH_EUR_JPY_SELL_2"},
    },
    # (3) bot-owned but NO known prefix substring -> excluded
    {
        "id": "3",
        "instrument": "GBP_USD",
        "currentUnits": "1000",
        "clientExtensions": {"tag": f"{_ROOT}NOT-A-GROUP_GBP_USD"},
    },
    # (4) manual trade (not bot-owned) -> excluded
    {
        "id": "4",
        "instrument": "AUD_USD",
        "currentUnits": "1000",
        "clientExtensions": {"tag": "manual_abc"},
    },
    # (5) no clientExtensions at all -> excluded
    {"id": "5", "instrument": "USD_CHF", "currentUnits": "1000"},
    # (6) second USD_JPY trade (multiple trades per pair), opposite direction
    {
        "id": "6",
        "instrument": "USD_JPY",
        "currentUnits": "-500",
        "clientExtensions": {"tag": f"{_ROOT}USD-STRENGTH_USD_JPY_SELL_3"},
    },
    # (7) another USD-quote trade -> net accumulation
    {
        "id": "7",
        "instrument": "GBP_USD",
        "currentUnits": "3000",
        "clientExtensions": {"tag": f"{_ROOT}USD-STRENGTH_GBP_USD_BUY_4"},
    },
]


def _gen_trades(rng, n):
    """Seeded generator: well-formed records only, all tag/ownership variants."""
    instruments = ["USD_JPY", "EUR_JPY", "GBP_USD", "EUR_USD", "USD_CHF", "AUD_USD"]
    units_pool = ["1000", "-1000", "500", "-500", "2500"]
    tag_makers = [
        lambda inst: f"{_ROOT}JPY-STRENGTH_{inst}_BUY",  # start
        lambda inst: f"{_ROOT}XX-USD-STRENGTH_{inst}_X",  # mid-string
        lambda inst: f"{_ROOT}CHF-STRENGTH_{inst}_SELL",  # start
        lambda inst: f"{_ROOT}UNMATCHED_{inst}",  # unmatched prefix
        lambda inst: f"manual_{inst}",  # not bot-owned
    ]
    out = []
    for i in range(n):
        inst = rng.choice(instruments)
        t = {
            "id": str(i),
            "instrument": inst,
            "currentUnits": rng.choice(units_pool),
        }
        if rng.random() >= 0.15:  # ~15% have no clientExtensions
            t["clientExtensions"] = {"tag": rng.choice(tag_makers)(inst)}
        out.append(t)
    return out


# ---------------------------------------------------------------------------
# Parity — helper vs. reference oracles
# ---------------------------------------------------------------------------
def test_parity_handcrafted_edges():
    net, count, eligible = runner._strategy_exposure(_HANDCRAFTED)
    o_net, o_held = _oracle_build_open_exposure(_HANDCRAFTED)
    o_count, _ = _oracle_execute_scan(_HANDCRAFTED, "__NO_SUCH_PAIR__")

    # Old decision view.
    assert net == o_net
    # Old execution view.
    assert count == o_count
    assert len(eligible) == count
    # eligible -> held reproduces the old held set.
    held = {
        (t["instrument"], 1 if float(t.get("currentUnits", 0)) > 0 else -1)
        for t in eligible
    }
    assert held == o_held


def test_parity_handcrafted_concrete_values():
    """Concrete expectations so a shared bug in both helper and oracle is caught."""
    net, count, eligible = runner._strategy_exposure(_HANDCRAFTED)
    assert count == 4  # ids 1, 2, 6, 7
    assert {t["id"] for t in eligible} == {"1", "2", "6", "7"}
    # id1 USD_JPY +1k: USD +1 / JPY -1 ; id2 EUR_JPY -2k: EUR -1 / JPY +1
    # id6 USD_JPY -500: USD -1 / JPY +1 ; id7 GBP_USD +3k: GBP +1 / USD -1
    assert net == {"USD": -1, "JPY": 1, "EUR": -1, "GBP": 1}


@pytest.mark.parametrize("seed", list(range(10)))
def test_parity_seeded_random_lists(seed):
    trades = _gen_trades(random.Random(seed), 40)

    net, count, eligible = runner._strategy_exposure(trades)
    o_net, o_held = _oracle_build_open_exposure(trades)
    o_count, _ = _oracle_execute_scan(trades, "__NO_SUCH_PAIR__")

    assert net == o_net
    assert count == o_count
    held = {
        (t["instrument"], 1 if float(t.get("currentUnits", 0)) > 0 else -1)
        for t in eligible
    }
    assert held == o_held


@pytest.mark.parametrize("seed", list(range(10)))
def test_parity_same_pair_check(seed):
    trades = _gen_trades(random.Random(seed), 40)
    _, _, eligible = runner._strategy_exposure(trades)
    for pair in ("USD_JPY", "EUR_JPY", "AUD_CAD", "GBP_USD"):
        helper_has = any(t.get("instrument") == pair for t in eligible)
        _, oracle_has = _oracle_execute_scan(trades, pair)
        assert helper_has == oracle_has


# ---------------------------------------------------------------------------
# Callers actually use the helper (behavior preserved)
# ---------------------------------------------------------------------------
def test_build_open_exposure_delegates_and_matches_oracle(monkeypatch):
    monkeypatch.setattr(runner, "_all_open_trades_snapshot", lambda: _HANDCRAFTED)
    net, held = runner._build_open_exposure()
    o_net, o_held = _oracle_build_open_exposure(_HANDCRAFTED)
    assert net == o_net
    assert held == o_held


def _entry(pair, action="BUY", tag_prefix="JPY-STRENGTH", group_name="JPY"):
    return {
        "signal": {
            "pair": pair,
            "action": action,
            "entry": 1.0,
            "stop_loss": 0.99,
            "take_profit": 1.02,
            "risk_reward": 2.0,
            "strength_score": 1.5,
        },
        "tag_prefix": tag_prefix,
        "group_name": group_name,
    }


def test_execute_single_signal_max_positions_uses_shared_count(monkeypatch):
    monkeypatch.setattr(runner, "SINGLE_HOST_GUARD_ENABLED", False)
    monkeypatch.setattr(runner, "_safe_emit", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_MAX_OPEN_POSITIONS", 1)
    monkeypatch.setattr(runner, "_all_open_trades_snapshot", lambda: _HANDCRAFTED)

    ok, reason = runner._execute_single_signal(
        _entry("AUD_CAD"), dry_run=False, global_scores={}
    )
    assert ok is False
    assert reason == "MAX_POSITIONS"  # 4 eligible >= cap of 1


def test_execute_single_signal_already_held_uses_shared_population(monkeypatch):
    monkeypatch.setattr(runner, "SINGLE_HOST_GUARD_ENABLED", False)
    monkeypatch.setattr(runner, "_safe_emit", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_MAX_OPEN_POSITIONS", 999)
    monkeypatch.setattr(runner, "_all_open_trades_snapshot", lambda: _HANDCRAFTED)

    ok, reason = runner._execute_single_signal(
        _entry("USD_JPY"), dry_run=False, global_scores={}
    )
    assert ok is False
    assert reason == "ALREADY_HELD"  # USD_JPY is bot-owned/eligible


class _ReachedDownstream(Exception):
    """Sentinel: proves the guard gates were passed and the order path began."""


def test_execute_single_signal_manual_only_does_not_block(monkeypatch):
    manual = [
        {
            "id": "m1",
            "instrument": "AUD_CAD",
            "currentUnits": "1000",
            "clientExtensions": {"tag": "manual_x"},
        }
    ]
    monkeypatch.setattr(runner, "SINGLE_HOST_GUARD_ENABLED", False)
    monkeypatch.setattr(runner, "_safe_emit", lambda *a, **k: None)
    # cap of 1 would fire IF manual trades were (wrongly) counted.
    monkeypatch.setattr(runner, "_MAX_OPEN_POSITIONS", 1)
    monkeypatch.setattr(runner, "_all_open_trades_snapshot", lambda: manual)

    def _stop(*a, **k):
        raise _ReachedDownstream()

    monkeypatch.setattr(runner, "make_strategy_tag", _stop)

    # Manual-only book -> not counted -> passes the cap/idempotency gates and
    # reaches the downstream order path (sentinel). Never MAX_POSITIONS/ALREADY_HELD.
    with pytest.raises(_ReachedDownstream):
        runner._execute_single_signal(
            _entry("AUD_CAD"), dry_run=False, global_scores={}
        )


# ---------------------------------------------------------------------------
# Fail-closed on malformed ELIGIBLE records (matches old _build_open_exposure)
# ---------------------------------------------------------------------------
def test_malformed_non_eligible_records_are_skipped_not_raised():
    # Unmatched/absent tags are excluded BEFORE any field is parsed.
    trades = [
        {"id": "n", "clientExtensions": {"tag": f"{_ROOT}UNMATCHED"}},
        {"id": "m"},
    ]
    net, count, eligible = runner._strategy_exposure(trades)
    assert (net, count, eligible) == ({}, 0, [])


@pytest.mark.parametrize(
    "bad",
    [
        # missing instrument
        {"id": "b1", "clientExtensions": {"tag": f"{_ROOT}JPY-STRENGTH_X"}},
        # un-splittable pair
        {
            "id": "b2",
            "instrument": "USDJPY",
            "currentUnits": "1000",
            "clientExtensions": {"tag": f"{_ROOT}JPY-STRENGTH_X"},
        },
        # non-numeric units
        {
            "id": "b3",
            "instrument": "USD_JPY",
            "currentUnits": "oops",
            "clientExtensions": {"tag": f"{_ROOT}JPY-STRENGTH_X"},
        },
    ],
)
def test_malformed_eligible_records_fail_closed(bad):
    with pytest.raises(Exception):
        runner._strategy_exposure([bad])


def test_documented_discrepancy_execute_scan_leniency_is_gone():
    """Pre-refactor divergence, pinned deliberately.

    The old `_execute_single_signal` scan never parsed ``currentUnits``, so an
    eligible record with non-numeric units was COUNTED (+1) there while
    `_build_open_exposure` raised. The unified helper now fails closed in BOTH
    callers (stricter, and consistent with the fail-closed HARD RULE). This test
    documents that intentional behavior change so it is not silently reverted.
    """
    bad = [
        {
            "id": "b",
            "instrument": "USD_JPY",
            "currentUnits": "oops",
            "clientExtensions": {"tag": f"{_ROOT}JPY-STRENGTH_X"},
        }
    ]
    # Old execution scan tolerated it:
    cnt, has = _oracle_execute_scan(bad, "EUR_USD")
    assert cnt == 1 and has is False
    # New shared helper fails closed instead:
    with pytest.raises(ValueError):
        runner._strategy_exposure(bad)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
