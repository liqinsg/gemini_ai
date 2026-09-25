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
import fcntl
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

EMERGENCY_LOCK_FILE = Path(__file__).resolve().parent / ".emergency_close_lock_v3"


def _acquire_profile_lock(profile: int):
    lock_path = Path(f"/tmp/runner_v3_{profile}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"pid:{os.getpid()} start:{datetime.now(timezone.utc).isoformat()}\n")
        lock_file.flush()
        return lock_file
    except BlockingIOError:
        print(f"[LOCK] Another v3 runner (profile {profile}) is active — exiting.")
        sys.exit(0)


def _check_emergency_lock() -> bool:
    return EMERGENCY_LOCK_FILE.exists()


def _set_emergency_lock(info: str = "") -> None:
    EMERGENCY_LOCK_FILE.write_text(
        f"time:{datetime.now(timezone.utc).isoformat()} pid:{os.getpid()} info:{info}\n"
    )


def _clear_emergency_lock() -> None:
    if EMERGENCY_LOCK_FILE.exists():
        EMERGENCY_LOCK_FILE.unlink()


def _emergency_close_all(account_id: str = None) -> dict:
    """Close ALL strategy-tagged positions across ALL groups and set emergency lock."""
    result = {"closed": 0, "errors": []}
    try:
        trades = _trading_core.get_all_open_trades()
    except Exception as exc:
        result["errors"].append(f"fetch_failed: {exc}")
        return result

    for trade in trades:
        tags = trade.get("clientExtensions", {}).get("tag", "")
        if not any(tag in tags for tag in ["JPY-STRENGTH", "USD-STRENGTH"]):
            continue
        inst = trade.get("instrument", "")
        try:
            ok, info = close_pair_position(_trading_core, inst)
            if ok:
                result["closed"] += 1
                print(f"  [EMERGENCY] Closed {inst}: {info}")
            else:
                result["errors"].append(f"{inst}: {info}")
        except Exception as exc:
            result["errors"].append(f"{inst}: {exc}")

    _set_emergency_lock(f"emergency_close_all_v3 account={account_id} closed={result['closed']}")
    print(f"[EMERGENCY] Closed {result['closed']} positions. Lock set.")
    return result


def _parse_comment_sltp(comment: str) -> dict | None:
    """Parse 'v3.0.0|entry=0.70360|SL=0.71300|TP=0.68400' → {entry, sl, tp}."""
    if not comment or "|" not in comment:
        return None
    try:
        parts = {}
        for seg in comment.split("|"):
            if "=" in seg:
                k, v = seg.split("=", 1)
                parts[k.strip()] = float(v.strip())
        if "entry" in parts and "SL" in parts and "TP" in parts:
            return parts
    except (ValueError, AttributeError):
        pass
    return None


def _sltp_guardian(dry_run: bool = False) -> dict:
    """Audit open strategy trades → re-attach SL/TP if broker dropped them."""
    report = {"scanned": 0, "sl_repaired": 0, "tp_repaired": 0, "failed": 0}
    try:
        open_trades = _trading_core.get_all_open_trades()
    except Exception as exc:
        print(f"  [SL/TP GUARDIAN] Fetch failed: {exc}")
        report["failed"] += 1
        return report

    strategy_trades = [
        t for t in open_trades
        if any(pfx in (t.get("clientExtensions", {}).get("tag", "") or "")
               for pfx in ["JPY-STRENGTH", "USD-STRENGTH"])
    ]
    if not strategy_trades:
        return report

    print(f"  [SL/TP GUARDIAN] Auditing {len(strategy_trades)} strategy trade(s)...")

    for trade in strategy_trades:
        report["scanned"] += 1
        inst = trade.get("instrument", "")
        cid = trade.get("id", "") or trade.get("tradeID", "")
        comment = trade.get("clientExtensions", {}).get("comment", "")
        parsed = _parse_comment_sltp(comment)
        if not parsed:
            continue

        current_sl = trade.get("stopLossOrder", {}).get("price")
        current_tp = trade.get("takeProfitOrder", {}).get("price")
        want_sl = parsed["SL"]
        want_tp = parsed["TP"]
        missing_sl = current_sl is None
        missing_tp = current_tp is None

        if not missing_sl and not missing_tp:
            continue

        print(f"    T{cid} {inst}: SL missing={missing_sl} TP missing={missing_tp} → repairing")
        if dry_run:
            continue

        signal = type("S", (), {
            "pair_to_trade": inst, "action": "BUY" if float(trade.get("currentUnits", 0)) > 0 else "SELL",
            "stop_loss": want_sl, "take_profit": want_tp,
            "reasoning": f"GUARDIAN-T{cid}",
        })()
        try:
            ok = _trading_core.attach_sl_tp_to_open_trade(
                signal, instrument=inst, dry_run=False
            )
            if ok:
                if missing_sl:
                    report["sl_repaired"] += 1
                if missing_tp:
                    report["tp_repaired"] += 1
                print(f"      ✅ Repaired T{cid}")
            else:
                report["failed"] += 1
                print(f"      ❌ Repair failed T{cid}")
        except Exception as exc:
            report["failed"] += 1
            print(f"      ❌ Repair exception T{cid}: {exc}")

    print(f"  [SL/TP GUARDIAN] Scanned={report['scanned']} "
          f"SL_repaired={report['sl_repaired']} TP_repaired={report['tp_repaired']} "
          f"failed={report['failed']}")
    return report


def _print_dxy_reference(global_scores: dict | None = None) -> None:
    """Print DXY reference — try real yfinance first, fallback to homemade proxy."""
    print("\n  === DXY REFERENCE ===")
    real_dxy = None
    try:
        import yfinance as yf
        hist = yf.Ticker("DX-Y.NYB").history(period="3d", interval="1d", timeout=5)
        if len(hist) >= 2:
            latest = hist["Close"].iloc[-1]
            prev = hist["Close"].iloc[-2]
            real_dxy = {"value": round(latest, 2), "chg_pct": round((latest - prev) / prev * 100, 2)}
    except Exception:
        pass

    proxy = None
    if global_scores and "USD" in global_scores:
        usd_score = global_scores["USD"]
        proxy_trend = "STRONG ▲" if usd_score > 1.5 else "MILD ▲" if usd_score > 0.3 else "NEUTRAL ═" if usd_score > -0.3 else "MILD ▼" if usd_score > -1.5 else "STRONG ▼"
        proxy = {"score": usd_score, "trend": proxy_trend}

    if real_dxy:
        print(f"  [REAL] yfinance DXY = {real_dxy['value']} ({real_dxy['chg_pct']:+.2f}%)")
    else:
        print("  [REAL] yfinance unavailable (offline or blocked)")
    if proxy:
        print(f"  [PROXY] _global_scores['USD'] = {proxy['score']:+.4f} → USD trend: {proxy['trend']}")
    print("  === END DXY ===\n")


def _print_mc_snapshot() -> None:
    """Print Markov-Chain regime snapshot for trade pairs (reference only, no trading decision)."""
    trade_pairs_all = []
    for gcfg in _strategy_groups.values():
        quote = gcfg["quote_ccy"]
        pairs = [p for p in getattr(_config_bot, "STRENGTH_PAIRS", []) if p.endswith(f"_{quote}")]
        trade_pairs_all.extend(pairs)
    trade_pairs_all = list(dict.fromkeys(trade_pairs_all))

    if not trade_pairs_all:
        return

    print("  === MC DAILY REGIME SNAPSHOT ===")
    for pair in trade_pairs_all:
        item = _get_mc_item(pair)
        if item is None:
            print(f"  {pair:10s} | [No MC data]")
            continue
        raw_regime = item.get("regime", "N/A")
        regime = _clean_mc_regime(raw_regime)
        p_up_val = item.get("p_up", item.get("P(UP)"))
        p_down_val = item.get("p_down", item.get("P(DOWN)"))
        price = item.get("current_price", item.get("expected_price", "?"))
        _icon = {"STRONG_MOMENTUM": "⚡", "CONSOLIDATION": "🔹", "NEUTRAL": "🔸", "N/A": "❓"}.get(regime, "•")
        bias = ""
        if p_up_val is not None and p_down_val is not None:
            try:
                _pu = float(p_up_val)
                _pd = float(p_down_val)
                if _pu > _pd + 1:
                    bias = f"| ▲ UP-bias P(UP)={_pu:.1f}%"
                elif _pd > _pu + 1:
                    bias = f"| ▼ DOWN-bias P(DOWN)={_pd:.1f}%"
                else:
                    bias = f"| NEUTRAL P(UP)={_pu:.1f}% P(DOWN)={_pd:.1f}%"
            except (ValueError, TypeError):
                bias = f"| P(UP)={p_up_val}% P(DOWN)={p_down_val}%"
        print(f"  {pair:10s} | {_icon} {regime:15s} {bias} | last={price}")
    print("  === END MC SNAPSHOT ===\n")


def _clean_mc_regime(raw) -> str:
    if not raw:
        return "NEUTRAL"
    s = str(raw).upper()
    for key in ("STRONG_MOMENTUM", "CONSOLIDATION", "NEUTRAL"):
        if key in s:
            return key
    if "MOMENTUM" in s:
        return "STRONG_MOMENTUM"
    return "NEUTRAL"


def _regime_policy(mc_regime: str) -> str:
    reg = _clean_mc_regime(mc_regime)
    if reg == "CONSOLIDATION":
        return "cautious"
    if reg == "STRONG_MOMENTUM":
        return "aggressive"
    return "normal"


def _get_group_mc_regime(group_pairs: list[str]) -> str:
    """Majority-vote MC regime for a group of pairs."""
    regimes = []
    for pair in group_pairs:
        item = _get_mc_item(pair)
        if item is None:
            continue
        regimes.append(_clean_mc_regime(item.get("regime", "NEUTRAL")))

    if not regimes:
        return "NO_MC_DATA"

    from collections import Counter
    counts = Counter(regimes)
    top_regime, _ = counts.most_common(1)[0]
    return top_regime


MC_CONSOLIDATION_STRENGTH_HURDLE = 0.30
MC_CONSOLIDATION_DISABLE_OVERRIDE = True
MC_CONSOLIDATION_SL_WIDEN_FACTOR = 1.2
MC_AGGRESSIVE_SL_NARROW_FACTOR = 0.9

ENABLE_MC_CONFLICT_CHECK = getattr(_config_bot, "ENABLE_MC_CONFLICT_CHECK", True)
ENABLE_MC_CONFLICT_BLOCK = getattr(_config_bot, "ENABLE_MC_CONFLICT_BLOCK", False)
ENABLE_MC_CONFLICT_BLOCK_MODERATE = getattr(_config_bot, "ENABLE_MC_CONFLICT_BLOCK_MODERATE", False)
MC_CONFLICT_PROB_THRESHOLD = getattr(_config_bot, "MC_CONFLICT_PROB_THRESHOLD", 0.52)
MC_CONFLICT_SEVERE_THRESHOLD = getattr(_config_bot, "MC_CONFLICT_SEVERE_THRESHOLD", 0.58)

_MC_CACHE: dict = {}


def _get_mc_item(pair: str) -> dict | None:
    global _MC_CACHE
    if pair in _MC_CACHE:
        return _MC_CACHE[pair]
    try:
        from get_mc_data import get_mc_data
    except ImportError:
        return None
    try:
        mc = get_mc_data(timeframe="D", date_val="latest", pair=pair)
        if mc and isinstance(mc, list) and mc:
            item = mc[0]
        elif isinstance(mc, dict) and mc.get("pairs"):
            item = mc["pairs"][0]
        else:
            item = None
        if item is not None:
            _MC_CACHE[pair] = item
        return item
    except Exception:
        return None


def _load_mc_cache(trade_pairs: list[str]) -> None:
    global _MC_CACHE
    _MC_CACHE.clear()
    for pair in trade_pairs:
        _get_mc_item(pair)
    if _MC_CACHE:
        print(f"  [MC] Cached {len(_MC_CACHE)}/{len(trade_pairs)} trade pairs")


def _fetch_mc_direction_prob(pair: str) -> dict | None:
    item = _get_mc_item(pair)
    if item is None:
        return None
    p_up = item.get("p_up", item.get("P(UP)"))
    p_down = item.get("p_down", item.get("P(DOWN)"))
    if p_up is None or p_down is None:
        return None
    try:
        return {"p_up": float(p_up) / 100.0 if float(p_up) > 1.5 else float(p_up),
                "p_down": float(p_down) / 100.0 if float(p_down) > 1.5 else float(p_down)}
    except (ValueError, TypeError):
        return None


def _check_mc_direction_conflict(signals: list[dict]) -> list[dict]:
    if not ENABLE_MC_CONFLICT_CHECK:
        return signals
    if not signals:
        return signals
    remaining = []
    for s in signals:
        pair = s.get("pair")
        action = s.get("action", "").upper()
        mc_probs = _fetch_mc_direction_prob(pair)
        if not mc_probs:
            s["mc_conflict"] = None
            remaining.append(s)
            continue
        p_up = mc_probs["p_up"]
        p_down = mc_probs["p_down"]
        if action == "BUY":
            signal_prob = p_up
            reverse_prob = p_down
            reverse_dir = "DOWN"
        elif action == "SELL":
            signal_prob = p_down
            reverse_prob = p_up
            reverse_dir = "UP"
        else:
            s["mc_conflict"] = None
            remaining.append(s)
            continue
        s["mc_signal_prob"] = signal_prob
        s["mc_reverse_prob"] = reverse_prob
        if reverse_prob >= MC_CONFLICT_SEVERE_THRESHOLD:
            s["mc_conflict"] = "SEVERE"
            print(f"  [MC CONFLICT] {action} {pair} | Signal-P={signal_prob*100:.1f}% vs "
                  f"MC-P={reverse_prob*100:.1f}%({reverse_dir}) [SEVERE]")
            if ENABLE_MC_CONFLICT_BLOCK:
                print(f"    🚫 BLOCKED: MC CONFLICT {pair} (SEVERE, BLOCK=True)")
                continue
        elif reverse_prob >= MC_CONFLICT_PROB_THRESHOLD:
            s["mc_conflict"] = "MODERATE"
            print(f"  [MC CONFLICT] {action} {pair} | Signal-P={signal_prob*100:.1f}% vs "
                  f"MC-P={reverse_prob*100:.1f}%({reverse_dir}) [MODERATE]")
            if ENABLE_MC_CONFLICT_BLOCK and ENABLE_MC_CONFLICT_BLOCK_MODERATE:
                print(f"    🚫 BLOCKED: MC CONFLICT {pair} (MODERATE, BLOCK + MODERATE_BLOCK=True)")
                continue
        else:
            s["mc_conflict"] = None
        remaining.append(s)
    return remaining


def _apply_mc_gate(signals: list[dict], mc_regime: str, quote_ccy: str) -> list[dict]:
    """Filter/adjust signals based on MC regime."""
    signals = _check_mc_direction_conflict(signals)
    if mc_regime in ("NO_MC_DATA", "NEUTRAL", None):
        return signals

    mode = _regime_policy(mc_regime)

    if mode == "aggressive":
        print(f"  [MC GATE] {quote_ccy} → STRONG_MOMENTUM → all signals pass, SL ×{MC_AGGRESSIVE_SL_NARROW_FACTOR}")
        _widen_sl(signals, MC_AGGRESSIVE_SL_NARROW_FACTOR)
        return signals

    if mode == "cautious":
        print(f"  [MC GATE] {quote_ccy} → CONSOLIDATION → strength hurdle ≥ {MC_CONSOLIDATION_STRENGTH_HURDLE}, "
              f"override={'DISABLED' if MC_CONSOLIDATION_DISABLE_OVERRIDE else 'allowed'}, "
              f"SL ×{MC_CONSOLIDATION_SL_WIDEN_FACTOR}")
        filtered = []
        for s in signals:
            if MC_CONSOLIDATION_DISABLE_OVERRIDE and s.get("is_override"):
                print(f"    🚫 DROP OVERRIDE {s['action']} {s['pair']} (consolidation)")
                continue
            if abs(s.get("strength_score", 0)) < MC_CONSOLIDATION_STRENGTH_HURDLE:
                print(f"    🚫 DROP {s['action']} {s['pair']} strength={abs(s.get('strength_score',0)):.3f} < "
                      f"{MC_CONSOLIDATION_STRENGTH_HURDLE} (consolidation hurdle)")
                continue
            filtered.append(s)
        _widen_sl(filtered, MC_CONSOLIDATION_SL_WIDEN_FACTOR)
        return filtered

    return signals


def _widen_sl(signals: list[dict], factor: float) -> None:
    """Narrow/widen SL in-place."""
    for s in signals:
        if "stop_loss" in s and "entry" in s:
            entry = s["entry"]
            sl = s["stop_loss"]
            pip_dist = abs(entry - sl)
            new_pip = pip_dist * factor
            direction = s["action"]
            if direction == "BUY":
                s["stop_loss"] = round(entry - new_pip, 5)
            else:
                s["stop_loss"] = round(entry + new_pip, 5)
            if "take_profit" in s and entry != sl:
                rr_old = abs(entry - s["take_profit"]) / pip_dist if pip_dist else 0
                new_tp_dist = new_pip * rr_old
                s["take_profit"] = round(entry + new_tp_dist if direction == "BUY" else entry - new_tp_dist, 5)


# ==========================================
# Load strategy and run per group
# ==========================================
import custom_strategy_v3 as _strategy
from custom_strategy_v3 import BaseCurrencyTrendStrategy
from utils.strategy_helpers import build_strength_matrix, format_strength_ranking, check_ma5_alignment
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
        {"signals": [...], "strategy": BaseCurrencyTrendStrategy, "cfg": group_cfg,
         "group_name": str, "mc_regime": str}
    """
    quote_ccy = group_cfg["quote_ccy"]
    tag_prefix = group_cfg["tag_prefix"]

    trade_pairs = [p for p in getattr(_config_bot, "STRENGTH_PAIRS", []) if p.endswith(f"_{quote_ccy}")]
    mc_regime = _get_group_mc_regime(trade_pairs)
    mode = _regime_policy(mc_regime)

    print(f"\n{'─' * 70}")
    print(f"[GROUP {group_name}] quote_ccy={quote_ccy} | tag_prefix={tag_prefix} | MC={mc_regime} (mode={mode})")
    print(f"{'─' * 70}")

    strategy = BaseCurrencyTrendStrategy(
        quote_ccy=quote_ccy,
        enable_atr_min_filter=_PROFILE_CFG.get("ENABLE_ATR_MINIMUM_FILTER", True),
        atr_min_pips=_PROFILE_CFG.get("ATR_MIN_PIPS", 6.0),
        atr_min_relative_pct=_PROFILE_CFG.get("ATR_MIN_RELATIVE_PCT", 0.045),
    )

    signals = strategy.generate_signals(global_scores)

    for s in signals:
        s["is_override"] = bool(s.get("override_source"))

    signals = _apply_mc_gate(signals, mc_regime, quote_ccy)

    return {"signals": signals, "strategy": strategy, "cfg": group_cfg,
            "group_name": group_name, "mc_regime": mc_regime}


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

    def _ranking_score(e):
        base = abs(e["signal"]["strength_score"])
        mc = e["signal"].get("mc_conflict")
        if mc == "SEVERE":
            base *= 0.6
        elif mc == "MODERATE":
            base *= 0.8
        return base

    all_entries.sort(key=_ranking_score, reverse=True)

    print(f"\n{'─' * 70}")
    print("[GLOBAL] Cross-group strength ranking (best per group, conflict-weighted):")
    for i, entry in enumerate(all_entries, 1):
        sig = entry["signal"]
        _tag = "⚡OVERRIDE" if sig.get("override_source") else "  NORMAL  "
        _mc_tag = ""
        if sig.get("mc_conflict"):
            _mc_tag = f" ⚠️MC:{sig['mc_conflict']}"
        print(
            f"  {i}. [{entry['group_name']}] {sig['action']} {sig['pair']} "
            f"score={sig['strength_score']:+.4f} {_tag}{_mc_tag}"
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

    if sig.get("mc_conflict"):
        _lvl = sig["mc_conflict"]
        _sp = sig.get("mc_signal_prob", 0) * 100
        _rp = sig.get("mc_reverse_prob", 0) * 100
        print(f"     ⚠️ MC DIRECTION CONFLICT [{_lvl}]: Signal-P={_sp:.1f}% vs Reverse-P={_rp:.1f}%")

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
    if sig.get("mc_conflict") == "SEVERE":
        strategy_tag += "_MCSEV"
    elif sig.get("mc_conflict") == "MODERATE":
        strategy_tag += "_MCMOD"
    strategy_comment = make_strategy_comment(
        sig["entry"], sig["stop_loss"], sig["take_profit"], RUNNER_VERSION
    )
    if sig.get("mc_conflict"):
        _mc_p = sig.get("mc_reverse_prob", 0) * 100
        strategy_comment += f" | MC_CONFLICT:{sig['mc_conflict']}(reverse={_mc_p:.1f}%)"
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
            try:
                ma_align = check_ma5_alignment(instrument, require_aligned=3, verbose=False)
            except Exception:
                ma_align = None

            if ma_align and ma_align != side:
                print(
                    f"  [MAINTAIN {group_name}] {instrument}: OVERRIDE trade, "
                    f"MA full reversal {side}→{ma_align} → closing"
                )
                ok, info = close_pair_position(_trading_core, instrument)
                print(f"    close result: ok={ok}, info={info}")
            else:
                _status = "opposite" if ma_align else "trend-aligned/neutral"
                print(
                    f"  [MAINTAIN {group_name}] {instrument}: OVERRIDE trade "
                    f"→ safe ({_status})"
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

    _lock = _acquire_profile_lock(_args.profile)

    if _check_emergency_lock():
        print("[EMERGENCY] Lock file exists — skipping cycle. Delete .emergency_close_lock_v3 to resume.")
        return

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

    _sltp_guardian(dry_run=dry_run)

    for gname, gcfg in _strategy_groups.items():
        _maintain_group_positions(gname, gcfg, dry_run)

    print("\n[RUNNER] Building global strength matrix (shared across all groups)...")
    _global_scores = build_strength_matrix()
    print(format_strength_ranking(_global_scores))

    _print_dxy_reference(_global_scores)

    _all_trade_pairs = []
    for _gcfg in _strategy_groups.values():
        _q = _gcfg["quote_ccy"]
        _all_trade_pairs.extend(p for p in getattr(_config_bot, "STRENGTH_PAIRS", []) if p.endswith(f"_{_q}"))
    _all_trade_pairs = list(dict.fromkeys(_all_trade_pairs))
    if _all_trade_pairs:
        _load_mc_cache(_all_trade_pairs)

    _print_mc_snapshot()

    _diag_strat = BaseCurrencyTrendStrategy(
        quote_ccy=next(iter(_strategy_groups.values()))["quote_ccy"],
        enable_atr_min_filter=_PROFILE_CFG.get("ENABLE_ATR_MINIMUM_FILTER", True),
        atr_min_pips=_PROFILE_CFG.get("ATR_MIN_PIPS", 6.0),
        atr_min_relative_pct=_PROFILE_CFG.get("ATR_MIN_RELATIVE_PCT", 0.045),
    )
    print("\n" + "=" * 60)
    _diag_groups = [g["quote_ccy"] for g in _strategy_groups.values()]
    print(f"[STRATEGY DIAGNOSTICS] Runtime parameters (applied to groups: {', '.join(_diag_groups)})")
    print("=" * 60)
    s = _diag_strat
    print(f"  QUOTE_CCY            : {s.quote_ccy}")
    print(f"  TRADE_PAIRS          : {s.trade_pairs}")
    print(f"  MIN_VALID_PAIRS      : {s.MIN_VALID_PAIRS}")
    print(f"  MIN_DOMINANT_PAIRS   : {s.MIN_DOMINANT_PAIRS}")
    print(f"  MIN_STRENGTH_PASS    : {s.MIN_STRENGTH_PASSING_PAIRS}")
    print(f"  ALIGNMENT_THRESHOLD  : {s.TREND_ALIGNMENT_REQUIRED} timeframes", end="")
    _cfg_align_maj = getattr(_config_bot, "ALIGNMENT_REQUIRE_MAJORITY", None)
    _cfg_align_min = getattr(_config_bot, "ALIGNMENT_THRESHOLD_MIN", None)
    _strat_align_maj = getattr(s, "ALIGNMENT_REQUIRE_MAJORITY", None)
    _strat_align_min = getattr(s, "ALIGNMENT_THRESHOLD_MIN", None)

    if _cfg_align_maj is None:
        _cfg_align_maj = False
    if _cfg_align_min is None:
        _cfg_align_min = 2

    _tp_count = len(s.trade_pairs) if hasattr(s, "trade_pairs") and s.trade_pairs else 3

    if _strat_align_maj:
        print(f" (MAJORITY mode: need ≥{_strat_align_min}/{_tp_count} aligned)")
    else:
        print(f" (STRICT mode: all {s.TREND_ALIGNMENT_REQUIRED} must match)")

    _align_warnings = []
    if _strat_align_maj != _cfg_align_maj:
        _align_warnings.append(
            f"ALIGNMENT_REQUIRE_MAJORITY: config={_cfg_align_maj} ≠ strategy-internal={_strat_align_maj}"
        )
    if _strat_align_min != _cfg_align_min:
        _align_warnings.append(
            f"ALIGNMENT_THRESHOLD_MIN: config={_cfg_align_min} ≠ strategy-internal={_strat_align_min}"
        )

    if _align_warnings:
        print("  ⚠️ [ALIGNMENT SELF-CHECK] MISMATCH — actual behavior may differ from logged:")
        for _w in _align_warnings:
            print(f"     ⚠️ {_w}")
    else:
        if getattr(_config_bot, "ALIGNMENT_REQUIRE_MAJORITY", None) is None or getattr(_config_bot, "ALIGNMENT_THRESHOLD_MIN", None) is None:
            _miss = []
            if getattr(_config_bot, "ALIGNMENT_REQUIRE_MAJORITY", None) is None: _miss.append("ALIGNMENT_REQUIRE_MAJORITY")
            if getattr(_config_bot, "ALIGNMENT_THRESHOLD_MIN", None) is None: _miss.append("ALIGNMENT_THRESHOLD_MIN")
            print(f"  ℹ️ [ALIGNMENT SELF-CHECK] {', '.join(_miss)} missing → using strategy defaults")
        else:
            print(f"  ✅ [ALIGNMENT SELF-CHECK] OK: logged matches strategy behavior")
    print(f"  STRENGTH_CUTOFF_RATIO: {s.STRENGTH_CUTOFF_RATIO} (dynamic = max_gap × ratio)")
    print(f"  MIN_STRENGTH_SCORE   : ±{s.MIN_STRENGTH_SCORE}")
    print(f"  MIN_MARKET_STRENGTH  : {s.MIN_MARKET_STRENGTH}")
    print(f"  ENABLE_ATR_MIN_FILTER: {s.ENABLE_ATR_MIN_FILTER}")
    if s.ENABLE_ATR_MIN_FILTER:
        print(f"  ATR_MIN_PIPS         : {s.ATR_MIN_ABSOLUTE_PIPS} → absolute={s.ATR_MIN_ABSOLUTE} (×pip={s.pip})")
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
    _override_floor = getattr(_config_bot, "DOMINANCE_OVERRIDE_MEDIAN_FLOOR", None)
    if _override_floor is not None:
        print(f"  OVERRIDE_MEDIAN_FLOOR: {_override_floor} (prevents noise-triggered OVERRIDE)")
    print(f"  ── MC CONFLICT CHECK ──")
    print(f"  MC_CONFLICT_CHECK    : enabled={ENABLE_MC_CONFLICT_CHECK}")
    print(f"  MC_CONFLICT_BLOCK    : enabled={ENABLE_MC_CONFLICT_BLOCK} (SEVERE always blocked when True)")
    print(f"  MC_MODERATE_BLOCK    : enabled={ENABLE_MC_CONFLICT_BLOCK_MODERATE} (MODERATE needs BOTH BLOCK flags True)")
    print(f"  MC_CONFLICT_PROB_TH  : ≥{MC_CONFLICT_PROB_THRESHOLD*100:.0f}% reverse prob = MODERATE")
    print(f"  MC_CONFLICT_SEVERE_TH: ≥{MC_CONFLICT_SEVERE_THRESHOLD*100:.0f}% reverse prob = SEVERE")
    print(f"  ── CROSS-GROUP ──")
    print(f"  CROSS_GROUP_MUTEX    : {_cross_mutex}")
    print("=" * 60)

    all_results = []
    for gname, gcfg in _strategy_groups.items():
        try:
            result = _run_single_group(gname, gcfg, _global_scores)
            all_results.append(result)
        except Exception as exc:
            print(f"  ❌ [GROUP {gname}] Strategy execution FAILED: {type(exc).__name__}: {exc}")
            traceback.print_exc()

    _check_cross_group_mutex(all_results)

    _global_top = _pick_global_top_signal(all_results)
    if _global_top:
        _execute_single_signal(_global_top, dry_run)
    else:
        print("\n[GLOBAL] No qualifying signals from any group → HOLD")

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
    from utils.utils import apply_jitter

    apply_jitter(min_sec=1, max_sec=5)
    run_cycle()