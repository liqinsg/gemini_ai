from typing import Iterable


def calculate_breadth(
    gaps: Iterable[float],
) -> tuple[int, int, float]:

    gaps = list(gaps)

    positives = sum(
        1
        for value in gaps
        if value > 0
    )

    total = len(gaps)

    ratio = (
        positives / total
        if total > 0
        else 0.0
    )

    return positives, total, ratio