#!/usr/bin/env python3
"""
JPY Weighted Gate — 历史分布分析 + 动态权重参数模拟
====================================================
用法:
    python analyze_jpy_gate.py [--csv logs/jpy_index_history.csv] [--out report.txt]

零依赖（只用标准库）。读取 composite index 历史 CSV，输出:
  1. score / delta 的真实分布（P50/P75/P90/P95/P99/max）
  2. 不同 (alpha, cap) 组合下的动态权重分布（最大单币权重、有效篮子数、HHI 集中度）
  3. 旧 gate（静态未归一化）的通过率与 bull/bear_sum 分布
  4. shadow_sum 分布（为后续阈值校准提供依据）
  5. 推荐 alpha / cap 范围

在 production server 上直接跑，10 秒出结果。
"""
import argparse
import csv
import math
import os
import sys
from collections import defaultdict

# ---------- 配置（与 run.env / scheduled_runner_v31 默认值一致）----------
# 基础权重（未归一化，JPY 自身排除在篮子外）
DEFAULT_BASE_WEIGHTS = {"EUR": 2.0, "USD": 1.5, "GBP": 1.0, "AUD": 0.5, "CHF": 0.1}
# 旧 gate 参数（run.env 当前值）
OLD_PASS_THRESHOLD = 0.8
OLD_STRONG_MIN_COUNT = 2
MIN_ABS_SCORE = 0.1  # JPY_WEIGHTED_CCY_MIN_ABS_SCORE

# 待扫描的参数网格（alpha=0 即纯静态归一化对照）
ALPHA_GRID = [0.0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5]
CAP_GRID = [1.0, 1.5, 2.0, 999.0]  # 999 = 不裁剪

BASKET = ["EUR", "USD", "GBP", "AUD", "CHF"]


def percentile(sorted_vals, p):
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * p / 100.0
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def fmt(v, width=8):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "  n/a".ljust(width)
    return f"{v:>{width}.3f}"


def load_rows(csv_path):
    """读取 jpy_index_history.csv，返回 list[dict]，只保留字段齐全的行。"""
    rows = []
    if not os.path.exists(csv_path):
        print(f"[ERROR] 文件不存在: {csv_path}", file=sys.stderr)
        sys.exit(1)
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                scores = {c: float(r[f"score_{c.lower()}"]) for c in ["JPY"] + BASKET}
            except (KeyError, ValueError):
                continue
            if any(not math.isfinite(v) for v in scores.values()):
                continue
            rows.append(scores)
    return rows


def compute_deltas(scores):
    jpy = scores["JPY"]
    return {c: jpy - scores[c] for c in BASKET}


def old_gate_decision(deltas, base_weights):
    """复现旧 gate：未归一化静态权重，min_abs 筛选，bull/bear 独立统计。"""
    bull_sum = bear_sum = 0.0
    bull_cnt = bear_cnt = 0
    for c in BASKET:
        d = deltas[c]
        if abs(d) < MIN_ABS_SCORE:
            continue
        w = base_weights.get(c, 0.0)
        weighted = d * w
        if d > 0:
            bull_sum += weighted
            bull_cnt += 1
        elif d < 0:
            bear_sum += weighted  # 负值累加
            bear_cnt += 1
    bull_ok = bull_sum >= OLD_PASS_THRESHOLD and bull_cnt >= OLD_STRONG_MIN_COUNT
    bear_ok = bear_sum <= -OLD_PASS_THRESHOLD and bear_cnt >= OLD_STRONG_MIN_COUNT
    passed = (bull_ok and not bear_ok) or (bear_ok and not bull_ok)
    direction = "SELL" if (bull_ok and not bear_ok) else ("BUY" if (bear_ok and not bull_ok) else None)
    return {
        "bull_sum": bull_sum, "bear_sum": bear_sum,
        "bull_cnt": bull_cnt, "bear_cnt": bear_cnt,
        "passed": passed, "direction": direction,
        "conflict": bull_ok and bear_ok,
    }


def dynamic_weights(deltas, base_weights, alpha, cap):
    """R_c = B_c * (1 + alpha * min(|delta|, cap)); W_c = R_c / sum(R)  (A1 上归一化)"""
    R = {}
    for c in BASKET:
        b = base_weights.get(c, 0.0)
        if b <= 0 or not math.isfinite(b):
            continue
        d = deltas[c]
        if not math.isfinite(d):
            continue
        capped = min(abs(d), cap)
        R[c] = b * (1.0 + alpha * capped)
    total = sum(R.values())
    if total <= 0:
        return {}, 0.0
    W = {c: r / total for c, r in R.items()}
    # HHI 集中度 + 有效篮子数（1/HHI）
    hhi = sum(w * w for w in W.values())
    eff_n = 1.0 / hhi if hhi > 0 else 0.0
    return W, eff_n


def shadow_sums(deltas, W):
    """用归一化动态权重计算 bull/bear_sum（尺度与旧 gate 不同，仅记录分布，不判 pass/fail）。"""
    bull = bear = 0.0
    bcnt = ecnt = 0
    for c in BASKET:
        d = deltas[c]
        if abs(d) < MIN_ABS_SCORE:
            continue
        w = W.get(c, 0.0)
        if d > 0:
            bull += d * w
            bcnt += 1
        elif d < 0:
            bear += d * w
            ecnt += 1
    return bull, bear, bcnt, ecnt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="logs/jpy_index_history.csv")
    ap.add_argument("--out", default=None, help="输出到文件，默认 stdout")
    args = ap.parse_args()

    out = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
    def p(*a, **kw):
        print(*a, **kw, file=out)

    rows = load_rows(args.csv)
    p("=" * 72)
    p("JPY WEIGHTED GATE — 历史分布分析报告")
    p("=" * 72)
    p(f"数据文件: {args.csv}")
    p(f"有效样本数: {len(rows)}")
    if len(rows) < 10:
        p("[WARN] 样本太少，结论仅供参考。至少需要 100+ 行。")
    p("")

    # ---------- 1. delta 分布 ----------
    p("-" * 72)
    p("1. |delta_c| = |S_JPY - S_c| 分布（百分比量纲）")
    p("-" * 72)
    delta_by_ccy = defaultdict(list)
    all_abs_delta = []
    for scores in rows:
        deltas = compute_deltas(scores)
        for c in BASKET:
            delta_by_ccy[c].append(abs(deltas[c]))
            all_abs_delta.append(abs(deltas[c]))
    p(f"{'CCY':>6} {'P50':>8} {'P75':>8} {'P90':>8} {'P95':>8} {'P99':>8} {'MAX':>8} {'mean':>8}")
    for c in BASKET:
        v = sorted(delta_by_ccy[c])
        p(f"{c:>6} {fmt(percentile(v,50))} {fmt(percentile(v,75))} {fmt(percentile(v,90))} "
          f"{fmt(percentile(v,95))} {fmt(percentile(v,99))} {fmt(v[-1])} {fmt(sum(v)/len(v))}")
    v = sorted(all_abs_delta)
    p(f"{'ALL':>6} {fmt(percentile(v,50))} {fmt(percentile(v,75))} {fmt(percentile(v,90))} "
      f"{fmt(percentile(v,95))} {fmt(percentile(v,99))} {fmt(v[-1])} {fmt(sum(v)/len(v))}")
    p("")
    p("  → cap 建议: 覆盖 P90~P95 的 |delta|，即 cap ≈ 上表 ALL 行 P90~P95。")
    p("    cap 太小 → 动态调整被截断；cap 太大 → 极端单币权重失控。")
    p("")

    # ---------- 2. 旧 gate 统计 ----------
    p("-" * 72)
    p("2. 旧 gate（静态未归一化权重）统计")
    p("-" * 72)
    old_bulls, old_bears = [], []
    old_pass = old_conflict = 0
    old_dir = {"BUY": 0, "SELL": 0, None: 0}
    for scores in rows:
        deltas = compute_deltas(scores)
        r = old_gate_decision(deltas, DEFAULT_BASE_WEIGHTS)
        old_bulls.append(r["bull_sum"])
        old_bears.append(r["bear_sum"])
        old_pass += int(r["passed"])
        old_conflict += int(r["conflict"])
        old_dir[r["direction"]] += 1
    p(f"  通过率: {old_pass}/{len(rows)} = {100*old_pass/len(rows):.1f}%")
    p(f"  矛盾拒绝(双阵营同时达标): {old_conflict}/{len(rows)} = {100*old_conflict/len(rows):.1f}%")
    p(f"  方向分布: BUY={old_dir['BUY']}  SELL={old_dir['SELL']}  无方向={old_dir[None]}")
    vb = sorted(old_bulls)
    ve = sorted([abs(x) for x in old_bears])
    p(f"  bull_sum 分布: P50={fmt(percentile(vb,50))} P90={fmt(percentile(vb,90))} MAX={fmt(vb[-1])}")
    p(f"  |bear_sum| 分布: P50={fmt(percentile(ve,50))} P90={fmt(percentile(ve,90))} MAX={fmt(ve[-1])}")
    p(f"  (旧阈值={OLD_PASS_THRESHOLD}, 可对比 P50/P90 看当前阈值松紧)")
    p("")

    # ---------- 3. 动态权重参数网格 ----------
    p("-" * 72)
    p("3. 动态权重参数网格模拟（alpha × cap）")
    p("   指标: maxW=平均最大单币权重 | effN=平均有效篮子数(1/HHI) | flip=与alpha=0权重排序翻转率")
    p("-" * 72)
    p(f"{'alpha':>6} {'cap':>6} {'maxW':>8} {'effN':>8} {'top_ccy':>10} {'flip%':>8}")
    baseline_top = []
    for scores in rows:
        deltas = compute_deltas(scores)
        W0, _ = dynamic_weights(deltas, DEFAULT_BASE_WEIGHTS, 0.0, 999.0)
        baseline_top.append(max(W0, key=W0.get) if W0 else None)

    results = {}
    for alpha in ALPHA_GRID:
        for cap in CAP_GRID:
            maxWs, effNs, flips = [], [], 0
            top_count = defaultdict(int)
            for i, scores in enumerate(rows):
                deltas = compute_deltas(scores)
                W, eff_n = dynamic_weights(deltas, DEFAULT_BASE_WEIGHTS, alpha, cap)
                if not W:
                    continue
                maxWs.append(max(W.values()))
                effNs.append(eff_n)
                top = max(W, key=W.get)
                top_count[top] += 1
                if alpha > 0 and top != baseline_top[i]:
                    flips += 1
            n = len(maxWs)
            key = (alpha, cap)
            results[key] = {
                "maxW": sum(maxWs) / n,
                "effN": sum(effNs) / n,
                "top": max(top_count, key=top_count.get),
                "flip": 100.0 * flips / n if n else 0,
            }
            p(f"{alpha:>6.2f} {cap:>6.1f} {fmt(results[key]['maxW'])} {fmt(results[key]['effN'])} "
              f"{results[key]['top']:>10} {results[key]['flip']:>7.1f}%")
    p("")
    p("  解读:")
    p("    maxW 越小越分散（<0.4 健康，>0.6 过度集中）")
    p("    effN 越大越分散（篮子5币，effN=5=完全分散，effN=2=集中在2币）")
    p("    flip% 是动态权重相对纯静态(alpha=0)的最高权重货币翻转比例，")
    p("          反映动态调权是否真正改变了权重结构（0%=完全没效果）")
    p("")

    # ---------- 4. shadow_sum 分布（为阈值校准）----------
    p("-" * 72)
    p("4. Shadow bull/bear_sum 分布（归一化动态权重，尺度≈|delta|）")
    p("   选几个代表性 (alpha,cap) 组合，供后续阈值校准参考")
    p("-" * 72)
    candidates = [(0.0, 999.0), (0.1, 1.5), (0.15, 1.5), (0.2, 1.5), (0.1, 2.0), (0.3, 1.5)]
    p(f"{'(alpha,cap)':>14} {'bullP50':>8} {'bullP90':>8} {'bullMAX':>8} {'|bear|P50':>9} {'|bear|P90':>9}")
    for alpha, cap in candidates:
        sb, se = [], []
        for scores in rows:
            deltas = compute_deltas(scores)
            W, _ = dynamic_weights(deltas, DEFAULT_BASE_WEIGHTS, alpha, cap)
            b, e, _, _ = shadow_sums(deltas, W)
            sb.append(b)
            se.append(abs(e))
        sb_s = sorted(sb); se_s = sorted(se)
        p(f"({alpha:.2f},{cap:>4.1f})   {fmt(percentile(sb_s,50))} {fmt(percentile(sb_s,90))} "
          f"{fmt(sb_s[-1])} {fmt(percentile(se_s,50)):>9} {fmt(percentile(se_s,90)):>9}")
    p("")
    p("  → 新阈值建议: 取 bull_sum P75~P90 作为 pass_threshold（归一化后尺度）。")
    p("    例如 alpha=0.15,cap=1.5 时 bullP90≈X，则新阈值≈X 可保持与旧 gate 相近的通过率。")
    p("")

    # ---------- 5. 推荐 ----------
    p("=" * 72)
    p("5. 参数推荐（基于本次数据）")
    p("=" * 72)
    v_all = sorted(all_abs_delta)
    rec_cap = percentile(v_all, 90)
    # 选 flip% 在 10~40% 且 maxW < 0.5 的最小 alpha
    rec_alpha = None
    for alpha in ALPHA_GRID:
        if alpha == 0:
            continue
        r = results.get((alpha, 1.5))
        if r and 10 <= r["flip"] <= 50 and r["maxW"] < 0.55:
            rec_alpha = alpha
            break
    p(f"  cap 推荐: {rec_cap:.2f}  (覆盖 |delta| P90；若 P90<1.0 用 1.0，若 >2.0 用 2.0)")
    p(f"  alpha 推荐: {rec_alpha if rec_alpha else '需更大样本/更宽网格'}  (翻转率10~50%且不过度集中)")
    p(f"  新阈值: 待 shadow_sum 分布确定后，取上表对应组合的 bullP75~P90。")
    p("")
    p("  实盘建议: 先用 alpha=推荐值 在 demo/最小仓位跑 1 周，同时 shadow 记录。")
    p("           1 周后对比新旧 gate 决策差异，再决定是否扩大仓位。")
    p("=" * 72)

    if out is not sys.stdout:
        out.close()
        print(f"报告已写入: {args.out}")


if __name__ == "__main__":
    main()
