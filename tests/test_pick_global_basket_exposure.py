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


# Module names that ``_load_runner()`` replaces with stand-ins in sys.modules.
_STUBBED_MODULES = (
    "config",
    "config_oanda",
    "config_bot_v3",
    "custom_strategy_v3",
    "utils.strategy_helpers",
    "utils.trading_core_v2",
    "utils.oanda_state",
    "utils.utils",
)

_MISSING = object()


@pytest.fixture(autouse=True)
def _isolate_sys_modules():
    """Undo the ``sys.modules`` stubbing done by ``_load_runner()``.

    Without this, the stand-ins OUTLIVE the test.  pytest collects files
    alphabetically, so every module collected after this one — and every later
    test in this very file — then resolves ``utils.strategy_helpers`` /
    ``config_bot_v3`` to a stub missing most of its attributes (e.g.
    ``get_candles``, ``CURRENCIES``).  That used to fail ~100 unrelated tests
    across the suite while looking like a bug in *their* code.

    Only the stubbed names are restored.  Snapshotting the WHOLE module table
    is not safe here: it would also roll back first-time imports of C-extension
    packages (numpy, yfinance, …), and re-importing those in the same process
    raises ``ImportError: cannot load module more than once per process``.
    """
    saved = {name: sys.modules.get(name, _MISSING) for name in _STUBBED_MODULES}
    yield
    for name, original in saved.items():
        if original is _MISSING:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original


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

        def get_all_open_trades(self):
            # _pick_global_basket is no longer purely functional: it consults the
            # OVERRIDE_MAX_OPEN cap via _count_open_override_trades(), which reads
            # the open-trade snapshot.  These unit tests hold no open trades.
            return []

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


def _make_config_module(monkeypatch):
    """Use the REAL config, overriding only the two values this file cares about.

    The previous version registered a 2-attribute fake ``config``.  That is not
    enough any more: importing ``utils.trading_core`` executes
    ``utils/__init__.py``, which pulls in ``data_provider``/``trading_core``/
    ``ml_confirmation``/…, each of which imports its own names from ``config``
    (``DATA_SOURCE``, ``GEMINI_API_KEY``, …).  The fake raised
    ``ImportError: cannot import name 'DATA_SOURCE'`` whenever ``utils`` had not
    already been imported by an earlier test file — i.e. the outcome depended on
    pytest's collection order.  Reusing the real module removes that whole
    class of order-dependence.
    """
    import config as real_config

    monkeypatch.setitem(sys.modules, "config", real_config)
    monkeypatch.setattr(real_config, "OANDA_ACCOUNT_ID", "", raising=False)
    monkeypatch.setattr(real_config, "OANDA_ENV", "practice", raising=False)
    return real_config


def _load_runner(monkeypatch, max_entries="1"):
    """Import scheduled_runner_v3 with its import-time dependencies stubbed."""
    # Import the real ``utils`` package BEFORE any stub is installed.
    #
    # ``scheduled_runner_v3`` does ``from utils.trading_core import get_candles``,
    # and importing a submodule executes ``utils/__init__.py``, which pulls in
    # data_provider / trading_core / ml_confirmation / …  Those modules import
    # their own names from ``config_oanda`` (``OANDA_ACCOUNT_ID``) and ``config``
    # (``DATA_SOURCE``, ``GEMINI_API_KEY``, …) that these minimal stubs do not
    # provide, so doing it lazily under the stubs raised ImportError and made the
    # outcome depend on whether an earlier test file had already imported
    # ``utils``.  Warming the real package first makes this file order-independent.
    import utils  # noqa: F401

    _make_config_oanda_module()
    _make_trading_core_module()
    _make_config_bot_v3_module()
    _make_config_module(monkeypatch)

    # The runner only imports the strategy class + a few helpers; neither is used
    # by _pick_global_basket, so light stubs keep this test offline and dependency-free.
    _make_skipped_module("custom_strategy_v3", BaseCurrencyTrendStrategy=type(
        "BaseCurrencyTrendStrategy", (), {"__init__": lambda self, *a, **k: None}
    ))
    _make_skipped_module(
        "utils.strategy_helpers",
        # scheduled_runner_v3 imports CURRENCIES (as _SCORING_CURRENCIES) to
        # validate the JPY gate-exclusion set against the scored universe.
        CURRENCIES=["USD", "EUR", "GBP", "AUD", "JPY", "CHF"],
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
        # Also imported by scheduled_runner_v3 at module scope; none of them are
        # exercised by _pick_global_basket, so no-op stand-ins are enough.
        is_bot_owned_trade=lambda trade, prefix: prefix in (
            (trade.get("clientExtensions") or {}).get("tag", "") or ""
        ),
        parse_strategy_comment=lambda *a, **k: None,
        _extract_raw_tag=lambda *a, **k: None,
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