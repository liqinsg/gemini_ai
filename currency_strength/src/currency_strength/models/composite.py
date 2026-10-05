from dataclasses import dataclass
from typing import List


@dataclass(slots=True)
class CompositeContribution:
    """
    单个货币对目标货币的贡献
    """

    currency: str

    strength: float

    weight: float

    gap: float

    contribution: float


@dataclass(slots=True)
class CompositeResult:
    """
    综合强弱指数结果
    """

    target: str

    raw_index: float

    net_contribution: float

    contribution_magnitude: float

    contribution_score: float

    gap_net: float

    gap_magnitude: float

    gap_score: float

    rank: int

    total_currencies: int

    breadth_positive: int

    breadth_total: int

    breadth_ratio: float

    dispersion: float

    contributions: List[CompositeContribution]