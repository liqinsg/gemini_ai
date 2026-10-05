import pytest

from currency_strength.utils.composite_strength import (
    CompositeStrengthCalculator,
)


@pytest.mark.unit
def test_missing_target():

    calc = CompositeStrengthCalculator(
        {
            "USD": 0.5,
            "EUR": 0.5,
        }
    )

    with pytest.raises(
        ValueError
    ):
        calc.calculate(
            {
                "USD": 1,
                "EUR": 2,
            },
            "JPY",
        )


@pytest.mark.unit
def test_missing_basket_currency():

    calc = CompositeStrengthCalculator(
        {
            "USD": 0.5,
            "EUR": 0.5,
        }
    )

    strengths = {
        "JPY": 2,
        "USD": 1,
    }

    with pytest.raises(
        ValueError
    ):
        calc.calculate(
            strengths,
            "JPY",
        )