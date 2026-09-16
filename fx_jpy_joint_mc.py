"""JPY joint Gaussian-copula Monte Carlo observer (shadow mode only).

This module deliberately has no imports from the strategy, risk, position, or
execution layers.  Its output is an observation record for later research;
nothing in this file may be used to make a trading decision.
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Any, Mapping, Optional

import numpy as np
import pandas as pd


PAIRS = ("USD_JPY", "EUR_JPY", "AUD_JPY", "CHF_JPY")
MIN_OBSERVATIONS = 60
OBSERVATIONS_LOG = Path(__file__).resolve().parent / "logs" / "jpy_joint_mc_observations.jsonl"
_NORMAL = NormalDist()


def _normal_cdf(values: np.ndarray) -> np.ndarray:
    """Normal CDF without adding a SciPy dependency."""
    return 0.5 * (1.0 + np.vectorize(math.erf)(values / math.sqrt(2.0)))


def _canonical_pair(name: Any) -> str:
    return str(name).upper().replace("=X", "").replace("/", "_").replace("-", "_").replace(" ", "_")


def _skip(reason: str, **extra: Any) -> dict[str, Any]:
    return {"event": "JPY_JOINT_MC", "timestamp": datetime.now(timezone.utc).isoformat(),
            "timeframe": "D", "status": "SKIPPED", "reason": reason, "pairs": list(PAIRS), **extra}


def _prepare_prices(prices_df: pd.DataFrame, lookback: int) -> tuple[Optional[pd.DataFrame], Optional[dict[str, Any]]]:
    if not isinstance(prices_df, pd.DataFrame) or prices_df.empty:
        return None, _skip("NO_PRICE_DATA")
    renamed = prices_df.copy()
    renamed.columns = [_canonical_pair(c) for c in renamed.columns]
    missing = [pair for pair in PAIRS if pair not in renamed.columns]
    if missing:
        return None, _skip("MISSING_PAIR", missing_pairs=missing)
    frame = renamed.loc[:, list(PAIRS)].apply(pd.to_numeric, errors="coerce")
    invalid = frame.isna().sum().to_dict()
    if any(frame[pair].isna().any() for pair in PAIRS):
        return None, _skip("NAN_PRICE_DATA", missing_values=invalid)
    if (frame <= 0).any().any():
        return None, _skip("INVALID_NONPOSITIVE_PRICE")
    # lookback returns need lookback + 1 prices; larger input is intentionally trimmed.
    frame = frame.tail(lookback + 1)
    returns = np.log(frame / frame.shift(1)).dropna()
    if len(returns) < MIN_OBSERVATIONS:
        return None, _skip("INSUFFICIENT_DATA", observations=int(len(returns)), minimum_observations=MIN_OBSERVATIONS)
    return frame, None


def _nearest_correlation(correlation: np.ndarray) -> tuple[np.ndarray, bool]:
    """Apply the smallest eigenvalue floor needed for a Cholesky-safe correlation."""
    corr = (correlation + correlation.T) / 2.0
    changed = False
    if not np.isfinite(corr).all():
        # Constant return columns have undefined rank correlation; use independence.
        return np.eye(corr.shape[0]), True
    try:
        np.linalg.cholesky(corr)
        return corr, changed
    except np.linalg.LinAlgError:
        changed = True
    values, vectors = np.linalg.eigh(corr)
    values = np.maximum(values, 1e-8)
    repaired = (vectors * values) @ vectors.T
    scale = np.sqrt(np.diag(repaired))
    repaired = repaired / np.outer(scale, scale)
    repaired = (repaired + repaired.T) / 2.0
    return repaired, changed


def run_jpy_joint_mc(prices_df: pd.DataFrame, forecast_days: int = 5,
                     n_simulations: int = 10_000, confidence_level: float = 0.90,
                     lookback: int = 90, random_seed: Optional[int] = 42) -> dict[str, Any]:
    """Run a four-cross, daily Gaussian-copula observation.

    ``prices_df`` must contain all four named pair columns.  Results are pure
    data and have no side effects; use :func:`log_jpy_joint_observation` to
    persist them explicitly.
    """
    if forecast_days not in (5, 10):
        return _skip("UNSUPPORTED_FORECAST_DAYS", forecast_days=forecast_days)
    if n_simulations < 1 or not 0 < confidence_level < 1 or lookback < MIN_OBSERVATIONS:
        return _skip("INVALID_CONFIGURATION")
    prices, skipped = _prepare_prices(prices_df, lookback)
    if skipped:
        return skipped
    assert prices is not None
    returns = np.log(prices / prices.shift(1)).dropna()
    n_obs = len(returns)
    # Empirical ranks preserve marginal shape while estimating Gaussian dependence.
    ranks = returns.rank(method="average").to_numpy() / (n_obs + 1.0)
    latent = np.array([[_NORMAL.inv_cdf(float(u)) for u in row] for row in ranks])
    if np.any(np.std(latent, axis=0) == 0):
        correlation, regularized = np.eye(len(PAIRS)), True
    else:
        correlation, regularized = _nearest_correlation(np.corrcoef(latent, rowvar=False))
    try:
        cholesky = np.linalg.cholesky(correlation)
    except np.linalg.LinAlgError:
        return _skip("CHOLESKY_FAILURE")

    rng = np.random.default_rng(random_seed)
    independent = rng.standard_normal((n_simulations, forecast_days, len(PAIRS)))
    gaussian = independent @ cholesky.T
    uniforms = _normal_cdf(gaussian)
    # Inverse empirical marginals: simulated returns, not price shocks.
    sorted_returns = np.sort(returns.to_numpy(), axis=0)
    grid = (np.arange(n_obs) + 0.5) / n_obs
    simulated_steps = np.empty_like(uniforms)
    for index in range(len(PAIRS)):
        simulated_steps[:, :, index] = np.interp(uniforms[:, :, index], grid, sorted_returns[:, index])
    final_returns = np.exp(simulated_steps.sum(axis=1)) - 1.0
    current = prices.iloc[-1].to_numpy(dtype=float)
    final_prices = current * (1.0 + final_returns)
    jpy_strength_count = (final_returns < 0).sum(axis=1)
    downside_thresholds = np.quantile(returns.to_numpy(), 0.20, axis=0)
    tail_count = (final_returns < downside_thresholds).sum(axis=1)
    dispersion = final_returns.std(axis=1)
    mean_final_return = final_returns.mean(axis=1)
    # Score is a unitless standardized basket return: positive == JPY strength.
    score_scale = float(np.std(mean_final_return))
    joint_score = float(-np.mean(mean_final_return) / score_scale) if score_scale > 0 else 0.0
    lower_q, upper_q = (1 - confidence_level) / 2, (1 + confidence_level) / 2
    off_diagonal = correlation[np.triu_indices(len(PAIRS), k=1)]
    individual = {}
    for i, pair in enumerate(PAIRS):
        individual[pair] = {
            "current_price": float(current[i]), "median_final_price": float(np.median(final_prices[:, i])),
            "lower_final_price": float(np.quantile(final_prices[:, i], lower_q)),
            "upper_final_price": float(np.quantile(final_prices[:, i], upper_q)),
            "p_positive_return": float(np.mean(final_returns[:, i] > 0)),
            "p_negative_return": float(np.mean(final_returns[:, i] < 0)),
        }
    counts = {str(k): float(np.mean(jpy_strength_count == k)) for k in range(5)}
    return {
        "event": "JPY_JOINT_MC", "timestamp": datetime.now(timezone.utc).isoformat(), "timeframe": "D",
        "status": "OK", "model": "gaussian_copula", "pairs": list(PAIRS), "lookback": lookback,
        "observations": int(n_obs), "latest_timestamp": str(prices.index[-1]), "forecast_days": forecast_days,
        "simulations": n_simulations, "confidence_level": confidence_level, "correlation_regularized": regularized,
        "correlation_matrix": {pair: {other: float(correlation[i, j]) for j, other in enumerate(PAIRS)} for i, pair in enumerate(PAIRS)},
        "avg_correlation": float(off_diagonal.mean()), "min_correlation": float(off_diagonal.min()), "max_correlation": float(off_diagonal.max()),
        "p_jpy_strength_4of4": counts["4"], "p_jpy_strength_3of4": counts["3"], "p_jpy_strength_2of4": counts["2"],
        "p_jpy_strength_1of4": counts["1"], "p_jpy_strength_0of4": counts["0"],
        "p_jpy_strength_3_or_more": float(np.mean(jpy_strength_count >= 3)), "jpy_joint_score": joint_score,
        "dispersion_p10": float(np.quantile(dispersion, .10)), "dispersion_median": float(np.median(dispersion)), "dispersion_p90": float(np.quantile(dispersion, .90)),
        "p_all_4_downside_tail": float(np.mean(tail_count == 4)), "p_3_or_more_downside_tail": float(np.mean(tail_count >= 3)),
        "individual": individual,
    }


def log_jpy_joint_observation(record: Mapping[str, Any], log_path: Path = OBSERVATIONS_LOG) -> bool:
    """Append an observation only; this JSONL file is never read by execution code."""
    if record.get("status") != "OK":
        return False
    payload = dict(record)
    payload.update({"forecast_start_prices": {p: payload["individual"][p]["current_price"] for p in PAIRS},
                    "actual_5d_outcome": None, "actual_resolved_at": None})
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def format_report(record: Mapping[str, Any]) -> str:
    if record.get("status") != "OK":
        return f"JPY JOINT MONTE CARLO — SHADOW OBSERVATION\nSTATUS: SKIPPED\nREASON: {record.get('reason')}"
    lines = ["=" * 56, "JPY JOINT MONTE CARLO — SHADOW OBSERVATION", "=" * 56,
             f"Model: Gaussian Copula | Timeframe: D | Lookback: {record['lookback']}",
             f"Forecast: {record['forecast_days']}D | Simulations: {record['simulations']:,}", "",
             "JPY JOINT DIRECTION"]
    for k in (4, 3, 2, 1, 0): lines.append(f"{k}/4 JPY Strength : {record[f'p_jpy_strength_{k}of4']:.1%}")
    lines += [f"P(>=3/4 JPY Strength): {record['p_jpy_strength_3_or_more']:.1%}", f"JPY Joint Score: {record['jpy_joint_score']:+.2f}", "",
              f"Cross Dispersion  P10/P50/P90: {record['dispersion_p10']:.4%} / {record['dispersion_median']:.4%} / {record['dispersion_p90']:.4%}",
              f"Dependence Avg/Min/Max: {record['avg_correlation']:.2f} / {record['min_correlation']:.2f} / {record['max_correlation']:.2f}", "", "INDIVIDUAL PAIRS"]
    for pair, values in record["individual"].items():
        lines.append(f"{pair}: P(up) {values['p_positive_return']:.1%}, P(down) {values['p_negative_return']:.1%}, final P05/Med/P95 {values['lower_final_price']:.3f}/{values['median_final_price']:.3f}/{values['upper_final_price']:.3f}")
    return "\n".join(lines + ["", "STATUS: OBSERVATION ONLY — NO TRADING DECISION WAS MODIFIED", "=" * 56])


def fetch_daily_prices() -> pd.DataFrame:
    """Fetch only the four daily closes using the project's existing yfinance source."""
    import yfinance as yf
    data = {}
    for pair in PAIRS:
        ticker = pair.replace("_", "") + "=X"
        frame = yf.download(ticker, period="1y", interval="1d", progress=False, auto_adjust=False)
        if frame.empty or "Close" not in frame:
            continue
        close = frame["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        data[pair] = close
    return pd.DataFrame(data)


def _synthetic_prices(rows: int = 130) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    common = rng.normal(0, .004, rows)
    data = {pair: start * np.exp(np.cumsum(common + rng.normal(0, .002, rows)))
            for pair, start in zip(PAIRS, (150, 162, 98, 171))}
    return pd.DataFrame(data, index=pd.date_range("2026-01-01", periods=rows, freq="B"))


def main() -> int:
    parser = argparse.ArgumentParser(description="JPY joint MC shadow observer")
    parser.add_argument("--forecast-days", type=int, default=5, choices=(5, 10))
    parser.add_argument("--simulations", type=int, default=10_000)
    parser.add_argument("--demo", action="store_true", help="Run deterministic synthetic data demo; does not log.")
    args = parser.parse_args()
    record = run_jpy_joint_mc(_synthetic_prices() if args.demo else fetch_daily_prices(), args.forecast_days, args.simulations)
    print(format_report(record))
    if not args.demo and record.get("status") == "OK":
        print(f"Observation log: {'written' if log_jpy_joint_observation(record) else 'FAILED'}")
    return 0 if record.get("status") == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
