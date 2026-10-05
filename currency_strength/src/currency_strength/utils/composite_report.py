from currency_strength.models.composite import (
    CompositeResult
)


def print_composite_report(
    result: CompositeResult
) -> None:

    print()
    print("=" * 70)
    print(
        f"{result.target} Composite Strength Index"
    )
    print("=" * 70)

    print()

    for row in result.contributions:

        print(
            f"{row.currency:<5}"
            f" gap={row.gap:+.4f}"
            f" weight={row.weight:.3f}"
            f" contribution={row.contribution:+.4f}"
        )

    print()

    print(
        f"Rank: "
        f"{result.rank}/{result.total_currencies}"
    )

    print(
        f"Breadth: "
        f"{result.breadth_positive}/"
        f"{result.breadth_total} "
        f"({result.breadth_ratio:.1%})"
    )

    print(
        f"Raw Composite: "
        f"{result.raw_index:+.4f}"
    )

    print(
        f"Contribution Magnitude: "
        f"{result.contribution_magnitude:.4f}"
    )

    print(
        f"Contribution Score: "
        f"{result.contribution_score:.2f}%"
    )

    print(
        f"Gap Magnitude: "
        f"{result.gap_magnitude:.4f}"
    )

    print(
        f"Gap Score: "
        f"{result.gap_score:.2f}%"
    )

    print(
        f"Dispersion: "
        f"{result.dispersion:.4f}"
    )

    print()