import pytest


@pytest.mark.unit
def test_scale_behavior(
    calculator,
    proposal_strengths,
):

    scaled = {
        k: v * 100
        for k, v
        in proposal_strengths.items()
    }

    r1 = calculator.calculate(
        proposal_strengths,
        "JPY",
    )

    r2 = calculator.calculate(
        scaled,
        "JPY",
    )

    assert (
        r2.raw_index
        == pytest.approx(
            r1.raw_index * 100
        )
    )

    assert (
        r2.contribution_score
        == pytest.approx(
            r1.contribution_score
        )
    )

    assert (
        r2.gap_score
        == pytest.approx(
            r1.gap_score
        )
    )