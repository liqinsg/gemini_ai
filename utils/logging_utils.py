import logging
import sys
import os
from pathlib import Path
from datetime import datetime
import time

# 1. 吃宿主机/容器的 TZ 环境变量
logging.Formatter.converter = time.localtime

TZ_NAME = os.getenv("TZ", "UTC").split("/")[-1] # Singapore

# ─── Log Directory ───
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
TZ = os.getenv("TZ")
TZ_NAME = os.getenv("TZ", "Asia/Singapore").split("/")[-1]
LOG_FILE = LOG_DIR / f"fx_bot_{datetime.now().strftime('%Y%m%d')}.log"

# ─── Format ───
LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
DATE_FORMAT = f"%Y-%m-%d %H:%M:%S {TZ_NAME}"

# ─── Initialize ───
logger = logging.getLogger("fx_bot")
logger.setLevel(logging.DEBUG)
logger.propagate = False

# ─── File Handler ───
file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
file_handler.setLevel(logging.DEBUG)

# ─── Console Handler ───
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
console_handler.setLevel(logging.INFO)

# ─── Attach ───
if not logger.handlers:
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

# ──────────────────────────────────────────────────────────
# 🔇 中央静音：第三方库全关INFO，只留WARNING+
# 以后新库加在这里就行
# ──────────────────────────────────────────────────────────
SILENCE_LIST = [
    "oandapyV20",              # OANDA的HTTP请求日志
    "oandapyV20.endpoints", 
    "requests",                # requests库
    "urllib3",                 # requests底层
    "httpx",                   # 以防以后换httpx
    "httpcore",                
    "asyncio",                 # 太吵
]

for _lib in SILENCE_LIST:
    _lib_log = logging.getLogger(_lib)
    _lib_log.setLevel(logging.WARNING) # INFO以下全干掉
    _lib_log.propagate = False         # 不往上抛给root logger


# ─── 中央时间函数 ───
def now():
    """统一入口：获取本地时间"""
    return datetime.now()


def today_str():
    """统一入口：获取本地日期 20250902"""
    return now().strftime("%Y%m%d")


def fmt(dt=None, fmt_str="%Y-%m-%d %H:%M:%S"):
    """统一格式化，带时区名"""
    dt = dt or now()
    return f"{dt.strftime(fmt_str)} {TZ_NAME}"


def get_logger(name: str = None) -> logging.Logger:
    """Get a shared logger — use in every .py file"""
    if not name or name == "fx_bot":
        return logger
    # 子 logger：名字带模块名，但 handler/格式由 fx_bot 统一控制
    child = logging.getLogger(f"fx_bot.{name}")
    child.propagate = True
    return child
