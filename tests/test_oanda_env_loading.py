import importlib.util
import os
import sys
from pathlib import Path

from dotenv import dotenv_values


def test_config_prioritizes_run_env_from_project_root(monkeypatch, tmp_path):
    """Regression test: run.env overrides .env even when cwd differs."""
    project_root = Path(__file__).resolve().parents[1]
    assert (project_root / ".env").exists()
    expected_env = dotenv_values(project_root / "run.env").get("OANDA_ENV", "practice")

    monkeypatch.chdir(tmp_path)
    for key in ["OANDA_ENV", "OANDA_API_TOKEN", "OANDA_ACCOUNT_ID"]:
        monkeypatch.delenv(key, raising=False)

    config_path = project_root / "config.py"
    spec = importlib.util.spec_from_file_location("project_config_under_test", config_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.pop("project_config_under_test", None)
    spec.loader.exec_module(module)

    assert module.OANDA_ENV == expected_env
    assert module.OANDA_ACCOUNT_ID
    assert module.OANDA_API_TOKEN
