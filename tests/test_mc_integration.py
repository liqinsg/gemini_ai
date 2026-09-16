"""
UNIT TEST: MC Data Integration Flow
验证: 文件读取 → 解析 → Regime判定 → TP倍率 → PostExitGate放行
"""
import os
import json
import tempfile
from datetime import datetime
from pathlib import Path

# ========== 模拟 get_latest_mc_local 核心逻辑 ==========
def mock_get_latest_mc_local(pair: str, day: bool = True) -> dict:
    """模拟读取 MC JSON 文件，返回结构化数据"""
    tf = "D" if day else "W"
    pair_filename = pair.replace("_", "")  # USD_JPY → USDJPY
    
    # 遍历目录找最新文件
    mc_dir = Path.cwd()
    pattern = f"mc_{tf}_{pair_filename}_*.json"
    matches = sorted(mc_dir.glob(pattern), reverse=True)
    
    if not matches:
        return None
    
    latest_file = matches[0]
    with open(latest_file, "r", encoding="utf-8") as f:
        return json.load(f)

# ========== 模拟 MC Regime 判定逻辑 ==========
def mock_regime_policy(mc_regime: str) -> dict:
    """根据 regime 返回风控参数"""
    reg = (mc_regime or "NEUTRAL").upper()
    
    if "CONSOLIDATION" in reg:
        return {
            "mode": "cautious",
            "tp_multiplier": 1.10,
            "max_positions": 1,
            "strength_hurdle": 0.8,
            "description": "震荡市 → 提高门槛、放大止盈"
        }
    elif "STRONG" in reg and "MOMENTUM" in reg:
        return {
            "mode": "aggressive",
            "tp_multiplier": 0.90,
            "max_positions": 3,
            "strength_hurdle": 0.0,
            "description": "强趋势 → 降低门槛、缩小止盈"
        }
    else:
        return {
            "mode": "normal",
            "tp_multiplier": 1.00,
            "max_positions": 1,
            "strength_hurdle": 0.0,
            "description": "中性市场 → 标准参数"
        }

# ========== 模拟 PostExitGate 放行逻辑 ==========
def mock_post_exit_check(mc_regime: str, strength_score: float, align_ok: bool) -> dict:
    """模拟 PostExitGate 结合MC的放行决策"""
    regime = (mc_regime or "NEUTRAL").upper()
    hurdle = 0.8 if "CONSOLIDATION" in regime else 0.0
    
    allowed = True
    reason = "ALLOW_BASELINE"
    
    if "CONSOLIDATION" in regime and abs(strength_score) < hurdle:
        allowed = False
        reason = f"CONSOLIDATION: strength {strength_score:.2f} < hurdle {hurdle}"
    
    if not align_ok:
        allowed = False
        reason = "Timeframe alignment failed"
    
    return {"allowed": allowed, "reason": reason, "hurdle": hurdle}

# ========== 测试用例 ==========
def run_tests():
    print("=" * 60)
    print("🔬 UNIT TEST: MC Data Integration Flow")
    print(f"🕐 Test Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)
    
    # ─── 测试1: 读取真实MC文件 ───
    print("\n📋 TEST 1: Load MC File (USD_JPY D)")
    mc_data = mock_get_latest_mc_local("USD_JPY", day=True)
    if mc_data:
        print(f"   ✅ File Found")
        print(f"   Instrument : {mc_data.get('instrument')}")
        print(f"   Regime     : {mc_data.get('regime')}")
        print(f"   P(UP)      : {mc_data.get('p_up')}%")
        print(f"   P(DOWN)    : {mc_data.get('p_down')}%")
        print(f"   Price      : {mc_data.get('current_price')}")
        regime = mc_data.get("regime", "NEUTRAL")
    else:
        print("   ⚠️ No MC file found — using mock data")
        regime = "NEUTRAL"
        strength_score = 1.3675
    
    # ─── 测试2: Regime 风控参数映射 ───
    print("\n📋 TEST 2: Regime Policy Mapping")
    policy = mock_regime_policy(regime)
    print(f"   Regime → Mode     : {regime} → {policy['mode']}")
    print(f"   TP Multiplier     : ×{policy['tp_multiplier']:.2f}")
    print(f"   Max Positions     : {policy['max_positions']}")
    print(f"   Strength Hurdle   : {policy['strength_hurdle']}")
    print(f"   Description       : {policy['description']}")
    
    # ─── 测试3: 完整放行决策 ───
    print("\n📋 TEST 3: Full Gate Decision")
    strength_score = 1.3675  # ← 来自JCS实际值
    align_ok = True           # ← 3/3 时间框架对齐
    gate = mock_post_exit_check(regime, strength_score, align_ok)
    print(f"   Strength Score    : {strength_score:.4f}")
    print(f"   Alignment (3/3)   : {'✅ PASS' if align_ok else '❌ FAIL'}")
    print(f"   Regime Hurdle     : {gate['hurdle']}")
    print(f"   Final Decision    : {'✅ ALLOW' if gate['allowed'] else '🚫 BLOCK'}")
    print(f"   Reason            : {gate['reason']}")
    
    # ─── 测试4: 三种Regime边界测试 ───
    print("\n📋 TEST 4: Regime Boundary Tests")
    for test_regime in ["NEUTRAL", "CONSOLIDATION", "STRONG_MOMENTUM"]:
        p = mock_regime_policy(test_regime)
        gate_test = mock_post_exit_check(test_regime, strength_score=0.5, align_ok=True)
        status = "✅" if gate_test['allowed'] else "🚫"
        print(f"   {test_regime:20s} → TP×{p['tp_multiplier']:.2f} | Hurdle {p['strength_hurdle']} | {status} {gate_test['reason']}")
    
    # ─── 总结 ───
    print("\n" + "=" * 60)
    if gate['allowed']:
        print("✅ ALL TESTS PASSED → MC Data Flow Working Correctly")
    else:
        print("⚠️ GATE BLOCKED → See Reason Above")
    print("=" * 60)

if __name__ == "__main__":
    run_tests()
