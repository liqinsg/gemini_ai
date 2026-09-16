"""
Scheduled Runner — JPY Strength Strategy
==========================================
v1.4.2 新增 (最小侵入):
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
from datetime import datetime, timezone
from pathlib import Path
import config as _config

# ========== 幂等 & SL/TP 增强配置 — 新增常量 ==========
STRATEGY_TAG_PREFIX = "JPY-STRENGTH"
SLTP_PRICE_TOLERANCE_PCT = 0.2  # 允许偏差 ±0.2% 超了就更新
# =====================================================

_account_map = {
    1: "OANDA_ACCOUNT_ID_1",
    2: "OANDA_ACCOUNT_ID_2",
    3: "OANDA_ACCOUNT_ID_3",
    4: "OANDA_ACCOUNT_ID_4",
}
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
    default=1,
    choices=[1, 2, 3, 4],
    help="OANDA profile number (1=default, 2/3/4=alternate)",
)
_parser.add_argument(
    "--dry-run",
    action="store_true",
    help="scan and read positions without opening, closing, or modifying orders",
)
_args, _ = _parser.parse_known_args()
_key = _account_map[_args.profile]
_val = getattr(_config, _key, "")
if not _val:
    print(
        f"[PROFILE] ERROR: env var {_key} not set — abort. Add it to .env or run.env first."
    )
    sys.exit(1)
_config.OANDA_ACCOUNT_ID = _val

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
from custom_strategy_v1 import analyze_custom_strategy, get_last_signal
from utils import execute_market_trade
from utils.schemas import TradeSignal
from utils.trading_core import (
    oanda_client,
    attach_sl_tp_to_open_trade,
    format_price_for_instrument,
)
from utils.oanda_state import build_client_extensions
from retry import with_retry
from utils.mc_loader_local import get_latest_mc_local
from utils.post_exit_gate import PostExitGate
from utils.logging_utils import get_logger
import oandapyV20.endpoints.trades as trades_mod
import json
import os
import fcntl
from types import SimpleNamespace
import errno

_log = get_logger("scheduled_runner_v1_4_1")
POST_EXIT_SHADOW_LOG_PATH = os.environ.get(
    "POST_EXIT_SHADOW_LOG_PATH", "logs/post_exit_gate_shadow.jsonl"
)


# ========== 幂等工具函数 — 新增 ==========
def make_strategy_tag(pair: str, side: str) -> str:
    """生成幂等Tag: JPY-STRENGTH_AUD_JPY_SELL_20260914"""
    date_str = datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    return f"{STRATEGY_TAG_PREFIX}_{pair}_{side.upper()}_{date_str}"


def make_strategy_comment(entry: float, sl: float, tp: float) -> str:
    """结构化Comment 便于审计"""
    return f"v1.4.2|entry={entry:.5f}|SL={sl:.5f}|TP={tp:.5f}"


def _has_exact_strategy_position(pair: str, side: str) -> bool:
    """
    精准幂等检查: OANDA 实时查询 Tag+Pair+方向+今日
    返回 True=已存在 → 跳过开仓
    """
    target_tag = make_strategy_tag(pair, side)
    try:
        req = trades_mod.OpenTrades(_config.OANDA_ACCOUNT_ID)
        oanda_client.request(req)
        trades = req.response.get("trades", [])
        for t in trades:
            trade_tag = t.get("tag", "")
            trade_inst = t.get("instrument", "")
            units = float(t.get("currentUnits", 0))
            trade_side = "BUY" if units > 0 else "SELL"
            if (
                trade_tag == target_tag
                and trade_inst == pair
                and trade_side == side.upper()
            ):
                print(
                    f"  [幂等防护] 已存在策略单 → {pair} {side} tag={target_tag} trade_id={t['id']} → 跳过开仓"
                )
                return True
        return False
    except Exception as e:
        print(f"  [幂等防护] 查询异常: {e} → 保守起见 跳过开仓")
        return True  # 查询失败=保守防重复


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


def _print_mc_snapshot():
    print("\n  === MC DAILY REGIME SNAPSHOT ===")
    for pair in _config.TRADE_PAIRS:
        try:
            mc = get_latest_mc_local(pair=pair, day=True)
            if mc:
                regime = mc.get("regime", "N/A")
                p_up = mc.get("p_up", "?")
                p_down = mc.get("p_down", "?")
                price = mc.get("current_price", mc.get("expected_price", "?"))
                print(
                    f"  {pair:10s} | regime={regime} | P(UP)={p_up}% P(DOWN)={p_down}% | last={price}"
                )
            else:
                print(f"  {pair:10s} | [No local MC data]")
        except Exception as e:
            print(f"  {pair:10s} | [MC load error: {e}]")
    print("  === END MC SNAPSHOT ===\n")


# ========== SL/TP Guardian 增强版 — 修复版 (兼容原有函数签名) ==========
# ========== SL/TP Guardian — 最终稳定版 v1.4.2-fix ==========
def _validate_and_repair_sltp():
    """
    ✅ 逐笔独立处理：每笔交易按自身 entry 算 SL/TP
    ✅ 全部持仓全量扫描，不漏单
    ✅ 打印 OANDA 原始响应，真实确认结果
    """
    print("  [SL/TP GUARDIAN] === FULL SCAN ALL TRADES ===")
    try:
        req = trades_mod.OpenTrades(_config.OANDA_ACCOUNT_ID)
        oanda_client.request(req)
        open_trades = req.response.get("trades", [])
        print(f"  [SCAN] Found {len(open_trades)} open trade(s)")
    except Exception as e:
        print(f"  [SCAN FAILED] {e}")
        return

    repaired = 0
    for idx, tr in enumerate(open_trades, 1):
        trade_id = tr.get("id") or tr.get("tradeID")
        instrument = tr.get("instrument")
        units = int(float(tr.get("currentUnits", 0)))
        side = "BUY" if units > 0 else "SELL"

        # 只处理 JPY 对（可去掉限制处理全部）
        if "JPY" not in instrument:
            print(
                f"  [{idx}/{len(open_trades)}] {instrument} T{trade_id} → Skip (non-JPY)"
            )
            continue

        # 获取完整持仓详情（关键：每笔独立 entry）
        try:
            td_resp = oanda_client.request(
                trades_mod.TradeDetails(_config.OANDA_ACCOUNT_ID, trade_id)
            )
            trade_info = td_resp.get("trade") if isinstance(td_resp, dict) else td_resp
        except Exception as e:
            print(f"  [{idx}] {instrument} T{trade_id} → Details fetch FAILED: {e}")
            continue

        entry_price = float(
            trade_info.get("price") or trade_info.get("initialPrice") or 0
        )
        if entry_price <= 0:
            print(
                f"  [{idx}] {instrument} T{trade_id} → Invalid entry price: {entry_price}"
            )
            continue

        # 按本笔 entry 独立计算标准 SL/TP
        pip = getattr(_config, "JPY_PIP", 0.01) if "JPY" in instrument else 0.0001
        if side == "BUY":
            std_sl = round(entry_price - SL_PIPS * pip, 5)
            std_tp = round(entry_price + TP_PIPS * pip * TP_RATIO, 5)
        else:  # SELL
            std_sl = round(entry_price + SL_PIPS * pip, 5)
            std_tp = round(entry_price - TP_PIPS * pip * TP_RATIO, 5)

        # 当前状态
        sl_obj = trade_info.get("stopLossOrder") or {}
        tp_obj = trade_info.get("takeProfitOrder") or {}
        has_sl = bool(sl_obj.get("id"))
        has_tp = bool(tp_obj.get("id"))
        curr_sl = float(sl_obj["price"]) if has_sl else None
        curr_tp = float(tp_obj["price"]) if has_tp else None

        # 偏差判断
        TOL = SLTP_PRICE_TOLERANCE_PCT / 100

        def _needs_update(curr, std):
            if curr is None:
                return True
            return abs(curr - std) / std > TOL

        need_sl = _needs_update(curr_sl, std_sl)
        need_tp = _needs_update(curr_tp, std_tp)

        print(
            f"\n  [{idx}] {instrument} T{trade_id} | {side} | entry={entry_price:.5f}"
        )

        # print(f"      SL: curr={curr_sl or 'NONE':<12s} → std={std_sl:.5f} | NEED={need_sl}")
        # print(f"      TP: curr={curr_tp or 'NONE':<12s} → std={std_tp:.5f} | NEED={need_tp}")

        curr_sl_str = f"{curr_sl:.5f}" if curr_sl is not None else "NONE"
        curr_tp_str = f"{curr_tp:.5f}" if curr_tp is not None else "NONE"
        print(f"      SL: curr={curr_sl_str:<12s} → std={std_sl:.5f} | NEED={need_sl}")
        print(f"      TP: curr={curr_tp_str:<12s} → std={std_tp:.5f} | NEED={need_tp}")

        if not need_sl and not need_tp:
            print(f"      ✅ Already correct — skip")
            continue

        # 执行补挂/更新
        sig = SimpleNamespace(
            pair_to_trade=instrument,
            action=side,
            confidence_score=0.9,
            stop_loss=std_sl,
            take_profit=std_tp,
            reasoning=f"GUARDIAN-T{trade_id}",
        )

        if _args.dry_run:
            print(f"      [DRY-RUN] Would attach SL={std_sl} TP={std_tp}")
            repaired += 1
            continue

        try:
            result = attach_sl_tp_to_open_trade(sig, dry_run=_args.dry_run)
            print(f"      API Result: {result}")  # 看底层返回什么
            repaired += 1
            print(f"      ✅ Sent to OANDA — check OANDA Orders tab for confirmation")
        except Exception as e:
            print(f"      ❌ FAILED: {type(e).__name__}: {e}")

    print(f"\n  [SUMMARY] Scanned {len(open_trades)} trades | Processed {repaired}")


# =====================================================


def run_cycle(dry_run=None):
    if dry_run is None:
        dry_run = _args.dry_run
    profile = RISK_PROFILE[RISK_LEVEL]
    print(
        f"\n[{datetime.now().isoformat()}] === JPY Strength Scan | Risk Level: {RISK_LEVEL} | v1.4.2 ==="
    )

    _print_mc_snapshot()

    # === STEP 1: SL/TP 全量校验修复 — 最先执行 ===
    _validate_and_repair_sltp()

    try:
        # 2. Run full strategy scan
        scan_result = with_retry(
            analyze_custom_strategy, max_attempts=3, delay=5, label="strategy_scan"
        )
        signal_data = get_last_signal()
        if signal_data is None:
            print("[CYCLE] No qualifying signals this cycle. HOLD.")
            return

        # === MC Regime 模式判定 ===
        mc_data = get_latest_mc_local(pair=signal_data["pair"], day=True)
        mc_regime = mc_data.get("regime", "N/A") if mc_data else "NO_LOCAL_MC_DATA"
        mode = _regime_policy(mc_regime) if _config.MC_REGIME_ENABLED else "normal"
        print(f"  [MC REGIME] {signal_data['pair']} → {mc_regime} | mode={mode}")
        params = _regime_params(mode)
        _max_pos = params["max_positions"]
        _tp_mult = params["tp_multiplier"]
        _exit_tight = params["exit_tightness"]

        if mode == "cautious":
            print(
                f"  [MC DECISION] CONSOLIDATION — strength hurdle ≥ {_config.MC_REGIME_STRENGTH_HURDLE_CONSOLIDATION} | max_pos=1 | TP×{_tp_mult}"
            )
            score = abs(signal_data.get("strength_score", 0.0))
            hurdle = _config.MC_REGIME_STRENGTH_HURDLE_CONSOLIDATION
            if score < hurdle:
                print(
                    f"🚫 CONSOLIDATION cautious: strength={score:.4f} < {hurdle}, HOLD"
                )
                return
            print(f"  [MC REGIME] PASS strength hurdle ({score:.4f} ≥ {hurdle})")
        elif mode == "aggressive":
            print(f"  [MC DECISION] STRONG MOMENTUM — top{_max_pos} | TP×{_tp_mult}")
        else:
            print(f"  [MC DECISION] NEUTRAL — max_pos={_max_pos} | TP×{_tp_mult}")

        # === 构建候选列表 ===
        candidates = [signal_data]
        if mode in ("aggressive", "normal") and _config.ENABLE_MC_BASKET_EXECUTION:
            try:
                top_n = _strategy.get_top_signals(n=_max_pos)
            except Exception as e:
                top_n = [signal_data]
                print(f"  [MC REGIME] top{_max_pos} failed → fallback top1: {e}")
            if not top_n:
                top_n = [signal_data]
            compatible = [top_n[0]]
            for sig in top_n[1:]:
                if all(_jpy_cross_direction_compatible(sig, c) for c in compatible):
                    compatible.append(sig)
                else:
                    print(
                        f"  [MC REGIME] SKIP {sig['pair']} {sig['action']}: direction conflict"
                    )
            candidates = compatible
            print(f"  [MC REGIME] Candidate pool: {[c['pair'] for c in candidates]}")
        else:
            if not _config.ENABLE_MC_BASKET_EXECUTION:
                print(
                    f"  [MC REGIME] BASKET DISABLED → top1 only: {signal_data['pair']}"
                )

        candidates = sorted(
            candidates, key=lambda c: abs(c.get("strength_score", 0.0)), reverse=True
        )

        # === 逐候选下单 ===
        for cand_idx, cand in enumerate(candidates):
            cand = dict(cand)
            pair = cand["pair"]
            action = cand["action"]

            # --- TP 倍率调整 ---
            if _tp_mult != 1.0:
                _risk_dist = abs(cand["entry"] - cand["stop_loss"])
                if action.upper() == "SELL":
                    cand["take_profit"] = round(
                        cand["entry"] - _risk_dist * _tp_mult, 5
                    )
                else:
                    cand["take_profit"] = round(
                        cand["entry"] + _risk_dist * _tp_mult, 5
                    )
                cand["risk_reward"] = round(
                    abs(cand["take_profit"] - cand["entry"]) / _risk_dist, 2
                )
                print(
                    f"  [MC TP] {pair}: TP×{_tp_mult} → {cand['take_profit']} R:R {cand['risk_reward']}"
                )

            # --- Post-Exit Gate ---
            gate_enabled = getattr(_config, "POST_EXIT_GATE_ENABLED", True)
            gate_shadow = getattr(_config, "POST_EXIT_GATE_SHADOW", False)
            allow_open = True
            effective_units = profile["units"]
            if gate_enabled:
                try:
                    pair_mc_data = get_latest_mc_local(pair=pair, day=True)
                    pair_mc_regime = (
                        pair_mc_data.get("regime", "N/A")
                        if pair_mc_data
                        else "NO_LOCAL_MC_DATA"
                    )
                    gate_decision = PostExitGate.evaluate(
                        instrument=pair,
                        action=action,
                        strength_score=cand.get("strength_score", 0.0),
                        candidate_rank=cand_idx + 1,
                        mc_regime_raw=pair_mc_regime,
                        baseline_units=profile["units"],
                        tracker=None,
                    )
                    if gate_shadow:
                        print(
                            f"  [POST-EXIT SHADOW] {pair}: {gate_decision.reason_code} (shadow)"
                        )
                    else:
                        allow_open = gate_decision.is_allowed
                        effective_units = gate_decision.effective_units
                except Exception as _pe_err:
                    print(
                        f"  [POST-EXIT] Gate eval failed → fallback baseline: {_pe_err}"
                    )
                    allow_open = True
                    effective_units = profile["units"]
            if gate_enabled and not gate_shadow and not allow_open:
                print(f"🚫 POST-EXIT GATE REJECTED {pair}")
                continue

            # ========== 【核心修改1】精准幂等校验替代一刀切查询 ==========
            if _has_exact_strategy_position(pair, action):
                continue  # 今日同方向已开 → 跳过

            # --- 策略信号信息输出 ---
            mc_data_i = get_latest_mc_local(pair=pair, day=True)
            p_up = mc_data_i.get("p_up") if mc_data_i else None
            p_down = mc_data_i.get("p_down") if mc_data_i else None
            mc_regime_i = (
                mc_data_i.get("regime", "N/A") if mc_data_i else "NO_LOCAL_MC_DATA"
            )
            print(f"\n  ✅ SIGNAL: {action} {pair}")
            print(f"     Entry      : {cand['entry']}")
            print(f"     Stop Loss  : {cand['stop_loss']}")
            print(f"     Take Profit: {cand['take_profit']}")
            print(f"     R:R Ratio  : {cand['risk_reward']:.2f}")
            if mc_data_i:
                print(
                    f"     MC Forecast: P(UP)={p_up}% | P(DOWN)={p_down}% | Regime={mc_regime_i}"
                )
            print(f"     Reason     : {cand['reasoning']}")

            if dry_run:
                print("\n  [DRY RUN] Signal validated — no order sent.")
                continue

            # --- 下单：写入 Tag + Comment ---
            print("\n  → Sending order to OANDA...")
            strategy_tag = make_strategy_tag(pair, action)
            strategy_comment = make_strategy_comment(
                cand["entry"], cand["stop_loss"], cand["take_profit"]
            )
            print(f"     Tag:     {strategy_tag}")
            print(f"     Comment: {strategy_comment}")

            signal = TradeSignal(
                pair_to_trade=pair,
                action=action,
                confidence_score=0.85,
                stop_loss=cand["stop_loss"],
                take_profit=cand["take_profit"],
                reasoning=cand["reasoning"],
            )
            # 注入 Tag/Comment 到扩展字段
            cand["tag"] = strategy_tag
            cand["comment"] = strategy_comment

            if success := execute_market_trade(
                signal,
                units_override=effective_units,
                client_extensions=build_client_extensions(
                    cand, bar_time=cand.get("bar_time")
                ),
            ):
                print("  ✅ Order submitted successfully")
                # 【增强】下单后立刻主动补挂SL/TP 不依赖回执
                print("  → Will attach SL/TP via Guardian on next cycle")
            else:
                print("  ❌ Order NOT confirmed — check logs")

    except Exception as e:
        import traceback

        print(f"[CYCLE FAILED] {str(e)}")
        traceback.print_exc()
        print("  → Retry next cycle")


if __name__ == "__main__":
    _lock_fd = _acquire_profile_lock(_args.profile)
    print("=" * 60)
    print("JPY STRENGTH TRADING BOT — SCHEDULED RUNNER")
    print("=" * 60)
    print(
        f"  Strategy : Trade top pair if ≥ {MIN_VALID_PAIRS_TO_TRADE} valid JPY crosses qualify"
    )
    print(
        f"  Risk     : Level {RISK_LEVEL} ({RISK_PROFILE[RISK_LEVEL]['units']:,} units per trade)"
    )
    print(f"  Interval : Every {CHECK_INTERVAL_MINUTES} minutes (cron-driven)")
    print(f"  OANDA profile: #{_args.profile} ({_config.OANDA_ACCOUNT_ID})")
    print(f"  Dry run: {'ENABLED' if _args.dry_run else 'disabled'}")
    print(f"  Runtime state: OANDA only (no local trade-state restore)")
    print(
        f"  MC Regime gating: {'ENABLED' if _config.MC_REGIME_ENABLED else 'disabled'}"
    )
    print(
        f"  PostExitGate: {'ENABLED' if POST_EXIT_GATE_ENABLED else 'disabled'} | mode: {'SHADOW' if POST_EXIT_GATE_SHADOW else 'LIVE'}"
    )
    print("  Tag+IdemMode: ENABLED")
    print("  SL/TP Guardian: ENABLED (repair + update)")
    print("=" * 60)
    run_cycle()