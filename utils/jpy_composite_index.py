"""
JPY Composite Strength Index — V1.1 (Observation-only / research instrument)
===========================================================================

Implements Design V1.1 (docs/spec). Pure function, no shared state, no
trading decisions, no side effects. Never writes to anything the trading
path reads. Gated by its own master switch at call sites.

Relationship to existing code
-----------------------------
``build_strength_matrix`` in utils/strategy_helpers.py produces the per-
currency strength dict that this module consumes as a READ-ONLY snapshot.
Nothing here re-fetches candles or calls any market-data API.

Metric classification (per §10)
--------------------------------
Core V1        : RawIndex (``RawIndex``)
Diagnostics    : DirectionalBalance, Breadth, WeightedBreadth, Rank, BasketMean, sigma_W
Research-only  : z, rolling_percentile, corr/S_JPY/R2, residual
"""
from __future__ import annotations

import copy
import hashlib
import math
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, timezone


SCHEMA_VERSION = "1.1.0"

VALID_WEIGHT_SUM_TOL = 1e-9
DEFAULT_BREADTH_EPS = 1e-6
DEFAULT_RESEARCH_EPS = 1e-9
DEFAULT_RANK_EPS = 1e-9


@dataclass
class BasketConfig:
    """Validated, immutable basket configuration.

    ``currencies`` is the ordered list of basket currencies (excluding JPY).
    ``weights`` maps each currency to a positive float summing to 1.
    ``hash_`` is a deterministic fingerprint of ``weights`` for logging.
    """
    currencies: Tuple[str, ...]
    weights: Dict[str, float]
    jpy_code: str = "JPY"
    hash_: str = ""

    @classmethod
    def build(
        cls,
        basket_weights: Dict[str, float],
        jpy_code: str = "JPY",
        valid_universe: Optional[List[str]] = None,
    ) -> "BasketConfig":
        """Validate and construct a :class:`BasketConfig`.

        Raises :class:`ValueError` for any invalid input — never silently
        normalizes or drops currencies (§11).
        """
        if not isinstance(basket_weights, dict) or not basket_weights:
            raise ValueError("basket_weights must be a non-empty dict")

        if jpy_code in basket_weights:
            raise ValueError(f"{jpy_code} must not appear in its own basket")

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

        if abs(total - 1.0) >= VALID_WEIGHT_SUM_TOL:
            raise ValueError(
                f"weights sum to {total:.12f}, must sum to 1.0 (±{VALID_WEIGHT_SUM_TOL})"
            )

        if valid_universe is not None:
            universe = set(valid_universe)
            missing = [c for c in seen if c not in universe]
            if jpy_code not in universe:
                missing.append(jpy_code)
            if missing:
                raise ValueError(
                    f"unknown currencies not in valid_universe: {missing}"
                )

        ordered = tuple(sorted(seen.keys()))
        weights = {c: seen[c] for c in ordered}
        payload = f"{jpy_code}|" + ",".join(f"{c}={weights[c]:.12f}" for c in ordered)
        hash_ = hashlib.sha256(payload.encode("ascii")).hexdigest()[:12]

        return cls(currencies=ordered, weights=weights, jpy_code=jpy_code, hash_=hash_)


@dataclass
class JpyCompositeObservation:
    """Single observation record returned by :func:`compute_jpy_composite`.

    ``None``-valued fields are research-only metrics that degrade cleanly
    (zero denominator, stale data, etc.) — they are NOT hard errors.
    """
    schema_version: str
    snapshot_timestamp_utc: str
    weight_hash: str
    currencies_basket: Tuple[str, ...]
    jpy_code: str

    raw_index: float
    basket_mean: float
    dispersion_sigma_w: float
    magnitude: float
    directional_balance: Optional[float]
    breadth_fraction: float
    weighted_breadth: float
    rank: int
    rank_total: int
    jpy_strength: float

    z_research: Optional[float]
    rolling_percentile_research: Optional[float] = None
    corr_with_sjpy_research: Optional[float] = None
    r_squared_research: Optional[float] = None
    residual_research: Optional[float] = None
    regression_a_research: Optional[float] = None
    regression_b_research: Optional[float] = None

    warnings: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Core pure computation
# ---------------------------------------------------------------------------

def compute_jpy_composite(
    strength_scores: Dict[str, float],
    basket: BasketConfig,
    *,
    snapshot_timestamp: Optional[datetime] = None,
    history_raw_index: Optional[List[float]] = None,
    strength_pairs_jpy_first: bool = False,
    breadth_eps: float = DEFAULT_BREADTH_EPS,
    research_eps: float = DEFAULT_RESEARCH_EPS,
) -> Optional[JpyCompositeObservation]:
    """Compute the JPY Composite Strength Index observation.

    Parameters
    ----------
    strength_scores:
        Full per-currency strength dict produced by
        :func:`build_strength_matrix`. **Must contain JPY and every basket
        currency.** If any required currency is missing / NaN / non-finite
        the observation is invalidated and ``None`` is returned (§12 —
        no partial-basket renormalization).
    basket:
        Validated :class:`BasketConfig`.
    snapshot_timestamp:
        Optional observation timestamp. Defaults to ``datetime.now(UTC)``.
    history_raw_index:
        Optional list of prior ``RawIndex`` values, oldest first, used to
        compute a rolling percentile (research-only, §6). ``None`` or too
        few observations → ``rolling_percentile_research`` stays ``None``.
    strength_pairs_jpy_first:
        If True and JPY is a pair in strength_scores but not a standalone
        key, attempt to derive ``S_JPY`` from the pair scores. Not used by
        the normal production path — exists for tests only.
    breadth_eps, research_eps:
        Tolerances for breadth comparisons and research-denominator guards.

    Returns
    -------
    :class:`JpyCompositeObservation` or ``None`` if input validation fails
    (§12 strict invalidation).
    """
    ts = snapshot_timestamp or datetime.now(timezone.utc)

    if not _validate_strength_input(strength_scores, basket):
        return None

    S_JPY = float(strength_scores[basket.jpy_code])
    if not math.isfinite(S_JPY):
        return None

    S_i: Dict[str, float] = {c: float(strength_scores[c]) for c in basket.currencies}
    if not all(math.isfinite(v) for v in S_i.values()):
        return None

    W = basket.weights
    D = {c: S_JPY - S_i[c] for c in basket.currencies}
    C = {c: W[c] * D[c] for c in basket.currencies}

    RawIndex = sum(C.values())
    B = sum(W[c] * S_i[c] for c in basket.currencies)

    sigma_w_sq = sum(W[c] * (S_i[c] - B) ** 2 for c in basket.currencies)
    sigma_w = math.sqrt(max(sigma_w_sq, 0.0))

    Magnitude = sum(W[c] * abs(D[c]) for c in basket.currencies)

    if Magnitude > research_eps:
        DirectionalBalance = 50.0 + 50.0 * (RawIndex / Magnitude)
    else:
        DirectionalBalance = None

    breadth_count = sum(1 for c in basket.currencies if D[c] > breadth_eps)
    breadth_fraction = breadth_count / len(basket.currencies)
    weighted_breadth = sum(W[c] for c in basket.currencies if D[c] > breadth_eps)

    ranked = sorted(strength_scores.items(), key=lambda kv: kv[1], reverse=True)
    rank_total = len(ranked)
    rank = next((i + 1 for i, (c, _) in enumerate(ranked) if c == basket.jpy_code), 0)

    if sigma_w > research_eps:
        z_research = RawIndex / sigma_w
    else:
        z_research = None

    rolling_percentile = _rolling_percentile(history_raw_index, RawIndex)

    return JpyCompositeObservation(
        schema_version=SCHEMA_VERSION,
        snapshot_timestamp_utc=ts.isoformat(),
        weight_hash=basket.hash_,
        currencies_basket=basket.currencies,
        jpy_code=basket.jpy_code,
        raw_index=float(RawIndex),
        basket_mean=float(B),
        dispersion_sigma_w=float(sigma_w),
        magnitude=float(Magnitude),
        directional_balance=float(DirectionalBalance) if DirectionalBalance is not None else None,
        breadth_fraction=float(breadth_fraction),
        weighted_breadth=float(weighted_breadth),
        rank=int(rank),
        rank_total=int(rank_total),
        jpy_strength=float(S_JPY),
        z_research=float(z_research) if z_research is not None else None,
        rolling_percentile_research=float(rolling_percentile) if rolling_percentile is not None else None,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_strength_input(
    strength_scores: Dict[str, float], basket: BasketConfig
) -> bool:
    if not isinstance(strength_scores, dict) or not strength_scores:
        return False
    if basket.jpy_code not in strength_scores:
        return False
    for c in basket.currencies:
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
# Diagnostic / pretty printing (never writes to trading paths)
# ---------------------------------------------------------------------------

def format_observation(obs: JpyCompositeObservation) -> str:
    """Human-readable one-line summary for the observation log."""
    parts = [
        f"[JCI v{obs.schema_version}] ts={obs.snapshot_timestamp_utc}",
        f"hash={obs.weight_hash}",
        f"S_JPY={obs.jpy_strength:+.4f}",
        f"RawIndex={obs.raw_index:+.4f}",
        f"BasketMean={obs.basket_mean:+.4f}",
        f"sigma_W={obs.dispersion_sigma_w:.4f}",
        f"Magnitude={obs.magnitude:.4f}",
    ]
    if obs.directional_balance is not None:
        parts.append(f"DirBal={obs.directional_balance:.1f}")
    else:
        parts.append("DirBal=None(zero-Magnitude)")
    if obs.z_research is not None:
        parts.append(f"z={obs.z_research:+.3f}")
    else:
        parts.append("z=None(zero-dispersion)")
    parts.append(f"Breadth={obs.breadth_fraction:.0%}")
    parts.append(f"wBreadth={obs.weighted_breadth:.2f}")
    parts.append(f"Rank={obs.rank}/{obs.rank_total}")
    if obs.rolling_percentile_research is not None:
        parts.append(f"pctle={obs.rolling_percentile_research:.1f}")
    if obs.warnings:
        parts.append("warns=" + ",".join(obs.warnings))
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Offline redundancy analysis (§7 / §8) — optional, called manually
# ---------------------------------------------------------------------------

def analyze_redundancy(
    raw_indexes: List[float], sjpy_values: List[float]
) -> Dict[str, float]:
    """Compute Pearson ``corr`` and OLS ``RawIndex ≈ a + b·S_JPY`` over two
    equal-length series. Pure statistics — no trading interpretation.

    Returns ``{corr, r_squared, a, b}`` or ``{corr: nan, ...}`` if
    series are too short or degenerate.
    """
    import statistics

    n = len(raw_indexes)
    if n != len(sjpy_values) or n < 2:
        return {"corr": float("nan"), "r_squared": float("nan"),
                "a": float("nan"), "b": float("nan")}

    m_r = statistics.mean(raw_indexes)
    m_s = statistics.mean(sjpy_values)
    v_r = statistics.variance(raw_indexes)
    v_s = statistics.variance(sjpy_values)

    if v_r <= 0 or v_s <= 0:
        return {"corr": float("nan"), "r_squared": float("nan"),
                "a": float("nan"), "b": float("nan")}

    cov = sum((r - m_r) * (s - m_s) for r, s in zip(raw_indexes, sjpy_values)) / (n - 1)
    corr = cov / (math.sqrt(v_r) * math.sqrt(v_s))
    b = cov / v_s
    a = m_r - b * m_s
    ss_res = sum((r - (a + b * s)) ** 2 for r, s in zip(raw_indexes, sjpy_values))
    ss_tot = sum((r - m_r) ** 2 for r in raw_indexes)
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")

    return {"corr": corr, "r_squared": r_squared, "a": a, "b": b}


def compute_residual(
    raw_index: float, sjpy: float, a: float, b: float
) -> float:
    """``Residual = RawIndex − (a + b·S_JPY)``. Useful after
    :func:`analyze_redundancy` has produced fitted coefficients on a
    historical window."""
    return raw_index - (a + b * sjpy)