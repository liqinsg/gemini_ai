"""Broker-authoritative duplicate-entry prevention — no local `state/` files.

Covers the revision that replaced file-backed duplicate prevention with OANDA
live state:

  * utils.oanda_state.check_pair_level_strategy_position() — blocks on open
    trades / pending orders carrying this strategy's tag, fails CLOSED.
  * utils.post_exit_context.PostExitTracker — post-exit context derived from
    OANDA CLOSED trades (read-only, writes nothing).
  * scheduled_runner_v13.run_cycle() — the cron runner imports no `state/`
    module and refuses a duplicate entry before any order is submitted.

Run:  pytest tests/test_oanda_authoritative_guard.py -v
"""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import config
from utils.oanda_state import (
    build_post_exit_context,
    check_pair_level_strategy_position,
    is_strategy_record,
    normalize_strategy_tag,
    parse_oanda_time,
)
from utils.position_direction import PositionDecision

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_V13 = REPO_ROOT / "scheduled_runner_v13.py"


class FakeOanda:
    """Canned v20 client: each request pops the next scripted payload."""

    def __init__(self, *payloads, error=None):
        self.payloads = list(payloads)
        self.error = error
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if not self.payloads:
            return {}
        return self.payloads.pop(0)


def open_trade(tag="JPY-STRENGTH_GBP_JPY_BUY_20260914", units="10000", trade_id="T-1"):
    return {"id": trade_id, "instrument": "GBP_JPY", "currentUnits": units,
            "clientExtensions": {"tag": tag}}


def pending_order(tag="JPY-STRENGTH_GBP_JPY_BUY_20260914", order_id="O-1"):
    return {"id": order_id, "instrument": "GBP_JPY", "clientExtensions": {"tag": tag}}


# ---------------------------------------------------------------------------
# Tag recognition
# ---------------------------------------------------------------------------

def test_tag_normalisation_matches_legacy_and_versioned_tags():
    assert normalize_strategy_tag("jpy_strength") == "JPY-STRENGTH"
    assert is_strategy_record({"tag": "jpy_strength"})
    assert is_strategy_record({"tag": "JPY-STRENGTH_GBP_JPY_BUY_20260914"})
    assert is_strategy_record({"clientExtensions": {"tag": "JPY-STRENGTH"}})
    assert not is_strategy_record({"tag": "SOME-OTHER-BOT"})
    assert not is_strategy_record({})


# ---------------------------------------------------------------------------
# Pair-level guard
# ---------------------------------------------------------------------------

def test_clear_pair_is_allowed():
    client = FakeOanda({"trades": []}, {"orders": []})
    allowed, reason = check_pair_level_strategy_position(client, "acct-1", "GBP_JPY", "BUY")
    assert allowed is True
    assert "no strategy trade" in reason


def test_open_long_strategy_trade_blocks_buy_and_sell():
    """A held strategy long blocks a repeat BUY (duplicate) *and* a new SELL
    (prohibited opposite-direction dual position on the same pair)."""
    for side, marker in (("BUY", "duplicate"), ("SELL", "opposite-direction")):
        client = FakeOanda({"trades": [open_trade(units="10000")]})
        allowed, reason = check_pair_level_strategy_position(client, "acct-1", "GBP_JPY", side)
        assert allowed is False, f"{side} should have been blocked"
        assert marker in reason


def test_short_strategy_trade_blocks_a_new_short():
    client = FakeOanda({"trades": [open_trade(units="-7500")]})
    allowed, reason = check_pair_level_strategy_position(client, "acct-1", "GBP_JPY", "SELL")
    assert allowed is False
    assert "same-direction duplicate" in reason


def test_foreign_strategy_positions_do_not_block_our_entry():
    """A manual/other-bot position on the pair must not silence this strategy."""
    client = FakeOanda({"trades": [open_trade(tag="MANUAL-TRADE")]}, {"orders": []})
    allowed, _ = check_pair_level_strategy_position(client, "acct-1", "GBP_JPY", "BUY")
    assert allowed is True


def test_pending_strategy_order_blocks_entry():
    client = FakeOanda({"trades": []}, {"orders": [pending_order()]})
    allowed, reason = check_pair_level_strategy_position(client, "acct-1", "GBP_JPY", "BUY")
    assert allowed is False
    assert "in flight" in reason


def test_pending_order_query_failure_fails_closed_after_clear_trades():
    client = FakeOanda({"trades": []}, error=RuntimeError("orders endpoint down"))
    allowed, reason = check_pair_level_strategy_position(client, "acct-1", "GBP_JPY", "BUY")
    assert allowed is False
    assert "fail closed" in reason


def test_open_trade_query_failure_fails_closed():
    client = FakeOanda(error=RuntimeError("network down"))
    allowed, reason = check_pair_level_strategy_position(client, "acct-1", "GBP_JPY", "BUY")
    assert allowed is False
    assert "fail closed" in reason

# ---------------------------------------------------------------------------
# Post-exit context from OANDA closed trades
# ---------------------------------------------------------------------------

def test_parse_oanda_time_handles_nanoseconds_and_garbage():
    assert parse_oanda_time("2026-09-09T09:00:00.123456789Z") == datetime(
        2026, 9, 9, 9, 0, 0, 123456, tzinfo=timezone.utc
    )
    assert parse_oanda_time("2026-09-09T09:00:00Z") == datetime(
        2026, 9, 9, 9, 0, 0, tzinfo=timezone.utc
    )
    assert parse_oanda_time(None) is None
    assert parse_oanda_time("not-a-timestamp") is None


def test_build_post_exit_context_uses_newest_close_and_loss_streak():
    now = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)
    context = build_post_exit_context(
        [
            {"id": "A", "realizedPL": "-5.0", "closeTime": "2026-09-09T10:00:00Z"},
            {"id": "B", "realizedPL": "-2.0", "closeTime": "2026-09-09T11:00:00Z"},
        ],
        now=now,
    )
    assert context["closed_at"] == "2026-09-09T11:00:00+00:00"
    assert context["close_reason"] == "STOP_LOSS"
    assert context["tier"] == "tier3"
    assert context["consecutive_failures"] == 2
    assert context["elapsed_hours"] == pytest.approx(1.0)


def test_build_post_exit_context_is_neutral_without_history():
    context = build_post_exit_context([])
    assert context["closed_at"] is None
    assert context["tier"] is None
    assert context["consecutive_failures"] == 0
    assert context["elapsed_hours"] == 0.0


# ---------------------------------------------------------------------------
# The cron runner (scheduled_runner_v13.py)
# ---------------------------------------------------------------------------

def _candidate():
    return {
        "pair": "GBP_JPY",
        "action": "BUY",
        "bar_time": "2026-09-14T10:00:00Z",
        "entry": 204.50,
        "stop_loss": 203.80,
        "take_profit": 206.00,
        "strength_score": 0.42,
        "risk_reward": 2.1,
        "reasoning": "unit-test signal",
    }


def _load_runner_v13(monkeypatch):
    """Import the cron runner offline, with every strategy/execution hook stubbed."""
    monkeypatch.setattr(config, "POST_EXIT_GATE_ENABLED", False)
    monkeypatch.setattr(config, "MC_REGIME_ENABLED", False)
    spec = importlib.util.spec_from_file_location("scheduled_runner_v13_guard_test", RUNNER_V13)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

    candidate = _candidate()
    monkeypatch.setattr(runner, "with_retry", lambda fn, **_kwargs: fn())
    monkeypatch.setattr(runner, "analyze_custom_strategy", lambda *_a, **_k: "fake report")
    monkeypatch.setattr(runner, "get_last_signal", lambda: candidate)
    monkeypatch.setattr(runner, "get_latest_mc_local", lambda **_kwargs: None)
    # The shadow gate is observational; keep it offline/noisy-log free.
    monkeypatch.setattr(runner, "POST_EXIT_SHADOW_MODE", False)
    return runner


def test_runner_v13_has_no_local_state_import():
    """Regression guard for the crash this revision fixes: the runner must not
    import the retired local `state/` package (or any of its modules)."""
    source = RUNNER_V13.read_text(encoding="utf-8")
    assert "from state" not in source
    assert "import state" not in source
    assert "state.post_exit_context" not in source


def test_runner_v13_blocks_duplicate_entry_via_oanda_guard(monkeypatch, capsys):
    runner = _load_runner_v13(monkeypatch)
    execute_market_trade = MagicMock(return_value=True)
    monkeypatch.setattr(runner, "execute_market_trade", execute_market_trade)
    guard = MagicMock(return_value=(False, "same-direction duplicate (trade_id=T-1)"))
    monkeypatch.setattr(runner, "check_pair_level_strategy_position", guard)

    runner.run_cycle()

    output = capsys.readouterr().out
    assert "BLOCKED by OANDA idempotency guard" in output
    execute_market_trade.assert_not_called()
    guard.assert_called_once()
    # The guard must be asked about the live account, not a file on disk.
    passed_args = guard.call_args.args
    assert passed_args[2] == "GBP_JPY" and passed_args[3] == "BUY"


def test_runner_v13_allowed_entry_is_tagged_and_submitted_once(monkeypatch, capsys):
    runner = _load_runner_v13(monkeypatch)
    monkeypatch.setattr(
        runner, "check_pair_level_strategy_position", MagicMock(return_value=(True, "clear"))
    )
    monkeypatch.setattr(runner, "resolve_and_prepare_entry", lambda *_a, **_k: PositionDecision.ENTER)
    execute_market_trade = MagicMock(return_value=True)
    monkeypatch.setattr(runner, "execute_market_trade", execute_market_trade)

    runner.run_cycle()

    assert execute_market_trade.call_count == 1
    client_extensions = execute_market_trade.call_args.kwargs["client_extensions"]
    # Tagged with the prefix the guard matches on, so the NEXT cycle can see it.
    assert client_extensions["tag"] == config.STRATEGY_TAG_PREFIX
    assert client_extensions["id"].startswith(f"{config.STRATEGY_TAG_PREFIX.upper()}_GBP_JPY_")
    assert "[IDEMPOTENCY] GBP_JPY BUY allowed" in capsys.readouterr().out

