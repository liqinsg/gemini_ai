import pytest

from currency_strength.normalization.contribution import (
    normalize_contributions,
)

from currency_strength.normalization.gap import (
    normalize_gaps,
)


@pytest.mark.unit
def test_positive_normalization():

    score = normalize_contributions(
        10,
        10,
    )

    assert score == 100.0


@pytest.mark.unit
def test_negative_normalization():

    score = normalize_contributions(
        -10,
        10,
    )

    assert score == 0.0


@pytest.mark.unit
def test_neutral_normalization():

    score = normalize_contributions(
        0,
        10,
    )

    assert score == 50.0


@pytest.mark.unit
def test_gap_normalization():

    score = normalize_gaps(
        5,
        10,
    )

    assert score == 75.0