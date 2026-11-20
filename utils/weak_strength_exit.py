"""
主动出局 · 弱强度离场 (Weak-Strength Exit)
==========================================
入场是「从高往下」：按 strength_score 取最强 → 开仓。
出场正好相反：按「方向调整支撑分」升序，把**全局支撑分最低的 1 笔**平掉。

支撑分 (support score, 越高 = 当前强度越支持该持仓)：
    BUY : base − quote
    SELL: quote − base
其中 base/quote 为 instrument 的两个货币（USD_JPY → base=USD, quote=JPY）。
分数越低 → 当前强度越不支持该持仓 → 越该主动离场。

跨所有计价货币汇总持仓，按支撑分全局升序排名；只平支撑分最低的 1 笔。
最短持仓时间闸：年龄 < MIN_HOLD_MIN 的仓位本轮不主动平（用户确认保留）。

设计约束：
  • 纯函数、无 I/O（除影子日志辅助）、**永不抛异常**。
  • fail-closed：分数缺失 / 年龄不可知 → 该笔本轮不参与主动平仓。
  • 只负责「算出该平哪些 trade_id」；真正的下单由 runner 执行。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"

# --------------------------------------------------------------------- #
# 配置兜底：当 runner 未提供 config 时使用（与 config.WEAK_STRENGTH_EXIT 对齐）
# --------------------------------------------------------------------- #
_CFG_FALLBACK: Dict[str, Any] = {
    "ENABLED": True,
    "SHADOW": True,
    "N": 1,             # 每周期最多主动平 1 笔
    "MIN_HOLD_MIN": 180.0,  # 最短持仓时间（分钟）
}


def _merge_cfg(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(_CFG_FALLBACK)
    if isinstance(cfg, dict):
        for k, v in cfg.items():
            out[k] = v
    return out


def _as_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def split_pair(instrument: Any) -> Optional[Tuple[str, str]]:
    """``USD_JPY`` → ``("USD", "JPY")``；无法拆分返回 None（永不抛异常）。"""
    try:
        text = str(instrument).strip().upper()
    except Exception:
        return None
    if "_" in text:
        base, _, quote = text.partition("_")
    elif len(text) == 6 and text.isalpha():
        # 兜底：USDJPY → USD/JPY
        base, quote = text[:3], text[3:]
    else:
        return None
    base, quote = base.strip(), quote.strip()
    if not base or not quote:
        return None
    return base, quote


def support_score(
    global_scores: Optional[Dict[str, float]], instrument: Any, side: Any
) -> Optional[float]:
    """方向调整支撑分：越高 = 当前强度越支持该持仓。

    BUY : base − quote
    SELL: quote − base

    任一货币不在 strength 矩阵中 → 返回 None（fail-closed，永不抛异常）。
    """
    try:
        pair = split_pair(instrument)
        if pair is None:
            return None
        base, quote = pair
        scores = global_scores or {}
        if base not in scores or quote not in scores:
            return None
        gap = float(scores[base]) - float(scores[quote])
        side_u = str(side).strip().upper()
        if side_u == "SELL":
            return -gap
        return gap
    except Exception:
        return None


def compute_weak_exit_plan(
    positions: Optional[Iterable[Dict[str, Any]]],
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """算出本轮该主动平掉的 trade_id 列表（不做任何下单）。

    ``positions`` 每项形如::

        {
          "trade_id": "123",
          "instrument": "USD_JPY",
          "quote": "JPY",          # 分组键（计价货币）
          "side": "BUY",           # BUY / SELL
          "support": 0.42,         # 方向调整支撑分（None → 跳过）
          "age_min": 245.0,        # 持仓年龄（分钟，None → 跳过）
        }

    返回::

        {
          "enabled": bool,
          "close_ids": [trade_id, ...],
          "details": [ {trade_id, instrument, quote, side, support, age_min, rank, group_size} ... ],
          "skipped": [ {trade_id, reason} ... ],
        }
    """
    c = _merge_cfg(cfg)
    out: Dict[str, Any] = {
        "enabled": bool(c.get("ENABLED")),
        "close_ids": [],
        "details": [],
        "skipped": [],
    }
    if not out["enabled"]:
        return out

    # User mandate: weak-strength exit closes exactly one trade per cycle.
    close_limit = 1
    min_hold = _as_float(c.get("MIN_HOLD_MIN"))
    if min_hold is None:
        min_hold = float(_CFG_FALLBACK["MIN_HOLD_MIN"])

    eligible: List[Dict[str, Any]] = []
    for p in positions or []:
        try:
            p = dict(p)
        except Exception:
            continue
        tid = str(p.get("trade_id") or "")
        sup = _as_float(p.get("support"))
        age = _as_float(p.get("age_min"))
        if sup is None:
            out["skipped"].append({"trade_id": tid, "reason": "no_support_score"})
            continue
        if age is None:
            out["skipped"].append({"trade_id": tid, "reason": "no_age"})
            continue
        if age < min_hold:
            out["skipped"].append(
                {"trade_id": tid, "reason": f"min_hold({age:.0f}<{min_hold:.0f})"}
            )
            continue
        eligible.append({**p, "support": sup, "age_min": age})

    eligible.sort(key=lambda x: x["support"])
    eligible_count = len(eligible)
    for rank, p in enumerate(eligible[:close_limit]):
        tid = str(p.get("trade_id") or "")
        if not tid:
            continue
        out["close_ids"].append(tid)
        out["details"].append(
            {
                "trade_id": tid,
                "instrument": p.get("instrument", ""),
                "quote": p.get("quote", ""),
                "side": p.get("side", ""),
                "support": round(p["support"], 4),
                "age_min": round(p["age_min"], 1),
                "rank": rank + 1,
                "group_size": eligible_count,
            }
        )
    return out


def format_weak_exit_audit(detail: Dict[str, Any]) -> str:
    """单行审计文本。"""
    try:
        return (
            f"T{detail.get('trade_id')} {detail.get('instrument')} "
            f"{detail.get('side')} support={detail.get('support')} "
            f"age={detail.get('age_min')}min "
            f"rank={detail.get('rank')}/{detail.get('group_size')} "
            f"(quote={detail.get('quote')})"
        )
    except Exception:
        return str(detail)


def log_weak_exit_shadow(record: Dict[str, Any], logs_dir: Optional[Path] = None) -> None:
    """追加一条影子记录到 ``logs/weak_exit_shadow_YYYYMMDD.jsonl``（永不抛异常）。"""
    try:
        base = Path(logs_dir) if logs_dir else LOGS_DIR
        base.mkdir(parents=True, exist_ok=True)
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        path = base / f"weak_exit_shadow_{day}.jsonl"
        payload = dict(record)
        payload.setdefault("ts_utc", datetime.now(timezone.utc).isoformat())
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


if __name__ == "__main__":  # pragma: no cover - 手工冒烟测试
    _demo_scores = {"USD": 1.20, "JPY": -0.30, "EUR": 0.10, "CHF": 0.05}
    _demo_positions = [
        {"trade_id": "1", "instrument": "USD_JPY", "quote": "JPY", "side": "BUY",
         "support": support_score(_demo_scores, "USD_JPY", "BUY"), "age_min": 300},
        {"trade_id": "2", "instrument": "EUR_JPY", "quote": "JPY", "side": "BUY",
         "support": support_score(_demo_scores, "EUR_JPY", "BUY"), "age_min": 300},
        {"trade_id": "3", "instrument": "USD_CHF", "quote": "CHF", "side": "BUY",
         "support": support_score(_demo_scores, "USD_CHF", "BUY"), "age_min": 300},
        {"trade_id": "4", "instrument": "AUD_JPY", "quote": "JPY", "side": "BUY",
         "support": support_score(_demo_scores, "AUD_JPY", "BUY"), "age_min": 30},  # young
    ]
    _plan = compute_weak_exit_plan(_demo_positions, {"ENABLED": True, "N": 1, "MIN_HOLD_MIN": 180})
    print("support scores:", {p["trade_id"]: p["support"] for p in _demo_positions})
    print("close_ids:", _plan["close_ids"])
    for d in _plan["details"]:
        print("  FIRE:", format_weak_exit_audit(d))
    for s in _plan["skipped"]:
        print("  SKIP:", s)
