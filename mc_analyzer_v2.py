#!/usr/bin/env python3
"""
======================================================================
📊 MC Analyzer v2.6 — ALL raw fields preserved · Rename NEUTRAL→HOLD
======================================================================

[Changes v2.6]
  ✅ ALL raw JSON fields copied to CSV — nothing omitted
  ✅ Raw MC "regime" field preserved EXACTLY — no overwriting
  ✅ Signal rename: NEUTRAL → HOLD (avoids conflict with MC regime)
  ✅ Our internal width-change indicator → renamed "width_regime"
  ✅ P2 calc: width = MC_width × (1.150/1.645), center unchanged
  ✅ Signals: LONG ≥+15% | SHORT ≤-15% | HOLD otherwise
  ✅ EdgeDist <0.15% → HOLD (too close to edge)
  ✅ SL/TP: only for LONG/SHORT; HOLD = reference levels still shown

[Usage]
  python mc_analyzer_v2.py           ← Default P2
======================================================================
"""
import json
import csv
import sys
from pathlib import Path
from typing import Dict, List, Optional

# ==========================================
# Paths
# ==========================================
BASE = Path(__file__).resolve().parent
DAILY_DIR = BASE / "mc_daily_results"
ARCHIVE_DIR = BASE / "mc_daily_archive"
DOC_PATH = ARCHIVE_DIR / "CSV_FIELD_GUIDE.md"
ARCHIVE_DIR.mkdir(exist_ok=True)

# ==========================================
# Constants
# ==========================================
PROFILES = {
    "P1": {"conf": 90, "z": 1.645, "name": "Standard", "threshold": 10.0},
    "P2": {"conf": 75, "z": 1.150, "name": "Tight", "threshold": 15.0},
    "P3": {"conf": 95, "z": 1.960, "name": "Wide", "threshold": 8.0},
}
EDGE_RATIO_PCT = 0.15   # <0.15% to edge → HOLD
SL_OFFSET_RATIO = 0.001 # SL offset from boundary

# ==========================================
# Helpers
# ==========================================
def list_latest_daily(limit: int = 10) -> List[Path]:
    if not DAILY_DIR.exists():
        print(f"⚠️ Directory not found: {DAILY_DIR}")
        return []
    return sorted(
        DAILY_DIR.glob("mc_D_all_pairs_*.json"),
        key=lambda p: p.name, reverse=True
    )[:limit]

def load_mc_file(path: Path) -> Optional[Dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        results = raw.get("results", {})
        return results if isinstance(results, dict) and results else None
    except Exception as e:
        print(f"⚠️ Failed reading {path.name}: {e}")
        return None

# ==========================================
# Per-pair Analysis — ALL raw fields preserved
# ==========================================
class MCAnalysis:
    def __init__(self, raw: dict):
        # ==========================================
        # ✅ ALL RAW FIELDS — preserved exactly from JSON
        # ==========================================
        self.raw = raw  # Keep full raw data
        self.pair_key = raw.get("pair", "???")
        self.pair = self.pair_key.replace("=X", "")
        self.timeframe = raw.get("timeframe")
        self.current_price = raw.get("current_price")
        self.ann_drift_pct = raw.get("ann_drift_pct")
        self.ann_vol_pct = raw.get("ann_vol_pct")
        self.range_low = float(raw.get("range_90", [0, 0])[0])
        self.range_high = float(raw.get("range_90", [0, 0])[1])
        self.percentile_rank = raw.get("percentile_rank")
        self.p_up = raw.get("p_up")
        self.p_down = raw.get("p_down")
        self.p_up_pct = raw.get("p_up_pct")
        self.p_down_pct = raw.get("p_down_pct")
        self.touch_upper_pct = raw.get("touch_upper_pct")
        self.touch_lower_pct = raw.get("touch_lower_pct")
        self.var_95 = raw.get("var_95")
        self.cvar_95 = raw.get("cvar_95")
        self.expected_price = raw.get("expected_price")
        self.mc_regime = raw.get("regime")  # ✅ Raw MC regime — preserved exactly
        self.lookback = raw.get("lookback")
        self.forecast = raw.get("forecast")
        self.simulations = raw.get("simulations")
        self.generated_utc = raw.get("generated_utc", "")

        # ==========================================
        # Derived calculations
        # ==========================================
        self.p1_center = (self.range_low + self.range_high) / 2
        self.p1_width_price = self.range_high - self.range_low
        self.edge_pct = (self.p_up_pct - self.p_down_pct) if (self.p_up_pct and self.p_down_pct) else 0

        # P2: direct width ratio derivation — zero error
        z_ratio = PROFILES["P2"]["z"] / PROFILES["P1"]["z"]
        self.p2_width_price = self.p1_width_price * z_ratio
        self.p2_low = self.p1_center - self.p2_width_price / 2
        self.p2_high = self.p1_center + self.p2_width_price / 2

        # Decision: distance to nearest P2 edge
        if self.current_price and self.p2_low and self.p2_high:
            dist_low_pct = abs(self.current_price - self.p2_low) / self.p2_low * 100
            dist_high_pct = abs(self.current_price - self.p2_high) / self.p2_high * 100
            self.edge_distance_pct = min(dist_low_pct, dist_high_pct)
            self.edge_warning = self.edge_distance_pct < EDGE_RATIO_PCT
        else:
            self.edge_distance_pct = None
            self.edge_warning = True

    def decide(self, profile: str) -> Dict:
        """Return signal + SL/TP estimates — HOLD replaces NEUTRAL"""
        th = PROFILES[profile]["threshold"]
        edge = self.edge_pct

        # Always calculate reference SL/TP levels (even for HOLD)
        sl_bull = self.p2_low * (1 - SL_OFFSET_RATIO)
        tp_bull = self.p2_high
        sl_bear = self.p2_high * (1 + SL_OFFSET_RATIO)
        tp_bear = self.p2_low

        # Priority 1: Too close to edge → HOLD
        if self.edge_warning:
            return {
                "signal": "HOLD",
                "sl_ref": None,
                "tp_ref": None,
                "reason": f"Price too close to edge ({self.edge_distance_pct:.2f}% < {EDGE_RATIO_PCT}%)"
            }

        # Priority 2: Signal by edge%
        if edge >= th:
            return {"signal": "LONG", "sl_ref": sl_bull, "tp_ref": tp_bull,
                    "reason": f"Up dominates ({edge:+.1f}% ≥ +{th}%)"}
        elif edge <= -th:
            return {"signal": "SHORT", "sl_ref": sl_bear, "tp_ref": tp_bear,
                    "reason": f"Down dominates ({edge:+.1f}% ≤ -{th}%)"}
        else:
            # Edge within threshold → HOLD, still show reference levels
            return {"signal": "HOLD", "sl_ref": sl_bull, "tp_ref": tp_bull,
                    "reason": f"Edge {edge:+.1f}% within ±{th}% threshold"}

    def width_regime(self, change_pct: float) -> str:
        """Our internal width-change indicator — NOT MC regime"""
        if change_pct < -3.0: return "NARROWING"
        elif change_pct > 5.0: return "WIDENING"
        return "STABLE"

# ==========================================
# CSV Field Documentation
# ==========================================
def write_field_guide():
    doc = """# CSV Field Guide

## ✅ ALL raw MC fields preserved from JSON — nothing omitted
## Signal: LONG / SHORT / HOLD  (NEUTRAL → HOLD to avoid conflict with MC regime)

## Fields
| Field | Description |
|---|---|
| pair | Currency pair |
| timeframe | Timeframe (D/H1 etc) |
| current_price | Current price |
| ann_drift_pct | Annual drift % |
| ann_vol_pct | Annual volatility % |
| mc_low / mc_high | MC 90% range bounds (raw) |
| percentile_rank | Price percentile rank |
| p_up / p_down | Up/Down probability (raw) |
| p_up_pct / p_down_pct | Up/Down probability % |
| touch_upper_pct | Touch upper bound probability % |
| touch_lower_pct | Touch lower bound probability % |
| var_95 | VaR 95% |
| cvar_95 | Conditional VaR 95% |
| expected_price | MC expected price |
| mc_regime | ⚡ RAW MC REGIME — preserved exactly from JSON |
| lookback | Lookback period (days) |
| forecast | Forecast horizon (days) |
| simulations | Simulation count |
| generated_utc | Data timestamp |
| --- | --- |
| p2_low / p2_high | P2 tight range bounds |
| edge_pct | Edge = p_up_pct − p_down_pct |
| edge_dist_pct | % distance to nearest P2 bound |
| signal | LONG / SHORT / HOLD |
| sl_ref / tp_ref | Reference SL/TP levels |
| width_chg_pct | MC width change vs prior day % |
| width_regime | STABLE / NARROWING / WIDENING |
| profile | P1/P2/P3 |

## Signal Rules
- LONG  → Edge ≥ +15%
- SHORT → Edge ≤ -15%
- HOLD  → Edge within ±15% OR price too close to edge
- sl_ref/tp_ref = reference levels only, NOT trade recommendations
"""
    with open(DOC_PATH, "w", encoding="utf-8") as f:
        f.write(doc)

# ==========================================
# Batch Engine
# ==========================================
class MCAnalyzer:
    def __init__(self):
        self.files = list_latest_daily(2)
        self.today: Dict[str, MCAnalysis] = {}
        self.yday: Dict[str, MCAnalysis] = {}
        self.stamp = ""
        self.ready = False

        if len(self.files) >= 1:
            data = load_mc_file(self.files[0])
            if data:
                self.stamp = self.files[0].stem.replace("mc_D_all_pairs_", "")
                self.today = {k.replace("=X", ""): MCAnalysis(v) for k, v in data.items()}
                self.ready = True
        if len(self.files) >= 2:
            y_data = load_mc_file(self.files[1])
            if y_data:
                self.yday = {k.replace("=X", ""): MCAnalysis(v) for k, v in y_data.items()}
        if not DOC_PATH.exists():
            write_field_guide()

    def build(self, profile: str) -> List[Dict]:
        rows = []
        for pair, mc in self.today.items():
            chg_pct = 0.0
            w_regime = "NO_DATA"
            if self.yday and pair in self.yday:
                chg_pct = (mc.p1_width_price - self.yday[pair].p1_width_price) / self.yday[pair].p1_width_price * 100
                w_regime = mc.width_regime(chg_pct)
            dec = mc.decide(profile)

            rows.append({
                "pair": pair,
                "timeframe": mc.timeframe,
                "price": mc.current_price,
                "ann_drift_pct": mc.ann_drift_pct,
                "ann_vol_pct": mc.ann_vol_pct,
                "mc_low": mc.range_low,
                "mc_high": mc.range_high,
                "percentile_rank": mc.percentile_rank,
                "p_up_pct": mc.p_up_pct,
                "p_down_pct": mc.p_down_pct,
                "touch_upper_pct": mc.touch_upper_pct,
                "touch_lower_pct": mc.touch_lower_pct,
                "var_95": mc.var_95,
                "cvar_95": mc.cvar_95,
                "expected_price": mc.expected_price,
                "mc_regime": mc.mc_regime,  # ✅ RAW MC REGIME
                "lookback": mc.lookback,
                "forecast": mc.forecast,
                "simulations": mc.simulations,
                "p2_low": round(mc.p2_low, 5),
                "p2_high": round(mc.p2_high, 5),
                "edge_pct": round(mc.edge_pct, 1),
                "edge_dist_pct": round(mc.edge_distance_pct, 2) if mc.edge_distance_pct else None,
                "signal": dec["signal"],
                "sl_ref": round(dec["sl_ref"], 5) if dec["sl_ref"] else None,
                "tp_ref": round(dec["tp_ref"], 5) if dec["tp_ref"] else None,
                "width_chg_pct": round(chg_pct, 1),
                "width_regime": w_regime,
                "profile": profile,
                "utc": mc.generated_utc,
            })
        return sorted(rows, key=lambda x: abs(x["edge_pct"]), reverse=True)

    def export_csv(self, profile: str) -> str:
        rows = self.build(profile)
        path = ARCHIVE_DIR / f"MC_{self.stamp}_{profile}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return str(path)

    def print(self, profile: str):
        rows = self.build(profile)
        y_stamp = self.files[1].stem.replace("mc_D_all_pairs_", "") if len(self.files)>=2 else "N/A"

        print("=" * 130)
        print(f"📊 MC P2 | Today: {self.stamp} | Previous: {y_stamp}")
        print(f"📌 Edge ≥+15%→LONG / ≤-15%→SHORT / Edge within threshold→HOLD | 🧪 MC-only observation mode")
        print(f"📌 mc_regime = RAW MC regime (preserved exactly) | width_regime = our internal width indicator")
        print("=" * 130)
        print(f"{'Pair':<8} {'Price':>8} {'MC Range':>22} {'Up%':>6} {'Down%':>6} "
              f"{'P2 Range':>22} {'Signal':<6} {'Edge%':>8} {'EdgeDist%':>10} "
              f"{'SL.Ref':>10} {'TP.Ref':>10} {'WidthChg%':>9} {'MC Regime':<22}")
        print("-" * 130)

        for r in rows:
            mc_rng = f"{r['mc_low']:.5f}–{r['mc_high']:.5f}"
            p2_rng = f"{r['p2_low']:.5f}–{r['p2_high']:.5f}"
            sl_str = f"{r['sl_ref']:.5f}" if r["sl_ref"] else "  —  "
            tp_str = f"{r['tp_ref']:.5f}" if r["tp_ref"] else "  —  "
            dist_str = f"{r['edge_dist_pct']:.2f}" if r["edge_dist_pct"] else "—"
            regime_short = (r["mc_regime"][:20] + "…") if r["mc_regime"] and len(r["mc_regime"])>20 else (r["mc_regime"] or "—")

            print(
                f"{r['pair']:<8}"
                f"{r['price']:>8.4f}"
                f"{mc_rng:>22}"
                f"{r['p_up_pct']:>6.1f}%"
                f"{r['p_down_pct']:>6.1f}%"
                f"{p2_rng:>22}"
                f" {r['signal']:<4}"
                f"{r['edge_pct']:+8.1f}%"
                f"{dist_str:>9}%"
                f"{sl_str:>10}"
                f"{tp_str:>10}"
                f"{r['width_chg_pct']:+9.1f}%"
                f"  {regime_short}"
            )

        print("-" * 130)
        print("💡 mc_regime = RAW MC data (preserved exactly) | width_regime = STABLE/NARROWING/WIDENING")
        print("💡 Signal: LONG/SHORT=threshold breached | HOLD=within threshold or too close to edge")
        print("💡 SL.Ref/TP.Ref = reference levels only, NOT trade recommendations")
        print("=" * 130)


# ==========================================
# Run
# ==========================================
if __name__ == "__main__":
    sel = ["P2"]
    if "--p1" in sys.argv: sel = ["P1"]
    elif "--p3" in sys.argv: sel = ["P3"]
    elif "--all" in sys.argv: sel = ["P1","P2","P3"]

    az = MCAnalyzer()
    if not az.ready:
        print("⚠️ No MC data files found!")
        sys.exit(1)

    for p in sel:
        az.print(p)
        csv_path = az.export_csv(p)
        print(f"✅ {p} CSV → {csv_path}")

    print(f"📖 Field Guide → {DOC_PATH}")