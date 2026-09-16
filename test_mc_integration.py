"""
MC 逐货币对区间分析 — 全量扫描版
✅ 读取今日汇总文件 → 遍历所有货币对
✅ 每对独立显示: 区间边界 + 当前价 + 百分位 + 位置图 + 收敛判定
✅ 不笼统、不合并、逐个清晰
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# ========== 配置 ==========
MC_RESULTS_DIR = Path.cwd() / "mc_results"
CONVERGENCE_LOWER = 30.0
CONVERGENCE_UPPER = 70.0
SUFFIX_D = "mc_D_all_pairs"
SUFFIX_W = "mc_W_all_pairs"

# ========== 名称互转 ==========
def normalize_pair_key(pair: str) -> str:
    """USD_JPY → USDJPY=X"""
    return pair.replace("_", "").upper() + "=X"

def normalize_pair_display(pair_key: str) -> str:
    """USDJPY=X → USD_JPY"""
    s = pair_key.replace("=X", "").upper()
    if len(s) == 6:
        return f"{s[:3]}_{s[3:]}"
    return pair_key

# ========== 找汇总文件 ==========
def find_latest_summary_file(timeframe: str = "D") -> Optional[Path]:
    today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    prefix = SUFFIX_D if timeframe.upper() == "D" else SUFFIX_W
    matches = sorted(
        MC_RESULTS_DIR.glob(f"{prefix}_{today_str}_*.json"),
        reverse=True
    )
    return matches[0] if matches else None

# ========== 读取全部货币对数据 ==========
def load_all_pairs_bounds(timeframe: str = "D") -> List[Dict]:
    """读取汇总文件 → 返回全部货币对的完整数据列表"""
    f = find_latest_summary_file(timeframe)
    if not f:
        print(f"⚠️ 未找到今日{timeframe}线汇总文件")
        return []

    data = json.loads(f.read_text())
    results = data.get("results", {})
    collected = []

    for pair_key, d in results.items():
        rng = d.get("range_90", [None, None])
        bounds = {
            "instrument": normalize_pair_display(pair_key),
            "timeframe": timeframe.upper(),
            "current_price": d.get("current_price"),
            "lower_bound": rng[0],
            "upper_bound": rng[1],
            "percentile_rank": d.get("percentile_rank"),
            "p_up_pct": d.get("p_up_pct", d.get("p_up")),
            "p_down_pct": d.get("p_down_pct", d.get("p_down")),
        }
        # 只保留数据完整的对
        if all([bounds["lower_bound"], bounds["upper_bound"],
                bounds["current_price"], bounds["percentile_rank"] is not None]):
            collected.append(bounds)

    return collected

# ========== 单对收敛判定 ==========
def evaluate_zone(bounds: Dict) -> Dict:
    L = bounds["lower_bound"]
    U = bounds["upper_bound"]
    P = bounds["current_price"]
    pct = bounds["percentile_rank"]
    instrument = bounds["instrument"]

    dist_to_upper = U - P
    dist_to_lower = P - L

    if pct >= CONVERGENCE_UPPER:
        zone = f"⚠️ 靠近上沿 (≥{CONVERGENCE_UPPER}%)"
        mode = "CAUTIOUS"
        weight = 0.5
    elif pct <= CONVERGENCE_LOWER:
        zone = f"⚠️ 靠近下沿 (≤{CONVERGENCE_LOWER}%)"
        mode = "CAUTIOUS"
        weight = 0.5
    else:
        zone = f"✅ 区间中部 ({CONVERGENCE_LOWER}%–{CONVERGENCE_UPPER}%)"
        mode = "NORMAL"
        weight = 1.0

    return {
        **bounds,
        "position_pct": pct,
        "dist_to_upper": dist_to_upper,
        "dist_to_lower": dist_to_lower,
        "zone_label": zone,
        "mode": mode,
        "weight": weight,
    }

# ========== 单对打印报告 ==========
def print_single_pair(result: Dict) -> None:
    """📊 打印单个货币对的完整区间报告"""
    L, U, P = result["lower_bound"], result["upper_bound"], result["current_price"]
    pct = result["position_pct"]

    # 可视化条形图 24格
    bar_width = 24
    pos_index = round((pct / 100.0) * (bar_width - 1))
    bar = ["─"] * bar_width
    if 0 <= pos_index < bar_width:
        bar[pos_index] = "●"
    bar_line = "".join(bar)

    print(f"📊 {result['instrument']:10s} | 区间: {L:.4f}─{bar_line}─{U:.4f}")
    print(f"            现价: {P:.4f} | 百分位: {pct:5.1f}% | 距上沿: {result['dist_to_upper']:.4f} 距下沿: {result['dist_to_lower']:.4f}")
    print(f"            上涨概率: {result['p_up_pct']:.1f}% | {result['zone_label']} | 建议: {result['mode']} (×{result['weight']:.1f})")
    print()

# ========== 一键扫描全部并打印 ==========
def analyze_all_pairs(timeframe: str = "D") -> List[Dict]:
    """扫描全部 → 逐对打印 → 返回完整结果列表"""
    all_bounds = load_all_pairs_bounds(timeframe)
    if not all_bounds:
        return []

    print("=" * 72)
    print(f"📈 MC 逐货币对区间分析  [{timeframe}线]  今日共 {len(all_bounds)} 个货币对")
    print("=" * 72)

    results = []
    for bounds in all_bounds:
        r = evaluate_zone(bounds)
        results.append(r)
        print_single_pair(r)

    # 汇总统计
    cautious = [r for r in results if r["mode"] == "CAUTIOUS"]
    normal = [r for r in results if r["mode"] == "NORMAL"]
    print("=" * 72)
    print(f"📋 汇总: 中部{len(normal)}对 | 边界收敛区{len(cautious)}对 → 建议谨慎/减半仓位")
    if cautious:
        print(f"   ⚠️ 边界预警: {', '.join(r['instrument'] for r in cautious)}")
    print("=" * 72)

    return results

# ========== 也支持查单个货币对 ==========
def analyze_single_pair(pair: str, timeframe: str = "D") -> Optional[Dict]:
    """查指定单个货币对"""
    all_bounds = load_all_pairs_bounds(timeframe)
    for b in all_bounds:
        if b["instrument"].upper() == pair.upper():
            r = evaluate_zone(b)
            print("\n" + "─" * 72)
            print(f"🎯 指定查询: {pair}")
            print("─" * 72)
            print_single_pair(r)
            return r
    print(f"⚠️ 未找到货币对: {pair}")
    return None

# ========== 运行入口 ==========
if __name__ == "__main__":
    import sys
    arg = sys.argv[1] if len(sys.argv) > 1 else ""

    if arg.upper() in ["D", "W"]:
        # 只指定周期 → 扫描全部
        analyze_all_pairs(arg.upper())
    elif arg:
        # 指定货币对 → 只查这一个
        tf = sys.argv[2] if len(sys.argv) > 2 else "D"
        analyze_single_pair(arg, tf)
    else:
        # 无参数 → 日线全部
        analyze_all_pairs("D")