import pytest

from currency_strength.indicators.breadth import (
    calculate_breadth,
)


@pytest.mark.unit
def test_all_positive():

    positives, total, ratio = (
        calculate_breadth(
            [1, 2, 3]
        )
    )

    assert positives == 3
    assert total == 3
    assert ratio == 1.0


@pytest.mark.unit
def test_all_negative():

    positives, total, ratio = (
        calculate_breadth(
            [-1, -2]
        )
    )

    assert positives == 0
    assert total == 2
    assert ratio == 0.0


@pytest.mark.unit
def test_mixed():

    positives, total, ratio = (
        calculate_breadth(
            [1, -1, 3]
        )
    )

    assert positives == 2
    assert total == 3

    assert ratio == pytest.approx(
        0.666666666
    )


@pytest.mark.unit
def test_empty():

    positives, total, ratio = (
        calculate_breadth([])
    )

    assert positives == 0
    assert total == 0
    assert ratio == 0.0