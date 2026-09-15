import json
from datetime import datetime, timedelta, timezone

import pytest

from utils import post_exit_context
from utils import post_exit_gate


class FakeTracker:
    def __init__(self, context):
        self.context = context
        self.calls = []

    def get_context(self, instrument):
        self.calls.append(instrument)
        return dict(self.context)


class FakeOandaAccount:
    """Minimal v20-client stand-in: returns canned closed trades for one account."""

    STRATEGY_TAG = "JPY-STRENGTH"

    def __init__(self, closed_trades):
        self.closed_trades = list(closed_trades)
        self.requests = []

    def request(self, request):
        self.requests.append(request)
        return {"trades": list(self.closed_trades)}


def _closed_trade(close_time, realized_pl, tag="JPY-STRENGTH", trade_id="T-1"):
    return {
        "id": trade_id,
        "instrument": "EUR_JPY",
        "clientExtensions": {"tag": tag},
        "realizedPL": str(realized_pl),
        "closeTime": close_time,
    }


def test_close_reason_mapping_is_explicit():
    assert post_exit_gate.map_close_reason_to_tier("TP") == "tier1"
    assert post_exit_gate.map_close_reason_to_tier("STRATEGY_INVALIDATION: gap") == "tier3"
    assert post_exit_gate.map_close_reason_to_tier("STOP_LOSS") == "tier3"
    assert post_exit_gate.map_close_reason_to_tier("not-a-known-reason") == "unknown"


def test_live_multiplier_baseline_and_window_expiry(monkeypatch, tmp_path):
    log_path = tmp_path / "gate.jsonl"
    monkeypatch.setattr(post_exit_gate, "POST_EXIT_GATE_LOG_PATH", str(log_path))
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    tracker = FakeTracker({
        "closed_at": recent,
        "close_reason": "STOP_LOSS",
        "elapsed_hours": 1.0,
    })

    # Expected multipliers come from config, so this test tracks live tuning
    # instead of hard-coding a snapshot of it.
    tier_mult = post_exit_gate.POST_EXIT_TIER_MULTIPLIER["tier3"]
    rank_mult = post_exit_gate.RANK_MULTIPLIER[3]
    regime_mult = post_exit_gate.MC_REGIME_MULTIPLIER["CONSOLIDATION"]
    live_mult = tier_mult * rank_mult * regime_mult
    no_mc_mult = tier_mult * rank_mult

    baseline_threshold = post_exit_gate.PostExitGate.BASELINE_STRENGTH_THRESHOLD
    no_mc_threshold = baseline_threshold * no_mc_mult
    live_threshold = baseline_threshold * live_mult
    assert no_mc_threshold < live_threshold, "post-exit gate must tighten the hurdle"
    # Clears the plain baseline but NOT the tightened post-exit hurdle.
    borderline_score = (no_mc_threshold + live_threshold) / 2
    size_mult = post_exit_gate.POST_EXIT_SIZE_MULTIPLIER["tier3"]

    decision = post_exit_gate.PostExitGate.evaluate(
        instrument="USD_JPY",
        action="BUY",
        strength_score=borderline_score,
        candidate_rank=3,
        mc_regime_raw="CONSOLIDATION",
        baseline_units=1000,
        actual_aligned_tf=3,
        tracker=tracker,
    )

    assert decision.tier_multiplier == pytest.approx(tier_mult)
    assert decision.rank_multiplier == pytest.approx(rank_mult)
    assert decision.regime_multiplier == pytest.approx(regime_mult)
    assert decision.effective_multiplier_live == pytest.approx(live_mult)
    assert decision.effective_multiplier_baseline_no_mc == pytest.approx(no_mc_mult)
    assert decision.baseline_decision == "ALLOW"
    assert decision.is_allowed is False
    assert decision.reason_code == "REJECTED_POST_EXIT_STRENGTH"
    assert decision.effective_units == round(1000 * size_mult)
    assert json.loads(log_path.read_text())[
        "unknown_close_reason"
    ] is False

    tracker.context["elapsed_hours"] = 25.0
    expired = post_exit_gate.PostExitGate.evaluate(
        instrument="USD_JPY",
        action="BUY",
        strength_score=0.04,
        candidate_rank=1,
        mc_regime_raw="NEUTRAL",
        baseline_units=1000,
        actual_aligned_tf=3,
        tracker=tracker,
    )
    assert expired.window_active is False
    assert expired.effective_multiplier_live == 1.0
    assert expired.effective_units == 1000
    assert expired.reason_code == "ALLOW_BASELINE"


def test_alignment_is_clamped_to_three_and_context_comes_from_oanda(monkeypatch, tmp_path):
    """The tracker must read OANDA's CLOSED trades, not any local state file —
    and the gate must still clamp alignment/units exactly as before."""
    client = FakeOandaAccount([
        _closed_trade(
            close_time=(datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
            realized_pl=-18.4,
            trade_id="T-CLOSED-1",
        )
    ])
    tracker = post_exit_context.PostExitTracker(api_client=client, account_id="acct-1")

    context = tracker.get_context("EUR_JPY")
    assert context["close_reason"] == "STOP_LOSS"
    assert context["tier"] == "tier3"
    assert context["consecutive_failures"] == 1
    assert context["closed_at"] is not None
    assert len(client.requests) == 1
    # Instrument filter and CLOSED state must be sent to OANDA.
    assert "EUR_JPY" in str(client.requests[0].params)

    decision = post_exit_gate.PostExitGate.evaluate(
        instrument="EUR_JPY",
        action="SELL",
        strength_score=0.10,
        candidate_rank=4,
        mc_regime_raw="CONSOLIDATION",
        baseline_units=1000,
        actual_aligned_tf=2,
        tracker=tracker,
    )
    assert decision.effective_alignment == 3
    assert decision.effective_units == round(
        1000 * post_exit_gate.POST_EXIT_SIZE_MULTIPLIER["tier3"]
    )
    assert decision.is_allowed is False
    assert decision.reason_code == "REJECTED_POST_EXIT_ALIGNMENT"


def test_tracker_tier_follows_the_newest_oanda_close(monkeypatch, tmp_path):
    """Tier is broker truth: the MOST RECENT close decides it, and a streak of
    losing closes is counted from OANDA's own realizedPL."""
    losses_then_win = FakeOandaAccount([
        _closed_trade("2026-09-09T09:00:00.123456789Z", realized_pl=12.0, trade_id="T-WIN"),
        _closed_trade("2026-09-09T08:00:00.123456789Z", realized_pl=-30.0, trade_id="T-LOSS-2"),
        _closed_trade("2026-09-09T07:00:00.123456789Z", realized_pl=-11.0, trade_id="T-LOSS-1"),
    ])
    tracker = post_exit_context.PostExitTracker(api_client=losses_then_win, account_id="acct-1")

    context = tracker.get_context("EUR_JPY")
    assert context["tier"] == "tier1"
    assert context["close_reason"] == "PROFIT_TAKE"
    assert context["closed_at"] == "2026-09-09T09:00:00.123456+00:00"
    # newest close is a win -> streak resets even though the two before it lost
    assert context["consecutive_failures"] == 0

    only_losses = FakeOandaAccount([
        _closed_trade("2026-09-09T09:00:00Z", realized_pl=-2.5, trade_id="T-LOSS-2b"),
        _closed_trade("2026-09-09T08:00:00Z", realized_pl=-7.5, trade_id="T-LOSS-1b"),
    ])
    losing_tracker = post_exit_context.PostExitTracker(api_client=only_losses, account_id="acct-1")
    losing_context = losing_tracker.get_context("EUR_JPY")
    assert losing_context["tier"] == "tier3"
    assert losing_context["consecutive_failures"] == 2


def test_tracker_ignores_other_strategies_and_never_raises_on_failure():
    """Foreign trades must not colour this strategy's context, and a broken
    OANDA query degrades to a neutral context instead of breaking the cycle."""
    foreign_only = FakeOandaAccount([
        _closed_trade("2026-09-09T09:00:00Z", realized_pl=-99.0, tag="SOME-OTHER-BOT")
    ])
    tracker = post_exit_context.PostExitTracker(api_client=foreign_only, account_id="acct-1")
    context = tracker.get_context("EUR_JPY")
    assert context["closed_at"] is None
    assert context["consecutive_failures"] == 0

    class ExplodingClient:
        def request(self, request):
            raise RuntimeError("simulated OANDA outage")

    broken = post_exit_context.PostExitTracker(api_client=ExplodingClient(), account_id="acct-1")
    neutral = broken.get_context("EUR_JPY")
    assert neutral["closed_at"] is None
    assert neutral["tier"] is None
    assert neutral["consecutive_failures"] == 0
    assert "neutral" in neutral["source"]
