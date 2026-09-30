import random
import time
import datetime
import sys
import os
import json
import fcntl
from pathlib import Path
from typing import *

BASE_DIR = Path(__file__).resolve().parent.parent  # 项目根目录，不是 utils/ 本身
COOLDOWN_FILE = BASE_DIR / "cooldown.json"
COOLDOWN_PERIODS = 2  # 默认冷却2轮 = 30分钟

BOT_OWNED_TAG_ROOT = "GEMINIAIBOT_V3"
_BOT_TAG_SEP = "::"


def _tag_with_bot_root(strategy_tag: str) -> str:
    """把策略 tag 包装成系统自有格式：GEMINIAIBOT_V3::{原tag}"""
    if strategy_tag.startswith(BOT_OWNED_TAG_ROOT + _BOT_TAG_SEP):
        return strategy_tag
    return f"{BOT_OWNED_TAG_ROOT}{_BOT_TAG_SEP}{strategy_tag}"


def _extract_raw_tag(tag: str) -> str:
    """从系统 tag 中剥离 root 前缀，返回原策略 tag"""
    prefix = BOT_OWNED_TAG_ROOT + _BOT_TAG_SEP
    if tag.startswith(prefix):
        return tag[len(prefix):]
    return tag


def is_bot_owned_trade(trade: dict) -> bool:
    """判断 trade 是否属于本系统（检查 tag/comment 是否以 BOT_OWNED_TAG_ROOT 开头）。
    所有由本系统开的仓位的 clientExtensions.tag 都以该 root 开头。
    """
    tag = str(
        trade.get("tag")
        or trade.get("clientExtensions", {}).get("tag")
        or ""
    )
    return tag.startswith(BOT_OWNED_TAG_ROOT)

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"

sys.path.extend([str(BASE_DIR), str(BASE_DIR / "utils")])

# ─── H4 SL 参数 ───
REQUIRED_H4_CANDLES = 4
SL_OFFSET_PIPS = 20
SL_MAX_ALLOWED_PIPS = 200


def load_cooldown(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as e:
        print(f"{RED}Failed to load cooldown from {path}: {e}{RESET}")
        return {}


def save_cooldown(path, data):
    path.write_text(json.dumps(data))


def apply_jitter(min_sec: float = 1.0, max_sec: float = 8.0) -> None:
    """在脚本主逻辑开始前增加抖动延迟，避免并发请求撞车"""
    delay = random.uniform(min_sec, max_sec)
    time.sleep(delay)


def forex_market_closed():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("Europe/London"))
    wd = now.weekday()
    return (
        wd == 5  # Saturday
        or (wd == 6 and now.hour < 21)  # Sunday before open
        or (wd == 4 and now.hour >= 21)  # Friday after close
    )


def format_oanda_time(ts):
    dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))  # OANDA有Z
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def calculate_sl_zone(side: str, entry_price: float, h4_candles: list, pip_size: float):
    """
    Calculate Stop-Loss per H4 Zone Hierarchy Rules + Max SL Cap
    SELL: SL = max(H4 highs of last 4 CLOSED candles) + 20 pips
    BUY:  SL = min(H4 lows of last 4 CLOSED candles)  - 20 pips
    Cap:  If SL distance > 200 pips → abort trade (return skip=True)
    """
    # ─── Validate Input ───
    if len(h4_candles) < REQUIRED_H4_CANDLES:
        raise ValueError(
            f"Insufficient H4 candles: need ≥{REQUIRED_H4_CANDLES}, got {len(h4_candles)}"
        )

    # ─── Calculate SL per H4 Zone Hierarchy ───
    if side.upper() == "SELL":
        ref_level = max(c["high"] for c in h4_candles)
        sl_price = ref_level + (SL_OFFSET_PIPS * pip_size)
        sl_pips = (sl_price - entry_price) / pip_size

    elif side.upper() == "BUY":
        ref_level = min(c["low"] for c in h4_candles)
        sl_price = ref_level - (SL_OFFSET_PIPS * pip_size)
        sl_pips = (entry_price - sl_price) / pip_size

    else:
        raise ValueError(f"Invalid order side: '{side}' — must be 'BUY' or 'SELL'")

    # ─── Enforce Max SL Cap ───
    if sl_pips > SL_MAX_ALLOWED_PIPS:
        skip_trade = True
        print(
            f"{RED}🚫 SL TOO LARGE — ABORT | {side} | Distance: {sl_pips:.1f} pips > {SL_MAX_ALLOWED_PIPS}{RESET}"
        )
    else:
        skip_trade = False
        print(f"{GREEN}✅ SL ACCEPTED | {side} | Distance: {sl_pips:.1f} pips{RESET}")

    return sl_price, sl_pips, skip_trade


def set_emergency_lock(lock_file: Path, info: str) -> None:
    try:
        lock_file.write_text(f"{time.time()}|{info}\n")
        print(f"  [EXEC] emergency lock set: {lock_file}")
    except Exception as exc:
        print(f"  [EXEC] Failed to set emergency lock: {exc}")


def clear_emergency_lock(lock_file: Path) -> None:
    try:
        if lock_file.exists():
            lock_file.unlink()
            print("  [EXEC] emergency lock cleared")
    except Exception as exc:
        print(f"  [EXEC] Failed to clear emergency lock: {exc}")


def is_emergency_lock_active(lock_file: Path) -> bool:
    try:
        return lock_file.exists()
    except Exception:
        return False


def acquire_profile_lock(profile: int):
    lock_path = Path(f"/tmp/runner_{profile}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "a+")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(f"pid:{os.getpid()} start:{datetime.datetime.now().isoformat()}\n")
        lock_file.flush()
        return lock_file
    except BlockingIOError:
        print(f"[LOCK] Another runner (profile {profile}) is active — exiting.")
        sys.exit(0)


def make_strategy_tag(
    pair: str, side: str, prefix: str = "JPY-STRENGTH", date_fmt: str = "%Y%m%d"
) -> str:
    date_str = datetime.datetime.now(datetime.timezone.utc).strftime(date_fmt)
    raw_tag = f"{prefix}_{pair}_{side.upper()}_{date_str}"
    return _tag_with_bot_root(raw_tag)


def make_strategy_comment(entry: float, sl: float, tp: float, version: str,
                          strength_score: float | None = None,
                          strength_rank:  int   | None = None,
                          extra: dict | None = None) -> str:
    parts = [f"v{version}", f"entry={entry:.5f}", f"SL={sl:.5f}", f"TP={tp:.5f}"]
    if strength_score is not None:
        parts.append(f"sc={float(strength_score):+.4f}")
    if strength_rank is not None:
        parts.append(f"rk={int(strength_rank)}")
    if isinstance(extra, dict):
        for k, v in extra.items():
            if v is None:
                continue
            key_clean = str(k).replace("|", "_").replace("=", "_")
            val_clean = str(v).replace("|", "_").replace("=", "_")
            parts.append(f"{key_clean}={val_clean}")
    return "|".join(parts)


def parse_strategy_comment(comment: str) -> dict:
    out: dict = {}
    if not isinstance(comment, str):
        return out
    # Accept both "|" and whitespace as segment separators (historical comments
    # were space-separated "v3 entry=... SL=... TP=...").
    segments: list[str] = []
    if "|" in comment:
        segments = [tok.strip() for tok in comment.split("|") if tok.strip()]
    else:
        segments = [tok.strip() for tok in comment.split() if tok.strip()]
    for seg in segments:
        if "=" in seg:
            k, v = seg.split("=", 1)
            k = k.strip()
            v = v.strip()
            out[k] = v
            if k in ("entry", "SL", "TP", "sc"):
                try: out[k + "_f"] = float(v)
                except Exception: pass
            elif k == "rk":
                try: out[k + "_i"] = int(v)
                except Exception: pass
        else:
            # "v3" style prefix
            if seg.lower().startswith("v"):
                out["version"] = seg[1:]
    # Convenience aliases
    if "sc_f" in out:
        for alias in ("open_strength_score", "entry_strength_score", "strength_score"):
            out[alias] = out["sc_f"]
    if "rk_i" in out:
        for alias in ("open_strength_rank", "entry_strength_rank", "rank"):
            out[alias] = out["rk_i"]
    for alias in ("SL_f", "TP_f", "entry_f"):
        if alias in out:
            out[alias[:-2]] = out[alias]
    return out


def is_strategy_trade(trade: dict, prefix: str) -> bool:
    """
    判断 trade 是否属于指定策略组。
    兼容两种 tag 格式：
      1) 旧格式（无前缀 root）：{prefix}_xxx
      2) 新格式（带系统 root）：GEMINIAIBOT_V3::{prefix}_xxx
    """
    tag = str(trade.get("tag") or trade.get("clientExtensions", {}).get("tag") or "")
    raw_tag = _extract_raw_tag(tag)
    return raw_tag.startswith(prefix)


def check_pair_level_strategy_position(
    trading_core, pair: str, side: str, prefix: str
) -> tuple[bool, str]:
    try:
        for trade in trading_core.get_all_open_trades():
            if trade.get("instrument") != pair or not is_strategy_trade(trade, prefix):
                continue
            current_side = "BUY" if float(trade.get("currentUnits", 0)) > 0 else "SELL"
            reason = (
                "same-direction duplicate"
                if current_side == side.upper()
                else "pair-level protection; opposite-direction dual position prohibited"
            )
            print(
                f"  [IDEMPOTENCY] BLOCK {pair} {side}: {reason}; trade_id={trade.get('id')}"
            )
            return False, reason

        try:
            for order in trading_core.get_pending_orders():
                if order.get("instrument") != pair:
                    continue
                tag = order.get("tag", "") or order.get("clientExtensions", {}).get(
                    "tag", ""
                )
                raw_tag = _extract_raw_tag(str(tag))
                if raw_tag.startswith(prefix):
                    reason = "pending order exists → pair blocked"
                    print(f"  [IDEMPOTENCY] BLOCK {pair} {side}: {reason}")
                    return False, reason
        except Exception:
            pass

        return True, "no JPY-STRENGTH position on pair"
    except Exception as exc:
        print(
            f"  [IDEMPOTENCY] FAIL CLOSED {pair} {side}: open-trade query failed: {exc}"
        )
        return False, "open-trade query failure (fail closed)"


def sltp_decision(
    current: float | None,
    calculated: float,
    precision_tol: float = 0.001,
    update_threshold: float = 0.005,
) -> tuple[str, float | None]:
    if current is None:
        return "UPDATE_REQUIRED", None
    delta = calculated - current
    magnitude = round(abs(delta), 10)
    if magnitude < precision_tol:
        return "NO_CHANGE", delta
    if magnitude < update_threshold:
        return "MONITOR_ONLY", delta
    return "UPDATE_REQUIRED", delta


def audit_side(
    label: str,
    current: float | None,
    calculated: float,
    decision: str,
    request: str = "-",
    result: str = "-",
    order_id: str | None = None,
) -> None:
    delta = "N/A" if current is None else f"{calculated - current:+.5f}"
    print(
        f"{label}:\n  OANDA_CURRENT={'NONE' if current is None else f'{current:.5f}'}\n"
        f"  CALC={calculated:.5f}\n  DELTA={delta}\n  DECISION={decision}\n"
        f"  REQUEST={request}\n  OANDA_RESULT={result}"
        + (f"\n  ORDER_ID={order_id}" if order_id else "")
    )


def confirmation_result(
    order: dict, calculated: float, instrument: str, price_formatter=None
) -> tuple[str, str | None]:
    order_id = order.get("id")
    if price_formatter is None:
        from utils.trading_core_v2 import TradingCore

        price_formatter = TradingCore.format_price_for_instrument
    if order_id and order.get("price") == price_formatter(calculated, instrument):
        return "CONFIRMED", order_id
    return "NOT_CONFIRMED", order_id

def compute_all_cross_strengths(
    scores: dict[str, float],
) -> list[tuple[str, float, str]]:
    currencies = sorted(scores.keys())
    result = []
    for i, base in enumerate(currencies):
        for quote in currencies[i + 1 :]:
            delta = scores[base] - scores[quote]
            if delta > 0:
                pair = f"{base}_{quote}"
                direction = "BUY"
            else:
                pair = f"{quote}_{base}"
                direction = "SELL"
                delta = abs(delta)
            result.append((pair, delta, direction))
    result.sort(key=lambda x: x[1], reverse=True)
    return result

def format_cross_strength_ranking(
    cross_pairs: list[tuple[str, float, str]],
    top_n: int | None = None,
    threshold: float | None = None,
) -> str:
    lines = ["\n  === FULL CROSS STRENGTH RANKING (all currencies) ==="]
    if threshold is not None:
        lines.append(f"  Extreme threshold: |delta| ≥ {threshold}")
    lines.append("  Rank  Pair         Δ        Direction  Bar")
    lines.append("  " + "-" * 65)

    shown = cross_pairs[:top_n] if top_n else cross_pairs
    extreme_count = 0
    for rank, (pair, delta, direction) in enumerate(shown, 1):
        bar = "█" * min(int(delta * 10), 50)
        flag = ""
        if threshold is not None and delta >= threshold:
            flag = " ⚠️ EXTREME"
            extreme_count += 1
        lines.append(
            f"  {rank:>4}  {pair:<12} {delta:+.4f}   {direction:<10} {bar}{flag}"
        )

    if threshold is not None:
        lines.append(f"\n  Extreme pairs (≥ {threshold}): {extreme_count}")
    return "\n".join(lines)