#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Example Interface Module: fx_monte_carlo.interface
Description:
    数据访问层参考示例。供调用者拷贝到本地项目中使用。
    1. 纯粹的数据契约：按日期和时间框架安全加载、解析 fx_monte_carlo 生成的复合 JSON 数据。
    2. 业务使用样例：附带展示如何 fail-closed 地消费 Fib 区间与概率标注（prob_bands / belief）。

契约纪律（docs/MC_PROB_ANNOTATION_SPEC_v1.0.md）：
    * 只读：本模块绝不回写 payload（C-1）。
    * fail-closed：数据缺失 / 过期 / 退化 / 窄区间时，**不得**给出「满仓」这类乐观默认，
      必须由调用方显式决策（P-4）。
    * 有效期是硬 TTL 且**闭左**：`now_utc >= expires_utc` 即失效（§7 / §9.3 / V-9）。
    * 容忍降级：早期产物没有 distribution / prob_bands / belief，消费者须降级而非报错（§10）。

⚠️ 注意：下面的 example_* 系列仅为「如何消费数据」的参考示例，请勿直接用于生产；
   其仓位分档与 BUY/SELL 方向无关（同一 zone 对多空含义不同），生产策略须自行实现方向感知。
   详见 interface.md 第 4 节。
"""

import datetime
import json
import os
import re
from pathlib import Path
from typing import Dict, Any, Optional

DEFAULT_RESULTS_DIR = Path(
    os.getenv("FX_MC_RESULTS_DIR", str(Path.home() / "projects" / "fx_monte_carlo" / "mc_results"))
)

# 契约常量（本模块保持零第三方依赖，故在此本地定义；语义与冻结规格一致）
# 窄区间门限（%）：(q95 - q05) / current_price × 100 < 1.2（§4.3.2）。
MIN_WIDTH_PCT_GATE = 1.2


def get_latest_fib_mc_data(
    day: str, timeframe: str = "D", results_dir: Path = DEFAULT_RESULTS_DIR
) -> Optional[Dict[str, Any]]:
    """
    根据日期前缀（如 '20261105'）和时间框架（'D'/'W'）自动通配匹配并加载最新的 MC+Fib 复合 JSON 数据。
    """
    if not results_dir.exists():
        return None

    regex = re.compile(rf"^mc_{re.escape(timeframe)}_all_pairs_{re.escape(day)}_(\d{{4}})\.json$")
    matched = []
    for entry in results_dir.iterdir():
        if entry.is_file():
            m = regex.match(entry.name)
            if m:
                matched.append((m.group(1), entry))

    if not matched:
        return None

    matched.sort(key=lambda x: x[0])
    latest_file = matched[-1][1]
    try:
        return json.loads(latest_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[!] Interface Error reading json {latest_file.name}: {e}")
        return None


def get_pair_analytics(
    pair: str, day: str, timeframe: str = "D", results_dir: Path = DEFAULT_RESULTS_DIR
) -> Optional[Dict[str, Any]]:
    """
    基础检索函数：获取指定交易对在特定日期的所有 MC 统计指标与 Fibonacci 空间分割详情。
    """
    data = get_latest_fib_mc_data(day, timeframe, results_dir)
    if not data or "results" not in data or pair not in data["results"]:
        return None

    return data["results"][pair]


# ==============================================================================
# 🛡️ 有效期与标注状态（只读 · fail-closed）
# 对应 docs/MC_PROB_ANNOTATION_SPEC_v1.0.md §5 / §7 / §9.3 与
# docs/MC_FIB_V1_2_ACTION_PLAN.md P0-3 / P0-4。
# ==============================================================================
def _parse_utc(value: Any) -> Optional[datetime.datetime]:
    """解析带时区的 ISO-8601 时间戳并归一化到 UTC；缺失 / 非法 / 裸时间戳一律返回 None（fail-closed）。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(datetime.timezone.utc)


def _require_utc_now(now_utc: Optional[datetime.datetime]) -> datetime.datetime:
    """归一化调用方传入的 `now_utc`；裸（naive）时间戳属调用方错误，直接拒绝。"""
    if now_utc is None:
        return datetime.datetime.now(datetime.timezone.utc)
    if not isinstance(now_utc, datetime.datetime) or now_utc.tzinfo is None:
        raise ValueError("now_utc must be a timezone-aware datetime")
    return now_utc.astimezone(datetime.timezone.utc)


def is_pair_advisory_fresh(
    rec: Optional[Dict[str, Any]], now_utc: Optional[datetime.datetime] = None
) -> bool:
    """
    单个 pair 的静态标注快照是否仍在有效期内（硬 TTL，闭左）。

    基准为 `belief.expires_utc`。任一条件不满足即 False（fail-closed）：
      * 无 `belief` 块（如 schema 1.1.0 及更早产物）；
      * `expires_utc` 缺失 / 非法 / 无时区偏移；
      * `now_utc >= expires_utc`（闭左：恰好到期即失效）。
    """
    if not isinstance(rec, dict):
        return False
    belief = rec.get("belief")
    if not isinstance(belief, dict):
        return False
    expires = _parse_utc(belief.get("expires_utc"))
    if expires is None:
        return False
    return _require_utc_now(now_utc) < expires


def is_snapshot_fresh(
    data: Optional[Dict[str, Any]], now_utc: Optional[datetime.datetime] = None
) -> bool:
    """
    整批快照是否新鲜：**所有** pair 的 `belief` 均在有效期内。

    空 payload / 无 `results` / 任一 pair 无法证明新鲜 → False（fail-closed）。
    仅用于给调用方一个显式的新鲜度闸门；不再依赖文件名排序推断时效（P0-4）。
    """
    if not isinstance(data, dict):
        return False
    results = data.get("results")
    if not isinstance(results, dict) or not results:
        return False
    now = _require_utc_now(now_utc)
    return all(is_pair_advisory_fresh(rec, now) for rec in results.values())


def _range_90_pct(rec: Dict[str, Any]) -> Optional[float]:
    """按 §4.3.2 计算窄区间门限口径 `(q95 - q05) / current_price × 100`；不可用时返回 None。"""
    rng = rec.get("range_90")
    price = rec.get("current_price")
    if not isinstance(rng, (list, tuple)) or len(rng) != 2:
        return None
    try:
        lower, upper = float(rng[0]), float(rng[1])
        price = float(price)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    return (upper - lower) / price * 100.0


def get_pair_advisory(
    rec: Optional[Dict[str, Any]], now_utc: Optional[datetime.datetime] = None
) -> Dict[str, Any]:
    """
    返回单个 pair 的**显式 + fail-closed** 标注状态（只读，供调用方做单一健康检查）。

    `state` 取值与判定优先级（自上而下，任一命中即向下降级）：

    | state          | 条件                                                        |
    |----------------|-------------------------------------------------------------|
    | `invalid`      | 缺 `prob_bands` / `belief`，或块结构不合法                    |
    | `stale`        | 快照已过期（`now_utc >= expires_utc`）                        |
    | `degenerate`   | `distribution.degenerate` 或 `prob_bands.degenerate` 为真     |
    | `narrow_range` | 宽度不可用，或 `range_90_pct < MIN_WIDTH_PCT_GATE`            |
    | `healthy`      | 以上皆不成立                                                  |

    返回的 `verdict` 仅在 `state == "healthy"` 时透传 payload 值，其余一律 `"NO_EDGE"`，
    以免调用方把过期 / 退化的旧结论当作当前判断（P-4）。
    """
    out: Dict[str, Any] = {
        "state": "invalid",
        "available": False,
        "fresh": False,
        "stale": False,
        "degenerate": False,
        "narrow_range": False,
        "verdict": "NO_EDGE",
        "advisory": True,
    }
    if not isinstance(rec, dict):
        return out

    prob_bands = rec.get("prob_bands")
    belief = rec.get("belief")
    if not isinstance(prob_bands, dict) or not isinstance(belief, dict):
        return out

    out["available"] = True
    distribution = rec.get("distribution")
    out["degenerate"] = bool(
        (isinstance(distribution, dict) and distribution.get("degenerate"))
        or prob_bands.get("degenerate")
    )
    width = _range_90_pct(rec)
    out["narrow_range"] = width is None or width < MIN_WIDTH_PCT_GATE

    out["fresh"] = is_pair_advisory_fresh(rec, now_utc)
    out["stale"] = not out["fresh"]

    if out["stale"]:
        out["state"] = "stale"
    elif out["degenerate"]:
        out["state"] = "degenerate"
    elif out["narrow_range"]:
        out["state"] = "narrow_range"
    else:
        out["state"] = "healthy"
        out["verdict"] = belief.get("verdict", "NO_EDGE")
    return out


# ==============================================================================
# 🌟 经典业务使用样例（Example Strategy: 动态仓位缩放 K 系数）
# 说明：这只是本系统提供的一种推荐参考用法，调用者完全可以根据需要编写自己的策略。
# ==============================================================================
def example_calculate_position_multiplier(
    pair: str, day: str, timeframe: str = "D"
) -> Dict[str, Any]:
    """
    [示例方法] 演示如何 **fail-closed** 地消费底层数据，分两步：

    1. 先过标注健康闸门（`get_pair_advisory`）：缺块 / 过期 / 退化 / 窄区间
       → 直接返回 `multiplier_k = None`，**不落任何乐观默认**，由调用方显式决策；
    2. 仅在 `state == "healthy"` 时，才按 Fib `zone_index` 给出参考系数：
         - 均衡核心区 (zone_index 2, 3): K = 1.0
         - 次核心区 (zone_index 1, 4): K = 0.5
         - 极端边缘区 (zone_index 0, 5): K = 0.2

    ⚠️ 仅供参考，请勿用于生产：分档与 BUY/SELL 方向无关（同一 zone 对多空含义不同），
       生产策略须自行实现方向感知（详见 interface.md 第 4 节）。

    另注：`fib.risk_model.multiplier_k` 在窄区间 / 退化时会给出 `1.0`（生产者侧既有行为，
    本轮冻结），故本示例**不**采信该字段，改以本模块的 fail-closed 闸门为准。
    """
    res = get_pair_analytics(pair, day, timeframe)
    if not res:
        return {
            "status": "NO_ADVISORY",
            "multiplier_k": None,
            "reason": "data_not_found",
            "advisory": True,
        }

    advisory = get_pair_advisory(res)
    if advisory["state"] != "healthy":
        return {
            "status": "NO_ADVISORY",
            "multiplier_k": None,
            "reason": f"advisory_state_{advisory['state']}",
            "advisory_state": advisory["state"],
            "advisory": True,
        }

    fib_info = res.get("fib", {})
    zone_idx = fib_info.get("zone_index")
    if zone_idx not in (0, 1, 2, 3, 4, 5):
        return {
            "status": "NO_ADVISORY",
            "multiplier_k": None,
            "reason": "fib_zone_index_missing",
            "advisory": True,
        }

    if zone_idx in (2, 3):
        k_factor = 1.0
    elif zone_idx in (1, 4):
        k_factor = 0.5
    else:
        k_factor = 0.2

    return {
        "status": "SUCCESS",
        "pair": pair,
        "current_price": res.get("current_price"),
        "zone_index": zone_idx,
        "zone_label": fib_info.get("current_zone", {}).get("label"),
        "action": fib_info.get("labels", {}).get("action"),
        "multiplier_k": k_factor,
        "advisory_state": advisory["state"],
        "verdict": advisory["verdict"],
        "advisory": True,
    }


if __name__ == "__main__":
    today_str = datetime.datetime.now().strftime("%Y%m%d")
    print(f"=== Interface Example Test for EURUSD=X ({today_str}) ===")

    # 测试基础数据获取
    bundle = get_latest_fib_mc_data(today_str, timeframe="D")
    print("Bundle Retrieved:", bool(bundle))
    print("Snapshot Fresh:", is_snapshot_fresh(bundle))

    # 测试 fail-closed 标注状态（只读）
    rec = get_pair_analytics("EURUSD=X", today_str)
    print("Pair Advisory:", json.dumps(get_pair_advisory(rec), ensure_ascii=False))

    # 测试样例策略计算
    strategy_res = example_calculate_position_multiplier("EURUSD=X", today_str)
    print(json.dumps(strategy_res, indent=2, ensure_ascii=False))