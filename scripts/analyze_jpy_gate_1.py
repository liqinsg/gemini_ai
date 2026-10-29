#!/usr/bin/env python3
"""
JPY Weighted Gate — 历史分布分析 + 动态权重参数模拟 v2
======================================================
v2 更新: 同时对比两组基础权重（OLD 旧默认 / NEW 调优后），
        验证降低 EUR、提权 AUD 后动态权重是否真正生效。

用法:
    python analyze_jpy_gate.py [--csv logs/jpy_index_history.csv] [--out report.txt]

零依赖（只用标准库）。
"""
import argparse
import csv
import math
import os
import sys
from collections import defaultdict

# ---------- 两组基础权重对比 ----------
# OLD: 仓库默认（EUR 过高，AUD 过低）
OLD_BASE_WEIGHTS = {"EUR": 2.0, "USD": 1.5, "GBP": 1.0, "AUD": 0.5, "CHF": 0.1}
# NEW: 基于历史 delta 分布调优（AUD delta 最大应提权，EUR 降权）
NEW_BASE_WEIGHTS = {"EUR": 1.4, "USD": 1.3, "GBP": 1.0, "AUD": 1.2, "CHF": 0.2}

WEIGHT_SETS = {"OLD": OLD_BASE_WEIGHTS, "NEW": NEW_BASE_WEIGHTS}

# 旧 gate 参数（run.env 当前值）
OLD_PASS_THRESHOLD = 0.8
OLD_STRONG_MIN_COUNT = 2
MIN_ABS_SCORE = 0.1

# 参数网格
ALPHA_GRID = [0.0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 0.8]
CAP_GRID = [1.5, 2.0, 999.0]

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
            bear_sum += weighted
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
    hhi = sum(w * w for w in W.values())
    eff_n = 1.0 / hhi if hhi > 0 else 0.0
    return W, eff_n


def shadow_sums(deltas, W):
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


def run_grid(rows, base_weights, label):
    """对一组基础权重跑完整 alpha×cap 网格，返回结果 dict。"""
    baseline_top = []
    for scores in rows:
        deltas = compute_deltas(scores)
        W0, _ = dynamic_weights(deltas, base_weights, 0.0, 999.0)
        baseline_top.append(max(W0, key=W0.get) if W0 else None)

    results = {}
    for alpha in ALPHA_GRID:
        for cap in CAP_GRID:
            maxWs, effNs, flips = [], [], 0
            top_count = defaultdict(int)
            for i, scores in enumerate(rows):
                deltas = compute_deltas(scores)
                W, eff_n = dynamic_weights(deltas, base_weights, alpha, cap)
                if not W:
                    continue
                maxWs.append(max(W.values()))
                effNs.append(eff_n)
                top = max(W, key=W.get)
                top_count[top] += 1
                if alpha > 0 and top != baseline_top[i]:
                    flips += 1
            n = len(maxWs)
            results[(alpha, cap)] = {
                "maxW": sum(maxWs) / n,
                "effN": sum(effNs) / n,
                "top": max(top_count, key=top_count.get) if top_count else "?",
                "flip": 100.0 * flips / n if n else 0,
            }
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="logs/jpy_index_history.csv")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    out = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
    def p(*a, **kw):
        print(*a, **kw, file=out)

    rows = load_rows(args.csv)
    p("=" * 72)
    p("JPY WEIGHTED GATE — 历史分布分析报告 v2（新旧基础权重对比）")
    p("=" * 72)
    p(f"数据文件: {args.csv}  |  有效样本数: {len(rows)}")
    p(f"OLD 权重: EUR=2.0 USD=1.5 GBP=1.0 AUD=0.5 CHF=0.1  (sum=5.1)")
    p(f"NEW 权重: EUR=1.4 USD=1.3 GBP=1.0 AUD=1.2 CHF=0.2  (sum=5.1)")
    if len(rows) < 10:
        p("[WARN] 样本太少，结论仅供参考。")
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
    p(f"  → cap 参考: |delta| P90={percentile(v,90):.2f}, P95={percentile(v,95):.2f}")
    p("")

    # ---------- 2. 旧 gate 统计（两组权重分别算）----------
    p("-" * 72)
    p("2. 旧 gate 统计（未归一化静态权重，阈值=0.8）")
    p("-" * 72)
    for label, bw in WEIGHT_SETS.items():
        old_pass = old_conflict = 0
        old_dir = {"BUY": 0, "SELL": 0, None: 0}
        bulls, bears = [], []
        for scores in rows:
            deltas = compute_deltas(scores)
            r = old_gate_decision(deltas, bw)
            old_pass += int(r["passed"])
            old_conflict += int(r["conflict"])
            old_dir[r["direction"]] += 1
            bulls.append(r["bull_sum"])
            bears.append(abs(r["bear_sum"]))
        vb = sorted(bulls); ve = sorted(bears)
        p(f"  [{label}] 通过率={100*old_pass/len(rows):.1f}%  矛盾拒绝={100*old_conflict/len(rows):.1f}%  "
          f"方向: BUY={old_dir['BUY']} SELL={old_dir['SELL']} 无方向={old_dir[None]}")
        p(f"         bull_sum P50={fmt(percentile(vb,50))} P90={fmt(percentile(vb,90))} MAX={fmt(vb[-1])}  "
          f"|bear| P50={fmt(percentile(ve,50))} P90={fmt(percentile(ve,90))}")
    p("")

    # ---------- 3. 网格对比（核心）----------
    p("-" * 72)
    p("3. 动态权重网格模拟 — OLD vs NEW 基础权重对比")
    p("   flip% = 最高权重货币相对 alpha=0 的翻转率（动态是否真正生效的核心指标）")
    p("-" * 72)
    grid_results = {}
    for label, bw in WEIGHT_SETS.items():
        grid_results[label] = run_grid(rows, bw, label)

    p(f"{'alpha':>6} {'cap':>6} | {'OLD maxW':>8} {'OLD flip%':>10} {'OLD top':>8} | "
      f"{'NEW maxW':>8} {'NEW flip%':>10} {'NEW top':>8} | {'Δflip':>7}")
    p("-" * 88)
    for alpha in ALPHA_GRID:
        for cap in CAP_GRID:
            ro = grid_results["OLD"][(alpha, cap)]
            rn = grid_results["NEW"][(alpha, cap)]
            dflip = rn["flip"] - ro["flip"]
            p(f"{alpha:>6.2f} {cap:>6.1f} | {ro['maxW']:>8.3f} {ro['flip']:>9.1f}% {ro['top']:>8} | "
              f"{rn['maxW']:>8.3f} {rn['flip']:>9.1f}% {rn['top']:>8} | {dflip:>+6.1f}%")
    p("")
    p("  解读:")
    p("    NEW 权重的 flip% 应显著高于 OLD → 证明降 EUR/提 AUD 后动态权重真正生效")
    p("    maxW 应下降 → 权重更分散")
    p("    top 列应出现非 EUR 货币 → 不再是 EUR 铁王座")
    p("")

    # ---------- 4. shadow_sum 分布（NEW 权重）----------
    p("-" * 72)
    p("4. Shadow bull/bear_sum 分布（NEW 权重，归一化后尺度≈|delta|）")
    p("   用于校准新阈值：取 bullP75~P90 作为新 pass_threshold")
    p("-" * 72)
    candidates = [(0.0, 999.0), (0.1, 2.0), (0.2, 2.0), (0.3, 2.0), (0.3, 1.5), (0.5, 2.0)]
    p(f"{'(alpha,cap)':>14} {'bullP50':>8} {'bullP75':>8} {'bullP90':>8} {'bullMAX':>8} {'|bear|P75':>9} {'|bear|P90':>9}")
    for alpha, cap in candidates:
        sb, se = [], []
        for scores in rows:
            deltas = compute_deltas(scores)
            W, _ = dynamic_weights(deltas, NEW_BASE_WEIGHTS, alpha, cap)
            b, e, _, _ = shadow_sums(deltas, W)
            sb.append(b); se.append(abs(e))
        sb_s = sorted(sb); se_s = sorted(se)
        p(f"({alpha:.2f},{cap:>4.1f})   {fmt(percentile(sb_s,50))} {fmt(percentile(sb_s,75))} "
          f"{fmt(percentile(sb_s,90))} {fmt(sb_s[-1])} {fmt(percentile(se_s,75)):>9} {fmt(percentile(se_s,90)):>9}")
    p("")

    # ---------- 5. 推荐 ----------
    p("=" * 72)
    p("5. 参数推荐（基于 NEW 权重）")
    p("=" * 72)
    v_all = sorted(all_abs_delta)
    rec_cap = max(1.5, min(2.5, percentile(v_all, 90)))
    # 找 NEW 权重下 flip% 首次 ≥10% 且 maxW<0.45 的最小 alpha
    rec_alpha = None
    for alpha in ALPHA_GRID:
        if alpha == 0:
            continue
        r = grid_results["NEW"].get((alpha, 2.0))
        if r and r["flip"] >= 10 and r["maxW"] < 0.45:
            rec_alpha = alpha
            break
    # 新阈值：alpha=rec_alpha, cap=rec_cap 下的 bullP75
    thr_alpha = rec_alpha or 0.3
    sb = []
    for scores in rows:
        deltas = compute_deltas(scores)
        W, _ = dynamic_weights(deltas, NEW_BASE_WEIGHTS, thr_alpha, rec_cap)
        b, _, _, _ = shadow_sums(deltas, W)
        sb.append(b)
    sb_s = sorted(sb)
    new_thr_p75 = percentile(sb_s, 75)
    new_thr_p90 = percentile(sb_s, 90)

    p(f"  基础权重: NEW (EUR=1.4, USD=1.3, GBP=1.0, AUD=1.2, CHF=0.2)")
    p(f"  cap 推荐: {rec_cap:.2f}")
    p(f"  alpha 推荐: {rec_alpha if rec_alpha else '需更大alpha（网格上限0.8仍不足）'}")
    p(f"  新 pass_threshold: {new_thr_p75:.3f} (P75) ~ {new_thr_p90:.3f} (P90)")
    p(f"     （取 P75 通过率≈25%，取 P90 通过率≈10%，建议从 P80 附近开始）")
    p("")
    p("  实盘建议: 用 NEW 权重 + alpha=推荐值 + 新阈值，在 demo/最小仓位跑 1 周，")
    p("           同时 shadow 记录。1 周后对比新旧 gate 决策差异，再决定扩大仓位。")
    p("=" * 72)

    if out is not sys.stdout:
        out.close()
        print(f"报告已写入: {args.out}")


if __name__ == "__main__":
    main()
