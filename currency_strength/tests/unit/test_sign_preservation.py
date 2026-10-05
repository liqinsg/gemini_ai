import pytest

from currency_strength.utils.composite_strength import (
    CompositeStrengthCalculator,
)


@pytest.mark.unit
def test_sign_preservation():

    strengths = {
        "JPY": 5,
        "USD": 3,
        "EUR": 8,
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

    for item in result.contributions:

        if item.gap > 0:
            assert item.contribution > 0

        if item.gap < 0:
            assert item.contribution < 0