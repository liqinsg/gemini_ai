"""utils/prob_zone_sizing.py — 概率路线仓位执行覆盖层 (gemini_ai · 只缩不放大).

依据 fx_monte_carlo 的 MC 概率标注（``prob_bands`` / ``belief`` / ``distribution``）
在**开仓时**按方向确信度对仓位做 shrink-only 调整。

本模块**只允许改一件事**：仓位（``size_units``）。
    * 绝不触碰：信号方向 / 打分权重 / SL / TP / MC 融合。
    * 绝不放大：``size_scale`` ∈ ``[F_LO, 1.0]``。

三条轴
------
    方向确信度 (L0)：S = |p_up − p_down| / 100  →  conv(S)             [死区 + 线性斜坡]
    风险封顶   (L1)：risk_cap(W) = clamp(R_REF / W, C_LO, 1.0)         [W = 区间宽度 %]
    合成           ：size_scale = min(conv, risk_cap, regime_mult)     [独立封顶 · 非乘法]

fail-closed
-----------
「不知道 ≠ 安全」：数据缺失 / 过期 / 退化 / 窄区间 / p_up 非法
→ ``size_scale`` **只取 F_LO**（禁止满仓），且**不进入** conv/risk_cap。

契约
----
只读（C-1）；数据访问全部经 ``interface.py``；``compute_prob_plan`` 永不抛异常，
异常 → floor plan（fail-closed，绝不回退到 1.0）。
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 本阶段只支持 Daily 快照；"W" 等一律按"数据不可用"降级，绝不抛异常。
SUPPORTED_TIMEFRAMES = ("D", "W")

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"

# 权威副本在 config.py(PROB_ZONE_SIZING)；仅当调用方传入不完整 / 空的 cfg 时兜底。
_CFG_FALLBACK: Dict[str, Any] = {
    "ENABLED": True,
    "S0": 0.10,
    "S1": 0.40,
    "F_LO": 0.25,
    "R_REF": 2.0,
    "C_LO": 0.25,
    "MIN_LOT_LIVE": 1,
    "MIN_LOT_DEMO": 1000,
    "REQUIRE_ALIGNED": False,
    "MC_MIN_ALIGNED_PCT": 50.0,
    "REGIME_MIN_MULT": 0.25,
    "VERDICT_MODE": "soft",
    "VERDICT_SOFT_MULT": 0.5,
    "FAILCLOSED_SKIP": False,
}


# ─────────────────────────────────────────────────────────────────────────────
# 数据访问（经 interface.py；interface 不可用 → fail-closed）
# ─────────────────────────────────────────────────────────────────────────────
def load_prob_index(day: str, timeframe: str = "D") -> Dict[str, Any]:
    """加载一次 MC 快照并返回 ``results`` 索引（永不抛异常，失败返回 ``{}``）。"""
    if timeframe not in SUPPORTED_TIMEFRAMES:
        logger.warning(
            "Prob Zone: timeframe %r unsupported (only %s) — prob unavailable",
            timeframe,
            "/".join(SUPPORTED_TIMEFRAMES),
        )
        return {}
    try:
        import interface as mc_iface
    except Exception as exc:  # pragma: no cover - environment specific
        logger.warning("Prob Zone: interface.py unavailable (%s) — prob unavailable", exc)
        return {}
    try:
        bundle = mc_iface.get_latest_fib_mc_data(day, timeframe)
    except Exception as exc:
        logger.warning(
            "Prob Zone: get_latest_fib_mc_data(%s, %s) failed (%s) — prob unavailable",
            day,
            timeframe,
            exc,
        )
        return {}
    results = (bundle or {}).get("results") if isinstance(bundle, dict) else None
    return results if isinstance(results, dict) else {}


def _advisory(rec: Any) -> Optional[Dict[str, Any]]:
    """经 ``interface.get_pair_advisory()`` 取标注状态；不可用 → None（fail-closed）。"""
    try:
        import interface as mc_iface
    except Exception as exc:  # pragma: no cover - environment specific
        logger.warning("Prob Zone: interface.py unavailable (%s) — fail-closed", exc)
        return None
    try:
        return mc_iface.get_pair_advisory(rec)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Prob Zone: get_pair_advisory failed (%s) — fail-closed", exc)
        return None


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


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _normalize_direction(direction: Any) -> Optional[str]:
    try:
        text = str(direction).strip().upper()
    except Exception:  # pragma: no cover
        return None
    return text if text in ("BUY", "SELL") else None


def to_mc_key(pair: Any) -> str:
    """OANDA ``USD_JPY`` / ``USDJPY`` / ``USDJPY=X`` → MC 快照键 ``USDJPY=X``（永不抛异常）。"""
    try:
        text = (
            str(pair)
            .strip()
            .upper()
            .replace("=X", "")
            .replace("_", "")
            .replace("/", "")
        )
    except Exception:  # pragma: no cover
        return ""
    return f"{text}=X"


# ─────────────────────────────────────────────────────────────────────────────
# 三个数学件
# ─────────────────────────────────────────────────────────────────────────────
def _conv(s_value: float, s0: float, s1: float, f_lo: float) -> float:
    """方向确信度映射（死区 + 线性斜坡），值域 [F_LO, 1.0]，只缩不放大。"""
    f_lo = _clamp(f_lo, 0.0, 1.0)
    s0 = _clamp(s0, 0.0, 1.0)
    s1 = max(s0, _clamp(s1, 0.0, 1.0))
    if s_value <= s0:
        return f_lo
    if s_value >= s1:
        return 1.0
    ramp = (s_value - s0) / (s1 - s0)
    return _clamp(f_lo + (1.0 - f_lo) * ramp, f_lo, 1.0)


def _risk_cap(w_pct: float, r_ref: float, c_lo: float) -> float:
    """风险封顶（倒数封顶），值域 [C_LO, 1.0]，W ≤ R_REF 时不约束。"""
    c_lo = _clamp(c_lo, 0.0, 1.0)
    if w_pct <= 0:
        return 1.0
    if r_ref <= 0:
        return 1.0
    return _clamp(r_ref / w_pct, c_lo, 1.0)


def _regime_mult(regime: Any, min_mult: float) -> float:
    """无方向 regime 文本 → 仓位倍率（只降不升）。

    MOMENTUM / OVERBOUGHT / OVERSOLD（有边）→ 1.0；
    CONSOLIDATION / RANGE / NEUTRAL（无边）→ min_mult；
    未识别 / 缺失 → 1.0（不因未知而降级，方向由 MC 门控负责）。
    """
    text = regime.upper() if isinstance(regime, str) else ""
    if "MOMENTUM" in text or "OVERBOUGHT" in text or "OVERSOLD" in text:
        return 1.0
    if "CONSOLIDATION" in text or "RANGE" in text or "NEUTRAL" in text:
        return _clamp(min_mult, 0.0, 1.0)
    return 1.0


def _verdict_mult(verdict: Any, soft_mult: float) -> float:
    """无方向 verdict 文本 → 仓位折减倍率（只降不升，软惩罚）。"""
    text = verdict.upper() if isinstance(verdict, str) else ""
    if "MOMENTUM" in text or "OVERBOUGHT" in text or "OVERSOLD" in text:
        return 1.0
    if "NO_EDGE" in text or "RANGE" in text or "NEUTRAL" in text:
        return _clamp(soft_mult, 0.0, 1.0)
    return 1.0


def _width_pct(rec: Dict[str, Any]) -> Optional[float]:
    """区间宽度 W = (q95 − q05) / current_price × 100（%）。"""
    if not isinstance(rec, dict):
        return None
    rng = rec.get("range_90")
    if not isinstance(rng, (list, tuple)) or len(rng) != 2:
        return None
    lower = _as_float(rng[0])
    upper = _as_float(rng[1])
    price = _as_float(rec.get("current_price"))
    if lower is None or upper is None or price is None or price <= 0:
        return None
    return (upper - lower) / price * 100.0


def _p_up_down(rec: Dict[str, Any]) -> "tuple":
    """返回 (p_up, p_down)；非法 / 不自洽 → (None, None)（fail-closed）。"""
    if not isinstance(rec, dict):
        return None, None
    p_up = _as_float(rec.get("p_up"))
    if p_up is None or p_up < 0.0 or p_up > 100.0:
        return None, None
    p_down = _as_float(rec.get("p_down"))
    if p_down is None:
        p_down = 100.0 - p_up
    if p_down < 0.0 or p_down > 100.0:
        return None, None
    if abs((p_up + p_down) - 100.0) > 0.5:  # 不自洽
        return None, None
    return p_up, p_down


# ─────────────────────────────────────────────────────────────────────────────
# 返回契约
# ─────────────────────────────────────────────────────────────────────────────
def _size_units(base_lot: Any, size_scale: float, min_lot: int) -> int:
    """int(base_lot · scale + 0.5)（禁用 banker's rounding）后以 MIN_LOT 兜底。"""
    lot = _as_float(base_lot)
    if lot is None or lot <= 0:
        lot = 1.0
    units = int(lot * float(size_scale) + 0.5)
    return max(int(min_lot), units)


def _base_plan(
    base_lot: Any,
    cfg: Dict[str, Any],
    reason: str,
    *,
    size_scale: float,
    skip: bool = False,
    advisory: Optional[Dict[str, Any]] = None,
    p_up: Optional[float] = None,
    p_down: Optional[float] = None,
    aligned: Optional[float] = None,
    s_value: Optional[float] = None,
    conv: Optional[float] = None,
    width_pct: Optional[float] = None,
    risk_cap: Optional[float] = None,
    regime_mult: Optional[float] = None,
) -> Dict[str, Any]:
    """完整契约字典（永远包含全部键）。"""
    f_lo = _as_float(_cfg_get(cfg, "F_LO", _CFG_FALLBACK["F_LO"]))
    f_lo = 0.25 if f_lo is None else _clamp(f_lo, 0.0, 1.0)
    min_lot = _as_int(_cfg_get(cfg, "MIN_LOT", 1), 1)
    if min_lot < 1:
        min_lot = 1
    advisory = advisory if isinstance(advisory, dict) else {}
    return {
        "skip": bool(skip),
        "reason": reason,
        "advisory_state": advisory.get("state", "unknown"),
        "verdict": advisory.get("verdict", "NO_EDGE"),
        "p_up": p_up,
        "p_down": p_down,
        "aligned": aligned,
        "S": s_value,
        "conv": conv,
        "W": width_pct,
        "risk_cap": risk_cap,
        "regime_mult": regime_mult,
        "f_lo": f_lo,
        "size_scale": float(size_scale),
        "size_units": _size_units(base_lot, size_scale, min_lot),
    }


def _floor_plan(
    base_lot: Any,
    cfg: Dict[str, Any],
    reason: str,
    *,
    skip: bool = False,
    advisory: Optional[Dict[str, Any]] = None,
    p_up: Optional[float] = None,
    p_down: Optional[float] = None,
    aligned: Optional[float] = None,
) -> Dict[str, Any]:
    """fail-closed 计划：size_scale 只取 F_LO，禁止满仓。"""
    f_lo = _as_float(_cfg_get(cfg, "F_LO", _CFG_FALLBACK["F_LO"]))
    f_lo = 0.25 if f_lo is None else _clamp(f_lo, 0.0, 1.0)
    return _base_plan(
        base_lot,
        cfg,
        reason,
        size_scale=f_lo,
        skip=skip,
        advisory=advisory,
        p_up=p_up,
        p_down=p_down,
        aligned=aligned,
    )


def _neutral_plan(base_lot: Any, cfg: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """未启用 / 方向非法：不做任何缩放（size_scale=1.0）。"""
    return _base_plan(base_lot, cfg, reason, size_scale=1.0)


def compute_weekly_structure_plan(
    *,
    direction: Any,
    entry: Any,
    weekly_atr: Any,
    forward_level: Any,
    cfg: Optional[Dict[str, Any]] = None,
    base_lot: Any = 1,
) -> Dict[str, Any]:
    """Conservative W-structure cap. No level is usable unless it is ahead of price."""
    c = cfg if isinstance(cfg, dict) else {}
    normalized = _normalize_direction(direction)
    entry_f = _as_float(entry)
    atr_f = _as_float(weekly_atr)
    level_f = _as_float(forward_level)
    if normalized is None or entry_f is None or atr_f is None or atr_f <= 0 or level_f is None:
        return _floor_plan(base_lot, c, "weekly_structure_unavailable", skip=True)

    if normalized == "BUY":
        if level_f <= entry_f:
            return _base_plan(base_lot, c, "weekly_level_not_forward", size_scale=1.0)
        distance_atr = (level_f - entry_f) / atr_f
    else:
        if level_f >= entry_f:
            return _base_plan(base_lot, c, "weekly_level_not_forward", size_scale=1.0)
        distance_atr = (entry_f - level_f) / atr_f

    block = _as_float(c.get("BLOCK_DISTANCE_ATR"), 1.0)
    tight = _as_float(c.get("TIGHT_DISTANCE_ATR"), 2.0)
    reduce = _as_float(c.get("REDUCE_DISTANCE_ATR"), 3.0)
    tight_scale = _as_float(c.get("TIGHT_SCALE"), 0.25)
    reduce_scale = _as_float(c.get("REDUCE_SCALE"), 0.50)
    block = 1.0 if block is None else block
    tight = 2.0 if tight is None else tight
    reduce = 3.0 if reduce is None else reduce
    tight_scale = 0.25 if tight_scale is None else _clamp(tight_scale, 0.0, 1.0)
    reduce_scale = 0.50 if reduce_scale is None else _clamp(reduce_scale, 0.0, 1.0)

    if distance_atr <= block:
        scale = 0.0
        reason = "weekly_structure_conflict_block"
        skip = True
    elif distance_atr <= tight:
        scale = tight_scale
        reason = "weekly_structure_tight"
        skip = False
    elif distance_atr <= reduce:
        scale = reduce_scale
        reason = "weekly_structure_reduce"
        skip = False
    else:
        scale = 1.0
        reason = "weekly_structure_clear"
        skip = False

    plan = _base_plan(
        base_lot,
        c,
        reason,
        size_scale=scale,
        skip=skip,
        width_pct=distance_atr,
    )
    plan["forward_level"] = level_f
    plan["distance_atr"] = distance_atr
    return plan


# ─────────────────────────────────────────────────────────────────────────────
# 唯一对外的覆盖层入口
# ─────────────────────────────────────────────────────────────────────────────
def compute_prob_plan(
    rec: Any,
    direction: str,
    cfg: Optional[Dict[str, Any]],
    base_lot: int,
    regime: Any = None,
) -> Dict[str, Any]:
    """单个 pair 的概率路线**仓位**计划（永不抛异常，永远返回完整契约）。

    ``direction`` 必须是 BUY / SELL（BUY=LONG, SELL=SHORT），其余降级为 floor plan。
    本函数只决定 ``size_units``（shrink-only）；不改 TP / SL / 方向。
    """
    cfg_dict: Dict[str, Any] = cfg if isinstance(cfg, dict) else {}
    try:
        # 覆盖层总开关（关闭 → 原样返回，不缩放）。
        if not _as_bool(_cfg_get(cfg_dict, "ENABLED", _CFG_FALLBACK["ENABLED"]), True):
            return _neutral_plan(base_lot, cfg_dict, "disabled")

        record: Dict[str, Any] = rec if isinstance(rec, dict) else {}

        normalized = _normalize_direction(direction)
        if normalized is None:
            logger.error(
                "Prob Zone: direction %r rejected — only BUY/SELL allowed", direction
            )
            return _floor_plan(base_lot, cfg_dict, "prob_invalid_direction")

        # ── 1) 标注健康闸门（fail-closed）──────────────────────────────────
        advisory = _advisory(record)
        state = advisory.get("state") if isinstance(advisory, dict) else None
        if state != "healthy":
            logger.warning(
                "Prob Zone: advisory state=%s (verdict=%s) → fail-closed (F_LO)",
                state or "unavailable",
                (advisory or {}).get("verdict", "?"),
            )
            return _floor_plan(
                base_lot,
                cfg_dict,
                f"advisory_{state or 'unavailable'}",
                skip=_as_bool(_cfg_get(cfg_dict, "FAILCLOSED_SKIP", False), False),
                advisory=advisory if isinstance(advisory, dict) else None,
            )

        # ── 2) L0 方向概率（唯一真理源）───────────────────────────────────
        p_up, p_down = _p_up_down(record)
        if p_up is None:
            logger.warning("Prob Zone: p_up/p_down missing or inconsistent → fail-closed")
            return _floor_plan(
                base_lot,
                cfg_dict,
                "p_up_invalid",
                skip=_as_bool(_cfg_get(cfg_dict, "FAILCLOSED_SKIP", False), False),
                advisory=advisory,
            )

        aligned = p_up if normalized == "BUY" else p_down

        # ── 3) 开仓门控（默认关闭：只缩仓，不拦截开仓）───────────────────
        min_aligned = _as_float(
            _cfg_get(cfg_dict, "MC_MIN_ALIGNED_PCT", _CFG_FALLBACK["MC_MIN_ALIGNED_PCT"])
        )
        min_aligned = 50.0 if min_aligned is None else _clamp(min_aligned, 0.0, 100.0)
        if _as_bool(
            _cfg_get(cfg_dict, "REQUIRE_ALIGNED", _CFG_FALLBACK["REQUIRE_ALIGNED"]), False
        ) and aligned < min_aligned:
            logger.warning(
                "Prob Zone: MC direction conflict — %s aligned=%.1f%% < %.1f%% → SKIP",
                normalized,
                aligned,
                min_aligned,
            )
            return _floor_plan(
                base_lot,
                cfg_dict,
                "mc_direction_conflict",
                skip=True,
                advisory=advisory,
                p_up=p_up,
                p_down=p_down,
                aligned=aligned,
            )

        # ── 4) 三轴合成 ───────────────────────────────────────────────────
        s_value = abs(p_up - p_down) / 100.0
        conv = _conv(
            s_value,
            _as_float(_cfg_get(cfg_dict, "S0", _CFG_FALLBACK["S0"]), 0.10),
            _as_float(_cfg_get(cfg_dict, "S1", _CFG_FALLBACK["S1"]), 0.40),
            _as_float(_cfg_get(cfg_dict, "F_LO", _CFG_FALLBACK["F_LO"]), 0.25),
        )
        width_pct = _width_pct(record)
        risk_cap = (
            _risk_cap(
                width_pct,
                _as_float(_cfg_get(cfg_dict, "R_REF", _CFG_FALLBACK["R_REF"]), 2.0),
                _as_float(_cfg_get(cfg_dict, "C_LO", _CFG_FALLBACK["C_LO"]), 0.25),
            )
            if width_pct is not None
            else 1.0
        )

        regime_text = regime if regime else record.get("regime")
        regime_mult = _regime_mult(
            regime_text,
            _as_float(
                _cfg_get(cfg_dict, "REGIME_MIN_MULT", _CFG_FALLBACK["REGIME_MIN_MULT"]),
                0.25,
            ),
        )

        f_lo = _as_float(_cfg_get(cfg_dict, "F_LO", _CFG_FALLBACK["F_LO"]), 0.25)
        size_scale = _clamp(min(conv, risk_cap, regime_mult), f_lo, 1.0)
        reason = "ok"

        # ── 5) verdict 门控（VERDICT_MODE：strict / soft / off）──────────
        verdict = advisory.get("verdict") if isinstance(advisory, dict) else None
        verdict_mode = str(
            _cfg_get(cfg_dict, "VERDICT_MODE", _CFG_FALLBACK["VERDICT_MODE"]) or "soft"
        ).strip().lower()
        if verdict_mode == "strict":
            if verdict == "NO_EDGE":
                size_scale = min(size_scale, f_lo)
                reason = "verdict_no_edge"
        elif verdict_mode == "soft":
            soft_mult = _as_float(
                _cfg_get(cfg_dict, "VERDICT_SOFT_MULT", _CFG_FALLBACK["VERDICT_SOFT_MULT"])
            )
            soft_mult = 0.5 if soft_mult is None else _clamp(soft_mult, 0.0, 1.0)
            penalty = _verdict_mult(verdict, soft_mult)
            if penalty < 1.0:
                size_scale = _clamp(size_scale * penalty, f_lo, 1.0)
                reason = "verdict_soft_penalty"

        return _base_plan(
            base_lot,
            cfg_dict,
            reason,
            size_scale=size_scale,
            advisory=advisory,
            p_up=p_up,
            p_down=p_down,
            aligned=aligned,
            s_value=s_value,
            conv=conv,
            width_pct=width_pct,
            risk_cap=risk_cap,
            regime_mult=regime_mult,
        )
    except Exception:  # pragma: no cover - 防御：绝不向上传播
        logger.exception("Prob Zone: compute_prob_plan failed — fail-closed floor plan")
        return _floor_plan(base_lot, cfg_dict, "prob_error")


# ─────────────────────────────────────────────────────────────────────────────
# 审计日志（每笔候选一行，开仓与 skip 都记录）
# ─────────────────────────────────────────────────────────────────────────────
def format_prob_audit(pair: str, direction: str, plan: Optional[Dict[str, Any]]) -> str:
    """渲染强制审计字段为单行 ``key=value | ...``（永不抛异常）。"""
    try:
        safe: Dict[str, Any] = plan if isinstance(plan, dict) else {}

        def _num(value: Any, digits: int = 2) -> str:
            number = _as_float(value)
            return "None" if number is None else f"{number:.{digits}f}"

        parts = [
            f"pair={pair}",
            f"dir={direction}",
            f"reason={safe.get('reason', '?')}",
            f"state={safe.get('advisory_state', '?')}",
            f"verdict={safe.get('verdict', '?')}",
            f"P_UP={_num(safe.get('p_up'), 1)}",
            f"aligned={_num(safe.get('aligned'), 1)}",
            f"S={_num(safe.get('S'), 3)}",
            f"conv={_num(safe.get('conv'))}",
            f"W%={_num(safe.get('W'), 2)}",
            f"risk_cap={_num(safe.get('risk_cap'))}",
            f"regime_mult={_num(safe.get('regime_mult'))}",
            f"size_scale={_num(safe.get('size_scale'))}",
            f"units={safe.get('size_units')}",
        ]
        return " | ".join(parts)
    except Exception:  # pragma: no cover - 审计绝不能打断运行
        return f"pair={pair} | dir={direction} | reason=audit_error"


def log_sizing_shadow(
    record: Dict[str, Any], logs_dir: Optional[Path] = None
) -> Optional[str]:
    """追加一行影子记录到 ``logs/lots_sizing_shadow_YYYYMMDD.jsonl``（永不抛异常）。"""
    try:
        rec = dict(record) if isinstance(record, dict) else {}
        now = datetime.now(timezone.utc)
        rec.setdefault("ts", now.isoformat(timespec="seconds"))
        directory = Path(logs_dir) if logs_dir is not None else LOGS_DIR
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"lots_sizing_shadow_{now.strftime('%Y%m%d')}.jsonl"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return str(path)
    except Exception as exc:  # pragma: no cover - 审计绝不能打断运行
        logger.warning("prob_zone_sizing: shadow write skipped (%s)", exc)
        return None


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    _demo = {
        "p_up": 62.0,
        "p_down": 38.0,
        "current_price": 1.10,
        "range_90": [1.08, 1.12],
        "regime": "NEUTRAL",
        "prob_bands": {"degenerate": False},
        "belief": {"verdict": "UP", "expires_utc": "2099-01-01T00:00:00+00:00"},
        "distribution": {"degenerate": False},
    }
    _plan = compute_prob_plan(_demo, "BUY", None, 10000)
    print(format_prob_audit("EURUSD=X", "BUY", _plan))
