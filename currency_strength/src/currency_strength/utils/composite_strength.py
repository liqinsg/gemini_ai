from typing import Dict
import math

from currency_strength.models.composite import (
    CompositeContribution,
    CompositeResult,
)

from currency_strength.indicators.rank import (
    calculate_rank,
)

from currency_strength.indicators.breadth import (
    calculate_breadth,
)

from currency_strength.normalization.contribution import (
    normalize_contributions,
)

from currency_strength.normalization.gap import (
    normalize_gaps,
)


class CompositeStrengthCalculator:

    def __init__(
        self,
        basket_weights: Dict[str, float],
        normalize_weights: bool = True,
    ):

        self.weights = dict(
            basket_weights
        )

        if normalize_weights:

            total = sum(
                self.weights.values()
            )

            if total <= 0:
                raise ValueError(
                    "weight sum must be positive"
                )

            self.weights = {
                k: v / total
                for k, v
                in self.weights.items()
            }

        for ccy, weight in self.weights.items():

            if weight <= 0:
                raise ValueError(
                    f"{ccy} weight must be > 0"
                )

    def calculate(
        self,
        strengths: Dict[str, float],
        target: str,
    ) -> CompositeResult:

        if target not in strengths:
            raise ValueError(
                f"{target} not found"
            )

        target_strength = strengths[target]

        contributions = []

        gaps = []

        for ccy, weight in self.weights.items():

            if ccy == target:
                continue

            if ccy not in strengths:
                raise ValueError(
                    f"{ccy} not found"
                )

            strength = strengths[ccy]

            gap = (
                target_strength
                - strength
            )

            contribution = gap * weight

            gaps.append(gap)

            contributions.append(
                CompositeContribution(
                    currency=ccy,
                    strength=strength,
                    weight=weight,
                    gap=gap,
                    contribution=contribution,
                )
            )

        raw_index = sum(
            x.contribution
            for x in contributions
        )

        contribution_magnitude = sum(
            abs(x.contribution)
            for x in contributions
        )

        gap_net = sum(gaps)

        gap_magnitude = sum(
            abs(x)
            for x in gaps
        )

        contribution_score = (
            normalize_contributions(
                raw_index,
                contribution_magnitude,
            )
        )

        gap_score = (
            normalize_gaps(
                gap_net,
                gap_magnitude,
            )
        )

        rank, total_ccy = calculate_rank(
            strengths,
            target,
        )

        breadth_positive, breadth_total, breadth_ratio = (
            calculate_breadth(
                gaps
            )
        )

        if contributions:

            mean_contrib = (
                raw_index
                / len(contributions)
            )

            variance = (
                sum(
                    (
                        c.contribution
                        - mean_contrib
                    ) ** 2
                    for c in contributions
                )
                / len(contributions)
            )

            dispersion = math.sqrt(
                variance
            )

        else:
            dispersion = 0.0

        return CompositeResult(
            target=target,

            raw_index=raw_index,

            net_contribution=raw_index,

            contribution_magnitude=contribution_magnitude,

            contribution_score=contribution_score,

            gap_net=gap_net,

            gap_magnitude=gap_magnitude,

            gap_score=gap_score,

            rank=rank,

            total_currencies=total_ccy,

            breadth_positive=breadth_positive,

            breadth_total=breadth_total,

            breadth_ratio=breadth_ratio,

            dispersion=dispersion,

            contributions=contributions,
        )

    def calculate_all(
        self,
        strengths: Dict[str, float],
    ) -> dict[str, CompositeResult]:

        return {
            currency: self.calculate(
                strengths,
                currency,
            )
            for currency in strengths
        }