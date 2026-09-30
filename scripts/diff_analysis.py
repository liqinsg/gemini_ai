#!/usr/bin/env python3
import re, json, sys
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"

PATTERNS = {
    "timestamp": re.compile(r"Time\s+:\s+([^\n]+)"),
    "gap": re.compile(r"GLOBAL_MAX_GAP:\s+([\d.]+)"),
    "jpy_signal": re.compile(r"\[SELECTION-JPY\].*?NO TRADE|Selected.*?JPY"),
    "usd_signal": re.compile(r"\[SELECTION-USD\].*?NO TRADE|Selected.*?USD"),
    "chf_signal": re.compile(r"\[SELECTION-CHF\].*?NO TRADE|Selected.*?USD_CHF"),
    "jpy_vote": re.compile(r"AUD_JPY.*?No consensus: buy=([\d.]+).*sell=([\d.]+).*need\s+([\d.]+)", re.DOTALL),
    "usd_vote": re.compile(r"AUD_USD.*?No consensus: buy=([\d.]+).*sell=([\d.]+).*need\s+([\d.]+)", re.DOTALL),
    "positions": re.compile(r"open=(\d+)|MAX_POSITIONS=(\d+)"),
    "decision": re.compile(r"\[GLOBAL\](.*?HOLD|Selected.*?BUY|Selected.*?SELL)"),
    "skip_held": re.compile(r"\[SKIP HELD\].*$", re.MULTILINE),
}

def parse_log(name: str, path: Path):
    text = path.read_text()
    res = {"name": name, "path": str(path)}

    m = PATTERNS["gap"].search(text)
    res["gap"] = float(m.group(1)) if m else None

    res["jpy"] = "NO_TRADE" if "NO TRADE" in (PATTERNS["jpy_signal"].search(text) or {"": ""}).group(0) else "SIGNAL"
    res["usd"] = "NO_TRADE" if "NO TRADE" in (PATTERNS["usd_signal"].search(text) or {"": ""}).group(0) else "SIGNAL"
    res["chf"] = "NO_TRADE" if "NO TRADE" in (PATTERNS["chf_signal"].search(text) or {"": ""}).group(0) else "SIGNAL"

    for label, key in [("AUD_JPY", "jpy_vote"), ("AUD_USD", "usd_vote")]:
        m = PATTERNS[key].search(text)
        if m:
            res[f"{key}_buy"] = float(m.group(1))
            res[f"{key}_sell"] = float(m.group(2))
            res[f"{key}_thr"] = float(m.group(3))

    m = PATTERNS["decision"].search(text)
    res["decision"] = m.group(1).strip() if m else "UNKNOWN"
    res["skip_held"] = bool(PATTERNS["skip_held"].search(text))

    return res

def latest_log(d: Path) -> Path | None:
    files = sorted(d.glob("run_*.log"), reverse=True)
    return files[0] if files else None

def main():
    bl_path = latest_log(LOGS / "baseline")
    td_path = latest_log(LOGS / "tuned")

    if not bl_path or not td_path:
        print("⚠️  Run both configs first via parallel_runner.sh")
        sys.exit(1)

    bl = parse_log("BASELINE", bl_path)
    td = parse_log("TUNED", td_path)

    print("="*60)
    print(f"  DIFF COMPARISON — {datetime.utcnow():%Y-%m-%d %H:%M} UTC")
    print(f"  Baseline: {bl_path.name}")
    print(f"  Tuned:    {td_path.name}")
    print("="*60)

    rows = [
        ("GAP", bl["gap"], td["gap"], "same"),
        ("JPY Group", bl["jpy"], td["jpy"], "✅" if bl["jpy"]==td["jpy"] else "🔼 CHANGE"),
        ("USD Group", bl["usd"], td["usd"], "✅" if bl["usd"]==td["usd"] else "🔼 CHANGE"),
        ("CHF Group", bl["chf"], td["chf"], "✅" if bl["chf"]==td["chf"] else "🔼 CHANGE"),
        ("Global Decision", bl["decision"], td["decision"], "✅" if bl["decision"]==td["decision"] else "🔼 CHANGE"),
        ("SKIP_HELD", bl["skip_held"], td["skip_held"], "same"),
    ]

    for k, vb, vt, flag in rows:
        print(f"\n{k:<20} | BASELINE={vb!s:<20} TUNED={vt!s:<20} {flag}")

    print("\n── Vote Threshold Detail ──")
    for pair in ["jpy_vote", "usd_vote"]:
        if f"{pair}_buy" not in bl: continue
        b_buy, b_thr = bl[f"{pair}_buy"], bl[f"{pair}_thr"]
        t_buy, t_thr = td.get(f"{pair}_buy", b_buy), td.get(f"{pair}_thr", b_thr)
        status = "🔼 PASSES" if b_buy >= t_thr else "🚫 still blocked"
        print(f"  {pair.upper().replace('_VOTE','')}: score={b_buy:.2f} | baseline={b_thr:.2f} tuned={t_thr:.2f} → {status}")

if __name__ == "__main__":
    main()