import pytest


@pytest.mark.unit
def test_calculate_all(
    calculator,
    proposal_strengths,
):

    result = calculator.calculate_all(
        proposal_strengths
    )

    assert len(result) == len(
        proposal_strengths
    )

    assert "JPY" in result
    assert "USD" in result
    assert "EUR" in result

    assert (
        result["CHF"].rank == 1
    )

    assert (
        result["EUR"].rank == 6
    )