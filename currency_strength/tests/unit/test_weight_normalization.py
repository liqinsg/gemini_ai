import pytest

from currency_strength.utils.composite_strength import (
    CompositeStrengthCalculator,
)


@pytest.mark.unit
def test_normalization():

    calc = CompositeStrengthCalculator(
        {
            "USD": 30,
            "EUR": 25,
            "GBP": 20,
            "AUD": 15,
            "CHF": 10,
        }
    )

    assert sum(
        calc.weights.values()
    ) == pytest.approx(1.0)