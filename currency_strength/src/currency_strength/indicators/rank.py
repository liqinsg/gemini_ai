from typing import Dict


def calculate_rank(
    strengths: Dict[str, float],
    target: str,
) -> tuple[int, int]:

    sorted_strengths = sorted(
        strengths.items(),
        key=lambda item: item[1],
        reverse=True,
    )

    rank = next(
        idx + 1
        for idx, (currency, _)
        in enumerate(sorted_strengths)
        if currency == target
    )

    return rank, len(strengths)