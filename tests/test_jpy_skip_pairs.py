"""
Tests for JPY_SKIP_PAIRS — blacklist-style run.env pair-subsetting.

Review hardening (2026-10-01):
  R1. Skip affects NEW ENTRIES only (MAINTAIN / guardian / early-exit /
      net-exposure / MC cache must still manage open trades on skipped pairs).
  R2. Unknown pair name: --live => ABORT JPY signal generation, --dry-run =>
      WARN loudly (no silent fail-open).
  R3. All JPY pairs skipped => empty JPY signals, explicit log, never fall
      back to the auto-derived 4.
  R4. Parser: comma/space separators, case-insensitive, hyphen→underscore,
      duplicate collapse.
  R5. Regression: JPY_SKIP_PAIRS unset → TRADE_PAIRS for JPY/USD/CHF match
      their pre-feature baselines.

Uses subprocess invocation of `python scheduled_runner_v3.py ...` instead of
reimporting a module with side effects.  No real broker traffic because all
paths use --dry-run or are stopped before OANDA API hits that require auth.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
SR = str(PROJECT / "scheduled_runner_v3.py")
BASE_ENV = os.environ.copy()


def _run(extra_env: dict[str, str] | None = None,
         args: list[str] | None = None,
         timeout: int = 90) -> tuple[int, str, str]:
    env = BASE_ENV.copy()
    if extra_env:
        env.update(extra_env)
    cmd = [sys.executable, SR, "--live", "--dry-run"] + (list(args or []))
    p = subprocess.run(cmd, cwd=str(PROJECT), env=env, capture_output=True, text=True,
                       timeout=timeout)
    return p.returncode, p.stdout, p.stderr


# -------- R4 parser tests (read CONFIG lines, inspect audit TRADE_PAIRS) --------

PARSE_CASES = [
    # (env_value, expected_effective_set, extra_note)
    ("EUR_JPY,AUD_JPY", {"EUR_JPY", "AUD_JPY"}, "comma"),
    ("EUR_JPY AUD_JPY",   {"EUR_JPY", "AUD_JPY"}, "space"),
    ("eur-jpy AUD_jpy  , gbp_jpy  ", {"EUR_JPY", "AUD_JPY", "GBP_JPY"}, "case/hyphen/whitespace"),
    ("USD_JPY,USD_JPY usd-jpy", {"USD_JPY"}, "duplicates collapse"),
    ("", None, "empty => all 4 kept (no skip)"),
    ("   , ,  ", None, "whitespace-only => all 4 kept (no skip)"),
]


@pytest.mark.parametrize("val,expected,note", PARSE_CASES)
def test_r4_skip_parser_normalization(val, expected, note):
    rc, out, err = _run(
        extra_env={"JPY_SKIP_PAIRS": val},
        args=["--trade-jpy-only"],
    )
    assert rc == 0, f"non-zero exit {rc}.\nSTDERR:\n{err[-500:]}\nSTDOUT:\n{out[-1500:]}"

    m = re.search(r"\[CONFIG\] JPY_SKIP_PAIRS = (\(none\)|\[.*?\])", out)
    assert m, f"missing CONFIG JPY_SKIP_PAIRS. tail:\n{out[-1000:]}"
    cfg_val = m.group(1)

    if expected is None:
        assert cfg_val == "(none)", (note, out[-500:])
        return
    parsed_set = set(re.findall(r"'([A-Z_]+)'", cfg_val))
    assert parsed_set == expected, (note, parsed_set, expected)

    m2 = re.search(r"TRADE_PAIRS\[JPY\][^\n]*: (\[.*?\])\s*$", out, re.M)
    assert m2, f"missing audit TRADE_PAIRS[JPY]. tail:\n{out[-1000:]}"
    eff_set = set(re.findall(r"'([A-Z_]+)'", m2.group(1)))
    baseline = {"USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY"}
    assert eff_set == baseline - expected, (
        f"{note}: effective={sorted(eff_set)} vs expected kept={sorted(baseline-expected)}"
    )


# -------- R2 unknown pair: --dry-run => warn loudly; live=>abort (via driver) --------

def test_r2_unknown_pair_dry_run_warns_loudly():
    rc, out, err = _run(
        extra_env={"JPY_SKIP_PAIRS": "NZD_JPY,EUR_JPY"},
        args=["--trade-jpy-only"],
    )
    assert rc == 0, f"rc={rc}\n{err[-400:]}"
    assert "[JPY_SKIP_PAIRS] DRY-RUN: unknown JPY pair(s) in skip list" in out
    assert "WILL BE REJECTED on --live" in out
    m = re.search(r"TRADE_PAIRS\[JPY\][^\n]*: (\[.*?\])\s*$", out, re.M)
    kept = set(re.findall(r"'([A-Z_]+)'", m.group(1))) if m else set()
    assert "EUR_JPY" not in kept
    assert kept & {"USD_JPY", "GBP_JPY", "AUD_JPY"}


def test_r2_unknown_pair_live_mode_aborts_jpy_signals_via_driver(tmp_path):
    # Directly test the abort branch by calling _run_single_group() in a tiny
    # driver process with _dry_run_val forced False after argparse resolution,
    # and with an explicit JPY_SKIP_PAIRS value (unknown + known pairs).
    sr_path = str(PROJECT / "scheduled_runner_v3.py")
    driver = tmp_path / "drv.py"
    driver.write_text(
        rf"""
import sys, os, json, io, contextlib
sys.path.insert(0, {str(PROJECT)!r})  # so import config_oanda works from any cwd
os.chdir({str(PROJECT)!r})
os.environ["JPY_SKIP_PAIRS"] = "NZD_JPY,CAD_JPY,EUR_JPY"
sys.argv = ["sr", "--live"]
src = open({sr_path!r}).read()
src = src.replace('if __name__ == "__main__":', 'if __name__ == "__X__":')
ns = {{"__name__": "sr_drv", "__file__": {sr_path!r}}}
exec(compile(src, {sr_path!r}, "exec"), ns)
ns["_dry_run_val"] = False
gs = {{"USD": 1.0, "GBP": 0.5, "EUR": 0.0, "JPY": -2.0, "AUD": -1.5, "CHF": -3.0}}
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    res = ns["_run_single_group"]("JPY", ns["_strategy_groups"]["JPY"], gs)
print(json.dumps({{
    "skip_reason": res.get("skip_reason"),
    "signals_len": len(res.get("signals", [])),
    "has_live_abort_line": "LIVE MODE ABORT" in buf.getvalue(),
    "tail_stdout": buf.getvalue()[-500:],
}}))
""".lstrip()
    )
    env = BASE_ENV.copy()
    for _k in ("JPY_SKIP_PAIRS",):
        env.pop(_k, None)
    p = subprocess.run(
        [sys.executable, str(driver)],
        cwd=str(PROJECT), env=env, capture_output=True, text=True, timeout=180,
    )
    assert p.returncode == 0, (
        f"driver exit={p.returncode}\nstdout={p.stdout[-1500:]}\nstderr={p.stderr[-800:]}"
    )
    last = [ln for ln in p.stdout.splitlines() if ln.strip().startswith("{")]
    assert last, f"no JSON line printed. stdout:\n{p.stdout[-1500:]}\nstderr:\n{p.stderr[-800:]}"
    data = json.loads(last[-1])
    assert data["skip_reason"] == "JPY_SKIP_UNKNOWN_PAIRS_LIVE_ABORT", (
        data, "tail:\n", data.get("tail_stdout")
    )
    assert data["signals_len"] == 0
    assert data["has_live_abort_line"] is True


# -------- R3 all skipped => ∅ signals, explicit log, NO fallback --------

def test_r3_all_jpy_pairs_skipped_returns_empty_without_fallback():
    all4 = "USD_JPY,EUR_JPY,GBP_JPY,AUD_JPY"
    rc, out, err = _run(
        extra_env={"JPY_SKIP_PAIRS": all4},
        args=["--trade-jpy-only"],
    )
    assert rc == 0, f"rc={rc}\n{err[-400:]}"
    assert "ALL JPY pairs skipped" in out, out[-1500:]
    assert "Refusing to fall back" in out
    m = re.search(r"TRADE_PAIRS\[JPY\][^\n]*: (\[.*?\])\s*$", out, re.M)
    assert m, out[-1000:]
    kept = set(re.findall(r"'([A-Z_]+)'", m.group(1)))
    assert kept == set(), "all skipped → empty kept set, no fallback"


# -------- R1 new entries only: MAINTAIN manages skipped-pair open trades --------

def test_r1_structural_skip_only_affects_new_entry_path():
    text = (PROJECT / "scheduled_runner_v3.py").read_text()
    m = re.search(
        r"^def _maintain_group_positions\(.*?(?=\n^def |\Z)",
        text, re.M | re.S,
    )
    assert m, "could not locate _maintain_group_positions source"
    src = m.group(0)
    assert 'group_cfg["instruments"]' not in src
    assert "_gcfg[\"instruments\"]" not in src

    sys.path.insert(0, str(PROJECT))
    import scheduled_runner_v3 as sr
    fake_trade = {
        "id": "9999",
        "instrument": "EUR_JPY",
        "currentUnits": "1",
        "price": "160.000",
        "clientExtensions": {
            "id": "t_9999",
            "tag": "GEMINIAIBOT_V3::JPY-STRENGTH_EUR_JPY_BUY_20261001",
        },
    }
    assert sr.is_bot_owned_trade(fake_trade), (
        "EUR_JPY tagged trade must still be bot-owned so guardian/maintain "
        "process it even when EUR_JPY is removed from new-entry instruments."
    )


# -------- R5 regression: unset => JPY/USD/CHF baselines match --------

def test_r5_unset_three_group_trade_pairs_match_baselines():
    baseline = {
        "JPY": {"AUD_JPY", "EUR_JPY", "GBP_JPY", "USD_JPY"},
        "USD": {"AUD_USD", "EUR_USD", "GBP_USD", "NZD_USD"},
        "CHF": {"USD_CHF"},
    }
    extra_env = {"JPY_SKIP_PAIRS": ""}  # hygiene — force "(none)" semantics
    rc, out, err = _run(extra_env=extra_env, args=[])
    assert rc == 0, f"rc={rc}\n{err[-500:]}\n{out[-1000:]}"

    for gn, expected_set in baseline.items():
        m = re.search(rf"TRADE_PAIRS\[{gn:>3s}\][^\n]*: (\[.*?\])\s*$", out, re.M)
        assert m, f"missing TRADE_PAIRS[{gn}] line. tail:\n{out[-2000:]}"
        got = set(re.findall(r"'([A-Z_]+)'", m.group(1)))
        assert got == expected_set, (
            f"group {gn} got {sorted(got)} vs baseline {sorted(expected_set)}"
        )
    assert "[CONFIG] JPY_SKIP_PAIRS = (none)" in out, out[-1200:]
