"""
PostExitGate Shadow Evaluation Report Generator
Reads JSONL log and prints comprehensive statistics including:
  - Exit reason breakdown (SL / TP / ACTIVE)
  - Risk level & active-exit ratio
  - Pass/Reject rates
  - Cooling & multiplier stats
"""
import json
import os
from collections import Counter, defaultdict
from datetime import datetime

LOG_PATH = "logs/post_exit_gate_shadow.jsonl"

# ─── Color helpers ───
C_RESET = "\033[0m"
C_GREEN = "\033[92m"
C_RED = "\033[91m"
C_YELLOW = "\033[93m"
C_BLUE = "\033[94m"
C_BOLD = "\033[1m"

def load_records():
    if not os.path.exists(LOG_PATH):
        print(f"{C_YELLOW}⚠️  日志文件不存在: {LOG_PATH}{C_RESET}")
        print("💡 先跑几天产生数据，或运行模拟数据生成脚本测试效果")
        return []
    records = []
    with open(LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records

def fmt_pct(num, total):
    if total == 0:
        return "  0.0%"
    return f"{num/total*100:5.1f}%"

def main():
    records = load_records()
    if not records:
        return

    total = len(records)
    pass_ct = sum(1 for r in records if r.get("verdict") == "WOULD_PASS")
    rej_ct = sum(1 for r in records if r.get("verdict") == "WOULD_REJECT")
    reset_ct = sum(1 for r in records if r.get("regime_reset_triggered", False))

    # ─── Exit reason breakdown ───
    exit_reasons = [r.get("exit_reason", "UNKNOWN") for r in records]
    reason_counts = Counter(exit_reasons)

    # ─── Risk stats ───
    risk_levels = [r.get("risk_level", "normal") for r in records]
    risk_counts = Counter(risk_levels)
    avg_active_ratio = sum(r.get("active_ratio", 0.0) for r in records) / total
    avg_multiplier = sum(r.get("risk_multiplier", 1.0) for r in records) / total
    avg_cooling = sum(r.get("cooling_remaining_hours", 0.0) for r in records) / total
    cooling_active = sum(1 for r in records if r.get("in_cooling", False))

    # ─── Core metrics ───
    avg_hurdle = sum(r.get("effective_hurdle", 0.0) for r in records) / total
    avg_mdecay = sum(r.get("m_decay", 0.0) for r in records) / total
    avg_mstreak = sum(r.get("m_streak", 0.0) for r in records) / total

    # ─── Print Report ───
    print("=" * 60)
    print(f"{C_BOLD}📊  PostExitGate 影子模式分析报告  v4.1{C_RESET}")
    print("=" * 60)
    print(f"📅  样本总量: {total:4d}  条记录")
    print()
    print(f"✅ WOULD_PASS:  {pass_ct:5d}  {fmt_pct(pass_ct, total)}")
    print(f"❌ WOULD_REJECT:{rej_ct:5d}  {fmt_pct(rej_ct, total)}")
    print(f"🔄 重置触发:    {reset_ct:5d}  {fmt_pct(reset_ct, total)}")
    print()

    # ─── Exit Reason Breakdown (NEW) ───
    print(f"{C_BOLD}📋 平仓原因分布:{C_RESET}")
    for reason, label in [
        ("ACTIVE", "主动平仓"),
        ("SL", "止损平仓"),
        ("TP", "止盈平仓"),
        ("UNKNOWN", "未知类型"),
    ]:
        c = reason_counts.get(reason, 0)
        print(f"   {label:10s} {c:4d}  {fmt_pct(c, total)}")
    print()

    # ─── Risk Analysis (NEW) ───
    print(f"{C_BOLD}⚠️  风险评估:{C_RESET}")
    print(f"   主动平仓占比均值: {avg_active_ratio:.1%}")
    for level, label, color in [
        ("high", "高风险", C_RED),
        ("elevated", "风险上升", C_YELLOW),
        ("normal", "正常", C_GREEN),
    ]:
        c = risk_counts.get(level, 0)
        if c > 0:
            print(f"   {color}{label}时段:{C_RESET} {c:4d}  {fmt_pct(c, total)}")
    print(f"   门槛乘数均值:     ×{avg_multiplier:.2f}")
    print(f"   冷却期生效次数:   {cooling_active:4d}  {fmt_pct(cooling_active, total)}")
    print(f"   平均剩余冷却:     {avg_cooling:.2f}h")
    print()

    # ─── Original Metrics ───
    print(f"📐 平均门槛值:   {avg_hurdle:.4f}")
    print(f"🔥 连败系数均值: {avg_mstreak:.3f}  (理论范围 1.0–3.0)")
    print(f"⏳ 衰减系数均值: {avg_mdecay:.3f}  (理论范围 0.45–1.0)")
    print()

    # ─── RFC Evaluation ───
    rej_rate = rej_ct / total * 100 if total else 0
    print(f"{C_BOLD}🎯 RFC v4.1 转正参考:{C_RESET}")
    if 5 <= rej_rate <= 25:
        status = f"{C_GREEN}✅ 理想区间 5%–25%{C_RESET}"
    elif rej_rate < 5:
        status = f"{C_YELLOW}⚠️  偏低，建议 5%–25%{C_RESET}"
    else:
        status = f"{C_RED}⚠️  偏高，建议 5%–25%{C_RESET}"
    print(f"   REJECT 占比: {rej_rate:.1f}%  → {status}")
    if total >= 100:
        print(f"   ✅ 样本量 ≥100")
    else:
        print(f"   ⚠️  样本量不足100，继续积累数据")
    print("=" * 60)

if __name__ == "__main__":
    main()