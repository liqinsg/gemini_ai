import random
import time
import datetime
import sys
import os
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # 项目根目录，不是 utils/ 本身
COOLDOWN_FILE = BASE_DIR / "cooldown.json"
COOLDOWN_PERIODS = 2  # 默认冷却2轮 = 30分钟

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