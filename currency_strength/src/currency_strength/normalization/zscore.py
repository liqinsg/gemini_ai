from statistics import mean
from statistics import pstdev


def zscore(
    value: float,
    samples: list[float],
) -> float:

    if len(samples) < 2:
        return 0.0

    std = pstdev(samples)

    if std == 0:
        return 0.0

    return (
        value - mean(samples)
    ) / std