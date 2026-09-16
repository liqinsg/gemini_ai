import numpy as np
import pandas as pd

from fx_jpy_joint_mc import PAIRS, format_report, run_jpy_joint_mc


def prices(rows=100):
    rng = np.random.default_rng(4)
    common = rng.normal(0, .003, rows)
    return pd.DataFrame({pair: start * np.exp(np.cumsum(common + rng.normal(0, .001, rows)))
                         for pair, start in zip(PAIRS, (150, 160, 95, 170))})


def test_valid_four_pair_data_produces_joint_observation():
    result = run_jpy_joint_mc(prices(), n_simulations=300, random_seed=3)
    assert result["status"] == "OK"
    assert result["event"] == "JPY_JOINT_MC"
    assert set(result["individual"]) == set(PAIRS)
    assert "OBSERVATION ONLY" in format_report(result)


def test_missing_pair_skips_without_reducing_dimension():
    result = run_jpy_joint_mc(prices().drop(columns="CHF_JPY"), n_simulations=20)
    assert result["status"] == "SKIPPED"
    assert result["reason"] == "MISSING_PAIR"


def test_constant_prices_do_not_crash_and_are_regularized():
    flat = pd.DataFrame({pair: np.repeat(value, 100) for pair, value in zip(PAIRS, (150, 160, 95, 170))})
    result = run_jpy_joint_mc(flat, n_simulations=100)
    assert result["status"] == "OK"
    assert result["correlation_regularized"] is True
    assert result["jpy_joint_score"] == 0.0


def test_simulation_shape_implied_by_probabilities_and_valid_correlation():
    result = run_jpy_joint_mc(prices(), n_simulations=500)
    assert result["status"] == "OK"
    matrix = np.array([[result["correlation_matrix"][a][b] for b in PAIRS] for a in PAIRS])
    np.linalg.cholesky(matrix)
    probabilities = [result[f"p_jpy_strength_{n}of4"] for n in range(5)]
    assert all(0 <= value <= 1 for value in probabilities)
    assert sum(probabilities) == 1.0
    for values in result["individual"].values():
        assert 0 <= values["p_positive_return"] <= 1
        assert 0 <= values["p_negative_return"] <= 1
