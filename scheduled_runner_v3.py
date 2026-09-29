"""
Scheduled Runner v3 — Multi-Group Base-Currency Strength Strategy
=================================================================
Architecture:
    • Global ONE build_strength_matrix() → ensures consistent currency strength baseline
    • Each group (JPY, USD, ...) gets its own subset → independent dominance filter → independent signals
    • Cross-group NET-EXPOSURE cap (CROSS_MAX_NET_PER_CCY) — supersedes the old
      CROSS_GROUP_MUTEX_* pair-level mutex, which is DEPRECATED and has no effect here
    • Strategy params ALL come from config_bot_v3.STRATEGY_GROUPS + generic v4 constants

Tag format: {GROUP_TAG_PREFIX}_{PAIR}_{SIDE}_{YYYYMMDD}
  e.g. JPY-STRENGTH_EUR_JPY_BUY_20250115
       USD-STRENGTH_EUR_USD_SELL_20250115
"""

import sys
import traceback
import argparse
import os
import fcntl
import re
import datetime as _dt_mod
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from typing import Any, Dict, List, Tuple

from risk_engine_v22 import (
    RiskManagementRunner,
    ReconcileStatus,
    TradeState,
)
from utils.trading_core import get_candles as _get_oanda_candles_raw

PROJECT_ROOT = Path(__file__).resolve().parent


def _parse_bool_env(val: str | bool) -> bool:
    if isinstance(val, bool):
        return val
    v = str(val).strip().lower()
    return v in ("1", "true", "t", "yes", "y", "on")


def _parse_macd_arg(val: str) -> bool | None:
    """Parse --use-macd VALUE → strict true/false; empty means None (let run.env/defaults win)."""
    if val is None:
        return None
    s = str(val).strip().lower()
    if s == "":
        return None
    if s in ("1", "true", "t", "yes", "y", "on"):
        return True
    if s in ("0", "false", "f", "no", "n", "off"):
        return False
    raise argparse.ArgumentTypeError(
        f"--use-macd expects one of: true/false/1/0/yes/no/on/off (case-insensitive); got '{val}'"
    )


_parser = argparse.ArgumentParser(description="Base-Currency Strength Strategy — Multi-Group v4")
_parser.add_argument("--profile", "-p", "--account", "-a", dest="profile", type=int, default=2)
_parser.add_argument("--live", action="store_true")
_parser.add_argument("--debug", type=int, choices=[1, 2, 3])
_parser.add_argument("--dry-run", action="store_true")
_parser.add_argument("--lots", type=int, default=None)
_parser.add_argument("--max-entries", "-n", type=int, default=1, help="Max signals to enter per cycle; 1=top only (default), 2+=basket")
_parser.add_argument(
    "--use-macd",
    dest="use_macd",
    type=_parse_macd_arg,
    nargs="?",
    const=True,
    default=None,
    metavar="true|false",
    help="Explicit MACD filter value: --use-macd true  or  --use-macd false  (highest priority; plain --use-macd defaults to true).",
)
_parser.add_argument(
    "--no-use-macd",
    "--no-macd",
    dest="use_macd_force_disable",
    action="store_true",
    default=False,
    help="[Deprecated, prefer --use-macd false] Explicitly disable MACD filter (overrides run.env USE_MACD=true).",
)

_args, _ = _parser.parse_known_args()

if _args.max_entries < 1:
    print(f"[CONFIG] ERROR: --max-entries must be >= 1, got {_args.max_entries}")
    sys.exit(2)

if _args.live:
    os.environ["OANDA_ENV"] = "live"

import config_oanda as _oanda_config
_oanda_profile = _oanda_config.get_oanda_profile("live" if _args.live else "practice")
_profile_name = f"profile{_args.profile}"
_account_suffix = "_LIVE" if _oanda_profile["env"] == "live" else ""
_account_key = f"OANDA_ACCOUNT_ID_{_args.profile}{_account_suffix}"
_account_id = getattr(_oanda_config, _account_key, "")
if not _account_id:
    print(f"[PROFILE] ERROR: {_profile_name} has no account configured in config_oanda")
    sys.exit(1)

_oanda_client = _oanda_profile["oanda_client"]
if _oanda_client is None:
    print("[PROFILE] ERROR: OANDA client construction failed — token may be missing")
    sys.exit(1)

from utils.trading_core_v2 import TradingCore
from config_oanda import is_market_open as _oanda_is_market_open

_dry_run_val = bool(_args.dry_run)
print(f"[CONFIG] dry_run = {_dry_run_val}")
try:
    _market_open_val = _oanda_is_market_open("EUR_USD")
    _market_closed_val = not _market_open_val
except Exception as _mc_exc:
    print(f"[CONFIG] WARNING: failed to determine market status via is_market_open(): {_mc_exc} — defaulting market_closed=False")
    _market_closed_val = False
print(f"[CONFIG] market_closed = {_market_closed_val}")

_trading_core = TradingCore(
    oanda_client=_oanda_client,
    oanda_account_id=_account_id,
    dry_run=_dry_run_val,
    market_closed=_market_closed_val,
)


def _get_bot_trades_for_instrument(instrument: str) -> List[Dict[str, Any]]:
    """
    Return only the BOT-OWNED open trades for an instrument.
    SAFETY: Manual trades on the same instrument are NEVER included.
    """
    try:
        all_trades = _trading_core.get_all_open_trades()
    except Exception as exc:
        print(f"  [BOT-FILTER] Cannot fetch open trades for {instrument}: {exc}")
        return []
    return [
        t for t in all_trades
        if t.get("instrument") == instrument and is_bot_owned_trade(t)
    ]


def _parse_oanda_openTime(ot) -> datetime | None:
    """
    Parse OANDA RFC3339-like openTime strings ("2026-09-21T21:18:05.464991501Z").
    Accepts str OR a v20 Trade object; returns timezone-aware UTC datetime or None.
    """
    if ot is None:
        return None
    s = ot if isinstance(ot, str) else getattr(ot, "openTime", None)
    if not isinstance(s, str) or not s:
        return None
    try:
        head = s.replace("Z", "+00:00")
        return datetime.fromisoformat(head)
    except Exception:
        try:
            m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$", s)
            if m:
                base = m.group(1)
                frac = (m.group(2) or "")[:6].ljust(6, "0")
                tz = m.group(3).replace("Z", "+00:00")
                return datetime.fromisoformat(f"{base}.{frac}{tz}")
        except Exception:
            return None
    return None


def _find_open_trade_by_id(trade_id: str) -> Dict[str, Any] | None:
    """Return the raw open-trade dict for a given trade_id, or None."""
    try:
        for t in _trading_core.get_all_open_trades():
            t_id = str(t.get("id", "") or t.get("tradeID", ""))
            if t_id == str(trade_id):
                return t
    except Exception:
        pass
    return None


# =====================================================================
# HARD-INVARIANT OWNERSHIP GATES
# ---------------------------------------------------------------------
# Every caller that intends to MUTATE broker state (close / modify SL)
# MUST route through one of these gates BEFORE touching TradingCore.
#
# Instrument is ONLY for DISCOVERY; the AUTHORIZATION identity is
# always `is_bot_owned_trade` + concrete `trade_id`.
#
# If any gate detects a non-bot-owned trade_id, it:
#   1. prints a loud warning,
#   2. returns False / skips the mutation (fail-CLOSED),
#   3. NEVER proceeds to the underlying broker API call.
# =====================================================================

def _gated_close_trade_by_id(
    trade_id: str,
    *,
    client_request_id: str,
    gate_name: str = "GATE",
) -> tuple[bool, dict[str, Any]]:
    """
    Ownership-gated close. REFUSES to close any trade not bot-owned.

    Returns: (success: bool, info: dict)
        info may include realizedPL / instrument / units etc. on success.
    """
    trade = _find_open_trade_by_id(trade_id)
    if trade is None:
        print(
            f"  [{gate_name}] SKIP close T{trade_id}: "
            f"trade not in OpenTrades (already closed?)"
        )
        return False, {"skipped": "not in open trades"}
    if not is_bot_owned_trade(trade):
        tag = trade.get("clientExtensions", {}).get("tag", "<no-tag>")
        inst = trade.get("instrument", "?")
        print(
            f"  ⚠️  [{gate_name}] OWNERSHIP MISMATCH → REFUSE close T{trade_id} "
            f"{inst}. tag={tag!r}. This trade is NOT bot-owned — hard invariant."
        )
        return False, {"refused": "not bot-owned"}
    ok, info = _trading_core.close_trade_by_id(
        str(trade_id), client_request_id=client_request_id
    )
    if ok and isinstance(info, dict):
        pl = info.get("realizedPL")
        parts = [f"T{trade_id}"]
        if pl not in (None, ""):
            parts.append(f"realizedPL={pl}")
        if "units" in info:
            parts.append(f"units={info['units']}")
        if "price" in info:
            parts.append(f"closePx={info['price']}")
        print(f"  [{gate_name}] CLOSE OK: " + " | ".join(parts))
    elif not ok:
        err = info.get("error") if isinstance(info, dict) else ""
        print(f"  [{gate_name}] CLOSE FAIL: T{trade_id} err={err}")
    return bool(ok), (info if isinstance(info, dict) else {})


def _gated_update_sl_by_id(
    trade_id: str,
    new_sl: float,
    *,
    client_request_id: str,
    gate_name: str = "GATE",
) -> bool:
    """Ownership-gated SL update. REFUSES to modify SL on non-bot trades."""
    trade = _find_open_trade_by_id(trade_id)
    if trade is None:
        print(
            f"  [{gate_name}] SKIP SL-update T{trade_id}: "
            f"trade not in OpenTrades (already closed?)"
        )
        return False
    if not is_bot_owned_trade(trade):
        tag = trade.get("clientExtensions", {}).get("tag", "<no-tag>")
        inst = trade.get("instrument", "?")
        print(
            f"  ⚠️  [{gate_name}] OWNERSHIP MISMATCH → REFUSE SL-update T{trade_id} "
            f"{inst} SL→{new_sl}. tag={tag!r}. Hard invariant."
        )
        return False
    return _trading_core.update_trade_sl_only(
        str(trade_id), float(new_sl), client_request_id=client_request_id
    )


def _gated_attach_sltp_by_id(
    trade_id: str,
    instrument: str,
    stop_loss: float,
    take_profit: float,
    *,
    client_request_id: str | None = None,
    gate_name: str = "GATE",
    dry_run: bool = False,
) -> bool:
    """Ownership-gated SL/TP attach. REFUSES non-bot trades."""
    trade = _find_open_trade_by_id(trade_id)
    if trade is None:
        print(
            f"  [{gate_name}] SKIP attach-SLTP T{trade_id}: "
            f"trade not in OpenTrades (already closed?)"
        )
        return False
    if not is_bot_owned_trade(trade):
        tag = trade.get("clientExtensions", {}).get("tag", "<no-tag>")
        inst = trade.get("instrument", "?")
        print(
            f"  ⚠️  [{gate_name}] OWNERSHIP MISMATCH → REFUSE attach-SLTP "
            f"T{trade_id} {inst}. tag={tag!r}. Hard invariant."
        )
        return False
    return _trading_core.attach_sl_tp_to_trade_id(
        trade_id=str(trade_id),
        instrument=instrument,
        stop_loss=float(stop_loss),
        take_profit=float(take_profit),
        dry_run=dry_run,
        client_request_id=client_request_id,
    )


# ---------------------------------------------------------------------
# Convenience: close ALL bot-owned trades on one instrument via gated calls
# ---------------------------------------------------------------------
def _close_bot_trades_for_instrument(
    instrument: str,
    req_id_prefix: str = "CLOSE_BOT",
) -> tuple[bool, dict]:
    """
    SAFETY-FIRST close. Closes ONLY bot-owned trades on `instrument`,
    one by one via the ownership-gated wrapper. Manual positions on the
    same instrument are NEVER enumerated, NEVER authorized, NEVER touched.

    Replaces the former `close_pair_position()` which did a DANGEROUS
    position-level (instrument-level) flat-close.

    Returns: (ok, detail_dict)
    """
    bot_trades = _get_bot_trades_for_instrument(instrument)
    if not bot_trades:
        return True, {"status": "already_flat", "closed_count": 0}

    closed = 0
    failed = 0
    last_err = ""
    for t in bot_trades:
        trade_id = str(t.get("id", "") or t.get("tradeID", ""))
        if not trade_id:
            failed += 1
            last_err = "missing_trade_id"
            continue
        try:
            ok, _c_info = _gated_close_trade_by_id(
                trade_id,
                client_request_id=f"{req_id_prefix}_{instrument}_T{trade_id}",
                gate_name="CLOSE-BY-INSTR",
            )
            if ok:
                closed += 1
                _pl = _c_info.get("realizedPL") if isinstance(_c_info, dict) else None
                _pl_str = f" pl={_pl}" if _pl is not None else ""
                print(f"  [BOT-CLOSE] ✅ T{trade_id} {instrument} closed OK{_pl_str}")
            else:
                failed += 1
                last_err = f"gated_close refused or failed for T{trade_id}"
        except Exception as exc:
            failed += 1
            last_err = f"T{trade_id}: {exc}"
            print(f"  [BOT-CLOSE] ❌ T{trade_id} {instrument} exception: {exc}")

    detail = {
        "status": "closed" if failed == 0 else "partial",
        "closed_count": closed,
        "failed_count": failed,
        "last_error": last_err,
    }
    ok = (failed == 0) and (closed > 0)
    return ok, detail


class _TradingCoreRiskAdapter:
    """
    Adapter that wraps TradingCore to match the Broker interface contract
    required by RiskManagementRunner (risk_engine_v22.py).
    Converts raw TradingCore return values → risk engine enum types.

    HARD-INVARIANT: Every MUTATING call (close / update SL) is routed through
    the ownership gate BEFORE touching TradingCore. The risk engine hands us
    a `trade_id` but we re-verify `is_bot_owned_trade` on the live OpenTrades
    snapshot — the risk engine's memory is NOT the authorization source.
    """

    def __init__(self, tc: TradingCore):
        self._tc = tc
        self._spec_cache: Dict[str, Tuple[float, float]] = {}

    def query_trade_state(self, trade_id: str) -> Tuple[TradeState, float]:
        state_raw, units = self._tc.query_trade_raw(trade_id)
        if state_raw == "EXISTS":
            return TradeState.EXISTS, float(units)
        if state_raw == "CLOSED":
            return TradeState.CLOSED, 0.0
        return TradeState.UNKNOWN, float(units)

    def send_close_order(self, trade_id: str, client_request_id: str) -> bool:
        ok, _ = _gated_close_trade_by_id(
            str(trade_id),
            client_request_id=client_request_id,
            gate_name="RISK-ENGINE",
        )
        return bool(ok)

    def update_trade_sl(self, trade_id: str, new_sl: float, client_request_id: str) -> bool:
        return _gated_update_sl_by_id(
            str(trade_id),
            float(new_sl),
            client_request_id=client_request_id,
            gate_name="RISK-ENGINE",
        )

    def get_instrument_spec(self, symbol: str) -> Tuple[float, float]:
        if symbol in self._spec_cache:
            return self._spec_cache[symbol]
        raw = self._tc.get_instrument_spec_raw(symbol)
        result = (float(raw["tick_size"]), float(raw["min_stop_distance"]))
        self._spec_cache[symbol] = result
        return result

    def send_close_trade_order(self, trade_id, req_id):
        return self.send_close_order(trade_id, req_id)


_risk_adapter = _TradingCoreRiskAdapter(_trading_core)
_risk_state_path = str(PROJECT_ROOT / "risk_state.json")
_risk_runner = RiskManagementRunner(broker_adapter=_risk_adapter, state_path=_risk_state_path)


def _convert_oanda_candles(candles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Convert OANDA native candle format (mid.c / mid.h / mid.l / mid.o)
    to the flat format required by risk_engine_v22 (close/high/low + complete + time).
    """
    out: List[Dict[str, Any]] = []
    for c in candles:
        mid = c.get("mid", {})
        entry = {
            "complete": bool(c.get("complete", False)),
            "time": str(c.get("time", "")),
            "close": float(mid.get("c", 0.0)),
            "high": float(mid.get("h", 0.0)),
            "low": float(mid.get("l", 0.0)),
            "open": float(mid.get("o", 0.0)),
        }
        out.append(entry)
    return out


def _fetch_h1_candles_risk(instrument: str, count: int = 40) -> List[Dict[str, Any]]:
    """Fetch H1 candles and convert to risk-engine flat format."""
    raw = _get_oanda_candles_raw(instrument, "H1", count=count)
    return _convert_oanda_candles(raw)


def _fetch_daily_candles_risk(instrument: str, count: int = 120) -> List[Dict[str, Any]]:
    """Fetch Daily candles and convert to risk-engine flat format."""
    raw = _get_oanda_candles_raw(instrument, "D", count=count)
    return _convert_oanda_candles(raw)


import config as _config
import config_bot_v3 as _config_bot

if _profile_name not in _config_bot.PROFILE_CFG:
    print(f"[PROFILE] ERROR: {_profile_name} is not defined in config_bot_v3")
    sys.exit(1)

_PROFILE_CFG = _config_bot.load_profile(_profile_name)
_PROFILE_CFG["OANDA_ACCOUNT_ID"] = _account_id
for _key, _value in _PROFILE_CFG.items():
    setattr(_config, _key, _value)

# ========== Load run.env (silently skip if missing) ==========
_ENV_LOADED_KEYS: Dict[str, str] = {}
_run_env_path = PROJECT_ROOT / "run.env"
if _run_env_path.exists():
    load_dotenv(_run_env_path, override=False)
    # --- v3 run.env PARAMETER WHITELIST ---
    # Priority rule (per user spec): run.env value wins; if missing fall back
    # to getattr(_config_bot, key); if that's also missing, use a hardcoded
    # fallback.  Important parameters with an EXISTING CLI flag intentionally
    # do NOT change behaviour (CLI still wins via its own argparse logic).
    _candidates = {
        "USE_MACD",
        "LIVE_LOT_SIZE",
        "DEMO_LOT_SIZE",
        "CROSS_MAX_NET_PER_CCY",  # Per-ccy net exposure cap
    }
    # Also recognise MACD_TF_PARAMS env overrides: MACD_<TF>_<KEY>
    _macd_tf_keys = ("H4", "H1", "M30", "M15", "M5")
    _macd_param_keys = ("FAST", "SLOW", "SIGNAL")
    for _tf in _macd_tf_keys:
        for _k in _macd_param_keys:
            _candidates.add(f"MACD_{_tf}_{_k}")
    for _k in sorted(_candidates):
        _v = os.environ.get(_k)
        if _v is not None and str(_v).strip() != "":
            _ENV_LOADED_KEYS[_k] = str(_v).strip()
            if not _k.startswith("MACD_"):
                print(f"[CONFIG] loaded from run.env: {_k}={_ENV_LOADED_KEYS[_k]}")
else:
    print(f"[CONFIG] run.env not found at {_run_env_path} — skipping (using defaults/CLI)")


def _env_or_config(key: str, fallback, *, cfg_module=None, value_type=None):
    """Resolve a parameter respecting the run.env-first priority rule.

    Lookup order:
      1. run.env (via os.environ.get / _ENV_LOADED_KEYS)
      2. getattr on cfg_module (default: _config_bot from enclosing scope)
      3. hardcoded fallback

    value_type: if supplied, the run.env string value is coerced via
      value_type(raw) with graceful fallback on failure (falls through to
      layers 2 / 3 instead of raising).
    """
    mod = cfg_module if cfg_module is not None else _config_bot
    # Layer 1: run.env
    raw = _ENV_LOADED_KEYS.get(key)
    if raw is None:
        # fallback: direct os.environ (in case caller exported externally)
        raw = os.environ.get(key)
    if raw is not None and str(raw).strip() != "":
        if value_type is None:
            return str(raw).strip()
        try:
            if value_type is bool:
                s = str(raw).strip().lower()
                if s in ("1", "true", "yes", "on", "y", "t"):
                    return True
                if s in ("0", "false", "no", "off", "n", "f"):
                    return False
                raise ValueError(f"not a boolean: {raw}")
            return value_type(raw)
        except (TypeError, ValueError):
            print(
                f"[CONFIG] WARNING: run.env {key}={raw!r} is not a valid "
                f"{value_type.__name__ if value_type else 'string'} — ignoring"
            )
    # Layer 2: config module attribute
    if hasattr(mod, key):
        return getattr(mod, key)
    # Layer 3: caller-supplied fallback
    return fallback


# ========== Resolve MACD_TF_PARAMS: run.env MACD_<TF>_<KEY> > config default (12/26/9 fallback)
#            CLI NOT involved — as requested (per user requirement "不设命令行参数")
_MACD_TF_PARAMS_DEFAULT: Dict[str, Dict[str, int]] = {
    "H4": {"fast": 12, "slow": 26, "signal": 9},
    "H1": {"fast": 12, "slow": 26, "signal": 9},
    "M30": {"fast": 12, "slow": 26, "signal": 9},
    "M15": {"fast": 12, "slow": 26, "signal": 9},
    "M5":  {"fast": 12, "slow": 26, "signal": 9},
}
_MACD_TF_PARAMS_EFFECTIVE: Dict[str, Dict[str, int]]
# Start by cloning config-level default if present
_cfg_macd = getattr(_config_bot, "MACD_TF_PARAMS", None)
if isinstance(_cfg_macd, dict):
    _MACD_TF_PARAMS_EFFECTIVE = {tf: dict(_MACD_TF_PARAMS_DEFAULT[tf]) for tf in _MACD_TF_PARAMS_DEFAULT}
    for _tf, _vals in _cfg_macd.items():
        if _tf in _MACD_TF_PARAMS_EFFECTIVE and isinstance(_vals, dict):
            for _k in ("fast", "slow", "signal"):
                if _k in _vals:
                    try:
                        v = int(_vals[_k])
                        if v > 0:
                            _MACD_TF_PARAMS_EFFECTIVE[_tf][_k] = v
                    except (TypeError, ValueError):
                        pass
else:
    _MACD_TF_PARAMS_EFFECTIVE = {tf: dict(v) for tf, v in _MACD_TF_PARAMS_DEFAULT.items()}

# Overlay run.env overrides (highest priority: MACD_<TF>_<FAST|SLOW|SIGNAL>)
_MACD_ENV_APPLIED: Dict[str, Dict[str, int]] = {}
for _tf in list(_MACD_TF_PARAMS_EFFECTIVE.keys()):
    for _env_key_name, _param_key in (("FAST", "fast"), ("SLOW", "slow"), ("SIGNAL", "signal")):
        _env_var = f"MACD_{_tf}_{_env_key_name}"
        if _env_var in _ENV_LOADED_KEYS:
            try:
                v = int(_ENV_LOADED_KEYS[_env_var])
                if v > 0:
                    _MACD_TF_PARAMS_EFFECTIVE[_tf][_param_key] = v
                    _MACD_ENV_APPLIED.setdefault(_tf, {})[_param_key] = v
                else:
                    print(f"[CONFIG] WARNING: {_env_var}={_ENV_LOADED_KEYS[_env_var]} ignored (must be >= 1)")
            except (TypeError, ValueError):
                print(f"[CONFIG] WARNING: {_env_var}={_ENV_LOADED_KEYS[_env_var]} ignored (not a positive int)")
if _MACD_ENV_APPLIED:
    parts = []
    for _tf, _vals in _MACD_ENV_APPLIED.items():
        parts.append(_tf + "(" + ",".join(f"{k}={v}" for k, v in _vals.items()) + ")")
    print("[CONFIG] MACD params overridden from run.env: " + " | ".join(parts))
else:
    print("[CONFIG] MACD params: using config defaults (per-TF via MACD_TF_PARAMS)")
# Print final MACD effective
print("[CONFIG] MACD effective params:")
for _tf in ("H4", "H1", "M30", "M15", "M5"):
    p = _MACD_TF_PARAMS_EFFECTIVE.get(_tf, _MACD_TF_PARAMS_DEFAULT[_tf])
    print(
        f"           {_tf}: "
        f"fast={p['fast']} slow={p['slow']} signal={p['signal']}"
    )
# Publish resolved params to strategy_helpers global override so that even
# callers in custom_strategy_v3.py (and any future module) pick them up
# WITHOUT needing to thread parameters through every function.
# NOTE: set_macd_tf_params lives in utils.strategy_helpers and is imported
# later in the file (after the profile-dependent imports).  We resolve it
# dynamically here so that the MACD-resolution block can remain near the
# top of the file alongside other run.env-driven config.
try:
    import importlib as _importlib
    _sh = _importlib.import_module("utils.strategy_helpers")
    getattr(_sh, "set_macd_tf_params")(_MACD_TF_PARAMS_EFFECTIVE)
    del _importlib, _sh
except Exception as _exc:
    print(f"[CONFIG] WARNING: set_macd_tf_params failed: {_exc}")
# End MACD_TF_PARAMS resolution block ===============

# ========== Resolve EARLY-EXIT (runner-initiated close) parameters ==========
# Priority: run.env > config_bot_v3 defaults.  No CLI (per user rule).
# Philosophy: NEVER exit on noise — ALL gates must fire simultaneously.
def _ee_int(k: str, default: int) -> int:
    if k in _ENV_LOADED_KEYS:
        try: return int(_ENV_LOADED_KEYS[k])
        except Exception: print(f"[CONFIG] WARNING: bad env {k}={_ENV_LOADED_KEYS[k]!r} → use default {default}")
    return getattr(_config_bot, k, default)

def _ee_float(k: str, default: float) -> float:
    if k in _ENV_LOADED_KEYS:
        try: return float(_ENV_LOADED_KEYS[k])
        except Exception: print(f"[CONFIG] WARNING: bad env {k}={_ENV_LOADED_KEYS[k]!r} → use default {default}")
    return getattr(_config_bot, k, default)

def _ee_bool(k: str, default: bool) -> bool:
    if k in _ENV_LOADED_KEYS:
        return _parse_bool_env(_ENV_LOADED_KEYS[k])
    return bool(getattr(_config_bot, k, default))

def _ee_list(k: str, default: list) -> list:
    if k in _ENV_LOADED_KEYS:
        raw = _ENV_LOADED_KEYS[k]
        try:
            parts = [p.strip().upper() for p in raw.replace(";", ",").split(",") if p.strip()]
            if parts: return parts
        except Exception:
            print(f"[CONFIG] WARNING: bad env {k}={raw!r} → use default {default}")
    return list(getattr(_config_bot, k, default))

_EARLY_EXIT_CFG = {
    "MIN_HOLD_MIN":              _ee_int("EARLY_EXIT_MIN_HOLD_MINUTES", 180),
    "OVERRIDE_MIN_HOLD_MIN":     _ee_int("EARLY_EXIT_OVERRIDE_MIN_HOLD_MINUTES", 10080),
    "OVERRIDE_DISABLE_RUNNER":   _ee_bool("EARLY_EXIT_OVERRIDE_DISABLE_RUNNER_CLOSE", True),
    "MA_REQ_NORMAL":             _ee_float("EARLY_EXIT_REQUIRE_MA_ALIGNED_NORMAL", 2.4),
    "MA_REQ_OVERRIDE":           _ee_float("EARLY_EXIT_REQUIRE_MA_ALIGNED_OVERRIDE", 3.5),
    "MACD_AGREE_TF":             _ee_int("EARLY_EXIT_REQUIRE_MACD_AGREE_TF_COUNT", 2),
    "STRENGTH_REV_ABS":          _ee_float("EARLY_EXIT_STRENGTH_REVERSAL_MIN_ABS", 0.5),
    "STRENGTH_REV_RANK_DROP":    _ee_int("EARLY_EXIT_STRENGTH_REVERSAL_MIN_RANK_DROP", 2),
    "ALLOW_AT_LOSS":             _ee_bool("EARLY_EXIT_ALLOW_AT_LOSS", False),
    "TF_MA":                     _ee_list("EARLY_EXIT_TF_LIST_MA", ["H4","H1","M30"]),
    "TF_MACD":                   _ee_list("EARLY_EXIT_TF_LIST_MACD", ["H4","H1","M30"]),
    "REQUIRE_H4":                _ee_bool("EARLY_EXIT_REQUIRE_H4_CONFIRM", True),
}
print("[CONFIG] EARLY-EXIT effective (philosophy: ALL gates must fire):")
print(f"           MIN_HOLD (normal/override): {_EARLY_EXIT_CFG['MIN_HOLD_MIN']} / {_EARLY_EXIT_CFG['OVERRIDE_MIN_HOLD_MIN']} min")
print(f"           OVERRIDE runner-close: {'DISABLED (broker SL/TP only)' if _EARLY_EXIT_CFG['OVERRIDE_DISABLE_RUNNER'] else 'ENABLED (very strict gate)'}")
print(f"           MA req aligned (normal/override): {_EARLY_EXIT_CFG['MA_REQ_NORMAL']} / {_EARLY_EXIT_CFG['MA_REQ_OVERRIDE']}")
print(f"           MACD agree TFs (min): {_EARLY_EXIT_CFG['MACD_AGREE_TF']}  TF_MA={_EARLY_EXIT_CFG['TF_MA']}  TF_MACD={_EARLY_EXIT_CFG['TF_MACD']}")
print(f"           STRENGTH_REV: abs≥{_EARLY_EXIT_CFG['STRENGTH_REV_ABS']} OR rank-drop≥{_EARLY_EXIT_CFG['STRENGTH_REV_RANK_DROP']}")
print(f"           ALLOW_AT_LOSS: {_EARLY_EXIT_CFG['ALLOW_AT_LOSS']} | REQUIRE_H4_CONFIRM: {_EARLY_EXIT_CFG['REQUIRE_H4']}")
# ========== End EARLY-EXIT resolution ==========

# ========== Resolve USE_MACD: CLI --use-macd true|false > run.env USE_MACD > config default ==========
_cli_macd_explicit: bool | None = None
if _args.use_macd is not None:
    _cli_macd_explicit = _parse_bool_env(_args.use_macd)
    _USE_MACD_SOURCE = "cli (--use-macd " + ("true" if _cli_macd_explicit else "false") + ")"
if _args.use_macd_force_disable:
    if _cli_macd_explicit is None:
        _cli_macd_explicit = False
        _USE_MACD_SOURCE = "cli (--no-macd, deprecated; prefer --use-macd false)"
    elif _cli_macd_explicit:
        print("[CONFIG] WARNING: both --use-macd true AND --no-macd passed; --no-macd takes precedence (deprecated flag).")
        _cli_macd_explicit = False
        _USE_MACD_SOURCE = "cli (--no-macd overrides --use-macd true; deprecated; prefer --use-macd false)"

if _cli_macd_explicit is not None:
    _USE_MACD_EFFECTIVE = _cli_macd_explicit
elif "USE_MACD" in _ENV_LOADED_KEYS:
    _USE_MACD_EFFECTIVE = _parse_bool_env(_ENV_LOADED_KEYS["USE_MACD"])
    _USE_MACD_SOURCE = f"run.env USE_MACD={_ENV_LOADED_KEYS['USE_MACD']}"
else:
    _USE_MACD_EFFECTIVE = getattr(_config_bot, "USE_MACD", True)
    _USE_MACD_SOURCE = "defaults"
print(f"[CONFIG] USE_MACD = {_USE_MACD_EFFECTIVE}  (source: {_USE_MACD_SOURCE})")

setattr(_config_bot, "USE_MACD", _USE_MACD_EFFECTIVE)
setattr(_config, "USE_MACD", _USE_MACD_EFFECTIVE)

RUNNER_VERSION = "3.0.0"
PRICE_PRECISION_TOL = 0.001
STRATEGY_UPDATE_THRESHOLD = 0.005

EMERGENCY_LOCK_FILE = Path(__file__).resolve().parent / ".emergency_close_lock_v3"


def _acquire_profile_lock(profile: int):
    lock_path = Path(f"/tmp/runner_v3_{profile}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"pid:{os.getpid()} start:{datetime.now(timezone.utc).isoformat()}\n")
        lock_file.flush()
        return lock_file
    except BlockingIOError:
        print(f"[LOCK] Another v3 runner (profile {profile}) is active — exiting.")
        sys.exit(0)


def _check_emergency_lock() -> bool:
    return EMERGENCY_LOCK_FILE.exists()


def _set_emergency_lock(info: str = "") -> None:
    EMERGENCY_LOCK_FILE.write_text(
        f"time:{datetime.now(timezone.utc).isoformat()} pid:{os.getpid()} info:{info}\n"
    )


def _clear_emergency_lock() -> None:
    if EMERGENCY_LOCK_FILE.exists():
        EMERGENCY_LOCK_FILE.unlink()


def _emergency_close_all(account_id: str = None) -> dict:
    """Close ALL bot-owned trades (SAFE: never touches manual positions).

    HARD-INVARIANT: Even in emergency we route through the ownership gate.
    "Emergency" is NEVER a license to bypass the ownership filter.
    """
    result = {"closed": 0, "errors": [], "skipped_manual": 0}
    try:
        all_trades = _trading_core.get_all_open_trades()
    except Exception as exc:
        result["errors"].append(f"fetch_failed: {exc}")
        return result

    _strategy_prefixes = {cfg["tag_prefix"] for cfg in _strategy_groups.values()}

    for trade in all_trades:
        inst = trade.get("instrument", "")
        trade_id = str(trade.get("id", "") or trade.get("tradeID", ""))
        tags = trade.get("clientExtensions", {}).get("tag", "")

        if not is_bot_owned_trade(trade):
            result["skipped_manual"] += 1
            continue
        raw_tag = tags.split("::")[-1] if "::" in tags else tags
        if not any(pfx in raw_tag for pfx in _strategy_prefixes):
            continue
        if not trade_id:
            result["errors"].append(f"{inst}: missing trade_id")
            continue
        try:
            ok, _emg_info = _gated_close_trade_by_id(
                trade_id,
                client_request_id=f"EMG_BOT_{inst}_T{trade_id}",
                gate_name="EMERGENCY",
            )
            if ok:
                result["closed"] += 1
                _pl = _emg_info.get("realizedPL") if isinstance(_emg_info, dict) else None
                _pl_str = f" pl={_pl}" if _pl is not None else ""
                print(f"  [EMERGENCY] ✅ Closed T{trade_id} {inst}{_pl_str}")
            else:
                result["errors"].append(
                    f"{inst} T{trade_id}: gate refused or close failed"
                )
        except Exception as exc:
            result["errors"].append(f"{inst} T{trade_id}: {exc}")

    _set_emergency_lock(f"emergency_close_all_v3 account={account_id} closed={result['closed']}")
    print(
        f"[EMERGENCY] Closed {result['closed']} bot trades. "
        f"Skipped {result['skipped_manual']} manual. Lock set."
    )
    return result


def _parse_comment_sltp(comment: str) -> dict | None:
    """Parse strategy comment → {entry, SL, TP} plus optional {sc,rk,...}.

    Uses utils.utils.parse_strategy_comment (handles both old "v3 a=x b=y"
    space format and new "v3|a=x|b=y" pipe format, and returns aliases
    open_strength_score/open_strength_rank automatically).
    """
    from utils.utils import parse_strategy_comment
    out: dict | None = None
    try:
        parsed = parse_strategy_comment(comment or "")
    except Exception:
        parsed = {}
    if not parsed:
        return None
    entry = parsed.get("entry") or parsed.get("entry_f")
    sl    = parsed.get("SL")    or parsed.get("SL_f")
    tp    = parsed.get("TP")    or parsed.get("TP_f")
    try:
        if entry is None or sl is None or tp is None:
            return None
        entry = float(entry); sl = float(sl); tp = float(tp)
        out = {"entry": entry, "SL": sl, "TP": tp}
        # Also propagate optional strength / rank fields for audit print.
        if "open_strength_score" in parsed and parsed["open_strength_score"] is not None:
            out["sc"] = float(parsed["open_strength_score"])
        elif "sc" in parsed and parsed["sc"] not in (None, ""):
            try: out["sc"] = float(parsed["sc"])
            except Exception: pass
        if "open_strength_rank" in parsed and parsed["open_strength_rank"] is not None:
            out["rk"] = int(parsed["open_strength_rank"])
        elif "rk" in parsed and parsed["rk"] not in (None, ""):
            try: out["rk"] = int(parsed["rk"])
            except Exception: pass
        if "override" in parsed:
            out["override"] = parsed["override"]
        return out
    except (ValueError, TypeError):
        return out if out else None


def _sltp_guardian(dry_run: bool = False) -> dict:
    """Audit open bot-owned strategy trades → re-attach SL/TP if broker dropped them.

    SAFETY:
      • Only processes trades passing is_bot_owned_trade() — manual trades are skipped.
      • Uses attach_sl_tp_to_trade_id() with the concrete trade_id of EACH
        individual trade — NEVER resolves to "first trade of position" which
        could accidentally attach SL/TP onto a sibling manual position on the
        same instrument.
    """
    report = {"scanned": 0, "sl_repaired": 0, "tp_repaired": 0, "failed": 0, "skipped_manual": 0}
    try:
        open_trades = _trading_core.get_all_open_trades()
    except Exception as exc:
        print(f"  [SL/TP GUARDIAN] Fetch failed: {exc}")
        report["failed"] += 1
        return report

    strategy_trades: List[Dict[str, Any]] = []
    for t in open_trades:
        if not is_bot_owned_trade(t):
            report["skipped_manual"] += 1
            continue
        tag = t.get("clientExtensions", {}).get("tag", "") or ""
        raw_tag = tag.split("::")[-1] if "::" in tag else tag
        if any(pfx in raw_tag for pfx in ("JPY-STRENGTH", "USD-STRENGTH", "CHF-STRENGTH")):
            strategy_trades.append(t)
    if not strategy_trades:
        return report

    print(
        f"  [SL/TP GUARDIAN] Auditing {len(strategy_trades)} strategy trade(s) "
        f"(skipped_manual={report['skipped_manual']})..."
    )

    for trade in strategy_trades:
        report["scanned"] += 1
        inst = trade.get("instrument", "")
        cid = str(trade.get("id", "") or trade.get("tradeID", ""))
        if not cid:
            print(f"    [SKIP] {inst}: missing trade_id")
            continue
        comment = trade.get("clientExtensions", {}).get("comment", "")
        parsed = _parse_comment_sltp(comment)
        if not parsed:
            continue

        current_sl = trade.get("stopLossOrder", {}).get("price")
        current_tp = trade.get("takeProfitOrder", {}).get("price")
        want_sl = parsed["SL"]
        want_tp = parsed["TP"]
        missing_sl = current_sl is None
        missing_tp = current_tp is None

        # ---- Pretty-print a single audit line like v1.4.4 used to, so we
        # can visually verify ATR-calculated SL/TP actually propagated into
        # the broker (and weren't ±0.200 / ±1.000 placeholders):
        entry  = parsed.get("entry")
        sc_str = ""
        if "sc" in parsed and parsed["sc"] not in (None, ""):
            sc_str = f" sc={parsed['sc']}"
        elif "open_strength_score" in parsed and parsed["open_strength_score"] is not None:
            try: sc_str = f" sc={float(parsed['open_strength_score']):+.4f}"
            except Exception: sc_str = ""
        rk_str = ""
        if ("rk" in parsed and parsed["rk"] not in (None, "")) or \
           (parsed.get("open_strength_rank") is not None):
            rk = parsed.get("rk") if parsed.get("rk") not in (None, "") else parsed.get("open_strength_rank")
            rk_str = f" rk={rk}"
        cur_s  = f"SL={current_sl or '—'}"
        cur_t  = f"TP={current_tp or '—'}"
        cal_s  = f"CALC_SL={want_sl:.5f}"
        cal_t  = f"CALC_TP={want_tp:.5f}"
        entry_s = f"entry={entry:.5f}" if entry is not None else "entry=n/a"
        decision = "SKIP(ok)"
        if missing_sl or missing_tp:
            missing_parts = []
            if missing_sl: missing_parts.append("SL")
            if missing_tp: missing_parts.append("TP")
            decision = "REPAIR(" + "+".join(missing_parts) + ")"
        else:
            # Also flag if broker value differs meaningfully from CALC
            # (drift protection in case comment was edited mid-run).
            _sl_diff_ok = abs(float(current_sl) - float(want_sl)) < 0.0005 if not missing_sl else True
            _tp_diff_ok = abs(float(current_tp) - float(want_tp)) < 0.0005 if not missing_tp else True
            if not _sl_diff_ok or not _tp_diff_ok:
                drift = []
                if not _sl_diff_ok: drift.append("SL-drift")
                if not _tp_diff_ok: drift.append("TP-drift")
                decision = "WILL-REPAIR(" + "+".join(drift) + ")"
                missing_sl = missing_sl or (not _sl_diff_ok)
                missing_tp = missing_tp or (not _tp_diff_ok)
        print(
            f"    T{cid} {inst} {entry_s} | {cur_s} {cur_t}  "
            f"| {cal_s} {cal_t} | {decision}{sc_str}{rk_str}"
        )

        if not (missing_sl or missing_tp):
            continue

        print(
            f"      → repairing (trade_id-specific attach: "
            f"SL missing/drifted={missing_sl}, TP missing/drifted={missing_tp})"
        )
        if dry_run:
            continue

        try:
            ok = _gated_attach_sltp_by_id(
                trade_id=cid,
                instrument=inst,
                stop_loss=want_sl,
                take_profit=want_tp,
                dry_run=False,
                client_request_id=f"GUARD_SLTP_{inst}_T{cid}",
                gate_name="SLTP-GUARDIAN",
            )
            if ok:
                if missing_sl:
                    report["sl_repaired"] += 1
                if missing_tp:
                    report["tp_repaired"] += 1
                print(f"      ✅ Repaired T{cid}")
            else:
                report["failed"] += 1
                print(f"      ❌ Repair failed or gate-refused T{cid}")
        except Exception as exc:
            report["failed"] += 1
            print(f"      ❌ Repair exception T{cid}: {exc}")

    print(
        f"  [SL/TP GUARDIAN] Scanned={report['scanned']} "
        f"SL_repaired={report['sl_repaired']} TP_repaired={report['tp_repaired']} "
        f"failed={report['failed']} skipped_manual={report['skipped_manual']}"
    )
    return report


def _print_dxy_reference(global_scores: dict | None = None) -> None:
    """Print DXY reference — try real yfinance first, fallback to homemade proxy."""
    print("\n  === DXY REFERENCE ===")
    real_dxy = None
    try:
        import yfinance as yf
        hist = yf.Ticker("DX-Y.NYB").history(period="3d", interval="1d", timeout=5)
        if len(hist) >= 2:
            latest = hist["Close"].iloc[-1]
            prev = hist["Close"].iloc[-2]
            real_dxy = {"value": round(latest, 2), "chg_pct": round((latest - prev) / prev * 100, 2)}
    except Exception:
        pass

    proxy = None
    if global_scores and "USD" in global_scores:
        usd_score = global_scores["USD"]
        proxy_trend = "STRONG ▲" if usd_score > 1.5 else "MILD ▲" if usd_score > 0.3 else "NEUTRAL ═" if usd_score > -0.3 else "MILD ▼" if usd_score > -1.5 else "STRONG ▼"
        proxy = {"score": usd_score, "trend": proxy_trend}

    if real_dxy:
        print(f"  [REAL] yfinance DXY = {real_dxy['value']} ({real_dxy['chg_pct']:+.2f}%)")
    else:
        print("  [REAL] yfinance unavailable (offline or blocked)")
    if proxy:
        print(f"  [PROXY] _global_scores['USD'] = {proxy['score']:+.4f} → USD trend: {proxy['trend']}")
    print("  === END DXY ===\n")


def _print_mc_snapshot() -> None:
    """Print Markov-Chain regime snapshot for trade pairs (reference only, no trading decision)."""
    trade_pairs_all = []
    for gcfg in _strategy_groups.values():
        quote = gcfg["quote_ccy"]
        pairs = [p for p in getattr(_config_bot, "STRENGTH_PAIRS", []) if p.endswith(f"_{quote}")]
        trade_pairs_all.extend(pairs)
    trade_pairs_all = list(dict.fromkeys(trade_pairs_all))

    if not trade_pairs_all:
        return

    print("  === MC DAILY REGIME SNAPSHOT ===")
    for pair in trade_pairs_all:
        item = _get_mc_item(pair)
        if item is None:
            print(f"  {pair:10s} | [No MC data]")
            continue
        raw_regime = item.get("regime", "N/A")
        regime = _clean_mc_regime(raw_regime)
        p_up_val = item.get("p_up", item.get("P(UP)"))
        p_down_val = item.get("p_down", item.get("P(DOWN)"))
        price = item.get("current_price", item.get("expected_price", "?"))
        _icon = {"STRONG_MOMENTUM": "⚡", "CONSOLIDATION": "🔹", "NEUTRAL": "🔸", "N/A": "❓"}.get(regime, "•")
        bias = ""
        if p_up_val is not None and p_down_val is not None:
            try:
                _pu = float(p_up_val)
                _pd = float(p_down_val)
                if _pu > _pd + 1:
                    bias = f"| ▲ UP-bias P(UP)={_pu:.1f}%"
                elif _pd > _pu + 1:
                    bias = f"| ▼ DOWN-bias P(DOWN)={_pd:.1f}%"
                else:
                    bias = f"| NEUTRAL P(UP)={_pu:.1f}% P(DOWN)={_pd:.1f}%"
            except (ValueError, TypeError):
                bias = f"| P(UP)={p_up_val}% P(DOWN)={p_down_val}%"
        print(f"  {pair:10s} | {_icon} {regime:15s} {bias} | last={price}")
    print("  === END MC SNAPSHOT ===\n")


def _clean_mc_regime(raw) -> str:
    if not raw:
        return "NEUTRAL"
    s = str(raw).upper()
    for key in ("STRONG_MOMENTUM", "CONSOLIDATION", "NEUTRAL"):
        if key in s:
            return key
    if "MOMENTUM" in s:
        return "STRONG_MOMENTUM"
    return "NEUTRAL"


def _regime_policy(mc_regime: str) -> str:
    reg = _clean_mc_regime(mc_regime)
    if reg == "CONSOLIDATION":
        return "cautious"
    if reg == "STRONG_MOMENTUM":
        return "aggressive"
    return "normal"


def _get_group_mc_regime(group_pairs: list[str]) -> str:
    """Majority-vote MC regime for a group of pairs."""
    regimes = []
    for pair in group_pairs:
        item = _get_mc_item(pair)
        if item is None:
            continue
        regimes.append(_clean_mc_regime(item.get("regime", "NEUTRAL")))

    if not regimes:
        return "NO_MC_DATA"

    from collections import Counter
    counts = Counter(regimes)
    top_regime, _ = counts.most_common(1)[0]
    return top_regime


def _get_global_mc_regime(all_trade_pairs: list[str]) -> str:
    """Majority-vote MC regime across ALL trade pairs (global risk stance)."""
    return _get_group_mc_regime(all_trade_pairs)


MC_CONSOLIDATION_STRENGTH_HURDLE = 0.30
MC_CONSOLIDATION_DISABLE_OVERRIDE = True
MC_CONSOLIDATION_SL_WIDEN_FACTOR = 1.2
MC_AGGRESSIVE_SL_NARROW_FACTOR = 0.9

MC_CONSOLIDATION_MAX_POSITIONS = getattr(_config_bot, "MC_MAX_POSITIONS_CONSOLIDATION", 1)
MC_NEUTRAL_MAX_POSITIONS = getattr(_config_bot, "MC_MAX_POSITIONS_NEUTRAL", 2)
MC_STRONG_MOMENTUM_MAX_POSITIONS = getattr(_config_bot, "MC_MAX_POSITIONS_AGGRESSIVE", 3)

ENABLE_MC_CONFLICT_CHECK = getattr(_config_bot, "ENABLE_MC_CONFLICT_CHECK", True)
ENABLE_MC_CONFLICT_BLOCK = getattr(_config_bot, "ENABLE_MC_CONFLICT_BLOCK", False)
ENABLE_MC_CONFLICT_BLOCK_MODERATE = getattr(_config_bot, "ENABLE_MC_CONFLICT_BLOCK_MODERATE", False)
MC_CONFLICT_PROB_THRESHOLD = getattr(_config_bot, "MC_CONFLICT_PROB_THRESHOLD", 0.52)
MC_CONFLICT_SEVERE_THRESHOLD = getattr(_config_bot, "MC_CONFLICT_SEVERE_THRESHOLD", 0.58)

_MC_CACHE: dict = {}


def _get_mc_item(pair: str) -> dict | None:
    global _MC_CACHE
    if pair in _MC_CACHE:
        return _MC_CACHE[pair]
    try:
        from get_mc_data import get_mc_data
    except ImportError:
        return None
    try:
        mc = get_mc_data(timeframe="D", date_val="latest", pair=pair)
        if mc and isinstance(mc, list) and mc:
            item = mc[0]
        elif isinstance(mc, dict) and mc.get("pairs"):
            item = mc["pairs"][0]
        else:
            item = None
        if item is not None:
            _MC_CACHE[pair] = item
        return item
    except Exception:
        return None


def _load_mc_cache(trade_pairs: list[str]) -> None:
    global _MC_CACHE
    _MC_CACHE.clear()
    for pair in trade_pairs:
        _get_mc_item(pair)
    if _MC_CACHE:
        print(f"  [MC] Cached {len(_MC_CACHE)}/{len(trade_pairs)} trade pairs")


def _fetch_mc_direction_prob(pair: str) -> dict | None:
    item = _get_mc_item(pair)
    if item is None:
        return None
    p_up = item.get("p_up", item.get("P(UP)"))
    p_down = item.get("p_down", item.get("P(DOWN)"))
    if p_up is None or p_down is None:
        return None
    try:
        return {"p_up": float(p_up) / 100.0 if float(p_up) > 1.5 else float(p_up),
                "p_down": float(p_down) / 100.0 if float(p_down) > 1.5 else float(p_down)}
    except (ValueError, TypeError):
        return None


def _check_mc_direction_conflict(signals: list[dict]) -> list[dict]:
    if not ENABLE_MC_CONFLICT_CHECK:
        return signals
    if not signals:
        return signals
    remaining = []
    for s in signals:
        pair = s.get("pair")
        action = s.get("action", "").upper()
        mc_probs = _fetch_mc_direction_prob(pair)
        if not mc_probs:
            s["mc_conflict"] = None
            remaining.append(s)
            continue
        p_up = mc_probs["p_up"]
        p_down = mc_probs["p_down"]
        if action == "BUY":
            signal_prob = p_up
            reverse_prob = p_down
            reverse_dir = "DOWN"
        elif action == "SELL":
            signal_prob = p_down
            reverse_prob = p_up
            reverse_dir = "UP"
        else:
            s["mc_conflict"] = None
            remaining.append(s)
            continue
        s["mc_signal_prob"] = signal_prob
        s["mc_reverse_prob"] = reverse_prob
        if reverse_prob >= MC_CONFLICT_SEVERE_THRESHOLD:
            s["mc_conflict"] = "SEVERE"
            print(f"  [MC CONFLICT] {action} {pair} | Signal-P={signal_prob*100:.1f}% vs "
                  f"MC-P={reverse_prob*100:.1f}%({reverse_dir}) [SEVERE]")
            if ENABLE_MC_CONFLICT_BLOCK:
                print(f"    🚫 BLOCKED: MC CONFLICT {pair} (SEVERE, BLOCK=True)")
                continue
        elif reverse_prob >= MC_CONFLICT_PROB_THRESHOLD:
            s["mc_conflict"] = "MODERATE"
            print(f"  [MC CONFLICT] {action} {pair} | Signal-P={signal_prob*100:.1f}% vs "
                  f"MC-P={reverse_prob*100:.1f}%({reverse_dir}) [MODERATE]")
            if ENABLE_MC_CONFLICT_BLOCK and ENABLE_MC_CONFLICT_BLOCK_MODERATE:
                print(f"    🚫 BLOCKED: MC CONFLICT {pair} (MODERATE, BLOCK + MODERATE_BLOCK=True)")
                continue
        else:
            s["mc_conflict"] = None
        remaining.append(s)
    return remaining


def _apply_mc_gate(signals: list[dict], mc_regime: str, quote_ccy: str) -> list[dict]:
    """Filter/adjust signals based on MC regime."""
    signals = _check_mc_direction_conflict(signals)
    if mc_regime in ("NO_MC_DATA", "NEUTRAL", None):
        return signals

    mode = _regime_policy(mc_regime)

    if mode == "aggressive":
        print(f"  [MC GATE] {quote_ccy} → STRONG_MOMENTUM → all signals pass, SL ×{MC_AGGRESSIVE_SL_NARROW_FACTOR}")
        _widen_sl(signals, MC_AGGRESSIVE_SL_NARROW_FACTOR)
        return signals

    if mode == "cautious":
        print(f"  [MC GATE] {quote_ccy} → CONSOLIDATION → strength hurdle ≥ {MC_CONSOLIDATION_STRENGTH_HURDLE}, "
              f"override={'DISABLED' if MC_CONSOLIDATION_DISABLE_OVERRIDE else 'allowed'}, "
              f"SL ×{MC_CONSOLIDATION_SL_WIDEN_FACTOR}")
        filtered = []
        for s in signals:
            if MC_CONSOLIDATION_DISABLE_OVERRIDE and s.get("is_override"):
                print(f"    🚫 DROP OVERRIDE {s['action']} {s['pair']} (consolidation)")
                continue
            if abs(s.get("strength_score", 0)) < MC_CONSOLIDATION_STRENGTH_HURDLE:
                print(f"    🚫 DROP {s['action']} {s['pair']} strength={abs(s.get('strength_score',0)):.3f} < "
                      f"{MC_CONSOLIDATION_STRENGTH_HURDLE} (consolidation hurdle)")
                continue
            filtered.append(s)
        _widen_sl(filtered, MC_CONSOLIDATION_SL_WIDEN_FACTOR)
        return filtered

    return signals


def _widen_sl(signals: list[dict], factor: float) -> None:
    """Narrow/widen SL in-place."""
    for s in signals:
        if "stop_loss" in s and "entry" in s:
            entry = s["entry"]
            sl = s["stop_loss"]
            pip_dist = abs(entry - sl)
            new_pip = pip_dist * factor
            direction = s["action"]
            if direction == "BUY":
                s["stop_loss"] = round(entry - new_pip, 5)
            else:
                s["stop_loss"] = round(entry + new_pip, 5)
            if "take_profit" in s and entry != sl:
                rr_old = abs(entry - s["take_profit"]) / pip_dist if pip_dist else 0
                new_tp_dist = new_pip * rr_old
                s["take_profit"] = round(entry + new_tp_dist if direction == "BUY" else entry - new_tp_dist, 5)


# ==========================================
# Load strategy and run per group
# ==========================================
import custom_strategy_v3 as _strategy
from custom_strategy_v3 import BaseCurrencyTrendStrategy
from utils.strategy_helpers import (
    build_strength_matrix, format_strength_ranking, check_ma5_alignment, check_ma5_cross,
    check_macd_histogram,
)
from utils.oanda_state import build_client_extensions
from utils.utils import (
    acquire_profile_lock, check_pair_level_strategy_position,
    is_strategy_trade, make_strategy_tag, make_strategy_comment,
    is_bot_owned_trade,
)

_strategy_groups = _config_bot.STRATEGY_GROUPS
_pip_map = _config_bot.PIP_SIZE_BY_QUOTE
# Base net-cap — resolved once per process start via run.env/config.
# The actual effective cap is further adjusted in main() based on
# GLOBAL_MAX_GAP + MC regime so extreme-momentum runs are allowed to
# expand to 3 while consolidation periods are pinned to base (typically 2).
#
# Dola 2026-09-29 recommendation for run.env:
#   CROSS_MAX_NET_PER_CCY=2   # BASE; boost logic lifts to 3 / 4 under
#                              STRONG_MOMENTUM / STRONG_GAP synthetic mc.
#   With BASE=2 the boost-then-synthesis sequence becomes meaningful:
#     NEUTRAL + gap<1.8 → base=2
#     NEUTRAL + gap≥1.8 → 2→3 (boost) then +1 (STRONG_GAP) = 4
#     STRONG_MOMENTUM   → 2→3 (boost)
#   If you set BASE=3, all three branches collapse to 3/3/3 (i.e. the
#   "boosted" behaviour becomes the floor, which kills differentiation).
_CROSS_NET_CAP_BASE = _env_or_config("CROSS_MAX_NET_PER_CCY", 2, value_type=int)
_cross_net_cap = _CROSS_NET_CAP_BASE

# Part B — OVERRIDE priority channel.
# OVERRIDE signals (dominance-ratio extreme) get a dedicated entry slot
# independent of the general MC-driven position cap, but always remain
# subject to net-exposure / idempotency / SL-TP risk controls.
# The per-cycle ceiling avoids consecutive overrides overwhelming the
# basket allocation in a single 1h sweep.
OVERRIDE_MAX_PER_CYCLE = 1

_IS_LIVE = os.environ.get("OANDA_ENV", "practice").lower() in ("live", "real")
_MAX_OPEN_POSITIONS = getattr(_config_bot, "MC_MAX_POSITIONS_NEUTRAL", 2)

PROJECT_ROOT = Path(__file__).resolve().parent


# -------------------------------------------
# Helper: run ONE strategy group
# -------------------------------------------
def _run_single_group(group_name: str, group_cfg: dict, global_scores: dict) -> dict:
    """
    Execute one strategy group. Returns result dict:
        {"signals": [...], "strategy": BaseCurrencyTrendStrategy, "cfg": group_cfg,
         "group_name": str, "mc_regime": str}
    """
    quote_ccy = group_cfg["quote_ccy"]
    tag_prefix = group_cfg["tag_prefix"]

    trade_pairs = [p for p in getattr(_config_bot, "STRENGTH_PAIRS", []) if p.endswith(f"_{quote_ccy}")]
    mc_regime = _get_group_mc_regime(trade_pairs)
    mode = _regime_policy(mc_regime)

    print(f"\n{'─' * 70}")
    print(f"[GROUP {group_name}] quote_ccy={quote_ccy} | tag_prefix={tag_prefix} | MC={mc_regime} (mode={mode})")
    print(f"{'─' * 70}")

    strategy = BaseCurrencyTrendStrategy(
        quote_ccy=quote_ccy,
        enable_atr_min_filter=_PROFILE_CFG.get("ENABLE_ATR_MINIMUM_FILTER", True),
        atr_min_pips=_PROFILE_CFG.get("ATR_MIN_PIPS", 6.0),
        atr_min_relative_pct=_PROFILE_CFG.get("ATR_MIN_RELATIVE_PCT", 0.045),
        min_strength_passing_pairs=group_cfg.get("MIN_STRENGTH_PASSING_PAIRS"),
        min_dominant_pairs=group_cfg.get("MIN_DOMINANT_PAIRS"),
        min_valid_pairs_to_trade=group_cfg.get("MIN_VALID_PAIRS_TO_TRADE"),
        mc_regime=mc_regime,  # Part C: strategy uses this for CHF threshold relaxation
    )

    signals = strategy.generate_signals(global_scores)

    for s in signals:
        s["is_override"] = bool(s.get("override_source"))

    signals = _apply_mc_gate(signals, mc_regime, quote_ccy)

    return {"signals": signals, "strategy": strategy, "cfg": group_cfg,
            "group_name": group_name, "mc_regime": mc_regime}


# -------------------------------------------
# Build net exposure from open strategy trades
# -------------------------------------------
def _build_open_exposure() -> tuple[dict, set]:
    """
    Scan ONLY bot-owned open trades → build net exposure + held set.

    HARD-INVARIANT: Manual trades / other-bot trades NEVER enter this view.
    A manual BUY on EUR_USD must NOT count as bot exposure, must NOT block
    a bot-generated signal, and must NOT change the idempotency `held` set.
    """
    net = {}
    held = set()
    all_trades = _trading_core.get_all_open_trades()

    _prefixes = {cfg["tag_prefix"] for cfg in _strategy_groups.values()}

    for t in all_trades:
        if not is_bot_owned_trade(t):
            continue
        tag = t.get("clientExtensions", {}).get("tag", "") or ""
        raw_tag = tag.split("::")[-1] if "::" in tag else tag
        if not any(p in raw_tag for p in _prefixes):
            continue
        inst = t["instrument"]
        base, quote = inst.split("_")
        units = float(t.get("currentUnits", 0))
        s = 1 if units > 0 else -1
        net[base] = net.get(base, 0) + s
        net[quote] = net.get(quote, 0) - s
        held.add((inst, s))

    return net, held


# -------------------------------------------
# Global ranking → pick TOP signal across all groups
# -------------------------------------------
def _pick_global_basket(
    all_results: list[dict],
    max_entries: int = 1,
    open_net: dict | None = None,
    held: set | None = None,
) -> list[dict]:
    """
    Rank all signals across ALL groups by abs(strength_score) × MC weights,
    then greedily pick top `max_entries` while respecting:
      1. Already-held positions (skip duplicates, flag reversals for idempotency path)
      2. Cross-currency net exposure cap (per-side cap from open_net + pending picks)
      3. BASE-currency thesis contradiction against existing exposure

    The highest-ranked valid signal always wins — weak signals never veto strong ones.

    Exposure bookkeeping tracks base AND quote ccy (net[] accumulates both) and the
    NET CAP (#2) is enforced on both sides — so EUR_USD BUY + GBP_USD BUY = double
    short USD is still capped. The thesis check (#3) is enforced on the BASE ccy only:
    the quote-side sign is structural (every BUY shorts its quote), not a thesis clash,
    so checking it would wrongly veto legitimate structures such as
    EUR_USD BUY + USD_JPY BUY.

    Returns list of entry dicts: {"signal": {...}, "group_name": str, ...}
    """
    mc_sev_w = getattr(_config_bot, "CROSS_MC_SEVERE_WEIGHT", 0.6)
    mc_mod_w = getattr(_config_bot, "CROSS_MC_MODERATE_WEIGHT", 0.8)
    cap = _cross_net_cap  # uses per-cycle dynamic value (BASE or BOOSTED) set in main()

    all_entries = []
    for gr in all_results:
        for sig in gr["signals"]:
            all_entries.append({
                "signal": sig,
                "group_name": gr["group_name"],
                "tag_prefix": gr["cfg"]["tag_prefix"],
                "group_cfg": gr["cfg"],
            })

    if not all_entries:
        return []

    def _ranking_score(e):
        base = abs(e["signal"]["strength_score"])
        mc = e["signal"].get("mc_conflict")
        if mc == "SEVERE":
            base *= mc_sev_w
        elif mc == "MODERATE":
            base *= mc_mod_w
        return base

    all_entries.sort(key=lambda e: (_ranking_score(e), e["signal"]["pair"]), reverse=True)

    net = dict(open_net or {})
    held_now = set(held or ())
    basket = []
    skipped_due_to_conflict = 0
    skipped_due_to_held = 0
    skipped_due_to_cap = 0
    # Part B2 — per-cycle OVERRIDE cap (basket level).  The execution layer
    # re-checks the same ceiling before submitting, but we enforce it here
    # too so the lower-ranked OVERRIDE entries never displace NORMAL picks.
    _accepted_override_count = 0

    print(f"\n{'─' * 70}")
    print(f"[GLOBAL] Picking basket (max={max_entries}, net-cap={cap}/ccy):")
    if net:
        _net_str = ", ".join(f"{c}:{v:+d}" for c, v in sorted(net.items()))
        print(f"  [NET from open] {_net_str}")

    for i, entry in enumerate(all_entries):
        sig = entry["signal"]
        pair = sig["pair"]
        action = sig["action"]
        base, quote = pair.split("_")
        s = 1 if action == "BUY" else -1
        deltas = {base: s, quote: -s}
        base_delta = {base: s}

        _is_override = bool(sig.get("override_source"))
        _tag = "⚡OVERRIDE" if _is_override else "  NORMAL  "
        _mc_tag = f" ⚠️MC:{sig['mc_conflict']}" if sig.get("mc_conflict") else ""
        _rank = _ranking_score(entry)

        # Part B2 rule #1: never accept more OVERRIDE signals per cycle
        # than the dedicated-channel ceiling.  NORMAL picks keep flowing.
        if _is_override and _accepted_override_count >= OVERRIDE_MAX_PER_CYCLE:
            print(
                f"  {i+1}. {pair} {action} score={sig['strength_score']:+.4f} → "
                f"[OVERRIDE FULL {_accepted_override_count}/{OVERRIDE_MAX_PER_CYCLE}]"
                f"{_tag}{_mc_tag}"
            )
            continue

        rev = (pair, -s) in held_now
        dup = (pair, s) in held_now
        if dup and not rev:
            print(f"  {i+1}. {pair} {action} score={sig['strength_score']:+.4f} → [SKIP HELD]{_tag}{_mc_tag}")
            skipped_due_to_held += 1
            continue
        if rev:
            print(f"  {i+1}. {pair} {action} score={sig['strength_score']:+.4f} → [REVERSE PENDING]{_tag}{_mc_tag}")

        oppose = [c for c, d in base_delta.items() if net.get(c, 0) * d < 0]
        if oppose and not rev:
            _detail = ", ".join(f"{c}:{net.get(c,0):+d}→{d:+d}" for c, d in base_delta.items())
            print(f"  {i+1}. {pair} {action} score={sig['strength_score']:+.4f} → [OPPOSES {oppose}] | {_detail} {_tag}{_mc_tag}")
            skipped_due_to_conflict += 1
            continue

        cap_hit = [c for c, d in deltas.items() if abs(net.get(c, 0) + d) > cap]
        if cap_hit:
            _detail = ", ".join(f"{c}:{net.get(c,0):+d}+{d:+d}→{net.get(c,0)+d:+d}" for c, d in deltas.items())
            print(f"  {i+1}. {pair} {action} score={sig['strength_score']:+.4f} → [NET CAP {cap_hit}] {_tag}{_mc_tag}")
            skipped_due_to_cap += 1
            continue

        basket.append(entry)
        if _is_override:
            _accepted_override_count += 1
        for c, d in deltas.items():
            net[c] = net.get(c, 0) + d
        held_now.add((pair, s))
        print(f"  {i+1}. ✅ {pair} {action} score={sig['strength_score']:+.4f} {_tag}{_mc_tag}")

        if len(basket) >= max_entries:
            break

    _summary = []
    if skipped_due_to_held:
        _summary.append(f"held={skipped_due_to_held}")
    if skipped_due_to_conflict:
        _summary.append(f"oppose={skipped_due_to_conflict}")
    if skipped_due_to_cap:
        _summary.append(f"cap={skipped_due_to_cap}")
    if _accepted_override_count:
        _summary.append(f"override={_accepted_override_count}/{OVERRIDE_MAX_PER_CYCLE}")
    if _summary:
        print(f"  [SKIP breakdown] {', '.join(_summary)}")
    print(f"{'─' * 70}")

    return basket


# -------------------------------------------
# Execute ONE top signal
# -------------------------------------------
def _execute_single_signal(
    top_entry: dict,
    dry_run: bool,
    *,
    cycle_override_issued_before: int = 0,
) -> tuple[bool, str | None]:
    """Execute a basket signal.

    Returns
    -------
    (submitted_ok, blocked_reason)
      - submitted_ok    : True iff execute_market_trade returned True / dry-run
                          would have been submitted (signal actually issued).
      - blocked_reason  : None if submitted_ok, else one of
                          {"OVERRIDE_CHAN_FULL", "FETCH_ERR",
                           "MAX_POSITIONS", "ALREADY_HELD"}
    (Override issuance counter semantics remain unchanged: the caller adds +1
    only when submitted_ok AND the signal was an OVERRIDE.)
    """
    sig = top_entry["signal"]
    pair = sig["pair"]
    action = sig["action"]
    tag_prefix = top_entry["tag_prefix"]
    group_name = top_entry["group_name"]
    is_override = bool(sig.get("override_source"))
    override_type = sig.get("override_type") if is_override else None
    override_ratio = float(sig.get("override_ratio") or 0.0) if is_override else 0.0

    print(
        f"\n{'─' * 70}\n"
        f"[EXECUTE {group_name}] tag_prefix={tag_prefix} | {action} {pair}"
        + (
            f" | OVERRIDE[{override_type}] ratio={override_ratio:.2f}"
            if is_override and override_type
            else ""
        )
        + f"\n{'─' * 70}"
    )

    print(
        f"  ✅ SIGNAL [{group_name}]: {action} {pair}\n"
        f"     Entry: {sig['entry']} | SL: {sig['stop_loss']} | TP: {sig['take_profit']} | "
        f"R:R={sig['risk_reward']:.2f}"
    )
    if is_override:
        print(f"     ⚡ Source: OVERRIDE ({sig['override_source']}, type={override_type})")

    if sig.get("mc_conflict"):
        _lvl = sig["mc_conflict"]
        _sp = sig.get("mc_signal_prob", 0) * 100
        _rp = sig.get("mc_reverse_prob", 0) * 100
        print(f"     ⚠️ MC DIRECTION CONFLICT [{_lvl}]: Signal-P={_sp:.1f}% vs Reverse-P={_rp:.1f}%")

    if dry_run:
        print(f"  [{group_name}] DRY-RUN → skipping order submission")
        return True, None

    # Part B2 rule #1 — secondary OVERRIDE issuance ceiling at the
    # execution layer.  The basket layer already capped it, but a
    # previous basket signal might have just been filled in the same
    # cycle, so we re-check to avoid any 2-OVERRIDE race.
    if is_override and cycle_override_issued_before >= OVERRIDE_MAX_PER_CYCLE:
        print(
            f"  🚫 [{group_name}] OVERRIDE CHANNEL FULL — "
            f"cycle_count={cycle_override_issued_before}/{OVERRIDE_MAX_PER_CYCLE} → "
            f"HOLDING, no new OVERRIDE entries"
        )
        return False, "OVERRIDE_CHAN_FULL"

    try:
        _existing = _trading_core.get_all_open_trades()
        _prefixes = {cfg["tag_prefix"] for cfg in _strategy_groups.values()}
        _strategy_open = 0
        _bot_has_this_pair = False
        for t in _existing:
            if not is_bot_owned_trade(t):
                continue
            tag = t.get("clientExtensions", {}).get("tag", "") or ""
            raw_tag = tag.split("::")[-1] if "::" in tag else tag
            if any(p in raw_tag for p in _prefixes):
                _strategy_open += 1
                if t.get("instrument") == pair:
                    _bot_has_this_pair = True
    except Exception as exc:
        print(f"  ❌ [{group_name}] Cannot fetch open trades ({exc}) → fail-closed, skip entry")
        return False, "FETCH_ERR"

    # Part B2 rule #2 — OVERRIDE channel bypasses the general MC-driven
    # position ceiling.  Every other risk guard (this-pair held, net
    # exposure, SL/TP/ATR) remains enforced below and downstream.
    _over_cap = _strategy_open >= _MAX_OPEN_POSITIONS
    if _over_cap:
        if is_override:
            _channel_n = cycle_override_issued_before + 1
            print(
                f"  ⚡ OVERRIDE CHANNEL — bypass general position cap "
                f"(general={_strategy_open}/{_MAX_OPEN_POSITIONS}) "
                f"| cycle_count={_channel_n}/{OVERRIDE_MAX_PER_CYCLE}"
            )
        else:
            print(
                f"  🚫 [{group_name}] Position limit reached: "
                f"{_strategy_open}/{_MAX_OPEN_POSITIONS} bot-owned open → HOLDING, no new entries"
            )
            return False, "MAX_POSITIONS"

    if _bot_has_this_pair:
        print(
            f"  🚫 [{group_name}] Already holds bot-owned position on {pair} → "
            f"skipping (idempotency / manual trades on same pair ignored by design)"
        )
        return False, "ALREADY_HELD"

    print(
        f"  [{group_name}] Open bot-owned positions: {_strategy_open}/{_MAX_OPEN_POSITIONS}"
        + ("" if not _over_cap or not is_override else " (via OVERRIDE CHANNEL)")
    )

    strategy_tag = make_strategy_tag(pair, action, tag_prefix)
    if is_override:
        strategy_tag += "_OVERRIDE"
    if sig.get("mc_conflict") == "SEVERE":
        strategy_tag += "_MCSEV"
    elif sig.get("mc_conflict") == "MODERATE":
        strategy_tag += "_MCMOD"

    # ---- Stash open-time strength score + rank into comment so the
    # 5-gate early-exit engine can later detect "strength reversal" against
    # an apples-to-apples baseline.  (Score is already computed here at ENTRY
    # TIME; at close-time strength will be re-measured vs the entry baseline.)
    _open_strength_score: float | None = sig.get("strength_score")
    _open_strength_rank:  int   | None = None
    try:
        _quote_ccy: str | None = None
        _group_name_find: str | None = None
        for _gn, _g in _strategy_groups.items():
            _inst = _g.get("instruments") or {}
            if isinstance(_inst, dict) and pair in _inst:
                _quote_ccy = _g.get("quote_ccy")
                _group_name_find = _gn
                break
        if _quote_ccy is None and group_name and group_name in _strategy_groups:
            _g0 = _strategy_groups[group_name]
            _quote_ccy = _g0.get("quote_ccy")
            _group_name_find = group_name

        if _quote_ccy and _open_strength_score is None:
            try:
                _m, _c, _ = build_strength_matrix([pair], verbose=False)
                _b = pair.replace("_" + _quote_ccy, "")
                if _b in _c and _quote_ccy in _c:
                    _open_strength_score = float(_m[_c.index(_b), _c.index(_quote_ccy)])
            except Exception:
                pass

        # Compute open rank against the peers of the same strategy group at open-time
        if _group_name_find is not None and _quote_ccy:
            try:
                _grp_cfg = _strategy_groups[_group_name_find]
                _grp_pairs = list((_grp_cfg.get("instruments") or {}).keys())
                _m2, _c2, _ = build_strength_matrix(_grp_pairs, verbose=False)

                def _sc2(p):
                    b = p.replace("_" + _quote_ccy, "")
                    if b not in _c2 or _quote_ccy not in _c2:
                        return None
                    return float(_m2[_c2.index(b), _c2.index(_quote_ccy)])
                _ranked = sorted(
                    [(p, _sc2(p)) for p in _grp_pairs if _sc2(p) is not None],
                    key=lambda x: x[1],
                )
                for _idx, (_p, _s) in enumerate(_ranked):
                    if _p == pair:
                        _open_strength_rank = _idx
                        break
            except Exception:
                _open_strength_rank = None
    except Exception:
        _open_strength_rank = None

    extra_meta = {}
    if is_override:
        extra_meta["override"] = sig["override_source"]
        if override_type:
            extra_meta["ovr_t"] = override_type
        if override_ratio:
            extra_meta["ovr_r"] = f"{override_ratio:.2f}"
    if group_name:
        extra_meta["grp"] = str(group_name)
    if tag_prefix:
        extra_meta["pfx"] = str(tag_prefix)
    strategy_comment = make_strategy_comment(
        sig["entry"], sig["stop_loss"], sig["take_profit"], RUNNER_VERSION,
        strength_score=_open_strength_score,
        strength_rank=_open_strength_rank,
        extra=extra_meta or None,
    )
    if sig.get("mc_conflict"):
        _mc_p = sig.get("mc_reverse_prob", 0) * 100
        strategy_comment += f" | MC_CONFLICT:{sig['mc_conflict']}(reverse={_mc_p:.1f}%)"
    sig["tag"], sig["comment"] = strategy_tag, strategy_comment

    if _trading_core.execute_market_trade(
        instrument=pair,
        action=action,
        units=_EFFECTIVE_LOTS,
        stop_loss=sig["stop_loss"],
        take_profit=sig["take_profit"],
        dry_run=False,
        client_extensions=build_client_extensions(
            sig, strategy_tag=strategy_tag, bar_time=sig.get("bar_time")
        ),
    ):
        print(f"  ✅ [{group_name}] Order submitted: {action} {pair}")
        return True, None
    else:
        print(f"  ❌ [{group_name}] Order failed: {action} {pair}")
        return False, "BROKER_REJECT"


# -------------------------------------------
# SL/TP Maintenance (per group)
# -------------------------------------------
def _maintain_group_positions(group_name: str, group_cfg: dict, dry_run: bool, global_scores: dict | None = None) -> None:
    tag_prefix = group_cfg["tag_prefix"]
    quote_ccy = group_cfg["quote_ccy"]

    _h1_cache: Dict[str, List[Dict[str, Any]]] = {}
    _daily_cache: Dict[str, List[Dict[str, Any]]] = {}

    print(f"\n  [MAINTAIN {group_name}] Scanning open trades tagged {tag_prefix}* (bot-owned only)")
    try:
        open_trades = _trading_core.get_all_open_trades()
    except Exception as exc:
        print(f"  [MAINTAIN {group_name}] Fetch failed: {exc}")
        return

    # -----------------------------------------------------------------
    # HARD-INVARIANT boundary:
    #   ALL open_trades (DISCOVERY)
    #     → bot_owned_trades (AUTHORIZATION)
    #       → strategy_group filter (LOGIC)
    #
    # Any trade that does not pass `is_bot_owned_trade` is dropped
    # HERE — before risk processing, before override/early-exit
    # decisions, before SL updates.
    # -----------------------------------------------------------------
    bot_trades: List[Dict[str, Any]] = []
    skipped_manual = 0
    for t in open_trades:
        if is_bot_owned_trade(t):
            bot_trades.append(t)
        else:
            skipped_manual += 1
    if skipped_manual:
        print(
            f"  [MAINTAIN {group_name}] Ownership filter: "
            f"{len(bot_trades)} bot-owned kept / {skipped_manual} manual/other dropped"
        )

    # -----------------------------------------------------------------
    # Prune the risk-engine's pending-exit map against the set of trade
    # IDs currently open on the broker.  This removes stale entries for
    # positions closed by broker SL/TP, manual intervention or
    # unresolved partial fills (Bug #5 / Claude v22→v23 patch).
    # ALWAYS print an audit line so cycle logs prove the step ran.
    # -----------------------------------------------------------------
    _all_open_ids: List[str] = []
    for t in bot_trades:
        _tid = str(t.get("id", "") or t.get("tradeID", ""))
        if _tid:
            _all_open_ids.append(_tid)
    _prune = _risk_runner.prune_pending_exits(_all_open_ids)
    print(
        f"  [INFO] prune pending_exits: {_prune['removed']} removed | "
        f"open={_prune['open_count']} | pending={_prune['pending_count']}"
    )

    for trade in bot_trades:
        if not is_strategy_trade(trade, tag_prefix):
            continue
        instrument = trade.get("instrument", "")
        current_units = float(trade.get("currentUnits", 0))
        side = "BUY" if current_units > 0 else "SELL"
        tags = trade.get("clientExtensions", {}).get("tag", "")

        trade_id = str(trade.get("id", "") or trade.get("tradeID", ""))
        risk_side = "LONG" if current_units > 0 else "SHORT"
        _sl_raw = trade.get("stopLossOrder", {}).get("price")
        current_sl_val = float(_sl_raw) if _sl_raw else None

        # --- Gate-0 (min-hold) for RISK processing: ----------------------
        # Claude review: if a trade was opened seconds ago on a higher-TF
        # signal, the H1 momentum ratio may already be below -deadzone and
        # a noise-triggered false exit could fire before the trade has
        # any evidence.  To prevent this, we require the trade to be at
        # least MIN_HOLD_RISK_MIN minutes old before invoking process_h1_bar
        # / process_daily_bar.  Early-exit Gate-0 below enforces its own
        # (stricter) 180min / 7d thresholds independently.
        _ot_raw = None
        if hasattr(trade, "openTime"):
            _ot_raw = trade.openTime
        if _ot_raw is None and isinstance(trade, dict):
            _ot_raw = trade.get("openTime")
        if _ot_raw is None:
            try:
                raw_dict = getattr(trade, "dict", lambda: {})()
                if isinstance(raw_dict, dict):
                    _ot_raw = raw_dict.get("openTime")
            except Exception:
                _ot_raw = None
        _open_dt = _parse_oanda_openTime(_ot_raw)
        _age_min = 0.0
        _gate0_risk_hold_min = float(group_cfg.get("MIN_HOLD_RISK_PROCESS_MIN", 45))
        if _open_dt is not None:
            _age_min = max(0.0, (datetime.now(timezone.utc) - _open_dt).total_seconds() / 60.0)
        _risk_gate0_ok = _open_dt is not None and _age_min >= _gate0_risk_hold_min

        _gate0_need = max(0.0, _gate0_risk_hold_min)
        if not trade_id:
            print(f"  [RISK-H1 {group_name}] {instrument} {risk_side}: SKIP — no trade_id on record")
        elif not _risk_gate0_ok:
            _age_str = (
                f"{_age_min:.0f}min" if _open_dt is not None else "openTime-missing"
            )
            print(
                f"  [RISK GATE0 SKIP] age={_age_str} < {_gate0_need:.0f}min "
                f"— HOLD, no risk processing for T{trade_id} {instrument} {risk_side}"
            )
            print(
                f"  [RISK-H1 {group_name}] T{trade_id} {instrument} {risk_side}: "
                f"SKIP Gate-0 risk hold — age={_age_str} (need ≥{_gate0_need:.0f}min)"
            )
            print(
                f"  [RISK-D {group_name}] T{trade_id} {instrument} {risk_side}: "
                f"SKIP Gate-0 risk hold — daily SL untouched"
            )
        else:
            print(
                f"  [RISK GATE0 OK] age={_age_min:.0f}min ≥ MIN_HOLD {_gate0_need:.0f}min "
                f"— PASSED (T{trade_id} {instrument})"
            )
            h1_candles = _h1_cache.setdefault(
                instrument, _fetch_h1_candles_risk(instrument, count=40)
            )
            report = _risk_runner.process_h1_bar(
                trade_id=trade_id,
                symbol=instrument,
                side=risk_side,
                raw_h1_candles=h1_candles,
                is_in_event_window=False,
            )
            print(
                f"  [RISK-H1 {group_name}] T{trade_id} {instrument} {risk_side}: "
                f"{report.status.name} | {report.message}"
            )
            if report.status == ReconcileStatus.STATE_UNKNOWN:
                print(
                    f"    ⚠️  STATE UNKNOWN — trigger alert, manual check recommended "
                    f"(remaining_units={report.remaining_units})"
                )
            if report.status in (ReconcileStatus.SUCCESS_CLOSED, ReconcileStatus.ALREADY_CLOSED):
                continue

            if dry_run:
                print(
                    f"  [RISK-D {group_name}] T{trade_id} {instrument} {risk_side}: "
                    f"DRY-RUN — skip daily SL update"
                )
            else:
                daily_candles = _daily_cache.setdefault(
                    instrument, _fetch_daily_candles_risk(instrument, count=120)
                )
                try:
                    bid, ask = _trading_core.get_bid_ask(instrument)
                    if bid is None or ask is None:
                        raise ValueError("bid/ask unavailable")
                except Exception as _sl_exc:
                    print(
                        f"  [RISK-D {group_name}] T{trade_id} {instrument}: "
                        f"skip SL update — {_sl_exc}"
                    )
                else:
                    try:
                        sl_result = _risk_runner.process_daily_bar(
                            trade_id=trade_id,
                            symbol=instrument,
                            side=risk_side,
                            current_broker_sl=current_sl_val,
                            current_bid=bid,
                            current_ask=ask,
                            raw_daily_candles=daily_candles,
                        )
                        print(
                            f"  [RISK-D {group_name}] T{trade_id} {instrument} {risk_side}: "
                            f"SL={sl_result.name} | bid={bid} ask={ask}"
                        )
                    except ValueError as _ve:
                        print(
                            f"  [RISK-D {group_name}] T{trade_id} {instrument}: "
                            f"daily ATR not ready ({_ve}) — will retry next cycle"
                        )

        # ============================================================
        # EARLY-EXIT (runner-initiated close): 5 GATES MODEL
        #
        # PHILOSOPHY: NEVER exit on noise. Trust the broker SL/TP.
        # A runner close fires ONLY when ALL of the following open:
        #   Gate 0. Trade is old enough (MIN_HOLD_MIN elapsed)
        #   Gate 1. STRENGTH reversed (score sign flipped + abs≥threshold,
        #             OR rank dropped ≥N places vs peers in same group)
        #   Gate 2. MA aligned AGAINST the current side on ≥K TFs
        #           (and H4 MUST confirm, if enabled)
        #   Gate 3. MACD histogram opposite on ≥N TFs
        #   Gate 4. P/L rule: either profit, OR loss-exit explicitly
        #           allowed via EARLY_EXIT_ALLOW_AT_LOSS
        #
        # OVERRIDE TRADES: by default gate-0 threshold = 10080 min (7 days)
        #   AND runner close is disabled entirely. Overrides live/die
        #   only by broker SL/TP.
        # ============================================================
        is_override_trade = "OVERRIDE" in tags or (isinstance(tags, str) and "override" in tags.lower())

        ee_cfg = _EARLY_EXIT_CFG
        if is_override_trade and ee_cfg["OVERRIDE_DISABLE_RUNNER"]:
            print(
                f"  [MAINTAIN {group_name}] {instrument}: OVERRIDE trade "
                f"→ runner close disabled (broker SL/TP only); tag={tags}"
            )
            continue

        min_hold_min = (ee_cfg["OVERRIDE_MIN_HOLD_MIN"]
                        if is_override_trade else ee_cfg["MIN_HOLD_MIN"])
        ma_req         = (ee_cfg["MA_REQ_OVERRIDE"]
                        if is_override_trade else ee_cfg["MA_REQ_NORMAL"])
        tf_ma          = ee_cfg["TF_MA"]
        tf_macd        = ee_cfg["TF_MACD"]
        macd_agree_min = ee_cfg["MACD_AGREE_TF"]
        rev_abs_min    = ee_cfg["STRENGTH_REV_ABS"]
        rev_rank_drop  = ee_cfg["STRENGTH_REV_RANK_DROP"]
        allow_at_loss  = ee_cfg["ALLOW_AT_LOSS"]
        require_h4     = ee_cfg["REQUIRE_H4"]

        ee_label = "OVERRIDE-EXIT" if is_override_trade else "EARLY-EXIT"

        # ---- Gate 0: MIN-HOLD ----
        gate0_ok = False
        age_min_str = "n/a"
        _ot_raw = None
        if hasattr(trade, "openTime"):
            _ot_raw = trade.openTime
        if _ot_raw is None and isinstance(trade, dict):
            _ot_raw = trade.get("openTime")
        if _ot_raw is None:
            try:
                # Fallback: inspect dict() accessor (oandapyV20 objects expose this)
                raw = getattr(trade, "dict", lambda: {})()
                if isinstance(raw, dict):
                    _ot_raw = raw.get("openTime")
            except Exception:
                _ot_raw = None
        open_dt = _parse_oanda_openTime(_ot_raw)
        if open_dt is not None:
            age_min = max(0.0, (datetime.now(timezone.utc) - open_dt).total_seconds() / 60.0)
            age_min_str = f"{age_min:.0f}min"
            gate0_ok = age_min >= float(min_hold_min)
        else:
            # If we cannot determine age, fail-closed: keep trade open.
            gate0_ok = False
            print(f"  [{ee_label} {group_name}] {instrument}: gate0(MIN_HOLD) SKIP — cannot parse openTime")

        # ---- Gate 1: STRENGTH reversal ----
        # For Gate 1 we need the per-instrument strength score against the
        # group's quote-side peers.  Uses the precomputed `global_scores`
        # dict (currency → float) that is built ONCE per runner cycle, so
        # no redundant API calls and no signature drift risk.
        gate1_ok = False
        gate1_reason = None
        try:
            g_quote_ccy = group_cfg.get("quote_ccy", "USD")
            g_inst_dict = group_cfg.get("instruments") or {}
            group_pairs_peer = list(g_inst_dict.keys())
            # The strength comparison set must contain ALL peers of the group
            # so rank-drop is meaningful.  Fall back to the group's full
            # instrument list even if only one pair is open right now.
            compare_set = list(dict.fromkeys(group_pairs_peer + [instrument]))
            _scores = global_scores or {}
            inst_base = instrument.replace("_" + g_quote_ccy, "")
            def _score(pair: str) -> float | None:
                b = pair.replace("_" + g_quote_ccy, "")
                if b not in _scores or g_quote_ccy not in _scores:
                    return None
                return _scores[b] - _scores[g_quote_ccy]
            current_score = _score(instrument) if _scores else None

            # --- Part 4 Gate1 FALLBACK for single-pair groups --------------
            # Groups like CHF with only one instrument (USD_CHF) have an
            # empty compare_set.  Instead of closing Gate1 forever (which
            # leaves the position *only* protected by broker SL/TP), we
            # substitute "current strength percentile within self-history
            # of recent H1 score proxies" for the group rank.  When rank
            # flips from near-top to near-bottom (≥drop thresholds) we
            # still fire reversal.
            _used_fallback_rank = False
            _fallback_rank: int | None = None
            _fallback_window_size = 20  # ~20h of history, proxy for intraday
            _fallback_total_peers = 5  # synthetic 5-bucket ranking: 0..4
            if len(compare_set) <= 1 and current_score is not None:
                try:
                    _fallback_candles = _h1_cache.setdefault(
                        instrument, _fetch_h1_candles_risk(instrument, count=_fallback_window_size + 1)
                    )
                    if len(_fallback_candles) >= 3:
                        _closes = [
                            float(c.get("close", 0.0))
                            for c in _fallback_candles
                            if float(c.get("close", 0.0)) > 0
                        ]
                        if _closes and len(_closes) >= 3:
                            # Proxy per-bar "relative strength" = current close
                            # vs the previous bars.  We then compute the
                            # current bar's rank within a sliding synthetic
                            # peer list of size 5 — the higher the current
                            # close vs history the stronger the buy-side rank.
                            _latest = _closes[-1]
                            _hist = _closes[:-1] if len(_closes) > 1 else _closes
                            _rank_val = sum(1 for x in _hist if _latest > x)
                            _n = max(len(_hist), 1)
                            # Map [0..n-1] → 0..(N-1) quantile buckets
                            _q = int(_rank_val * _fallback_total_peers / _n) if _n > 0 else 0
                            _fallback_rank = max(0, min(_fallback_total_peers - 1, _q))
                            _used_fallback_rank = True
                            print(
                                f"    [GATE1 FALLBACK] {group_name} single-pair → "
                                f"use self-history rank (window={len(_closes)}h, "
                                f"open_rank bucket={open_rank}, current_rank bucket="
                                f"{_fallback_rank})"
                            )
                except Exception as _fb_exc:
                    print(
                        f"    [GATE1 FALLBACK WARN] {group_name} self-history err: "
                        f"{_fb_exc} — keeping group-score gate"
                    )
            # ---------------------------------------------------------------
            # Tag comment: try to read the *open-time* strength score from
            # clientExtensions.comment (make_strategy_comment format:
            # "s=.. d=.. g=.. v=.. e=.. sl=.. tp=.." includes optional sc=)
            open_score: float | None = None
            open_rank: int  | None = None
            try:
                from utils.utils import parse_strategy_comment as _psc
                _comment_str = None
                try:
                    ce = getattr(trade, "clientExtensions", None)
                    if ce is not None:
                        _comment_str = getattr(ce, "comment", None)
                except Exception:
                    _comment_str = None
                if isinstance(_comment_str, str) and _comment_str:
                    _parsed = _psc(_comment_str)
                    if isinstance(_parsed, dict):
                        for _k in ("entry_strength_score", "open_strength_score", "sc", "strength_score"):
                            if _k in _parsed and _parsed[_k] is not None:
                                try: open_score = float(_parsed[_k]); break
                                except Exception: pass
                        for _k in ("entry_strength_rank", "open_rank", "rk", "rank"):
                            if _k in _parsed and _parsed[_k] is not None:
                                try: open_rank = int(_parsed[_k]); break
                                except Exception: pass
            except Exception:
                open_score = None
                open_rank = None

            # Strength-reversal via score sign-flip + min abs
            if current_score is not None and open_score is not None and open_score != 0.0:
                sign_flip = (open_score > 0.0 and current_score < 0.0) or (open_score < 0.0 and current_score > 0.0)
                if sign_flip and abs(current_score) >= rev_abs_min:
                    gate1_ok = True
                    gate1_reason = (f"SCORE open={open_score:+.3f}→now={current_score:+.3f} "
                                    f"(flip+abs≥{rev_abs_min})")

            # Strength-reversal via rank drop
            _did_rank_compare = False
            if not gate1_ok and open_rank is not None and compare_set:
                # Compute current rank (sort by score ASC so #0 = weakest)
                scored = [(p, _score(p)) for p in compare_set]
                scored = [(p, s) for (p, s) in scored if s is not None]
                scored.sort(key=lambda x: x[1])
                current_rank = next((i for i, (p, _) in enumerate(scored) if p == instrument), None)
                if current_rank is not None:
                    _did_rank_compare = True
                    drop = abs(current_rank - open_rank)
                    # Reversal: rank went from "top of list (strong buy)"
                    # to "bottom of list (strong sell)" relative side = flipped.
                    signs_differ = True
                    if current_score is not None and open_score is not None and open_score != 0.0:
                        signs_differ = ((open_score > 0) != (current_score > 0))
                    if drop >= rev_rank_drop and signs_differ:
                        gate1_ok = True
                        gate1_reason = (f"RANK open={open_rank}→now={current_rank} "
                                        f"(drop={drop}≥{rev_rank_drop})")

            # Part 4 Gate1 FALLBACK branch (for CHF-style single-pair groups):
            # When compare_set is empty, treat synthetic rank buckets the same
            # as multi-pair ranks.  Note: for true 1-pair groups, open_rank is
            # always 0 (since there's only one ranked peer), so we compare
            # against the history-bucket mean — if we dropped ≥ 2 buckets
            # *and* strength sign flipped, trigger.
            if (
                not gate1_ok
                and not _did_rank_compare
                and _used_fallback_rank
                and _fallback_rank is not None
            ):
                _bucket_drop_threshold = max(
                    2,
                    int(max(1, int(rev_rank_drop * 3 / 4)) + 1),
                )
                _signs_differ_fb = False
                if current_score is not None and open_score is not None and open_score != 0.0:
                    _signs_differ_fb = ((open_score > 0) != (current_score > 0))
                _center_ref_bucket = (
                    int(_fallback_total_peers / 2) if (open_rank is None or len(compare_set) <= 1)
                    else int(open_rank)
                )
                _drop_fb = abs(_fallback_rank - _center_ref_bucket)
                if _drop_fb >= _bucket_drop_threshold or (
                    current_score is not None
                    and open_score is not None
                    and open_score != 0.0
                    and ((open_score > 0) != (current_score > 0))
                    and abs(current_score) >= rev_abs_min
                    and _drop_fb >= max(1, _bucket_drop_threshold - 1)
                ):
                    gate1_ok = True
                    gate1_reason = (
                        f"RANK FALLBACK open(bucket={_center_ref_bucket})→"
                        f"now(bucket={_fallback_rank}) "
                        f"(drop={_drop_fb}≥{_bucket_drop_threshold})"
                    )
                    if _signs_differ_fb:
                        gate1_reason += " + sign-flip match"
            # If no open_score/open_rank in comment, fail-closed gate1
            # (we need an apples-to-apples comparison to claim "reversal").
            if not gate1_ok and (open_score is None and open_rank is None):
                gate1_reason = "NO_OPEN_SCORE/RANK (cannot compute reversal) → gate1 CLOSED"
            elif not gate1_ok:
                gate1_reason = (f"open_sc={open_score} now_sc={current_score} "
                                f"open_rk={open_rank} → no reversal")
        except Exception as _e1:
            gate1_ok = False
            gate1_reason = f"STRENGTH lookup err: {_e1}"

        # ---- Gate 2: MA aligned against side ----
        gate2_ok = False
        ma_align: str | None = None
        try:
            ma_result = check_ma5_cross(
                instrument, require_aligned=ma_req, timeframes=tf_ma,
                cross_lookback=4, cross_weight=1.0, slope_weight=0.8,
                verbose=False,
            )
            ma_align = ma_result
            # ma_align ∈ {"BUY","SELL",None,"ABOVE","BELOW"} depending on
            # the helper path; map to side tokens.
            ma_opposite = False
            if isinstance(ma_align, str):
                ma_up   = ma_align in ("BUY",  "ABOVE", "LONG",  "EXPAND_UP")
                ma_down = ma_align in ("SELL", "BELOW", "SHORT", "EXPAND_DOWN")
                # For a BUY trade, ma_down is opposite.
                if side == "BUY" and ma_down:
                    ma_opposite = True
                elif side == "SELL" and ma_up:
                    ma_opposite = True
                else:
                    # If the helper produced a numeric alignment score we
                    # also try to interpret via alignment_count key on the
                    # raw returned dict variant.
                    pass
            # Try dict form: {"direction":"BUY","aligned_count":3, ...}
            try:
                if isinstance(ma_result, dict) and "aligned_count" in ma_result:
                    d = (ma_result.get("direction") or "").upper()
                    if (side == "BUY"  and d.startswith("SELL")) or \
                       (side == "SELL" and d.startswith("BUY")):
                        ac = int(ma_result.get("aligned_count", 0))
                        if ac >= int(ma_req):
                            ma_opposite = True
            except Exception:
                pass
            # H4 confirmation check
            h4_confirms = True
            if require_h4:
                try:
                    h4_align = check_ma5_cross(
                        instrument, require_aligned=0.1, timeframes=["H4"],
                        cross_lookback=4, cross_weight=1.0, slope_weight=0.8,
                        verbose=False,
                    )
                    h4_up   = isinstance(h4_align, str) and h4_align in ("BUY","ABOVE","LONG","EXPAND_UP")
                    h4_down = isinstance(h4_align, str) and h4_align in ("SELL","BELOW","SHORT","EXPAND_DOWN")
                    if side == "BUY" and not h4_down:
                        h4_confirms = False
                    elif side == "SELL" and not h4_up:
                        h4_confirms = False
                except Exception:
                    h4_confirms = False  # fail-closed if H4 cannot be read
            gate2_ok = bool(ma_opposite) and bool(h4_confirms)
        except Exception as _e2:
            gate2_ok = False
            ma_align = f"err:{_e2}"

        # ---- Gate 3: MACD histogram opposite on ≥N TFs ----
        gate3_ok = False
        macd_per_tf: dict[str, str | None] = {}
        try:
            macd_info = check_macd_histogram(
                instrument, timeframes=tf_macd, verbose=False,
            )
            agree = 0
            if isinstance(macd_info, dict):
                per_tf = macd_info.get("per_tf") or {}
                for tf_key in tf_macd:
                    tf_d = None
                    if isinstance(per_tf, dict) and tf_key in per_tf:
                        tf_entry = per_tf[tf_key]
                        if isinstance(tf_entry, dict):
                            tf_d = (tf_entry.get("direction") or tf_entry.get("trend") or "").upper()
                        elif isinstance(tf_entry, str):
                            tf_d = tf_entry.upper()
                    else:
                        tf_d = None
                    macd_per_tf[tf_key] = tf_d
                    if not tf_d:
                        continue
                    up   = ("UP" in tf_d) or ("EXPAND_UP" == tf_d) or ("BUY" == tf_d)
                    down = ("DOWN" in tf_d) or ("EXPAND_DOWN" == tf_d) or ("SELL" == tf_d)
                    if (side == "BUY" and down) or (side == "SELL" and up):
                        agree += 1
            gate3_ok = agree >= macd_agree_min
        except Exception as _e3:
            gate3_ok = False

        # ---- Gate 4: P/L rule (don't exit at loss unless allowed) ----
        gate4_ok = False
        pl_brief = "n/a"
        try:
            # Unrealized P/L from trade object:
            upl = getattr(trade, "unrealizedPL", None)
            price = getattr(trade, "price", None)
            avg_close = None
            if upl is not None:
                try:
                    f_upl = float(upl)
                    pl_brief = f"unrealPL={f_upl:+.2f}"
                    if f_upl >= 0.0:
                        gate4_ok = True
                    elif allow_at_loss:
                        gate4_ok = True
                        pl_brief += " (LOSS EXIT ALLOWED)"
                except Exception:
                    pass
            # If no unrealPL field, we can't tell → fail-closed for safety
        except Exception as _e4:
            gate4_ok = False
            pl_brief = f"pl-err:{_e4}"

        gates_passed = gate0_ok and gate1_ok and gate2_ok and gate3_ok and gate4_ok

        # ---- Summary log (single line every cycle, so we can audit) ----
        def _b(v): return "PASS" if v else "----"
        print(
            f"  [{ee_label} GATES {group_name}] {instrument} {side} "
            f"[age={age_min_str}] "
            f"G0(minhold)={_b(gate0_ok)} G1(strength-rev)={_b(gate1_ok)} "
            f"G2(MA-opp)={_b(gate2_ok)} G3(MACD≥{macd_agree_min})={_b(gate3_ok)} "
            f"G4(PL-rule)={_b(gate4_ok)} | "
            f"ma={ma_align} macd={macd_per_tf} pl={pl_brief} "
            f"g1={gate1_reason or ''}"
        )

        if gates_passed:
            reasons = []
            if gate0_ok: reasons.append(f"age≥{min_hold_min}min")
            reasons.append(f"STRENGTH:{gate1_reason or 'reversed'}")
            reasons.append(f"MA-OPPOSITE:{ma_align}")
            reasons.append(f"MACD-OPPOSITE({gate3_ok})")
            reasons.append(f"PL:{pl_brief}")
            print(
                f"  [{ee_label} {group_name}] {instrument} {side}: "
                f"ALL 5 GATES PASSED → {' + '.join(reasons)} → closing (bot-owned only)"
            )
            if not dry_run:
                ok, info = _close_bot_trades_for_instrument(
                    instrument,
                    req_id_prefix=("OVERRIDE_BOT" if is_override_trade else "EARLYEXIT_BOT"),
                )
                pl_line = ""
                if isinstance(info, dict):
                    for _k in ("realizedPL", "pl", "p/l"):
                        if _k in info and info[_k] not in (None, ""):
                            pl_line = f" pl={info[_k]}"
                            break
                print(f"    close result: ok={ok}{pl_line} info={info}")
            else:
                print(f"    [DRY-RUN] Would close bot trades on {instrument}")
        # ---- END EARLY-EXIT 5 GATES ----


# -------------------------------------------
# Main cycle
# -------------------------------------------
def run_cycle(dry_run: bool = None):
    if dry_run is None:
        dry_run = _args.dry_run

    _lock = _acquire_profile_lock(_args.profile)

    if _check_emergency_lock():
        print("[EMERGENCY] Lock file exists — skipping cycle. Delete .emergency_close_lock_v3 to resume.")
        return

    global _EFFECTIVE_LOTS, _MAX_OPEN_POSITIONS, _cross_net_cap
    _EFFECTIVE_LOTS = _resolve_effective_lots()

    _now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    _groups_str = ", ".join(f"{g}[{cfg['quote_ccy']}]" for g, cfg in _strategy_groups.items())
    print(
        f"\n{'=' * 70}\n"
        f"  BASE-CURRENCY STRENGTH BOT — SCHEDULED RUNNER v3.0\n"
        f"{'=' * 70}\n"
        f"  Strategy : Global strength matrix → per-group dominance filter → signal\n"
        f"  Groups   : {_groups_str}\n"
        f"  NetCap   : {_cross_net_cap}/ccy | Override  : enabled={getattr(_config_bot, 'DOMINANCE_OVERRIDE_ENABLED', True)}\n"
        f"  Profile  : {_profile_name} | Env: {_oanda_profile['env'].upper()} | DryRun: {dry_run}\n"
        f"  Account  : {_account_id}\n"
        f"  Lots     : {_EFFECTIVE_LOTS}\n"
        f"  MaxPos   : {_MAX_OPEN_POSITIONS} ({'LIVE' if _IS_LIVE else 'DEMO'}) | "
        f"MaxEntries: {_args.max_entries}\n"
        f"  Time     : {_now}\n"
        f"{'=' * 70}"
    )

    _sltp_guardian(dry_run=dry_run)

    print("\n[RUNNER] Building global strength matrix (shared across all groups)...")
    _global_scores = build_strength_matrix()
    print(format_strength_ranking(_global_scores))

    _print_dxy_reference(_global_scores)

    for gname, gcfg in _strategy_groups.items():
        _maintain_group_positions(gname, gcfg, dry_run, _global_scores)

    _all_trade_pairs = []
    for _gcfg in _strategy_groups.values():
        _q = _gcfg["quote_ccy"]
        _all_trade_pairs.extend(p for p in getattr(_config_bot, "STRENGTH_PAIRS", []) if p.endswith(f"_{_q}"))
    _all_trade_pairs = list(dict.fromkeys(_all_trade_pairs))
    if _all_trade_pairs:
        _load_mc_cache(_all_trade_pairs)

    _global_mc = _get_global_mc_regime(_all_trade_pairs) if _all_trade_pairs else "NO_MC_DATA"
    if _global_mc == "CONSOLIDATION":
        _MAX_OPEN_POSITIONS = MC_CONSOLIDATION_MAX_POSITIONS
        print(f"\n  [GLOBAL MC] → CONSOLIDATION → max positions = {_MAX_OPEN_POSITIONS}")
    elif _global_mc == "STRONG_MOMENTUM":
        _MAX_OPEN_POSITIONS = MC_STRONG_MOMENTUM_MAX_POSITIONS
        print(f"\n  [GLOBAL MC] → STRONG_MOMENTUM → max positions = {_MAX_OPEN_POSITIONS}")
    else:
        _MAX_OPEN_POSITIONS = MC_NEUTRAL_MAX_POSITIONS
        print(f"\n  [GLOBAL MC] → {_global_mc} → max positions = {_MAX_OPEN_POSITIONS} (NEUTRAL default)")

    # --- Dynamic CROSS_MAX_NET_PER_CCY (per-ccy net-exposure cap) ---
    # Philosophy (per user spec): expand to 3 ONLY under extreme conditions,
    # otherwise stick to the run.env/config base (typically 2).
    #
    # Conditions for BOOST to 3 (all must hold):
    #   A. Global MC regime is NOT CONSOLIDATION (no "warning" from MC)
    #   B. Either: global strength gap ≥ 1.8 (OVERRIDE threshold = "很强"),
    #             OR  MC regime == STRONG_MOMENTUM
    _ranked_scores = sorted(_global_scores.values(), reverse=True)
    _global_max_gap = (_ranked_scores[0] - _ranked_scores[-1]) if len(_ranked_scores) >= 2 else 0.0
    if _CROSS_NET_CAP_BASE > 2:
        print(
            f"  [NETCAP BASE⚠️] run.env CROSS_MAX_NET_PER_CCY={_CROSS_NET_CAP_BASE} > 2. "
            "All boost branches collapse to the same base; differentiation lost. "
            "Recommended BASE=2 so STRONG_MOMENTUM/STRONG_GAP can show meaningful lift."
        )
    _boost_net_cap = False
    if _global_mc != "CONSOLIDATION":
        if _global_max_gap >= 1.8 or _global_mc == "STRONG_MOMENTUM":
            _boost_net_cap = True
    # Force floor at 3 during boost, but never go below the user-configured base
    # (so a run.env base of 3 stays 3 regardless; a base of 2 expands only when safe)
    _cross_net_cap = max(_CROSS_NET_CAP_BASE, 3) if _boost_net_cap else _CROSS_NET_CAP_BASE
    if _boost_net_cap:
        print(
            f"  [NETCAP BOOST] gap={_global_max_gap:.3f} MC={_global_mc} → "
            f"CROSS_MAX_NET_PER_CCY {_CROSS_NET_CAP_BASE}→{_cross_net_cap}"
        )
    else:
        print(
            f"  [NETCAP BASE]  gap={_global_max_gap:.3f} MC={_global_mc} → "
            f"CROSS_MAX_NET_PER_CCY = {_cross_net_cap} (no boost)"
        )

    # --- Part D — STRONG_GAP synthetic MC label -------------------------
    # Pure MC can be NEUTRAL even when the global strength matrix shows a
    # spread ≥ 1.8 (e.g. USD 1.12 vs CHF -2.02 → 3.14 gap).  This is an
    # independent "strong signal" dimension, so we synthesize STRONG_GAP
    # and apply its own deltas: cross-net-cap +1, general position cap +0
    # (per user: do NOT blindly add risk via max-positions until observed).
    # The original raw MC (NEUTRAL) remains the group-level regime label
    # because per-group MC classification must not be retroactively altered
    # by a global spread-derived signal.
    STRONG_GAP_GAP_THRESHOLD = 1.8
    STRONG_GAP_NET_CAP_BOOST = 1
    STRONG_GAP_MAX_POS_BOOST = 0
    _was_neutral = _global_mc == "NEUTRAL"
    _gap_ok = _global_max_gap >= STRONG_GAP_GAP_THRESHOLD
    if _was_neutral and _gap_ok:
        _net_before = _cross_net_cap
        _mp_before = _MAX_OPEN_POSITIONS
        # Apply deltas — clamp net_cap to a sensible hard floor so an env
        # with base=1 doesn't leap to 10 overnight:
        _cross_net_cap = max(_CROSS_NET_CAP_BASE, _cross_net_cap + STRONG_GAP_NET_CAP_BOOST)
        _cross_net_cap = min(_cross_net_cap, 8)
        _MAX_OPEN_POSITIONS = max(
            1, _MAX_OPEN_POSITIONS + STRONG_GAP_MAX_POS_BOOST
        )
        print(
            f"  [MC SYNTHESIS] GAP={_global_max_gap:.4f} ≥{STRONG_GAP_GAP_THRESHOLD} "
            f"→ MC upgraded NEUTRAL→STRONG_GAP | "
            f"net_cap boosted {_net_before}→{_cross_net_cap}"
            + (
                f" | max_pos {_mp_before}→{_MAX_OPEN_POSITIONS}"
                if STRONG_GAP_MAX_POS_BOOST
                else f" | max_pos unchanged (={_MAX_OPEN_POSITIONS})"
            )
        )

    _print_mc_snapshot()

    _diag_strat = BaseCurrencyTrendStrategy(
        quote_ccy=next(iter(_strategy_groups.values()))["quote_ccy"],
        enable_atr_min_filter=_PROFILE_CFG.get("ENABLE_ATR_MINIMUM_FILTER", True),
        atr_min_pips=_PROFILE_CFG.get("ATR_MIN_PIPS", 6.0),
        atr_min_relative_pct=_PROFILE_CFG.get("ATR_MIN_RELATIVE_PCT", 0.045),
    )
    print("\n" + "=" * 60)
    _diag_groups = [g["quote_ccy"] for g in _strategy_groups.values()]
    print(f"[STRATEGY DIAGNOSTICS] Runtime parameters (applied to groups: {', '.join(_diag_groups)})")
    print("=" * 60)
    s = _diag_strat
    print(f"  QUOTE_CCY            : {s.quote_ccy}")
    print(f"  TRADE_PAIRS          : {s.trade_pairs}")
    print(f"  MIN_VALID_PAIRS      : {s.MIN_VALID_PAIRS}")
    print(f"  MIN_DOMINANT_PAIRS   : {s.MIN_DOMINANT_PAIRS}")
    print(f"  MIN_STRENGTH_PASS    : {s.MIN_STRENGTH_PASSING_PAIRS}")
    print(f"  ALIGNMENT_THRESHOLD  : {s.TREND_ALIGNMENT_REQUIRED} timeframes", end="")
    _cfg_align_maj = getattr(_config_bot, "ALIGNMENT_REQUIRE_MAJORITY", None)
    _cfg_align_min = getattr(_config_bot, "ALIGNMENT_THRESHOLD_MIN", None)
    _strat_align_maj = getattr(s, "ALIGNMENT_REQUIRE_MAJORITY", None)
    _strat_align_min = getattr(s, "ALIGNMENT_THRESHOLD_MIN", None)

    if _cfg_align_maj is None:
        _cfg_align_maj = False
    if _cfg_align_min is None:
        _cfg_align_min = 2

    _tp_count = len(s.trade_pairs) if hasattr(s, "trade_pairs") and s.trade_pairs else 3

    if _strat_align_maj:
        print(f" (MAJORITY mode: need ≥{_strat_align_min}/{_tp_count} aligned)")
    else:
        print(f" (STRICT mode: all {s.TREND_ALIGNMENT_REQUIRED} must match)")

    _align_warnings = []
    if _strat_align_maj != _cfg_align_maj:
        _align_warnings.append(
            f"ALIGNMENT_REQUIRE_MAJORITY: config={_cfg_align_maj} ≠ strategy-internal={_strat_align_maj}"
        )
    if _strat_align_min != _cfg_align_min:
        _align_warnings.append(
            f"ALIGNMENT_THRESHOLD_MIN: config={_cfg_align_min} ≠ strategy-internal={_strat_align_min}"
        )

    if _align_warnings:
        print("  ⚠️ [ALIGNMENT SELF-CHECK] MISMATCH — actual behavior may differ from logged:")
        for _w in _align_warnings:
            print(f"     ⚠️ {_w}")
    else:
        if getattr(_config_bot, "ALIGNMENT_REQUIRE_MAJORITY", None) is None or getattr(_config_bot, "ALIGNMENT_THRESHOLD_MIN", None) is None:
            _miss = []
            if getattr(_config_bot, "ALIGNMENT_REQUIRE_MAJORITY", None) is None: _miss.append("ALIGNMENT_REQUIRE_MAJORITY")
            if getattr(_config_bot, "ALIGNMENT_THRESHOLD_MIN", None) is None: _miss.append("ALIGNMENT_THRESHOLD_MIN")
            print(f"  ℹ️ [ALIGNMENT SELF-CHECK] {', '.join(_miss)} missing → using strategy defaults")
        else:
            print(f"  ✅ [ALIGNMENT SELF-CHECK] OK: logged matches strategy behavior")
    print(f"  STRENGTH_CUTOFF_RATIO: {s.STRENGTH_CUTOFF_RATIO} (dynamic = max_gap × ratio)")
    print(f"  MIN_STRENGTH_SCORE   : ±{s.MIN_STRENGTH_SCORE}")
    print(f"  MIN_MARKET_STRENGTH  : {s.MIN_MARKET_STRENGTH}")
    print(f"  ENABLE_ATR_MIN_FILTER: {s.ENABLE_ATR_MIN_FILTER}")
    if s.ENABLE_ATR_MIN_FILTER:
        print(f"  ATR_MIN_PIPS         : {s.ATR_MIN_ABSOLUTE_PIPS} → absolute={s.ATR_MIN_ABSOLUTE} (×pip={s.pip})")
        print(f"  ATR_MIN_RELATIVE%    : {s.ATR_MIN_RELATIVE_PCT}%")
    print(f"  ENABLE_ATR_SLTP      : {getattr(_config_bot, 'ENABLE_ATR_SLTP', True)}")
    print(f"  MIN_RR               : {s.MIN_RR}")
    print(f"  ENABLE_MACRO_PROTEC  : {getattr(_config_bot, 'ENABLE_MACRO_PROTECTION', False)}")
    print(f"  SKIP_SIDEWAYS_PAIRS  : {s.SKIP_SIDEWAYS_PAIRS}")
    print(f"  TRADE_TOP_PAIRS      : {s.TRADE_TOP_PAIRS}")
    print(f"  PIP_SIZE             : {s.pip}")
    print(f"  ── DOMINANCE FILTER ──")
    print(f"  DOMINANCE_RATIO      : enabled={s.DOMINANCE_RATIO_ENABLED} threshold={s.DOMINANCE_RATIO_THRESHOLD}")
    print(f"  GAP_SEPARATION       : {s.GAP_SEPARATION_THRESHOLD}")
    print(f"  ── OVERRIDE MODE ──")
    print(f"  OVERRIDE             : enabled={s.DOMINANCE_OVERRIDE_ENABLED} threshold={s.DOMINANCE_OVERRIDE_THRESHOLD}")
    _override_floor = getattr(_config_bot, "DOMINANCE_OVERRIDE_MEDIAN_FLOOR", None)
    if _override_floor is not None:
        print(f"  OVERRIDE_MEDIAN_FLOOR: {_override_floor} (prevents noise-triggered OVERRIDE)")
    print(f"  ── MC CONFLICT CHECK ──")
    print(f"  MC_CONFLICT_CHECK    : enabled={ENABLE_MC_CONFLICT_CHECK}")
    print(f"  MC_CONFLICT_BLOCK    : enabled={ENABLE_MC_CONFLICT_BLOCK} (SEVERE always blocked when True)")
    print(f"  MC_MODERATE_BLOCK    : enabled={ENABLE_MC_CONFLICT_BLOCK_MODERATE} (MODERATE needs BOTH BLOCK flags True)")
    print(f"  MC_CONFLICT_PROB_TH  : ≥{MC_CONFLICT_PROB_THRESHOLD*100:.0f}% reverse prob = MODERATE")
    print(f"  MC_CONFLICT_SEVERE_TH: ≥{MC_CONFLICT_SEVERE_THRESHOLD*100:.0f}% reverse prob = SEVERE")
    print(f"  ── CROSS-GROUP (exposure-based) ──")
    print(f"  CROSS_MAX_NET_PER_CCY: {_cross_net_cap}")
    print(f"  CROSS_GROUP_MUTEX    : DEPRECATED (no effect in v3; see patches/ for jcs legacy)")
    print("=" * 60)

    all_results = []
    for gname, gcfg in _strategy_groups.items():
        try:
            result = _run_single_group(gname, gcfg, _global_scores)
            all_results.append(result)
        except Exception as exc:
            print(f"  ❌ [GROUP {gname}] Strategy execution FAILED: {type(exc).__name__}: {exc}")
            traceback.print_exc()

    try:
        _open_net, _held = _build_open_exposure()
    except Exception as exc:
        print(f"  ❌ [GLOBAL] Cannot read open trades ({exc}) → HOLD this cycle (fail-closed)")
        print(f"\n{'=' * 70}\n[RUNNER v3] Cycle complete\n{'=' * 70}")
        return

    _basket = _pick_global_basket(
        all_results,
        max_entries=_args.max_entries,
        open_net=_open_net,
        held=_held,
    )

    if not _basket:
        print("\n[GLOBAL] No qualifying signals from any group → HOLD")
    else:
        print(f"\n[GLOBAL] Executing basket: {len(_basket)} signal(s)")
        _executed_count = 0
        _skipped_filtered_count = 0  # filtered by basket-level net/oppose/cap (pre-execution)
        _rejected_count = 0         # rejected at execution-layer (post-dispatch)
        _rejected_breakdown: dict[str, int] = {}
        _cycle_override_issued = 0
        for entry in _basket:
            _submitted_ok, _block_reason = _execute_single_signal(
                entry, dry_run, cycle_override_issued_before=_cycle_override_issued
            )
            _sig = entry["signal"]
            _is_override = bool(_sig.get("override_source"))
            if _submitted_ok:
                _executed_count += 1
                if _is_override:
                    _cycle_override_issued += 1
            else:
                if _block_reason in (
                    "OVERRIDE_CHAN_FULL",
                    "MAX_POSITIONS",
                    "ALREADY_HELD",
                ):
                    _skipped_filtered_count += 1
                else:
                    _rejected_count += 1
                if _block_reason:
                    _rejected_breakdown[_block_reason] = (
                        _rejected_breakdown.get(_block_reason, 0) + 1
                    )

        # Final audit line (three-state tally, never mis-report the count).
        print(
            f"\n[GLOBAL] Basket complete: "
            f"✅ EXECUTED={_executed_count}/{len(_basket)} sent"
            + (
                f" | ⏸️ SKIPPED={_skipped_filtered_count} filtered "
                f"(cap/limit/override-full/held)"
                if _skipped_filtered_count
                else ""
            )
            + (
                f" | 🚫 REJECTED={_rejected_count} "
                + (
                    ("("
                     + ", ".join(f"{k}={v}" for k, v in sorted(_rejected_breakdown.items()))
                     + ")")
                    if _rejected_breakdown
                    else "(broker/system)"
                )
                if _rejected_count
                else ""
            )
            + (
                f" | override_issued={_cycle_override_issued}/{OVERRIDE_MAX_PER_CYCLE}"
                if _cycle_override_issued or any(
                    e["signal"].get("override_source") for e in _basket
                )
                else ""
            )
        )

    print(f"\n{'=' * 70}\n[RUNNER v3] Cycle complete\n{'=' * 70}")


def _resolve_effective_lots() -> int:
    is_live = os.environ.get("OANDA_ENV", "practice").lower() in ("live", "real")
    if _args.lots is not None:
        return _args.lots
    env_key = "LIVE_LOT_SIZE" if is_live else "DEMO_LOT_SIZE"
    env_val = os.getenv(env_key)
    if env_val and env_val.strip():
        try:
            return int(env_val)
        except ValueError:
            pass
    return _config_bot.LIVE_LOT_SIZE if is_live else _config_bot.DEMO_LOT_SIZE


if __name__ == "__main__":
    run_cycle()
