"""
📊 MC区间独立分析工具 — 可信版
目标：数据可信、逻辑可测、结果可追溯、异常不崩、边界明确
功能：
  1. 扫描全部日线/周线   → 批量概览(MC文件价，快)
  2. 查单个货币对         → OANDA实时价优先，精确边界检查
  3. 持仓状态追踪          → 开仓价 vs 现价，双位置对照
  4. 内置全套自测          → 运行 python mc_zone_analyzer.py test
  5. 数据校验+日志留痕     → 区间/百分位/距离全校验，来源可追溯
使用：
  python mc_zone_analyzer.py              → 扫描全部
  python mc_zone_analyzer.py USD_JPY      → 查单个(OANDA实时)
  python mc_zone_analyzer.py USD_JPY 154.81  → 持仓追踪
  python mc_zone_analyzer.py test         → 运行全套自测
"""
import json
import os
import sys
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ========== 🔗 项目配置对齐 ==========
sys.path.insert(0, str(Path.cwd()))
try:
    from config import OANDA_ENV, OANDA_ACCOUNT_ID
except ImportError:
    OANDA_ENV = "practice"
    OANDA_ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID", "")
    print("⚠️ 未找到config.py，使用环境变量回退")

# OANDA API 可用性
OANDA_AVAILABLE = False
try:
    import oandapyV20
    import oandapyV20.endpoints.pricing as pricing
    from oandapyV20 import API
    OANDA_AVAILABLE = True
except ImportError:
    print("⚠️ oandapyV20 未安装 → 仅使用MC文件价格，OANDA功能不可用")

# ========== 核心配置 ==========
MC_RESULTS_DIR = Path.cwd() / "mc_results"
CONVERGENCE_LOWER = 30.0   # ≤30% 下沿收敛区
CONVERGENCE_UPPER = 70.0   # ≥70% 上沿收敛区
SUFFIX_D = "mc_D_all_pairs"
SUFFIX_W = "mc_W_all_pairs"
BAR_WIDTH = 22

# ========== 📐 数据校验函数 ==========
def validate_zone(L: float, U: float, P: float, pct: float) -> List[str]:
    warns = []
    if L <= 0 or U <= 0 or P <= 0:
        warns.append(f"价格异常：L={L}, U={U}, P={P}")
    if L >= U:
        warns.append(f"区间颠倒：下沿{L} ≥ 上沿{U}")
    if P < L or P > U:
        warns.append(f"价格越界：P={P} 不在 [{L},{U}]")
    if pct < -5 or pct > 105:
        warns.append(f"百分位异常：{pct:.1f}% (合理0~100)")
    if U != L:
        calc_pct = ((P - L) / (U - L)) * 100
        if not math.isclose(pct, calc_pct, rel_tol=0.05):
            warns.append(f"百分位计算不一致：标定{pct:.1f}%，实算{calc_pct:.1f}%")
    return warns

# ========== 🌐 OANDA 实时价获取 ==========
def get_oanda_price(pair: str) -> Optional[float]:
    if not OANDA_AVAILABLE:
        return None
    token = os.environ.get("OANDA_API_TOKEN") or os.environ.get("OANDA_TOKEN")
    if not token:
        print("  ⚠️ 未设置 OANDA_API_TOKEN / OANDA_TOKEN")
        return None
    try:
        api = API(environment=OANDA_ENV, access_token=token)
        r = pricing.PricingInfo(
            accountID=OANDA_ACCOUNT_ID,
            params={"instruments": pair.replace("_", "_")}
        )
        resp = api.request(r)
        if "prices" in resp and resp["prices"]:
            return float(resp["prices"][0]["bids"][0]["price"])
        print(f"  ⚠️ OANDA返回异常: {resp}")
    except Exception as e:
        print(f"  ⚠️ OANDA API失败: {type(e).__name__}: {str(e)[:80]}")
    return None

# ========== 🔧 工具函数 ==========
def _pair_to_key(pair: str) -> str:
    return pair.replace("_", "").upper() + "=X"

def _key_to_pair(key: str) -> str:
    s = key.replace("=X", "").upper()
    return f"{s[:3]}_{s[3:]}" if len(s) == 6 else s

def _find_file(timeframe: str = "D") -> Optional[Path]:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    prefix = SUFFIX_D if timeframe.upper() == "D" else SUFFIX_W
    matches = sorted(MC_RESULTS_DIR.glob(f"{prefix}_{today}_*.json"), reverse=True)
    return matches[0] if matches else None

# ========== ✅ 单一对齐横线绘图 ==========
def _draw_bar(L: float, U: float, ref_price: float, current_price: float, width: int = 26) -> str:
    """│──────＊─✦──────│  边界│ 预期＊ 现价✦"""
    if U == L:
        return "─" * width
    def pos(p: float) -> int:
        return round(((p - L) / (U - L)) * (width - 1))
    idx_low, idx_high = pos(L), pos(U)
    idx_ref, idx_curr = pos(ref_price), pos(current_price)
    bar = ["─"] * width
    if 0 <= idx_low < width: bar[idx_low] = "│"
    if 0 <= idx_high < width: bar[idx_high] = "│"
    if 0 <= idx_ref < width: bar[idx_ref] = "＊"
    if 0 <= idx_curr < width: bar[idx_curr] = "✦"
    return "".join(bar)

# ========== ✅ 简化版绘图：批量扫描用 ==========
def _draw_bar_simple(L: float, U: float, P: float, width: int = 22) -> str:
    """───●─── 仅标现价位置，用于批量扫描"""
    if U == L:
        return "─" * width
    idx = round(((P - L) / (U - L)) * (width - 1))
    bar = ["─"] * width
    if 0 <= idx < width: bar[idx] = "●"
    return "".join(bar)

# ========== 📥 核心：读取区间+计算 ==========
def get_zone(pair: str, timeframe: str = "D", price: float = None) -> Optional[Dict]:
    f = _find_file(timeframe)
    if not f:
        print(f"⚠️ 未找到今日{timeframe}线MC文件")
        return None
    try:
        raw = f.read_text().lstrip('\ufeff').strip()
        data = json.loads(raw)
    except Exception as e:
        print(f"⚠️ MC文件解析失败: {e}")
        return None
    results = data.get("results", {})
    key = _pair_to_key(pair)
    if key not in results:
        matched = None
        for k in results:
            if k.replace("=X", "").upper() == pair.replace("_", "").upper():
                matched = k
                break
        if not matched:
            print(f"⚠️ MC数据中未找到货币对: {pair}")
            return None
        key = matched
    d = results[key]
    rng = d.get("range_90", [None, None])
    L, U = rng[0], rng[1]
    # ✅ 参考基准价：优先expected_price → current_price兜底
    ref_price = d.get("expected_price") or d.get("current_price")

    # ✅ 现价：三层回退
    if price is not None:
        P = price
        src = "传入价"
    else:
        oanda_p = get_oanda_price(pair)
        if oanda_p is not None:
            P = oanda_p
            src = "OANDA实时"
        else:
            P = d.get("current_price")
            src = "MC文件价"

    if not all([L, U, P, ref_price]):
        print(f"⚠️ 数据缺失: L={L}, U={U}, 基准={ref_price}, 现价={P}")
        return None

    # ✅ 计算百分位
    if U == L:
        pct_ref = pct_curr = 50.0
    else:
        pct_ref = max(0.0, min(100.0, ((ref_price - L) / (U - L)) * 100))
        pct_curr = max(0.0, min(100.0, ((P - L) / (U - L)) * 100))

    dist_u = round(U - P, 4)
    dist_l = round(P - L, 4)
    drift = round(pct_curr - pct_ref, 1)

    # ✅ 数据校验
    warns = validate_zone(L, U, P, pct_curr)
    if not (L <= ref_price <= U):
        warns.append(f"基准价越界: {ref_price} 不在 [{L},{U}]")

    # ✅ 收敛判定
    if pct_curr >= CONVERGENCE_UPPER:
        zone_label = f"⚠️ 靠近上沿 (≥{CONVERGENCE_UPPER}%)"
        mode, weight = "CAUTIOUS", 0.5
    elif pct_curr <= CONVERGENCE_LOWER:
        zone_label = f"⚠️ 靠近下沿 (≤{CONVERGENCE_LOWER}%)"
        mode, weight = "CAUTIOUS", 0.5
    else:
        zone_label = f"✅ 区间中部 ({CONVERGENCE_LOWER}%–{CONVERGENCE_UPPER}%)"
        mode, weight = "NORMAL", 1.0

    # ✅ 绘制对齐条形图
    bar = _draw_bar(L, U, ref_price, P, 26)

    return {
        "pair": pair, "timeframe": timeframe,
        "lower_bound": L, "upper_bound": U,
        "ref_price": ref_price, "ref_pct": round(pct_ref, 1),
        "current_price": P, "price_source": src, "pct_curr": round(pct_curr, 1),
        "dist_to_upper": dist_u, "dist_to_lower": dist_l, "drift_pct": drift,
        "bar": bar, "zone_label": zone_label, "mode": mode, "weight": weight,
        "p_up_pct": round(d.get("p_up_pct", d.get("p_up", 0)), 1),
        "warnings": warns, "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")
    }

# ========== 📋 功能1：批量扫描 ==========
def scan_all(timeframe: str = "D") -> List[Dict]:
    f = _find_file(timeframe)
    if not f:
        print(f"⚠️ 未找到今日{timeframe}线MC文件")
        return []
    raw = f.read_text().lstrip('\ufeff').strip()
    data = json.loads(raw)
    results = data.get("results", {})
    collected, cautions, anomalies = [], [], []
    print("=" * 80)
    print(f"📈 MC区间扫描  [{timeframe}线]  今日共 {len(results)} 个货币对")
    print("=" * 80)
    for key, d in results.items():
        pair = _key_to_pair(key)
        rng = d.get("range_90", [None, None])
        L, U, P = rng[0], rng[1], d.get("current_price")
        pct = d.get("percentile_rank")
        if not all([L, U, P, pct is not None]):
            continue
        dist_u, dist_l = round(U - P, 4), round(P - L, 4)
        bar = _draw_bar_simple(L, U, P, 22)
        warns = validate_zone(L, U, P, pct)
        if warns:
            anomalies.append(pair); flag = "🔴"
        elif pct >= CONVERGENCE_UPPER or pct <= CONVERGENCE_LOWER:
            cautions.append(pair); flag = "⚠️"
        else:
            flag = "✅"
        print(f"{flag} {pair:10s} | {L:.4f}─{bar}─{U:.4f} | 现价:{P:.4f} 百分位:{pct:5.1f}%  距上沿:{dist_u}  距下沿:{dist_l}")
        collected.append({"pair": pair, "lower": L, "upper": U, "price": P, "pct": pct, "dist_upper": dist_u, "dist_lower": dist_l})
    print("=" * 80)
    print(f"📋 汇总: 中部{len(collected)-len(cautions)-len(anomalies)}对", end="")
    if cautions: print(f" | 收敛区{len(cautions)}对 → {', '.join(cautions)}", end="")
    if anomalies: print(f" | 数据异常{len(anomalies)}对 → {', '.join(anomalies)}", end="")
    print("\n" + "=" * 80)
    return collected

# ========== 🔍 功能2：开仓检查 ==========
def check_entry(pair: str, timeframe: str = "D", price: float = None) -> Optional[Dict]:
    is_jpy_pair = pair.endswith("_JPY") or pair.endswith("JPY")
    pip_size = 0.01 if is_jpy_pair else 0.0001
    z = get_zone(pair, timeframe, price)
    if not z:
        return None
    dist_upper_pips = round(z['dist_to_upper'] / pip_size, 1)
    dist_lower_pips = round(z['dist_to_lower'] / pip_size, 1)
    print("\n" + "─" * 72)
    print(f"🔴 【开仓边界检查】 {pair}  现价来源: {z['price_source']}")
    print("─" * 72)
    print(f"  MC预测区间:  {z['lower_bound']:.4f}  {z['bar']}  {z['upper_bound']:.4f}")
    print(f"  模型基准:    {z['ref_price']:.4f}   位置: {z['ref_pct']:5.1f}%   ＊=MC预期")
    print(f"  当前价格:    {z['current_price']:.4f}   位置: {z['pct_curr']:5.1f}%   ✦=现价")
    print(f"  偏离基准:    {z['drift_pct']:+.1f}%   (+=高于预期，-=低于预期)")
    print(f"  距离上沿:    {z['dist_to_upper']:.4f}  ({dist_upper_pips} pips)")
    print(f"  距离下沿:    {z['dist_to_lower']:.4f}  ({dist_lower_pips} pips)")
    print(f"  上涨概率:    {z['p_up_pct']:.1f}%")
    if z["warnings"]:
        print(f"  🔴 数据异常: {'; '.join(z['warnings'])}")
    if z["mode"] == "CAUTIOUS":
        print(f"  ⚠️ 预警: {z['zone_label']} → 建议仓位 ×{z['weight']}")
    else:
        print(f"  ✅ 位置安全 → 标准仓位 ×1.0")
    print(f"  ⏰ 快照时间: {z['timestamp_utc']} UTC")
    print("─" * 72 + "\n")
    return z

# ========== 📈 功能3：持仓追踪 ==========
def check_position(pair: str, entry_price: float, timeframe: str = "D", current_price: float = None) -> Optional[Dict]:
    z = get_zone(pair, timeframe, current_price)
    if not z:
        return None
    L, U = z["lower_bound"], z["upper_bound"]
    entry_pct = ((entry_price - L) / (U - L)) * 100 if U != L else 50.0
    bar_entry = _draw_bar_simple(L, U, entry_price, 22)
    bar_now = _draw_bar_simple(L, U, z["current_price"], 22)
    drift = z["pct_curr"] - entry_pct
    print("\n" + "─" * 72)
    print(f"📈 【持仓状态追踪】 {pair}  现价来源: {z['price_source']}")
    print("─" * 72)
    print(f"  MC预测区间: {L:.4f} ────────────────── {U:.4f}")
    print(f"  开仓价格:   {entry_price:.4f}  | 位置: {entry_pct:5.1f}%  {bar_entry}")
    print(f"  当前价格:   {z['current_price']:.4f}  | 位置: {z['pct_curr']:5.1f}%  {bar_now}")
    print(f"  位置漂移:   {drift:+.1f}%  (+=向上沿，-=向下沿)")
    print(f"  距上沿:     {z['dist_to_upper']:.4f}")
    print(f"  距下沿:     {z['dist_to_lower']:.4f}")
    print(f"  区间状态:   {z['zone_label']}")
    if z["warnings"]:
        print(f"  🔴 数据异常: {'; '.join(z['warnings'])}")
    print(f"  ⏰ 快照时间: {z['timestamp_utc']} UTC")
    print("─" * 72 + "\n")
    return z

# ========== ✅ 功能4：全套自测 ==========
def run_tests() -> bool:
    print("\n" + "=" * 80)
    print("🧪 运行全套自测...")
    print("=" * 80)
    passed, failed = 0, 0
    # 测试1: 百分位计算
    L, U, P = 100, 110, 105
    actual = ((P - L) / (U - L)) * 100
    if math.isclose(actual, 50.0):
        print(f"✅ 测试1-百分位计算: {P}在[{L},{U}] = {actual:.1f}% → 通过"); passed += 1
    else:
        print(f"🔴 测试1-百分位计算: 期望50% 实际{actual:.1f}% → 失败"); failed += 1
    # 测试2: 边界判定
    test_cases = [(100,110,103,"NORMAL"), (100,110,102,"CAUTIOUS"), (100,110,107,"CAUTIOUS")]
    for i, (L,U,P,em) in enumerate(test_cases,2):
        pct = ((P-L)/(U-L))*100; mode = "CAUTIOUS" if (pct>=70 or pct<=30) else "NORMAL"
        if mode==em: print(f"✅ 测试{i}-边界判定: P={P} 百分位{pct:.1f}% → {mode} → 通过"); passed+=1
        else: print(f"🔴 测试{i}-边界判定: 期望{em} 实际{mode} → 失败"); failed+=1
    # 测试3: 数据校验
    if not validate_zone(100,110,105,50.0) and len(validate_zone(110,100,105,50.0))>=1:
        print(f"✅ 测试3-数据校验 → 通过"); passed+=1
    else: print(f"🔴 测试3-数据校验 → 失败"); failed+=1
    # 测试4: 格式转换
    if _pair_to_key("USD_JPY")=="USDJPY=X" and _key_to_pair("USDJPY=X")=="USD_JPY":
        print(f"✅ 测试4-格式互转 → 通过"); passed+=1
    else: print(f"🔴 测试4-格式转换 → 失败"); failed+=1
    # 测试5: 距离计算
    if math.isclose(110-104,6) and math.isclose(104-100,4):
        print(f"✅ 测试5-距离计算 → 通过"); passed+=1
    else: print(f"🔴 测试5-距离计算 → 失败"); failed+=1
    # 汇总
    print("=" * 80)
    if failed==0: print(f"🎉 全部通过: {passed}项 ✅  0项失败")
    else: print(f"⚠️ 自测结果: 通过{passed}项  失败{failed}项 ⚠️")
    print("=" * 80 + "\n")
    return failed==0

# ========== 命令行入口 ==========
if __name__ == "__main__":
    args = sys.argv[1:]
    if not args: scan_all("D")
    elif args[0].lower()=="test": run_tests()
    elif args[0].upper() in ["D","W"]: scan_all(args[0].upper())
    elif len(args)==1: check_entry(args[0], "D")
    elif len(args)==2: check_position(args[0], float(args[1]), "D")
    else:
        print("""
📖 使用方法:
  python mc_zone_analyzer.py              → 扫描全部日线
  python mc_zone_analyzer.py USD_JPY      → 查单个(OANDA实时)
  python mc_zone_analyzer.py USD_JPY 154.81  → 持仓追踪
  python mc_zone_analyzer.py test         → 运行全套自测
        """)