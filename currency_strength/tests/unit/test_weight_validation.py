import pytest

from currency_strength.utils.composite_strength import (
    CompositeStrengthCalculator,
)


@pytest.mark.unit
def test_negative_weight():

    with pytest.raises(
        ValueError
    ):
        CompositeStrengthCalculator(
            {
                "USD": 1,
                "EUR": -1,
            }
        )


@pytest.mark.unit
def test_zero_weight():

    with pytest.raises(
        ValueError
    ):
        CompositeStrengthCalculator(
            {
                "USD": 1,
                "EUR": 0,
            }
        )