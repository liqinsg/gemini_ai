"""
JPY Composite Strength Index — LIVE OBSERVER  V1.2
Layer 3: Real market strength scores → full transparent index output
STRICT: no silent fallbacks, no demo data, no trading logic touched.

Usage (from scheduled_runner_v3 AFTER building strength_scores):
    from utils.jpy_index_live import print_live_jpy_index
    print_live_jpy_index(strength_scores)
"""

from __future__ import annotations
import csv
import os
import sys
import re
from datetime import datetime, timezone
from typing import Dict, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from utils.jpy_composite_index import (
    BasketConfig,
    compute_jpy_composite,
    JpyCompositeObservation,
)

REQUIRED_WEIGHT_KEYS = [
    "JPY_WEIGHTED_WEIGHT_EUR",
    "JPY_WEIGHTED_WEIGHT_USD",
    "JPY_WEIGHTED_WEIGHT_GBP",
    "JPY_WEIGHTED_WEIGHT_AUD",
    "JPY_WEIGHTED_WEIGHT_CHF",
]
BASKET_CURRENCIES = ["EUR", "USD", "GBP", "AUD", "CHF"]
TARGET_CURRENCY = "JPY"


def _load_strict_env() -> Dict[str, float]:
    """Load weights from run.env — FAIL LOUD if missing/invalid. NO defaults."""
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    env_path = os.path.join(project_root, "run.env")

    if not os.path.exists(env_path):
        raise FileNotFoundError(
            f"[JPY INDEX LIVE] Cannot locate run.env → expected at: {env_path}"
        )

    raw: Dict[str, float] = {}
    found_keys: set = set()
    env_content = ""

    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            env_content += line
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = re.sub(r"\s+#.*$", "", val).strip()

            if key in REQUIRED_WEIGHT_KEYS:
                try:
                    raw[key] = float(val)
                    found_keys.add(key)
                except ValueError:
                    raise ValueError(
                        f"[JPY INDEX LIVE] Invalid value: {key}={val!r} — must be numeric"
                    )

    # Strict: ALL required weights must exist
    missing = [k for k in REQUIRED_WEIGHT_KEYS if k not in found_keys]
    if missing:
        raise ValueError(
            f"[JPY INDEX LIVE] MISSING weight(s) in run.env: {missing}\n"
            "→ Cannot compute LIVE index with guessed defaults. Fix run.env first."
        )

    # Warn: JPY weight is legacy, not used here
    if "JPY_WEIGHTED_WEIGHT_JPY" in env_content:
        print("⚠️  [JPY INDEX LIVE] Note: JPY_WEIGHTED_WEIGHT_JPY found in run.env")
        print(f"   → {TARGET_CURRENCY} is the target, never in its own basket.")
        print("   → This parameter belongs to OLD Gate, NOT USED by Composite Index.\n")

    # Map to currency codes
    weights = {
        "EUR": raw["JPY_WEIGHTED_WEIGHT_EUR"],
        "USD": raw["JPY_WEIGHTED_WEIGHT_USD"],
        "GBP": raw["JPY_WEIGHTED_WEIGHT_GBP"],
        "AUD": raw["JPY_WEIGHTED_WEIGHT_AUD"],
        "CHF": raw["JPY_WEIGHTED_WEIGHT_CHF"],
    }

    # NEW: Strict validation — reject zero/negative weights before normalization
    for c, w in weights.items():
        if w <= 0:
            raise ValueError(f"[JPY INDEX LIVE] Invalid weight: {c}={w} — must be > 0")
    total = sum(weights.values())
    if total <= 0:
        raise ValueError(f"[JPY INDEX LIVE] Total weight = {total} — must be positive")

    return weights


def _format_obs(
    obs: JpyCompositeObservation,
    strength_scores: Dict[str, float],
    raw_weights: Dict[str, float],
    normalized_weights: Dict[str, float],
    timestamp: datetime,
) -> str:
    """Full traceability: inputs → weights → contributions → result."""
    S_jpy = strength_scores[TARGET_CURRENCY]

    lines = [
        "=" * 72,
        "📊 JPY COMPOSITE INDEX — LIVE OBSERVATION  V1.2",
        f"🕒 Timestamp : {timestamp.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "🔢 Data source: real-time strength matrix",
        "",
        "── INPUT STRENGTH SCORES ──",
        f"  {TARGET_CURRENCY:4s} (target)  = {S_jpy:+10.6f}",
    ]

    for c in BASKET_CURRENCIES:
        s = strength_scores[c]
        lines.append(f"  {c:4s} (basket)  = {s:+10.6f}")

    lines.extend(
        [
            "",
            "── BASKET WEIGHTS ──",
            f"  {'Currency':<8s} {'Raw(env)':>12s} {'Normalized':>12s}",
        ]
    )
    for c in BASKET_CURRENCIES:
        lines.append(f"  {c:<8s} {raw_weights[c]:12.4f} {normalized_weights[c]:12.6f}")

    lines.extend(["", "── CONTRIBUTION BREAKDOWN ──"])
    for c in BASKET_CURRENCIES:
        gap = S_jpy - strength_scores[c]
        contrib = normalized_weights[c] * gap
        flag = "✅" if contrib > 0 else "❌" if contrib < 0 else "➖"
        lines.append(
            f"  {flag} {c}: (JPY-{c}) = {gap:+9.6f} × {normalized_weights[c]:.6f} = {contrib:+9.6f}"
        )

    trend = (
        "↑ BULL"
        if obs.raw_index > 0
        else "↓ BEAR" if obs.raw_index < 0 else "═ NEUTRAL"
    )
    db = getattr(obs, "directional_balance", None)
    mag = getattr(obs, "magnitude", None)
    z = getattr(obs, "z_research", None)
    bf = getattr(obs, "breadth_fraction", None)
    wb = getattr(obs, "weighted_breadth", None)

    lines.extend(
        [
            "",
            "── FINAL RESULT ──",
            f"  RawIndex         = {obs.raw_index:+10.6f}  {trend}",
        ]
    )

    # FIX: No fabricated default for 0.0 — None = N/A, actual 0.0 = displayed
    if db is not None:
        lines.append(
            f"  DirectionalBalance = {db:.2f}  (0=weak / 50=neutral / 100=strong)"
        )
    else:
        lines.append("  DirectionalBalance = N/A")

    if mag is not None:
        lines.append(f"  Magnitude         = {mag:.6f}  (consensus strength)")
    if z is not None:
        lines.append(f"  Z-ratio (σ)       = {z:.4f}  (signal/noise)")

    lines.extend(
        [
            f"  Rank              = {obs.rank}/{obs.rank_total}",
        ]
    )
    if bf is not None:
        lines.append(f"  BreadthFraction   = {bf:.1%}  (unanimity across basket)")
    else:
        lines.append("  BreadthFraction   = N/A")
    if wb is not None:
        lines.append(f"  WeightedBreadth   = {wb:.1%}")
    else:
        lines.append("  WeightedBreadth   = N/A")

    lines.extend(
        [
            f"  BasketMean        = {obs.basket_mean:+.6f}  (average opponent strength)",
            f"  σ_W (dispersion)  = {obs.dispersion_sigma_w:.6f}",
            f"  Config Hash       = {obs.weight_hash}",
            "=" * 72,
        ]
    )
    return "\n".join(lines)


# ... 保留前面已有的全部代码 ...


def _append_to_csv(
    obs: JpyCompositeObservation,
    strength_scores: Dict[str, float],
    raw_weights: Dict[str, float],
    normalized_weights: Dict[str, float],
    timestamp: datetime,
    csv_path: str = "logs/jpy_index_history.csv",
) -> None:
    """Append this observation to CSV — creates dir/file if missing."""
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)

    row = {
        "timestamp_utc": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "raw_index": f"{obs.raw_index:.8f}",
        "directional_balance": (
            f"{db:.4f}"
            if (db := getattr(obs, "directional_balance", None)) is not None
            else ""
        ),
        "magnitude": (
            f"{mag:.8f}" if (mag := getattr(obs, "magnitude", None)) is not None else ""
        ),
        "z_research": (
            f"{z:.6f}" if (z := getattr(obs, "z_research", None)) is not None else ""
        ),
        "rank": obs.rank,
        "rank_total": obs.rank_total,
        "breadth_fraction": (
            f"{bf:.6f}"
            if (bf := getattr(obs, "breadth_fraction", None)) is not None
            else ""
        ),
        "weighted_breadth": (
            f"{wb:.6f}"
            if (wb := getattr(obs, "weighted_breadth", None)) is not None
            else ""
        ),
        "basket_mean": f"{obs.basket_mean:.8f}",
        "dispersion_sigma_w": f"{obs.dispersion_sigma_w:.8f}",
        "weight_hash": obs.weight_hash,
        # Input scores — full traceability
        "score_jpy": f"{strength_scores['JPY']:.8f}",
        "score_eur": f"{strength_scores['EUR']:.8f}",
        "score_usd": f"{strength_scores['USD']:.8f}",
        "score_gbp": f"{strength_scores['GBP']:.8f}",
        "score_aud": f"{strength_scores['AUD']:.8f}",
        "score_chf": f"{strength_scores['CHF']:.8f}",
        # Normalized weights — config snapshot
        "w_eur": f"{normalized_weights['EUR']:.6f}",
        "w_usd": f"{normalized_weights['USD']:.6f}",
        "w_gbp": f"{normalized_weights['GBP']:.6f}",
        "w_aud": f"{normalized_weights['AUD']:.6f}",
        "w_chf": f"{normalized_weights['CHF']:.6f}",
        # Contributions
        "contrib_eur": f"{(normalized_weights['EUR'] * (strength_scores['JPY'] - strength_scores['EUR'])):.8f}",
        "contrib_usd": f"{(normalized_weights['USD'] * (strength_scores['JPY'] - strength_scores['USD'])):.8f}",
        "contrib_gbp": f"{(normalized_weights['GBP'] * (strength_scores['JPY'] - strength_scores['GBP'])):.8f}",
        "contrib_aud": f"{(normalized_weights['AUD'] * (strength_scores['JPY'] - strength_scores['AUD'])):.8f}",
        "contrib_chf": f"{(normalized_weights['CHF'] * (strength_scores['JPY'] - strength_scores['CHF'])):.8f}",
    }

    write_header = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if write_header:
            writer.writeheader()
        writer.writerow(row)

    print(f"💾 Logged → {csv_path}")


def print_live_jpy_index(
    strength_scores: Dict[str, float],
    timestamp: Optional[datetime] = None,
    csv_path: str = "logs/jpy_index_history.csv",
) -> Optional[JpyCompositeObservation]:
    ts = timestamp or datetime.now(timezone.utc)

    # 1. Load config — strict
    try:
        raw_weights = _load_strict_env()
    except (ValueError, FileNotFoundError) as e:
        print(f"\n❌ [JPY INDEX LIVE] CONFIG ERROR: {e}\n")
        return None

    total_w = sum(raw_weights.values())
    normalized = {c: w / total_w for c, w in raw_weights.items()}

    # 2. Validate inputs
    required = ["JPY", "EUR", "USD", "GBP", "AUD", "CHF"]
    if missing := [c for c in required if c not in strength_scores]:
        print(f"\n❌ [JPY INDEX LIVE] DATA INCOMPLETE — missing: {missing}\n")
        return None

    # 3. Compute
    cfg = BasketConfig.build(normalized, jpy_code="JPY")

    obs = compute_jpy_composite(strength_scores, cfg, snapshot_timestamp=ts)
    if obs is None:
        print("\n❌ [JPY INDEX LIVE] COMPUTATION FAILED\n")
        return None

    # 4. Print full report
    print()
    print(_format_obs(obs, strength_scores, raw_weights, normalized, ts))

    # 5. Append to CSV — NEW
    _append_to_csv(obs, strength_scores, raw_weights, normalized, ts, csv_path)
    print()

    return obs
