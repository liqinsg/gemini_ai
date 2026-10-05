def percentile_rank(
    value: float,
    samples: list[float],
) -> float:

    if not samples:
        return 50.0

    below = sum(
        1 for x in samples
        if x <= value
    )

    return (
        below / len(samples)
    ) * 100.0