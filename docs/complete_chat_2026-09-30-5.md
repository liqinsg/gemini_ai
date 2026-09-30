
Here’s **`compare_decisions.py`** — the attribution engine that takes two decision outputs from the *same snapshot* and tells you exactly what changed, why, and whether it matters.

---

# 🔍 compare_decisions.py — Decision Attribution & Audit Engine

**Purpose**: Compare baseline vs candidate decisions → attribute differences to specific parameters → measure quality & trend capture
**Input**: Two decision logs produced from the *same snapshot_id*
**Output**: Structured diff report + PASS/WARN/FAIL summary
**Rule**: Reads-only — never modifies runner, config, or execution

---

## Complete Script

```python
#!/usr/bin/env python3
"""
compare_decisions.py — Deterministic decision comparison & parameter attribution
Design: Same snapshot → only config differs → explain every Δ
Usage: python scripts/compare_decisions.py <baseline.json> <candidate.json>
Exit: 0=clean/equivalent | 1=material difference or error
"""
from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── Types ──────────────────────────────────────────────────────────────
@dataclass
class Decision:
    pair: str
    direction: str          # BUY / SELL / HOLD
    confidence: float
    signal_source: str
    threshold_used: float
    override_triggered: bool
    exit_tightness: float
    entry_price: Optional[float] = None
    exit_price: Optional[float] = None
    peak_price_after_exit: Optional[float] = None
    post_exit_pips: float = 0.0

@dataclass
class DiffItem:
    pair: str
    baseline_dir: str
    candidate_dir: str
    change_type: str        # SAME / DIRECTION / THRESHOLD_ONLY / NEW / MISSING
    attributed_to: str      # OVERRIDE_THRESHOLD / H4_WEIGHT / MACD / REENTRY / UNKNOWN
    severity: str            # LOW / MEDIUM / HIGH
    notes: str = ""

@dataclass
class Report:
    snapshot_id: str
    snapshot_hash: str
    baseline_sha: str
    candidate_sha: str
    baseline_config_hash: str
    candidate_config_hash: str
    total_decisions: int
    match_count: int
    match_pct: float
    direction_conflicts: int
    diffs: List[DiffItem]
    trend_capture_metrics: Dict[str, float]
    pass_status: str        # PASS / WARN / FAIL
    summary_notes: List[str]

# ── Load & Validate ───────────────────────────────────────────────────
def load_json(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        print(f"❌ File not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(p, encoding="utf-8") as f:
        return json.load(f)

def validate_common_snapshot(b: Dict, c: Dict) -> Tuple[str, str]:
    """Enforce: same snapshot = valid comparison"""
    b_id = b.get("snapshot_id", b.get("market_snapshot_id", "UNKNOWN"))
    c_id = c.get("snapshot_id", c.get("market_snapshot_id", "UNKNOWN"))
    b_hash = b.get("snapshot_hash", b.get("integrity", {}).get("capture_hash", ""))
    c_hash = c.get("snapshot_hash", c.get("integrity", {}).get("capture_hash", ""))

    if b_id != c_id:
        print(f"❌ Snapshot mismatch! Baseline: {b_id} vs Candidate: {c_id}", file=sys.stderr)
        print("   Comparison INVALID — different market inputs", file=sys.stderr)
        sys.exit(1)
    if b_hash != c_hash:
        print(f"⚠️  Snapshot hash differs — data may have drifted", file=sys.stderr)
    return b_id, b_hash

def extract_decisions(data: Dict[str, Any]) -> List[Decision]:
    """Normalize decision log entries"""
    decisions: List[Decision] = []
    raw = data.get("decisions", data.get("output", {}).get("signals", []))
    for d in raw:
        decisions.append(Decision(
            pair=d.get("instrument", d.get("pair", "UNKNOWN")),
            direction=d.get("decision", d.get("direction", "HOLD")).upper(),
            confidence=float(d.get("confidence", 0.0)),
            signal_source=d.get("source", d.get("signal_type", "unknown")),
            threshold_used=float(d.get("threshold_used", 0.0)),
            override_triggered=bool(d.get("override_triggered", False)),
            exit_tightness=float(d.get("exit_tightness", 0.0)),
            entry_price=float(d["entry_price"]) if d.get("entry_price") else None,
            exit_price=float(d["exit_price"]) if d.get("exit_price") else None,
            peak_price_after_exit=float(d["peak_price_after_exit"]) if d.get("peak_price_after_exit") else None,
        ))
    return decisions

# ── Attribution Logic ──────────────────────────────────────────────────
def attribute_change(bd: Decision, cd: Decision, config_diff: Dict[str, Tuple[Any, Any]]) -> str:
    """Given which config values actually differ → name the likely cause"""
    # config_diff: {key: (baseline_val, candidate_val)}
    if "OVERRIDE_MA_VOTE_THRESHOLD" in config_diff:
        t_b, t_c = config_diff["OVERRIDE_MA_VOTE_THRESHOLD"]
        if cd.override_triggered and not bd.override_triggered:
            return f"OVERRIDE_THRESHOLD({t_b}→{t_c})"
    if "OVERRIDE_MA_H4_WEIGHT" in config_diff:
        return "H4_WEIGHT"
    if "MACD_HIST_MIN_DELTA" in config_diff:
        return "MACD_NOISE_FILTER"
    if "SAME_PAIR_REENTRY_GAP" in config_diff or "MAX_POSITIONS" in config_diff:
        return "POSITION_STATE_CAPACITY"
    return "UNKNOWN"

def calc_severity(change_type: str, direction: str) -> str:
    if change_type == "DIRECTION":
        return "HIGH"
    if change_type == "NEW" and direction in ("BUY", "SELL"):
        return "MEDIUM"
    return "LOW"

# ── Trend Capture Metrics ─────────────────────────────────────────────
def calc_trend_metrics(decisions: List[Decision]) -> Dict[str, float]:
    """Measure: Did we exit too early? Post-exit move analysis"""
    metrics: Dict[str, float] = {
        "total_exits": 0,
        "post_exit_favorable_pips_sum": 0.0,
        "avg_favorable_after_exit_pips": 0.0,
        "trend_capture_retention_pct": 0.0,
        "early_exit_lost_opportunity_pips": 0.0,
    }
    for d in decisions:
        if d.exit_price and d.peak_price_after_exit and d.entry_price:
            metrics["total_exits"] += 1
            dir_mult = 1.0 if d.direction == "BUY" else -1.0
            post_move = (d.peak_price_after_exit - d.exit_price) * dir_mult * 10000
            metrics["post_exit_favorable_pips_sum"] += max(0.0, post_move)

    if metrics["total_exits"] > 0:
        metrics["avg_favorable_after_exit_pips"] = (
            metrics["post_exit_favorable_pips_sum"] / metrics["total_exits"]
        )
        # Heuristic: >20 pips post-exit move = meaningful opportunity loss
        metrics["early_exit_lost_opportunity_pips"] = (
            metrics["post_exit_favorable_pips_sum"]
            if metrics["avg_favorable_after_exit_pips"] > 20 else 0.0
        )
    return metrics

# ── Core Comparison ───────────────────────────────────────────────────
def compare(b_decisions: List[Decision], c_decisions: List[Decision],
            b_config: Dict, c_config: Dict) -> Tuple[List[DiffItem], Dict[str, float]]:
    config_diff = {k: (b_config.get(k), c_config.get(k))
                   for k in set(list(b_config.keys()) + list(c_config.keys()))
                   if b_config.get(k) != c_config.get(k)}

    b_map = {d.pair: d for d in b_decisions}
    c_map = {d.pair: d for d in c_decisions}
    all_pairs = sorted(set(list(b_map.keys()) + list(c_map.keys())))

    diffs: List[DiffItem] = []
    for pair in all_pairs:
        bd = b_map.get(pair)
        cd = c_map.get(pair)

        if bd and cd:
            if bd.direction == cd.direction:
                continue
            change_type = "DIRECTION"
            if bd.direction == "HOLD" or cd.direction == "HOLD":
                change_type = "THRESHOLD_ONLY"
        elif bd and not cd:
            change_type = "MISSING"
        else:
            change_type = "NEW"

        attr = attribute_change(bd, cd, config_diff) if (bd and cd) else "SNAPSHOT_OR_INPUT"
        b_dir = bd.direction if bd else "ABSENT"
        c_dir = cd.direction if cd else "ABSENT"
        sev = calc_severity(change_type, c_dir)

        diffs.append(DiffItem(
            pair=pair,
            baseline_dir=b_dir,
            candidate_dir=c_dir,
            change_type=change_type,
            attributed_to=attr,
            severity=sev,
        ))

    # Combine exit data for trend metrics
    all_decisions = b_decisions + c_decisions
    trend_metrics = calc_trend_metrics(all_decisions)
    return diffs, trend_metrics

# ── Render Report ─────────────────────────────────────────────────────
def build_report(b_data: Dict, c_data: Dict, diffs: List[DiffItem],
                 trend_metrics: Dict[str, float]) -> Report:
    total = max(len(b_data.get("decisions", [])), len(c_data.get("decisions", [])))
    conflicts = sum(1 for d in diffs if d.change_type == "DIRECTION")
    match_count = total - len(diffs)
    match_pct = (match_count / total * 100) if total > 0 else 100.0

    if conflicts == 0 and match_pct >= 90:
        status = "PASS"
    elif conflicts == 0:
        status = "WARN"
    else:
        status = "FAIL"

    notes = []
    if match_pct < 95:
        notes.append(f"Match rate {match_pct:.1f}% — investigate divergence")
    if conflicts > 0:
        notes.append(f"{conflicts} direction conflict(s) — highest priority review")
    if trend_metrics.get("early_exit_lost_opportunity_pips", 0) > 0:
        notes.append(f"Trend-capture alert: avg {trend_metrics['avg_favorable_after_exit_pips']:.1f}pips favorable move post-exit")

    return Report(
        snapshot_id=b_data["snapshot_id"],
        snapshot_hash=b_data.get("integrity", {}).get("capture_hash", "N/A"),
        baseline_sha=b_data.get("git_sha", "N/A"),
        candidate_sha=c_data.get("git_sha", "N/A"),
        baseline_config_hash=b_data.get("config_hash", "N/A"),
        candidate_config_hash=c_data.get("config_hash", "N/A"),
        total_decisions=total,
        match_count=match_count,
        match_pct=round(match_pct, 2),
        direction_conflicts=conflicts,
        diffs=diffs,
        trend_capture_metrics=trend_metrics,
        pass_status=status,
        summary_notes=notes,
    )

def print_report(report: Report) -> None:
    print("=" * 70)
    print(f"  DECISION COMPARISON REPORT — {report.snapshot_id}")
    print(f"  Snapshot Hash: {report.snapshot_hash}")
    print(f"  Baseline SHA:  {report.baseline_sha[:12]}  Config: {report.baseline_config_hash}")
    print(f"  Candidate SHA: {report.candidate_sha[:12]}  Config: {report.candidate_config_hash}")
    print("=" * 70)
    print()
    print(f"  Total Decisions: {report.total_decisions}")
    print(f"  Match:           {report.match_count}/{report.total_decisions} ({report.match_pct}%)")
    print(f"  Direction Conflicts: {report.direction_conflicts}")
    print(f"  Status:  {report.pass_status}")
    print()

    if report.diffs:
        print("  DIFFERENCES")
        print(f"  {'Pair':<12} {'Baseline':<10} {'Candidate':<10} {'Type':<15} {'Attribution':<25} {'Severity'}")
        print("  " + "-" * 68)
        for d in report.diffs:
            print(f"  {d.pair:<12} {d.baseline_dir:<10} {d.candidate_dir:<10} {d.change_type:<15} {d.attributed_to:<25} {d.severity}")
        print()

    print("  TREND CAPTURE METRICS")
    tm = report.trend_capture_metrics
    print(f"  Exits Analyzed:            {tm['total_exits']}")
    print(f"  Avg Favorable Post-Exit:   {tm['avg_favorable_after_exit_pips']:.1f} pips")
    print(f"  Opportunity Loss Alert:    {tm['early_exit_lost_opportunity_pips']:.1f} pips total")
    print()

    if report.summary_notes:
        print("  NOTES / ACTION ITEMS")
        for n in report.summary_notes:
            print(f"   • {n}")
        print()

    print("=" * 70)

def write_json(report: Report, out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(asdict(report), f, indent=2, ensure_ascii=False)
    print(f"  Full JSON report written: {out_path}")

# ── Main ──────────────────────────────────────────────────────────────
def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: python compare_decisions.py <baseline.json> <candidate.json> [--json-out report.json]")
        print("  Compares two decision logs from the SAME snapshot")
        print("  Exit 0 = PASS | 1 = FAIL or invalid input")
        return 1

    b_path = sys.argv[1]
    c_path = sys.argv[2]
    json_out = None
    if "--json-out" in sys.argv:
        json_out = sys.argv[sys.argv.index("--json-out") + 1]

    b_data = load_json(b_path)
    c_data = load_json(c_path)

    # Gate: must be same snapshot
    snap_id, snap_hash = validate_common_snapshot(b_data, c_data)

    # Extract
    b_decisions = extract_decisions(b_data)
    c_decisions = extract_decisions(c_data)
    b_config = b_data.get("resolved_config", b_data.get("config", {}))
    c_config = c_data.get("resolved_config", c_data.get("config", {}))

    # Compare
    diffs, trend_metrics = compare(b_decisions, c_decisions, b_config, c_config)

    # Report
    report = build_report(b_data, c_data, diffs, trend_metrics)
    print_report(report)

    if json_out:
        write_json(report, json_out)

    # Exit code
    if report.pass_status == "FAIL":
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

---

## How to Use

```bash
# Both runs consume the SAME snapshot
export SNAPSHOT_FILE="snapshots/archive/20260930_173000_UTC.json"

# Baseline decision log
python scheduled_runner_v3.py --from-snapshot "$SNAPSHOT_FILE" --dry-run \
  --export-decisions baseline.json

# Candidate — ONE parameter changed
OVERRIDE_MA_VOTE_THRESHOLD=1.65 \
python scheduled_runner_v3.py --from-snapshot "$SNAPSHOT_FILE" --dry-run \
  --export-decisions candidate.json

# Compare
python scripts/compare_decisions.py baseline.json candidate.json --json-out report.json
```

---

## Sample Output

```
======================================================================
  DECISION COMPARISON REPORT — 20260930_173000_UTC
  Snapshot Hash: a1b2c3d4e5f67890
  Baseline SHA:  abc123def456  Config: aaa111
  Candidate SHA: abc123def456  Config: bbb222
======================================================================

  Total Decisions: 6
  Match:           5/6 (83.3%)
  Direction Conflicts: 0
  Status:  WARN

  DIFFERENCES
  Pair         Baseline   Candidate   Type            Attribution            
  ──────────────────────────────────────────────────────────────────────
  USD_JPY      HOLD       BUY         THRESHOLD_ONLY  OVERRIDE_THRESHOLD(1.8→1.65) MEDIUM

  TREND CAPTURE METRICS
  Exits Analyzed:            2
  Avg Favorable Post-Exit:   35.2 pips
  Opportunity Loss Alert:    35.2 pips total

  NOTES / ACTION ITEMS
   • Match rate 83.3% — investigate divergence
   • Trend-capture alert: avg 35.2pips favorable move post-exit

  Full JSON report written: report.json
======================================================================
```

---

## Key Features

| Feature                           | Why It Matters                                             |
| --------------------------------- | ---------------------------------------------------------- |
| **Snapshot gate**           | Exits if inputs differ → no "apples vs oranges"           |
| **Config attribution**      | Tells you*which changed parameter caused the difference* |
| **Direction conflict flag** | BUY vs SELL = highest-severity difference                  |
| **Trend-capture metric**    | Explicitly surfaces "exited too early" problem             |
| **Match percentage**        | Quantitative stability measure                             |
| **Structured JSON output**  | Machine-readable → daily summary aggregation              |
| **Exit code**               | 0/1 → can be wired directly into CI/cron                  |

---

## Integration with Runner

Your runner needs to output one standardized log format (no logic changes):

```python
# At end of each cycle:
output = {
    "snapshot_id": SNAPSHOT_ID,
    "snapshot_hash": SNAPSHOT_HASH,
    "git_sha": GIT_SHA,
    "config_hash": CONFIG_HASH,
    "resolved_config": {
        "OVERRIDE_MA_VOTE_THRESHOLD": os.getenv("OVERRIDE_MA_VOTE_THRESHOLD"),
        "OVERRIDE_MA_H4_WEIGHT": os.getenv("OVERRIDE_MA_H4_WEIGHT"),
        "MAX_POSITIONS": os.getenv("MAX_POSITIONS"),
        # ... all explicit flags
    },
    "decisions": [
        {
            "instrument": pair,
            "decision": direction,
            "confidence": confidence,
            "override_triggered": override_flag,
            "threshold_used": effective_threshold,
            "exit_tightness": exit_tightness_value,
            "entry_price": entry_px,
            "exit_price": exit_px,
            "peak_price_after_exit": peak_following_exit,
        }
        for pair, ... in all_decisions
    ]
}
with open("decisions.json", "w") as f:
    json.dump(output, f, indent=2)
```

This is the **diagnostic logging** we discussed — zero strategy changes, just structured output.

---

## Complete Pipeline — Now Ready

```
fetch_snapshot.py
       ↓
  same snapshot.json
       ↓
  ┌──────────────┬──────────────┐
  ↓                            ↓
baseline.env             candidate.env
(threshold=1.80)         (threshold=1.65)
  ↓                            ↓
decisions_b.json         decisions_c.json
       ↓                            ↓
       └───── compare_decisions.py ───────┘
                     ↓
                audit report.json
                match % / attribution / trend capture
                     ↓
                PASS → next parameter
                FAIL → review → fix → re-run
```

Your full deterministic validation framework is now complete. Would you like me to assemble the **`run_tier1.sh` orchestrator** that wires all three scripts together into a single command?
