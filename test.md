
## 🔧 精确修改清单（工具恢复后执行）

### 修改 1：custom_strategy_v3.py line 15-16 — 加 config_bot_v3 import

```diff
- import config as _config
- from config import (
+ import config as _config
+ import config_bot_v3 as _config_bot_v3
+ from config_bot_v3 import PIP_SIZE_BY_QUOTE
+ from config import (
```

### 修改 2：custom_strategy_v3.py line 154-166 — 改参数来源

把以下 6 处：
```python
# 旧写法（config.py 没有这些变量）
self.DOMINANCE_RATIO_ENABLED = (
    dominance_ratio_enabled if dominance_ratio_enabled is not None else _config.DOMINANCE_RATIO_ENABLED
)
self.DOMINANCE_RATIO_THRESHOLD = (
    dominance_ratio_threshold if dominance_ratio_threshold is not None else _config.DOMINANCE_RATIO_THRESHOLD
)
self.GAP_SEPARATION_THRESHOLD = (
    gap_separation_threshold if gap_separation_threshold is not None else _config.GAP_SEPARATION_THRESHOLD
)
self.DOMINANCE_OVERRIDE_ENABLED = (
    dominance_override_enabled if dominance_override_enabled is not None else _config.DOMINANCE_OVERRIDE_ENABLED
)
self.DOMINANCE_OVERRIDE_THRESHOLD = (
    dominance_override_threshold if dominance_override_threshold is not None else _config.DOMINANCE_OVERRIDE_THRESHOLD
)
```

改成：
```python
# 新写法（优先从 config_bot_v3 取，fallback 到 config.py，最后硬编码默认值）
self.DOMINANCE_RATIO_ENABLED = (
    dominance_ratio_enabled if dominance_ratio_enabled is not None
    else getattr(_config_bot_v3, "DOMINANCE_RATIO_ENABLED", getattr(_config, "DOMINANCE_RATIO_ENABLED", True))
)
self.DOMINANCE_RATIO_THRESHOLD = (
    dominance_ratio_threshold if dominance_ratio_threshold is not None
    else getattr(_config_bot_v3, "DOMINANCE_RATIO_THRESHOLD", getattr(_config, "DOMINANCE_RATIO_THRESHOLD", 2.0))
)
self.GAP_SEPARATION_THRESHOLD = (
    gap_separation_threshold if gap_separation_threshold is not None
    else getattr(_config_bot_v3, "GAP_SEPARATION_THRESHOLD", getattr(_config, "GAP_SEPARATION_THRESHOLD", 1.3))
)
self.DOMINANCE_OVERRIDE_ENABLED = (
    dominance_override_enabled if dominance_override_enabled is not None
    else getattr(_config_bot_v3, "DOMINANCE_OVERRIDE_ENABLED", getattr(_config, "DOMINANCE_OVERRIDE_ENABLED", True))
)
self.DOMINANCE_OVERRIDE_THRESHOLD = (
    dominance_override_threshold if dominance_override_threshold is not None
    else getattr(_config_bot_v3, "DOMINANCE_OVERRIDE_THRESHOLD", getattr(_config, "DOMINANCE_OVERRIDE_THRESHOLD", 2.4))
)
```

### 修改 3：删除 line 544-982 — 整块冗余 JPYTrendStrategy

从 `# JPY TREND STRATEGY — Compatibility wrapper` 注释开始，到 `_active_strategy = JPYTrendStrategy()` 前结束，**全删**。删完后文件从 1029 行 → 约 600 行。

### 修改 4：custom_strategy_v3.py line ~543（原 986）— 改 `_active_strategy`

```diff
- _active_strategy = JPYTrendStrategy()
+ _active_strategy = BaseCurrencyTrendStrategy(quote_ccy="JPY")
```

---

现在等工具恢复后我来执行这 4 步。或者你可以手动改 —— 每一步都是精确的 find-replace。要不要我等一下再试工具？        _atr_rel_pct = (atr / _entry_ref) * 100
                    if atr < self.ATR_MIN_ABSOLUTE or _atr_rel_pct < self.ATR_MIN_RELATIVE_PCT:
                        print(
                            f"    → Skip low-vol: ATR={atr:.4f} < {self.ATR_MIN_ABSOLUTE:.4f} | "
                            f"REL={_atr_rel_pct:.3f}% < {self.ATR_MIN_RELATIVE_PCT:.3f}%"
                        )
                        continue

                sl_multiplier = (
                    self.ATR_SL_MULTIPLIER_HIGH_VOL if (z_score or 0) > 1 else
                    self.ATR_SL_MULTIPLIER_LOW_VOL if (z_score or 0) < -1 else
                    self.ATR_SL_MULTIPLIER_NORMAL
                )
                sl_distance = atr * sl_multiplier
                tp_distance = sl_distance * self.ATR_RR_MULTIPLE

                entry, sl, tp = (
                    (prices["ask"], round(prices["ask"] - sl_distance, 3),
                     round(prices["ask"] + tp_distance, 3))
                    if direction == "BUY" else
                    (prices["bid"], round(prices["bid"] + sl_distance, 3),
                     round(prices["bid"] - tp_distance, 3))
                )
                sl_reference = f"ATR x{sl_multiplier}"
                target_type = f"ATR x{sl_multiplier * self.ATR_RR_MULTIPLE:.2f}"

                if ENABLE_MACRO_PROTECTION and direction == "BUY" and entry \
                    > weekly_levels["resistance"] - self.MACRO_PROTECTION_PIPS * self.pip:
                    print("    → Skip: too close to weekly resistance")
                    continue
                if ENABLE_MACRO_PROTECTION and direction == "SELL" and entry \
                    < weekly_levels["support"] + self.MACRO_PROTECTION_PIPS * self.pip:
                    print("    → Skip: too close to weekly support")
                    continue

                if direction == "BUY" and (tp <= entry or sl >= entry):
                    print("    → Skip: invalid SL/TP")
                    continue
                if direction == "SELL" and (tp >= entry or sl <= entry):
                    print("    → Skip: invalid SL/TP")
                    continue
            else:
                entry = prices["ask"] if direction == "BUY" else prices["bid"]
                if direction == "BUY":
                    sl = round(daily_levels["support"] - (SL_BUFFER_PIPS + SPREAD_PIPS) * self.pip, 3)
                    broke_out = (
                        confirmed_breakout(pair, daily_levels["resistance"], "above")
                        if ENABLE_BREAKOUT_CONFIRMATION else entry > daily_levels["resistance"]
                    )
                    if broke_out and weekly_levels["resistance"] <= entry:
                        tp = round(entry + TP_PIPS * self.pip, 3)
                        target_type = "Fixed target (stale weekly level)"
                    else:
                        tp = round(
                            (weekly_levels["resistance"] if broke_out else daily_levels["resistance"])
                            - self.FRONT_RUN_PIPS * self.pip, 3
                        )
                        target_type = "Weekly Resistance" if broke_out else "Daily Resistance"
                    sl_reference = "Daily Support"
                else:
                    sl = round(daily_levels["resistance"] + (SL_BUFFER_PIPS + SPREAD_PIPS) * self.pip, 3)
                    broke_down = (
                        confirmed_breakout(pair, daily_levels["support"], "below")
                        if ENABLE_BREAKOUT_CONFIRMATION else entry < daily_levels["support"]
                    )
                    if broke_down and weekly_levels["support"] >= entry:
                        tp = round(entry - TP_PIPS * self.pip, 3)
                        target_type = "Fixed target (stale weekly level)"
                    else:
                        tp = round(
                            (weekly_levels["support"] if broke_down else daily_levels["support"])
                            + self.FRONT_RUN_PIPS * self.pip, 3
                        )
                        target_type = "Weekly Support" if broke_down else "Daily Support"
                    sl_reference = "Daily Resistance"

            risk = abs(entry - sl)
            reward = abs(tp - entry)
            rr = reward / risk if risk > 0 else 0.0
            if rr < self.MIN_RR:
                print(f"    → Skip: R:R {rr:.2f} below {self.MIN_RR}")
                continue

            mode_tag = "[OVERRIDE]" if is_override else "[NORMAL]"
            print(f"    ✅ VALID {mode_tag}: {direction} {pair} | R:R {rr:.2f}")
            all_valid_signals.append({
                "pair": pair,
                "action": direction,
                "bar_time": _signal_bar_time(),
                "entry": entry,
                "stop_loss": sl,
                "take_profit": tp,
                "strength_score": strength_score,
                "risk_reward": round(rr, 2),
                "reasoning": f"{mode_tag} Aligned {direction} | SL={sl_reference} | TP={target_type}",
                "override_source": "dominance_ratio" if is_override else None,
                "priority": "HIGH" if is_override else "NORMAL",
            })

        # --- FINAL SELECTION ---
        valid_count = len(all_valid_signals)
        print(f"\n[SELECTION-{self.quote_ccy}] Total valid: {valid_count} | strength-passing: {strength_pass_count}")

        if strength_pass_count < self.MIN_STRENGTH_PASSING_PAIRS:
            print(
                f"  ❌ Only {strength_pass_count} pair(s) pass strength cutoff "
                f"(need ≥ {self.MIN_STRENGTH_PASSING_PAIRS}) → NO TRADE"
            )
            return []

        if valid_count < self.MIN_VALID_PAIRS:
            print(
                f"  ❌ Only {valid_count} valid pair(s) — need ≥ {self.MIN_VALID_PAIRS} → NO TRADE"
            )
            return []

        buy_count = sum(1 for s in all_valid_signals if s["action"] == "BUY")
        sell_count = sum(1 for s in all_valid_signals if s["action"] == "SELL")
        max_side = max(buy_count, sell_count)
        if max_side < self.MIN_DOMINANT_PAIRS:
            print(
                f"  ❌ Directional consensus too thin: BUYs={buy_count} SELLs={sell_count} "
                f"(need ≥ {self.MIN_DOMINANT_PAIRS} same direction) → NO TRADE"
            )
            return []

        top_pair = max(all_valid_signals, key=lambda x: abs(x["strength_score"]))
        print(
            f"  ✅ Selected (vs {self.quote_ccy}): {top_pair['action']} {top_pair['pair']} "
            f"({top_pair['strength_score']:+.4f})"
        )

        return all_valid_signals

    def rules_description(self) -> str:
        return f"""
RULES SUMMARY — quote_ccy={self.quote_ccy}:
  • Trade pairs: {self.trade_pairs}
  • Min valid pairs: {self.MIN_VALID_PAIRS}
  • Directional consensus: ≥{self.MIN_DOMINANT_PAIRS} same direction
  • Resonance: ≥{self.MIN_STRENGTH_PASSING_PAIRS} pass strength cutoff
  • Timeframes aligned: {self.TREND_ALIGNMENT_REQUIRED}
  • Dominance ratio: enabled={self.DOMINANCE_RATIO_ENABLED}, ratio≥{self.DOMINANCE_RATIO_THRESHOLD}
  • Override mode: enabled={self.DOMINANCE_OVERRIDE_ENABLED}, ratio≥{self.DOMINANCE_OVERRIDE_THRESHOLD}
  • Pip size: {self.pip}
"""
```

**然后修改 JPYTrendStrategy** 让它变成简单的兼容 wrapper（删除原来的大段代码，改为子类化）：

```python
# ==========================================
# JPY TREND STRATEGY — Compatibility wrapper
# ==========================================
class JPYTrendStrategy(BaseCurrencyTrendStrategy):
    def __init__(self, trade_pairs=None, **kwargs):
        super().__init__(quote_ccy="JPY", trade_pairs=trade_pairs, **kwargs)
```

**同时修改导入部分**，确保从 config_bot 导入新常量（在现有 `from config import ...` 之后加）：

```python
# Add at the top after existing imports:
import config_bot as _config_bot
from config_bot import (
    PIP_SIZE_BY_QUOTE,
    DOMINANCE_RATIO_ENABLED,
    DOMINANCE_RATIO_THRESHOLD,
    GAP_SEPARATION_THRESHOLD,
    DOMINANCE_OVERRIDE_ENABLED,
    DOMINANCE_OVERRIDE_THRESHOLD,
    STRATEGY_GROUPS,
    STRENGTH_PAIRS,
    STRENGTH_TIMEFRAMES,
)
```

---

### Phase 3 — scheduled_runner_v4.py：双组并行 Runner

这是一个新的 runner 文件（或基于 v3 的改造），核心改动：
1. 全局一次 `build_strength_matrix()` → 各组独立取子集
2. `_run_single_group(group_name, group_cfg)` 通用函数
3. 遍历 `STRATEGY_GROUPS` 依次执行
4. 跨组互斥框架（默认关闭）

```python
"""
Scheduled Runner v4 — Multi-Group Base-Currency Strength Strategy
=================================================================
Architecture:
    • Global ONE build_strength_matrix() → ensures consistent currency strength baseline
    • Each group (JPY, USD, ...) gets its own subset → independent dominance filter → independent signals
    • Cross-group mutex (CROSS_GROUP_MUTEX_ENABLED=False by default)
    • Strategy params ALL come from config_bot.STRATEGY_GROUPS + generic v4 constants

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
import config_bot as _config_bot

if _profile_name not in _config_bot.PROFILE_CFG:
    print(f"[PROFILE] ERROR: {_profile_name} is not defined in config_bot")
    sys.exit(1)

_PROFILE_CFG = _config_bot.load_profile(_profile_name)
_PROFILE_CFG["OANDA_ACCOUNT_ID"] = _account_id
for _key, _value in _PROFILE_CFG.items():
    setattr(_config, _key, _value)

RUNNER_VERSION = "4.0.0"
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
# Load strategy and run per group
# ==========================================
import custom_strategy_v1 as _strategy
from custom_strategy_v1 import BaseCurrencyTrendStrategy
from utils.strategy_helpers import get_atr_with_volatility_context, get_live_prices, get_support_resistance
from utils.oanda_state import build_client_extensions
from utils.utils import (
    acquire_profile_lock, check_pair_level_strategy_position,
    is_strategy_trade, make_strategy_tag, make_strategy_comment,
    close_pair_position as _close_pair, get_all_open_trades,
)
from utils.post_exit_gate import PostExitGate
from utils.logging_utils import get_logger
import json
import time

_strategy_groups = _config_bot.STRATEGY_GROUPS
_pip_map = _config_bot.PIP_SIZE_BY_QUOTE
_cross_mutex = _config_bot.CROSS_GROUP_MUTEX_ENABLED

print("\n" + "=" * 70)
print(f"[RUNNER v4] Multi-group scan | groups=list({list(_strategy_groups.keys())}) | cross_mutex={_cross_mutex}")
print("=" * 70)

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
# Execute group results (entry + maintenance)
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

        # Current MA alignment early-exit
        try:
            ma_align = check_ma5_alignment(instrument, require_aligned=2, verbose=False)
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

    print(
        f"\n{'=' * 70}\n"
        f"[RUNNER v4] {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} | "
        f"account={_account_id} | profile={_profile_name} | env={_oanda_profile['env']}\n"
        f"{'=' * 70}"
    )

    global _EFFECTIVE_LOTS
    _EFFECTIVE_LOTS = _resolve_effective_lots()

    # Pre-maintenance: scan + SL/TP + early-exit for all groups
    for gname, gcfg in _strategy_groups.items():
        _maintain_group_positions(gname, gcfg, dry_run)

    # Run each group
    all_results = []
    for gname, gcfg in _strategy_groups.items():
        try:
            result = _run_single_group(gname, gcfg, _global_scores)
            all_results.append(result)
        except Exception as exc:
            print(f"  ❌ [GROUP {gname}] Strategy execution FAILED: {type(exc).__name__}: {exc}")
            traceback.print_exc()

    # Cross-group mutex check
    _check_cross_group_mutex(all_results)

    # Execute entries for each group
    for result in all_results:
        try:
            _execute_group_results(result, dry_run)
        except Exception as exc:
            print(f"  ❌ [EXECUTE {result['group_name']}] FAILED: {type(exc).__name__}: {exc}")
            traceback.print_exc()

    print(f"\n{'=' * 70}\n[RUNNER v4] Cycle complete\n{'=' * 70}")


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
```

---

### Phase 4 — config.py 保持兼容

config.py **不需要删除旧参数**，只需在文件开头加一段说明：

```python
# config.py — v7 (legacy compatibility)
# Most strategy parameters have been migrated to config_bot.py v4 block.
# This file retains JPY-prefixed legacy params + scheduler-specific constants.
# New code should import from config_bot, not config.py.
```

---

## 📊 参数迁移对照表

| 旧名 (config.py JPY前缀) | 新名 (config_bot.py v4通用) | 说明 |
|---|---|---|
| `JPY_PIP = 0.01` | `PIP_SIZE_BY_QUOTE["JPY"] = 0.01` | 按 quote ccy 查找 |
| `JPY_ATR_PERIOD = 14` | `ATR_PERIOD = 14` | 通用 |
| `JPY_ATR_HISTORY_LOOKBACK = 50` | `ATR_HISTORY_LOOKBACK = 50` | 通用 |
| `JPY_ATR_SL_MULTIPLIER_NORMAL = 2.2` | `ATR_SL_MULTIPLIER_NORMAL = 2.2` | 通用 |
| `JPY_ATR_SL_MULTIPLIER_HIGH_VOL = 2.8` | `ATR_SL_MULTIPLIER_HIGH_VOL = 2.8` | 通用 |
| `JPY_ATR_SL_MULTIPLIER_LOW_VOL = 1.8` | `ATR_SL_MULTIPLIER_LOW_VOL = 1.8` | 通用 |
| `JPY_ATR_RR_MULTIPLE = 2.0` | `ATR_RR_MULTIPLE = 2.0` | 通用 |
| *(不存在)* | `STRATEGY_GROUPS = {"JPY": {...}, "USD": {...}}` | **新增** |
| *(不存在)* | `DOMINANCE_RATIO_THRESHOLD = 2.0` | **新增** |
| *(不存在)* | `DOMINANCE_OVERRIDE_THRESHOLD = 2.4` | **新增** |
| *(不存在)* | `CROSS_GROUP_MUTEX_ENABLED = False` | **新增** |

---

## 🚀 灰度验证步骤

1. **先跑 JPY 组单组**（临时把 `STRATEGY_GROUPS` 只留 JPY），对比 v3 结果应完全一致
2. 开启 dominance filter（默认阈值 2.0）→ 观察日志中 `ratio=` 分布
3. 开启 override（阈值 2.4）→ 观察 `⚡ OVERRIDE` 触发频率
4. 接入 USD 组 → 两组独立运行
5. 打开 `CROSS_GROUP_MUTEX_ENABLED = True` → 检测跨组 base ccy 冲突
