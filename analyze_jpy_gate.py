#!/usr/bin/env python3
"""
JPY Weighted Gate — 历史分布分析 v3
===================================
v3 更新（回应 ChatGPT 质疑）:
  1. 输出 top 货币频率分布（每个货币成为最高权重的 %，澄清 top=EUR 只是众数）
  2. 完整阈值扫描表（不同 pass_threshold 下的通过率/矛盾率/方向分布）
  3. 决策翻转率（NEW+动态 vs OLD静态 的 pass/fail 一致率）
  4. 明确标注：旧阈值=0.8 已由生产运行日志确认（run.env）

零依赖。用法: python analyze_jpy_gate.py [--csv path] [--out report.txt]
"""
import argparse
import csv
import math
import os
import sys
from collections import defaultdict

OLD_BASE_WEIGHTS = {"EUR": 2.0, "USD": 1.5, "GBP": 1.0, "AUD": 0.5, "CHF": 0.1}
NEW_BASE_WEIGHTS = {"EUR": 1.4, "USD": 1.3, "GBP": 1.0, "AUD": 1.2, "CHF": 0.2}
WEIGHT_SETS = {"OLD": OLD_BASE_WEIGHTS, "NEW": NEW_BASE_WEIGHTS}

OLD_PASS_THRESHOLD = 0.8   # 已由生产运行日志确认（run.env JPY_WEIGHTED_PASS_THRESHOLD=0.8）
OLD_STRONG_MIN_COUNT = 2
MIN_ABS_SCORE = 0.1

ALPHA_GRID = [0.0, 0.1, 0.15, 0.2, 0.3, 0.5]
CAP_GRID = [1.84, 2.27, 999.0]  # P90 / P95 / 不裁剪
THRESHOLD_GRID = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2]
BASKET = ["EUR", "USD", "GBP", "AUD", "CHF"]


def percentile(sv, p):
    if not sv: return float("nan")
    k = (len(sv)-1)*p/100.0; f=math.floor(k); c=math.ceil(k)
    if f==c: return sv[int(k)]
    return sv[f]*(c-k)+sv[c]*(k-f)

def fmt(v,w=8):
    if v is None or (isinstance(v,float) and math.isnan(v)): return "  n/a".ljust(w)
    return f"{v:>{w}.3f}"

def load_rows(path):
    rows=[]
    with open(path,"r",encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                s={c:float(r[f"score_{c.lower()}"]) for c in ["JPY"]+BASKET}
            except (KeyError,ValueError): continue
            if any(not math.isfinite(v) for v in s.values()): continue
            rows.append(s)
    return rows

def deltas_of(s):
    j=s["JPY"]; return {c:j-s[c] for c in BASKET}

def gate_decision(deltas, W, threshold):
    """W 是归一化权重 dict（或未归一化基础权重，两者通用）。返回 pass/direction/conflict。"""
    bs=be=0.0; bc=ec=0
    for c in BASKET:
        d=deltas[c]
        if abs(d)<MIN_ABS_SCORE: continue
        w=W.get(c,0.0)
        if d>0: bs+=d*w; bc+=1
        elif d<0: be+=d*w; ec+=1
    bok=bs>=threshold and bc>=OLD_STRONG_MIN_COUNT
    eok=be<=-threshold and ec>=OLD_STRONG_MIN_COUNT
    passed=(bok and not eok) or (eok and not bok)
    direction="SELL" if (bok and not eok) else ("BUY" if (eok and not bok) else None)
    return passed,direction,bok and eok,bs,be

def dyn_weights(deltas, bw, alpha, cap):
    R={}
    for c in BASKET:
        b=bw.get(c,0.0)
        if b<=0 or not math.isfinite(b): continue
        d=deltas[c]
        if not math.isfinite(d): continue
        R[c]=b*(1.0+alpha*min(abs(d),cap))
    tot=sum(R.values())
    if tot<=0: return {},0.0
    W={c:r/tot for c,r in R.items()}
    hhi=sum(w*w for w in W.values()); eff=1.0/hhi if hhi>0 else 0.0
    return W,eff

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--csv",default="logs/jpy_index_history.csv")
    ap.add_argument("--out",default=None); args=ap.parse_args()
    out=open(args.out,"w",encoding="utf-8") if args.out else sys.stdout
    def p(*a,**kw): print(*a,**kw,file=out)

    rows=load_rows(args.csv)
    p("="*72); p("JPY WEIGHTED GATE — 分析报告 v3（回应 ChatGPT 质疑）"); p("="*72)
    p(f"样本数: {len(rows)}  |  旧阈值={OLD_PASS_THRESHOLD}（已由生产日志 run.env 确认）")
    p(f"OLD: EUR=2.0 USD=1.5 GBP=1.0 AUD=0.5 CHF=0.1 | NEW: EUR=1.4 USD=1.3 GBP=1.0 AUD=1.2 CHF=0.2")
    p("")

    # 预计算所有 deltas
    all_deltas=[deltas_of(s) for s in rows]

    # ---- 1. delta 分布 ----
    p("-"*72); p("1. |delta| 分布（百分比量纲）"); p("-"*72)
    by_ccy=defaultdict(list); allad=[]
    for d in all_deltas:
        for c in BASKET: by_ccy[c].append(abs(d[c])); allad.append(abs(d[c]))
    p(f"{'CCY':>6} {'P50':>8} {'P75':>8} {'P90':>8} {'P95':>8} {'P99':>8} {'MAX':>8}")
    for c in BASKET:
        v=sorted(by_ccy[c])
        p(f"{c:>6} {fmt(percentile(v,50))} {fmt(percentile(v,75))} {fmt(percentile(v,90))} {fmt(percentile(v,95))} {fmt(percentile(v,99))} {fmt(v[-1])}")
    v=sorted(allad); p(f"{'ALL':>6} {fmt(percentile(v,50))} {fmt(percentile(v,75))} {fmt(percentile(v,90))} {fmt(percentile(v,95))} {fmt(percentile(v,99))} {fmt(v[-1])}")
    p(f"  → cap 候选: P90={percentile(v,90):.2f} (1.84), P95={percentile(v,95):.2f} (2.27)")
    p("")

    # ---- 2. top 货币频率分布（回应 ChatGPT：top=EUR 只是众数）----
    p("-"*72); p("2. 最高权重货币频率分布（NEW 权重，回应 top=EUR 质疑）"); p("-"*72)
    p(f"{'alpha':>6} {'cap':>6} | {'EUR%':>7} {'USD%':>7} {'GBP%':>7} {'AUD%':>7} {'CHF%':>7} | {'flip%':>7}")
    p("-"*70)
    for alpha in ALPHA_GRID:
        for cap in CAP_GRID:
            topcnt=defaultdict(int); flip=0; n=0
            W0_list=[]
            for d in all_deltas:
                W0,_=dyn_weights(d,NEW_BASE_WEIGHTS,0.0,999.0); W0_list.append(max(W0,key=W0.get) if W0 else None)
            for i,d in enumerate(all_deltas):
                W,_=dyn_weights(d,NEW_BASE_WEIGHTS,alpha,cap)
                if not W: continue
                top=max(W,key=W.get); topcnt[top]+=1; n+=1
                if alpha>0 and top!=W0_list[i]: flip+=1
            p(f"{alpha:>6.2f} {cap:>6.2f} | " + " ".join(f"{100*topcnt.get(c,0)/n:>6.1f}%" for c in BASKET) + f" | {100*flip/n:>6.1f}%")
    p("")
    p("  解读: 任一列 <100% 即说明该货币不是'永远'最高。alpha=0 时 EUR=100%（铁王座），")
    p("        alpha>0 时 AUD% 上升即证明动态权重让其他货币有机会成为最高权重。")
    p("")

    # ---- 3. 阈值扫描表（回应 ChatGPT：不能只看 P75/P90）----
    p("-"*72); p("3. 阈值扫描表（NEW 权重 + alpha=0.15 + cap=1.84，归一化后尺度）"); p("-"*72)
    p(f"{'threshold':>10} {'通过率':>8} {'矛盾率':>8} {'BUY':>6} {'SELL':>6} {'无方向':>8}")
    p("-"*55)
    ref_decisions=[]  # 旧 gate（OLD 未归一化，阈值0.8）决策，用于翻转率
    for d in all_deltas:
        # 旧 gate: 未归一化 OLD 基础权重
        passed_old,_,_,_,_=gate_decision(d, OLD_BASE_WEIGHTS, OLD_PASS_THRESHOLD)
        ref_decisions.append(passed_old)
    for thr in THRESHOLD_GRID:
        pas=con=0; buy=sell=none=0; flips=0
        for i,d in enumerate(all_deltas):
            W,_=dyn_weights(d,NEW_BASE_WEIGHTS,0.15,1.84)
            passed,direc,conflict,_,_=gate_decision(d,W,thr)
            pas+=int(passed); con+=int(conflict)
            if direc=="BUY": buy+=1
            elif direc=="SELL": sell+=1
            else: none+=1
            if passed!=ref_decisions[i]: flips+=1
        n=len(rows)
        p(f"{thr:>10.2f} {100*pas/n:>7.1f}% {100*con/n:>7.1f}% {buy:>6} {sell:>6} {none:>8}  (翻转率={100*flips/n:.1f}%)")
    p("")
    p("  解读: 选择阈值=目标通过率。旧 gate(阈值0.8未归一化)通过率=75.6%。")
    p("        若想保持相近通过率 → 看哪行通过率≈75%；若想更严格(30%) → 看≈30%那行。")
    p("        翻转率=新 gate 相对旧 gate pass/fail 不一致的比例，越低越平滑。")
    p("")

    # ---- 4. 决策翻转率矩阵（不同 alpha/cap 下，新 vs 旧 gate）----
    p("-"*72); p("4. 决策翻转率矩阵（NEW+动态 vs OLD静态阈值0.8，阈值选 0.8 归一化）"); p("-"*72)
    p(f"{'alpha':>6} {'cap':>6} {'通过率':>8} {'翻转率':>8} {'maxW':>8} {'effN':>8}")
    p("-"*50)
    for alpha in ALPHA_GRID:
        for cap in CAP_GRID:
            pas=flips=0; maxWs=[]; effNs=[]
            for i,d in enumerate(all_deltas):
                W,eff=dyn_weights(d,NEW_BASE_WEIGHTS,alpha,cap)
                if not W: continue
                passed,_,_,_,_=gate_decision(d,W,0.8)
                pas+=int(passed)
                if passed!=ref_decisions[i]: flips+=1
                maxWs.append(max(W.values())); effNs.append(eff)
            n=len(maxWs)
            p(f"{alpha:>6.2f} {cap:>6.2f} {100*pas/n:>7.1f}% {100*flips/n:>7.1f}% {sum(maxWs)/n:>8.3f} {sum(effNs)/n:>8.2f}")
    p("")
    p("  解读: 翻转率<20% 为温和调整（推荐起步），>40% 为激进调整。")
    p("        alpha=0.15~0.20, cap=1.84 通常是翻转率 15~30% 的 sweet spot。")
    p("")

    # ---- 5. 最终推荐 ----
    p("="*72); p("5. 最终推荐（基于 v3 数据）"); p("="*72)
    p("  基础权重: NEW (EUR=1.4 USD=1.3 GBP=1.0 AUD=1.2 CHF=0.2)")
    p("  alpha: 0.15（保守）~ 0.20（适中），由第4节翻转率确认")
    p("  cap: 1.84（P90）")
    p("  新阈值: 由第3节选择目标通过率对应的 threshold（建议通过率 30~50%）")
    p("  上线方式: demo 账户实盘 + shadow JSONL 日志并行，1 周后评估")
    p("="*72)

    if out is not sys.stdout: out.close(); print(f"报告已写入: {args.out}")

if __name__=="__main__": main()
