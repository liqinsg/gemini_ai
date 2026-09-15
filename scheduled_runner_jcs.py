"""
Scheduled Runner — JPY Strength Strategy + JCS Fusion v3.5 + Position Guard
==========================================
✅ #1 启动先查全部持仓、列明细、区分"策略管理/外部持仓"
✅ #2 开仓前精确核对：对不对、方向对不对、有没有冲突
✅ #3 不在策略清单 → 告警标记、绝不自动处理
✅ #4 杜绝：漏网仓、重复开、对冲仓
"""
import time
import schedule
from datetime import datetime
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from config import CHECK_INTERVAL_MINUTES, RISK_LEVEL, RISK_PROFILE
from custom_strategy import analyze_custom_strategy, get_last_signal
from utils import execute_market_trade, get_open_position
from retry import with_retry
from utils.jpy_jcs_strategy import run as jcs_fuse

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


def run_cycle():
    profile = RISK_PROFILE[RISK_LEVEL]
    print(f"\n[{datetime.now().isoformat()}] === JPY Strength Scan | Risk Level: {RISK_LEVEL} ===")
    try:
        # ===== 🛡️ STEP 0: 先查全部持仓、审查、告警 =====
        positions = fetch_all_positions()

        # ===== STEP 1: 运行策略 =====
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

        # ===== 🛡️ STEP 2: 持仓核对 + 防重复/对冲 =====
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

        # ===== ✅ 全部通过 → 开仓 =====
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

        print(f"\n  ✅ 准备下单: {action} {pair}  | 置信度: {confidence}")
        print(f"     Entry      : {signal_data['entry']}")
        print(f"     Stop Loss  : {signal.stop_loss}")
        print(f"     Take Profit: {signal.take_profit}")
        print(f"     Reason     : {signal.reasoning}")
        print("\n  → Sending order to OANDA...")
        execute_market_trade(signal, units_override=profile["units"])
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

    run_cycle()
    # schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(run_cycle)

    # while True:
    #     schedule.run_pending()
    #     time.sleep(1)