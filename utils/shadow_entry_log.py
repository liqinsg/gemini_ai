"""Stage C — 入场影子日志（log-only，独立影子存储）。

范围（Owner 已批准 Stage C）：
  - **只**追加 JSONL 影子日志，并维护**独立**影子状态文件
    （``logs/arbitration_shadow_state.json``）。
  - **绝不**触碰下单 / 撤单 / 成交归因核心逻辑；**绝不**读/写生产
    ``risk_state.json``。
  - **绝不抛异常**：任何失败都被吞掉并打印一行提示（fail-safe），
    以保证主流程零行为变更。

开关：
  - 环境变量 ``SHADOW_ENTRY_LOG``（``1/true/yes/on`` 开；``0/false/no/off`` 关）。
  - 未设置时回退到 ``config.SHADOW_ENTRY_LOG``；仍无则用 ``DEFAULT_ENABLED``。
  - ``DEFAULT_ENABLED = True``：Stage C 已放行，默认建立可复盘基线；
    设 ``SHADOW_ENTRY_LOG=0`` 可一键关闭（不影响任何交易行为）。

本模块**不做**风控计算，仅记录决策链原始数据（含 NAV 缺失时的显式标注），
供后续离线复盘与 Stage D 影子运行对照。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_ENABLED = True

_ROOT = Path(__file__).resolve().parent.parent
_LOG_DIR = Path(os.environ.get("SHADOW_ENTRY_LOG_DIR", str(_ROOT / "logs")))
_STATE_PATH = _LOG_DIR / "arbitration_shadow_state.json"

# 影子状态文件里保留的最近周期数（防止无限增长）
_MAX_CYCLES = 50

_MODE_LABEL = "shadow"


def _truthy(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "t", "yes", "y", "on")


def _falsy(val: Any) -> bool:
    if isinstance(val, bool):
        return not val
    return str(val).strip().lower() in ("0", "false", "f", "no", "n", "off")


def enabled() -> bool:
    """是否启用影子日志。env 优先，其次 config 属性，最后默认值。"""
    raw = os.environ.get("SHADOW_ENTRY_LOG")
    if raw is not None and str(raw).strip() != "":
        return _truthy(raw)
    try:
        import config as _config  # 延迟导入，避免 import 环

        cfg_val = getattr(_config, "SHADOW_ENTRY_LOG", None)
        if cfg_val is not None:
            return _truthy(cfg_val)
    except Exception:
        pass
    return bool(DEFAULT_ENABLED)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _append_jsonl(record: Dict[str, Any]) -> None:
    """追加一行到当日影子 JSONL。任何失败都无害。"""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        path = _LOG_DIR / f"shadow_entry_{stamp}.jsonl"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001 — 影子日志失败绝不打断主流程
        print(f"[SHADOW] ERROR: entry log write failed (non-fatal): {exc}")


def _load_state() -> Dict[str, Any]:
    try:
        if _STATE_PATH.exists():
            with open(_STATE_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                return data
    except Exception as exc:  # noqa: BLE001 — 损坏则重建，绝不外抛
        print(f"[SHADOW] WARN: shadow state unreadable ({exc}) → rebuilding")
    return {"version": 1, "cycles": {}}


def _save_state_atomic(state: Dict[str, Any]) -> None:
    """原子写（tmp + os.replace）到**独立**影子状态文件。失败无害。"""
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        state["updated_ts"] = _now_iso()
        tmp = f"{_STATE_PATH}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, default=str, indent=2)
        os.replace(tmp, _STATE_PATH)
    except Exception as exc:  # noqa: BLE001
        print(f"[SHADOW] ERROR: shadow state write failed (non-fatal): {exc}")


def _prune_cycles(cycles: Dict[str, Any]) -> Dict[str, Any]:
    if len(cycles) <= _MAX_CYCLES:
        return cycles
    try:
        ordered = sorted(
            cycles.items(), key=lambda kv: str(kv[1].get("ts") or ""), reverse=True
        )
        return dict(ordered[:_MAX_CYCLES])
    except Exception:
        return cycles


def _ensure_cycle(state: Dict[str, Any], cycle_id: str) -> Dict[str, Any]:
    cycles = state.setdefault("cycles", {})
    cyc = cycles.get(cycle_id)
    if not isinstance(cyc, dict):
        cyc = {"ts": _now_iso(), "entries": [], "blocks": [], "cum_units": 0}
        cycles[cycle_id] = cyc
    return cyc


def record_entry_decision(
    *,
    cycle_id: str,
    seq: int,
    pair: str,
    action: str,
    entry: Any,
    stop_loss: Any,
    take_profit: Any,
    risk_reward: Any = None,
    units: Optional[int] = None,
    strength_score: Any = None,
    strength_rank: Any = None,
    mc_conflict: Any = None,
    is_override: bool = False,
    override_type: Any = None,
    exit_mode: Any = None,
    dlot_scale: Any = None,
    dlot_reason: Any = None,
    prob_size_units: Any = None,
    prob_skip: bool = False,
    quote_ccy: Any = None,
    account_id: Any = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """记录一次**入场决策**（放行到下单前的完整决策链）。永不抛异常。"""
    if not enabled():
        return
    try:
        _sl = None
        _dist = None
        try:
            if entry is not None and stop_loss is not None:
                _dist = abs(float(entry) - float(stop_loss))
        except Exception:
            _dist = None
        rec: Dict[str, Any] = {
            "ts": _now_iso(),
            "event": "ENTRY_DECISION",
            "cycle_id": cycle_id,
            "seq": seq,
            "mode": _MODE_LABEL,
            "pair": pair,
            "action": action,
            "entry": entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "risk_reward": risk_reward,
            "sl_distance_abs": _dist,
            "units": units,
            "strength_score": strength_score,
            "strength_rank": strength_rank,
            "mc_conflict": mc_conflict,
            "is_override": bool(is_override),
            "override_type": override_type,
            "exit_mode": exit_mode,
            "dlot_scale": dlot_scale,
            "dlot_reason": dlot_reason,
            "prob_size_units": prob_size_units,
            "prob_skip": bool(prob_skip),
            "quote_ccy": quote_ccy,
            "account_id": account_id,
            # 定仓契约要求风险金额化；NAV 未接入（O-1）前无法计算 → 显式标注。
            "risk_amount": None,
            "risk_nav_available": False,
            "risk_note": "NAV unavailable (O-1 pending) — risk_amount not computable",
        }
        if extra:
            rec.update(extra)
        _append_jsonl(rec)

        state = _load_state()
        cyc = _ensure_cycle(state, cycle_id)
        cyc.setdefault("entries", []).append(
            {
                "seq": seq,
                "pair": pair,
                "action": action,
                "units": units,
                "sl_distance_abs": _dist,
                "risk_amount": None,
                "ts": rec["ts"],
            }
        )
        try:
            if units is not None:
                cyc["cum_units"] = int(cyc.get("cum_units") or 0) + int(units)
        except Exception:
            pass
        state["last_cycle_id"] = cycle_id
        state["cycles"] = _prune_cycles(state.get("cycles", {}))
        _save_state_atomic(state)
    except Exception as exc:  # noqa: BLE001 — 记录失败绝不外抛
        print(f"[SHADOW] ERROR: record_entry_decision failed (non-fatal): {exc}")


def record_entry_blocked(
    *,
    cycle_id: str,
    seq: int,
    pair: Any,
    action: Any,
    block_reason: Any,
    is_override: bool = False,
    account_id: Any = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """记录一次**入场被拦截 / 执行前校验未通过**（含 gate 原因）。永不抛异常。"""
    if not enabled():
        return
    try:
        rec: Dict[str, Any] = {
            "ts": _now_iso(),
            "event": "ENTRY_BLOCKED",
            "cycle_id": cycle_id,
            "seq": seq,
            "mode": _MODE_LABEL,
            "pair": pair,
            "action": action,
            "block_reason": block_reason,
            "is_override": bool(is_override),
            "account_id": account_id,
        }
        if extra:
            rec.update(extra)
        _append_jsonl(rec)

        state = _load_state()
        cyc = _ensure_cycle(state, cycle_id)
        cyc.setdefault("blocks", []).append(
            {"seq": seq, "pair": pair, "action": action, "block_reason": block_reason}
        )
        state["last_cycle_id"] = cycle_id
        state["cycles"] = _prune_cycles(state.get("cycles", {}))
        _save_state_atomic(state)
    except Exception as exc:  # noqa: BLE001
        print(f"[SHADOW] ERROR: record_entry_blocked failed (non-fatal): {exc}")


def record_cycle(
    *,
    cycle_id: str,
    basket_size: int,
    executed: int,
    rejected: int,
    skipped: int,
    override: int,
    positions: Optional[List[Dict[str, Any]]] = None,
    positions_error: Any = None,
    open_net: Optional[Dict[str, Any]] = None,
    account_id: Any = None,
    global_scores: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """记录**周期级**影子汇总：持仓/止损状态 + 同周期多笔累计。永不抛异常。"""
    if not enabled():
        return
    try:
        rec: Dict[str, Any] = {
            "ts": _now_iso(),
            "event": "CYCLE_SHADOW",
            "cycle_id": cycle_id,
            "mode": _MODE_LABEL,
            "basket_size": basket_size,
            "executed": executed,
            "rejected": rejected,
            "skipped": skipped,
            "override": override,
            "account_id": account_id,
            "positions": positions,
            "positions_error": positions_error,
            "open_net": open_net,
            "global_scores": global_scores,
            # 组合风险金额：NAV 未接入前不可算 → 显式标注（对齐定仓契约 §7）。
            "portfolio_risk_amount": None,
            "risk_nav_available": False,
        }
        if extra:
            rec.update(extra)
        _append_jsonl(rec)

        state = _load_state()
        cyc = _ensure_cycle(state, cycle_id)
        cyc["executed"] = executed
        cyc["rejected"] = rejected
        cyc["skipped"] = skipped
        cyc["override"] = override
        cyc["basket_size"] = basket_size
        cyc["positions_count"] = len(positions) if positions else None
        cyc["cum_units"] = cyc.get("cum_units") or 0
        cyc["portfolio_risk_amount"] = None
        state["last_cycle_id"] = cycle_id
        state["last_positions"] = positions
        state["cycles"] = _prune_cycles(state.get("cycles", {}))
        _save_state_atomic(state)
    except Exception as exc:  # noqa: BLE001
        print(f"[SHADOW] ERROR: record_cycle failed (non-fatal): {exc}")


def project_positions(trades: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """把 broker 开放交易投影为紧凑的持仓/止损视图（供影子日志用）。"""
    out: List[Dict[str, Any]] = []
    try:
        for t in trades or []:
            if not isinstance(t, dict):
                continue
            _sl = t.get("stopLossOnFill")
            if not isinstance(_sl, dict):
                _sl = t.get("stopLoss") if isinstance(t.get("stopLoss"), dict) else {}
            out.append(
                {
                    "trade_id": t.get("id"),
                    "instrument": t.get("instrument"),
                    "units": t.get("currentUnits") or t.get("units"),
                    "price": t.get("price"),
                    "unrealizedPL": t.get("unrealizedPL"),
                    "sl_price": (_sl or {}).get("price"),
                    "tag": ((t.get("clientExtensions") or {}) or {}).get("tag"),
                }
            )
    except Exception as exc:  # noqa: BLE001
        print(f"[SHADOW] WARN: project_positions failed (non-fatal): {exc}")
    return out
