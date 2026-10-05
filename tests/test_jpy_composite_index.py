"""
Property tests for utils/jpy_composite_index.py — JPY Composite Strength Index V1.2
Layer 1: Golden math regression (fixed input → fixed output)
Layer 2: Config integration (run.env weights → no silent fallbacks)
No hardcoded weights. No silent defaults. No trading logic.
Run: python -m pytest tests/test_jpy_composite_index.py -v -s
"""
from __future__ import annotations
import math
import sys
import os
import re
from typing import Dict
import pytest
from utils.jpy_composite_index import (
    BasketConfig,
    JpyCompositeObservation,
    compute_jpy_composite,
    VALID_WEIGHT_SUM_TOL,
)

# ===========================================================================
# LAYER 2 — Load config STRICTLY: missing = FAIL, no silent defaults
# ===========================================================================
def _load_env_config() -> Dict[str, float | int]:
    """Load from run.env — raise if required weight missing."""
    env_path = os.path.join(os.path.dirname(__file__), "..", "run.env")
    if not os.path.exists(env_path):
        raise FileNotFoundError(f"run.env not found at {env_path}")

    config: Dict[str, float | int] = {}
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = re.sub(r"\s+#.*$", "", val).strip()
            if key.startswith("JPY_WEIGHTED_"):
                try:
                    config[key] = float(val) if "." in val or "e" in val.lower() else int(val)
                except ValueError:
                    config[key] = val

    # Strict: these weights MUST exist
    required_weights = [
        "JPY_WEIGHTED_WEIGHT_EUR", "JPY_WEIGHTED_WEIGHT_USD",
        "JPY_WEIGHTED_WEIGHT_GBP", "JPY_WEIGHTED_WEIGHT_AUD",
        "JPY_WEIGHTED_WEIGHT_CHF",
    ]
    missing = [k for k in required_weights if k not in config]
    if missing:
        raise ValueError(f"Config incomplete — missing: {missing}")

    # Load thresholds (may be used in gate tests, not index formula)
    for k in ["PASS_THRESHOLD", "STRONG_MIN_COUNT", "CCY_MIN_ABS_SCORE"]:
        full_key = f"JPY_WEIGHTED_{k}"
        if full_key in os.environ and full_key not in config:
            val = os.environ[full_key]
            config[full_key] = float(val) if "." in val or "e" in val.lower() else int(val)

    return config

ENV_CFG = _load_env_config()

# Build basket weights — JPY explicitly excluded
RAW_WEIGHTS_ENV = {
    "EUR": ENV_CFG["JPY_WEIGHTED_WEIGHT_EUR"],
    "USD": ENV_CFG["JPY_WEIGHTED_WEIGHT_USD"],
    "GBP": ENV_CFG["JPY_WEIGHTED_WEIGHT_GBP"],
    "AUD": ENV_CFG["JPY_WEIGHTED_WEIGHT_AUD"],
    "CHF": ENV_CFG["JPY_WEIGHTED_WEIGHT_CHF"],
}
PASS_THRESHOLD = ENV_CFG.get("JPY_WEIGHTED_PASS_THRESHOLD", 1.0)
MIN_COUNT = ENV_CFG.get("JPY_WEIGHTED_STRONG_MIN_COUNT", 2)
MIN_ABS_SCORE = ENV_CFG.get("JPY_WEIGHTED_CCY_MIN_ABS_SCORE", 0.1)

# Validate weights before use
for c, w in RAW_WEIGHTS_ENV.items():
    if w <= 0:
        raise ValueError(f"Weight {c}={w} invalid — must be > 0")
_W_SUM = sum(RAW_WEIGHTS_ENV.values())
BASKET_WEIGHTS_REAL = {k: v / _W_SUM for k, v in RAW_WEIGHTS_ENV.items()}

print(f"\n🔧 Config loaded from run.env:")
print(f"   PASS_THRESHOLD={PASS_THRESHOLD}  MIN_COUNT={MIN_COUNT}  MIN_ABS_SCORE={MIN_ABS_SCORE}")
print(f"   Raw weights  : {RAW_WEIGHTS_ENV}")
print(f"   Normalized    : {{{', '.join(f'{c}:{w:.4f}' for c,w in BASKET_WEIGHTS_REAL.items())}}}")
print(f"   Note: JPY_WEIGHTED_WEIGHT_JPY not used — JPY is target, not in basket\n")

# ===========================================================================
# OBSERVATION PRINTER — no fabricated defaults
# ===========================================================================
def _show_obs(obs: JpyCompositeObservation | None, label: str = "") -> None:
    if obs is None:
        print(f"  📊 {label} → NONE / INVALID")
        return
    trend = "↑ BULL" if obs.raw_index > 0 else "↓ BEAR" if obs.raw_index < 0 else "═ NEUTRAL"
    print(f"  📊 {label}")
    print(f"    RawIndex         = {obs.raw_index:+.6f}  {trend}")

    db = getattr(obs, "directional_balance", None)
    print(f"    DirectionalBalance = {db:.2f}" if db is not None else "    DirectionalBalance = N/A")

    mag = getattr(obs, "magnitude", None)
    if mag is not None:
        print(f"    Magnitude         = {mag:.6f}")

    z = getattr(obs, "z_research", None)
    if z is not None:
        print(f"    Z-research        = {z:.4f}")

    print(f"    Rank              = {obs.rank}/{obs.rank_total}")

    bf = getattr(obs, "breadth_fraction", None)
    wb = getattr(obs, "weighted_breadth", None)
    bf_str = f"{bf:.1%}" if bf is not None else "N/A"
    wb_str = f"{wb:.1%}" if wb is not None else "N/A"
    print(f"    BreadthFraction   = {bf_str}  | Weighted = {wb_str}")

    print(f"    BasketMean        = {obs.basket_mean:+.6f}")
    sigma = getattr(obs, "dispersion_sigma_w", None)
    if sigma is not None:
        print(f"    σ_W               = {sigma:.6f}")

# ===========================================================================
# LAYER 1 — GOLDEN: Fixed input → fixed expected output (math regression)
# ===========================================================================
GOLDEN_SCORES = {
    "CHF": +1.4326, "JPY": +0.7527, "GBP": +0.6239,
    "AUD": +0.1593, "USD": -0.0751, "EUR": -1.8002,
}
GOLDEN_WEIGHTS = {c: 0.2 for c in ["CHF", "GBP", "AUD", "USD", "EUR"]}
GOLDEN_EXPECTED = {
    "RawIndex": 0.6846, "BasketMean": 0.0681, "Magnitude": 0.9566,
    "DirectionalBalance": 85.8, "sigma_W": 1.067, "z": 0.64,
    "Rank": 2, "RankTotal": 6,
}

class TestGoldenExample:
    """Fixed reference — if this changes, formula changed intentionally."""
    def setup_method(self):
        self.basket = BasketConfig.build(GOLDEN_WEIGHTS)
        self.obs = compute_jpy_composite(GOLDEN_SCORES, self.basket)
        assert self.obs is not None
        _show_obs(self.obs, "LAYER 1 — Golden Reference (Equal Weights)")

    def test_raw_index(self):
        assert abs(self.obs.raw_index - GOLDEN_EXPECTED["RawIndex"]) < 1e-4
    def test_basket_mean(self):
        assert abs(self.obs.basket_mean - GOLDEN_EXPECTED["BasketMean"]) < 1e-4
    def test_magnitude(self):
        mag = getattr(self.obs, "magnitude", None)
        assert mag is not None and abs(mag - GOLDEN_EXPECTED["Magnitude"]) < 1e-4
    def test_directional_balance(self):
        db = getattr(self.obs, "directional_balance", None)
        assert db is not None and abs(db - GOLDEN_EXPECTED["DirectionalBalance"]) < 0.2
    def test_sigma_w(self):
        sw = getattr(self.obs, "dispersion_sigma_w", None)
        assert sw is not None and abs(sw - GOLDEN_EXPECTED["sigma_W"]) < 1e-3
    def test_z_research(self):
        z = getattr(self.obs, "z_research", None)
        assert z is not None and abs(z - GOLDEN_EXPECTED["z"]) < 0.01
    def test_rank(self):
        assert self.obs.rank == GOLDEN_EXPECTED["Rank"]
        assert self.obs.rank_total == GOLDEN_EXPECTED["RankTotal"]

# ===========================================================================
# LAYER 2 — Config Integration: Real weights from run.env
# ===========================================================================
class TestRealConfig:
    """Formula × actual config values."""
    def setup_method(self):
        self.cfg = BasketConfig.build(BASKET_WEIGHTS_REAL)
        assert "JPY" not in self.cfg.currencies

    def test_weights_loaded_and_normalized(self):
        assert abs(sum(BASKET_WEIGHTS_REAL.values()) - 1.0) < 1e-9
        assert all(c in self.cfg.currencies for c in ["EUR", "USD", "GBP", "AUD", "CHF"])

    def test_golden_scores_actual_weights(self):
        obs = compute_jpy_composite(GOLDEN_SCORES, self.cfg)
        assert obs is not None
        _show_obs(obs, "LAYER 2 — Golden Scores × Actual Weights")
        S_jpy = GOLDEN_SCORES["JPY"]
        print("    Contributions:")
        for c in self.cfg.currencies:
            gap = S_jpy - GOLDEN_SCORES[c]
            contrib = BASKET_WEIGHTS_REAL[c] * gap
            flag = "✅" if contrib > 0 else "❌" if contrib < 0 else "➖"
            print(f"      {flag} {c}: {contrib:+9.6f}")

    def test_jpy_strong_all_positive(self):
        scores = {"JPY": 2.0, "EUR": 0.5, "USD": 0.4, "GBP": 0.3, "AUD": 0.2, "CHF": 0.1}
        obs = compute_jpy_composite(scores, self.cfg)
        assert obs is not None
        _show_obs(obs, "LAYER 2 — JPY Strongest All ↑")
        assert obs.raw_index > 0
        db = getattr(obs, "directional_balance", None)
        assert db is not None and db > 90

    def test_jpy_weak_all_negative(self):
        scores = {"JPY": -2.0, "EUR": -0.5, "USD": -0.4, "GBP": -0.3, "AUD": -0.2, "CHF": -0.1}
        obs = compute_jpy_composite(scores, self.cfg)
        assert obs is not None
        _show_obs(obs, "LAYER 2 — JPY Weakest All ↓")
        assert obs.raw_index < 0
        db = getattr(obs, "directional_balance", None)
        assert db is not None and db < 10

    def test_eur_dominance(self):
        """Highest weight currency should dominate index."""
        scores = {"JPY": 0.0, "EUR": -2.0, "USD": 0.0, "GBP": 0.0, "AUD": 0.0, "CHF": 0.0}
        obs = compute_jpy_composite(scores, self.cfg)
        assert obs is not None
        _show_obs(obs, "LAYER 2 — EUR Dominant (w=largest)")
        expected = BASKET_WEIGHTS_REAL["EUR"] * 2.0
        assert obs.raw_index == pytest.approx(expected, rel=1e-9)

    def test_gate_threshold_reference_only(self):
        """Show old gate equivalent — thresholds NOT used in index formula."""
        scores_pass = {"JPY": 1.0, "EUR": -0.6, "USD": -0.5, "GBP": 0.0, "AUD": 0.0, "CHF": 0.0}
        obs = compute_jpy_composite(scores_pass, self.cfg)
        assert obs is not None
        _show_obs(obs, f"Gate Reference — PASS scenario (≥{PASS_THRESHOLD})")
        eur_gate = RAW_WEIGHTS_ENV["EUR"] * abs(1.0 - (-0.6))
        usd_gate = RAW_WEIGHTS_ENV["USD"] * abs(1.0 - (-0.5))
        gate_total = eur_gate + usd_gate
        print(f"    Old Gate total = {gate_total:.2f}  vs PASS_THRESHOLD = {PASS_THRESHOLD}")
        print(f"    NOTE: Thresholds belong to Gate, NOT Composite Index formula.\n")

# ===========================================================================
# Mathematical Properties — hold regardless of config
# ===========================================================================
class TestSignPreservation:
    def test_mixed_signs(self):
        cfg = BasketConfig.build(BASKET_WEIGHTS_REAL)
        scores = {"JPY": 0.0, "EUR": 0.8, "USD": -0.6, "GBP": 0.3, "AUD": -0.2, "CHF": 0.1}
        obs = compute_jpy_composite(scores, cfg)
        assert obs is not None
        S_jpy = scores["JPY"]
        for c in cfg.currencies:
            gap = S_jpy - scores[c]
            contrib = BASKET_WEIGHTS_REAL[c] * gap
            assert math.copysign(1, contrib) == math.copysign(1, gap), f"{c} sign flipped"

class TestTranslationInvariance:
    @pytest.mark.parametrize("K", [-5.0, 3.0])
    def test_index_invariant(self, K):
        cfg = BasketConfig.build(BASKET_WEIGHTS_REAL)
        base = compute_jpy_composite(GOLDEN_SCORES, cfg)
        shifted = {c: v + K for c, v in GOLDEN_SCORES.items()}
        shifted_obs = compute_jpy_composite(shifted, cfg)
        assert base is not None and shifted_obs is not None
        assert abs(shifted_obs.raw_index - base.raw_index) < 1e-10

class TestScaleInvariance:
    @pytest.mark.parametrize("K", [0.5, 2.0])
    def test_scales_linearly(self, K):
        cfg = BasketConfig.build(BASKET_WEIGHTS_REAL)
        base = compute_jpy_composite(GOLDEN_SCORES, cfg)
        scaled = {c: v * K for c, v in GOLDEN_SCORES.items()}
        scaled_obs = compute_jpy_composite(scaled, cfg)
        assert base is not None and scaled_obs is not None
        assert scaled_obs.raw_index == pytest.approx(base.raw_index * K, rel=1e-10)
        # FIX: Explicit None check, not truthy
        db_base = getattr(base, "directional_balance", None)
        db_scaled = getattr(scaled_obs, "directional_balance", None)
        if db_base is not None and db_scaled is not None:
            assert abs(db_scaled - db_base) < 1e-10

# ===========================================================================
# Input Validation
# ===========================================================================
class TestBasketValidation:
    def test_rejects_jpy_in_basket(self):
        with pytest.raises(ValueError, match="JPY must not appear"):
            BasketConfig.build({"JPY": 0.5, "USD": 0.5})
    def test_rejects_zero_weight(self):
        with pytest.raises(ValueError, match="must be > 0"):
            BasketConfig.build({"USD": 0.0, "EUR": 1.0})
    def test_rejects_negative_weight(self):
        with pytest.raises(ValueError, match="must be > 0"):
            BasketConfig.build({"USD": 1.2, "EUR": -0.2})

class TestInvalidation:
    def setup_method(self):
        self.cfg = BasketConfig.build(BASKET_WEIGHTS_REAL)
        self.full = {"JPY": 0.5, "EUR": 0.2, "USD": -0.1, "GBP": 0.0, "AUD": 0.1, "CHF": -0.3}
    def test_missing_jpy_returns_none(self):
        s = {k: v for k, v in self.full.items() if k != "JPY"}
        assert compute_jpy_composite(s, self.cfg) is None
    def test_nan_returns_none(self):
        s = dict(self.full); s["EUR"] = float("nan")
        assert compute_jpy_composite(s, self.cfg) is None

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-s"]))