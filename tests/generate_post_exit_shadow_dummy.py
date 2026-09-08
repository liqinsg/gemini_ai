#!/usr/bin/env python3
"""
Generate DUMMY shadow-mode log data for PostExitGate validation & testing.
Produces realistic-looking logs/post_exit_gate_shadow.jsonl following RFC v4.0.
NOTE: For TESTING ONLY — not used in production.
"""

import json
import math
import random
import json
import math
import random
from datetime import datetime, timezone, timedelta
from pathlib import Path

LOG_PATH = Path("logs/post_exit_gate_shadow.jsonl")
PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "NZD_USD", "USD_CAD"]
TIERS = ["tier1", "tier2", "tier3"]


def main():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    records = []
    now = datetime.now(timezone.utc)

    for _ in range(100):
        pair = random.choice(PAIRS)
        tier = random.choice(TIERS)

        if tier == "tier1":
            baseline, m_reason = 1.00, 1.00
        elif tier == "tier2":
            baseline, m_reason = 1.05, 1.10
        else:  # tier3
            baseline, m_reason = 1.15, 1.25

        consecutive_failures = random.randint(0, 6)
        elapsed_hours = random.uniform(0.0, 48.0)

        # Compute multipliers
        if consecutive_failures > 0:
            m_streak = min(1.0 + 0.25 * (consecutive_failures - 1), 3.0)
        else:
            m_streak = 1.0

        # m_decay = math.exp(-elapsed_hours / 6.0)
        # m_decay = max(math.exp(-elapsed_hours / 6.0), 0.7)
        m_decay = max(math.exp(-elapsed_hours / 6.0), 0.45)
        if elapsed_hours <= 1.0:
            m_decay = 1.0

        # Regime reset trigger (~8% chance)
        alignment = 3 if random.random() < 0.08 else random.choice([1, 2])
        rank = 1 if alignment == 3 else random.randint(1, 4)
        gap_delta = random.uniform(0.3, 2.2) * baseline
        regime_reset = (alignment == 3 and rank == 1 and gap_delta >= baseline * 1.5)

        if regime_reset:
            m_reason, m_streak, m_decay = 1.0, 1.0, 1.0

        effective_hurdle = baseline * m_reason * m_streak * m_decay
        verdict = "WOULD_PASS" if gap_delta >= effective_hurdle else "WOULD_REJECT"

        ts = now - timedelta(hours=random.uniform(0, 168))  # within last 7 days

        records.append({
            "log_type": "post_exit_shadow",
            "timestamp_utc": ts.isoformat(),
            "pair": pair,
            "tier": tier,
            "verdict": verdict,
            "effective_hurdle": round(effective_hurdle, 4),
            "gap_delta": round(gap_delta, 4),
            "m_streak": round(m_streak, 4),
            "m_decay": round(m_decay, 4),
            "regime_reset_triggered": regime_reset,
            "consecutive_failures": consecutive_failures,
            "elapsed_hours": round(elapsed_hours, 2),
            "alignment": alignment,
            "rank": rank
        })

    # Sort chronologically and write
    records.sort(key=lambda r: r["timestamp_utc"])
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, sort_keys=True) + "\n")

    print(f"✅ Generated {len(records)} dummy shadow records → {LOG_PATH}")


if __name__ == "__main__":
    main()