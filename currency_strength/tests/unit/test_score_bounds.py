import pytest


@pytest.mark.unit
@pytest.mark.parametrize(
    "currency",
    [
        "CHF",
        "JPY",
        "GBP",
        "AUD",
        "USD",
        "EUR",
    ],
)
def test_scores_