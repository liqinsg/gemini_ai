"""
PostExitGate Shadow Dummy Data Generator v4.2
PARAMETER-ALIGNED with post_exit_gate.py — change ONE place, both update.
"""
import json
import math
import os
import random
import time
from datetime import datetime, timezone
from pathlib import Path

# ============================================================
# 🎯 【参数区 · 与主文件完全一致】改这里 = 改全部
# ============================================================
HALF_LIFE_HOURS   = 8.0
FLOOR_DECAY       = 0.45
STRICT_WINDOW_H   = 1.5

NOTICE_COUNT      = 2
ELEVATED_COUNT    = 3
HIGH_COUNT        = 4

NOTICE_RATIO      = 0.50
ELEVATED_RATIO    = 0.67
HIGH_RATIO        = 0.75

MULT_NOTICE       = 1.0
MULT_ELEVATED     = 1.3
MULT_HIGH         = 1.5

COOL_NOTICE_H     = 0.0
COOL_ELEVATED_H   = 4.0
COOL_HIGH_H       = 8.0

# Tier & Pair Config
TIERS = {
    "tier1": {"baseline": 1.00, "m_reason": 1.00},
    "tier2": {"baseline": 1.05, "m_reason": 1.10},
    "tier3": {"baseline": 1.15, "m_reason": 1.25},
}
PAIRS = ["AUD_USD", "EUR_USD", "GBP_USD", "USD_JPY", "EUR_JPY", "USD_CAD"]
EXIT_POOL = ["ACTIVE", "ACTIVE", "SL", "TP", "TP"]  # ~40% ACTIVE

LOG_PATH = Path("logs/post_exit_gate_shadow.jsonl")
os.makedirs(LOG_PATH.parent, exist_ok=True)

# ============================================================
# 模拟风控状态 — 逻辑与主文件完全一致
# ============================================================
class SimRiskState:
    def __init__(self):
        self.history = []
        self.cooling_until = 0.0

    def record_exit(self, reason):
        self.history.append(reason)
        if len(self.history) > max(HIGH_COUNT, 4):
            self.history.pop(0)
        self._update()

    def _update(self):
        total = len(self.history)
        if total == 0:
            return
        act = sum(1 for r in self.history if r == "ACTIVE")
        ratio = act / total
        now = time.time()

        if   act >= HIGH_COUNT     and ratio >= HIGH_RATIO:
            self.cooling_until = now + COOL_HIGH_H * 3600
        elif act >= ELEVATED_COUNT and ratio >= ELEVATED_RATIO:
            self.cooling_until = now + COOL_ELEVATED_H * 3600
        elif act >= NOTICE_COUNT   and ratio >= NOTICE_RATIO:
            self.cooling_until = now + COOL_NOTICE_H * 3600

    def get_risk(self):
        total = len(self.history)
        if total == 0:
            return "normal", 1.0, 0.0, 0.0
        act = sum(1 for r in self.history if r == "ACTIVE")
        ratio = act / total
        now = time.time()
        rem_cool = max(0.0, (self.cooling_until - now) / 3600)

        if   act >= HIGH_COUNT     and ratio >= HIGH_RATIO:
            return "high",     MULT_HIGH,     rem_cool, ratio
        elif act >= ELEVATED_COUNT and ratio >= ELEVATED_RATIO:
            return "elevated", MULT_ELEVATED, rem_cool, ratio
        elif act >= NOTICE_COUNT   and ratio >= NOTICE_RATIO:
            return "notice",   MULT_NOTICE,   rem_cool, ratio
        return "normal", 1.0, rem_cool, ratio

    @staticmethod
    def calc_decay(elapsed_h):
        decay = math.exp(-elapsed_h / HALF_LIFE_HOURS)
        decay = max(decay, FLOOR_DECAY)
        if elapsed_h <= STRICT_WINDOW_H:
            decay = 1.0
        return decay

    @staticmethod
    def calc_streak(losses):
        s = 1.0 + 0.25 * (losses - 1)
        return min(s, 3.0) if losses > 0 else 1.0

# ============================================================
# 生成记录
# ============================================================
def generate_one(state):
    tier_name = random.choice(list(TIERS.keys()))
    tier = TIERS[tier_name]
    baseline, m_reason = tier["baseline"], tier["m_reason"]
    consecutive_loss = random.randint(0, 5)
    elapsed_h = random.uniform(0.1, 48.0)
    alignment = random.choice([1, 2, 3])
    rank = random.randint(1, 5)

    m_decay = SimRiskState.calc_decay(elapsed_h)
    m_streak = SimRiskState.calc_streak(consecutive_loss)

    gap_delta = random.uniform(0.3, 2.2)
    reset = alignment == 3 and rank == 1
    if reset:
        gap_delta = max(gap_delta, baseline * 1.5)
        m_decay, m_streak = 1.0, 1.0

    level, rmult, cool_rem, ratio = state.get_risk()
    hurdle = baseline * m_reason * m_streak * m_decay * rmult
    verdict = "WOULD_PASS" if gap_delta >= hurdle else "WOULD_REJECT"
    exit_reason = random.choice(EXIT_POOL)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pair": random.choice(PAIRS),
        "tier": tier_name,
        "exit_reason": exit_reason,
        "baseline": baseline,
        "m_reason": m_reason,
        "consecutive_failures": consecutive_loss,
        "elapsed_hours": round(elapsed_h, 2),
        "alignment": alignment,
        "rank": rank,
        "gap_delta": round(gap_delta, 4),
        "effective_hurdle": round(hurdle, 6),
        "m_decay": round(m_decay, 6),
        "m_streak": round(m_streak, 6),
        "risk_multiplier": round(rmult, 2),
        "risk_level": level,
        "active_ratio": round(ratio, 2),
        "in_cooling": cool_rem > 0.01,
        "cooling_remaining_hours": round(cool_rem, 2),
        "regime_reset_triggered": reset,
        "verdict": verdict,
    }, exit_reason

def main():
    N = 100
    state = SimRiskState()
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        for _ in range(N):
            record, reason = generate_one(state)
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            state.record_exit(reason)

    print(f"✅ Generated {N} records → {LOG_PATH}")
    print(f"📋 Rules: {NOTICE_COUNT}→notice/{ELEVATED_COUNT}→elevated/{HIGH_COUNT}→high")
    print(f"🔗 Logic aligned with PostExitGate v4.2")

if __name__ == "__main__":
    main()