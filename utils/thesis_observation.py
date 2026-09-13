"""Passive, per-cycle observations for JPY-cross positions."""

from __future__ import annotations

from typing import Mapping, Optional


def get_thesis_snapshot(
    instrument: str,
    direction: str,
    strength_matrix: Optional[Mapping[str, float]],
    trade_pairs: Optional[list[str]] = None,
) -> dict:
    """Return raw JPY rank, gap, breadth, and pair-strength observations."""
    snapshot = {
        "jpy_rank": None,
        "jpy_gap": None,
        "confirming_breadth": None,
        "total_crosses": None,
        "pair_strength": None,
    }
    if not strength_matrix or "JPY" not in strength_matrix:
        return snapshot

    currencies = list(strength_matrix.items())
    ordered = sorted(currencies, key=lambda item: item[1], reverse=True)
    jpy_score = strength_matrix["JPY"]
    snapshot["jpy_rank"] = next(
        (index for index, (currency, _) in enumerate(ordered, start=1) if currency == "JPY"),
        None,
    )
    non_jpy_scores = [score for currency, score in currencies if currency != "JPY"]
    if non_jpy_scores:
        snapshot["jpy_gap"] = jpy_score - max(non_jpy_scores)

    if "_" in instrument:
        base_currency = instrument.split("_", 1)[0]
        if base_currency in strength_matrix:
            snapshot["pair_strength"] = strength_matrix[base_currency] - jpy_score

    pairs = trade_pairs or [f"{currency}_JPY" for currency, _ in currencies if currency != "JPY"]
    relative_strengths = []
    for pair in pairs:
        if not pair.endswith("_JPY"):
            continue
        base_currency = pair.split("_", 1)[0]
        if base_currency in strength_matrix:
            relative_strengths.append(strength_matrix[base_currency] - jpy_score)

    snapshot["total_crosses"] = len(relative_strengths)
    if direction in ("BUY", "SELL"):
        sign = 1 if direction == "BUY" else -1
        snapshot["confirming_breadth"] = sum(
            1 for relative_strength in relative_strengths if relative_strength * sign > 0
        )
    return snapshot