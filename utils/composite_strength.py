"""
Generic Composite Strength Index — V1.1 (Observation-only / research instrument)
================================================================================

Implements Design V1.1 (docs/spec). Pure function, no shared state, no
trading decisions, no side effects. Never writes to anything the trading
path reads.

Relationship to existing code
-----------------------------
``build_strength_matrix`` in utils/strategy_helpers.py produces the per-
currency strength dict that this module consumes as a READ-ONLY snapshot.
Nothing here re-fetches candles or calls any market-data API.

Metric classification (per §10 of V1.1 spec)
---------------------------------------------
Core V1        : RawIndex
Diagnostics    : DirectionalBalance (weighted DB of contributions),
                 GapScore (unweighted DB of raw gaps), Breadth,
                 WeightedBreadth, Rank, BasketMean, BasketDispersion sigma_W
Research-only  : z, rolling_percentile, corr/target_strength/R2, residual
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, timezone


SCHEMA_VERSION = "1.1.0"

VALID_WEIGHT_SUM_TOL = 1e-9
DEFAULT_BREADTH_EPS = 1e-6
DEFAULT_RESEARCH_EPS = 1e-9


@dataclass
class CompositeContribution:
    currency: str
    target_strength: float
    basket_strength: float
    weight: float
    gap: float
    contribution: float


@dataclass
class CompositeConfig:
    """Validated, immutable basket configuration.

    ``basket`` is the ordered list of basket currencies (excluding target).
    ``weights`` maps each basket currency to a positive float summing to 1.
    ``hash_`` is a deterministic fingerprint of ``weights`` for logging.
    """
    basket: Tuple[str, ...]
    weights: Dict[str, float]
    target_code: str
    hash_: str
    normalize_weights_applied: bool = False

    @classmethod
    def build(
        cls,
        basket_weights: Dict[str, float],
        target_code: str = "JPY",
        valid_universe: Optional[List[str]] = None,
        *,
        normalize_weights: bool = False,
    ) -> "CompositeConfig":
        """Validate and construct a :class:`CompositeConfig`.

        Raises :class:`ValueError` for any invalid input. Never silently
        normalizes by default — pass ``normalize_weights=True`` to enable
        explicit, logged normalization. (§11 of V1.1 spec.)
        """
        if not isinstance(basket_weights, dict) or not basket_weights:
            raise ValueError("basket_weights must be a non-empty dict")

        if target_code in basket_weights:
            raise ValueError(f"{target_code} must not appear in its own basket")

        seen: Dict[str, float] = {}
        total = 0.0
        for ccy, w in basket_weights.items():
            if not isinstance(ccy, str) or not ccy:
                raise ValueError(f"invalid currency code: {ccy!r}")
            if ccy in seen:
                raise ValueError(f"duplicate currency in basket: {ccy}")
            try:
                wf = float(w)
            except (TypeError, ValueError):
                raise ValueError(f"weight for {ccy} is not numeric: {w!r}")
            if not math.isfinite(wf):
                raise ValueError(f"weight for {ccy} is not finite: {w}")
            if wf <= 0:
                raise ValueError(f"weight for {ccy} must be > 0, got {wf}")
            seen[ccy] = wf
            total += wf

        norm_applied = False
        if normalize_weights:
            if total <= 0:
                raise ValueError(f"cannot normalize: weights sum to {total}")
            seen = {k: v / total for k, v in seen.items()}
            total = 1.0
            norm_applied = True

        if abs(total - 1.0) >= VALID_WEIGHT_SUM_TOL:
            raise ValueError(
                f"weights sum to {total:.12f}, must sum to 1.0 (±{VALID_WEIGHT_SUM_TOL}). "
                f"Pass normalize_weights=True to auto-normalize."
            )

        if valid_universe is not None:
            universe = set(valid_universe)
            missing = [c for c in seen if c not in universe]
            if target_code not in universe:
                missing.append(target_code)
            if missing:
                raise ValueError(f"unknown currencies not in valid_universe: {missing}")

        ordered = tuple(sorted(seen.keys()))
        weights = {c: seen[c] for c in ordered}
        payload = f"{target_code}|" + ",".join(f"{c}={weights[c]:.12f}" for c in ordered)
        hash_ = hashlib.sha256(payload.encode("ascii")).hexdigest()[:12]

        return cls(basket=ordered, weights=weights, target_code=target_code,
                   hash_=hash_, normalize_weights_applied=norm_applied)


@dataclass
class CompositeResult:
    target: str
    schema_version: str
    snapshot_timestamp_utc: str
    weight_hash: str
    basket: Tuple[str, ...]
    normalize_weights_applied: bool

    target_strength: float
    raw_index: float
    basket_mean: float
    basket_dispersion_sigma_w: float
    contribution_magnitude: float
    directional_balance: Optional[float]
    gap_magnitude: float
    gap_score: float
    breadth_positive: int
    breadth_total: int
    breadth_fraction: float
    weighted_breadth: float
    rank: int
    rank_total: int

    contribution_dispersion: float
    z_research: Optional[float]
    rolling_percentile_research: Optional[float] = None

    contributions: List[CompositeContribution] = field(default_factory=list)
    warnings: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.as_dict(), indent=indent, default=str)


# ---------------------------------------------------------------------------
# Core pure computation
# ---------------------------------------------------------------------------

def compute_composite(
    strength_scores: Dict[str, float],
    config: CompositeConfig,
    *,
    snapshot_timestamp: Optional[datetime] = None,
    history_raw_index: Optional[List[float]] = None,
    breadth_eps: float = DEFAULT_BREADTH_EPS,
    research_eps: float = DEFAULT_RESEARCH_EPS,
) -> Optional[CompositeResult]:
    """Compute a single composite observation for ``config.target_code``.

    Parameters
    ----------
    strength_scores:
        Full per-currency strength dict. **Must contain target_code and every
        basket currency.** If any required currency is missing / NaN /
        non-finite the observation is invalidated and ``None`` is returned
        (§12 strict invalidation — no partial-basket renormalization).
    config:
        Validated :class:`CompositeConfig`.
    snapshot_timestamp:
        Defaults to ``datetime.now(UTC)``.
    history_raw_index:
        Optional list of prior ``RawIndex`` values, oldest first (§6).

    Returns
    -------
    :class:`CompositeResult` or ``None`` if input validation fails.
    """
    ts = snapshot_timestamp or datetime.now(timezone.utc)

    if not _validate_strength_input(strength_scores, config):
        return None

    S_target = float(strength_scores[config.target_code])
    if not math.isfinite(S_target):
        return None

    S_i: Dict[str, float] = {c: float(strength_scores[c]) for c in config.basket}
    if not all(math.isfinite(v) for v in S_i.values()):
        return None

    W = config.weights
    D = {c: S_target - S_i[c] for c in config.basket}
    C = {c: W[c] * D[c] for c in config.basket}

    RawIndex = sum(C.values())
    B = sum(W[c] * S_i[c] for c in config.basket)

    sigma_w_sq = sum(W[c] * (S_i[c] - B) ** 2 for c in config.basket)
    sigma_w = math.sqrt(max(sigma_w_sq, 0.0))

    contribution_magnitude = sum(W[c] * abs(D[c]) for c in config.basket)
    gap_magnitude = sum(abs(D[c]) for c in config.basket)
    gap_net = sum(D[c] for c in config.basket)

    if contribution_magnitude > research_eps:
        DirectionalBalance = 50.0 + 50.0 * (RawIndex / contribution_magnitude)
    else:
        DirectionalBalance = None

    if gap_magnitude > research_eps:
        GapScore = 50.0 + 50.0 * (gap_net / gap_magnitude)
    else:
        GapScore = 50.0

    breadth_positive = sum(1 for c in config.basket if D[c] > breadth_eps)
    breadth_total = len(config.basket)
    breadth_fraction = breadth_positive / breadth_total if breadth_total > 0 else 0.0
    weighted_breadth = sum(W[c] for c in config.basket if D[c] > breadth_eps)

    ranked = sorted(strength_scores.items(), key=lambda kv: kv[1], reverse=True)
    rank_total = len(ranked)
    rank = next((i + 1 for i, (c, _) in enumerate(ranked) if c == config.target_code), 0)

    contributions = [
        CompositeContribution(
            currency=c,
            target_strength=S_target,
            basket_strength=S_i[c],
            weight=W[c],
            gap=D[c],
            contribution=C[c],
        )
        for c in config.basket
    ]

    if contributions:
        mean_c = sum(cc.contribution for cc in contributions) / len(contributions)
        var_c = sum((cc.contribution - mean_c) ** 2 for cc in contributions) / len(contributions)
        contribution_dispersion = math.sqrt(max(var_c, 0.0))
    else:
        contribution_dispersion = 0.0

    if sigma_w > research_eps:
        z_research = RawIndex / sigma_w
    else:
        z_research = None

    rolling_percentile = _rolling_percentile(history_raw_index, RawIndex)

    return CompositeResult(
        target=config.target_code,
        schema_version=SCHEMA_VERSION,
        snapshot_timestamp_utc=ts.isoformat(),
        weight_hash=config.hash_,
        basket=config.basket,
        normalize_weights_applied=config.normalize_weights_applied,
        target_strength=float(S_target),
        raw_index=float(RawIndex),
        basket_mean=float(B),
        basket_dispersion_sigma_w=float(sigma_w),
        contribution_magnitude=float(contribution_magnitude),
        directional_balance=float(DirectionalBalance) if DirectionalBalance is not None else None,
        gap_magnitude=float(gap_magnitude),
        gap_score=float(GapScore),
        breadth_positive=int(breadth_positive),
        breadth_total=int(breadth_total),
        breadth_fraction=float(breadth_fraction),
        weighted_breadth=float(weighted_breadth),
        rank=int(rank),
        rank_total=int(rank_total),
        contribution_dispersion=float(contribution_dispersion),
        z_research=float(z_research) if z_research is not None else None,
        rolling_percentile_research=float(rolling_percentile) if rolling_percentile is not None else None,
        contributions=contributions,
    )


def compute_all_composites(
    strength_scores: Dict[str, float],
    basket_weights: Dict[str, float],
    *,
    snapshot_timestamp: Optional[datetime] = None,
    breadth_eps: float = DEFAULT_BREADTH_EPS,
    research_eps: float = DEFAULT_RESEARCH_EPS,
) -> Dict[str, Optional[CompositeResult]]:
    """Compute Composite Index for every currency in ``strength_scores``,
    all using the same basket weights (the basket is the other currencies).
    Returns ``{target_code: CompositeResult | None}``.

    Note: the caller must provide weights that sum to 1 for the basket of
    each target. If you want equal-weight relative baskets, just pass
    equal weights for every currency except the target — but this function
    does NOT auto-derive per-target baskets; it uses ``basket_weights``
    directly as-is (minus the target row for each target).
    """
    results: Dict[str, Optional[CompositeResult]] = {}
    for target in strength_scores:
        target_basket = {c: w for c, w in basket_weights.items() if c != target}
        if target not in basket_weights and target_basket:
            try:
                cfg = CompositeConfig.build(target_basket, target_code=target)
            except ValueError:
                results[target] = None
                continue
            results[target] = compute_composite(
                strength_scores, cfg,
                snapshot_timestamp=snapshot_timestamp,
                breadth_eps=breadth_eps,
                research_eps=research_eps,
            )
        else:
            results[target] = None
    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_strength_input(
    strength_scores: Dict[str, float], config: CompositeConfig
) -> bool:
    if not isinstance(strength_scores, dict) or not strength_scores:
        return False
    if config.target_code not in strength_scores:
        return False
    for c in config.basket:
        if c not in strength_scores:
            return False
    return True


def _rolling_percentile(
    history: Optional[List[float]], current: float
) -> Optional[float]:
    if history is None or len(history) < 5:
        return None
    values = [float(v) for v in history if isinstance(v, (int, float)) and math.isfinite(v)]
    if len(values) < 5:
        return None
    below = sum(1 for v in values if v <= current)
    return 100.0 * below / len(values)


# ---------------------------------------------------------------------------
# Offline redundancy analysis (§7 / §8)
# ---------------------------------------------------------------------------

def analyze_redundancy(
    raw_indexes: List[float], target_values: List[float]
) -> Dict[str, float]:
    import statistics
    n = len(raw_indexes)
    if n != len(target_values) or n < 2:
        return {"corr": float("nan"), "r_squared": float("nan"),
                "a": float("nan"), "b": float("nan")}
    m_r = statistics.mean(raw_indexes)
    m_s = statistics.mean(target_values)
    v_r = statistics.variance(raw_indexes)
    v_s = statistics.variance(target_values)
    if v_r <= 0 or v_s <= 0:
        return {"corr": float("nan"), "r_squared": float("nan"),
                "a": float("nan"), "b": float("nan")}
    cov = sum((r - m_r) * (s - m_s) for r, s in zip(raw_indexes, target_values)) / (n - 1)
    corr = cov / (math.sqrt(v_r) * math.sqrt(v_s))
    b = cov / v_s
    a = m_r - b * m_s
    ss_res = sum((r - (a + b * s)) ** 2 for r, s in zip(raw_indexes, target_values))
    ss_tot = sum((r - m_r) ** 2 for r in raw_indexes)
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return {"corr": corr, "r_squared": r_squared, "a": a, "b": b}


def compute_residual(
    raw_index: float, target_strength: float, a: float, b: float
) -> float:
    return raw_index - (a + b * target_strength)