"""
PostExitGate — SHADOW EVALUATION ONLY.

STRICT GUARDRAIL: This module is READ-ONLY / OBSERVATIONAL.
- It does NOT modify, intercept, or influence any order/entry execution.
- Its evaluate_shadow() method returns a verdict that callers MUST ignore
  for live decisions; results are logged to JSONL for offline analysis only.
"""
from __future__ import annotations

import math
from typing import Any, Dict

import config as _config


def _cfg(name: str, default: Any) -> Any:
    return getattr(_config, name, default)


class PostExitGate:
    """
    Shadow gate that computes what a post-exit penalty WOULD have been,
    without applying it to any live execution.
    """

    # Configurable from config.py; fall back to sensible defaults.
    HALF_LIFE_HOURS: float = _cfg("POST_EXIT_HALF_LIFE_HOURS", 6.0)
    RULES: Dict[str, Dict[str, float]] = _cfg(
        "POST_EXIT_RULES",
        {
            "tier1": {"baseline": 1.00, "m_reason": 1.00},
            "tier2": {"baseline": 1.05, "m_reason": 1.10},
            "tier3": {"baseline": 1.15, "m_reason": 1.25},
        },
    )

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
        Compute a shadow hurdle and verdict.

        Parameters
        ----------
        baseline : float
            Tier baseline (e.g. 1.00, 1.05, 1.15).
        m_reason : float
            Close-reason multiplier (e.g. 1.00, 1.10, 1.25).
        consecutive_failures : int
            Number of consecutive losing exits for this instrument.
        elapsed_hours : float
            Hours since the last exit.
        alignment : int
            Number of aligned timeframes (e.g. 3 for 3/3).
        rank : int
            Strength rank of the candidate (1 = top).
        gap_delta : float
            Raw strength-gap delta of the candidate.

        Returns
        -------
        dict with keys:
            verdict : "WOULD_PASS" | "WOULD_REJECT"
            effective_hurdle : float
            m_decay : float
            m_streak : float
            regime_reset_triggered : bool
        """
        # --- decay & streak ---
        # m_decay = max(math.exp(-elapsed_hours / cls.HALF_LIFE_HOURS), 0.7)
        m_decay = max(math.exp(-elapsed_hours / cls.HALF_LIFE_HOURS), 0.45)

        if elapsed_hours <= 1.0:
            m_decay = 1.0  # 平仓1小时内保持 1.0 不变

        m_streak = 1.0 + 0.25 * (consecutive_failures - 1)
        # if m_streak > 3.0:
        #     m_streak = 3.0
        m_streak = min(m_streak, 3.0)
        if consecutive_failures <= 0:
            m_streak = 1.0

        # --- regime reset ---
        regime_reset_triggered = False
        if alignment == 3 and rank == 1 and gap_delta >= baseline * 1.5:
            m_decay = 1.0
            m_streak = 1.0
            regime_reset_triggered = True

        effective_hurdle = baseline * m_reason * m_streak * m_decay

        # A signal "WOULD_PASS" the shadow gate if its gap_delta clears the hurdle.
        # This is intentionally conservative: the gate only REJECTS when the
        # post-exit penalty is still large enough to suppress the signal.
        verdict = "WOULD_PASS" if gap_delta >= effective_hurdle else "WOULD_REJECT"

        return {
            "verdict": verdict,
            "effective_hurdle": round(effective_hurdle, 6),
            "m_decay": round(m_decay, 6),
            "m_streak": round(m_streak, 6),
            "regime_reset_triggered": regime_reset_triggered,
        }