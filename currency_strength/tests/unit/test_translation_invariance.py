import pytest


@pytest.mark.unit
def test_translation_invariance(
    calculator,
    proposal_strengths,
):

    shifted = {
        k: v + 100
        for k, v
        in proposal_strengths.items()
    }

    r1 = calculator.calculate(
        proposal_strengths,
        "JPY",
    )

    r2 = calculator.calculate(
        shifted,
        "JPY",
    )

    assert r1.raw_index == pytest.approx(
        r2.raw_index
    )

    assert (
        r1.contribution_score
        == pytest.approx(
            r2.contribution_score
        )
    )