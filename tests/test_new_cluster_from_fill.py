"""
tests/test_new_cluster_from_fill.py

Regression test for the exit_tightness kwarg mismatch bug:
scheduled_runner_v1.4.1.py calls new_cluster_from_fill() with an
exit_tightness kwarg that the function does not accept, causing
cluster creation to fail silently on every live fill.

This test calls new_cluster_from_fill() the same way the live
runner does, so a signature mismatch fails CI instead of
surfacing only in production.
"""
import inspect
import pytest
from unittest.mock import MagicMock, patch

from utils.pyramid_cluster import new_cluster_from_fill


class TestNewClusterFromFillSignature:
    """Guards against silent kwarg-mismatch failures at the call site."""

    def test_signature_accepts_all_kwargs_used_by_live_runner(self):
        """
        Introspects the call site in scheduled_runner_v1.4.1.py (or the
        args below, kept in sync with it) and asserts every kwarg passed
        live is actually accepted by the function signature.

        UPDATE THIS LIST if the live call site changes.
        """
        live_call_kwargs = {
            "pair": "AUD_JPY",
            "fill_price": 110.239,
            "units": 10000,
            "side": "short",
            "atr": 0.15,
            "exit_tightness": "normal",  # <-- the kwarg that currently breaks this
        }

        sig = inspect.signature(new_cluster_from_fill)
        accepted_params = set(sig.parameters.keys())
        has_var_kwargs = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in sig.parameters.values()
        )

        missing = set(live_call_kwargs) - accepted_params
        if missing and not has_var_kwargs:
            pytest.fail(
                f"new_cluster_from_fill() does not accept kwarg(s) "
                f"{missing} that the live runner passes. "
                f"Either add these params to the function signature, "
                f"or fix the call site in scheduled_runner_v1.4.1.py "
                f"to stop passing them."
            )

    def test_call_does_not_raise_with_live_kwargs(self):
        """
        End-to-end guard: actually invoke the function with the live
        kwarg set (with dependencies stubbed) and confirm it doesn't
        raise. Update the stubs below to match current constructor
        deps in pyramid_cluster.py.
        """
        live_call_kwargs = {
            "pair": "AUD_JPY",
            "fill_price": 110.239,
            "units": 10000,
            "side": "short",
            "atr": 0.15,
            "exit_tightness": "normal",
        }

        with patch("utils.pyramid_cluster.PyramidCluster") as MockCluster:
            MockCluster.return_value = MagicMock()
            try:
                new_cluster_from_fill(**live_call_kwargs)
            except TypeError as e:
                pytest.fail(
                    f"new_cluster_from_fill() raised TypeError with the "
                    f"exact kwargs the live runner passes: {e}"
                )

    def test_cluster_creation_failure_is_logged_not_silent(self, caplog):
        """
        If new_cluster_from_fill() is called with a bad kwarg and the
        call site wraps it in try/except, assert the failure is logged
        at ERROR level with pair + fill price + exception — not swallowed.
        Adjust the import path to wherever the live try/except lives.
        """
        # from scheduled_runner_v1_4_1 import handle_fill  # adjust import
        # with caplog.at_level(logging.ERROR):
        #     handle_fill(pair="AUD_JPY", fill_price=110.239, ...)
        # assert "AUD_JPY" in caplog.text
        # assert "110.239" in caplog.text
        pytest.skip("Wire up once handle_fill()'s exception path is confirmed")
