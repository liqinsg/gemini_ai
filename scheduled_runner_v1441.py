"""
Scheduled Runner — JPY Strength Strategy
==========================================
v1.4.5 CLI Args + run.env Configuration Binding:
    • 新增 CLI 参数 (--min-gap / --max-entries / --mc-neutral-tp /
      --align / --strict / --gap-threshold)
    • 新增 run.env 环境变量绑定 (MIN_GAP / MAX_ENTRIES / MC_NEUTRAL_TP /
      ALIGN_REQUIRED / STRICT_ALIGN / GAP_THRESHOLD / DRY_RUN)
    • 优先级: CLI > run.env > 硬编码默认 (严格向后兼容)
    • MAX_ENTRIES>1 时列 [SELECTED]/[ALTERNATE], =1 保持 top-only
    • 只在 --live 模式生效; 无 --live 时所有新参数静默跳过

v1.4.4.1 auditable execution enhancement:
    • 幂等防重复开仓: OANDA Tag+Comment 精准识别 → 策略+Pair+方向+日期
      不再一刀切查有无持仓; 区分策略单/手动单/历史单
    • SL/TP Guardian 增强: 缺则补 + 偏差则更新(阈值±0.2%), 每次运行必校验
    • Tag 格式: JPY-STRENGTH_{PAIR}_{SIDE}_{YYYYMMDD}
    • Comment: 入口价|SL|TP|版本 便于审计

v1.4 原有功能不变: MC Regime / PostExitGate / 进程锁 / 多账户 / Dry-Run
"""

import sys
import time
import argparse
import os
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent

# ========== Hardcoded defaults (lowest priority) ==========
_DEFAULT_CFG = {
    "min_gap": 0.9314,
    "max_entries": 1,
    "mc_neutral_tp": 1.5,
    "align": 3,
    "strict": 3.0,
    "gap_threshold": 1.5,
}


def _parse_bool_env(val: str) -> bool:
    v = str(val).strip().lower()
    return v in ("1", "true", "t", "yes", "y", "on")


# ========== Parse CLI args FIRST (before any config import) ==========
_parser = argparse.ArgumentParser(
    description="JPY Strength Strategy — pick OANDA profile"
)
_parser.add_argument(
    "--profile",
    "-p",
    "--account",
    "-a",
    dest="profile",
    type=int,
    default=2,
    help="OANDA/strategy profile number",
)
_parser.add_argument(
    "--live",
    action="store_true",
    help="use the live OANDA environment; practice is the default",
)
_parser.add_argument(
    "--debug",
    type=int,
    choices=[1, 2, 3],
    help="Debug: 3=full open | 2=medium | 1=mild",
)
_parser.add_argument(
    "--dry-run",
    dest="dry_run",
    action="store_true",
    default=None,
    help="scan and read positions without opening, closing, or modifying orders",
)
_parser.add_argument(
    "--no-dry-run",
    dest="dry_run",
    action="store_false",
    help="explicitly disable dry-run (overrides run.env DRY_RUN=true)",
)
_parser.add_argument(
    "--lots",
    type=int,
    default=None,
    help="Override position size (units). Falls back to run.env LIVE_LOT_SIZE/DEMO_LOT_SIZE, then config_bot defaults.",
)
_parser.add_argument("--min-gap", "-m", type=float, default=None, help="Minimum strength gap to qualify a pair (USD_JPY threshold), default 0.9314")
_parser.add_argument("--max-entries", "-n", type=int, default=None, help="Max number of valid pairs to enter per cycle; 1=top only, 2+=basket")
_parser.add_argument("--mc-neutral-tp", type=float, default=None, help="TP multiplier when MC regime = NEUTRAL, default 1.5")
_parser.add_argument("--align", type=int, default=None, choices=[2, 3], help="Required aligned timeframes: 3=H4/H1/M30 all same; 2=majority")
_parser.add_argument("--strict", type=float, default=None, help="Base alignment threshold constant, default 3.0")
_parser.add_argument("--gap-threshold", type=float, default=None, help="Global GAP 'strong' classification level, default 1.5")

_args, _ = _parser.parse_known_args()

# ========== Validation (CLI first-pass for explicit out-of-range) ==========
if _args.max_entries is not None and _args.max_entries < 1:
    print(f"[CONFIG] ERROR: --max-entries must be >= 1, got {_args.max_entries}")
    sys.exit(2)
if _args.min_gap is not None and _args.min_gap < 0.01:
    print(f"[CONFIG] ERROR: --min-gap must be >= 0.01, got {_args.min_gap}")
    sys.exit(2)
if _args.align is not None and _args.align not in (2, 3):
    print(f"[CONFIG] ERROR: --align must be 2 or 3, got {_args.align}")
    sys.exit(2)

# ========== Load run.env (silently skip if missing) ==========
_ENV_LOADED_KEYS = {}
_run_env_path = PROJECT_ROOT / "run.env"
if _run_env_path.exists():
    load_dotenv(_run_env_path, override=False)
    _candidates = {
        "MIN_GAP", "MAX_ENTRIES", "MC_NEUTRAL_TP", "ALIGN_REQUIRED",
        "STRICT_ALIGN", "GAP_THRESHOLD", "LIVE_LOT_SIZE", "DRY_RUN",
    }
    for _k in _candidates:
        _v = os.environ.get(_k)
        if _v is not None and str(_v).strip() != "":
            _ENV_LOADED_KEYS[_k] = str(_v).strip()
            print(f"[CONFIG] loaded from run.env: {_k}={_ENV_LOADED_KEYS[_k]}")
else:
    print(f"[CONFIG] run.env not found at {_run_env_path} — skipping (using defaults/CLI)")

# ========== Merge: hardcoded defaults < run.env < CLI (only when --live) ==========
def _effective_cfg() -> dict:
    """Merge layers. If NOT --live, return pure defaults for backward compatibility."""
    live = bool(_args.live)
    cfg = dict(_DEFAULT_CFG)
    sources = {k: "defaults" for k in _DEFAULT_CFG}

    if not live:
        return cfg, sources

    env_map = {
        "min_gap":       ("MIN_GAP",        float),
        "max_entries":   ("MAX_ENTRIES",    int),
        "mc_neutral_tp": ("MC_NEUTRAL_TP",  float),
        "align":         ("ALIGN_REQUIRED", int),
        "strict":        ("STRICT_ALIGN",   float),
        "gap_threshold": ("GAP_THRESHOLD",  float),
    }
    for key, (env_key, conv) in env_map.items():
        raw = _ENV_LOADED_KEYS.get(env_key)
        if raw is None:
            continue
        try:
            parsed = conv(raw)
            cfg[key] = parsed
            sources[key] = "run.env"
        except (ValueError, TypeError):
            print(f"[CONFIG] WARNING: run.env {env_key}={raw!r} not {conv.__name__} — ignored")

    cli_map = {
        "min_gap":       _args.min_gap,
        "max_entries":   _args.max_entries,
        "mc_neutral_tp": _args.mc_neutral_tp,
        "align":         _args.align,
        "strict":        _args.strict,
        "gap_threshold": _args.gap_threshold,
    }
    for key, val in cli_map.items():
        if val is not None:
            cfg[key] = val
            sources[key] = "cli-override"

    return cfg, sources


_EFF_CFG, _EFF_SOURCES = _effective_cfg()

# ========== Final validation on the effective values (only matters for --live) ==========
if _args.live:
    if _EFF_CFG["align"] not in (2, 3):
        print(f"[CONFIG] ERROR: effective ALIGN={_EFF_CFG['align']} invalid (must be 2 or 3). Check CLI / run.env.")
        sys.exit(2)
    if _EFF_CFG["max_entries"] < 1:
        print(f"[CONFIG] ERROR: effective MAX_ENTRIES={_EFF_CFG['max_entries']} invalid (must be >= 1). Check CLI / run.env.")
        sys.exit(2)
    if _EFF_CFG["min_gap"] < 0.01:
        print(f"[CONFIG] ERROR: effective MIN_GAP={_EFF_CFG['min_gap']} invalid (must be >= 0.01). Check CLI / run.env.")
        sys.exit(2)

# ========== dry-run resolution: CLI --[no-]dry-run > run.env DRY_RUN > False ==========
if _args.dry_run is not None:
    _DRY_RUN_EFFECTIVE = bool(_args.dry_run)
elif "DRY_RUN" in _ENV_LOADED_KEYS:
    _DRY_RUN_EFFECTIVE = _parse_bool_env(_ENV_LOADED_KEYS["DRY_RUN"])
else:
    _DRY_RUN_EFFECTIVE = False
_args.dry_run = _DRY_RUN_EFFECTIVE

# ========== Print override banner (only if non-default source present anywhere) ==========
def _print_startup_banner():
    print("\n[CONFIG] "
          f"STRICT={_EFF_CFG['strict']}  "
          f"GAP_THRESHOLD={_EFF_CFG['gap_threshold']}  "
          f"MIN_GAP={_EFF_CFG['min_gap']}")
    print("         "
          f"MAX_ENTRIES={_EFF_CFG['max_entries']}  "
          f"MC_NEUTRAL_TP={_EFF_CFG['mc_neutral_tp']}  "
          f"ALIGN={_EFF_CFG['align']}")
    src_set = sorted(set(_EFF_SOURCES.values()))
    print(f"         Source: {' / '.join(src_set)}")
    if _args.live:
        any_override = any(s != "defaults" for s in _EFF_SOURCES.values())
        if any_override:
            overrides = []
            for k in _DEFAULT_CFG:
                if _EFF_SOURCES[k] != "defaults":
                    overrides.append(f"{k.upper()}={_EFF_CFG[k]}<-{_EFF_SOURCES[k]}")
            print(f"[CONFIG OVERRIDE] {', '.join(overrides)}")
    else:
        print("[CONFIG OVERRIDE] none (--live not set; new parameters are intentionally ignored for backward compatibility)")
    print()

# ===== Eagerly print banner NOW, before any profile/account check that might abort =====
_print_startup_banner()

# ========== Set OANDA_ENV BEFORE importing config (critical!) ==========
if _args.live:
    os.environ["OANDA_ENV"] = "live"

import config_oanda as _oanda_config
from utils.trading_core_v2 import TradingCore
import config as _config
import config_bot as _config_bot

from config import load_strategy_config
from config import (
    CHECK_INTERVAL_MINUTES,
    MIN_VALID_PAIRS_TO_TRADE,
    RISK_LEVEL,
    RISK_PROFILE,
    POST_EXIT_GATE_ENABLED,
    POST_EXIT_GATE_SHADOW,
    # ALIGNMENT_THRESHOLD,
    # DYNAMIC_RISK_TIMEFRAME,
    # SL_RATIO,
    TP_RATIO,
    SL_PIPS,
    TP_PIPS,
)
import custom_strategy_v1 as _strategy
from custom_strategy_v1 import analyze_custom_strategy, get_last_signal
from utils.strategy_helpers import check_ma5_alignment
from utils.oanda_state import build_client_extensions
from retry import with_retry
from get_mc_data import get_mc_data
from utils.post_exit_gate import PostExitGate
from utils.logging_utils import get_logger
import json
import fcntl

# import errno

from config_bot import (
    DEMO_LOT_SIZE as _CFG_BOT_DEMO_LOT,
    LIVE_LOT_SIZE as _CFG_BOT_LIVE_LOT,
)

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

if _profile_name not in _config_bot.PROFILE_CFG:
    print(f"[PROFILE] WARNING: {_profile_name} is not defined in config_bot.PROFILE_CFG — falling back to default (profile2)")
_PROFILE_CFG = _config_bot.load_profile(_profile_name)
_PROFILE_CFG["OANDA_ACCOUNT_ID"] = _account_id
for _key, _value in _PROFILE_CFG.items():
    setattr(_config, _key, _value)

_trading_core = TradingCore(
    oanda_client = _oanda_client,
    oanda_account_id = _account_id,
)

# ========== 幂等 & SL/TP 增强配置 — 新增常量 ==========
RUNNER_VERSION = "1.4.5"
STRATEGY_TAG_PREFIX = "JPY-STRENGTH"
PRICE_PRECISION_TOL = 0.001
STRATEGY_UPDATE_THRESHOLD = 0.005
# =====================================================

# ✅ ADD: 用 profile 配置重建 _active_strategy（注入 ATR 过滤参数）
_strategy._active_strategy = _strategy.JPYTrendStrategy(
    trade_pairs=_config.TRADE_PAIRS,
    enable_atr_min_filter=_PROFILE_CFG.get("ENABLE_ATR_MINIMUM_FILTER", True),
    atr_min_absolute=_PROFILE_CFG.get("ATR_MIN_ABSOLUTE", 0.060),
    atr_min_relative_pct=_PROFILE_CFG.get("ATR_MIN_RELATIVE_PCT", 0.045),
)

_RUN_CFG = load_strategy_config(_args.debug)
if _args.live:
    _RUN_CFG["ALIGNMENT_THRESHOLD"]    = _EFF_CFG["strict"]
    _RUN_CFG["STRENGTH_GAP_THRESHOLD"] = _EFF_CFG["gap_threshold"]
    _RUN_CFG["MIN_STRENGTH_SCORE"]     = _EFF_CFG["min_gap"]
    try:
        _config.MC_TP_MULTIPLIER_NEUTRAL = _EFF_CFG["mc_neutral_tp"]
    except Exception:
        pass
_config.ALIGNMENT_THRESHOLD = _RUN_CFG["ALIGNMENT_THRESHOLD"]
_config.STRENGTH_GAP_THRESHOLD = _RUN_CFG["STRENGTH_GAP_THRESHOLD"]
_config.MIN_STRENGTH_SCORE = _RUN_CFG["MIN_STRENGTH_SCORE"]
_config.REQUIRE_ALIGNED = _EFF_CFG["align"] if _args.live else _RUN_CFG["ALIGNMENT_THRESHOLD"]
if _args.debug is None:
    print(
        f"[CONFIG] STRICT defaults → ALIGN={_RUN_CFG['ALIGNMENT_THRESHOLD']}  GAP={_RUN_CFG['STRENGTH_GAP_THRESHOLD']}  MIN={_RUN_CFG['MIN_STRENGTH_SCORE']}"
    )


def _resolve_effective_lots() -> tuple[int, str]:
    is_live = bool(_args.live)
    if _args.lots is not None:
        if _args.lots <= 0:
            print(
                f"  [LOT] WARNING: CLI --lots={_args.lots} invalid (must be >0), falling through"
            )
        else:
            return _args.lots, f"CLI --lots={_args.lots}"
    env_key = "LIVE_LOT_SIZE" if is_live else "DEMO_LOT_SIZE"
    env_val = _ENV_LOADED_KEYS.get(env_key) or os.getenv(env_key)
    if env_val and str(env_val).strip():
        try:
            parsed = int(str(env_val).strip())
            if parsed <= 0:
                print(
                    f"  [LOT] WARNING: run.env {env_key}={env_val} invalid (<=0), falling through"
                )
            else:
                return parsed, f"run.env {env_key}={env_val}"
        except ValueError:
            print(
                f"  [LOT] WARNING: run.env {env_key}={env_val} not int, falling through"
            )
    cfg_bot_default = _CFG_BOT_LIVE_LOT if is_live else _CFG_BOT_DEMO_LOT
    return (
        cfg_bot_default,
        f"config_bot {'LIVE_LOT_SIZE' if is_live else 'DEMO_LOT_SIZE'}={cfg_bot_default}",
    )


_EFFECTIVE_LOTS, _LOT_SOURCE = _resolve_effective_lots()
RISK_PROFILE[RISK_LEVEL]["units"] = _EFFECTIVE_LOTS

_log = get_logger(f"scheduled_runner_v{RUNNER_VERSION}")
POST_EXIT_SHADOW_LOG_PATH = os.environ.get(
    "POST_EXIT_SHADOW_LOG_PATH", "logs/post_exit_gate_shadow.jsonl"
)

# Emergency lock prevents automatic re-entry after an emergency close-all
EMERGENCY_LOCK_FILE = PROJECT_ROOT / ".emergency_close_lock_v144"


def _set_emergency_lock_v144(info: str = "emergency_close_all_jpy v144") -> None:
    try:
        EMERGENCY_LOCK_FILE.write_text(f"{time.time()}|{info}\n")
        print(f"  [EXEC] emergency lock set: {EMERGENCY_LOCK_FILE}")
    except Exception as e:
        print(f"  [EXEC] Failed to set emergency lock: {e}")


def _clear_emergency_lock_v144() -> None:
    try:
        if EMERGENCY_LOCK_FILE.exists():
            EMERGENCY_LOCK_FILE.unlink()
            print("  [EXEC] emergency lock cleared")
    except Exception as e:
        print(f"  [EXEC] Failed to clear emergency lock: {e}")


def _is_emergency_lock_active_v144() -> bool:
    try:
        return EMERGENCY_LOCK_FILE.exists()
    except Exception:
        return False


def emergency_close_all_jpy_v144(
    require_practice_check: bool = True, set_lock: bool = True
) -> dict:
    acct = _trading_core.oanda_account_id
    if not acct:
        print("  [EXEC][EMERGENCY] ❌ missing account id for emergency close")
        return {"found": 0, "closed": 0, "failed": 0, "details": []}

    try:
        if require_practice_check and _oanda_profile["env"] != "practice":
            print(
                f"  [EXEC][EMERGENCY] WARNING: env={_oanda_profile['env']} (not practice). Aborting emergency close."
            )
            return {"found": 0, "closed": 0, "failed": 0, "details": []}
    except Exception:
        pass

    print("\n" + "!" * 60)
    print(
        "[EXEC][EMERGENCY] Initiating EMERGENCY CLOSE ALL JPY positions — BYPASSING strategy filters (v144)"
    )
    print(f"[EXEC][EMERGENCY] Account: {acct}")

    found = closed = failed = 0
    details = []
    try:
        open_positions = _trading_core.get_all_open_positions()

        for p in open_positions:
            instr = p.get("instrument")
            if not instr:
                continue
            if "_JPY" not in instr:
                details.append({"instrument": instr, "status": "skipped_not_jpy"})
                continue
            found += 1
            long_u = int(float(p.get("long", {}).get("units", 0)))
            short_u = int(float(p.get("short", {}).get("units", 0)))
            if long_u == 0 and short_u == 0:
                details.append({"instrument": instr, "status": "already_flat"})
                continue

            try:
                print(f"  [EXEC][EMERGENCY] Closing {instr}")
                ok = _trading_core.close_position(instrument=instr)
                if ok:
                    print(f"  [EXEC][EMERGENCY] ✅ Closed {instr}")
                    closed += 1
                    details.append({"instrument": instr, "status": "closed"})
                else:
                    print(f"  [EXEC][EMERGENCY] ❌ Failed to close {instr}")
                    failed += 1
                    details.append({"instrument": instr, "status": "failed"})
            except Exception as e:
                print(f"  [EXEC][EMERGENCY] ❌ Exception closing {instr}: {e}")
                failed += 1
                details.append(
                    {"instrument": instr, "status": "failed", "error": str(e)}
                )

    except Exception as e:
        print(f"  [EXEC][EMERGENCY] ❌ emergency enumeration failed: {e}")
        return {"found": found, "closed": closed, "failed": failed, "details": details}

    if set_lock:
        try:
            _set_emergency_lock_v144(
                info=f"emergency_close_all_jpy_v144 account={acct}"
            )
        except Exception:
            pass

    print("\n" + "!" * 60)
    print("📋 EMERGENCY CLOSE REPORT (v144)")
    print("!" * 60)
    print(f"  Account: {acct}")
    print(f"  JPY instruments found: {found} | Closed: {closed} | Failed: {failed}")
    for d in details:
        print(f"    - {d.get('instrument','?')}: {d.get('status')}")

    return {"found": found, "closed": closed, "failed": failed, "details": details}


def _close_pair_position_v144(account_id: str, instrument: str) -> tuple[bool, dict]:
    pos = _trading_core.get_open_position(instrument)
    if not pos:
        return True, {"status": "already_flat"}
    ok = _trading_core.close_position(instrument=instrument)
    if ok:
        return True, {"status": "closed"}
    return False, {"status": "failed"}


# ========== 幂等工具函数 — 新增 ==========
def make_strategy_tag(pair: str, side: str) -> str:
    """生成幂等Tag: JPY-STRENGTH_AUD_JPY_SELL_20260914"""
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{STRATEGY_TAG_PREFIX}_{pair}_{side.upper()}_{date_str}"


def make_strategy_comment(entry: float, sl: float, tp: float) -> str:
    """结构化Comment 便于审计"""
    return f"v{RUNNER_VERSION}|entry={entry:.5f}|SL={sl:.5f}|TP={tp:.5f}"


def _is_jpy_strength_trade(trade: dict) -> bool:
    """Identify this runner's trades from OANDA-persisted strategy metadata."""
    tag = str(trade.get("tag") or trade.get("clientExtensions", {}).get("tag") or "")
    return tag.startswith(STRATEGY_TAG_PREFIX)


def _check_pair_level_strategy_position(pair: str, side: str) -> tuple[bool, str]:
    try:
        for trade in _trading_core.get_all_open_trades():
            if trade.get("instrument") != pair or not _is_jpy_strength_trade(trade):
                continue
            current_side = "BUY" if float(trade.get("currentUnits", 0)) > 0 else "SELL"
            reason = (
                "same-direction duplicate"
                if current_side == side.upper()
                else "pair-level protection; opposite-direction dual position prohibited"
            )
            print(
                f"  [IDEMPOTENCY] BLOCK {pair} {side}: {reason}; trade_id={trade.get('id')}"
            )
            return False, reason

        try:
            for order in _trading_core.get_pending_orders():
                if order.get("instrument") != pair:
                    continue
                tag = order.get("tag", "") or order.get("clientExtensions", {}).get(
                    "tag", ""
                )
                if tag.startswith(STRATEGY_TAG_PREFIX):
                    reason = "pending order exists → pair blocked"
                    print(f"  [IDEMPOTENCY] BLOCK {pair} {side}: {reason}")
                    return False, reason
        except Exception:
            pass

        return True, "no JPY-STRENGTH position on pair"
    except Exception as exc:
        print(
            f"  [IDEMPOTENCY] FAIL CLOSED {pair} {side}: open-trade query failed: {exc}"
        )
        return False, "open-trade query failure (fail closed)"


def _has_exact_strategy_position(pair: str, side: str) -> bool:
    """Backward-compatible boolean facade for pair-level idempotency."""
    allowed, _reason = _check_pair_level_strategy_position(pair, side)
    return not allowed


def _sltp_decision(
    current: float | None, calculated: float
) -> tuple[str, float | None]:
    """Separate display precision from the strategy's update decision."""
    if current is None:
        return "UPDATE_REQUIRED", None
    delta = calculated - current
    magnitude = round(abs(delta), 10)
    if magnitude < PRICE_PRECISION_TOL:
        return "NO_CHANGE", delta
    if magnitude < STRATEGY_UPDATE_THRESHOLD:
        return "MONITOR_ONLY", delta
    return "UPDATE_REQUIRED", delta


def _audit_side(
    label: str,
    current: float | None,
    calculated: float,
    decision: str,
    request: str = "-",
    result: str = "-",
    order_id: str | None = None,
) -> None:
    delta = "N/A" if current is None else f"{calculated - current:+.5f}"
    print(
        f"{label}:\n  OANDA_CURRENT={'NONE' if current is None else f'{current:.5f}'}\n"
        f"  CALC={calculated:.5f}\n  DELTA={delta}\n  DECISION={decision}\n"
        f"  REQUEST={request}\n  OANDA_RESULT={result}"
        + (f"\n  ORDER_ID={order_id}" if order_id else "")
    )


def _confirmation_result(
    order: dict, calculated: float, instrument: str
) -> tuple[str, str | None]:
    order_id = order.get("id")
    if order_id and order.get("price") == TradingCore.format_price_for_instrument(
        calculated, instrument
    ):
        return "CONFIRMED", order_id
    return "NOT_CONFIRMED", order_id


def _new_cycle_report() -> dict:
    return {
        "trades_scanned": 0,
        "sl_required": 0,
        "tp_required": 0,
        "sl_requests": 0,
        "tp_requests": 0,
        "confirmed": 0,
        "partial": 0,
        "not_confirmed": 0,
        "failed": 0,
        "entries_blocked": 0,
        "same_direction": 0,
        "opposite_direction": 0,
        "query_failures": 0,
        "candidate": "NONE",
        "idempotency": "N/A",
        "final_action": "HOLD",
        "reason": "No qualifying candidate.",
        "next_plan": "Current state: no qualifying candidate; monitoring JPY relative strength + existing strategy qualification rules.",
    }


def _print_full_cycle_report(report: dict, profile: dict, dry_run: bool) -> None:
    print("\n" + "═" * 50)
    print(f"📋 FULL CYCLE REPORT v{RUNNER_VERSION}")
    print("═" * 50)
    print(f"TIME: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(
        f"ACCOUNT: {_trading_core.oanda_account_id}\nPROFILE: {profile}\nMODE: {'DRY-RUN' if dry_run else _oanda_profile['env'].upper()}"
    )
    print("🔧 [SL/TP POSITION MAINTENANCE]")
    print(
        f"  Trades scanned: {report['trades_scanned']} | SL update required: {report['sl_required']} | TP update required: {report['tp_required']}"
    )
    print(
        f"  SL requests sent: {report['sl_requests']} | TP requests sent: {report['tp_requests']} | OANDA confirmed: {report['confirmed']}"
    )
    print(
        f"  Partial confirmations: {report['partial']} | Not confirmed: {report['not_confirmed']} | Failed: {report['failed']}"
    )
    print("🛡️ [IDEMPOTENCY]")
    print(
        f"  Pair-level protection: ENABLED | Entries blocked: {report['entries_blocked']} | Same-direction duplicates: {report['same_direction']} | Opposite-direction pair blocks: {report['opposite_direction']} | Query failures / fail-closed: {report['query_failures']}"
    )
    print("📈 [ENTRY DECISION]")
    print(
        f"  Candidate: {report['candidate']} | Idempotency: {report['idempotency']} | Final action: {report['final_action']}\n  Core reason: {report['reason']}"
    )
    print("📌 [NEXT PLAN]")
    print(f"  {report['next_plan']}\n  Next evaluation: next scheduled cycle")
    print("═" * 50)


# ==========================================


def _log_shadow(record: dict) -> bool:
    """Append one JSON record to the post-exit shadow log. Never raises."""
    try:
        path = Path(POST_EXIT_SHADOW_LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str, sort_keys=True) + "\n")
        return True
    except Exception as e:
        print(f"  [POST_EXIT_SHADOW] Log write failed (non-fatal): {e}")
        return False


def _acquire_profile_lock(profile: int):
    """Acquire a per-profile flock at /tmp/runner_{profile}.lock.
    If lock cannot be acquired immediately, exit the process to avoid overlapping runs.
    Returns the open file descriptor which should be kept open while the process runs.
    """
    lock_path = Path(f"/tmp/runner_{profile}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"pid:{os.getpid()} start:{datetime.now().isoformat()}\n")
        lock_file.flush()
        return lock_file
    except BlockingIOError:
        print(f"[LOCK] Another runner (profile {profile}) is active — exiting.")
        sys.exit(0)


# === MC Regime → 交易模式映射 ===
def _regime_policy(mc_regime: str) -> str:
    reg = (mc_regime or "").upper()
    if "CONSOLIDATION" in reg:
        return "cautious"
    if "STRONG" in reg and "MOMENTUM" in reg:
        return "aggressive"
    return "normal"


MC_REGIME_MULTIPLIER_SHADOW = {
    "STRONG_MOMENTUM": 0.90,
    "NEUTRAL": 1.00,
    "CONSOLIDATION": 1.10,
}


def _regime_shadow_multiplier(mc_regime: str) -> float:
    reg = (mc_regime or "").upper()
    if "CONSOLIDATION" in reg:
        return MC_REGIME_MULTIPLIER_SHADOW["CONSOLIDATION"]
    if "STRONG" in reg and "MOMENTUM" in reg:
        return MC_REGIME_MULTIPLIER_SHADOW["STRONG_MOMENTUM"]
    return MC_REGIME_MULTIPLIER_SHADOW["NEUTRAL"]


def _regime_params(mode: str) -> dict:
    return {
        "max_positions": {
            "cautious": _config.MC_MAX_POSITIONS_CONSOLIDATION,
            "normal": _config.MC_MAX_POSITIONS_NEUTRAL,
            "aggressive": _config.MC_MAX_POSITIONS_AGGRESSIVE,
        }.get(mode, 1),
        "tp_multiplier": {
            "cautious": _config.MC_TP_MULTIPLIER_CONSOLIDATION,
            "normal": _config.MC_TP_MULTIPLIER_NEUTRAL,
            "aggressive": _config.MC_TP_MULTIPLIER_AGGRESSIVE,
        }.get(mode, 1.0),
        "exit_tightness": {
            "cautious": _config.MC_EXIT_TIGHTNESS_CONSOLIDATION,
            "normal": _config.MC_EXIT_TIGHTNESS_NEUTRAL,
            "aggressive": _config.MC_EXIT_TIGHTNESS_AGGRESSIVE,
        }.get(mode, 1.0),
    }


def _is_jpy_cross(pair: str) -> bool:
    p = pair.upper().replace("=X", "").replace("_", "")
    return p.endswith("JPY")


def _jpy_cross_direction_compatible(s1: dict, s2: dict) -> bool:
    if not (_is_jpy_cross(s1["pair"]) and _is_jpy_cross(s2["pair"])):
        return True
    return s1["action"].upper() == s2["action"].upper()


def _get_mc_for_pair(pair: str, timeframe: str = "D") -> dict | None:
    try:
        data = get_mc_data(timeframe=timeframe, date_val="latest", pair=pair)
        pairs = data.get("pairs") or []
        return pairs[0] if pairs else None
    except Exception as e:
        print(f"  [MC FETCH] pair={pair} tf={timeframe} failed: {e}")
        return None


def _print_mc_snapshot():
    print("\n  === MC DAILY REGIME SNAPSHOT ===")
    for pair in _config.TRADE_PAIRS:
        try:
            mc = _get_mc_for_pair(pair)
            if mc:
                regime = mc.get("regime", "N/A")
                p_up = mc.get("p_up", "?")
                p_down = mc.get("p_down", "?")
                price = mc.get("current_price", mc.get("expected_price", "?"))
                print(
                    f"  {pair:10s} | regime={regime} | P(UP)={p_up}% P(DOWN)={p_down}% | last={price}"
                )
            else:
                print(f"  {pair:10s} | [No MC data]")
        except Exception as e:
            print(f"  {pair:10s} | [MC load error: {e}]")
    print("  === END MC SNAPSHOT ===\n")


# ========== SL/TP Guardian 增强版 — 修复版 (兼容原有函数签名) ==========
# ========== SL/TP Guardian — v1.4.4 auditable wrapper ==========
def _validate_and_repair_sltp(report: dict, dry_run: bool):
    print("  [SL/TP GUARDIAN] === FULL SCAN STRATEGY TRADES ===")
    try:
        open_trades = _trading_core.get_all_open_trades()
        print(f"  [SCAN] Found {len(open_trades)} open trade(s)")
    except Exception as exc:
        print(f"  [SCAN FAILED] {exc}")
        report["failed"] += 1
        return

    for trade in open_trades:
        if not _is_jpy_strength_trade(trade):
            continue
        trade_id = str(trade.get("id") or trade.get("tradeID") or "")
        instrument = trade.get("instrument", "")
        side = "BUY" if float(trade.get("currentUnits", 0)) > 0 else "SELL"
        report["trades_scanned"] += 1
        try:
            info = _trading_core.get_trade_details(trade_id)
            entry = float(info.get("price") or info.get("initialPrice") or 0)
            if entry <= 0:
                raise ValueError(f"invalid entry price: {entry}")
        except Exception as exc:
            print(
                f"  [SL/TP AUDIT] {instrument} {side} T{trade_id}: detail fetch FAILED: {exc}"
            )
            report["failed"] += 1
            continue

        pip = getattr(_config, "JPY_PIP", 0.01) if "JPY" in instrument else 0.0001
        sl_price = entry - SL_PIPS * pip if side == "BUY" else entry + SL_PIPS * pip
        tp_price = (
            entry + TP_PIPS * pip * TP_RATIO
            if side == "BUY"
            else entry - TP_PIPS * pip * TP_RATIO
        )
        calculated_sl = float(
            TradingCore.format_price_for_instrument(sl_price, instrument)
        )
        calculated_tp = float(
            TradingCore.format_price_for_instrument(tp_price, instrument)
        )
        sl_order, tp_order = (
            info.get("stopLossOrder") or {},
            info.get("takeProfitOrder") or {},
        )
        current_sl = (
            float(sl_order["price"]) if sl_order.get("price") is not None else None
        )
        current_tp = (
            float(tp_order["price"]) if tp_order.get("price") is not None else None
        )
        sl_decision, _ = _sltp_decision(current_sl, calculated_sl)
        tp_decision, _ = _sltp_decision(current_tp, calculated_tp)

        if current_sl is None:
            need_sl = sl_decision == "UPDATE_REQUIRED"
        else:
            need_sl = False
            sl_decision = "DRM_MANAGED"

        if current_tp is None:
            need_tp = tp_decision == "UPDATE_REQUIRED"
        else:
            need_tp = False
            tp_decision = "DRM_MANAGED"

        report["sl_required"] += int(need_sl)
        report["tp_required"] += int(need_tp)
        print(f"\n  [SL/TP AUDIT] {instrument} {side} T{trade_id} | entry={entry:.5f}")

        sl_request = tp_request = "-"
        sl_result = tp_result = "-"
        sl_id = tp_id = None
        if need_sl or need_tp:
            if dry_run:
                sl_request = tp_request = "SKIPPED_DRYRUN"
            else:
                sl_request = tp_request = "SENT"
                report["sl_requests"] += 1
                report["tp_requests"] += 1
                try:
                    sent = _trading_core.attach_sl_tp_to_open_trade(
                        instrument=instrument,
                        stop_loss=calculated_sl,
                        take_profit=calculated_tp,
                        dry_run=False,
                    )
                    if not sent:
                        sl_result = tp_result = "FAILED"
                    else:
                        final_trade = _trading_core.get_trade_details(trade_id)
                        final_sl, final_tp = (
                            final_trade.get("stopLossOrder") or {},
                            final_trade.get("takeProfitOrder") or {},
                        )
                        sl_result, sl_id = _confirmation_result(
                            final_sl, calculated_sl, instrument
                        )
                        tp_result, tp_id = _confirmation_result(
                            final_tp, calculated_tp, instrument
                        )
                except Exception as exc:
                    print(
                        f"  [SL/TP AUDIT] request/verification FAILED: {type(exc).__name__}: {exc}"
                    )
                    sl_result = tp_result = "FAILED"

            outcomes = {sl_result, tp_result}
            if "FAILED" in outcomes:
                report["failed"] += 1
            elif "NOT_CONFIRMED" in outcomes:
                if "CONFIRMED" in outcomes:
                    report["partial"] += 1
                else:
                    report["not_confirmed"] += 1
            elif "CONFIRMED" in outcomes:
                report["confirmed"] += 1

        _audit_side(
            "SL", current_sl, calculated_sl, sl_decision, sl_request, sl_result, sl_id
        )
        _audit_side(
            "TP", current_tp, calculated_tp, tp_decision, tp_request, tp_result, tp_id
        )

    print(f"\n  [SUMMARY] Strategy trades scanned: {report['trades_scanned']}")


# =====================================================


def run_cycle(dry_run=None):
    if dry_run is None:
        dry_run = _args.dry_run
    profile = RISK_PROFILE[RISK_LEVEL]
    report = _new_cycle_report()
    print(
        f"\n[{datetime.now().isoformat()}] === JPY Strength Scan | Risk Level: {RISK_LEVEL} | v{RUNNER_VERSION} ==="
    )
    # If emergency lock is active, avoid opening new entries this cycle,
    # but still allow scanning and exit handling so the bot can close opposite positions.
    emergency_lock_active = _is_emergency_lock_active_v144()
    if emergency_lock_active:
        print(
            "  ⚠️ Emergency lock active (v144) — will prevent NEW entries this cycle but will still process exit signals"
        )
    _print_mc_snapshot()
    _validate_and_repair_sltp(report, dry_run=dry_run)

    # ===== EARLY EXIT: scan open JPY trades and close if MA alignment opposes position =====
    try:
        if not dry_run:
            print(
                "\n  [EARLY-EXIT] Scanning open JPY trades for opposite MA alignment..."
            )
            for trade in _trading_core.get_all_open_trades():
                instr = trade.get("instrument")
                if not instr or not instr.endswith("_JPY"):
                    continue
                current_units = float(trade.get("currentUnits", 0))
                if current_units == 0:
                    continue
                current_side = "BUY" if current_units > 0 else "SELL"
                try:
                    ma_align = check_ma5_alignment(instr, require_aligned=2)
                except Exception as _e:
                    ma_align = None
                if ma_align and ma_align != current_side:
                    print(
                        f"  [EARLY-EXIT] {instr}: position {current_side} vs MA align {ma_align} → closing now"
                    )
                    ok, info = _close_pair_position_v144(
                        account_id=_trading_core.oanda_account_id, instrument=instr
                    )
                    if ok:
                        print(f"  [EARLY-EXIT] ✅ Closed {instr}: {info}")
                    else:
                        print(f"  [EARLY-EXIT] ❌ Failed to close {instr}: {info}")
    except Exception as e:
        print(f"  [EARLY-EXIT] scan error (non-fatal): {e}")

    try:
        with_retry(
            analyze_custom_strategy, max_attempts=3, delay=5, label="strategy_scan"
        )
        signal_data = get_last_signal()
        if signal_data is None:
            print("[CYCLE] No qualifying signals this cycle. HOLD.")
            report["reason"] = (
                "No qualifying candidate from the existing strategy scan."
            )
            return

        mc_data = _get_mc_for_pair(signal_data["pair"])
        mc_regime = mc_data.get("regime", "N/A") if mc_data else "NO_MC_DATA"
        mode = _regime_policy(mc_regime) if _config.MC_REGIME_ENABLED else "normal"
        print(f"  [MC REGIME] {signal_data['pair']} → {mc_regime} | mode={mode}")
        params = _regime_params(mode)
        max_positions, tp_mult = params["max_positions"], params["tp_multiplier"]
        if mode == "cautious":
            score, hurdle = (
                abs(signal_data.get("strength_score", 0.0)),
                _config.MC_REGIME_STRENGTH_HURDLE_CONSOLIDATION,
            )
            print(
                f"  [MC DECISION] CONSOLIDATION — strength hurdle ≥ {hurdle} | max_pos=1 | TP×{tp_mult}"
            )
            if score < hurdle:
                print(
                    f"🚫 CONSOLIDATION cautious: strength={score:.4f} < {hurdle}, HOLD"
                )
                report["reason"] = "MC consolidation strength hurdle was not met."
                report["next_plan"] = (
                    "Current state: candidate exists but the existing MC regime gate is not satisfied."
                )
                return
        elif mode == "aggressive":
            print(
                f"  [MC DECISION] STRONG MOMENTUM — top{max_positions} | TP×{tp_mult}"
            )
        else:
            print(f"  [MC DECISION] NEUTRAL — max_pos={max_positions} | TP×{tp_mult}")

        max_entries = _EFF_CFG["max_entries"] if _args.live else 1
        candidates = [signal_data]
        if (mode in ("aggressive", "normal") and _config.ENABLE_MC_BASKET_EXECUTION) or max_entries > 1:
            fetch_n = max(max_entries, max_positions) if mode in ("aggressive", "normal") else max_entries
            try:
                top_n = _strategy.get_top_signals(n=fetch_n) or [signal_data]
            except Exception as exc:
                top_n = [signal_data]
                print(f"  [MC REGIME] top{fetch_n} failed → fallback top1: {exc}")
            compatible = [top_n[0]]
            for candidate in top_n[1:]:
                if all(
                    _jpy_cross_direction_compatible(candidate, accepted)
                    for accepted in compatible
                ):
                    compatible.append(candidate)
                else:
                    print(
                        f"  [MC REGIME] SKIP {candidate['pair']} {candidate['action']}: direction conflict"
                    )
                    try:
                        print(
                            "  [EXEC][EMERGENCY] Direction conflict detected — invoking emergency_close_all_jpy_v144()"
                        )
                        emergency_close_all_jpy_v144()
                    except Exception as e:
                        print(f"  [EXEC][EMERGENCY] Failed to run emergency close: {e}")
            candidates = sorted(compatible, key=lambda value: abs(value.get("strength_score", 0.0)), reverse=True)
            print(f"  [SELECTION] MAX_ENTRIES={max_entries}  pool({len(candidates)}): {[c['pair'] for c in candidates]}")
            if max_entries > 1 and len(candidates) > 0:
                for idx, c in enumerate(candidates):
                    label = "[SELECTED]" if idx < max_entries else "[ALTERNATE]"
                    print(f"    {label} #{idx+1}: {c['pair']} {c['action']} score={abs(c.get('strength_score',0)):.4f}")
            candidates = candidates[:max_entries] if max_entries > 1 else candidates[:1]
        elif not _config.ENABLE_MC_BASKET_EXECUTION:
            print(f"  [MC REGIME] BASKET DISABLED → top1 only: {signal_data['pair']}")
            candidates = [signal_data]

        for candidate in sorted(
            candidates,
            key=lambda value: abs(value.get("strength_score", 0.0)),
            reverse=True,
        ):
            candidate = dict(candidate)
            pair, action = candidate["pair"], candidate["action"]
            if tp_mult != 1.0:
                risk_distance = abs(candidate["entry"] - candidate["stop_loss"])
                candidate["take_profit"] = round(
                    candidate["entry"]
                    + (-1 if action.upper() == "SELL" else 1) * risk_distance * tp_mult,
                    5,
                )
                candidate["risk_reward"] = round(
                    abs(candidate["take_profit"] - candidate["entry"]) / risk_distance,
                    2,
                )
                print(
                    f"  [MC TP] {pair}: TP×{tp_mult} → {candidate['take_profit']} R:R {candidate['risk_reward']}"
                )

            gate_enabled, gate_shadow, allow_open, effective_units = (
                getattr(_config, "POST_EXIT_GATE_ENABLED", True),
                getattr(_config, "POST_EXIT_GATE_SHADOW", False),
                True,
                profile["units"],
            )
            if gate_enabled:
                try:
                    pair_mc = _get_mc_for_pair(pair)
                    gate = PostExitGate.evaluate(
                        instrument=pair,
                        action=action,
                        strength_score=candidate.get("strength_score", 0.0),
                        candidate_rank=1,
                        mc_regime_raw=(pair_mc or {}).get("regime", "NO_MC_DATA"),
                        baseline_units=profile["units"],
                        tracker=None,
                    )
                    if gate_shadow:
                        print(
                            f"  [POST-EXIT SHADOW] {pair}: {gate.reason_code} (shadow)"
                        )
                    else:
                        allow_open, effective_units = (
                            gate.is_allowed,
                            gate.effective_units,
                        )
                except Exception as exc:
                    print(f"  [POST-EXIT] Gate eval failed → fallback baseline: {exc}")
            if gate_enabled and not gate_shadow and not allow_open:
                print(f"🚫 POST-EXIT GATE REJECTED {pair}")
                continue

            report["candidate"] = f"{pair} {action}"
            allowed, idem_reason = _check_pair_level_strategy_position(pair, action)
            report["idempotency"] = "PASS" if allowed else "BLOCKED"
            if not allowed:
                report["entries_blocked"] += 1
                report["final_action"], report["reason"] = "BLOCKED", idem_reason
                report["next_plan"] = (
                    f"Current state: {pair} is occupied or could not be checked; wait for the next scheduled cycle."
                )
                if "same-direction" in idem_reason:
                    report["same_direction"] += 1
                elif "opposite-direction" in idem_reason:
                    report["opposite_direction"] += 1
                    # Opposite-direction pair-level idempotency detected -> close that pair immediately (normal bot behavior)
                    try:
                        print(
                            f"  [EXEC] Opposite-direction detected on {pair} ({idem_reason}) — closing existing position for this pair now."
                        )
                        ok, info = _close_pair_position_v144(
                            account_id=_trading_core.oanda_account_id, instrument=pair
                        )
                        if ok:
                            print(
                                f"  [EXEC] ✅ Closed existing position for {pair}: {info}"
                            )
                            report["final_action"] = "CLOSE_ONLY"
                            report["reason"] = (
                                f"Closed opposing position on {pair} due to new signal."
                            )
                            # If an emergency lock was present, clear it — this was a normal scan-driven close
                            try:
                                _clear_emergency_lock_v144()
                                print(
                                    "  [EXEC] Cleared emergency lock (normal scan-driven close)."
                                )
                            except Exception:
                                pass
                        else:
                            print(
                                f"  [EXEC] ❌ Failed to close existing position for {pair}: {info}"
                            )
                            report["final_action"] = "CLOSE_FAILED"
                            report["reason"] = (
                                f"Attempted to close opposing position on {pair} but failed."
                            )
                    except Exception as e:
                        print(
                            f"  [EXEC] ❌ Exception while closing opposing position for {pair}: {e}"
                        )
                elif "query failure" in idem_reason:
                    report["query_failures"] += 1
                # After closing (or attempting to), do not open a new trade in the same cycle
                continue

            print(
                f"\n  ✅ SIGNAL: {action} {pair}\n     Entry      : {candidate['entry']}\n     Stop Loss  : {candidate['stop_loss']}\n     Take Profit: {candidate['take_profit']}\n     R:R Ratio  : {candidate['risk_reward']:.2f}\n     Reason     : {candidate['reasoning']}"
            )
            if dry_run:
                print("\n  [DRY RUN] Signal validated — no order sent.")
                report["reason"] = (
                    "Dry-run prevents entry requests after existing qualification rules passed."
                )
                report["next_plan"] = (
                    f"Current state: {pair} {action} qualified in dry-run; await the next scheduled cycle."
                )
                continue

            strategy_tag = make_strategy_tag(pair, action)
            strategy_comment = make_strategy_comment(
                candidate["entry"], candidate["stop_loss"], candidate["take_profit"]
            )
            print(
                f"\n  → Sending order to OANDA...\n     Tag:     {strategy_tag}\n     Comment: {strategy_comment}"
            )
            candidate["tag"], candidate["comment"] = strategy_tag, strategy_comment
            # Prevent executing new entries if an emergency lock is active
            if emergency_lock_active:
                print(
                    f"  ⚠️ Emergency lock active — skipping new entry for {pair} {action} this cycle"
                )
                report["final_action"] = "BLOCKED_BY_EMERGENCY_LOCK"
                report["reason"] = (
                    "Emergency lock active; manual intervention required to resume trading."
                )
                report["next_plan"] = (
                    "Manual clear of emergency lock to resume entries."
                )
                continue

            if _trading_core.execute_market_trade(
                instrument=pair,
                action=action,
                units=effective_units,
                stop_loss=candidate["stop_loss"],
                take_profit=candidate["take_profit"],
                dry_run=dry_run,
                client_extensions=build_client_extensions(
                    candidate,
                    strategy_tag=strategy_tag,
                    bar_time=candidate.get("bar_time"),
                ),
            ):
                print("  ✅ Order submitted successfully")
                report["final_action"] = "ENTER"
                report["reason"] = (
                    "Existing strategy qualification rules passed and an entry request was submitted."
                )
                report["next_plan"] = (
                    f"Position: {pair} {action}; recalculate SL/TP every cycle and request changes only when the threshold is exceeded."
                )
            else:
                print("  ❌ Order NOT confirmed — check logs")
                report["reason"] = (
                    "Entry request was not confirmed by the existing execution path."
                )
    except Exception as exc:
        import traceback

        print(f"[CYCLE FAILED] {exc}")
        traceback.print_exc()
        report["reason"] = f"Cycle exception: {exc}"
    finally:
        _print_full_cycle_report(report, profile, dry_run)


if __name__ == "__main__":
    from utils.utils import apply_jitter

    apply_jitter(min_sec=1, max_sec=5)
    # _lock_fd = _acquire_profile_lock(_args.profile)
    print("=" * 60)
    print(f"JPY STRENGTH TRADING BOT — SCHEDULED RUNNER v{RUNNER_VERSION}")
    print("=" * 60)
    print(
        f"  Strategy : Trade top pair if ≥ {MIN_VALID_PAIRS_TO_TRADE} valid JPY crosses qualify"
    )
    print(
        f"  Risk     : Level {RISK_LEVEL} ({RISK_PROFILE[RISK_LEVEL]['units']:,} units per trade)  ← {_LOT_SOURCE}"
    )

    print(f"  Interval : Every {CHECK_INTERVAL_MINUTES} minutes (cron-driven)")
    print(f"  OANDA profile: #{_args.profile} ({_trading_core.oanda_account_id})")
    print(f"  Dry run: {'ENABLED' if _args.dry_run else 'disabled'}")
    print("  Runtime state: OANDA only (no local trade-state restore)")
    print(
        f"  MC Regime gating: {'ENABLED' if _config.MC_REGIME_ENABLED else 'disabled'}"
    )
    print(
        f"  PostExitGate: {'ENABLED' if POST_EXIT_GATE_ENABLED else 'disabled'} | mode: {'SHADOW' if POST_EXIT_GATE_SHADOW else 'LIVE'}"
    )
    print("  Tag+IdemMode: ENABLED (pair-level)")
    print("  SL/TP Guardian: ENABLED (repair + update)")
    print("=" * 60)
    print("[CONFIG] profile=#{0}".format(_args.profile))
    print("[CONFIG] environment={0}".format(_oanda_profile["env"]))
    print("[CONFIG] account={0}".format(_trading_core.oanda_account_id))
    print("[CONFIG] credentials_source=config_oanda")
    print("[CONFIG] client_owner=scheduled_runner")
    print("[CONFIG] transport_owner=TradingCoreV2")
    print("[CONFIG] execution_mode={0}".format("DRY-RUN" if _args.dry_run else "LIVE"))

    run_cycle()