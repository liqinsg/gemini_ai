"""
PostExitGate — Shadow Evaluation & Active-Exit Risk Control
RFC v4.3 · COUNT=1 LENIENT START, FULL RECORDING
  - 1×ACTIVE → NOTICE ×1.1 / no cooldown / log
  - 2×ACTIVE → ELEVATED ×1.3 / 2h cooldown
  - 3×ACTIVE → HIGH ×1.5 / 4h cooldown
  - All verdicts logged regardless of enforcement
"""
from __future__ import annotations
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Deque, Literal
from collections import deque
import config as _config

# ============================================================
# 🎯 【参数区 · 已按 COUNT=1 配置】改数字不动逻辑
# ============================================================
def _cfg(name: str, default: Any) -> Any:
    return getattr(_config, name, default)

class PostExitGate:
    # ——— 基础参数 ———
    ENABLE_RISK: bool = _cfg("RISK_ENABLE", True)
    HALF_LIFE_HOURS: float = _cfg("POST_EXIT_HALF_LIFE_HOURS", 8.0)
    MAX_HISTORY: int = _cfg("POST_EXIT_MAX_HISTORY", 4)
    FLOOR_DECAY: float = _cfg("POST_EXIT_FLOOR_DECAY", 0.45)
    STRICT_WINDOW_HOURS: float = _cfg("POST_EXIT_STRICT_WINDOW", 1.5)

    # ——— 风控阈值 · COUNT=1 最宽松 ———
    NOTICE_COUNT:   int = _cfg("RISK_NOTICE_COUNT",   1)   # 1单 → 提示
    ELEVATED_COUNT: int = _cfg("RISK_ELEV_COUNT",     2)   # 2单 → 上升
    HIGH_COUNT:     int = _cfg("RISK_HIGH_COUNT",     3)   # 3单 → 高风险

    NOTICE_RATIO:   float = _cfg("RISK_NOTICE_RATIO", 0.50)
    ELEVATED_RATIO: float = _cfg("RISK_ELEV_RATIO",   0.67)
    HIGH_RATIO:    float = _cfg("RISK_HIGH_RATIO",   0.75)

    # ——— 风控力度 ———
    MULT_NOTICE:   float = _cfg("RISK_MULT_NOTICE",   1.1)  # 1单：温和+10%
    MULT_ELEVATED: float = _cfg("RISK_MULT_ELEV",     1.3)  # 2单：+30%
    MULT_HIGH:     float = _cfg("RISK_MULT_HIGH",     1.5)  # 3单：+50%

    COOL_NOTICE:   float = _cfg("RISK_COOL_NOTICE",   0.0)  # 1单：不冷却
    COOL_ELEVATED: float = _cfg("RISK_COOL_ELEV",     2.0)  # 2单：冷却2h
    COOL_HIGH:     float = _cfg("RISK_COOL_HIGH",     4.0)  # 3单：冷却4h

    # ============================================================
    # 👇 下面无需修改 · 自动适配参数
    # ============================================================

    _history: Deque["ExitRecord"] = deque(maxlen=MAX_HISTORY)
    _cooling_until: float = 0.0

    @dataclass
    class ExitRecord:
        reason: Literal["SL", "TP", "ACTIVE"]
        timestamp: float = field(default_factory=time.time)

    EXIT_REASON = Literal["SL", "TP", "ACTIVE"]

    # ==============================================
    #  STEP 1: 记录平仓原因
    # ==============================================
    @classmethod
    def record_exit(cls, reason: EXIT_REASON) -> None:
        """平仓后必调：record_exit("SL"/"TP"/"ACTIVE")"""
        cls._history.append(cls.ExitRecord(reason=reason))
        cls._update_risk_state()

    # ==============================================
    #  STEP 2: 统计 & 风控判断
    # ==============================================
    @classmethod
    def get_exit_stats(cls) -> Dict[str, Any]:
        total = len(cls._history)
        if total == 0:
            return {"total":0,"sl":0,"tp":0,"active":0,
                    "active_ratio":0.0,"risk_level":"normal"}

        sl  = sum(1 for r in cls._history if r.reason == "SL")
        tp  = sum(1 for r in cls._history if r.reason == "TP")
        act = sum(1 for r in cls._history if r.reason == "ACTIVE")
        ratio = act / total

        if   act >= cls.HIGH_COUNT     and ratio >= cls.HIGH_RATIO:
            level = "high"
        elif act >= cls.ELEVATED_COUNT and ratio >= cls.ELEVATED_RATIO:
            level = "elevated"
        elif act >= cls.NOTICE_COUNT   and ratio >= cls.NOTICE_RATIO:
            level = "notice"
        else:
            level = "normal"

        return {"total":total,"sl":sl,"tp":tp,"active":act,
                "active_ratio":round(ratio,2),"risk_level":level}

    @classmethod
    def _update_risk_state(cls) -> None:
        st = cls.get_exit_stats()
        now = time.time()
        if   st["risk_level"] == "high":     cls._cooling_until = now + cls.COOL_HIGH * 3600
        elif st["risk_level"] == "elevated": cls._cooling_until = now + cls.COOL_ELEVATED * 3600
        elif st["risk_level"] == "notice":   cls._cooling_until = now + cls.COOL_NOTICE * 3600

    @classmethod
    def get_risk_multiplier(cls) -> float:
        lv = cls.get_exit_stats()["risk_level"]
        if   lv == "high":     return cls.MULT_HIGH
        elif lv == "elevated": return cls.MULT_ELEVATED
        elif lv == "notice":   return cls.MULT_NOTICE
        return 1.0

    @classmethod
    def in_cooling(cls) -> bool:
        return time.time() < cls._cooling_until

    @classmethod
    def cooling_remaining_hours(cls) -> float:
        return max(0.0, (cls._cooling_until - time.time()) / 3600) if cls.in_cooling() else 0.0

    # ==============================================
    #  STEP 3: 影子评估（原始逻辑不变）
    # ==============================================
    @classmethod
    def evaluate_shadow(
        cls,
        baseline: float,
        m_reason: float,
        consecutive_failures: int,
        elapsed_hours: float,
        alignment: int,
        rank: int,
        gap_delta: float,
    ) -> Dict[str, Any]:
        m_decay = max(math.exp(-elapsed_hours / cls.HALF_LIFE_HOURS), cls.FLOOR_DECAY)
        if elapsed_hours <= cls.STRICT_WINDOW_HOURS:
            m_decay = 1.0

        m_streak = 1.0 + 0.25 * (consecutive_failures - 1)
        m_streak = min(m_streak, 3.0) if consecutive_failures > 0 else 1.0

        reset = alignment == 3 and rank == 1 and gap_delta >= baseline * 1.5
        if reset:
            m_decay, m_streak = 1.0, 1.0

        risk_mult = cls.get_risk_multiplier()
        hurdle = baseline * m_reason * m_streak * m_decay * risk_mult
        verdict = "WOULD_PASS" if gap_delta >= hurdle else "WOULD_REJECT"
        st = cls.get_exit_stats()

        return {
            "verdict": verdict,
            "effective_hurdle": round(hurdle, 6),
            "m_decay": round(m_decay, 6),
            "m_streak": round(m_streak, 6),
            "risk_multiplier": round(risk_mult, 2),
            "risk_level": st["risk_level"],
            "active_ratio": st["active_ratio"],
            "in_cooling": cls.in_cooling(),
            "cooling_remaining_hours": round(cls.cooling_remaining_hours(), 2),
            "regime_reset_triggered": reset,
        }

    # ==============================================
    # ✅ STEP 4: 统一入口 — 开仓前检查+日志+风控
    # ==============================================
    @classmethod
    def check_risk_before_open(
        cls,
        baseline: float,
        m_reason: float,
        consecutive_failures: int,
        elapsed_hours: float,
        alignment: int,
        rank: int,
        gap_delta: float,
    ) -> tuple[bool, Dict[str, Any]]:
        """
        开仓前统一调用：返回(是否放行, 完整详情)
        - 无论拦不拦，完整日志必打、状态必存
        - ENABLE_RISK=False 时只观察、不拦截
        """
        result = cls.evaluate_shadow(
            baseline=baseline,
            m_reason=m_reason,
            consecutive_failures=consecutive_failures,
            elapsed_hours=elapsed_hours,
            alignment=alignment,
            rank=rank,
            gap_delta=gap_delta,
        )

        # ✅ 强制完整记录 — 永远执行
        print(
            f"[风控日志] 裁决:{result['verdict']} | 风险:{result['risk_level']} "
            f"乘数:×{result['risk_multiplier']} | 冷却:{result['cooling_remaining_hours']}h "
            f"主动占比:{result['active_ratio']}"
        )
        if result["in_cooling"]:
            print(f"⏳ 处于冷却期，剩余 {result['cooling_remaining_hours']}h")

        # ✅ 风控判断
        if cls.ENABLE_RISK and result["verdict"] == "WOULD_REJECT":
            print(f"🚫 风控拦截: {result['risk_level']} 风险，门槛放大 ×{result['risk_multiplier']}")
            return False, result
        return True, result

    # ==============================================
    #  工具：重置历史
    # ==============================================
    @classmethod
    def clear_history(cls) -> None:
        cls._history.clear()
        cls._cooling_until = 0.0