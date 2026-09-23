"""
Scheduled Runner — JPY Strength Strategy
==========================================
v2 auditable execution enhancement:
    • 幂等防重复开仓: OANDA Tag+Comment 精准识别 → 策略+Pair+方向+日期
      不再一刀切查有无持仓; 区分策略单/手动单/历史单
    • SL/TP Guardian 增强: 缺则补 + 偏差则更新(阈值±0.2%), 每次运行必校验
    • Tag 格式: JPY-STRENGTH_{PAIR}_{SIDE}_{YYYYMMDD}
    • Comment: 入口价|SL|TP|版本 便于审计

v1.4 原有功能不变: MC Regime / PostExitGate / 进程锁 / 多账户 / Dry-Run
"""

import sys
# import time
import traceback
import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

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
    action="store_true",
    help="scan and read positions without opening, closing, or modifying orders",
)
_parser.add_argument(
    "--lots",
    type=int,
    default=None,
    help="Override position size (units). Falls back to run.env LIVE_LOT_SIZE/DEMO_LOT_SIZE, then config_bot defaults.",
)

_args, _ = _parser.parse_known_args()

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

from utils.trading_core_v2 import (
    TradingCore,
    close_pair_position,
    emergency_close_all_jpy,
)

_trading_core = TradingCore(
    oanda_client=_oanda_client,
    oanda_account_id=_account_id,
)

import config as _config
import config_bot as _config_bot

if _profile_name not in _config_bot.PROFILE_CFG:
    print(f"[PROFILE] ERROR: {_profile_name} is not defined in config_bot")
    sys.exit(1)
_PROFILE_CFG = _config_bot.load_profile(_profile_name)
_PROFILE_CFG["OANDA_ACCOUNT_ID"] = _account_id
for _key, _value in _PROFILE_CFG.items():
    setattr(_config, _key, _value)

# ========== 幂等 & SL/TP 增强配置 — 新增常量 ==========
RUNNER_VERSION = "2.0.0"
STRATEGY_TAG_PREFIX = "JPY-STRENGTH"
PRICE_PRECISION_TOL = 0.001
STRATEGY_UPDATE_THRESHOLD = 0.005
# =====================================================

from config import load_strategy_config

_RUN_CFG = load_strategy_config(_args.debug)
_config.ALIGNMENT_THRESHOLD = _RUN_CFG["ALIGNMENT_THRESHOLD"]
_config.STRENGTH_GAP_THRESHOLD = _RUN_CFG["STRENGTH_GAP_THRESHOLD"]
_config.MIN_STRENGTH_SCORE = _RUN_CFG["MIN_STRENGTH_SCORE"]
_config.REQUIRE_ALIGNED = _RUN_CFG["ALIGNMENT_THRESHOLD"]
if _args.debug is None:
    print(
        f"[CONFIG] STRICT defaults → ALIGN={_RUN_CFG['ALIGNMENT_THRESHOLD']}  GAP={_RUN_CFG['STRENGTH_GAP_THRESHOLD']}  MIN={_RUN_CFG['MIN_STRENGTH_SCORE']}"
    )

from config import (
    CHECK_INTERVAL_MINUTES,
    MIN_VALID_PAIRS_TO_TRADE,
    RISK_LEVEL,
    RISK_PROFILE,
    POST_EXIT_GATE_ENABLED,
    POST_EXIT_GATE_SHADOW,
    ALIGNMENT_THRESHOLD,
    DYNAMIC_RISK_TIMEFRAME,
    TP_RATIO,
    SL_RATIO,
    SL_PIPS,
    TP_PIPS,
)
import custom_strategy_v1 as _strategy

# ✅ ADD: 用 profile 配置重建 _active_strategy（注入 ATR 过滤参数）
_strategy._active_strategy = _strategy.JPYTrendStrategy(
    trade_pairs=_config.TRADE_PAIRS,
    enable_atr_min_filter=_PROFILE_CFG.get("ENABLE_ATR_MINIMUM_FILTER", True),
    atr_min_absolute=_PROFILE_CFG.get("ATR_MIN_ABSOLUTE", 0.060),
    atr_min_relative_pct=_PROFILE_CFG.get("ATR_MIN_RELATIVE_PCT", 0.045),
)

print("\n" + "=" * 60)
print("[STRATEGY DIAGNOSTICS] Runtime parameters")
print("=" * 60)
s = _strategy._active_strategy
print(f"  MIN_VALID_PAIRS      : {s.MIN_VALID_PAIRS}")
print(f"  MIN_DOMINANT_PAIRS   : {s.MIN_DOMINANT_PAIRS}")
print(f"  MIN_STRENGTH_PASS    : {s.MIN_STRENGTH_PASSING_PAIRS}")
print(f"  ALIGNMENT_THRESHOLD  : {s.TREND_ALIGNMENT_REQUIRED}/{len(_strategy.SIGNAL_TIMEFRAMES)} timeframes")
print(f"  STRENGTH_CUTOFF_RATIO: {s.STRENGTH_CUTOFF_RATIO} (dynamic = max_gap × ratio)")
print(f"  MIN_STRENGTH_SCORE   : ±{s.MIN_STRENGTH_SCORE}")
print(f"  MIN_MARKET_STRENGTH  : {_config.MIN_MARKET_STRENGTH}")
print(f"  ENABLE_ATR_MIN_FILTER: {s.ENABLE_ATR_MIN_FILTER}")
if s.ENABLE_ATR_MIN_FILTER:
    print(f"  ATR_MIN_ABSOLUTE     : {s.ATR_MIN_ABSOLUTE}")
    print(f"  ATR_MIN_RELATIVE%    : {s.ATR_MIN_RELATIVE_PCT}%")
print(f"  ENABLE_ATR_SLTP      : {_config.ENABLE_ATR_SLTP}")
print(f"  MIN_RR               : {s.MIN_RR}")
print(f"  ENABLE_MACRO_PROTEC  : {_config.ENABLE_MACRO_PROTECTION}")
print(f"  SKIP_SIDEWAYS_PAIRS  : {s.SKIP_SIDEWAYS_PAIRS}")
print(f"  TRADE_TOP_PAIRS      : {s.TRADE_TOP_PAIRS}")
print(f"  TRADE_PAIRS          : {s.trade_pairs}")
print("=" * 60 + "\n")

from custom_strategy_v1 import analyze_custom_strategy, get_last_signal, build_strength_matrix
from utils.strategy_helpers import check_ma5_alignment
from utils.oanda_state import build_client_extensions
from retry import with_retry
from get_mc_data import get_mc_data
from utils.post_exit_gate import PostExitGate
from utils.logging_utils import get_logger
import json
import os
from pathlib import Path
import time
from utils.utils import (
    acquire_profile_lock,
    audit_side,
    check_pair_level_strategy_position,
    clear_emergency_lock,
    confirmation_result,
    compute_all_cross_strengths,
    format_cross_strength_ranking,
    is_emergency_lock_active,
    is_strategy_trade,
    make_strategy_comment,
    make_strategy_tag,
    set_emergency_lock,
    sltp_decision,
)

from config_bot import (
    DEMO_LOT_SIZE as _CFG_BOT_DEMO_LOT,
    LIVE_LOT_SIZE as _CFG_BOT_LIVE_LOT,
)


def _resolve_effective_lots() -> tuple[int, str]:
    is_live = os.environ.get("OANDA_ENV", "practice").lower() in ("live", "real")
    if _args.lots is not None:
        return _args.lots, f"CLI --lots={_args.lots}"
    env_key = "LIVE_LOT_SIZE" if is_live else "DEMO_LOT_SIZE"
    env_val = os.getenv(env_key)
    if env_val and env_val.strip():
        try:
            return int(env_val), f"run.env {env_key}={env_val}"
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
PROJECT_ROOT = Path(__file__).resolve().parent
EMERGENCY_LOCK_FILE = PROJECT_ROOT / ".emergency_close_lock_v2"


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
        if not is_strategy_trade(trade, STRATEGY_TAG_PREFIX):
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
        sl_decision, _ = sltp_decision(
            current_sl, calculated_sl, PRICE_PRECISION_TOL, STRATEGY_UPDATE_THRESHOLD
        )
        tp_decision, _ = sltp_decision(
            current_tp, calculated_tp, PRICE_PRECISION_TOL, STRATEGY_UPDATE_THRESHOLD
        )

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
                        sl_result, sl_id = confirmation_result(
                            final_sl, calculated_sl, instrument
                        )
                        tp_result, tp_id = confirmation_result(
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

        audit_side(
            "SL", current_sl, calculated_sl, sl_decision, sl_request, sl_result, sl_id
        )
        audit_side(
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
    emergency_lock_active = is_emergency_lock_active(EMERGENCY_LOCK_FILE)
    if emergency_lock_active:
        print(
            "  ⚠️ Emergency lock active (v2) — will prevent NEW entries this cycle but will still process exit signals"
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
                    ma_align = check_ma5_alignment(instr, require_aligned=2, verbose=False)
                except Exception as _e:
                    ma_align = None
                if ma_align and ma_align != current_side:
                    print(
                        f"  [EARLY-EXIT] {instr}: position {current_side} vs MA align {ma_align} → closing now"
                    )
                    ok, info = close_pair_position(_trading_core, instr)
                    if ok:
                        print(f"  [EARLY-EXIT] ✅ Closed {instr}: {info}")
                    else:
                        print(f"  [EARLY-EXIT] ❌ Failed to close {instr}: {info}")
    except Exception as e:
        print(f"  [EARLY-EXIT] scan error (non-fatal): {e}")

    # ===== STRENGTH INVALIDATION EXIT: close open trades whose thesis gap reversed/eroded =====
    try:
        if not dry_run and getattr(_config, "ENABLE_STRATEGY_INVALIDATION_CLOSE", True):
            _gap_thresh = getattr(_config, "STRATEGY_INVALIDATION_MIN_GAP", 0.2)
            print(
                "\n  [STRENGTH-INVALIDATION] Scanning open JPY trades for thesis erosion..."
            )
            try:
                _matrix = build_strength_matrix()
            except Exception as _e:
                print(
                    f"  [STRENGTH-INVALIDATION] matrix build failed (non-fatal): {_e}"
                )
                _matrix = None

            if _matrix:
                _jpy_score = _matrix.get("JPY", 0.0)
                for trade in _trading_core.get_all_open_trades():
                    instr = trade.get("instrument")
                    if not instr or not instr.endswith("_JPY"):
                        continue
                    current_units = float(trade.get("currentUnits", 0))
                    if current_units == 0:
                        continue
                    current_side = "BUY" if current_units > 0 else "SELL"
                    base_ccy = instr.split("_")[0]
                    base_score = _matrix.get(base_ccy, 0.0)
                    gap = base_score - _jpy_score

                    thesis_intact = (
                        (current_side == "BUY" and gap > 0)
                        or (current_side == "SELL" and gap < 0)
                    )

                    if not thesis_intact:
                        print(
                            f"  [SI-EXIT] {instr}: {current_side} but gap={gap:+.4f} "
                            f"(base={base_score:+.4f} JPY={_jpy_score:+.4f}) → REVERSED → closing"
                        )
                        ok, info = close_pair_position(_trading_core, instr)
                        if ok:
                            print(f"  [SI-EXIT] ✅ Closed {instr}: {info}")
                        else:
                            print(f"  [SI-EXIT] ❌ Failed {instr}: {info}")
                    elif abs(gap) < _gap_thresh:
                        print(
                            f"  [SI-EXIT] {instr}: {current_side} but |gap|={abs(gap):.4f} "
                            f"< {_gap_thresh} → TOO THIN → closing"
                        )
                        ok, info = close_pair_position(_trading_core, instr)
                        if ok:
                            print(f"  [SI-EXIT] ✅ Closed {instr}: {info}")
                        else:
                            print(f"  [SI-EXIT] ❌ Failed {instr}: {info}")
    except Exception as e:
        print(f"  [STRENGTH-INVALIDATION] scan error (non-fatal): {e}")

    # ===== FULL CURRENCY CROSS STRENGTH SCAN (independent, display-only) =====
    try:
        _cs_scores = build_strength_matrix()
        _cs_pairs = compute_all_cross_strengths(_cs_scores)
        _cs_threshold = getattr(_config, "CROSS_STRENGTH_EXTREME_THRESHOLD", 2.0)
        print(format_cross_strength_ranking(_cs_pairs, threshold=_cs_threshold))
    except Exception as _e:
        print(f"  [CROSS-STRENGTH] scan skipped (non-fatal): {_e}")

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

        candidates = [signal_data]
        if mode in ("aggressive", "normal") and _config.ENABLE_MC_BASKET_EXECUTION:
            try:
                top_n = _strategy.get_top_signals(n=max_positions) or [signal_data]
            except Exception as exc:
                top_n = [signal_data]
                print(f"  [MC REGIME] top{max_positions} failed → fallback top1: {exc}")
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
                    # Direction conflict detected across candidates → emergency flatten of all JPY positions
                    try:
                        print(
                            "  [EXEC][EMERGENCY] Direction conflict detected — invoking emergency_close_all_jpy()"
                        )
                        emergency_close_all_jpy(
                            _trading_core,
                            env=_oanda_profile["env"],
                            lock_setter=lambda info: set_emergency_lock(
                                EMERGENCY_LOCK_FILE, info
                            ),
                        )
                    except Exception as e:
                        print(f"  [EXEC][EMERGENCY] Failed to run emergency close: {e}")
            candidates = compatible
            print(
                f"  [MC REGIME] Candidate pool: {[candidate['pair'] for candidate in candidates]}"
            )
        elif not _config.ENABLE_MC_BASKET_EXECUTION:
            print(f"  [MC REGIME] BASKET DISABLED → top1 only: {signal_data['pair']}")

        new_entries_this_cycle = 0
        _report_locked = False

        for candidate in sorted(
            candidates,
            key=lambda value: abs(value.get("strength_score", 0.0)),
            reverse=True,
        ):
            if _report_locked:
                break
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

            allowed, idem_reason = check_pair_level_strategy_position(
                _trading_core, pair, action, STRATEGY_TAG_PREFIX
            )
            if not allowed:
                report["entries_blocked"] += 1
                report["candidate"] = f"{pair} {action}"
                report["idempotency"] = "BLOCKED"
                report["final_action"], report["reason"] = "BLOCKED", idem_reason
                report["next_plan"] = (
                    f"Current state: {pair} is occupied or could not be checked; wait for the next scheduled cycle."
                )
                if "same-direction" in idem_reason:
                    report["same_direction"] += 1
                    continue
                if "opposite-direction" in idem_reason:
                    report["opposite_direction"] += 1
                    try:
                        print(
                            f"  [EXEC] Opposite-direction detected on {pair} ({idem_reason}) — closing existing position for this pair now."
                        )
                        ok, info = close_pair_position(_trading_core, pair)
                        if ok:
                            print(
                                f"  [EXEC] ✅ Closed existing position for {pair}: {info}"
                            )
                            report["final_action"] = "CLOSE_ONLY"
                            report["reason"] = (
                                f"Closed opposing position on {pair} due to new signal."
                            )
                            try:
                                clear_emergency_lock(EMERGENCY_LOCK_FILE)
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
                _report_locked = True
                continue

            report["candidate"] = f"{pair} {action}"
            report["idempotency"] = "PASS"

            print(
                f"\n  ✅ SIGNAL: {action} {pair}\n     Entry      : {candidate['entry']}\n     Stop Loss  : {candidate['stop_loss']}\n     Take Profit: {candidate['take_profit']}\n     R:R Ratio  : {candidate['risk_reward']:.2f}\n     Reason     : {candidate['reasoning']}"
            )
            if dry_run:
                print("\n  [DRY RUN] Signal validated — no order sent.")
                report["final_action"] = "HOLD"
                report["reason"] = (
                    "Dry-run prevents entry requests after existing qualification rules passed."
                )
                report["next_plan"] = (
                    f"Current state: {pair} {action} qualified in dry-run; await the next scheduled cycle."
                )
                _report_locked = True
                break

            strategy_tag = make_strategy_tag(pair, action, STRATEGY_TAG_PREFIX)
            strategy_comment = make_strategy_comment(
                candidate["entry"], candidate["stop_loss"], candidate["take_profit"], RUNNER_VERSION
            )
            print(
                f"\n  → Sending order to OANDA...\n     Tag:     {strategy_tag}\n     Comment: {strategy_comment}"
            )
            candidate["tag"], candidate["comment"] = strategy_tag, strategy_comment
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
                _report_locked = True
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
                new_entries_this_cycle += 1
                max_per_cycle = getattr(_config, "MAX_NEW_ENTRIES_PER_CYCLE", 1)
                if new_entries_this_cycle >= max_per_cycle:
                    _report_locked = True
                    print(
                        f"  [CYCLE CAP] new_entries_this_cycle={new_entries_this_cycle}/{max_per_cycle} — stopping basket loop."
                    )
                    break
            else:
                print("  ❌ Order NOT confirmed — check logs")
                report["reason"] = (
                    "Entry request was not confirmed by the existing execution path."
                )
                _report_locked = True

    except Exception as exc:
        print(f"[CYCLE FAILED] {exc}")
        traceback.print_exc()
        report["reason"] = f"Cycle exception: {exc}"
    finally:
        _print_full_cycle_report(report, profile, dry_run)


if __name__ == "__main__":
    from utils.utils import apply_jitter

    apply_jitter(min_sec=1, max_sec=5)
    # _lock_fd = acquire_profile_lock(_args.profile)
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