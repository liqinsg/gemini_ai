"""
Human-readable + bot-consumable report printers for CompositeResult.

Usage:
    from utils.composite_report import print_report, print_all_report
    from utils.composite_strength import CompositeConfig, compute_composite
    from utils.strategy_helpers import build_strength_matrix

    scores = build_strength_matrix()
    cfg = CompositeConfig.build({"USD":0.30,"EUR":0.25,"GBP":0.20,"AUD":0.15,"CHF":0.10}, target_code="JPY")
    obs = compute_composite(scores, cfg)
    print_report(obs)
    print(obs.to_json())
"""
from __future__ import annotations

from typing import Dict, Optional

from utils.composite_strength import CompositeResult


def print_report(result: CompositeResult) -> None:
    """Human-readable breakdown of one target's Composite Index."""
    print()
    print("=" * 72)
    print(f"  {result.target}  Composite Strength Index  "
          f"v{result.schema_version}")
    print("=" * 72)
    print(f"  ts    : {result.snapshot_timestamp_utc}")
    print(f"  hash  : {result.weight_hash}")
    print(f"  target strength S_{result.target} : {result.target_strength:+.4f}")
    if result.normalize_weights_applied:
        print(f"  (normalize_weights=True was applied)")
    print()

    print(f"  {'basket':<6} {'S_i':>9} {'w':>7} {'gap':>10} {'C_i=w·gap':>12}")
    print(f"  {'-'*5}  {'-'*8}  {'-'*6}  {'-'*9}  {'-'*11}")
    for c in result.contributions:
        sign = "+" if c.contribution > 0 else ""
        w_str = f"{c.weight:.3f}"
        print(f"  {c.currency:<6} {c.basket_strength:>+9.4f} "
              f"{w_str:>7} {c.gap:>+10.4f} "
              f"{sign}{c.contribution:>+11.4f}")
    print()

    print(f"  RawIndex (Σ w·gap)          : {result.raw_index:+.4f}")
    print(f"  BasketMean  (Σ w·S_i)       : {result.basket_mean:+.4f}")
    print(f"  sigma_W  (basket dispersion): {result.basket_dispersion_sigma_w:.4f}")
    print(f"  σ_C  (contribution disp.)   : {result.contribution_dispersion:.4f}")
    print()

    if result.directional_balance is not None:
        bar_len = int(result.directional_balance / 2.0)
        bar = "█" * bar_len + "░" * (50 - bar_len)
        print(f"  DirectionalBalance (weighted, §3)  : "
              f"{result.directional_balance:>6.2f}  {bar}")
    else:
        print(f"  DirectionalBalance                 : None  (zero Magnitude)")

    bar_len = int(result.gap_score / 2.0)
    bar = "█" * bar_len + "░" * (50 - bar_len)
    print(f"  GapScore (unweighted gaps)         : "
          f"{result.gap_score:>6.2f}  {bar}")
    print()

    print(f"  Breadth    : {result.breadth_positive}/{result.breadth_total} "
          f"({result.breadth_fraction:.0%} of basket beats {result.target})")
    print(f"  wBreadth   : {result.weighted_breadth:.3f} "
          f"(weighted fraction with positive gap)")
    print(f"  Rank       : {result.rank}/{result.rank_total}")
    print()

    if result.z_research is not None:
        print(f"  z (RawIndex / sigma_W)     : {result.z_research:+.3f}")
    else:
        print(f"  z (RawIndex / sigma_W)     : None  (zero basket dispersion)")
    if result.rolling_percentile_research is not None:
        print(f"  rolling percentile          : {result.rolling_percentile_research:.1f}%")

    print()


def print_rank_line(result: CompositeResult) -> None:
    """Compact one-liner suitable for rank tables."""
    line = (
        f"  {result.target:<4} RawIndex={result.raw_index:+.4f}  "
        f"DirBal={result.directional_balance if result.directional_balance is not None else 0:>5.1f}  "
        f"GapScore={result.gap_score:>5.1f}  "
        f"Breadth={result.breadth_positive}/{result.breadth_total}  "
        f"Rank={result.rank}/{result.rank_total}  "
        f"z={result.z_research if result.z_research is not None else 0:+.3f}  "
        f"S_{result.target}={result.target_strength:+.4f}"
    )
    print(line)


def print_all_report(results: Dict[str, Optional[CompositeResult]],
                     *, title: str = "Global Currency Composite Strength Matrix") -> None:
    """Print every target's composite result, sorted by RawIndex desc."""
    valid = [(t, r) for t, r in results.items() if r is not None]
    valid.sort(key=lambda x: x[1].raw_index, reverse=True)
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)
    for i, (target, r) in enumerate(valid, 1):
        print_rank_line(r)
    print()


def to_bot_dict(result: CompositeResult) -> dict:
    """Flattened dict suitable for feeding to trading bots / thresholds."""
    return {
        "target": result.target,
        "raw_index": result.raw_index,
        "directional_balance": result.directional_balance,
        "gap_score": result.gap_score,
        "basket_mean": result.basket_mean,
        "sigma_w": result.basket_dispersion_sigma_w,
        "contribution_dispersion": result.contribution_dispersion,
        "breadth_fraction": result.breadth_fraction,
        "weighted_breadth": result.weighted_breadth,
        "rank": result.rank,
        "rank_total": result.rank_total,
        "z": result.z_research,
        "target_strength": result.target_strength,
        "contributions": [
            {
                "currency": c.currency,
                "strength": c.basket_strength,
                "weight": c.weight,
                "gap": c.gap,
                "contribution": c.contribution,
            }
            for c in result.contributions
        ],
    }