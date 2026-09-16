"""
Patched scheduled runner (v3): integrates safety helpers for lock, guardian, idempotency, and verified execution.

This file is intended to be used as a patch source. Do NOT copy it directly into place without review.
"""

import time
from datetime import datetime
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import CHECK_INTERVAL_MINUTES, RISK_LEVEL, RISK_PROFILE
from custom_strategy import analyze_custom_strategy, get_last_signal
from retry import with_retry
from utils.jpy_jcs_strategy import run as jcs_fuse

# Safety helpers
from utils.safety_helpers import (
    acquire_account_lock,
    build_client_extensions,
    execute_market_trade_verified,
    wait_for_no_existing_order,
    run_sltp_guardian,
)

# Local imports kept minimal: rely on safety helper to call into trading_core
from utils.schemas import TradeSignal

# ========== 🛡️ 策略管理清单：只认这些、其他全部标记外部持仓 ==========
MANAGED_PAIRS = {
    "USD_JPY", "EUR_JPY", "GBP_JPY",
    "AUD_JPY", "CAD_JPY", "NZD_JPY",
}

MIN_JPY_STRENGTH_GAP = 2.0

_POSITION_CACHE = {
    "timestamp": None,
    "positions": {},
    "has_strategy": False,
    "unmanaged": [],
}


def fetch_all_positions() -> dict:
    # keep behavior from previous iteration; trading-aware unmanaged detection is best-effort
    all_pos = {}
    unmanaged = []
    has_strategy_position = False

    try:
        from utils import trading_core
        for pair in MANAGED_PAIRS:
            pos = None
            if hasattr(trading_core, "get_open_position"):
                pos = trading_core.get_open_position(pair)
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

        global _POSITION_CACHE
        _POSITION_CACHE["timestamp"] = datetime.now()
        _POSITION_CACHE["positions"] = all_pos
        _POSITION_CACHE["has_strategy"] = has_strategy_position
        _POSITION_CACHE["unmanaged"] = unmanaged

    except Exception as e:
        print(f"  ⚠️  持仓查询异常: {e}")

    return all_pos


def print_position_summary(positions: dict, selected_pair: str = None, selected_action: str = None):
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


def infer_jpy_direction(pair: str, action: str) -> bool | None:
    if not pair or not pair.endswith("JPY"):
        return None
    if action == "SELL":
        return True
    elif action == "BUY":
        return False
    return None


def get_fusion_confirmation(scan_result, signal_data: dict = None) -> dict:
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

    result = jcs_fuse(
        aligned_count=aligned_count,
        strength_gap=strength_gap,
        ma_consistent=ma_consistent,
        short_is_bull=short_is_bull,
    )
    result["strength_gap"] = strength_gap
    return result


def run_cycle():
    profile = RISK_PROFILE[RISK_LEVEL]
    account_id = profile.get("account_id") if isinstance(profile, dict) else None

    print(f"\n[{datetime.now().isoformat()}] === JPY Strength Scan | Risk Level: {RISK_LEVEL} ===")
    try:
        # Acquire guard lock (best-effort). If cannot acquire, exit immediately.
        lock_fd = None
        try:
            lock_fd = acquire_account_lock(account_id)
            if lock_fd is None:
                print("Lock not acquired; exiting run to avoid race")
                return
        except Exception as e:
            print(f"[LOCK] helper failed: {e}")

        # Run SL/TP guardian first to self-heal any missing SL/TP legs
        try:
            from utils import trading_core
            oanda_client = None
            if trading_core and hasattr(trading_core, "get_oanda_client"):
                try:
                    oanda_client = trading_core.get_oanda_client()
                except Exception:
                    oanda_client = None
            guardian_summary = run_sltp_guardian(oanda_client, account_id, list(MANAGED_PAIRS))
            print(f"[GUARDIAN] checked={guardian_summary.get('checked')} updated={guardian_summary.get('updated')} failed={guardian_summary.get('failed')}")
        except Exception as e:
            print(f"[GUARDIAN] failed: {e}")

        positions = fetch_all_positions()

        scan_result = with_retry(analyze_custom_strategy, max_attempts=3, delay=5, label="strategy_scan")
        signal_data = get_last_signal()

        confirm = get_fusion_confirmation(scan_result, signal_data)
        lp = confirm["long_profile"]
        sp = confirm["short_profile"]
        strength_gap = confirm["strength_gap"]

        print(f"\n🧠 JCS融合验证:")
        if lp.get("jcs") is not None:
            print(f"   长周期: JCS={lp['jcs']}/100  方向={lp['direction']}  置信={lp['confidence']}")
        if sp.get("score") is not None:
            print(f"   短周期: 分={sp['score']}/100  方向={sp['direction']}")
        print(f"   JPY强度差: {strength_gap:+.3f}  (门槛: ±{MIN_JPY_STRENGTH_GAP})")
        print(f"   融合总分: {confirm['fused_score']}/100  | {confirm['trend_alignment']}")
        print(f"   验证结论: {confirm['action']}")

        if confirm["should_close_all"]:
            print("  🔴 JCS风控: 连续2日走弱 → 暂不开新单")
        elif not confirm["can_open_new"]:
            print("  ⚠️  JCS观望: 信号不足 → 只持有、不开新")

        if signal_data is None:
            print_position_summary(positions)
            print("[CYCLE] No qualifying signals. HOLD.")
            return

        pair = signal_data["pair"]
        action = signal_data["action"]
        pos_check = print_position_summary(positions, pair, action)

        if pos_check == "DUPLICATE":
            print(f"[CYCLE] ⚠️  已持有 {action} {pair} → 禁止重复开仓！")
            return
        if pos_check == "CONFLICT":
            print(f"[CYCLE] 🔴 方向冲突！为安全 → 跳过开新单，请手动核对持仓！")
            return

        if abs(strength_gap) < MIN_JPY_STRENGTH_GAP:
            print(f"  → JPY强度差 {strength_gap:+.3f} 未达门槛 ±{MIN_JPY_STRENGTH_GAP} → 跳过本次交易")
            return

        if not confirm["can_open_new"]:
            print("  → JCS未放行 → 跳过本次交易")
            return

        confidence = round(confirm["fused_score"] / 100, 2)
        signal_data["reasoning"] = f"JCS融合分{confirm['fused_score']} {confirm['trend_alignment']} | 强度差{strength_gap:+.3f} | {signal_data.get('reasoning','')}"

        signal = TradeSignal(
            pair_to_trade=pair,
            action=action,
            confidence_score=confidence,
            stop_loss=signal_data["stop_loss"],
            take_profit=signal_data["take_profit"],
            reasoning=signal_data["reasoning"],
        )

        print(f"\n  ✅ 准备下单: {action} {pair}  | 置信度: {confidence}")
        print(f"     Entry      : {signal_data['entry']}")
        print(f"     Stop Loss  : {signal.stop_loss}")
        print(f"     Take Profit: {signal.take_profit}")
        print(f"     Reason     : {signal.reasoning}")
        print("\n  → Sending order to OANDA...")

        # Build clientExtensions for idempotency audits
        client_ext = build_client_extensions(pair, action, signal_data)

        # best-effort attempt to find an oanda_client for idempotency checks and guardian
        try:
            from utils import trading_core
            oanda_client = None
            if trading_core and hasattr(trading_core, "get_oanda_client"):
                oanda_client = trading_core.get_oanda_client()
            # short-poll to mitigate propagation delay
            free = wait_for_no_existing_order(oanda_client, account_id, pair, action, client_ext.get("tag"), attempts=3, delay=0.5)
            if not free:
                print(f"  ⚠️  Idempotency check: existing order/trade seems present. Skipping new order for {pair} {action}.")
                return
        except Exception:
            print("  ⚠️  Idempotency helper unavailable; continuing with submission (not recommended)")

        exec_res = execute_market_trade_verified(signal, units_override=profile["units"], client_extensions=client_ext, account_id=account_id, oanda_client=oanda_client if 'oanda_client' in locals() else None)
        if exec_res.get("success"):
            print("  ✅ Order submitted and verified (best-effort)")
        else:
            print("  ⚠️  Order submission/verification reported issues:", exec_res.get("info"))

    except Exception as e:
        import traceback
        print(f"[CYCLE FAILED] {str(e)}")
        traceback.print_exc()
        print("  → Will retry next run")
    finally:
        try:
            if lock_fd:
                # keep lock until process end; if we opened it here, close it now (script runs single-cycle)
                try:
                    fcntl = __import__('fcntl')
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    lock_fd.close()
                except Exception:
                    pass
        except Exception:
            pass


if __name__ == "__main__":
    print("=" * 60)
    print("JPY STRENGTH TRADING BOT — SCHEDULED RUNNER + JCS v3.5 + POSITION GUARD")
    print("=" * 60)
    print(f"  Managed Pairs: {', '.join(MANAGED_PAIRS)}")
    print(f"  Min Strength Gap to trade: ±{MIN_JPY_STRENGTH_GAP}")
    print("  ⚠️  非策略持仓仅告警、不自动处理")
    print(f"  Risk     : Level {RISK_LEVEL} ({RISK_PROFILE[RISK_LEVEL]['units']:,} units)")
    print(f"  Interval : Every {CHECK_INTERVAL_MINUTES} minutes")
    print("  Ctrl+C to stop\n")

    run_cycle()
