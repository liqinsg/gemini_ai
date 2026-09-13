import utils.risk_integration as risk_integration
from utils.thesis_observation import get_thesis_snapshot


def test_get_thesis_snapshot_returns_minimum_fields():
    snapshot = get_thesis_snapshot(
        "GBP_JPY",
        "BUY",
        {"GBP": 1.0, "EUR": 0.5, "JPY": -0.5},
        ["GBP_JPY", "EUR_JPY"],
    )

    assert snapshot == {
        "jpy_rank": 3,
        "jpy_gap": -1.5,
        "confirming_breadth": 2,
        "total_crosses": 2,
        "pair_strength": 1.5,
    }


def test_missing_matrix_is_passive():
    assert get_thesis_snapshot("GBP_JPY", "BUY", None) == {
        "jpy_rank": None,
        "jpy_gap": None,
        "confirming_breadth": None,
        "total_crosses": None,
        "pair_strength": None,
    }


def test_logging_failure_does_not_skip_risk_action(monkeypatch):
    class Cluster:
        risk_manager = type("RiskManager", (), {"direction": 1, "state": object()})()
        risk_manager.unrealized_r = lambda _: 0.0

        def update(self, *args, **kwargs):
            state = type("State", (), {"value": "INIT"})()
            return type("Action", (), {"action": type("Type", (), {"value": "NO_CHANGE"})(), "reason": "", "state": state})()

        def to_dict(self):
            return {}

    action_calls = []
    monkeypatch.setattr(risk_integration, "list_managed_instruments", lambda: ["GBP_JPY"])
    monkeypatch.setattr(risk_integration, "load_cluster_data", lambda _: {})
    monkeypatch.setattr(risk_integration, "restore_cluster", lambda _: Cluster())
    monkeypatch.setattr(risk_integration, "reconcile_with_oanda", lambda *_: True)
    monkeypatch.setattr(risk_integration, "fetch_market_context", lambda *_: (190.0, 0.5, 191.0, 189.0))
    monkeypatch.setattr(risk_integration, "log_thesis_snapshot", lambda **_: (_ for _ in ()).throw(RuntimeError("log failed")))
    monkeypatch.setattr(risk_integration, "apply_risk_action", lambda *_: action_calls.append(True))
    monkeypatch.setattr(risk_integration, "save_cluster_data", lambda *_: None)

    result = risk_integration.manage_open_positions({"GBP": 1.0, "JPY": 0.0})

    assert result == ["GBP_JPY"]
    assert action_calls == [True]
