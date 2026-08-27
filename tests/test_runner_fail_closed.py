import importlib.util
from pathlib import Path
from unittest.mock import Mock


RUNNER_PATH = Path(__file__).resolve().parents[1] / "scheduled_runner_v1.3.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("scheduled_runner_v1_3", RUNNER_PATH)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


def test_run_cycle_stops_before_signal_analysis_when_state_is_unavailable(monkeypatch):
    runner = _load_runner()
    analyze = Mock()
    monkeypatch.setattr(runner._risk, "manage_open_positions", lambda _: None)
    monkeypatch.setattr(runner, "analyze_custom_strategy", analyze)

    runner.run_cycle()

    analyze.assert_not_called()