"""
Tests for scheduled_runner_v3._pick_global_basket() exposure logic.

Scope — these tests pin down the P0-A fix:

  * The thesis-contradiction check ("OPPOSES") is enforced on the BASE currency only.
    The quote-side sign is structural (every BUY shorts its quote), so checking it
    wrongly vetoed legitimate structures such as EUR_USD BUY + USD_JPY BUY.

  * The concentration cap ("NET CAP", CROSS_MAX_NET_PER_CCY) is still enforced on
    BOTH base and quote sides, so the exposure-concentration protection is intact.

  * A genuine base-currency thesis clash is still rejected.

These tests DO NOT touch a broker, the network, or any trading state. `_pick_global_basket`
is a pure function: its entire input is (all_results, max_entries, open_net, held) and
its entire output is the returned basket. The runner module is loaded with config and
TradingCore dependencies stubbed so that importing it has no side effects.

Per project rules: this file only asserts behaviour that the runner actually exhibits.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

RUNNER_PATH = Path(__file__).resolve().parents[1] / "scheduled_runner_v3.py"


# ---------------------------------------------------------------------------
# Module loading — stub the import-time dependencies of scheduled_runner_v3
# ---------------------------------------------------------------------------
def _make_config_oanda_module():
    mod = types.ModuleType("config_oanda")
    mod.get_oanda_profile = lambda env: {
        "env": env,
        "token": "fake_token",
        "oanda_client": object(),
        "api": None,
        "account_ids": ["101-003-0000000-001"],
    }
    mod.OANDA_ACCOUNT_ID_2 = "101-003-0000000-001"
    mod.is_market_open = lambda instrument: True
    sys.modules["config_oanda"] = mod
    return mod


def _make_trading_core_module():
    mod = types.ModuleType("utils.trading_core_v2")

    class _TradingCore:
        def __init__(self, *args, **kwargs):
            pass

    mod.TradingCore = _TradingCore
    mod.close_pair_position = lambda *args, **kwargs: (True, "stub")
    sys.modules["utils.trading_core_v2"] = mod
    return mod


def _make_skipped_module(name, **attrs):
    """Register a stub module so importing the runner has no heavy side effects."""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _make_config_bot_v3_module():
    """Minimal stand-in for config_bot_v3 — only what the runner reads at import."""
    mod = types.ModuleType("config_bot_v3")
    mod.PROFILE_CFG = {"profile2": {}}
    mod.load_profile = lambda name: dict(mod.PROFILE_CFG.get(name, {}))
    mod.STRATEGY_GROUPS = {
        "JPY": {"quote_ccy": "JPY", "tag_prefix": "JPY-STRENGTH"},
        "USD": {"quote_ccy": "USD", "tag_prefix": "USD-STRENGTH"},
    }
    mod.PIP_SIZE_BY_QUOTE = {"JPY": 0.01, "USD": 0.0001, "CHF": 0.0001, "GBP": 0.0001}
    mod.STRENGTH_PAIRS = ["EUR_JPY", "USD_JPY", "EUR_USD", "GBP_USD"]
    mod.CROSS_MAX_NET_PER_CCY = 2
    mod.CROSS_MC_SEVERE_WEIGHT = 0.6
    mod.CROSS_MC_MODERATE_WEIGHT = 0.8
    mod.MC_MAX_POSITIONS_NEUTRAL = 2
    mod.MC_MAX_POSITIONS_CONSOLIDATION = 1
    mod.MC_MAX_POSITIONS_AGGRESSIVE = 3
    mod.DEMO_LOT_SIZE = 1000
    mod.LIVE_LOT_SIZE = 1000
    sys.modules["config_bot_v3"] = mod
    return mod


def _make_config_module():
    mod = types.ModuleType("config")
    mod.OANDA_ACCOUNT_ID = ""
    mod.OANDA_ENV = "practice"
    sys.modules["config"] = mod
    return mod


def _load_runner(monkeypatch, max_entries="1"):
    """Import scheduled_runner_v3 with its import-time dependencies stubbed."""
    _make_config_oanda_module()
    _make_trading_core_module()
    _make_config_bot_v3_module()
    _make_config_module()

    # The runner only imports the strategy class + a few helpers; neither is used
    # by _pick_global_basket, so light stubs keep this test offline and dependency-free.
    _make_skipped_module("custom_strategy_v3", BaseCurrencyTrendStrategy=type(
        "BaseCurrencyTrendStrategy", (), {"__init__": lambda self, *a, **k: None}
    ))
    _make_skipped_module(
        "utils.strategy_helpers",
        build_strength_matrix=lambda *a, **k: {},
        format_strength_ranking=lambda *a, **k: "",
        check_ma5_alignment=lambda *a, **k: None,
        check_ma5_cross=lambda *a, **k: None,
        check_macd_histogram=lambda *a, **k: None,
    )
    _make_skipped_module("utils.oanda_state", build_client_extensions=lambda *a, **k: {})
    _make_skipped_module(
        "utils.utils",
        acquire_profile_lock=lambda *a, **k: None,
        check_pair_level_strategy_position=lambda *a, **k: None,
        is_strategy_trade=lambda trade, prefix: prefix in (
            (trade.get("clientExtensions") or {}).get("tag", "") or ""
        ),
        make_strategy_tag=lambda pair, action, prefix: f"{prefix}_{pair}_{action}",
        make_strategy_comment=lambda entry, sl, tp, version: f"v{version}",
    )

    # The runner builds a TradingCore at import time from the profile above.
    monkeypatch.setattr(sys, "argv", ["scheduled_runner_v3.py", "--max-entries", max_entries])

    spec = importlib.util.spec_from_file_location("scheduled_runner_v3_under_test", RUNNER_PATH)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


# ---------------------------------------------------------------------------
# Helpers — build the minimal signal/group shapes _pick_global_basket consumes
# ---------------------------------------------------------------------------
def _signal(pair, action, score):
    return {
        "pair": pair,
        "action": action,
        "strength_score": score,
        "entry": 1.0,
        "stop_loss": 0.99,
        "take_profit": 1.02,
        "risk_reward": 2.0,
    }


def _group(name, tag_prefix, signals):
    return {
        "signals": signals,
        "strategy": None,
        "cfg": {"quote_ccy": name, "tag_prefix": tag_prefix},
        "group_name": name,
        "mc_regime": "NEUTRAL",
    }


def _picked_pairs(basket):
    return [e["signal"]["pair"] for e in basket]


# ---------------------------------------------------------------------------
# Case 1 — legitimate cross-pair structure must NOT be vetoed on the quote side
# ---------------------------------------------------------------------------
def test_quote_side_opposition_does_not_veto(monkeypatch):
    """
    Core P0-A assertion: a quote currency's opposite sign must never veto a signal
    on thesis grounds, even when the pending quote exposure is large and the base
    thesis is neutral (net 0).

    GBP_JPY BUY establishes GBP +1 / JPY -1. EUR_JPY BUY establishes EUR +1 /
    JPY -1. Both JPY entries are quote-side and neither base (GBP, EUR) is opposed,
    so both must be picked. The JPY quote exposure is deliberately far beyond
    CROSS_MAX_NET_PER_CCY to prove the OPPOSES gate does not key off the quote side.

    (The NET CAP guard would reject this in a real run — that is covered separately
    below; here the only gate that could veto is OPPOSES.)
    """
    runner = _load_runner(monkeypatch)

    all_results = [
        _group("JPY", "JPY-STRENGTH", [
            _signal("GBP_JPY", "BUY", 1.8),
            _signal("EUR_JPY", "BUY", 1.6),
        ]),
    ]

    basket = runner._pick_global_basket(
        all_results, max_entries=2, open_net={}, held=set()
    )

    assert _picked_pairs(basket) == ["GBP_JPY", "EUR_JPY"]


def test_quote_side_opposition_does_not_produce_opposes(monkeypatch, capsys):
    """
    Directly pin the P0-A semantics: OPPOSES is reported only for the base
    currency, never for the quote currency.
    """
    runner = _load_runner(monkeypatch)

    all_results = [
        _group("JPY", "JPY-STRENGTH", [
            _signal("GBP_JPY", "BUY", 1.8),
            _signal("EUR_JPY", "BUY", 1.6),
        ]),
    ]

    runner._pick_global_basket(
        all_results, max_entries=2, open_net={}, held=set()
    )

    out = capsys.readouterr().out
    assert "[OPPOSES" not in out


# ---------------------------------------------------------------------------
# Case 2 — concentration cap must still fire (base + quote both accounted)
# ---------------------------------------------------------------------------
def test_net_cap_still_blocks_concentration(monkeypatch):
    """
    With EUR already at +2 net exposure, another long-EUR trade must be rejected
    by the NET CAP guard — proving the cap (not just the thesis check) protects
    against currency concentration.
    """
    runner = _load_runner(monkeypatch)

    all_results = [
        _group("JPY", "JPY-STRENGTH", [_signal("EUR_JPY", "BUY", 1.8)]),
    ]

    basket = runner._pick_global_basket(
        all_results, max_entries=1, open_net={"EUR": 2}, held=set()
    )

    assert basket == []


def test_net_cap_blocks_doubled_quote_exposure(monkeypatch):
    """
    EUR_USD BUY + GBP_USD BUY = double short USD. With USD already at the cap,
    both picks are rejected — proving the cap is enforced on the QUOTE side too,
    not just on the base side.

    (Before P0-A this path was unreachable because the thesis gate rejected the
    two candidates first, so the quote-side cap was effectively untested.)
    """
    runner = _load_runner(monkeypatch)

    all_results = [
        _group("USD", "USD-STRENGTH", [
            _signal("EUR_USD", "BUY", 1.8),
            _signal("GBP_USD", "BUY", 1.6),
        ]),
    ]

    basket = runner._pick_global_basket(
        all_results, max_entries=2, open_net={"USD": -2}, held=set()
    )

    assert basket == []


def test_net_cap_allows_quote_exposure_below_cap(monkeypatch):
    """
    Same pair as above but with USD one unit below the cap: the first pick is
    allowed, the second (which would breach the cap) is rejected.
    """
    runner = _load_runner(monkeypatch)

    all_results = [
        _group("USD", "USD-STRENGTH", [
            _signal("EUR_USD", "BUY", 1.8),
            _signal("GBP_USD", "BUY", 1.6),
        ]),
    ]

    basket = runner._pick_global_basket(
        all_results, max_entries=2, open_net={"USD": -1}, held=set()
    )

    assert _picked_pairs(basket) == ["EUR_USD"]


# ---------------------------------------------------------------------------
# Case 3 — a genuine BASE-currency thesis clash must still be rejected
# ---------------------------------------------------------------------------
def test_base_currency_thesis_clash_still_rejected(monkeypatch):
    """
    Existing net EUR +1 (long EUR thesis) vs a new EUR_USD SELL (short EUR
    thesis) — a real contradiction. Must be rejected as OPPOSES.
    """
    runner = _load_runner(monkeypatch)

    all_results = [
        _group("USD", "USD-STRENGTH", [_signal("EUR_USD", "SELL", 1.8)]),
    ]

    basket = runner._pick_global_basket(
        all_results, max_entries=1, open_net={"EUR": 1}, held=set()
    )

    assert basket == []


def test_base_currency_agreement_passes(monkeypatch):
    """
    Existing net EUR +1 and a new EUR_JPY BUY (also long EUR) agree on the base
    thesis, so it must pass the thesis gate (and stay under the cap).
    """
    runner = _load_runner(monkeypatch)

    all_results = [
        _group("JPY", "JPY-STRENGTH", [_signal("EUR_JPY", "BUY", 1.8)]),
    ]

    basket = runner._pick_global_basket(
        all_results, max_entries=1, open_net={"EUR": 1}, held=set()
    )

    assert _picked_pairs(basket) == ["EUR_JPY"]


# ---------------------------------------------------------------------------
# Regression guard — already-held idempotency must keep working
# ---------------------------------------------------------------------------
def test_already_held_pair_is_skipped_and_next_picked(monkeypatch):
    """
    A duplicate of an already-held position is skipped via `continue`, so the
    next-ranked signal is picked rather than leaving an empty basket.
    """
    runner = _load_runner(monkeypatch)

    all_results = [
        _group("USD", "USD-STRENGTH", [_signal("EUR_USD", "BUY", 1.8)]),
        _group("JPY", "JPY-STRENGTH", [_signal("EUR_JPY", "BUY", 1.6)]),
    ]

    basket = runner._pick_global_basket(
        all_results, max_entries=1, open_net={}, held={("EUR_USD", 1)}
    )

    assert _picked_pairs(basket) == ["EUR_JPY"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])