import pytest

from currency_strength.utils.composite_strength import (
    CompositeStrengthCalculator,
)


@pytest.mark.unit
def test_neutral_strength():

    strengths = {
        "JPY": 1,
        "USD": 1,
        "EUR": 1,
    }

    calc = CompositeStrengthCalculator(
        {
            "USD": 0.5,
            "EUR": 0.5,
        }
    )

    result = calc.calculate(
        strengths,
        "JPY",
    )

    assert result.raw_index == 0.0

    assert (
        result.contribution_score
        == 50.0
    )

    assert (
        result.gap_score
        == 50.0
    )