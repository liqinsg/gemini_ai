# utils/signal_instrumentation.py
"""
V2 Section 4.1 instrumentation layer: feature -> classify -> log -> observe.

STRICTLY ADDITIVE / OBSERVATION-ONLY MODULE.
--------------------------------------------
Nothing in this file makes or influences a trading decision. Every function
here either:
  (a) classifies an already-computed value into a descriptive bucket for
      logging purposes, or
  (b) appends a JSON-lines record to an observation log.

No function in this module returns a value that is consumed by any
existing filter/skip/continue statement in the live strategy or
risk-management code — callers use these outputs for logging only. This
module has no import dependency on any other project module, so importing
it can never change the behavior of anything that already worked.

Classification bin boundaries below are PROVISIONAL / LOG-ONLY, chosen only
so raw distributions are readable while eyeballing logs during the
observation phase (Section 4.1: "collect feature classifications" / "analyze
outcome distributions by feature category"). They are NOT calibrated
against any historical outcome data yet and must not be treated as
validated thresholds — do not build a hard filter from these bin edges
without first completing the evidence-gathering step described in
Section 4.1.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Log file locations (JSON-lines, append-only). Overridable via env var so
# tests never touch real log files on disk.
# ---------------------------------------------------------------------------
SIGNAL_OBSERVATION_LOG_PATH = os.environ.get(
    "V2_SIGNAL_OBSERVATION_LOG_PATH", "logs/v2_signal_observations.jsonl"
)
TRADE_OUTCOME_LOG_PATH = os.environ.get(
    "V2_TRADE_OUTCOME_LOG_PATH", "logs/v2_trade_outcomes.jsonl"
)

_write_lock = threading.Lock()


def _append_jsonl(path: str, record: dict) -> bool:
    """
    Append one JSON record as a line to `path`. NEVER RAISES — a logging
    failure must never interrupt the trading pipeline. Returns True on
    success, False on failure (failure is printed, not silently swallowed,
    so a broken log path is still visible in the cycle's console output).
    """
    try:
        dirname = os.path.dirname(path)
        if dirname:
            os.makedirs(dirname, exist_ok=True)
        line = json.dumps(record, default=str, sort_keys=True)
        with _write_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return True
    except Exception as e:
        print(f"[V2-INSTRUMENTATION] Failed to write log line to {path}: {e}")
        return False


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Classification enums
# ---------------------------------------------------------------------------

class SlopeClass(str, Enum):
    STRONG = "STRONG"
    WEAK = "WEAK"
    FLAT = "FLAT"
    AGAINST = "AGAINST"
    UNKNOWN = "UNKNOWN"


class VolatilityClass(str, Enum):
    COMPRESSED = "COMPRESSED"
    NORMAL = "NORMAL"
    ELEVATED = "ELEVATED"
    EXTREME = "EXTREME"
    UNKNOWN = "UNKNOWN"


class ProximityClass(str, Enum):
    NEAR = "NEAR"
    MODERATE = "MODERATE"
    CLEAR = "CLEAR"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# A. EMA5 slope classification
# ---------------------------------------------------------------------------

# Provisional bin edges (see module docstring). Expressed as a fraction of
# the earlier EMA value, so it's roughly comparable across instruments/
# timeframes without needing an external normalizer (e.g. ATR) that the
# slope computation itself doesn't have access to at its call site.
SLOPE_STRONG_THRESHOLD_FRAC = 0.001   # > 0.1% move in EMA over the lookback
SLOPE_FLAT_BAND_FRAC = 0.0001         # within +/-0.01% counts as FLAT


def classify_slope(
    ema_now: Optional[float], ema_past: Optional[float], direction: str
) -> Dict[str, Any]:
    """
    Classify the movement of an EMA over a lookback window relative to the
    signal direction. Pure function — does not fetch data and does not
    decide anything; the caller supplies ema_now/ema_past (values already
    computed from candles an existing alignment check already fetched) and
    the `direction` an existing, unmodified alignment vote already produced.

    Args:
        ema_now: EMA value at the most recent candle.
        ema_past: EMA value at some earlier candle (lookback is the
                  caller's concern — this function only compares the two
                  values it's given).
        direction: "BUY" or "SELL".

    Returns:
        dict with keys: label (SlopeClass value), raw_delta,
        raw_delta_frac (fractional change relative to ema_past).
        label is UNKNOWN if either input is None or ema_past == 0.
    """
    if ema_now is None or ema_past is None or ema_past == 0:
        return {"label": SlopeClass.UNKNOWN.value, "raw_delta": None, "raw_delta_frac": None}

    raw_delta = ema_now - ema_past
    raw_delta_frac = raw_delta / abs(ema_past)

    # Orient the delta so positive always means "moving toward the signal
    # direction", regardless of BUY/SELL.
    oriented = raw_delta_frac if direction == "BUY" else -raw_delta_frac

    if oriented <= -SLOPE_FLAT_BAND_FRAC:
        label = SlopeClass.AGAINST
    elif abs(oriented) < SLOPE_FLAT_BAND_FRAC:
        label = SlopeClass.FLAT
    elif oriented < SLOPE_STRONG_THRESHOLD_FRAC:
        label = SlopeClass.WEAK
    else:
        label = SlopeClass.STRONG

    return {"label": label.value, "raw_delta": raw_delta, "raw_delta_frac": raw_delta_frac}


# ---------------------------------------------------------------------------
# B. Volatility (ATR z-score) regime classification
# ---------------------------------------------------------------------------

# Provisional bin edges — deliberately WIDER than the existing +/-1 sizing
# thresholds already used for SL-multiplier selection elsewhere in the
# codebase (see Section 4.1: that existing sizing use is not, by itself,
# justification for these edges as a gate — these are observation bins only).
VOL_COMPRESSED_Z = -1.5
VOL_ELEVATED_Z = 1.0
VOL_EXTREME_Z = 2.0


def classify_volatility(z_score: Optional[float]) -> str:
    """Classify an already-computed ATR z-score into an observation bucket."""
    if z_score is None:
        return VolatilityClass.UNKNOWN.value
    if z_score < VOL_COMPRESSED_Z:
        return VolatilityClass.COMPRESSED.value
    if z_score > VOL_EXTREME_Z:
        return VolatilityClass.EXTREME.value
    if z_score > VOL_ELEVATED_Z:
        return VolatilityClass.ELEVATED.value
    return VolatilityClass.NORMAL.value


# ---------------------------------------------------------------------------
# C. Price-location (daily S/R proximity) classification
# ---------------------------------------------------------------------------

# Provisional bin edges, expressed in ATR-multiples of distance to the
# adverse level.
PROXIMITY_NEAR_ATR = 0.5
PROXIMITY_MODERATE_ATR = 1.5


def classify_price_location(
    entry: Optional[float],
    adverse_level: Optional[float],
    atr: Optional[float] = None,
    pip_size: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Classify how close `entry` is to `adverse_level` (the level that would
    hurt this trade if respected — resistance for a BUY, support for a
    SELL). Pure function — entry/adverse_level/atr are all values the
    caller already has from an existing, unmodified S/R fetch and ATR
    calculation.

    Returns:
        dict with keys: label (ProximityClass value), distance_raw,
        distance_atr (None if atr not supplied), distance_pips (None if
        pip_size not supplied).
    """
    if entry is None or adverse_level is None:
        return {
            "label": ProximityClass.UNKNOWN.value,
            "distance_raw": None,
            "distance_atr": None,
            "distance_pips": None,
        }

    distance_raw = abs(entry - adverse_level)
    distance_atr = (distance_raw / atr) if atr else None
    distance_pips = (distance_raw / pip_size) if pip_size else None

    if distance_atr is not None:
        if distance_atr < PROXIMITY_NEAR_ATR:
            label = ProximityClass.NEAR
        elif distance_atr < PROXIMITY_MODERATE_ATR:
            label = ProximityClass.MODERATE
        else:
            label = ProximityClass.CLEAR
    else:
        label = ProximityClass.UNKNOWN

    return {
        "label": label.value,
        "distance_raw": distance_raw,
        "distance_atr": distance_atr,
        "distance_pips": distance_pips,
    }


def level_cluster_strength(
    all_levels: Optional[List[float]], target_level: Optional[float], tolerance: float
) -> int:
    """
    Count how many fractal support/resistance points (from the raw list a
    S/R scan already produces internally) fall within `tolerance` of
    `target_level`. A higher count suggests `target_level` has been
    repeatedly tested/held (the genuinely dangerous case per Section 4.1's
    price-location discussion); a count of 1 (just the level itself)
    suggests it may simply be the most recent local extremum of an ongoing
    trend (the false-positive case). Pure function.
    """
    if not all_levels or target_level is None:
        return 0
    return sum(1 for lvl in all_levels if abs(lvl - target_level) <= tolerance)


# ---------------------------------------------------------------------------
# Log record builders + writers
# ---------------------------------------------------------------------------

def log_signal_observation(
    *,
    cycle_id: str,
    pair: str,
    stage: str,
    direction: Optional[str] = None,
    strength_score: Optional[float] = None,
    slope: Optional[dict] = None,
    volatility_class: Optional[str] = None,
    z_score: Optional[float] = None,
    atr: Optional[float] = None,
    price_location: Optional[dict] = None,
    extra: Optional[dict] = None,
) -> bool:
    """
    Level-1 "population" log: one record per candidate pair per cycle, at
    whichever stage(s) it reached, regardless of whether it goes on to
    become the executed trade. Never raises into the caller.
    """
    record = {
        "log_type": "signal_observation",
        "timestamp_utc": _utcnow_iso(),
        "cycle_id": cycle_id,
        "pair": pair,
        "stage": stage,
        "direction": direction,
        "strength_score": strength_score,
        "slope": slope,
        "volatility_class": volatility_class,
        "z_score": z_score,
        "atr": atr,
        "price_location": price_location,
        "extra": extra or {},
    }
    return _append_jsonl(SIGNAL_OBSERVATION_LOG_PATH, record)


def log_executed_signal(*, cycle_id: str, pair: str, direction: str, diagnostics: dict) -> bool:
    """
    Level-2 marker: records which (cycle_id, pair) combination was the
    single trade actually executed this cycle, with its full diagnostics
    bundle attached, so it can later be joined against
    `log_trade_outcome()`'s eventual close record.
    """
    record = {
        "log_type": "signal_executed",
        "timestamp_utc": _utcnow_iso(),
        "cycle_id": cycle_id,
        "pair": pair,
        "direction": direction,
        "diagnostics": diagnostics,
    }
    return _append_jsonl(SIGNAL_OBSERVATION_LOG_PATH, record)


def log_trade_outcome(
    *,
    instrument: str,
    direction: int,
    entry_price_0: float,
    r_unit_0: float,
    close_price: Optional[float],
    close_price_source: str,
    realized_r: Optional[float],
    close_reason: str,
    final_state: str,
) -> bool:
    """
    Outcome log: written when the risk-management layer detects a position
    has closed (see utils/risk_integration.py hook). Joined against
    `log_executed_signal()`'s diagnostics by instrument (the current
    architecture only ever has one open cluster per instrument at a time).

    NOTE on close_price: this is a best-effort APPROXIMATION (either the
    latest known price at the moment reconciliation detected an external
    close, or the price the risk manager used for its own evaluation this
    cycle when it triggered a self-close) — NOT the exact OANDA fill price.
    Obtaining the exact fill price/realized PL would require an additional
    OANDA API call (e.g. TradeDetails on the closed trade ID) that is not
    currently made anywhere in this codebase. `close_price_source` always
    documents which approximation was used so this is never ambiguous
    during later analysis.
    """
    record = {
        "log_type": "trade_outcome",
        "timestamp_utc": _utcnow_iso(),
        "instrument": instrument,
        "direction": direction,
        "entry_price_0": entry_price_0,
        "r_unit_0": r_unit_0,
        "close_price": close_price,
        "close_price_source": close_price_source,
        "realized_r": realized_r,
        "close_reason": close_reason,
        "final_state": final_state,
    }
    return _append_jsonl(TRADE_OUTCOME_LOG_PATH, record)
