"""
PostExitGate — Shadow Evaluation & Active-Exit Risk Control
RFC v4.1 · Shadow-first, observant, non-blocking, evolving with practice.
Design: Record → Retrieve → Analyze → Adjust Hurdle.
"""
from __future__ import annotations
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Deque, Literal
from collections import deque
import config as _config

# === Config Helpers ===
def _cfg(name: str, default: Any) -> Any:
    return getattr(_config, name, default)

# === Type Definitions ===
EXIT_REASON = Literal["SL", "TP", "ACTIVE"]  # 止损 / 止盈 / 主动平仓

# === Data Model: Exit Record ===
@dataclass
class ExitRecord:
    """Single exit record: reason + timestamp"""
    reason: EXIT_REASON
    timestamp: float = field(default_factory=time.time)

# === Main Gate Class ===
class PostExitGate:
    """
    Shadow gate with active-exit risk amplification.
    Workflow: Record exits → Analyze patterns → Raise hurdle → Return verdict.
    All verdicts are shadow-logged; callers ignore for live decisions during RFC.
    """

    # ——— Configurable Constants ———
    HALF_LIFE_HOURS: float = _cfg("POST_EXIT_HALF_LIFE_HOURS", 8.0)
    MAX_HISTORY: int = _cfg("POST_EXIT_MAX_HISTORY", 3)  # Track last N exits
    FLOOR_DECAY: float = _cfg("POST_EXIT_FLOOR_DECAY", 0.45)
    STRICT_WINDOW_HOURS: float = _cfg("POST_EXIT_STRICT_WINDOW", 1.5)  # 90min strict

    # Risk Rules
    RATIO_ELEVATED: float = _cfg("POST_EXIT_RATIO_ELEVATED", 0.67)  # ≥2/3 active → elevated
    RATIO_HIGH: float = _cfg("POST_EXIT_RATIO_HIGH", 1.0)  # 100% active ×2 → high risk
    MULTIPLIER_ELEVATED: float = _cfg("POST_EXIT_MULT_ELEVATED", 1.3)
    MULTIPLIER_HIGH: float = _cfg("POST_EXIT_MULT_HIGH", 1.5)
    COOLING_ELEVATED_H: float = _cfg("POST_EXIT_COOLING_ELEVATED", 4.0)
    COOLING_HIGH_H: float = _cfg("POST_EXIT_COOLING_HIGH", 8.0)

    # ——— In-Memory Exit History ———
    _history: Deque[ExitRecord] = deque(maxlen=MAX_HISTORY)
    _cooling_until: float = 0.0  # Timestamp until cooling expires

    # ==============================================
    #  STEP 1: RECORD — Save exit reason
    # ==============================================
    @classmethod
    def record_exit(cls, reason: EXIT_REASON) -> None:
        """
        Call EVERY TIME a position closes.
        reason: 'SL' | 'TP' | 'ACTIVE'
        """
        cls._history.append(ExitRecord(reason=reason))
        cls._update_risk_state()

    # ==============================================
    #  STEP 2: RETRIEVE & ANALYZE — Count & Ratio
    # ==============================================
    @classmethod
    def get_exit_stats(cls) -> Dict[str, Any]:
        """Retrieve and compute stats from history"""
        total = len(cls._history)
        if total == 0:
            return {
                "total": 0, "sl": 0, "tp": 0, "active": 0,
                "active_ratio": 0.0, "risk_level": "normal"
            }

        sl  = sum(1 for r in cls._history if r.reason == "SL")
        tp  = sum(1 for r in cls._history if r.reason == "TP")
        act = sum(1 for r in cls._history if r.reason == "ACTIVE")
        active_ratio = act / total

        if active_ratio >= cls.RATIO_HIGH and act >= 2:
            risk_level = "high"
        elif active_ratio >= cls.RATIO_ELEVATED:
            risk_level = "elevated"
        else:
            risk_level = "normal"

        return {
            "total": total,
            "sl": sl, "tp": tp, "active": act,
            "active_ratio": round(active_ratio, 2),
            "risk_level": risk_level,
        }

    # ==============================================
    #  STEP 3: RULES — Update cooling & multiplier
    # ==============================================
    @classmethod
    def _update_risk_state(cls) -> None:
        """Internal: apply rules → set cooling period"""
        stats = cls.get_exit_stats()
        if stats["risk_level"] == "high":
            cls._cooling_until = time.time() + cls.COOLING_HIGH_H * 3600
        elif stats["risk_level"] == "elevated":
            cls._cooling_until = time.time() + cls.COOLING_ELEVATED_H * 3600
        # normal: keep existing cooling if any, do NOT reset

    @classmethod
    def get_risk_multiplier(cls) -> float:
        """Get hurdle multiplier from exit pattern (≥1.0)"""
        stats = cls.get_exit_stats()
        level = stats["risk_level"]
        if level == "high":
            return cls.MULTIPLIER_HIGH
        elif level == "elevated":
            return cls.MULTIPLIER_ELEVATED
        return 1.0

    @classmethod
    def in_cooling(cls) -> bool:
        """Whether currently in cooling period"""
        return time.time() < cls._cooling_until

    @classmethod
    def cooling_remaining_hours(cls) -> float:
        """Remaining cooling time in hours"""
        if not cls.in_cooling():
            return 0.0
        return max(0.0, (cls._cooling_until - time.time()) / 3600)

    # ==============================================
    #  STEP 4: EVALUATE — Original Shadow Logic + Risk Multiplier
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
        """
        Compute shadow hurdle with active-exit risk amplification.
        RETURNS verdict — CALLER MUST IGNORE for live decisions during RFC.
        """
        # ——— Decay Calculation ———
        m_decay = max(math.exp(-elapsed_hours / cls.HALF_LIFE_HOURS), cls.FLOOR_DECAY)
        if elapsed_hours <= cls.STRICT_WINDOW_HOURS:
            m_decay = 1.0  # Strictest within 90min post-exit

        # ——— Streak Multiplier ———
        m_streak = 1.0 + 0.25 * (consecutive_failures - 1)
        m_streak = min(m_streak, 3.0)
        if consecutive_failures <= 0:
            m_streak = 1.0

        # ——— Regime Reset (strong signal clears penalties) ———
        regime_reset_triggered = False
        if alignment == 3 and rank == 1 and gap_delta >= baseline * 1.5:
            m_decay = 1.0
            m_streak = 1.0
            regime_reset_triggered = True

        # ——— Active-Exit Risk Multiplier (NEW) ———
        risk_mult = cls.get_risk_multiplier()

        # ——— Final Hurdle ———
        effective_hurdle = baseline * m_reason * m_streak * m_decay * risk_mult
        verdict = "WOULD_PASS" if gap_delta >= effective_hurdle else "WOULD_REJECT"

        # ——— Return Full Context ———
        stats = cls.get_exit_stats()
        return {
            "verdict": verdict,
            "effective_hurdle": round(effective_hurdle, 6),
            "m_decay": round(m_decay, 6),
            "m_streak": round(m_streak, 6),
            "risk_multiplier": round(risk_mult, 2),
            "risk_level": stats["risk_level"],
            "active_ratio": stats["active_ratio"],
            "in_cooling": cls.in_cooling(),
            "cooling_remaining_hours": round(cls.cooling_remaining_hours(), 2),
            "regime_reset_triggered": regime_reset_triggered,
        }

    # ==============================================
    #  Utilities
    # ==============================================
    @classmethod
    def clear_history(cls) -> None:
        """Reset history & cooling — e.g. new trading day"""
        cls._history.clear()
        cls._cooling_until = 0.0