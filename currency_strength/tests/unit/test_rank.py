import pytest

from currency_strength.indicators.rank import (
    calculate_rank,
)


@pytest.mark.unit
def test_rank_first():

    strengths = {
        "JPY": 10,
        "USD": 5,
        "EUR": 1,
    }

    rank, total = calculate_rank(
        strengths,
        "JPY",
    )

    assert rank == 1
    assert total == 3


@pytest.mark.unit
def test_rank_middle():

    strengths = {
        "USD": 10,
        "JPY": 8,
        "EUR": 3,
    }

    rank, _ = calculate_rank(
        strengths,
        "JPY",
    )

    assert rank == 2


@pytest.mark.unit
def test_rank_last():

    strengths = {
        "USD": 10,
        "EUR": 8,
        "JPY": 1,
    }

    rank, _ = calculate_rank(
        strengths,
        "JPY",
    )

    assert rank == 3
`