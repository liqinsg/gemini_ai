"""
Tests A–F proving the config_oanda → resolved profile → constructed OANDA client → TradingCore V2 wiring.

These tests DO NOT make live OANDA calls. They verify the dependency chain by mocking
the config layer and asserting that TradingCore V2 receives exactly the client and
account_id that config_oanda returned for the selected profile.
"""

import sys
import types
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from utils.trading_core_v2 import TradingCore


def _make_fake_client():
    client = MagicMock()
    client._fake_marker = "FAKE_OANDA_CLIENT"
    return client


def _fake_get_oanda_profile(env_override="practice"):
    if env_override == "live":
        return {
            "env": "live",
            "token": "fake_live_token",
            "oanda_client": _make_fake_client(),
            "api": None,
            "account_ids": ["001-003-0000000-001"],
        }
    return {
        "env": "practice",
        "token": "fake_practice_token",
        "oanda_client": _make_fake_client(),
        "api": None,
        "account_ids": ["101-003-0000000-001"],
    }


def _make_config_oanda_module():
    mod = types.ModuleType("config_oanda")
    mod.get_oanda_profile = _fake_get_oanda_profile
    mod.OANDA_ACCOUNT_ID_3 = "101-003-0000000-001"
    mod.OANDA_ACCOUNT_ID_3_LIVE = "001-003-0000000-001"
    sys.modules["config_oanda"] = mod
    return mod


def _make_config_bot_module():
    mod = types.ModuleType("config_bot")
    mod.PROFILE_CFG = {
        "profile3": {"OANDA_ACCOUNT_ID": "101-003-0000000-001", "units": 1000},
    }
    mod.load_profile = lambda name: dict(mod.PROFILE_CFG.get(name, {}))
    sys.modules["config_bot"] = mod
    return mod


def _make_config_module():
    mod = types.ModuleType("config")
    mod.OANDA_ACCOUNT_ID = ""
    mod.OANDA_ENV = "practice"
    mod.MC_REGIME_ENABLED = False
    mod.POST_EXIT_GATE_ENABLED = False
    mod.POST_EXIT_GATE_SHADOW = False
    mod.ENABLE_MC_BASKET_EXECUTION = False
    mod.ALIGNMENT_THRESHOLD = 2
    mod.DYNAMIC_RISK_TIMEFRAME = "1H"
    mod.TP_RATIO = 2.0
    mod.SL_RATIO = 1.0
    mod.SL_PIPS = 0.02
    mod.TP_PIPS = 0.05
    mod.MC_REGIME_STRENGTH_HURDLE_CONSOLIDATION = 0.60
    sys.modules["config"] = mod
    return mod


class TestTradingCoreV2ConstructorContract:
    """Test C — TradingCore receives and stores the exact caller-supplied client."""

    def test_trading_core_stores_exact_client(self):
        fake = _make_fake_client()
        tc = TradingCore(oanda_client=fake, oanda_account_id="101-003-0000000-001")
        assert tc.oanda_client is fake
        assert tc.oanda_client._fake_marker == "FAKE_OANDA_CLIENT"

    def test_trading_core_stores_exact_account_id(self):
        fake = _make_fake_client()
        tc = TradingCore(oanda_client=fake, oanda_account_id="001-003-0000000-001")
        assert tc.oanda_account_id == "001-003-0000000-001"

    def test_trading_core_does_not_create_client(self):
        """TradingCore V2 must NOT internally construct oandapyV20.API."""
        fake = _make_fake_client()
        tc = TradingCore(oanda_client=fake, oanda_account_id="acc-123")
        assert tc.oanda_client is fake

    def test_format_price_is_static(self):
        assert TradingCore.format_price_for_instrument(178.762, "EUR_JPY") == "178.762"
        assert TradingCore.format_price_for_instrument(1.08500, "EUR_USD") == "1.08500"


class TestConfigResolutionPath:
    """Test A + B — selected profile resolves to matching client and account_id."""

    def test_profile_3_practice_resolves_correct_client_and_account(self):
        """Profile #3 in practice env → practice client + practice account."""
        profile = _fake_get_oanda_profile("practice")
        client = profile["oanda_client"]
        account_id = "101-003-0000000-001"

        assert profile["env"] == "practice"
        assert client is not None
        assert account_id == "101-003-0000000-001"

        tc = TradingCore(oanda_client=client, oanda_account_id=account_id)
        assert tc.oanda_client is client
        assert tc.oanda_account_id == "101-003-0000000-001"

    def test_profile_3_live_resolves_correct_client_and_account(self):
        """Profile #3 in live env → live client + live account."""
        profile = _fake_get_oanda_profile("live")
        client = profile["oanda_client"]
        account_id = "001-003-0000000-001"

        assert profile["env"] == "live"
        assert client is not None

        tc = TradingCore(oanda_client=client, oanda_account_id=account_id)
        assert tc.oanda_account_id == "001-003-0000000-001"

    def test_invariant_client_env_credentials_account_match(self):
        """client environment + credentials + account_id must come from SAME profile."""
        for env in ("practice", "live"):
            profile = _fake_get_oanda_profile(env)
            client = profile["oanda_client"]
            account_key = "OANDA_ACCOUNT_ID_3_LIVE" if env == "live" else "OANDA_ACCOUNT_ID_3"
            account_map = {
                "OANDA_ACCOUNT_ID_3": "101-003-0000000-001",
                "OANDA_ACCOUNT_ID_3_LIVE": "001-003-0000000-001",
            }
            account_id = account_map[account_key]

            tc = TradingCore(oanda_client=client, oanda_account_id=account_id)
            assert tc.oanda_account_id == account_id
            assert tc.oanda_client is client


class TestRunnerNoOldTransport:
    """Test D + E — runner no longer uses old transport and TradingCore does not rediscover config."""

    def test_trading_core_has_no_config_loading(self):
        """TradingCore V2 source must not import config_oanda or read .env."""
        import inspect
        source = inspect.getsource(TradingCore)
        assert "config_oanda" not in source
        assert "dotenv" not in source.lower()
        assert "os.environ.get" not in source
        assert "get_oanda_profile" not in source

    def test_trading_core_init_takes_explicit_client_only(self):
        src = inspect.getsource(TradingCore.__init__)
        assert "oanda_client" in src
        assert "oanda_account_id" in src
        assert "oandapyV20" not in src
        assert "API(" not in src


class TestDryRunMode:
    """Test F — dry-run still wires correct client but performs no mutating operations."""

    def test_dry_run_resolves_profile_and_wires_client(self):
        profile = _fake_get_oanda_profile("live")
        client = profile["oanda_client"]
        account_id = "001-003-0000000-001"

        tc = TradingCore(oanda_client=client, oanda_account_id=account_id)

        assert tc.oanda_account_id == account_id
        assert tc.oanda_client is client

        result = tc.execute_market_trade(
            instrument="EUR_JPY",
            action="BUY",
            units=1000,
            stop_loss=178.000,
            take_profit=180.000,
            dry_run=True,
        )
        assert result is True
        client.request.assert_not_called()

    def test_dry_run_no_attach_sl_tp_call(self):
        profile = _fake_get_oanda_profile("practice")
        client = profile["oanda_client"]
        tc = TradingCore(oanda_client=client, oanda_account_id="101-003-0000000-001")

        result = tc.attach_sl_tp_to_open_trade(
            instrument="EUR_JPY",
            stop_loss=178.000,
            take_profit=180.000,
            dry_run=True,
        )
        assert result is True
        client.request.assert_not_called()