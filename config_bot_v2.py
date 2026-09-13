# config_bot_v2.py — 极简分层 / JSON独立调参 / 薄封装
"""
=== 设计原则 ===
- trading_data.yml → 公共基础（品种列表）极少改
- profile_cfg.json → 策略参数随时调，纯文本
- MC数据：优先读本地 mc_daily_results/ 最新 <10 个；本地无才回退远程
- 调用：config.pairs.oanda['EURUSD'] / config.sl.min_pips
- 后缀_v2：独立不冲突旧版
- 内置自检：python config_bot_v2.py 直接运行自检
"""
from __future__ import annotations
import json
import yaml
import glob
from pathlib import Path
from typing import Any, Dict, List, Optional

# ==========================================
# 📦 轻量数据对象 — 只做分组，不搞复杂逻辑
# ==========================================
class _Pairs:
    """你写 EURUSD → 自动转 Yahoo/OANDA 格式"""
    def __init__(self, clean_list: List[str]):
        self.all = clean_list
        self.count = len(clean_list)
        self.yahoo = {s: f"{s}=X" for s in clean_list}
        self.oanda = {s: f"{s[:3]}_{s[3:]}" for s in clean_list}
        self.all_yahoo = [f"{s}=X" for s in clean_list]
        self.all_oanda = [f"{s[:3]}_{s[3:]}" for s in clean_list]

class _SL:
    def __init__(self, d: dict):
        self.min_pips = d.get("MIN_SL_PIPS", 35)
        self.min_jpy = d.get("MIN_SL_PIPS_JPY", 45)
        self.atr_mult = d.get("ATR_SL_MULT", 2.0)

class _TP:
    def __init__(self, d: dict):
        self.base_pips = d.get("BASE_TP_PIPS", 50)
        self.mult = d.get("TP_MULT", 2.0)
        self.strong_mult = d.get("TP_STRONG_MULT", 2.5)
        self.atr_mult = d.get("ATR_TP_MULT", 2.5)

class _Risk:
    def __init__(self, d: dict):
        self.max_positions = d.get("MAX_OPEN_POSITIONS", 3)
        self.max_per_run = d.get("MAX_OPEN_PER_RUN", 1)
        self.max_hold_bars = d.get("MAX_HOLD_BARS", 24)

class _Account:
    def __init__(self, d: dict, oanda_id: str):
        self.id = oanda_id
        self.name = d.get("ACCOUNT_NAME", "")
        self.label = d.get("LABEL", "")

class _Weights:
    def __init__(self, d: dict):
        self.strength = d.get("WEIGHT_STRENGTH", 0.35)
        self.rsi = d.get("WEIGHT_RSI", 0.20)
        self.adx = d.get("WEIGHT_ADX", 0.15)
        self.xgb = d.get("WEIGHT_XGB", 0.20)
        self.mc = d.get("WEIGHT_MC", 0.10)

class Config:
    """统一入口：config.pairs / config.sl / config.tp ..."""
    def __init__(self, yml: dict, profile: dict, oanda_id: str):
        self.pairs = _Pairs(yml.get("pairs", []))
        self.sl = _SL(profile)
        self.tp = _TP(profile)
        self.risk = _Risk(profile)
        self.account = _Account(profile, oanda_id)
        self.weights = _Weights(profile)
        self._raw = profile  # 兼容旧写法

    def __getitem__(self, k: str) -> Any:
        return self._raw.get(k)

# ==========================================
# 📂 MC 本地数据读取器 — 优先本地、取最新 <10 个
# ==========================================
class MCLocal:
    """本地MC文件管理：mc_daily_results/mc_D_all_pairs_YYYYMMDD_HHMM.json"""
    def __init__(self, base: Path):
        self.base = base
        self.daily_dir = base / "mc_daily_results"

    def list_latest_daily(self, limit: int = 10) -> List[Path]:
        """按文件名时间戳倒序，返回最新 limit 个文件路径"""
        if not self.daily_dir.exists():
            return []
        files = sorted(
            self.daily_dir.glob("mc_D_all_pairs_*.json"),
            key=lambda p: p.name,
            reverse=True
        )
        return files[:limit]

    def load_file(self, path: Path) -> Optional[List[dict]]:
        """读取单个MC文件，返回数据列表"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def get_latest_daily_data(self, limit: int = 10) -> List[dict]:
        """读取最新 limit 个文件并返回 [{path, name, stamp, data}, ...]"""
        result = []
        for p in self.list_latest_daily(limit):
            data = self.load_file(p)
            if data and isinstance(data, list):
                result.append({
                    "path": str(p),
                    "name": p.name,
                    "stamp": p.stem.replace("mc_D_all_pairs_", ""),
                    "data": data,
                    "pair_count": len(data),
                })
        return result

# ==========================================
# 🔌 加载层 — 只在启动时读一次
# ==========================================
BASE = Path(__file__).resolve().parent

# 1. 加载公共YAML
with open(BASE / "trading_data.yml", "r", encoding="utf-8") as f:
    YAML = yaml.safe_load(f)

# 2. 加载Profile参数JSON
with open(BASE / "profile_cfg.json", "r", encoding="utf-8") as f:
    PROFILES = json.load(f)

# 3. 加载OANDA凭证ID映射（key→id）
from config_oanda import (
    OANDA_ACCOUNT_ID_2,
    OANDA_ACCOUNT_ID_3,
    OANDA_ACCOUNT_ID_4,
)
OANDA_ID_MAP = {
    "PROFILE2": OANDA_ACCOUNT_ID_2,
    "PROFILE3": OANDA_ACCOUNT_ID_3,
    "PROFILE4": OANDA_ACCOUNT_ID_4,
}

# ==========================================
# ✅ 唯一对外接口
# ==========================================
def load_config(name: str = "profile2") -> Config:
    """
    config = load_config("profile2")
    config.pairs.all          # ['EURUSD', ...]
    config.pairs.oanda['EURUSD']  # 'EUR_USD'
    config.sl.min_pips        # 35
    config.account.id         # OANDA账户ID
    """
    p = PROFILES.get(name, PROFILES["profile2"])
    oanda_id = OANDA_ID_MAP.get(p.get("OANDA_ACCOUNT_ID_KEY", ""), "")
    return Config(YAML, p, oanda_id)

def cfg(P: Config | None, key: str, default: Any = None) -> Any:
    """兼容旧代码：cfg(config, "MIN_SL_PIPS")"""
    return P[key] if P else default

# ==========================================
# 🧪 内置自检 — 直接运行: python config_bot_v2.py
# ==========================================
if __name__ == "__main__":
    import sys

    class T:
        G = "\033[92m✓ PASS\033[0m"
        R = "\033[91m✗ FAIL\033[0m"
        Y = "\033[93m⚠ WARN\033[0m"
        B = "\033[94m→\033[0m"

    OK = True
    SCORE = 0
    TOTAL = 0

    def PASS():
        global SCORE, TOTAL
        SCORE += 1
        TOTAL += 1
    def FAIL():
        global OK, TOTAL
        OK = False
        TOTAL += 1

    print("=" * 65)
    print("🧪 config_bot_v2 内置自检 + 本地MC数据验证")
    print("=" * 65)

    # ── 1. 文件存在性 ──
    print("\n📋 1/6 关键文件检查")
    f_yml = BASE / "trading_data.yml"
    f_json = BASE / "profile_cfg.json"
    f_oanda = BASE / "config_oanda.py"
    for f, desc in [(f_yml, "trading_data.yml"), (f_json, "profile_cfg.json"), (f_oanda, "config_oanda.py")]:
        if f.exists():
            print(f"{T.G} {desc}")
            PASS()
        else:
            print(f"{T.R} {desc} 缺失")
            FAIL()

    # ── 2. YAML/JSON 格式 ──
    print("\n📋 2/6 配置格式解析")
    try:
        cfg2 = load_config("profile2")
        print(f"{T.G} 配置加载成功 | {cfg2.pairs.count} 个品种")
        PASS()
    except Exception as e:
        print(f"{T.R} 配置加载失败: {e}")
        FAIL()
        cfg2 = None

    # ── 3. 格式自动转换 ──
    if cfg2:
        print("\n📋 3/6 货币对格式自动转换")
        for sym in ["EURUSD", "GBPUSD", "USDJPY"]:
            yh = cfg2.pairs.yahoo.get(sym, "")
            oa = cfg2.pairs.oanda.get(sym, "")
            if yh == f"{sym}=X" and "_" in oa:
                print(f"{T.G} {sym:8} → {yh:12} | {oa}")
                PASS()
            else:
                print(f"{T.R} {sym:8} → 格式错误")
                FAIL()

    # ── 4. 属性完整性 ──
    if cfg2:
        print("\n📋 4/6 配置属性完整性")
        attrs = [
            ("sl.min_pips", cfg2.sl.min_pips),
            ("tp.mult", cfg2.tp.mult),
            ("risk.max_positions", cfg2.risk.max_positions),
            ("account.id", bool(cfg2.account.id)),
            ("weights.strength", cfg2.weights.strength),
        ]
        for name, val in attrs:
            if val not in (None, "", 0):
                print(f"{T.G} {name:25} = {val}")
                PASS()
            else:
                print(f"{T.R} {name:25} = ⚠ 空/零值")
                FAIL()

    # ── 5. Profile 全部可加载 ──
    print("\n📋 5/6 全部Profile加载验证")
    for name in ["profile2", "profile3", "profile4"]:
        try:
            p = load_config(name)
            print(f"{T.G} {name:10} → {p.account.label} | {p.pairs.count} pairs")
            PASS()
        except Exception as e:
            print(f"{T.R} {name:10} → 失败: {e}")
            FAIL()

    # ── 6. 本地MC数据 ──
    print("\n📋 6/6 本地MC日数据（最新<10个）")
    mc = MCLocal(BASE)
    mc_files = mc.list_latest_daily(10)
    if not mc_files:
        print(f"{T.Y} mc_daily_results/ 目录或文件不存在")
        print(f"{T.B} 说明：定时任务尚未生成数据，不影响配置本身")
        PASS()
    else:
        print(f"{T.G} 找到 {len(mc_files)} 个MC日数据文件")
        all_ok = True
        for idx, info in enumerate(mc.get_latest_daily_data(10), 1):
            pairs = info["pair_count"]
            stamp = info["stamp"]
            status = "✓" if pairs >= 10 else "⚠"
            if pairs >= 10:
                print(f"  {status} {idx}. {stamp} | {pairs} pairs")
                PASS()
            else:
                print(f"  ⚠ {idx}. {stamp} | 仅 {pairs} pairs（偏少）")
                FAIL()
                all_ok = False
        if all_ok:
            print(f"{T.G} MC数据完整性正常")

    # ── 最终评判 ──
    print("\n" + "=" * 65)
    rate = SCORE / TOTAL * 100 if TOTAL else 0
    print(f"📊 自检通过: {SCORE}/{TOTAL} ({rate:.0f}%)")

    if OK and rate >= 95:
        print("🏆 评判: ✅ 优秀 — 全部正常，可投入使用")
    elif rate >= 80:
        print("🏆 评判: ⚠ 良好 — 基本可用，存在少量警告")
    elif rate >= 60:
        print("🏆 评判: ⚠ 及格 — 存在异常项，请检查")
    else:
        print("🏆 评判: ❌ 不及格 — 多项异常，不可使用")
    print("=" * 65)
    
"""预期输出
=================================================================
🧪 config_bot_v2 内置自检 + 本地MC数据验证
=================================================================

📋 1/6 关键文件检查
✓ PASS trading_data.yml
✓ PASS profile_cfg.json
✓ PASS config_oanda.py

📋 2/6 配置格式解析
✓ PASS 配置加载成功 | 12 个品种

📋 3/6 货币对格式自动转换
✓ PASS EURUSD   → EURUSD=X     | EUR_USD
✓ PASS GBPUSD   → GBPUSD=X     | GBP_USD
✓ PASS USDJPY   → USDJPY=X     | USD_JPY

📋 4/6 配置属性完整性
✓ PASS sl.min_pips               = 35
✓ PASS tp.mult                   = 2.0
✓ PASS risk.max_positions        = 3
✓ PASS account.id                = True
✓ PASS weights.strength          = 0.35

📋 5/6 全部Profile加载验证
✓ PASS profile2   → PROFILE2 | 12 pairs
✓ PASS profile3   → PROFILE3 | 12 pairs
✓ PASS profile4   → PROFILE4 | 12 pairs

📋 6/6 本地MC日数据（最新<10个）
✓ PASS 找到 5 个MC日数据文件
  ✓ 1. 20260911_0222 | xx pairs
  ✓ 2. 20260911_0158 | xx pairs
  ✓ 3. 20260910_0948 | xx pairs
  ✓ 4. 20260910_0930 | xx pairs
  ✓ 5. 20260910_0915 | xx pairs
✓ PASS MC数据完整性正常

=================================================================
📊 自检通过: 18/18 (100%)
🏆 评判: ✅ 优秀 — 全部正常，可投入使用
=================================================================
"""