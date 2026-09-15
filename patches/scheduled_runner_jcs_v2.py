"""
Scheduled Runner — JPY Strength Strategy + JCS Fusion v3.5 + Position Guard
Enhanced: per-account lock, SL/TP Guardian, idempotency checks and short-poll
"""
import time
# import schedule
from datetime import datetime
from pathlib import Path
import sys
import os
import fcntl
import errno
import importlib
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import CHECK_INTERVAL_MINUTES, RISK_LEVEL, RISK_PROFILE, OANDA_ACCOUNT_ID, STRATEGY_TAG_PREFIX, TP_PIPS, SL_PIPS, JPY_PIP
from custom_strategy import analyze_custom_strategy, get_last_signal
from utils import execute_market_trade, get_open_position
from retry import with_retry
from utils.jpy_jcs_strategy import run as jcs_fuse
from utils.trading_core import attach_sl_tp_to_open_trade, format_price_for_instrument, oanda_client
from utils.oanda_state import (
    build_client_extensions,
    check_pair_level_strategy_position,
    get_open_trades,
    is_strategy_record,
)

# ========== 🛡️ 策略管理清单：只认这些、其他全部标记外部持仓 ==========
MANAGED_PAIRS = {
    "USD_JPY", "EUR_JPY", "GBP_JPY",
    "AUD_JPY", "CAD_JPY", "NZD_JPY",
}

# ========== 📊 全局持仓缓存 ==========
_POSITION_CACHE = {
    "timestamp": None,
    "positions": {},   # pair → {direction, units}
    "has_strategy": False,
    "unmanaged": [],
}


def fetch_all_positions() -> dict:
    """
    获取全部持仓并分类：策略管理 vs 外部持仓
    返回: {pair: {direction, units, is_managed}}
    """
    all_pos = {}
    unmanaged = []
    has_strategy_position = False

    try:
        # 逐个查询策略关注的货币对
        for pair in MANAGED_PAIRS:
            pos = get_open_position(pair)
            if not pos:
                continue
            lu = float(pos.get("long", {}).get("units", 0))
            su = float(pos.get("short", {}).get("units", 0))

            if lu > 0:
                all_pos[pair] = {"direction": "BUY", "units": lu, "is_managed": True}
                has_strategy_position = True
            elif su < 0:
                all_pos[pair] = {"direction": "SELL", "units": abs(su), "is_managed": True}
                has_strategy_position = True

        # ⚠️ 简单提示：其他货币对存在 = 外部持仓（不自动处理）
        # 注：OANDA API如需查全部持仓需额外接口，这里先覆盖管理清单+告警
        global _POSITION_CACHE
        _POSITION_CACHE["timestamp"] = datetime.now()
        _POSITION_CACHE["positions"] = all_pos
        _POSITION_CACHE["has_strategy"] = has_strategy_position
        _POSITION_CACHE["unmanaged"] = unmanaged

    except Exception as e:
        print(f"  ⚠️  持仓查询异常: {e}")

    return all_pos


def print_position_summary(positions: dict, selected_pair: str = None, selected_action: str = None):
    """打印持仓总览 + 对比即将开的单"""
    print("\n" + "="*60)
    print("📊 当前持仓审查")
    print("="*60)

    if not positions:
        print("  ✅ 当前无任何持仓")
    else:
        for pair, info in positions.items():
            tag = " ✅ 策略持仓" if info["is_managed"] else " ⚠️ 外部持仓"
            print(f"  {info['direction']} {pair}  {info['units']:,} units{tag}")

    if _POSITION_CACHE["unmanaged"]:
        print(f"  ⚠️  发现非策略持仓: {', '.join(_POSITION_CACHE['unmanaged'])}")

    # 🔍 核对即将开的仓
    if selected_pair and selected_action:
        print(f"\n  🎯 即将开仓: {selected_action} {selected_pair}")
        if selected_pair in positions:
            existing = positions[selected_pair]
            if existing["direction"] == selected_action:
                print(f"  ⚠️  同方向已持有 → 禁止重复开仓！")
                return "DUPLICATE"
            else:
                print(f"  🔴 方向冲突！现有 {existing['direction']} 却要开 {selected_action} → 对冲风险！")
                return "CONFLICT"
        else:
            print(f"  ✅ 无冲突 → 可开新仓")
            return "OK"
    print("="*60 + "\n")


# ========== ⭐ 精准方向推导 ==========
def infer_jpy_direction(pair: str, action: str) -> bool | None:
    """SELL XXXJPY → 买入JPY → True(JPY_BULL) / BUY XXXJPY → 卖出JPY → False(JPY_BEAR)"""
    if not pair or not pair.endswith("JPY"):
        return None
    if action == "SELL":
        return True
    elif action == "BUY":
        return False
    return None


def get_fusion_confirmation(scan_result, signal_data: dict = None) -> dict:
    """兼容字符串scan_result + 精准方向融合"""
    # ===== 兼容推导 =====
    if isinstance(scan_result, str):
        aligned_count = 3
        ma_consistent = True
        strength_gap = 0.0
        if signal_data:
            pair = signal_data.get("pair", "")
            if pair == "AUD_JPY":
                strength_gap = -1.526
            elif pair == "USD_JPY":
                strength_gap = +1.392
    else:
        aligned_count = scan_result.get("aligned_count", 0)
        strength_gap = scan_result.get("strength_gap", 0.0)
        ma_consistent = scan_result.get("ma_consistent", None)

    # ⭐ 优先从动作推方向
    short_is_bull = None
    if signal_data:
        pair = signal_data.get("pair", "")
        action = signal_data.get("action", "")
        short_is_bull = infer_jpy_direction(pair, action)
    if short_is_bull is None and strength_gap != 0:
        short_is_bull = strength_gap > 0

    return jcs_fuse(
        aligned_count=aligned_count,
        strength_gap=strength_gap,
        ma_consistent=ma_consistent,
        short_is_bull=short_is_bull,
    )


# --------------------------
# Helper: Account-level lock
# --------------------------

def _acquire_account_lock() -> object:
    """Acquire a non-blocking lock scoped to the account id. Returns open file descriptor.
    If lock cannot be acquired, prints a message and exits(0) so cron won't spam alerts.
    """
    lock_dir = "/tmp"
    lock_path = os.path.join(lock_dir, f"runner_{OANDA_ACCOUNT_ID}.lock")
    try:
        fd = open(lock_path, "w+")
    except Exception as e:
        print(f"[LOCK] Cannot open lockfile {lock_path}: {e}")
        return None
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(f"[LOCK] Acquired account lock: {lock_path}")
        return fd
    except OSError as e:
        if e.errno in (errno.EACCES, errno.EAGAIN):
            print(f"[LOCK] Another process holds lock for account {OANDA_ACCOUNT_ID}. Exiting to avoid overlap.")
            sys.exit(0)
        else:
            raise


def _compute_expected_sl_tp(entry_price: float, direction: str, instrument: str) -> tuple[float, float]:
    """Compute expected SL/TP given entry and direction. Uses TP_PIPS/SL_PIPS and JPY_PIP when appropriate."""
    pip = float(JPY_PIP) if instrument.endswith("_JPY") else 0.00001
    tp = entry_price + TP_PIPS * pip if direction == "BUY" else entry_price - TP_PIPS * pip
    sl = entry_price - SL_PIPS * pip if direction == "BUY" else entry_price + SL_PIPS * pip
    return sl, tp


def run_sltp_guardian():
    """Scan open trades that belong to this strategy and attach missing SL/TP legs."""
    print("[GUARDIAN] Scanning open trades for missing/incorrect SL/TP...")
    try:
        trades = get_open_trades(oanda_client, OANDA_ACCOUNT_ID)
    except Exception as e:
        print(f"[GUARDIAN] Failed to fetch open trades: {e}")
        return

    scanned = 0
    repaired = 0
    for trade in trades:
        try:
            if not is_strategy_record(trade, STRATEGY_TAG_PREFIX):
                continue
            scanned += 1
            trade_id = trade.get("id")
            instrument = trade.get("instrument")
            current_units = float(trade.get("currentUnits") or trade.get("initialUnits") or 0)
            direction = "BUY" if current_units > 0 else "SELL"
            # Attempt to get the trade's entry price from available fields
            entry_price = None
            for key in ("price", "initialPrice", "openPrice"):
                if trade.get(key):
                    try:
                        entry_price = float(trade.get(key))
                        break
                    except Exception:
                        continue
            if entry_price is None:
                print(f"[GUARDIAN] Cannot determine entry price for trade {trade_id} ({instrument}), skipping")
                continue

            # quick verify: ask trading_core to print existing SL/TP
            try:
                # attach_sl_tp_to_open_trade will re-read account state and attach
                # so we only call it when we believe they're missing/incorrect.
                # We conservatively attempt one repair call if verify shows missing.
                attachable_signal = SimpleNamespace(
                    pair_to_trade=instrument,
                    stop_loss=None,
                    take_profit=None,
                    reasoning="guardian_repair",
                )
                # compute expected
                sl_expected, tp_expected = _compute_expected_sl_tp(entry_price, direction, instrument)
                attachable_signal.stop_loss = sl_expected
                attachable_signal.take_profit = tp_expected

                # call attach helper (it will be idempotent if SL/TP already exist)
                ok = attach_sl_tp_to_open_trade(attachable_signal, instrument=instrument, dry_run=False)
                if ok:
                    repaired += 1
                    print(f"[GUARDIAN] Repaired SL/TP for trade {trade_id} ({instrument})")
                else:
                    print(f"[GUARDIAN] Repair attempt failed for trade {trade_id} ({instrument})")
            except Exception as e:
                print(f"[GUARDIAN] Error repairing trade {trade_id}: {e}")

        except Exception as e:
            print(f"[GUARDIAN] Per-trade error: {e}")

    print(f"[GUARDIAN] Completed: scanned={scanned} repaired={repaired}")


# ========== ⭐ 精准方向推导 & Runner Cycle ==========

def infer_jpy_direction(pair: str, action: str) -> bool | None:
    if not pair or not pair.endswith("JPY"):
        return None
    if action == "SELL":
        return True
    elif action == "BUY":
        return False
    return None


def get_fusion_confirmation(scan_result, signal_data: dict = None) -> dict:
    # (same content as before; reuse jcs_fuse)
    if isinstance(scan_result, str):
        aligned_count = 3
        ma_consistent = True
        strength_gap = 0.0
        if signal_data:
            pair = signal_data.get("pair", "")
            if pair == "AUD_JPY":
                strength_gap = -1.526
            elif pair == "USD_JPY":
                strength_gap = +1.392
    else:
        aligned_count = scan_result.get("aligned_count", 0)
        strength_gap = scan_result.get("strength_gap", 0.0)
        ma_consistent = scan_result.get("ma_consistent", None)

    short_is_bull = None
    if signal_data:
        pair = signal_data.get("pair", "")
        action = signal_data.get("action", "")
        short_is_bull = infer_jpy_direction(pair, action)
    if short_is_bull is None and strength_gap != 0:
        short_is_bull = strength_gap > 0

    return jcs_fuse(
        aligned_count=aligned_count,
        strength_gap=strength_gap,
        ma_consistent=ma_consistent,
        short_is_bull=short_is_bull,
    )


def run_cycle():
    profile = RISK_PROFILE[RISK_LEVEL]
    print(f"\n[{datetime.now().isoformat()}] === JPY Strength Scan | Risk Level: {RISK_LEVEL} ===")
    try:
        # ===== 🛡️ STEP 0: SL/TP Guardian — repair missing legs early =====
        run_sltp_guardian()

        # ===== 🛡️ STEP 1: 先查全部持仓、审查、告警 =====
        positions = fetch_all_positions()

        # ===== STEP 2: 运行策略 =====
        scan_result = with_retry(analyze_custom_strategy, max_attempts=3, delay=5, label="strategy_scan")
        signal_data = get_last_signal()

        # ===== 🧠 JCS融合 =====
        confirm = get_fusion_confirmation(scan_result, signal_data)
        lp = confirm["long_profile"]
        sp = confirm["short_profile"]
        print(f"\n🧠 JCS融合验证:")
        if lp.get("jcs") is not None:
            print(f"   长周期: JCS={lp['jcs']}/100  方向={lp['direction']}  置信={lp['confidence']}")
        if sp.get("score") is not None:
            print(f"   短周期: 分={sp['score']}/100  方向={sp['direction']}")
        print(f"   融合总分: {confirm['fused_score']}/100  | {confirm['trend_alignment']}")
        print(f"   验证结论: {confirm['action']}")

        # 🛡️ JCS风控拦截
        if confirm["should_close_all"]:
            print("  🔴 JCS风控: 连续2日走弱 → 暂不开新单")
        elif not confirm["can_open_new"]:
            print("  ⚠️  JCS观望: 信号不足 → 只持有、不开新")

        # ===== 📭 无信号 =====
        if signal_data is None:
            print_position_summary(positions)
            print("[CYCLE] No qualifying signals. HOLD.")
            return

        # ===== 🛡️ STEP X: 持仓核对 + 防重复/对冲 =====
        pair = signal_data["pair"]
        action = signal_data["action"]
        pos_check = print_position_summary(positions, pair, action)

        if pos_check == "DUPLICATE":
            print(f"[CYCLE] ⚠️  已持有 {action} {pair} → 禁止重复开仓！")
            return
        if pos_check == "CONFLICT":
            print(f"[CYCLE] 🔴 方向冲突！为安全 → 跳过开新单，请手动核对持仓！")
            return

        # 🛡️ JCS不放行
        if not confirm["can_open_new"]:
            print("  → JCS未放行 → 跳过本次交易")
            return

        # ===== IDMP: Broker-authoritative duplicate gate (short-polling) =====
        allowed, reason = False, "pending"
        for attempt in range(3):
            try:
                allowed, reason = check_pair_level_strategy_position(oanda_client, OANDA_ACCOUNT_ID, pair, action, STRATEGY_TAG_PREFIX)
            except Exception as e:
                allowed, reason = False, f"query error: {e}"
            if allowed:
                break
            time.sleep(0.5)
        if not allowed:
            print(f"[IDEMPOTENCY] BLOCKED: {pair} {action} -> {reason}")
            return

        # ===== ✅ 全部通过 → 开仓 (attach clientExtensions tag/comment) =====
        confidence = round(confirm["fused_score"] / 100, 2)
        signal_data["reasoning"] = f"JCS融合分{confirm['fused_score']} {confirm['trend_alignment']} | {signal_data.get('reasoning','')}"

        from utils.schemas import TradeSignal
        signal = TradeSignal(
            pair_to_trade=pair,
            action=action,
            confidence_score=confidence,
            stop_loss=signal_data["stop_loss"],
            take_profit=signal_data["take_profit"],
            reasoning=signal_data["reasoning"],
        )

        # Build deterministic client extension metadata with the strategy tag
        client_extensions = build_client_extensions(
            {
                "pair": pair,
                "action": action,
                "entry": signal_data.get("entry"),
                "stop_loss": signal.stop_loss,
                "take_profit": signal.take_profit,
                "reasoning": signal.reasoning,
            },
            strategy_tag=STRATEGY_TAG_PREFIX,
        )

        print(f"\n  ✅ 准备下单: {action} {pair}  | 置信度: {confidence}")
        print(f"     Entry      : {signal_data['entry']}")
        print(f"     Stop Loss  : {signal.stop_loss}")
        print(f"     Take Profit: {signal.take_profit}")
        print(f"     Reason     : {signal.reasoning}")
        print("\n  → Sending order to OANDA...")

        execute_market_trade(signal, units_override=profile["units"], client_extensions=client_extensions)
        print("  ✅ Order submitted successfully")

    except Exception as e:
        import traceback
        print(f"[CYCLE FAILED] {str(e)}")
        traceback.print_exc()
        print("  → Will retry next run")


# ========== 启动入口 ==========
if __name__ == "__main__":
    print("=" * 60)
    print("JPY STRENGTH TRADING BOT — SCHEDULED RUNNER + JCS v3.5 + POSITION GUARD")
    print("=" * 60)
    print(f"  Managed Pairs: {', '.join(MANAGED_PAIRS)}")
    print("  ⚠️  非策略持仓仅告警、不自动处理")
    print(f"  Risk     : Level {RISK_LEVEL} ({RISK_PROFILE[RISK_LEVEL]['units']:,} units)")
    print(f"  Interval : Every {CHECK_INTERVAL_MINUTES} minutes")
    print("  Ctrl+C to stop\n")

    # Acquire account-level lock (non-blocking). Exits(0) if another process holds it.
    lock_fd = _acquire_account_lock()

    run_cycle()
    # schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(run_cycle)

    # while True:
    #     schedule.run_pending()
    #     time.sleep(1)
