"""Static guardrails for the v1.3.1 JPY joint-MC shadow integration."""
import ast
from pathlib import Path


RUNNER = Path(__file__).resolve().parents[1] / "scheduled_runner_v1.3.1.py"


def _function(tree, name):
    return next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)


def test_joint_observer_is_invoked_only_as_an_ignored_side_effect():
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    cycle = _function(tree, "run_cycle")
    calls = [node for node in ast.walk(cycle) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "run_jpy_joint_mc_observation"]
    assert len(calls) == 1
    # The observer call is an expression statement, not an input to trading logic.
    assert any(isinstance(node, ast.Expr) and node.value is calls[0] for node in ast.walk(cycle))


def test_observer_failure_is_caught_and_trading_names_are_absent_from_observer():
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    cycle = _function(tree, "run_cycle")
    assert any(isinstance(node, ast.Try) and any(
        isinstance(handler.type, ast.Name) and handler.type.id == "Exception"
        for handler in node.handlers
    ) for node in ast.walk(cycle))
    observer = _function(tree, "run_jpy_joint_mc_observation")
    forbidden = {"open_oanda_order", "execute_market_trade", "resolve_and_prepare_entry", "manage_open_positions"}
    assert not (forbidden & {node.id for node in ast.walk(observer) if isinstance(node, ast.Name)})