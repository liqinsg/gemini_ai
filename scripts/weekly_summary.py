#!/usr/bin/env python3
"""
Weekly validation summary generator for scheduled_runner_v3 parallel diffs.
Scans logs/baseline/ and logs/tuned/, groups by date, computes daily & weekly metrics.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
AUDIT_FILE = ROOT / "docs" / "validation_audit.md"

# Reuse same parsing patterns as diff_analysis.py
PATTERNS = {
    "timestamp": re.compile(r"Time\s+:\s+([^\n]+)"),
    "gap": re.compile(r"GLOBAL_MAX_GAP:\s+([\d.]+)"),
    "jpy_vote": re.compile(r"AUD_JPY.*?No consensus: buy=([\d.]+).*sell=([\d.]+).*need\s+([\d.]+)", re.DOTALL),
    "usd_vote": re.compile(r"AUD_USD.*?No consensus: buy=([\d.]+).*sell=([\d.]+).*need\s+([\d.]+)", re.DOTALL),
    "decision": re.compile(r"\[GLOBAL\](.*?HOLD|Selected.*?BUY|Selected.*?SELL|no qualifying signals)", re.IGNORECASE),
    "skip_held": re.compile(r"\[SKIP HELD\]"),
    "signal_count": re.compile(r"Selected\s*\(vs [A-Z]+\):"),
    "override_fired": re.compile(r"OVERRIDE this cycle:\s*NONE|fired_cycle=\d+"),
}

@dataclass
class RunRecord:
    date: str                # YYYY-MM-DD
    timestamp: str           # full timestamp
    variant: str             # baseline / tuned
    gap: float
    aud_jpy_score: Optional[float]
    aud_usd_score: Optional[float]
    decision: str
    skip_held: bool
    has_signal: bool
    override_fired: bool

def parse_log_file(path: Path) -> Optional[RunRecord]:
    text = path.read_text(encoding="utf-8", errors="ignore")

    # Extract timestamp from filename: run_YYYYMMDD_HHMMSS.log
    m = re.match(r"run_(\d{8})_(\d{6})", path.stem)
    if not m:
        return None
    ymd = m.group(1)
    date_str = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
    time_str = f"{m.group(2)[:2]}:{m.group(2)[2:4]}:{m.group(2)[4:]}"

    # Global gap
    gap_m = PATTERNS["gap"].search(text)
    gap = float(gap_m.group(1)) if gap_m else 0.0

    # AUD_JPY vote score
    jpy_score = None
    jpy_m = PATTERNS["jpy_vote"].search(text)
    if jpy_m:
        jpy_score = float(jpy_m.group(1))

    # AUD_USD vote score
    usd_score = None
    usd_m = PATTERNS["usd_vote"].search(text)
    if usd_m:
        usd_score = float(usd_m.group(1))

    # Final decision
    dec_m = PATTERNS["decision"].search(text)
    decision = dec_m.group(1).strip() if dec_m else "UNKNOWN"

    skip_held = bool(PATTERNS["skip_held"].search(text))
    has_signal = bool(PATTERNS["signal_count"].search(text))
    override_m = PATTERNS["override_fired"].search(text)
    override_fired = bool(override_m and "NONE" not in override_m.group(0))

    variant = path.parent.name  # baseline / tuned

    return RunRecord(
        date=date_str,
        timestamp=f"{date_str} {time_str}",
        variant=variant,
        gap=gap,
        aud_jpy_score=jpy_score,
        aud_usd_score=usd_score,
        decision=decision,
        skip_held=skip_held,
        has_signal=has_signal,
        override_fired=override_fired,
    )

def load_all_runs() -> List[RunRecord]:
    records: List[RunRecord] = []
    for variant in ["baseline", "tuned"]:
        for log_file in sorted((LOGS / variant).glob("run_*.log")):
            rec = parse_log_file(log_file)
            if rec:
                records.append(rec)
    return sorted(records, key=lambda r: r.timestamp)

def build_daily_table(records: List[RunRecord]) -> str:
    """Group by date → show baseline vs tuned side-by-side."""
    by_date = defaultdict(dict)  # date → {variant: record}
    for rec in records:
        by_date[rec.date][rec.variant] = rec

    lines = [
        "## Daily Side-by-Side",
        "",
        "| Date | Variant | GAP | AUD_JPY Score | AUD_USD Score | Signal | Decision | SKIP_HELD | Override Fired |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    for date in sorted(by_date, reverse=True):
        variants = by_date[date]
        bl = variants.get("baseline")
        td = variants.get("tuned")

        # Baseline row
        if bl:
            jpy_s = f"{bl.aud_jpy_score:.2f}" if bl.aud_jpy_score else "—"
            usd_s = f"{bl.aud_usd_score:.2f}" if bl.aud_usd_score else "—"
            sig = "✅" if bl.has_signal else "—"
            skip = "⚠️" if bl.skip_held else "—"
            overr = "✅" if bl.override_fired else "—"
            lines.append(
                f"| {date} | **Baseline** | {bl.gap:.2f} | {jpy_s} | {usd_s} | {sig} | {bl.decision} | {skip} | {overr} |"
            )

        # Tuned row
        if td:
            jpy_s = f"{td.aud_jpy_score:.2f}" if td.aud_jpy_score else "—"
            usd_s = f"{td.aud_usd_score:.2f}" if td.aud_usd_score else "—"
            sig = "✅" if td.has_signal else "—"
            skip = "⚠️" if td.skip_held else "—"
            overr = "✅" if td.override_fired else "—"
            highlight = " **← DIFF**" if (bl and (bl.has_signal != td.has_signal)) else ""
            lines.append(
                f"| {date} | **Tuned** | {td.gap:.2f} | {jpy_s} | {usd_s} | {sig} | {td.decision} | {skip} | {overr} |{highlight}"
            )

        # Spacer between dates
        lines.append(f"| | | | | | | | | |")

    return "\n".join(lines)

def build_weekly_summary(records: List[RunRecord]) -> str:
    """Aggregate counts across all logged runs."""
    bl_runs = [r for r in records if r.variant == "baseline"]
    td_runs = [r for r in records if r.variant == "tuned"]

    def summarize(runs):
        return {
            "runs": len(runs),
            "with_signal": sum(1 for r in runs if r.has_signal),
            "override_fired": sum(1 for r in runs if r.override_fired),
            "skip_held": sum(1 for r in runs if r.skip_held),
            "avg_gap": f"{(sum(r.gap for r in runs)/len(runs)):.2f}" if runs else "—",
        }

    bl_sum = summarize(bl_runs)
    td_sum = summarize(td_runs)

    lines = [
        "## Weekly Summary",
        "",
        "| Metric | Baseline (1.8 threshold) | Tuned (1.65 threshold) | Change |",
        "|---|---|---|---|",
        f"| Logged Runs | {bl_sum['runs']} | {td_sum['runs']} | — |",
        f"| Runs With Signal | {bl_sum['with_signal']} | {td_sum['with_signal']} | +{td_sum['with_signal'] - bl_sum['with_signal']} |",
        f"| Override Fired | {bl_sum['override_fired']} | {td_sum['override_fired']} | +{td_sum['override_fired'] - bl_sum['override_fired']} |",
        f"| SKIP_HELD Encountered | {bl_sum['skip_held']} | {td_sum['skip_held']} | {td_sum['skip_held'] - bl_sum['skip_held']} |",
        f"| Avg GAP | {bl_sum['avg_gap']} | {td_sum['avg_gap']} | — |",
        "",
        "### Interpretation",
        "- **Runs With Signal ↑**: More entries triggered → threshold working as intended",
        "- **Override Fired ↑**: Near-consensus signals now pass",
        "- **SKIP_HELD**: Count rising → consider reentry rules (`MAX_POSITIONS=3` + gap filter)",
        "- **Avg GAP**: Consistent → market conditions comparable across runs",
    ]
    return "\n".join(lines)

def write_audit(daily_table: str, summary: str) -> None:
    header = f"""# Validation Audit — Parallel Diff Report
Generated: {datetime.utcnow():%Y-%m-%d %H:%M UTC}
Source: `logs/baseline/*.log` + `logs/tuned/*.log`

---

"""
    content = header + summary + "\n\n---\n\n" + daily_table
    AUDIT_FILE.parent.mkdir(exist_ok=True)
    AUDIT_FILE.write_text(content, encoding="utf-8")
    print(f"✅ Audit written → {AUDIT_FILE}")

def main():
    records = load_all_runs()
    if not records:
        print("⚠️ No log files found. Run ./scripts/parallel_runner.sh first.")
        return

    daily_table = build_daily_table(records)
    summary = build_weekly_summary(records)

    # Print summary to console
    print("\n" + "="*60)
    print("  WEEKLY VALIDATION SUMMARY")
    print("="*60)
    print(summary)
    print("="*60)

    # Write full markdown report
    write_audit(daily_table, summary)

if __name__ == "__main__":
    main()