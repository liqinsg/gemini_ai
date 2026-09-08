#!/usr/bin/env python3
"""
PostExitGate Shadow-Mode Log Analyzer
Usage: python analyze_post_exit.py
Outputs key metrics to evaluate RFC v4.0 performance.
"""

import json
import sys
from collections import Counter
from pathlib import Path

LOG_PATH = "logs/post_exit_gate_shadow.jsonl"


def load_records():
    path = Path(LOG_PATH)
    if not path.exists():
        print(f"⚠️  日志文件不存在: {LOG_PATH}")
        print("💡 先跑几天产生数据，或运行模拟数据生成脚本测试效果")
        sys.exit(0)

    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if rec.get("log_type") == "post_exit_shadow":
                records.append(rec)
        except json.JSONDecodeError:
            continue

    if not records:
        print("⚠️  暂无影子评估记录，先让系统跑几天再分析")
        sys.exit(0)

    return records


def main():
    rows = load_records()
    n = len(rows)

    pass_ct = sum(1 for r in rows if r["verdict"] == "WOULD_PASS")
    rej_ct = sum(1 for r in rows if r["verdict"] == "WOULD_REJECT")
    reset_ct = sum(1 for r in rows if r.get("regime_reset_triggered"))

    avg_hurdle = sum(r["effective_hurdle"] for r in rows) / n
    avg_gap = sum(r["gap_delta"] for r in rows) / n
    avg_streak = sum(r.get("m_streak", 1.0) for r in rows) / n
    avg_decay = sum(r.get("m_decay", 1.0) for r in rows) / n

    rej_pct = rej_ct / n * 100

    # ===== 输出报告 =====
    print("=" * 60)
    print("📊  PostExitGate 影子模式分析报告")
    print("=" * 60)
    print(f"📅  样本总量: {n}  条记录")
    print(f"✅ WOULD_PASS:  {pass_ct:4d}  ({pass_ct/n*100:.1f}%)")
    print(f"❌ WOULD_REJECT: {rej_ct:4d}  ({rej_pct:.1f}%)")
    print(f"🔄 重置触发:     {reset_ct:4d}  ({reset_ct/n*100:.1f}%)")
    print()
    print(f"📐 平均门槛值:   {avg_hurdle:.4f}")
    print(f"📏 平均缺口值:   {avg_gap:.4f}")
    print(f"🔥 连败系数均值: {avg_streak:.3f}  (理论范围 1.0–3.0)")
    print(f"⏳ 衰减系数均值: {avg_decay:.3f}  (理论范围 0.0–1.0)")
    print()

    # 按交易对分布
    print("💱 按交易对分布:")
    print(f"{'交易对':<12} {'总数':>6} {'PASS率':>8} {'REJECT%':>8}")
    print("-" * 40)
    for pair in sorted(set(r["pair"] for r in rows)):
        pair_rows = [r for r in rows if r["pair"] == pair]
        pct_pass = sum(1 for r in pair_rows if r["verdict"] == "WOULD_PASS") / len(pair_rows) * 100
        pct_rej = 100 - pct_pass
        print(f"{pair:<12} {len(pair_rows):6d} {pct_pass:7.1f}% {pct_rej:7.1f}%")

    print()

    # 按 Tier 分布
    print("🏷️  按平仓等级分布:")
    print(f"{'Tier':<8} {'总数':>6} {'PASS率':>8} {'平均门槛':>10}")
    print("-" * 40)
    for tier in ["tier1", "tier2", "tier3"]:
        tier_rows = [r for r in rows if r.get("tier") == tier]
        if not tier_rows:
            continue
        pct_pass = sum(1 for r in tier_rows if r["verdict"] == "WOULD_PASS") / len(tier_rows) * 100
        avg_h = sum(r["effective_hurdle"] for r in tier_rows) / len(tier_rows)
        print(f"{tier:<8} {len(tier_rows):6d} {pct_pass:7.1f}% {avg_h:10.4f}")

    print()
    print("=" * 60)
    print("🎯 RFC v4.0 转正参考:")
    print(f"   ✅ REJECT 占比: {rej_pct:.1f}%  → 理想区间 5%–25%  ✅" if 5 <= rej_pct <= 25 else
          f"   ⚠️  REJECT 占比: {rej_pct:.1f}%  → 建议区间 5%–25%")
    print(f"   ✅ 样本量 ≥100 → {'✅' if n >= 100 else '⚠️  继续积累'}")
    print("=" * 60)


if __name__ == "__main__":
    main()