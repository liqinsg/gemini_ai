# utils/currency_strength.py
"""
Main entry point for currency strength calculation
Now with professional logging
"""
from config import (
    STRENGTH_PAIRS,
    STRENGTH_TIMEFRAMES,
    STRENGTH_SLOW_LOOKBACK
)
from .calculate_currency_strength import calculate_currency_strength
from .logger import logger


def get_currency_strength():
    """
    Returns:
        sorted_ranking: list[(currency_code, score)] strongest → weakest
        raw_scores: dict {currency_code: score}
    """
    timeframes = list(STRENGTH_TIMEFRAMES.keys())
    weights = list(STRENGTH_TIMEFRAMES.values())
    lookback = STRENGTH_SLOW_LOOKBACK

    logger.info("=== Calculating Currency Strength ===")
    logger.info(f"Timeframes: {timeframes}, Weights: {weights}, Lookback: {lookback}")

    scores = calculate_currency_strength(
        pairs=STRENGTH_PAIRS,
        timeframes=timeframes,
        weights=weights,
        lookback=lookback
    )

    sorted_ranking = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    # Log in both human-readable AND machine-friendly format
    logger.info("--- Strength Ranking ---")
    for curr, score in sorted_ranking:
        logger.info(f"{curr:4s} | {score:+.4f}")

    # Log as JSON-like string for easy parsing later
    logger.info(f"STRENGTH_SCORES: {scores}")

    return sorted_ranking, scores


if __name__ == "__main__":
    ranking, _ = get_currency_strength()
    print("\n=== Final Ranking ===")
    for curr, score in ranking:
        bar = "█" * max(0, int(score * 8)) if score > 0 else "░" * max(0, int(-score * 8))
        print(f"{curr:4s}: {score:+.4f} {'▲' if score > 0 else '▼'} {bar}")