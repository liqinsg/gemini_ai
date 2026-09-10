import json
from datetime import datetime, timedelta, timezone

import pytest

from state import post_exit_context
from utils import post_exit_gate


class FakeTracker:
    def __init__(self, context):
        self.context = context
        self.calls = []

    def get_context(self, instrument):
        self.calls.append(instrument)
        return dict(self.context)


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

    decision = post_exit_gate.PostExitGate.evaluate(
        instrument="USD_JPY",
        action="BUY",
        strength_score=0.045,
        candidate_rank=3,
        mc_regime_raw="CONSOLIDATION",
        baseline_units=1000,
        actual_aligned_tf=3,
        tracker=tracker,
    )

    assert decision.tier_multiplier == 1.30
    assert decision.rank_multiplier == 1.05
    assert decision.effective_multiplier_live == pytest.approx(1.5015)
    assert decision.effective_multiplier_baseline_no_mc == pytest.approx(1.365)
    assert decision.baseline_decision == "ALLOW"
    assert decision.is_allowed is False
    assert decision.reason_code == "REJECTED_POST_EXIT_STRENGTH"
    assert decision.effective_units == 500
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


def test_alignment_is_clamped_to_three_and_tracker_persists_atomically(monkeypatch, tmp_path):
    state_path = tmp_path / "open_clusters.json"
    state_path.write_text(json.dumps({"clusters": {}}))
    monkeypatch.setattr(post_exit_context, "CLUSTER_STATE_PATH", state_path)
    tracker = post_exit_context.PostExitTracker(log_path=str(tmp_path / "outcomes.jsonl"))
    tracker.record_exit("EUR_JPY", "TECHNICAL_INVALIDATION")

    persisted = json.loads(state_path.read_text())
    assert persisted["post_exit_states"]["EUR_JPY"]["is_active"] is True
    context = tracker.get_context("EUR_JPY")
    assert context["close_reason"] == "TECHNICAL_INVALIDATION"

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
    assert decision.effective_units == 750
    assert decision.is_allowed is False
    assert decision.reason_code == "REJECTED_POST_EXIT_ALIGNMENT"


def test_tracker_keeps_higher_tier_during_back_to_back_exit(monkeypatch, tmp_path):
    state_path = tmp_path / "open_clusters.json"
    state_path.write_text(json.dumps({"clusters": {}}))
    monkeypatch.setattr(post_exit_context, "CLUSTER_STATE_PATH", state_path)
    tracker = post_exit_context.PostExitTracker(log_path=str(tmp_path / "outcomes.jsonl"))

    first_time = "2026-09-09T08:00:00+00:00"
    tracker.record_exit("GBP_JPY", "STOP_LOSS", closed_at=first_time)
    tracker.record_exit("GBP_JPY", "TP", closed_at="2026-09-09T09:00:00+00:00")

    context = tracker.get_context("GBP_JPY")
    assert context["tier"] == "tier3"
    assert context["close_reason"] == "STOP_LOSS"
    assert context["closed_at"] == first_time
