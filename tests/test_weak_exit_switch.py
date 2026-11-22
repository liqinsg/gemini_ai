"""Regression tests for the weak-exit run.env soft switch (WEAK_EXIT_ENABLED).

Decision (2026-11-22): 主动出局由一个 run.env 软开关控制，**无条件生效**
（不像 MIN_GAP 那批只在 --live 生效），覆盖 config.WEAK_STRENGTH_EXIT["ENABLED"]。

    WEAK_EXIT_ENABLED=true   → 启用（真平仓）
    WEAK_EXIT_ENABLED=false  → 关闭本轮主动平仓
    run.env 未设             → 回退到 config.WEAK_STRENGTH_EXIT["ENABLED"]

v1441 行为用 mock 端到端验证；v3 用源码守卫（同一注入点）验证。

Run:
    python -m unittest -v tests.test_weak_exit_switch
"""

import os
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import config as cfg_mod
import scheduled_runner_v1441 as runner


def _jpy_trades():
    return [
        {
            "id": "1",
            "instrument": "EUR_JPY",
            "currentUnits": "1000",
            "openTime": "2026-11-20T00:00:00Z",
            "tag": "JPY-STRENGTH_EUR_JPY_BUY_20261120",
        },
        {
            "id": "2",
            "instrument": "USD_JPY",
            "currentUnits": "1000",
            "openTime": "2026-11-20T00:00:00Z",
            "tag": "JPY-STRENGTH_USD_JPY_BUY_20261120",
        },
    ]


class _FakeCore:
    """Close-recording fake TradingCore."""

    def __init__(self, trades):
        self._trades = trades
        self.closed = []

    def get_all_open_trades(self):
        return list(self._trades)

    def close_trade_by_id(self, tid, client_request_id=None):
        self.closed.append(str(tid))
        return True, {"realizedPL": "0.0"}


class TestWeakExitSwitch(unittest.TestCase):
    def _drive(self, env_value_marker, config_enabled=True):
        """env_value_marker: 'true' | 'false' | None(未设).

        返回 (fake_core, stdout)。
        """
        scores = {"USD": 1.0, "EUR": 0.5, "JPY": 0.0}
        core = _FakeCore(_jpy_trades())
        we_cfg = {"ENABLED": config_enabled, "SHADOW": False, "N": 1, "MIN_GROUP_SIZE": 2}

        loaded = {} if env_value_marker is None else {"WEAK_EXIT_ENABLED": env_value_marker}
        env_patch = (
            {"WEAK_EXIT_ENABLED": ""} if env_value_marker is None
            else {"WEAK_EXIT_ENABLED": env_value_marker}
        )

        patches = [
            patch.object(runner, "_trading_core", core),
            patch.object(runner, "build_strength_matrix", lambda: dict(scores)),
            patch.object(runner, "_ENV_LOADED_KEYS", loaded),
            patch.object(runner._config, "WEAK_STRENGTH_EXIT", we_cfg),
            patch.dict(os.environ, env_patch),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        buffer = StringIO()
        with redirect_stdout(buffer):
            runner._weak_exit_scan_v1441(dry_run=False)
        return core, buffer.getvalue()

    def test_env_false_disables_when_config_enabled(self):
        core, output = self._drive("false", config_enabled=True)
        self.assertEqual(core.closed, [])
        self.assertNotIn("WEAK-EXIT] FIRE", output)

    def test_env_true_applies_when_config_disabled(self):
        core, output = self._drive("true", config_enabled=False)
        # EUR_JPY support=0.5 < USD_JPY support=1.0 → 垫底 1 笔被平。
        self.assertEqual(core.closed, ["1"])
        self.assertIn("WEAK-EXIT] FIRE", output)

    def test_unset_env_falls_back_to_config_enabled(self):
        core, output = self._drive(None, config_enabled=True)
        self.assertEqual(core.closed, ["1"])
        self.assertIn("WEAK-EXIT] FIRE", output)

    def test_unset_env_falls_back_to_config_disabled(self):
        core, output = self._drive(None, config_enabled=False)
        self.assertEqual(core.closed, [])


class TestSwitchWiring(unittest.TestCase):
    def test_run_env_declares_switch(self):
        text = (Path(PROJECT_ROOT) / "run.env").read_text(encoding="utf-8")
        self.assertIn("WEAK_EXIT_ENABLED=", text)

    def test_v3_reads_switch_via_env_or_config(self):
        text = (Path(PROJECT_ROOT) / "scheduled_runner_v3.py").read_text(encoding="utf-8")
        self.assertIn('_env_or_config("WEAK_EXIT_ENABLED"', text)

    def test_config_default_enabled_is_true(self):
        self.assertTrue(cfg_mod.WEAK_STRENGTH_EXIT["ENABLED"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
