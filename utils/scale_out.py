"""utils/scale_out.py — 分批止盈（scale-out）减仓执行覆盖层 (gemini_ai · 只减不增).

用户裁定：**+1R 平 50% / +6R 平剩余尾仓**（以固定初始 R 为单位，``R = |entry − 初始 SL|``）。

本模块**只允许改一件事**：把剩余仓位分批平掉（只减不增 / 绝不加仓）。
    * 绝不触碰：信号方向 / 打分权重 / SL / TP / MC 融合。
    * 只决定「本次该平多少手」；真正的下单由调用方（TradingCore）执行。

设计
----
幂等
    R 锚定在**初始** entry / SL 上（永不因追踪 SL 或加仓而漂移）。
    每个梯级的目标是「累计平仓比例 × 原始手数」对应的**目标剩余**，
    本次平仓量 = 当前剩余 − 目标剩余 ⇒ 即使漏掉某次平仓也会自愈，绝不重复平仓。

fail-closed
    初始 entry / SL / 现价 / 原始手数 任一缺失或不合法 → **不平仓**（不推断、不猜测）。

契约
----
纯函数 + 小状态存储（无 I/O 之外的副作用，无 OANDA 调用）；``compute_scale_out_plan``
永不抛异常，异常 → 不动仓（fail-closed）。
"""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
STATE_PATH = LOGS_DIR / "scale_out_state.json"
STAGE_STATE_PATH = LOGS_DIR / "position_stage_state.json"
STAGE_RATIOS = (1.0, 0.5, 0.25)

_EPS = 1e-9

# 权威副本在 config.py(SCALE_OUT)；仅当调用方传入不完整 / 空的 cfg 时兜底。
_CFG_FALLBACK: Dict[str, Any] = {
    "ENABLED": True,
    "SHADOW": True,
    "LADDER": [[1.0, 0.5], [6.0, 0.5]],  # (R 阈值, 该梯级的原始仓位比例)
    "MIN_UNITS": 1,
}


# ─────────────────────────────────────────────────────────────────────────────
# 小而全（永不抛异常）的取值助手
# ─────────────────────────────────────────────────────────────────────────────
def _cfg_get(cfg: Dict[str, Any], key: str, default: Any) -> Any:
    try:
        value = cfg.get(key, default)
    except Exception:
        return default
    return default if value is None else value


def _as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
    return default


def _as_int(value: Any, default: int) -> int:
    number = _as_float(value)
    return default if number is None else int(number)


def _normalize_direction(direction: Any) -> Optional[str]:
    try:
        text = str(direction).strip().upper()
    except Exception:  # pragma: no cover
        return None
    return text if text in ("BUY", "SELL") else None


# ─────────────────────────────────────────────────────────────────────────────
# 梯级
# ─────────────────────────────────────────────────────────────────────────────
def _normalize_ladder(ladder: Any) -> List[Tuple[float, float]]:
    """归一化 ``[(R 阈值, 原始仓位比例), ...]``：过滤非法项、按 R 升序、按 R 去重。"""
    out: List[Tuple[float, float]] = []
    if not isinstance(ladder, (list, tuple)):
        return out
    for item in ladder:
        try:
            r_thr = _as_float(item[0])
            frac = _as_float(item[1])
        except (TypeError, IndexError):
            continue
        if r_thr is None or frac is None:
            continue
        if r_thr <= 0.0 or frac <= 0.0:
            continue
        out.append((r_thr, min(frac, 1.0)))
    out.sort(key=lambda x: x[0])
    deduped: List[Tuple[float, float]] = []
    for r_thr, frac in out:
        if deduped and abs(deduped[-1][0] - r_thr) < _EPS:
            continue
        deduped.append((r_thr, frac))
    return deduped


# ─────────────────────────────────────────────────────────────────────────────
# R 倍数
# ─────────────────────────────────────────────────────────────────────────────
def r_multiple(
    direction: Any,
    entry: Any,
    init_sl: Any,
    current_price: Any,
) -> Optional[float]:
    """未实现盈亏的 R 倍数（基于**初始** entry / SL，锚定不漂移）；不可用 → None。"""
    normalized = _normalize_direction(direction)
    if normalized is None:
        return None
    entry_f = _as_float(entry)
    sl_f = _as_float(init_sl)
    price_f = _as_float(current_price)
    if entry_f is None or sl_f is None or price_f is None:
        return None
    r_unit = abs(entry_f - sl_f)
    if r_unit <= 0.0:
        return None
    if normalized == "BUY":
        return (price_f - entry_f) / r_unit
    return (entry_f - price_f) / r_unit


# ─────────────────────────────────────────────────────────────────────────────
# 返回契约
# ─────────────────────────────────────────────────────────────────────────────
def _no_fire(reason: str, *, r: Optional[float] = None, skip: bool = False) -> Dict[str, Any]:
    return {
        "fire": False,
        "action": "NONE",
        "r": r,
        "rung_r": None,
        "rung_index": None,
        "close_ratio": 0.0,
        "close_units": 0,
        "remaining_units": None,
        "target_after": None,
        "reason": reason,
        "skip": bool(skip),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 唯一对外的计划入口
# ─────────────────────────────────────────────────────────────────────────────
def compute_scale_out_plan(
    *,
    direction: Any,
    entry: Any,
    init_sl: Any,
    current_price: Any,
    remaining_units: Any,
    original_units: Any,
    fired_rungs: Optional[Sequence[Any]] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """单个持仓的**分批止盈**计划（永不抛异常，永远返回完整契约）。

    ``direction`` 必须是 BUY / SELL（BUY=LONG, SELL=SHORT）；其余 → 不动仓。
    ``original_units`` 为该仓位**减仓前**的原始手数（R 梯级比例的分母）。
    ``fired_rungs`` 为已触发梯级的下标集合（幂等去重）。
    """
    cfg_dict: Dict[str, Any] = cfg if isinstance(cfg, dict) else {}
    try:
        if not _as_bool(_cfg_get(cfg_dict, "ENABLED", _CFG_FALLBACK["ENABLED"]), True):
            return _no_fire("disabled")

        normalized = _normalize_direction(direction)
        if normalized is None:
            return _no_fire("invalid_direction")

        remaining = _as_int(remaining_units, 0)
        original = _as_int(original_units, 0)
        min_units = max(1, _as_int(_cfg_get(cfg_dict, "MIN_UNITS", 1), 1))

        if original <= 0:
            return _no_fire("original_units_unknown")
        if remaining <= 0:
            return _no_fire("flat")
        if original <= min_units:
            # live LOT_SIZE=1 等不可分割的仓位：无法分批，整体跳过（broker 拒单保护）。
            return _no_fire("lot_too_small")

        r = r_multiple(normalized, entry, init_sl, current_price)
        if r is None:
            return _no_fire("anchor_unavailable")

        ladder = _normalize_ladder(
            _cfg_get(cfg_dict, "LADDER", _CFG_FALLBACK["LADDER"])
        )
        if not ladder:
            return _no_fire("no_ladder", r=r)

        fired: set = set()
        if isinstance(fired_rungs, (list, tuple, set)):
            for item in fired_rungs:
                idx = _as_int(item, -1)
                if idx >= 0:
                    fired.add(idx)

        cumulative = 0.0
        for idx, (r_thr, frac) in enumerate(ladder):
            cumulative = min(1.0, cumulative + frac)
            if idx in fired:
                continue
            if r + _EPS < r_thr:
                break  # 梯级按 R 升序：当前梯级未到，更高的也不会到
            target_after = max(0, int(round((1.0 - cumulative) * original)))
            close_units = remaining - target_after
            if close_units <= 0:
                continue  # 已在此梯级目标之下（例如更早的一次平仓已覆盖）→ 自愈跳过
            if close_units < min_units:
                return _no_fire("below_min_units", r=r)
            action = "FULL_CLOSE" if close_units >= remaining else "PARTIAL_CLOSE"
            return {
                "fire": True,
                "action": action,
                "r": r,
                "rung_r": r_thr,
                "rung_index": idx,
                "close_ratio": frac,
                "close_units": int(close_units),
                "remaining_units": int(remaining),
                "target_after": int(target_after),
                "reason": f"rung{idx}_r{r_thr:.2f}",
                "skip": False,
            }

        return _no_fire("below_ladder", r=r)
    except Exception:  # pragma: no cover - 防御：绝不向上传播
        logger.exception("scale_out: compute_scale_out_plan failed — no fire (fail-closed)")
        return _no_fire("scale_out_error")


# ─────────────────────────────────────────────────────────────────────────────
# 状态存储（每笔 trade 的已触发梯级 + 原始手数），原子写
# ─────────────────────────────────────────────────────────────────────────────
def load_state(path: Optional[Path] = None) -> Dict[str, Any]:
    """读取状态文件；缺失 / 损坏 → ``{}``（永不抛异常）。"""
    target = Path(path) if path is not None else STATE_PATH
    try:
        if not target.exists():
            return {}
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("scale_out: state load skipped (%s)", exc)
        return {}


def save_state(state: Dict[str, Any], path: Optional[Path] = None) -> bool:
    """原子写状态文件；失败 → False（永不抛异常）。"""
    target = Path(path) if path is not None else STATE_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            json.dumps(state if isinstance(state, dict) else {}, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        os.replace(tmp, target)
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("scale_out: state save skipped (%s)", exc)
        return False


def get_trade_state(state: Dict[str, Any], trade_id: Any) -> Dict[str, Any]:
    """读取单笔 trade 的状态；缺失 → ``{}``。"""
    if not isinstance(state, dict):
        return {}
    entry = state.get(str(trade_id))
    return entry if isinstance(entry, dict) else {}


def fired_rung_indices(state: Dict[str, Any], trade_id: Any) -> List[int]:
    """已触发梯级下标列表（永不抛异常）。"""
    entry = get_trade_state(state, trade_id)
    fired = entry.get("fired")
    out: List[int] = []
    if isinstance(fired, (list, tuple)):
        for item in fired:
            idx = _as_int(item, -1)
            if idx >= 0:
                out.append(idx)
    return out


def record_original_units(
    state: Dict[str, Any], trade_id: Any, original_units: Any
) -> Dict[str, Any]:
    """**首次**观测时锚定原始手数（之后不再改写，保证 R 分母稳定）。"""
    if not isinstance(state, dict):
        return {}
    key = str(trade_id)
    entry = state.get(key)
    if not isinstance(entry, dict):
        entry = {}
    if _as_int(entry.get("original_units"), 0) <= 0:
        units = _as_int(original_units, 0)
        if units > 0:
            entry["original_units"] = units
    entry.setdefault("fired", [])
    state[key] = entry
    return state


def mark_fired(
    state: Dict[str, Any],
    trade_id: Any,
    rung_index: Any,
    *,
    original_units: Any = None,
) -> Dict[str, Any]:
    """记录某梯级已平仓（幂等去重），并（可选）补齐原始手数。"""
    if not isinstance(state, dict):
        return {}
    key = str(trade_id)
    entry = state.get(key)
    if not isinstance(entry, dict):
        entry = {}
    idx = _as_int(rung_index, -1)
    fired = entry.get("fired")
    fired = list(fired) if isinstance(fired, list) else []
    if idx >= 0 and idx not in fired:
        fired.append(idx)
    entry["fired"] = fired
    if original_units is not None and _as_int(entry.get("original_units"), 0) <= 0:
        units = _as_int(original_units, 0)
        if units > 0:
            entry["original_units"] = units
    entry["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state[key] = entry
    return state


def prune_state(state: Dict[str, Any], keep_ids: Any) -> Dict[str, Any]:
    """只保留仍在持仓中的 trade_id，防止状态文件无限增长。"""
    if not isinstance(state, dict):
        return {}
    try:
        keep = {str(x) for x in (keep_ids or [])}
    except Exception:  # pragma: no cover
        return state
    return {k: v for k, v in state.items() if k in keep}


# ─────────────────────────────────────────────────────────────────────────────
# Per-pair staged position sizing
# ─────────────────────────────────────────────────────────────────────────────
def load_stage_state(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load persistent per-pair staged-position state."""
    target = Path(path) if path is not None else STAGE_STATE_PATH
    try:
        if not target.exists():
            return {}
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # pragma: no cover
        logger.warning("scale_out: stage state load skipped (%s)", exc)
        return {}


def save_stage_state(state: Dict[str, Any], path: Optional[Path] = None) -> bool:
    """Atomically save per-pair staged-position state."""
    target = Path(path) if path is not None else STAGE_STATE_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            json.dumps(state if isinstance(state, dict) else {}, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        os.replace(tmp, target)
        return True
    except Exception as exc:  # pragma: no cover
        logger.warning("scale_out: stage state save skipped (%s)", exc)
        return False


def get_stage_index(state: Dict[str, Any], instrument: Any) -> int:
    """Return stage index 0/1/2 for a pair. Unknown/corrupt falls back to 0."""
    try:
        pair = str(instrument).strip().upper()
        if not isinstance(state, dict):
            return 0
        stages = state.get("stages")
        raw = stages.get(pair, 0) if isinstance(stages, dict) else state.get(pair, 0)
        idx = int(raw)
        return min(max(idx, 0), len(STAGE_RATIOS) - 1)
    except Exception:
        return 0


def stage_ratio(state: Dict[str, Any], instrument: Any) -> float:
    return STAGE_RATIOS[get_stage_index(state, instrument)]


def advance_stage(state: Dict[str, Any], instrument: Any) -> int:
    """Advance pair to the next smaller position tier, capped at 25%."""
    pair = str(instrument).strip().upper()
    if not pair:
        return 0
    idx = min(get_stage_index(state, pair) + 1, len(STAGE_RATIOS) - 1)
    stages = state.get("stages")
    if not isinstance(stages, dict):
        stages = {
            key: value
            for key, value in state.items()
            if not str(key).startswith("_") and key != "stages"
        }
        state.clear()
        state["stages"] = stages
        state["_pending_tp"] = {}
        state["_processed_tp"] = []
    stages[pair] = idx
    return idx


def register_stage_trade(
    state: Dict[str, Any],
    instrument: Any,
    trade_id: Any,
    direction: Any,
    target_price: Any,
) -> bool:
    """Persist the broker trade identity needed to reconcile an attached TP."""
    try:
        pair = str(instrument).strip().upper()
        tid = str(trade_id).strip()
        side = _normalize_direction(direction)
        target = _as_float(target_price)
        if not pair or not tid or side is None or target is None:
            return False
        if "stages" not in state:
            legacy = {
                key: value
                for key, value in state.items()
                if not str(key).startswith("_")
            }
            state.clear()
            state["stages"] = legacy
        pending = state.setdefault("_pending_tp", {})
        if not isinstance(pending, dict):
            pending = {}
            state["_pending_tp"] = pending
        pending[tid] = {
            "instrument": pair,
            "direction": side,
            "target_price": target,
            "stage": get_stage_index(state, pair),
        }
        return True
    except Exception:
        return False


def pending_stage_trades(state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    pending = state.get("_pending_tp") if isinstance(state, dict) else None
    return pending if isinstance(pending, dict) else {}


def complete_stage_trade(
    state: Dict[str, Any], trade_id: Any, *, take_profit_confirmed: bool
) -> Optional[int]:
    """Remove a closed trade from reconciliation, advancing once only for TP."""
    tid = str(trade_id).strip()
    if not tid or not isinstance(state, dict):
        return None
    pending = pending_stage_trades(state)
    record = pending.pop(tid, None)
    if record is None:
        return None
    if not take_profit_confirmed:
        return None
    processed = state.setdefault("_processed_tp", [])
    if not isinstance(processed, list):
        processed = []
        state["_processed_tp"] = processed
    if tid in processed:
        return None
    processed.append(tid)
    del processed[:-500]
    return advance_stage(state, record.get("instrument"))


# ─────────────────────────────────────────────────────────────────────────────
# 审计 / 影子日志
# ─────────────────────────────────────────────────────────────────────────────
def format_scale_out_audit(pair: str, trade_id: Any, plan: Optional[Dict[str, Any]]) -> str:
    """渲染为单行 ``key=value | ...``（永不抛异常）。"""
    try:
        safe: Dict[str, Any] = plan if isinstance(plan, dict) else {}

        def _num(value: Any, digits: int = 2) -> str:
            number = _as_float(value)
            return "None" if number is None else f"{number:.{digits}f}"

        return " | ".join(
            [
                f"pair={pair}",
                f"T{trade_id}",
                f"fire={bool(safe.get('fire'))}",
                f"action={safe.get('action', '?')}",
                f"r={_num(safe.get('r'), 2)}",
                f"rung={safe.get('rung_index')}@{_num(safe.get('rung_r'), 2)}",
                f"ratio={_num(safe.get('close_ratio'), 2)}",
                f"close={safe.get('close_units')}",
                f"remain={safe.get('remaining_units')}",
                f"reason={safe.get('reason', '?')}",
            ]
        )
    except Exception:  # pragma: no cover - 审计绝不能打断运行
        return f"pair={pair} | T{trade_id} | reason=audit_error"


def log_scale_out_shadow(
    record: Dict[str, Any], logs_dir: Optional[Path] = None
) -> Optional[str]:
    """追加一行影子记录到 ``logs/scale_out_shadow_YYYYMMDD.jsonl``（永不抛异常）。"""
    try:
        rec = dict(record) if isinstance(record, dict) else {}
        now = datetime.now(timezone.utc)
        rec.setdefault("ts", now.isoformat(timespec="seconds"))
        directory = Path(logs_dir) if logs_dir is not None else LOGS_DIR
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"scale_out_shadow_{now.strftime('%Y%m%d')}.jsonl"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return str(path)
    except Exception as exc:  # pragma: no cover - 审计绝不能打断运行
        logger.warning("scale_out: shadow write skipped (%s)", exc)
        return None


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    _cfg = {"ENABLED": True, "LADDER": [[1.0, 0.5], [2.0, 0.5]], "MIN_UNITS": 1}
    for _price in (1.1000, 1.1050, 1.1100):
        _plan = compute_scale_out_plan(
            direction="BUY",
            entry=1.1000,
            init_sl=1.0950,
            current_price=_price,
            remaining_units=1000 if _price < 1.1100 else 500,
            original_units=1000,
            fired_rungs=[] if _price < 1.1050 else [0],
            cfg=_cfg,
        )
        print(format_scale_out_audit("EUR_USD", "123", _plan))
