#!/usr/bin/env python3
"""v2 动态权重逻辑验证（独立复制核心函数，不依赖 scheduled_runner_v31 的 import）。"""
import math


def compute_v2(global_scores, jpy_score, base_weights, exclude_ccies,
               min_abs_score, alpha, delta_cap, pass_threshold,
               strong_min_count, flatten_base=True):
    s_jpy = float(jpy_score)
    a = float(alpha); cap = float(delta_cap)
    if flatten_base:
        base = {c: (0.5 if c == "CHF" else 1.0) for c in base_weights}
    else:
        base = dict(base_weights)

    bull_R, bear_R, bull_delta, bear_delta = {}, {}, {}, {}
    for ccy, opp_score in global_scores.items():
        if ccy == "JPY" or ccy in exclude_ccies or opp_score is None:
            continue
        s_opp = float(opp_score)
        delta = s_jpy - s_opp
        if abs(delta) < min_abs_score:
            continue
        b = float(base.get(ccy, 0.0))
        if b <= 0:
            continue
        r = b * (1.0 + a * min(abs(delta), cap))
        if delta > 0:
            bull_R[ccy] = r; bull_delta[ccy] = delta
        elif delta < 0:
            bear_R[ccy] = r; bear_delta[ccy] = delta

    bull_total = sum(bull_R.values()); bear_total = sum(bear_R.values())
    bull_W = {c: r/bull_total for c, r in bull_R.items()} if bull_R else {}
    bear_W = {c: r/bear_total for c, r in bear_R.items()} if bear_R else {}
    bull_sum = sum(bull_delta[c]*w for c, w in bull_W.items()) if bull_W else 0.0
    bear_sum = sum(bear_delta[c]*w for c, w in bear_W.items()) if bear_W else 0.0
    bull_ok = bull_sum >= pass_threshold and len(bull_W) >= strong_min_count
    bear_ok = bear_sum <= -pass_threshold and len(bear_W) >= strong_min_count
    if bull_ok and not bear_ok:
        passed, direction = True, "SELL"
    elif bear_ok and not bull_ok:
        passed, direction = True, "BUY"
    else:
        passed, direction = False, None
    return {"passed": passed, "direction": direction,
            "bull_W": bull_W, "bear_W": bear_W,
            "bull_sum": bull_sum, "bear_sum": bear_sum,
            "bull_count": len(bull_W), "bear_count": len(bear_W)}


BASE = {"EUR": 1.4, "USD": 1.3, "GBP": 1.0, "AUD": 1.2, "CHF": 0.2}
KW = dict(base_weights=BASE, exclude_ccies=set(), min_abs_score=0.1,
          alpha=0.4, delta_cap=1.84, pass_threshold=0.35,
          strong_min_count=2, flatten_base=True)

def show(name, r):
    bw = ",".join(f"{c}:{w:.2f}" for c,w in sorted(r["bull_W"].items(), key=lambda x:-x[1]))
    erw = ",".join(f"{c}:{w:.2f}" for c,w in sorted(r["bear_W"].items(), key=lambda x:-x[1]))
    print(f"[{name}] pass={r['passed']} dir={r['direction']} "
          f"bull_sum={r['bull_sum']:+.3f}({r['bull_count']}) bear_sum={r['bear_sum']:+.3f}({r['bear_count']})")
    print(f"   bull_W={{{bw}}}  bear_W={{{erw}}}")

# 场景1：JPY 单边强势（对所有货币 delta>0）→ 应 PASS SELL
s1 = {"JPY": 1.5, "EUR": 0.2, "USD": 0.3, "GBP": 0.4, "AUD": 0.1, "CHF": 0.5}
show("场景1 JPY单边强", compute_v2(s1, s1["JPY"], **KW))

# 场景2：多空矛盾（EUR/USD 远弱于JPY，AUD/GBP 远强于JPY）→ 应 FAIL
s2 = {"JPY": 1.0, "EUR": -0.8, "USD": -0.7, "GBP": 1.8, "AUD": 1.9, "CHF": 0.9}
show("场景2 多空矛盾", compute_v2(s2, s2["JPY"], **KW))

# 场景3：EUR delta小(0.15)但静态权重高，AUD delta大(1.5)但静态权重低
# v2 拉平基础权重后，AUD 应主导 bull_sum
s3 = {"JPY": 1.6, "EUR": 1.45, "USD": 1.0, "GBP": 0.9, "AUD": 0.1, "CHF": 1.5}
r3_flat = compute_v2(s3, s3["JPY"], **KW)
r3_noflat = compute_v2(s3, s3["JPY"], **{**KW, "flatten_base": False})
show("场景3 拉平(去EUR中心)", r3_flat)
show("场景3 不拉平(EUR高权)", r3_noflat)
print(f"   → 拉平后 AUD 权重={r3_flat['bull_W'].get('AUD',0):.2f} vs 不拉平={r3_noflat['bull_W'].get('AUD',0):.2f}")

# 场景4：阵营内归一化校验 ΣW=1
for name, r in [("s1", compute_v2(s1, s1["JPY"], **KW)), ("s2", compute_v2(s2, s2["JPY"], **KW))]:
    sb = sum(r["bull_W"].values()); se = sum(r["bear_W"].values())
    print(f"[场景{name}] Σbull_W={sb:.4f} Σbear_W={se:.4f} (应≈1.0或0)")

# 场景5：alpha=0.4 放大效果：delta=1.84(达cap) vs delta=0.2 的权重比
# 拉平后基础=1.0 → R1=1.0*(1+0.4*1.84)=1.736, R2=1.0*(1+0.4*0.2)=1.08 → 权重比≈1.61:1
print(f"[场景5] 强信号(delta=1.84) vs 弱信号(delta=0.2) 权重比 = "
      f"{(1+0.4*1.84)/(1+0.4*0.2):.2f}:1 (v1 alpha=0.15 时仅 {(1+0.15*1.84)/(1+0.15*0.2):.2f}:1)")
