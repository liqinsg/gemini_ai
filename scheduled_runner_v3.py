"""
Scheduled Runner v3 — Multi-Group Base-Currency Strength Strategy
=================================================================
Architecture:
    • Global ONE build_strength_matrix() → ensures consistent currency strength baseline
    • Each group (JPY, USD, ...) gets its own subset → independent dominance filter → independent signals
    • Cross-group mutex (CROSS_GROUP_MUTEX_ENABLED=False by default)
    • Strategy params ALL come from config_bot_v3.STRATEGY_GROUPS + generic v4 constants

Tag format: {GROUP_TAG_PREFIX}_{PAIR}_{SIDE}_{YYYYMMDD}
  e.g. JPY-STRENGTH_EUR_JPY_BUY_20250115
       USD-STRENGTH_EUR_USD_SELL_20250115
"""

import sys
import traceback
import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

_parser = argparse.ArgumentParser(description="Base-Currency Strength Strategy — Multi-Group v4")
_parser.add_argument("--profile", "-p", "--account", "-a", dest="profile", type=int, default=2)
_parser.add_argument("--live", action="store_true")
_parser.add_argument("--debug", type=int, choices=[1, 2, 3])
_parser.add_argument("--dry-run", action="store_true")
_parser.add_argument("--lots", type=int, default=None)

_args, _ = _parser.parse_known_args()

if _args.live:
    os.environ["OANDA_ENV"] = "live"

import config_oanda as _oanda_config
_oanda_profile = _oanda_config.get_oanda_profile("live" if _args.live else "practice")
_profile_name = f"profile{_args.profile}"
_account_suffix = "_LIVE" if _oanda_profile["env"] == "live" else ""
_account_key = f"OANDA_ACCOUNT_ID_{_args.profile}{_account_suffix}"
_account_id = getattr(_oanda_config, _account_key, "")
if not _account_id:
    print(f"[PROFILE] ERROR: {_profile_name} has no account configured in config_oanda")
    sys.exit(1)

_oanda_client = _oanda_profile["oanda_client"]
if _oanda_client is None:
    print("[PROFILE] ERROR: OANDA client construction failed — token may be missing")
    sys.exit(1)

from utils.trading_core_v2 import TradingCore, close_pair_position
_trading_core = TradingCore(oanda_client=_oanda_client, oanda_account_id=_account_id)

import config as _config
import config_bot_v3 as _config_bot

if _profile_name not in _config_bot.PROFILE_CFG:
    print(f"[PROFILE] ERROR: {_profile_name} is not defined in config_bot_v3")
    sys.exit(1)

_PROFILE_CFG = _config_bot.load_profile(_profile_name)
_PROFILE_CFG["OANDA_ACCOUNT_ID"] = _account_id
for _key, _value in _PROFILE_CFG.items():
    setattr(_config, _key, _value)

RUNNER_VERSION = "3.0.0"
PRICE_PRECISION_TOL = 0.001
STRATEGY_UPDATE_THRESHOLD = 0.005

# ==========================================
# Global strength matrix — ONE build, groups share
# ==========================================
print("[RUNNER] Building global strength matrix (shared across all groups)...")
from utils.strategy_helpers import build_strength_matrix, format_strength_ranking, check_ma5_alignment
_global_scores = build_strength_matrix()
print(format_strength_ranking(_global_scores))

# ==========================================
# Strategy diagnostics (same style as runner_v2)
# ==========================================
from custom_strategy_v3 import BaseCurrencyTrendStrategy, build_strength_matrix, format_strength_ranking
_jpy_strat = BaseCurrencyTrendStrategy(
    quote_ccy="JPY",
    enable_atr_min_filter=_PROFILE_CFG.get("ENABLE_ATR_MINIMUM_FILTER", True),
    atr_min_absolute=_PROFILE_CFG.get("ATR_MIN_ABSOLUTE", 0.060),
    atr_min_relative_pct=_PROFILE_CFG.get("ATR_MIN_RELATIVE_PCT", 0.045),
)
print("\n" + "=" * 60)
print("[STRATEGY DIAGNOSTICS] Runtime parameters")
print("=" * 60)
s = _jpy_strat
print(f"  QUOTE_CCY            : {s.quote_ccy}")
print(f"  TRADE_PAIRS          : {s.trade_pairs}")
print(f"  MIN_VALID_PAIRS      : {s.MIN_VALID_PAIRS}")
print(f"  MIN_DOMINANT_PAIRS   : {s.MIN_DOMINANT_PAIRS}")
print(f"  MIN_STRENGTH_PASS    : {s.MIN_STRENGTH_PASSING_PAIRS}")
print(f"  ALIGNMENT_THRESHOLD  : {s.TREND_ALIGNMENT_REQUIRED} timeframes")
print(f"  STRENGTH_CUTOFF_RATIO: {s.STRENGTH_CUTOFF_RATIO} (dynamic = max_gap × ratio)")
print(f"  MIN_STRENGTH_SCORE   : ±{s.MIN_STRENGTH_SCORE}")
print(f"  MIN_MARKET_STRENGTH  : {s.MIN_MARKET_STRENGTH}")
print(f"  ENABLE_ATR_MIN_FILTER: {s.ENABLE_ATR_MIN_FILTER}")
if s.ENABLE_ATR_MIN_FILTER:
    print(f"  ATR_MIN_ABSOLUTE     : {s.ATR_MIN_ABSOLUTE}")
    print(f"  ATR_MIN_RELATIVE%    : {s.ATR_MIN_RELATIVE_PCT}%")
print(f"  ENABLE_ATR_SLTP      : {getattr(_config_bot, 'ENABLE_ATR_SLTP', True)}")
print(f"  MIN_RR               : {s.MIN_RR}")
print(f"  ENABLE_MACRO_PROTEC  : {getattr(_config_bot, 'ENABLE_MACRO_PROTECTION', False)}")
print(f"  SKIP_SIDEWAYS_PAIRS  : {s.SKIP_SIDEWAYS_PAIRS}")
print(f"  TRADE_TOP_PAIRS      : {s.TRADE_TOP_PAIRS}")
print(f"  PIP_SIZE             : {s.pip}")
print(f"  ── DOMINANCE FILTER ──")
print(f"  DOMINANCE_RATIO      : enabled={s.DOMINANCE_RATIO_ENABLED} threshold={s.DOMINANCE_RATIO_THRESHOLD}")
print(f"  GAP_SEPARATION       : {s.GAP_SEPARATION_THRESHOLD}")
print(f"  ── OVERRIDE MODE ──")
print(f"  OVERRIDE             : enabled={s.DOMINANCE_OVERRIDE_ENABLED} threshold={s.DOMINANCE_OVERRIDE_THRESHOLD}")
print(f"  ── CROSS-GROUP ──")
print(f"  CROSS_GROUP_MUTEX    : {getattr(_config_bot, 'CROSS_GROUP_MUTEX_ENABLED', False)}")
print("=" * 60 + "\n")

# ==========================================
# Load strategy and run per group
# ==========================================
import custom_strategy_v3 as _strategy
from custom_strategy_v3 import BaseCurrencyTrendStrategy
from utils.oanda_state import build_client_extensions
from utils.utils import (
    acquire_profile_lock, check_pair_level_strategy_position,
    is_strategy_trade, make_strategy_tag, make_strategy_comment,
)

_strategy_groups = _config_bot.STRATEGY_GROUPS
_pip_map = _config_bot.PIP_SIZE_BY_QUOTE
_cross_mutex = _config_bot.CROSS_GROUP_MUTEX_ENABLED

_IS_LIVE = os.environ.get("OANDA_ENV", "practice").lower() in ("live", "real")
_MAX_OPEN_POSITIONS = 3 if _IS_LIVE else 5

PROJECT_ROOT = Path(__file__).resolve().parent


# -------------------------------------------
# Helper: run ONE strategy group
# -------------------------------------------
def _run_single_group(group_name: str, group_cfg: dict, global_scores: dict) -> dict:
    """
    Execute one strategy group. Returns result dict:
        {"signals": [...], "strategy": BaseCurrencyTrendStrategy, "cfg": group_cfg}
    """
    quote_ccy = group_cfg["quote_ccy"]
    tag_prefix = group_cfg["tag_prefix"]

    print(f"\n{'─' * 70}")
    print(f"[GROUP {group_name}] quote_ccy={quote_ccy} | tag_prefix={tag_prefix}")
    print(f"{'─' * 70}")

    strategy = BaseCurrencyTrendStrategy(
        quote_ccy=quote_ccy,
        enable_atr_min_filter=_PROFILE_CFG.get("ENABLE_ATR_MINIMUM_FILTER", True),
        atr_min_absolute=_PROFILE_CFG.get("ATR_MIN_ABSOLUTE", 0.060),
        atr_min_relative_pct=_PROFILE_CFG.get("ATR_MIN_RELATIVE_PCT", 0.045),
    )

    signals = strategy.generate_signals(global_scores)
    return {"signals": signals, "strategy": strategy, "cfg": group_cfg, "group_name": group_name}


# -------------------------------------------
# Cross-group mutex check
# -------------------------------------------
def _check_cross_group_mutex(all_group_results: list[dict]) -> None:
    """
    If CROSS_GROUP_MUTEX_ENABLED: detect overlapping base currencies across groups.
    E.g. EUR_JPY BUY + EUR_USD SELL → same base ccy, opposite direction → FLATTEN.
    """
    if not _cross_mutex:
        print("\n[CROSS-MUTEX] Disabled — skipping cross-group conflict check")
        return

    all_entries = []
    for gr in all_group_results:
        for sig in gr["signals"]:
            all_entries.append({
                "group": gr["group_name"],
                "pair": sig["pair"],
                "action": sig["action"],
                "base_ccy": sig["pair"].split("_")[0],
            })

    seen = {}
    for entry in all_entries:
        key = entry["base_ccy"]
        if key in seen:
            existing = seen[key]
            if existing["action"] != entry["action"]:
                print(
                    f"  ⚠️ [CROSS-MUTEX] CONFLICT: {existing['group']} {existing['pair']} {existing['action']} "
                    f"vs {entry['group']} {entry['pair']} {entry['action']} (same base={key}, opposite directions)"
                )
                # FLATTEN policy: skip conflicting pair
                entry["conflict"] = True
        else:
            seen[key] = entry

    conflicts = [e for e in all_entries if e.get("conflict")]
    if not conflicts:
        print("  ✅ [CROSS-MUTEX] No cross-group conflicts detected")
    else:
        print(f"  ⚠️ [CROSS-MUTEX] {len(conflicts)} conflicting pair(s) flagged for skip")


# -------------------------------------------
# Global ranking → pick TOP signal across all groups
# -------------------------------------------
def _pick_global_top_signal(all_results: list[dict]) -> dict | None:
    """
    Collect all signals from all groups, rank by abs(strength_score) desc.
    Apply idempotency check. Return the highest-priority executable signal.
    Returns dict: {"signal": {...}, "group_name": str, "tag_prefix": str, "group_cfg": dict}
    """
    all_entries = []
    for gr in all_results:
        if not gr["signals"]:
            continue
        best = max(gr["signals"], key=lambda s: abs(s["strength_score"]))
        all_entries.append({
            "signal": best,
            "group_name": gr["group_name"],
            "tag_prefix": gr["cfg"]["tag_prefix"],
            "group_cfg": gr["cfg"],
        })

    if not all_entries:
        return None

    all_entries.sort(key=lambda e: abs(e["signal"]["strength_score"]), reverse=True)

    print(f"\n{'─' * 70}")
    print("[GLOBAL] Cross-group strength ranking (best per group):")
    for i, entry in enumerate(all_entries, 1):
        sig = entry["signal"]
        _tag = "⚡OVERRIDE" if sig.get("override_source") else "  NORMAL  "
        print(
            f"  {i}. [{entry['group_name']}] {sig['action']} {sig['pair']} "
            f"score={sig['strength_score']:+.4f} {_tag}"
        )
    print(f"{'─' * 70}")

    top = all_entries[0]
    sig = top["signal"]
    pair = sig["pair"]
    action = sig["action"]
    tag_prefix = top["tag_prefix"]

    print(f"\n[GLOBAL] ✅ SELECTED: [{top['group_name']}] {action} {pair}")

    allowed, idem_reason = check_pair_level_strategy_position(
        _trading_core, pair, action, tag_prefix
    )
    if not allowed:
        print(f"  🚫 [GLOBAL] Idempotency blocked {pair}: {idem_reason}")
        if "opposite-direction" in idem_reason:
            ok, info = close_pair_position(_trading_core, pair)
            print(f"  [GLOBAL] Opposite close result: ok={ok}, info={info}")
        return None

    return top


# -------------------------------------------
# Execute ONE top signal
# -------------------------------------------
def _execute_single_signal(top_entry: dict, dry_run: bool) -> None:
    sig = top_entry["signal"]
    pair = sig["pair"]
    action = sig["action"]
    tag_prefix = top_entry["tag_prefix"]
    group_name = top_entry["group_name"]

    print(
        f"\n{'─' * 70}\n"
        f"[EXECUTE {group_name}] tag_prefix={tag_prefix} | {action} {pair}\n"
        f"{'─' * 70}"
    )

    print(
        f"  ✅ SIGNAL [{group_name}]: {action} {pair}\n"
        f"     Entry: {sig['entry']} | SL: {sig['stop_loss']} | TP: {sig['take_profit']} | "
        f"R:R={sig['risk_reward']:.2f}"
    )
    if sig.get("override_source"):
        print(f"     ⚡ Source: OVERRIDE ({sig['override_source']})")

    if dry_run:
        print(f"  [{group_name}] DRY-RUN → skipping order submission")
        return

    try:
        _existing = _trading_core.get_all_open_trades()
        _strategy_open = sum(
            1 for t in _existing
            if any(is_strategy_trade(t, cfg["tag_prefix"]) for cfg in _strategy_groups.values())
        )
    except Exception:
        _strategy_open = 0

    if _strategy_open >= _MAX_OPEN_POSITIONS:
        print(
            f"  🚫 [{group_name}] Position limit reached: "
            f"{_strategy_open}/{_MAX_OPEN_POSITIONS} open → HOLDING, no new entries"
        )
        return

    print(f"  [{group_name}] Open strategy positions: {_strategy_open}/{_MAX_OPEN_POSITIONS}")

    strategy_tag = make_strategy_tag(pair, action, tag_prefix)
    if sig.get("override_source"):
        strategy_tag += "_OVERRIDE"
    strategy_comment = make_strategy_comment(
        sig["entry"], sig["stop_loss"], sig["take_profit"], RUNNER_VERSION
    )
    sig["tag"], sig["comment"] = strategy_tag, strategy_comment

    if _trading_core.execute_market_trade(
        instrument=pair,
        action=action,
        units=_EFFECTIVE_LOTS,
        stop_loss=sig["stop_loss"],
        take_profit=sig["take_profit"],
        dry_run=False,
        client_extensions=build_client_extensions(
            sig, strategy_tag=strategy_tag, bar_time=sig.get("bar_time")
        ),
    ):
        print(f"  ✅ [{group_name}] Order submitted: {action} {pair}")
    else:
        print(f"  ❌ [{group_name}] Order failed: {action} {pair}")


# -------------------------------------------
# Execute group results (legacy, kept for reference)
# -------------------------------------------
def _execute_group_results(group_result: dict, dry_run: bool) -> None:
    gr = group_result
    group_name = gr["group_name"]
    tag_prefix = gr["cfg"]["tag_prefix"]
    signals = gr["signals"]
    strategy = gr["strategy"]
    pip = strategy.pip

    print(f"\n{'─' * 70}")
    print(f"[EXECUTE {group_name}] tag_prefix={tag_prefix} | valid signals={len(signals)}")
    print(f"{'─' * 70}")

    if not signals:
        print(f"  [{group_name}] No qualifying signals → HOLD")
        return

    # Sort by strength_score desc, take top per TRADE_TOP_PAIRS
    top_pairs = sorted(signals, key=lambda x: abs(x["strength_score"]), reverse=True)
    max_entries = _config_bot.MAX_NEW_ENTRIES_PER_CYCLE

    for idx, candidate in enumerate(top_pairs):
        if idx >= max_entries:
            print(f"  [{group_name}] Cycle cap reached ({max_entries} entries this cycle) → stopping")
            break

        pair = candidate["pair"]
        action = candidate["action"]

        print(
            f"\n  ✅ SIGNAL [{group_name}]: {action} {pair}\n"
            f"     Entry: {candidate['entry']} | SL: {candidate['stop_loss']} | TP: {candidate['take_profit']} | R:R={candidate['risk_reward']:.2f}"
        )
        if candidate.get("override_source"):
            print(f"     ⚡ Source: OVERRIDE ({candidate['override_source']})")

        # Check idempotency (pair-level)
        allowed, idem_reason = check_pair_level_strategy_position(
            _trading_core, pair, action, tag_prefix
        )
        if not allowed:
            print(f"  🚫 [{group_name}] Idempotency blocked {pair}: {idem_reason}")
            if "opposite-direction" in idem_reason:
                ok, info = close_pair_position(_trading_core, pair)
                print(f"  [{group_name}] Opposite close result: ok={ok}, info={info}")
            continue

        if dry_run:
            print(f"  [{group_name}] DRY-RUN → skipping order submission")
            continue

        strategy_tag = make_strategy_tag(pair, action, tag_prefix)
        if candidate.get("override_source"):
            strategy_tag += "_OVERRIDE"
        strategy_comment = make_strategy_comment(
            candidate["entry"], candidate["stop_loss"], candidate["take_profit"], RUNNER_VERSION
        )
        candidate["tag"], candidate["comment"] = strategy_tag, strategy_comment

        if _trading_core.execute_market_trade(
            instrument=pair,
            action=action,
            units=_EFFECTIVE_LOTS,
            stop_loss=candidate["stop_loss"],
            take_profit=candidate["take_profit"],
            dry_run=False,
            client_extensions=build_client_extensions(
                candidate, strategy_tag=strategy_tag, bar_time=candidate.get("bar_time")
            ),
        ):
            print(f"  ✅ [{group_name}] Order submitted: {action} {pair}")
        else:
            print(f"  ❌ [{group_name}] Order failed: {action} {pair}")


# -------------------------------------------
# SL/TP Maintenance (per group)
# -------------------------------------------
def _maintain_group_positions(group_name: str, group_cfg: dict, dry_run: bool) -> None:
    tag_prefix = group_cfg["tag_prefix"]
    quote_ccy = group_cfg["quote_ccy"]
    pip = _pip_map.get(quote_ccy, 0.0001)

    print(f"\n  [MAINTAIN {group_name}] Scanning open trades tagged {tag_prefix}*")
    try:
        open_trades = _trading_core.get_all_open_trades()
    except Exception as exc:
        print(f"  [MAINTAIN {group_name}] Fetch failed: {exc}")
        return

    for trade in open_trades:
        if not is_strategy_trade(trade, tag_prefix):
            continue
        instrument = trade.get("instrument", "")
        current_units = float(trade.get("currentUnits", 0))
        side = "BUY" if current_units > 0 else "SELL"
        tags = trade.get("clientExtensions", {}).get("tag", "")

        is_override_trade = "OVERRIDE" in tags or "override" in tags.lower()

        if is_override_trade:
            print(
                f"  [MAINTAIN {group_name}] {instrument}: tagged OVERRIDE → "
                f"skip early-exit (require_aligned=3)"
            )
            continue

        try:
            ma_align = check_ma5_alignment(instrument, require_aligned=3, verbose=False)
        except Exception:
            ma_align = None
        if ma_align and ma_align != side:
            print(
                f"  [EARLY-EXIT {group_name}] {instrument}: {side} vs MA {ma_align} → closing"
            )
            if not dry_run:
                ok, info = close_pair_position(_trading_core, instrument)
                print(f"    close result: ok={ok}, info={info}")


# -------------------------------------------
# Main cycle
# -------------------------------------------
def run_cycle(dry_run: bool = None):
    if dry_run is None:
        dry_run = _args.dry_run

    global _EFFECTIVE_LOTS
    _EFFECTIVE_LOTS = _resolve_effective_lots()

    _now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    _groups_str = ", ".join(f"{g}[{cfg['quote_ccy']}]" for g, cfg in _strategy_groups.items())
    print(
        f"\n{'=' * 70}\n"
        f"  BASE-CURRENCY STRENGTH BOT — SCHEDULED RUNNER v3.0\n"
        f"{'=' * 70}\n"
        f"  Strategy : Global strength matrix → per-group dominance filter → signal\n"
        f"  Groups   : {_groups_str}\n"
        f"  CrossMtx : {_cross_mutex} | Override  : enabled={getattr(_config_bot, 'DOMINANCE_OVERRIDE_ENABLED', True)}\n"
        f"  Profile  : {_profile_name} | Env: {_oanda_profile['env'].upper()} | DryRun: {dry_run}\n"
        f"  Account  : {_account_id}\n"
        f"  Lots     : {_EFFECTIVE_LOTS}\n"
        f"  MaxPos   : {_MAX_OPEN_POSITIONS} ({'LIVE' if _IS_LIVE else 'DEMO'})\n"
        f"  Time     : {_now}\n"
        f"{'=' * 70}"
    )

    # Step 1: run each group → collect signals
    all_results = []
    for gname, gcfg in _strategy_groups.items():
        try:
            result = _run_single_group(gname, gcfg, _global_scores)
            all_results.append(result)
        except Exception as exc:
            print(f"  ❌ [GROUP {gname}] Strategy execution FAILED: {type(exc).__name__}: {exc}")
            traceback.print_exc()

    # Step 2: cross-group mutex check
    _check_cross_group_mutex(all_results)

    # Step 3: global ranking → pick TOP signal across ALL groups → execute ONLY that one
    _global_top = _pick_global_top_signal(all_results)
    if _global_top:
        _execute_single_signal(_global_top, dry_run)
    else:
        print("\n[GLOBAL] No qualifying signals from any group → HOLD")

    # Step 4: maintenance — SL/TP guardian + early-exit on existing positions
    for gname, gcfg in _strategy_groups.items():
        _maintain_group_positions(gname, gcfg, dry_run)

    print(f"\n{'=' * 70}\n[RUNNER v3] Cycle complete\n{'=' * 70}")


def _resolve_effective_lots() -> int:
    is_live = os.environ.get("OANDA_ENV", "practice").lower() in ("live", "real")
    if _args.lots is not None:
        return _args.lots
    env_key = "LIVE_LOT_SIZE" if is_live else "DEMO_LOT_SIZE"
    env_val = os.getenv(env_key)
    if env_val and env_val.strip():
        try:
            return int(env_val)
        except ValueError:
            pass
    return _config_bot.LIVE_LOT_SIZE if is_live else _config_bot.DEMO_LOT_SIZE


if __name__ == "__main__":
    run_cycle()