"""
📐 JCS 三合一 · OANDA 生产版
================================
✅ 自动适配两种运行方式
✅ 零警告 · 完整校验 · 已收盘K线保护
✅ 阈值/除零/DXY失败 全部健壮处理
"""
from pathlib import Path
import sys

# 自动加入项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import oandapyV20
import oandapyV20.endpoints.instruments as instruments
import pandas as pd
import numpy as np

from config_oanda import get_oanda_profile
profile = get_oanda_profile()
api = profile["api"]

if api is None:
    raise RuntimeError(
        "❌ OANDA API 未初始化！检查 OANDA_ENV / OANDA_API_TOKEN 配置。"
    )

# ========== 配置 ==========
PAIRS = {
    "CHF_JPY": "CHF_JPY",
    "USD_JPY": "USD_JPY",
    "EUR_JPY": "EUR_JPY",
    "GBP_JPY": "GBP_JPY",
    "AUD_JPY": "AUD_JPY",
    "CAD_JPY": "CAD_JPY",
    "NZD_JPY": "NZD_JPY",
}
THRESHOLD = 0.5
GRANULARITY = "D"

# ========== 拉汇率 ==========
def get_oanda_rates():
    rates = {}
    for ccy, instr in PAIRS.items():
        params = {"count": "4", "granularity": GRANULARITY, "price": "M"}
        r = instruments.InstrumentsCandles(instrument=instr, params=params)
        resp = api.request(r)
        candles = [c for c in resp["candles"] if c.get("complete", False)]
        if len(candles) < 2:
            raise ValueError(f"{instr}: 有效K线不足2根")
        closes = [float(c["mid"]["c"]) for c in candles[-2:]]
        rates[ccy] = closes
    return rates

# ========== 算强度 ==========
def calc_strength(rates):
    jpy_strength = {}
    for ccy, closes in rates.items():
        ret = np.log(closes[-1] / closes[-2])
        base_ccy = ccy.replace("_JPY", "")
        jpy_strength[base_ccy] = -ret

    jpy_strength["JPY"] = 0.0
    s = pd.Series(jpy_strength)
    mn, mx = s.min(), s.max()

    if np.isclose(mx, mn):
        return pd.Series(0.0, index=s.index).sort_values(ascending=False)

    normed = (s - mn) / (mx - mn) * 2 - 1
    return normed.sort_values(ascending=False)

# ========== DXY ==========
def get_dxy_pct():
    try:
        import yfinance as yf
        dxy = yf.download("DX-Y.NYB", period="3d", interval="1d", progress=False)["Close"]
        if len(dxy) >= 2:
            v_today = float(dxy.iloc[-1].item())
            v_prev = float(dxy.iloc[-2].item())
            return round((v_today - v_prev) / v_prev * 100, 2)
    except Exception as e:
        print(f"⚠️  DXY 读取失败: {e}")
    return None

# ========== 主流程 ==========
print("=" * 65)
print("📊 数据源: OANDA · 真实汇率 · 已收盘K线")
print("=" * 65)

rates = get_oanda_rates()
print(f"✅ 成功拉取 {len(rates)} 个货币对")

S = calc_strength(rates)
JPY, USD = S["JPY"], S["USD"]
TOP_PAIR_GAP = max(abs(S[ccy] - JPY) for ccy in S.index if ccy != "JPY")
DXY_PCT = get_dxy_pct()

# 打分
gap_top = JPY - USD
A_score = max(0, min(100, gap_top / 0.15 * 100))

if DXY_PCT is None:
    B_score, B_note = 50, "⚠️  DXY 不可用 → 中性"
elif DXY_PCT < -0.1:
    B_score, B_note = 90, f"✅ DXY{DXY_PCT:+.2f}% → 美元走弱"
elif DXY_PCT > +0.1:
    B_score, B_note = 20, f"🔴 DXY{DXY_PCT:+.2f}% → 美元走强"
else:
    B_score, B_note = 50, f"⚠️  DXY{DXY_PCT:+.2f}% → 横盘中性"

gap_ratio = TOP_PAIR_GAP / THRESHOLD
C_score = min(100, 50 + gap_ratio * 40) if gap_ratio >= 1.0 else 0
C_note = f"✅ 最强对 |gap|={TOP_PAIR_GAP:.4f}" if C_score > 0 else f"🔴 最强对 |gap|={TOP_PAIR_GAP:.4f}"

JCS = 0.45 * A_score + 0.30 * B_score + 0.25 * C_score

# 输出
print("\n【实时货币强度排名】")
for rank, (ccy, val) in enumerate(S.items(), 1):
    bar = "▲" if val >= 0 else "▼"
    marker = " ★" if ccy == "JPY" else ""
    print(f"  {rank}. {ccy}: {val:+.4f} {bar}{marker}")

print("\n" + "=" * 65)
print("📐 JCS 三合一综合强度报告")
print("=" * 65)
print(f"【层1 差距45%】 JPY-USD = {gap_top:.4f} → 得分: {A_score:.1f}/100")
print(f"【层2 DXY30%】  {B_note} → 得分: {B_score:.0f}/100")
print(f"【层3 支撑25%】 {C_note} → 得分: {C_score:.1f}/100")
print("-" * 65)
print(f"📐 综合 JCS = {JCS:.1f}/100")
print("=" * 65)

if JCS >= 80:
    print("✅ JPY真强势 → 持有 + 可开新单")
elif JCS >= 50:
    print("⚠️  强势有瑕疵 → 只持有、不开新单、观察")
else:
    print("🔴 三层不确认 → 全部平仓！不赌、不扛、跑得快！")