import pytest


@pytest.mark.unit
def test_dispersion_non_negative(
    calculator,
    proposal_strengths,
):

    result = calculator.calculate(
        proposal_strengths,
        "JPY",
    )

    assert result.dispersion >= 0


@pytest.mark.unit
def test_dispersion_zero_when_equal():

    from currency_strength.utils.composite_strength import (
        CompositeStrengthCalculator,
    )

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

    assert result.dispersion == 0