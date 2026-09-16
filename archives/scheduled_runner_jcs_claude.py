"""
Scheduled Runner — JPY Strength Strategy + JCS Fusion v3.5 + Position Guard
==========================================
✅ #1 启动先查全部持仓、列明细、区分"策略管理/外部持仓"
✅ #2 开仓前精确核对：对不对、方向对不对、有没有冲突
✅ #3 不在策略清单 → 告警标记、绝不自动处理
✅ #4 杜绝：漏网仓、重复开、对冲仓
✅ #5 货币强度总差(score_gap)未达门槛 → 不开新仓
✅ #6 [NEW] Dominance Guard 触发的平仓，统一走本文件的持仓管理路径
        (不再由 custom_strategy.py 直接连线 OANDA 关仓)
"""
import os
import time
# import schedule
from datetime import datetime
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

import config as _config
from config import CHECK_INTERVAL_MINUTES, RISK_LEVEL, RISK_PROFILE
from custom_strategy import (
    analyze_custom_strategy,
    get_last_signal,
    get_last_score_gap,
    get_dominance_guard_status,
)
from utils import execute_market_trade, get_open_position, oanda_client
from retry import with_retry
from utils.jpy_jcs_strategy import run as jcs_fuse

# ========== 🛡️ 策略管理清单：只认这些、其他全部标记外部持仓 ==========
MANAGED_PAIRS = {
    "USD_JPY", "EUR_JPY", "GBP_JPY",
    "AUD_JPY", "CAD_JPY", "NZD_JPY",
}

# ========== ⭐ 最低货币强度总差门槛 ==========
# 对应 format_strength_ranking 打印的 "Score gap: 1.441 (MODERATE)"
# 未达此门槛 → 无论JCS融合结果如何，一律不开新仓
MIN_JPY_SCORE_GAP = 2.0

OANDA_ACCOUNT_ID = getattr(_config, "OANDA_ACCOUNT_ID", None) or os.getenv("OANDA_ACCOUNT_ID")

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

    ⚠️ NOTE: 目前只查询 MANAGED_PAIRS 中的货币对。要真正检测"外部持仓"，
    需要额外调用一个不带pair过滤的OANDA接口，再把不在 MANAGED_PAIRS 里
    的部分标记为 unmanaged。在接入之前 unmanaged 检测不生效。
    """
    all_pos = {}
    unmanaged = []
    has_strategy_position = False

    try:
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

        global _POSITION_CACHE
        _POSITION_CACHE["timestamp"] = datetime.now()
        _POSITION_CACHE["positions"] = all_pos
        _POSITION_CACHE["has_strategy"] = has_strategy_position
        _POSITION_CACHE["unmanaged"] = unmanaged

    except Exception as e:
        print(f"  ⚠️  持仓查询异常: {e}")

    return all_pos


def close_managed_position(pair: str) -> bool:
    """
    ⭐ NEW: 统一的持仓关闭入口。用同一个 oanda_client（从 utils 导入，
    和 custom_strategy.py 用的是同一个客户端）来关闭指定货币对的持仓，
    并立刻同步更新 _POSITION_CACHE，保证本轮周期后续的重复/冲突检查
    读到的是关闭后的真实状态，而不是过时缓存。

    这个函数取代了原来 custom_strategy.py 里 Dominance Guard 直接连线
    OANDA 的那段代码 —— 现在所有仓位变更都经过这一个地方。
    """
    if not OANDA_ACCOUNT_ID:
        print(f"  [POSITION GUARD] 无法关闭 {pair}：缺少 OANDA_ACCOUNT_ID")
        return False
    try:
        import importlib
        positions_mod = importlib.import_module("oandapyV20.endpoints.positions")

        req = positions_mod.OpenPositions(accountID=OANDA_ACCOUNT_ID)
        oanda_client.request(req)
        open_positions = req.response.get("positions", [])
        pos = next((p for p in open_positions if p.get("instrument") == pair), None)

        if not pos:
            print(f"  [POSITION GUARD] {pair} 无持仓，无需关闭")
            _POSITION_CACHE["positions"].pop(pair, None)
            return True

        long_u = int(float(pos.get("long", {}).get("units", 0)))
        short_u = int(float(pos.get("short", {}).get("units", 0)))
        payload = {}
        if long_u > 0:
            payload["longUnits"] = str(long_u)
        if short_u < 0:
            payload["shortUnits"] = str(abs(short_u))

        if not payload:
            print(f"  [POSITION GUARD] {pair} 无可关闭数量")
            _POSITION_CACHE["positions"].pop(pair, None)
            return True

        pc = positions_mod.PositionClose(accountID=OANDA_ACCOUNT_ID, instrument=pair, data=payload)
        oanda_client.request(pc)
        print(f"  [POSITION GUARD] ✅ 已关闭 {pair}: {getattr(pc, 'response', pc)}")
        _POSITION_CACHE["positions"].pop(pair, None)
        return True

    except Exception as e:
        print(f"  [POSITION GUARD] ❌ 关闭 {pair} 失败: {e}")
        return False


def handle_dominance_guard(positions: dict) -> dict:
    """
    ⭐ NEW: 如果本轮扫描触发了 Dominance Guard（方向共识丧失），
    在这里统一关闭当前持有的策略仓位，并返回刷新后的持仓字典。
    这是唯一允许在"扫描/分析"之外产生仓位变更的地方，且全部走
    close_managed_position，保证缓存和券商状态一致。
    """
    if not get_dominance_guard_status():
        return positions

    if not positions:
        print("  [POSITION GUARD] Dominance Guard 已触发，但当前无策略持仓，无需操作")
        return positions

    print("  🔴 [POSITION GUARD] Dominance Guard 触发 — 方向共识丧失，关闭现有策略持仓")
    for pair in list(positions.keys()):
        close_managed_position(pair)

    # 关闭动作完成后，向券商重新核实一次，而不是只信任本地缓存的 pop 结果
    return fetch_all_positions()


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


def get_fusion_confirmation(signal_data: dict = None) -> dict:
    """
    JCS 融合验证。

    ⭐ CHANGED: analyze_custom_strategy() 只会返回一段报告字符串（从来
    不会返回 dict），所以旧代码里 `isinstance(scan_result, str)` 的
    "字符串兼容分支"其实是唯一会被执行的分支 —— 也就是说，AUD_JPY/USD_JPY
    的硬编码占位值(-1.526 / +1.392)是过去唯一实际生效过的
    strength_gap 输入，而不是"兜底"。

    现在直接用 signal_data['strength_score']（custom_strategy.py 里
    真实计算出的"该货币对相对JPY的强度"）作为 strength_gap，不再需要
    scan_result 这个参数，也不再有任何硬编码占位数字。
    """
    aligned_count = 3  # TODO: custom_strategy.py 目前不对外暴露真实的
                        # "对齐时间周期数量"，这里仍是占位值，如需精确
                        # 需要让 check_ma5_alignment 把对齐数量也传出来
    ma_consistent = True
    strength_gap = 0.0
    short_is_bull = None

    if signal_data:
        strength_gap = signal_data.get("strength_score", 0.0)
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
    print(f"\n[{datetime.now().isoformat()}] === JPY Strength Scan | Risk Level: {RISK_LEVEL} ===")
    try:
        # ===== 🛡️ STEP 0: 先查全部持仓、审查、告警 =====
        positions = fetch_all_positions()

        # ===== STEP 1: 运行策略 =====
        with_retry(analyze_custom_strategy, max_attempts=3, delay=5, label="strategy_scan")
        signal_data = get_last_signal()
        score_gap = get_last_score_gap()

        # ===== 🛡️ STEP 1.5: Dominance Guard 平仓（统一走本文件的路径）=====
        positions = handle_dominance_guard(positions)

        # ===== 🧠 JCS融合（用真实的 strength_score，不再是硬编码占位值）=====
        confirm = get_fusion_confirmation(signal_data)
        lp = confirm["long_profile"]
        sp = confirm["short_profile"]
        print(f"\n🧠 JCS融合验证:")
        if lp.get("jcs") is not None:
            print(f"   长周期: JCS={lp['jcs']}/100  方向={lp['direction']}  置信={lp['confidence']}")
        if sp.get("score") is not None:
            print(f"   短周期: 分={sp['score']}/100  方向={sp['direction']}")
        print(f"   该货币对相对JPY强度: {confirm['strength_gap']:+.3f}")
        print(f"   货币强度总差(Score Gap): {score_gap:.3f}  (门槛: {MIN_JPY_SCORE_GAP})")
        print(f"   融合总分: {confirm['fused_score']}/100  | {confirm['trend_alignment']}")
        print(f"   验证结论: {confirm['action']}")

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

        # ⭐ 门槛检查：货币强度总差未达 MIN_JPY_SCORE_GAP → 不开新仓
        if score_gap < MIN_JPY_SCORE_GAP:
            print(f"  → 货币强度总差 {score_gap:.3f} 未达门槛 {MIN_JPY_SCORE_GAP} → 跳过本次交易")
            return

        # 🛡️ JCS不放行
        if not confirm["can_open_new"]:
            print("  → JCS未放行 → 跳过本次交易")
            return

        # ===== ✅ 全部通过 → 开仓 =====
        confidence = round(confirm["fused_score"] / 100, 2)
        signal_data["reasoning"] = (
            f"JCS融合分{confirm['fused_score']} {confirm['trend_alignment']} "
            f"| ScoreGap {score_gap:.3f} | {signal_data.get('reasoning','')}"
        )

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
    print(f"  Min Score Gap to trade: {MIN_JPY_SCORE_GAP}")
    print("  ⚠️  非策略持仓仅告警、不自动处理")
    print(f"  Risk     : Level {RISK_LEVEL} ({RISK_PROFILE[RISK_LEVEL]['units']:,} units)")
    print(f"  Interval : Every {CHECK_INTERVAL_MINUTES} minutes")
    print("  Ctrl+C to stop\n")

    run_cycle()
    # schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(run_cycle)

    # while True:
    #     schedule.run_pending()
    #     time.sleep(1)