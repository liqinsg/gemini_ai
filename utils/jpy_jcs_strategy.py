"""
📐 JCS 融合决策引擎 · 机构级 v3.5
=====================================
✅ #1 short_dir 只信任传入值，不自动推断
✅ #2 长周期方向改用 gap 决定，JCS 只表可信度
✅ #3 JCS历史持久化(JSON)，重启不丢、双日确认才平仓
✅ #4 开仓/加仓分离，风控分级
✅ #5 Fail-Closed、方向独立、共振奖励/背离惩罚、对数评分
✅ 架构: 方向层 → 趋势层 → 时机层 → 融合层 → 风控层 → 执行层
"""
from pathlib import Path
import sys
import json
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from config_oanda import get_oanda_profile

import oandapyV20.endpoints.instruments as instruments
import pandas as pd
profile = get_oanda_profile()
api = profile["api"]

# ========== 全局配置 ==========
PAIRS = {
    "USD_JPY": "USD_JPY", "EUR_JPY": "EUR_JPY", "GBP_JPY": "GBP_JPY",
    "AUD_JPY": "AUD_JPY", "CAD_JPY": "CAD_JPY", "NZD_JPY": "NZD_JPY",
    "CHF_JPY": "CHF_JPY",
}
THRESHOLD_GAP = 0.5
GRANULARITY = "D"
JCS_VERY_STRONG = 80
JCS_STRONG      = 65
JCS_WEAK        = 45
W_LONG, W_SHORT = 0.60, 0.40

# 持久化路径
STATE_FILE = PROJECT_ROOT / "data" / "jcs_state.json"

# ========== 持久化读写 ==========
def load_jcs_history() -> list:
    """加载历史记录，不存在/损坏返回空"""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f).get("last_jcs", [])[-2:]
        except Exception:
            pass
    return []

def save_jcs_history(history: list):
    """保存最近2条，多余自动截断"""
    STATE_FILE.parent.mkdir(exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_jcs": history[-2:]}, f, indent=2)

# ========== 辅助: 对数压缩强度分 ==========
def strength_score(gap: float, scale: float = 0.12) -> float:
    """0→0, 0.05→34, 0.12→63, 0.30→91, 0.60→99；分辨率均匀不轻易满分"""
    return round(100 * (1 - np.exp(-abs(gap) / scale)), 1)

# ========== 长周期: 方向+可信度+历史 ==========
def get_longterm_profile():
    rates = {}
    for tag, inst in PAIRS.items():
        params = {"count": "4", "granularity": GRANULARITY, "price": "M"}
        resp = instruments.InstrumentsCandles(instrument=inst, params=params)
        api.request(resp)
        cands = resp.response["candles"]
        closes = [float(c["mid"]["c"]) for c in cands if c["complete"]]
        if len(closes) < 2:
            raise ValueError(f"{inst}: 有效K线不足2根")
        ret = np.log(closes[-1] / closes[-2])
        rates[tag] = -ret  # 转换为JPY相对强度

    s = pd.Series(rates)
    mn, mx = s.min(), s.max()

    if np.isclose(mx - mn, 0):
        gap_jpy_usd = 0.0
        jcs = 30.0
        long_dir = "NEUTRAL"
        top_gap = 0.0
    else:
        normed = (s - mn) / (mx - mn) * 2 - 1
        S = normed.sort_values(ascending=False)
        gap_jpy_usd = S["USD_JPY"]  # JPY相对USD差距

        # ⭐ #3: gap 纯方向、JCS 只表可信度
        if gap_jpy_usd > 0.15:
            long_dir = "JPY_BULL"
        elif gap_jpy_usd < -0.15:
            long_dir = "JPY_BEAR"
        else:
            long_dir = "NEUTRAL"

        top_gap = max(abs(S[c] - S["USD_JPY"]) for c in S.index if c != "USD_JPY")

        score_A = strength_score(gap_jpy_usd)
        score_B = 30  # 降低中性权重影响
        score_C = min(100, top_gap / 1.0 * 100)  # 连续平滑无跳变
        jcs = round(0.45 * score_A + 0.30 * score_B + 0.25 * score_C, 1)

    # ⭐ #3: 持久化 + 连续2日确认
    history = load_jcs_history()
    history.append(jcs)
    history = history[-2:]
    save_jcs_history(history)
    consecutive_bearish = len(history) >= 2 and all(j < JCS_WEAK for j in history)

    return {
        "jcs": jcs,
        "direction": long_dir,
        "gap_jpy_usd": round(gap_jpy_usd, 4),
        "top_gap": round(top_gap, 4),
        "consecutive_bearish": consecutive_bearish,
        "confidence": "VERY_STRONG" if jcs >= JCS_VERY_STRONG else "STRONG" if jcs >= JCS_STRONG else "WEAK" if jcs >= JCS_WEAK else "BEARISH",
    }

# ========== 短周期: 评分+方向(只信任传入值) ==========
def score_short_profile(aligned_count: int, total_timeframes: int = 3,
                        strength_gap: float = 0.0, ma_consistent: bool = None,
                        short_is_bull: bool | None = None) -> dict:
    """
    ⭐ #1: 方向只信任传入值，不从分数推断
    :param short_is_bull: True=JPY_BULL / False=JPY_BEAR / None=NEUTRAL
    """
    pct_aligned = aligned_count / total_timeframes
    score_align = min(100, pct_aligned * 120)
    score_gap = strength_score(strength_gap)
    score_trend = 100 if ma_consistent is True else (30 if ma_consistent is False else 50)

    composite = round(0.50 * score_align + 0.30 * score_gap + 0.20 * score_trend, 1)

    if composite >= 85: quality = "EXCELLENT"
    elif composite >= 70: quality = "GOOD"
    elif composite >= 55: quality = "FAIR"
    elif composite >= 40: quality = "WEAK"
    else: quality = "POOR"

    # ⭐ #1: 只信任传入值，绝不自动推断
    if short_is_bull is True:
        short_dir = "JPY_BULL"
    elif short_is_bull is False:
        short_dir = "JPY_BEAR"
    else:
        short_dir = "NEUTRAL"

    return {
        "score": composite,
        "quality": quality,
        "direction": short_dir,
        "aligned_count": aligned_count,
        "strength_gap": round(strength_gap, 4),
    }

# ========== 融合引擎: 方向优先 + 风控分级 ==========
def fuse_signals(long_p: dict, short_p: dict) -> dict:
    JCS = long_p["jcs"]
    SH = short_p["score"]
    long_dir = long_p["direction"]
    short_dir = short_p["direction"]

    # 方向精确比对
    if long_dir == short_dir and long_dir != "NEUTRAL":
        trend_aligned = "FULLY_ALIGNED"
        alignment_bonus = +5  # 多空同向都奖励
    elif long_dir != "NEUTRAL" and short_dir != "NEUTRAL" and long_dir != short_dir:
        trend_aligned = "DIVERGENCE⚠️"
        alignment_bonus = -10  # 只有背离才扣分
    else:
        trend_aligned = "PARTIAL"
        alignment_bonus = 0

    fused_score = round(max(0.0, min(100.0, JCS * W_LONG + SH * W_SHORT + alignment_bonus)), 1)

    # 风控闸门
    risk_level = "NORMAL"
    should_close_all = False
    if long_p["consecutive_bearish"]:
        risk_level = "CRITICAL"
        should_close_all = True
    elif trend_aligned.startswith("DIVERGENCE"):
        risk_level = "ELEVATED"

    # ⭐ #4: 开仓/加仓分离
    can_open_new = False
    can_add_position = False
    if risk_level == "CRITICAL":
        action = "🔴 CRITICAL → 连续2日走弱 → 全平仓、不开新"
    elif fused_score >= 85 and trend_aligned == "FULLY_ALIGNED":
        action = "🟢 同向共振 → 积极开新 + 允许加仓"
        can_open_new = True
        can_add_position = True
    elif fused_score >= 65 and JCS >= JCS_STRONG:
        action = "🟡 趋势确认 → 谨慎开新、不加仓"
        can_open_new = True
    else:
        action = "⚠️ 信号不足 → 只持有、不开新"

    return {
        "fused_score": fused_score,
        "risk_level": risk_level,
        "trend_alignment": trend_aligned,
        "alignment_bonus": alignment_bonus,
        "long_dir": long_dir,
        "short_dir": short_dir,
        "action": action,
        "can_open_new": can_open_new,
        "can_add_position": can_add_position,
        "should_close_all": should_close_all,
        "long_profile": long_p,
        "short_profile": short_p,
    }

# ========== 对外接口 ==========
def run(aligned_count: int, strength_gap: float = 0.0,
        ma_consistent: bool = None, short_is_bull: bool | None = None) -> dict:
    """
    :param aligned_count: H4/H1/M30 对齐数量 (0–3)
    :param strength_gap: JPY相对强度差 (正值=JPY强)
    :param ma_consistent: 均线趋势是否一致
    :param short_is_bull: True=JPY_BULL / False=JPY_BEAR / None=中性
    """
    try:
        long_p = get_longterm_profile()
        short_p = score_short_profile(
            aligned_count,
            strength_gap=strength_gap,
            ma_consistent=ma_consistent,
            short_is_bull=short_is_bull,
        )
        return fuse_signals(long_p, short_p)
    except Exception as e:
        print(f"⚠️  JCS引擎异常: {e}")
        return {
            "fused_score": 50.0,
            "risk_level": "UNKNOWN",
            "trend_alignment": "JCS_DOWN",
            "alignment_bonus": 0,
            "long_dir": "NEUTRAL",
            "short_dir": "NEUTRAL",
            "action": "⚠️  验证不可用 → 观望、不开新单",
            "can_open_new": False,   # Fail-Closed 🔒
            "can_add_position": False,
            "should_close_all": False,
            "long_profile": {"jcs": None, "direction": "NEUTRAL"},
            "short_profile": {"score": None, "direction": "NEUTRAL"},
        }


# ========== 独立测试 ==========
if __name__ == "__main__":
    result = run(
        aligned_count=2,
        strength_gap=1.728,
        short_is_bull=True,
    )
    lp = result["long_profile"]
    sp = result["short_profile"]

    print("=" * 70)
    print("🧠 JCS v3.5 · 机构级融合报告")
    print("=" * 70)
    print(f"\n📊 长周期: JCS={lp['jcs']}/100  方向={lp['direction']}  置信={lp['confidence']}")
    print(f"⏱️  短周期: 分={sp['score']}/100  方向={sp['direction']}")
    print(f"🔗 方向对齐: {result['trend_alignment']} (奖励{result['alignment_bonus']:+d})")
    print(f"📈 融合总分: {result['fused_score']}/100")
    print(f"🛡️  风控等级: {result['risk_level']}")
    print(f"\n🎯 {result['action']}")
    print(f"   可开新: {'✅ YES' if result['can_open_new'] else '❌ NO'}")
    print(f"   可加仓: {'✅ YES' if result['can_add_position'] else '❌ NO'}")
    print(f"   应全平: {'⚠️ YES 立即离场' if result['should_close_all'] else '✅ NO 继续持有'}")
    print("=" * 70)