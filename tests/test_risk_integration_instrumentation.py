"""
Tests for the V2 Section 4.1 additive outcome-logging hook in
utils/risk_integration.py::manage_open_positions().

Scope of these tests: verify that (a) the new logging calls never alter
control flow / return values / state-store calls, and (b) a logging
failure is swallowed rather than propagating. These tests do NOT exercise
OANDA network calls -- they mock at the same seams the module's own
docstring says it's tested at (oanda_client.request()).
"""
import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

import utils.risk_integration as ri
from utils.dynamic_risk_manager import RiskStateEnum, ActionType, RiskAction
from utils.pyramid_cluster import PyramidCluster


def _make_cluster(direction=1, entry=150.0, atr=0.5, sl_level=149.0):
    return PyramidCluster(
        initial_size=1000,
        entry_price=entry,
        direction=direction,
        atr_entry=atr,
        entry_time=datetime.now(timezone.utc),
        structural_sl_level=sl_level,
        initial_trade_id="TID-1",
    )


@pytest.fixture
def temp_outcome_log(tmp_path, monkeypatch):
    path = str(tmp_path / "outcomes.jsonl")
    monkeypatch.setattr("utils.signal_instrumentation.TRADE_OUTCOME_LOG_PATH", path)
    return path


def test_external_close_logs_outcome_and_preserves_deletion_behavior(temp_outcome_log, monkeypatch):
    """
    Externally-closed branch: reconcile_with_oanda returns False. Assert
    (1) delete_cluster_data is still called exactly as before, (2) an
    outcome record is written, (3) the returned still_managed list does
    NOT include this instrument -- identical to pre-instrumentation behavior.
    """
    cluster = _make_cluster()

    monkeypatch.setattr(ri, "list_managed_instruments", lambda: ["USD_JPY"])
    monkeypatch.setattr(ri, "load_cluster_data", lambda instr: {"stub": True})
    monkeypatch.setattr(ri, "restore_cluster", lambda data: cluster)
    monkeypatch.setattr(ri, "reconcile_with_oanda", lambda c, instr: False)  # externally closed
    monkeypatch.setattr(ri, "get_latest_price", lambda instr: 150.5)

    delete_calls = []
    monkeypatch.setattr(ri, "delete_cluster_data", lambda instr: delete_calls.append(instr))
    save_calls = []
    monkeypatch.setattr(ri, "save_cluster_data", lambda instr, data: save_calls.append(instr))

    result = ri.manage_open_positions()

    assert result == []  # unchanged semantics: externally-closed pair is NOT in still_managed
    assert delete_calls == ["USD_JPY"]
    assert save_calls == []

    with open(temp_outcome_log) as f:
        lines = f.readlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["instrument"] == "USD_JPY"
    assert record["close_reason"].startswith("closed_externally_or_by_broker")
    assert record["close_price"] == 150.5
    assert "approximate" in record["close_price_source"]


def test_full_close_by_own_action_logs_outcome_and_preserves_deletion_behavior(temp_outcome_log, monkeypatch):
    """
    Own-action full-close branch: reconcile succeeds (still_open=True), but
    cluster.update() drives the risk_manager to CLOSED this cycle. Assert
    the same delete/no-save/not-in-still-managed behavior as before, plus
    an outcome record using the cycle's evaluation price.
    """
    cluster = _make_cluster()

    monkeypatch.setattr(ri, "list_managed_instruments", lambda: ["EUR_JPY"])
    monkeypatch.setattr(ri, "load_cluster_data", lambda instr: {"stub": True})
    monkeypatch.setattr(ri, "restore_cluster", lambda data: cluster)
    monkeypatch.setattr(ri, "reconcile_with_oanda", lambda c, instr: True)  # still open pre-decision
    monkeypatch.setattr(ri, "fetch_market_context", lambda instr, c: (152.0, 0.4, 152.5, 149.0))

    full_close_action = RiskAction(
        action=ActionType.FULL_CLOSE, close_ratio=1.0,
        reason="test full close", state=RiskStateEnum.CLOSED,
    )
    monkeypatch.setattr(ri, "cluster_update_placeholder", None, raising=False)

    def fake_update(price, atr_now, hh, ll, current_time=None):
        cluster.risk_manager.state = RiskStateEnum.CLOSED
        return full_close_action

    monkeypatch.setattr(cluster, "update", fake_update)
    monkeypatch.setattr(ri, "apply_risk_action", lambda c, instr, action: None)

    delete_calls = []
    monkeypatch.setattr(ri, "delete_cluster_data", lambda instr: delete_calls.append(instr))
    save_calls = []
    monkeypatch.setattr(ri, "save_cluster_data", lambda instr, data: save_calls.append(instr))

    result = ri.manage_open_positions()

    assert result == []
    assert delete_calls == ["EUR_JPY"]
    assert save_calls == []

    with open(temp_outcome_log) as f:
        lines = f.readlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["instrument"] == "EUR_JPY"
    assert record["close_price"] == 152.0  # the cycle's evaluation price, not a new fetch
    assert "closed_by_own_risk_action" in record["close_reason"]
    assert "approximate" in record["close_price_source"]


def test_logging_failure_does_not_break_position_management(monkeypatch, temp_outcome_log):
    """
    If log_trade_outcome() itself raises for some reason, manage_open_positions()
    must still complete normally (delete state, return correct list) --
    the try/except around the V2 4.1 addition must isolate the failure.
    """
    cluster = _make_cluster()

    monkeypatch.setattr(ri, "list_managed_instruments", lambda: ["GBP_JPY"])
    monkeypatch.setattr(ri, "load_cluster_data", lambda instr: {"stub": True})
    monkeypatch.setattr(ri, "restore_cluster", lambda data: cluster)
    monkeypatch.setattr(ri, "reconcile_with_oanda", lambda c, instr: False)
    monkeypatch.setattr(ri, "get_latest_price", lambda instr: 195.0)

    def broken_log_trade_outcome(**kwargs):
        raise RuntimeError("simulated logging backend failure")

    monkeypatch.setattr(ri, "log_trade_outcome", broken_log_trade_outcome)

    delete_calls = []
    monkeypatch.setattr(ri, "delete_cluster_data", lambda instr: delete_calls.append(instr))

    result = ri.manage_open_positions()

    assert result == []  # position management still completed correctly
    assert delete_calls == ["GBP_JPY"]  # deletion still happened despite log failure


def test_no_dynamic_risk_manager_returns_empty_list_unchanged(monkeypatch):
    """Regression guard: flag-off short-circuit must be completely untouched."""
    monkeypatch.setattr(ri, "ENABLE_DYNAMIC_RISK_MANAGER", False)
    assert ri.manage_open_positions() == []
