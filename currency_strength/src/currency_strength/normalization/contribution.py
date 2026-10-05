def normalize_contributions(
    net: float,
    magnitude: float,
) -> float:

    if magnitude <= 0:
        return 50.0

    return (
        50.0
        + 50.0 * net / magnitude
    )
``