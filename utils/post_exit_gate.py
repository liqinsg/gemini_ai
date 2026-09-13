"""
PostExitGate — Adaptive Threshold Engine
==========================================
BALANCED / NEUTRAL mode — designed for demo/live participation.

Multipliers (BALANCED starting values, testable hypotheses):
  tier1=1.00  tier2=1.10  tier3=1.20
  rank1-2=1.00 rank3=1.03 rank4=1.07
  STRONG_MOMENTUM=0.97  NEUTRAL=1.00  CONSOLIDATION=1.05
  tier1 size=1.00  tier2=0.85  tier3=0.70

Architecture:
  Signal Generation
    ↓
  MC Regime (LIVE participation)
    ↓
  PostExitGate  ← this module
    ↓
  Existing Risk Manager
    ↓
  Execution

The gate NEVER overrides existing hard risk-manager controls.
Every evaluation logs to JSONL with both BASELINE and LIVE decisions.
"""
from __future__ import annotations

import math
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Literal, Optional, Tuple
from collections import deque

import config as _config
from config import SIGNAL_TIMEFRAMES, MIN_MARKET_STRENGTH, REQUIRE_ALIGNED


# ============================================================
# 🎯 Close Reason → Tier Mapping
# ============================================================
CLOSE_REASON_TIER_MAP: Dict[str, str] = {
    "TP": "tier1",
    "PROFIT_TAKE": "tier1",
    "HWM_LOCK": "tier1",
    "PROFIT_LOCK": "tier1",
    "TECHNICAL_INVALIDATION": "tier2",
    "STRATEGY_INVALIDATION": "tier3",
    "MULTI_FACTOR_INVALIDATION": "tier3",
    "SWEEP": "tier3",
    "STOP_LOSS": "tier3",
}

# ============================================================
# 🎯 Multiplier Matrices — BALANCED Starting Values
# ============================================================
POST_EXIT_TIER_MULTIPLIER: Dict[str, float] = dict(
    getattr(
        _config,
        "POST_EXIT_TIER_MULTIPLIER",
        {"tier1": 1.00, "tier2": 1.10, "tier3": 1.20},
    )
)

RANK_MULTIPLIER: Dict[int, float] = dict(
    getattr(_config, "POST_EXIT_RANK_MULTIPLIER", {1: 1.00, 2: 1.00, 3: 1.03, 4: 1.07})
)

MC_REGIME_MULTIPLIER: Dict[str, float] = dict(
    getattr(
        _config,
        "POST_EXIT_MC_REGIME_MULTIPLIER",
        {"STRONG_MOMENTUM": 0.97, "NEUTRAL": 1.00, "CONSOLIDATION": 1.05},
    )
)

POST_EXIT_SIZE_MULTIPLIER: Dict[str, float] = dict(
    getattr(
        _config,
        "POST_EXIT_SIZE_MULTIPLIER",
        {"tier1": 1.00, "tier2": 0.85, "tier3": 0.70},
    )
)

POST_EXIT_STRICT_WINDOW_HOURS: float = float(
    getattr(_config, "POST_EXIT_STRICT_WINDOW_HOURS", 24.0)
)
ALIGNMENT_THRESHOLD_CONFIG: int = int(
    getattr(_config, "ALIGNMENT_THRESHOLD", REQUIRE_ALIGNED)
)

# ============================================================
# 🎯 JSONL Log Path
# ============================================================
POST_EXIT_GATE_LOG_PATH = os.environ.get(
    "POST_EXIT_GATE_LOG_PATH", "logs/post_exit_gate_live.jsonl"
)


# ============================================================
#  Backward-compatible Exit History (old shadow engine tracking)
# ============================================================
class PostExitHistory:
    """Old exit history tracker — preserved for backward compatibility."""

    MAX_HISTORY: int = int(getattr(_config, "POST_EXIT_MAX_HISTORY", 4))

    _history: Deque[Literal["SL", "TP", "ACTIVE"]] = deque(maxlen=MAX_HISTORY)

    @classmethod
    def record_exit(cls, reason: Literal["SL", "TP", "ACTIVE"]) -> None:
        cls._history.append(reason)

    @classmethod
    def clear_history(cls) -> None:
        cls._history.clear()


# ============================================================
#  Close Reason → Tier Mapping helper
# ============================================================
def map_close_reason_to_tier(close_reason: Optional[str]) -> str:
    """Map a raw close_reason string (from trade outcome log) → tier string.

    Unknown reasons → "unknown" (no escalation, logged separately).
    """
    if not close_reason:
        return "unknown"
    reason_key = close_reason.upper().split(":", 1)[0].strip()
    return CLOSE_REASON_TIER_MAP.get(reason_key, "unknown")


# ============================================================
#  MC Regime → Key Normalization helper
# ============================================================
def normalize_mc_regime_key(mc_regime: Optional[str]) -> str:
    """Normalize an MC regime string (with emoji/prefix) → one of
    'STRONG_MOMENTUM', 'CONSOLIDATION', 'NEUTRAL' (default).
    """
    if not mc_regime:
        return "NEUTRAL"
    reg = mc_regime.upper()
    if "CONSOLIDATION" in reg:
        return "CONSOLIDATION"
    if "STRONG" in reg and "MOMENTUM" in reg:
        return "STRONG_MOMENTUM"
    return "NEUTRAL"


# ============================================================
#  Actual Alignment Count helper
# ============================================================
def compute_actual_aligned_tf(
    instrument: str, direction: str
) -> int:
    """Count how many of SIGNAL_TIMEFRAMES agree with the candidate direction.
    Returns int (typically 3). Falls back to REQUIRE_ALIGNED on any error.
    """
    try:
        from utils.strategy_helpers import get_candles, _ema

        buy_count = 0
        sell_count = 0
        for tf in SIGNAL_TIMEFRAMES:
            candles = get_candles(instrument, tf, count=10)
            if len(candles) < 6:
                continue
            prices = [float(c["mid"]["c"]) for c in candles]
            ma5 = _ema(prices, period=5)
            current = prices[-1]
            if current > ma5:
                buy_count += 1
            else:
                sell_count += 1

        if direction.upper() == "BUY":
            return buy_count
        return sell_count
    except Exception as e:
        print(f"  [POST_EXIT] alignment count fallback ({instrument}): {e}")
        return REQUIRE_ALIGNED


# ============================================================
#  Basket Correlation — Observation Only
# ============================================================
def get_basket_snapshot(tracker=None) -> Dict[str, Any]:
    """Scan all TRADE_PAIRS for PostExit strict-window activity.
    Returns {active_count, pairs_in_window, tiers, ...}.
    DECISION IMPACT: NONE — observation only.
    """
    if tracker is None:
        return {"active_count": 0, "pairs": [], "tiers": {}}
    pairs_in_window: List[str] = []
    tier_map: Dict[str, str] = {}

    for pair in getattr(_config, "TRADE_PAIRS", []):
        ctx = tracker.get_context(pair)
        closed_at = ctx.get("closed_at")
        elapsed = ctx.get("elapsed_hours", 0.0)
        if closed_at is None or elapsed == 0.0:
            continue
        if elapsed <= POST_EXIT_STRICT_WINDOW_HOURS:
            tier = map_close_reason_to_tier(ctx.get("close_reason"))
            pairs_in_window.append(pair)
            tier_map[pair] = tier

    return {
        "active_count": len(pairs_in_window),
        "pairs": pairs_in_window,
        "tiers": tier_map,
    }


# ============================================================
#  Main Gate — Adaptive Threshold Engine
# ============================================================
@dataclass
class GateDecision:
    """Complete gate decision with both baseline and LIVE paths."""

    # Identification
    timestamp_utc: str
    instrument: str
    action: str

    # Post-exit state
    close_reason: Optional[str]
    tier: str
    closed_at_utc: Optional[str]
    window_active: bool
    elapsed_hours: float

    # Candidate metadata
    candidate_rank: int
    actual_aligned_tf: int
    actual_strength_score: float
    actual_gap: float

    # Multipliers
    tier_multiplier: float
    rank_multiplier: float
    regime_multiplier: float
    effective_multiplier_live: float
    effective_multiplier_baseline_no_mc: float

    # MC regime
    mc_regime_raw: str
    mc_regime_key: str

    # Baseline thresholds
    baseline_strength_threshold: float
    effective_strength_threshold: float
    baseline_strength_threshold_no_mc: float
    baseline_gap_threshold: float
    effective_gap_threshold: float
    baseline_gap_threshold_no_mc: float
    baseline_alignment: int
    effective_alignment: int
    baseline_alignment_no_mc: int

    # Pass/fail per check
    strength_pass: bool
    gap_pass: bool
    alignment_pass: bool

    # Final decisions
    baseline_decision: str  # ALLOW or REJECTED
    post_exit_decision: str  # ALLOW or REJECTED with reason code
    reason_code: str
    is_allowed: bool

    # Position sizing
    baseline_units: int
    size_multiplier: float
    effective_units: int

    # Basket observation
    basket_snapshot: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp_utc": self.timestamp_utc,
            "instrument": self.instrument,
            "action": self.action,
            "close_reason": self.close_reason,
            "unknown_close_reason": bool(self.close_reason and self.tier == "unknown"),
            "tier": self.tier,
            "closed_at_utc": self.closed_at_utc,
            "window_active": self.window_active,
            "candidate_rank": self.candidate_rank,
            "actual_aligned_tf": self.actual_aligned_tf,
            "actual_strength_score": round(self.actual_strength_score, 6),
            "actual_gap": round(self.actual_gap, 6),
            "tier_multiplier": self.tier_multiplier,
            "rank_multiplier": self.rank_multiplier,
            "regime_multiplier": self.regime_multiplier,
            "effective_multiplier_live": round(self.effective_multiplier_live, 4),
            "effective_multiplier_BASELINE": round(
                self.effective_multiplier_baseline_no_mc, 4
            ),
            "mc_regime": self.mc_regime_raw,
            "baseline_strength_threshold": round(
                self.baseline_strength_threshold, 6
            ),
            "effective_strength_threshold": round(
                self.effective_strength_threshold, 6
            ),
            "baseline_strength_threshold_no_mc": round(
                self.baseline_strength_threshold_no_mc, 6
            ),
            "baseline_gap_threshold": round(self.baseline_gap_threshold, 6),
            "effective_gap_threshold": round(self.effective_gap_threshold, 6),
            "baseline_gap_threshold_no_mc": round(
                self.baseline_gap_threshold_no_mc, 6
            ),
            "baseline_alignment": self.baseline_alignment,
            "effective_alignment": self.effective_alignment,
            "baseline_alignment_no_mc": self.baseline_alignment_no_mc,
            "strength_pass": self.strength_pass,
            "gap_pass": self.gap_pass,
            "alignment_pass": self.alignment_pass,
            "baseline_decision": self.baseline_decision,
            "post_exit_decision": self.post_exit_decision,
            "reason_code": self.reason_code,
            "baseline_units": self.baseline_units,
            "size_multiplier": self.size_multiplier,
            "effective_units": self.effective_units,
            "basket_snapshot": self.basket_snapshot,
        }


class PostExitGate:
    """Adaptive Threshold Engine — LIVE decision layer (BALANCED mode).

    Architecture:
      Signal Generation → MC Regime → PostExitGate → Risk Manager → Execution

    NEVER overrides existing hard risk-manager controls. Logs EVERY evaluation.
    Falls back gracefully on missing data / exceptions.
    """

    ENABLED: bool = bool(getattr(_config, "POST_EXIT_GATE_ENABLED", True))
    SHADOW_MODE: bool = bool(getattr(_config, "POST_EXIT_GATE_SHADOW", False))

    # Baseline thresholds (read from config)
    BASELINE_STRENGTH_THRESHOLD: float = MIN_MARKET_STRENGTH
    BASELINE_GAP_THRESHOLD: float = MIN_MARKET_STRENGTH
    BASELINE_ALIGNMENT: int = ALIGNMENT_THRESHOLD_CONFIG

    @classmethod
    def record_exit(cls, reason: Literal["SL", "TP", "ACTIVE"]) -> None:
        """Record an exit for callers using the legacy class API."""
        PostExitHistory.record_exit(reason)

    @classmethod
    def _log_jsonl(cls, decision: GateDecision) -> None:
        try:
            path = Path(POST_EXIT_GATE_LOG_PATH)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(decision.to_dict(), default=str) + "\n")
        except Exception as e:
            print(f"  [POST_EXIT] JSONL log write failed (non-fatal): {e}")

    @classmethod
    def _print_console(cls, decision: GateDecision) -> None:
        d = decision
        print(
            f"\n  [POST-EXIT] PAIR: {d.instrument} | Tier: {d.tier} | Rank: {d.candidate_rank} | "
            f"Align: {d.actual_aligned_tf}/{len(SIGNAL_TIMEFRAMES)}"
        )
        print(
            f"    MC regime: {d.mc_regime_key} (applied: ×{d.regime_multiplier:.2f})"
        )
        print(
            f"    Multipliers: tier×{d.tier_multiplier:.2f} × rank×{d.rank_multiplier:.2f} "
            f"× MC×{d.regime_multiplier:.2f} = LIVE ×{d.effective_multiplier_live:.2f} | "
            f"BASELINE w/o MC: ×{d.effective_multiplier_baseline_no_mc:.2f}"
        )

        strength_str = "PASS" if d.strength_pass else "FAIL"
        gap_str = "PASS" if d.gap_pass else "FAIL"
        align_str = "PASS" if d.alignment_pass else "FAIL"
        print(
            f"    Strength: {abs(d.actual_strength_score):.4f} / "
            f"{d.effective_strength_threshold:.4f} [{strength_str}]"
        )
        print(
            f"    Gap: {d.actual_gap:.4f} / {d.effective_gap_threshold:.4f} [{gap_str}]"
        )
        print(
            f"    Alignment: {d.actual_aligned_tf} / {d.effective_alignment} [{align_str}]"
        )

        bsk = d.basket_snapshot
        print(
            f"    Basket: {bsk['active_count']}/{len(getattr(_config, 'TRADE_PAIRS', []))} "
            f"pairs in strict window {bsk['pairs']}"
        )
        print(
            f"    Decision: {d.reason_code} | Size: {d.baseline_units} → "
            f"{d.effective_units} units"
        )

    @classmethod
    def evaluate(
        cls,
        instrument: str,
        action: str,
        strength_score: float,
        candidate_rank: int = 1,
        mc_regime_raw: Optional[str] = None,
        baseline_units: int = 1000,
        actual_aligned_tf: Optional[int] = None,
        tracker=None,
    ) -> GateDecision:
        """Run the complete Adaptive Threshold Engine evaluation.

        Parameters
        ----------
        instrument : str
            Pair name, e.g. "USD_JPY".
        action : str
            "BUY" or "SELL".
        strength_score : float
            Raw strength score from the strategy.
        candidate_rank : int
            1 = top strength, 2 = second, 3 = third, 4 = fourth.
        mc_regime_raw : str or None
            Raw MC regime string (may contain emoji/prefix).
        baseline_units : int
            Default position size from RISK_PROFILE.
        actual_aligned_tf : int or None
            Count of agreeing H4/H1/M30 timeframes. None → computed live.

        Returns
        -------
        GateDecision
            Complete decision with is_allowed=True/False and reason_code.
        """
        timestamp = datetime.now(timezone.utc).isoformat()

        # Post-exit context is optional and must be supplied by an OANDA-backed
        # caller. The default runner is stateless and therefore uses neutral context.
        close_reason: Optional[str] = None
        closed_at_utc: Optional[str] = None
        elapsed_hours: float = 0.0
        if tracker is not None:
            try:
                ctx = tracker.get_context(instrument)
                close_reason = ctx.get("close_reason")
                closed_at_utc = ctx.get("closed_at")
                elapsed_hours = ctx.get("elapsed_hours", 0.0)
            except Exception as e:
                print(f"  [POST_EXIT] Tracker failed for {instrument}: {e} — fallback to neutral")

        window_active = (
            closed_at_utc is not None
            and elapsed_hours >= 0.0
            and elapsed_hours <= POST_EXIT_STRICT_WINDOW_HOURS
        )

        tier = map_close_reason_to_tier(close_reason)
        if tier == "unknown" and close_reason:
            print(
                f"  [POST_EXIT] WARNING: unknown close_reason='{close_reason}' "
                f"→ tier='unknown', no escalation applied"
            )

        # --- Rank clamping to valid range ---
        rank_clamped = max(1, min(candidate_rank, 4))

        # --- MC regime normalization ---
        mc_regime_key = normalize_mc_regime_key(mc_regime_raw)

        # --- Multipliers ---
        active_tier = tier if window_active else "unknown"
        tier_mult = POST_EXIT_TIER_MULTIPLIER.get(active_tier, 1.00)
        rank_mult = RANK_MULTIPLIER.get(rank_clamped, 1.00)
        regime_mult = MC_REGIME_MULTIPLIER.get(mc_regime_key, 1.00)

        # LIVE effective multiplier = tier × rank × MC
        effective_multiplier_live = tier_mult * rank_mult * regime_mult
        # Counterfactual BASELINE multiplier (no MC) = tier × rank
        effective_multiplier_baseline_no_mc = tier_mult * rank_mult

        # --- Thresholds ---
        baseline_strength = cls.BASELINE_STRENGTH_THRESHOLD
        effective_strength_threshold = baseline_strength * effective_multiplier_live

        baseline_gap = cls.BASELINE_GAP_THRESHOLD
        effective_gap_threshold = baseline_gap * effective_multiplier_live
        baseline_strength_threshold_no_mc = (
            baseline_strength * effective_multiplier_baseline_no_mc
        )
        baseline_gap_threshold_no_mc = (
            baseline_gap * effective_multiplier_baseline_no_mc
        )

        baseline_alignment = cls.BASELINE_ALIGNMENT
        baseline_alignment_no_mc = min(
            math.ceil(baseline_alignment * effective_multiplier_baseline_no_mc),
            3,
        )
        effective_alignment = math.ceil(
            baseline_alignment * effective_multiplier_live
        )
        effective_alignment = min(effective_alignment, baseline_alignment)

        # --- Actual values ---
        actual_strength_score = strength_score
        actual_gap = abs(strength_score)
        if actual_aligned_tf is None:
            actual_aligned_tf = compute_actual_aligned_tf(instrument, action)

        # --- Baseline and live three-check decisions ---
        baseline_pass = (
            abs(actual_strength_score) >= baseline_strength_threshold_no_mc
            and actual_gap >= baseline_gap_threshold_no_mc
            and actual_aligned_tf >= baseline_alignment_no_mc
        )
        strength_pass = abs(actual_strength_score) >= effective_strength_threshold
        gap_pass = actual_gap >= effective_gap_threshold
        alignment_pass = actual_aligned_tf >= effective_alignment

        # --- Rank check (capped — we only have 4 rank bands) ---
        rank_pass = rank_clamped <= 4

        # --- Reason code: first failure determines rejection ---
        if not strength_pass:
            reason_code = "REJECTED_POST_EXIT_STRENGTH"
        elif not gap_pass:
            reason_code = "REJECTED_POST_EXIT_GAP"
        elif not alignment_pass:
            reason_code = "REJECTED_POST_EXIT_ALIGNMENT"
        elif not rank_pass:
            reason_code = "REJECTED_POST_EXIT_RANK"
        elif baseline_pass and effective_multiplier_live == 1.0:
            reason_code = "ALLOW_BASELINE"
        else:
            reason_code = "ALLOW_POST_EXIT_STRICT"

        post_exit_decision = "ALLOW" if reason_code.startswith("ALLOW") else "REJECTED"
        baseline_decision = "ALLOW" if baseline_pass else "REJECTED"

        # --- Size multiplier ---
        size_mult = POST_EXIT_SIZE_MULTIPLIER.get(active_tier, 1.00)
        effective_units = round(baseline_units * size_mult)

        # --- Basket observation ---
        try:
            basket = get_basket_snapshot(tracker=tracker)
        except Exception:
            basket = {"active_count": 0, "pairs": [], "tiers": {}}

        is_allowed = post_exit_decision == "ALLOW"

        decision = GateDecision(
            timestamp_utc=timestamp,
            instrument=instrument,
            action=action,
            close_reason=close_reason,
            tier=tier,
            closed_at_utc=closed_at_utc,
            window_active=window_active,
            elapsed_hours=elapsed_hours,
            candidate_rank=rank_clamped,
            actual_aligned_tf=actual_aligned_tf,
            actual_strength_score=actual_strength_score,
            actual_gap=actual_gap,
            tier_multiplier=tier_mult,
            rank_multiplier=rank_mult,
            regime_multiplier=regime_mult,
            effective_multiplier_live=effective_multiplier_live,
            effective_multiplier_baseline_no_mc=effective_multiplier_baseline_no_mc,
            mc_regime_raw=mc_regime_raw or "NO_DATA",
            mc_regime_key=mc_regime_key,
            baseline_strength_threshold=baseline_strength,
            effective_strength_threshold=effective_strength_threshold,
            baseline_strength_threshold_no_mc=baseline_strength_threshold_no_mc,
            baseline_gap_threshold=baseline_gap,
            effective_gap_threshold=effective_gap_threshold,
            baseline_gap_threshold_no_mc=baseline_gap_threshold_no_mc,
            baseline_alignment=baseline_alignment,
            effective_alignment=effective_alignment,
            baseline_alignment_no_mc=baseline_alignment_no_mc,
            strength_pass=strength_pass,
            gap_pass=gap_pass,
            alignment_pass=alignment_pass,
            baseline_decision=baseline_decision,
            post_exit_decision=post_exit_decision,
            reason_code=reason_code,
            is_allowed=is_allowed,
            baseline_units=baseline_units,
            size_multiplier=size_mult,
            effective_units=effective_units,
            basket_snapshot=basket,
        )

        cls._log_jsonl(decision)
        cls._print_console(decision)

        return decision


# ============================================================
#  Backward-compatible facade (old API preserved)
# ============================================================
def record_exit(reason: Literal["SL", "TP", "ACTIVE"]) -> None:
    """Backward-compatible thin wrapper around PostExitHistory."""
    PostExitHistory.record_exit(reason)