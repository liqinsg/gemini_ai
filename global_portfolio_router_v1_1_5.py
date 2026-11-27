"""
Global Portfolio Router v1.1.5

Companion implementation for docs/Global_Portfolio_Router_v1_1_5.md.
Built on v1.1.4. Every gap listed in v1.1.5 §8 is now implemented behind an
independent switch, so each safeguard can be validated one at a time.

SWITCHES (class SW)
  -- inherited from v1.1.4 --
  CLOSED_BARS_ONLY     P2  drop OANDA's in-progress candle
  USE_REAL_POS_DATA    P1  real ATR + returns for open positions (never drops a position)
  REQUIRE_SL           P3  every order must carry a stop loss
  STRICT_PROMOTION     P4  3 consecutive windows, Sharpe AND drawdown, additive margin
  RISK_UNIT_EXPOSURE   P5  exposure in risk units (1 unit = one fixed-risk trade)
  PARALLEL_SNAPSHOT    P6  parallel fetch
  MTF_STRENGTH         P7  per-pair timeframe weights in momentum
  TRUE_ATR             P7  true-range ATR instead of std(diff)
  TIME_BASED_EMA       P7  strength EMA by elapsed time  [v1.1.5: DEFAULT ON]
  LIVE_COST            P7  cost_score from live OANDA spread / ATR
  SHADOW_OWN_POSITIONS P7  shadow routers track their own simulated positions
  PER_PAIR_BAR_CLOSE   P8  only tick when a tracked timeframe just closed
  PAIRS_FROM_FILE      P9  read pairs from mc_data.yml
  DRY_RUN              orders are only printed (ON by default)

  -- new in v1.1.5 (§8 gaps) --
  GROSS_EXPOSURE_CAP   P10 net AND gross exposure caps (max_ccy_gross_units)
  UNIVERSAL_COVERAGE   P11 coverage gate on BOTH snapshot paths, by pair AND currency
  H1_CORR_GUARD        P12 correlation on a uniform H1 series; missing data blocks the candidate
  H1_COST              P13 cost normalized by ATR_H1, regardless of the pair's momentum TF
  WLS_SLOPE            P14 time-decayed weighted least squares slope
  SHADOW_REAL_EXIT     P15 shadow positions exit on real rules, not a fixed tick count
  SHADOW_REJECTED      P16 rejected candidates logged to a "shadow rejected" book

ROLLOUT (practice account only until every step is clean):
  Enable one switch at a time; watch the [FALLBACK] ratio and the [Snapshot] coverage
  line each tick. Only raise DRY_RUN=False on the PRACTICE environment.

Credentials come from env vars: OANDA_TOKEN, OANDA_ACCOUNT_ID, OANDA_ENV (default "practice").
Orders on a non-practice environment additionally require ALLOW_LIVE_ORDERS=YES.
"""
from __future__ import annotations

import dataclasses
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

try:
    import oandapyV20
    import oandapyV20.endpoints.instruments as instruments
    import oandapyV20.endpoints.orders as orders_ep
    import oandapyV20.endpoints.positions as positions_ep
    import oandapyV20.endpoints.pricing as pricing_ep
except ImportError:  # allows offline self-test without the SDK
    oandapyV20 = None


class SW:
    CLOSED_BARS_ONLY = False
    USE_REAL_POS_DATA = False
    REQUIRE_SL = False
    STRICT_PROMOTION = False
    RISK_UNIT_EXPOSURE = False
    PARALLEL_SNAPSHOT = False
    MTF_STRENGTH = False
    TRUE_ATR = False
    TIME_BASED_EMA = True       # P7: v1.1.5 target default ON (was OFF in v1.1.4)
    LIVE_COST = False
    SHADOW_OWN_POSITIONS = False
    PER_PAIR_BAR_CLOSE = False  # P8: only trade pairs whose primary TF just closed
    PAIRS_FROM_FILE = True      # P9: read pairs from mc_data.yml instead of the legacy list
    DRY_RUN = True
    # --- new in v1.1.5 ---
    GROSS_EXPOSURE_CAP = False  # P10
    UNIVERSAL_COVERAGE = False  # P11
    H1_CORR_GUARD = False       # P12
    H1_COST = False             # P13
    WLS_SLOPE = False           # P14
    SHADOW_REAL_EXIT = False    # P15
    SHADOW_REJECTED = False     # P16


# ---- tunables ----
MIN_COVERAGE = 24            # P6/P11: min pairs (of 28) needed to Z-score
MIN_CCY_PAIRS = 2            # P11: min pairs per currency to trust the cross-section (assumption, tune)
SNAPSHOT_TIMEOUT = 5.0       # P6 parallel timeout (legacy sequential keeps 2.0)
HALF_LIFE_SEC = 6 * 3600     # P7: strength half-life (6 H1 ticks)
COST_CAP = 0.25              # P7: spread/ATR at which cost_score hits 0 (assumption, tune)
SL_ATR_MULT = 2.0            # P3: stop distance = SL_ATR_MULT * ATR
SHADOW_HOLD_STEPS = 12       # P7: simulated shadow position lifetime (ticks, legacy rule)
SHADOW_MAX_STEPS = 48        # P15: hard horizon backstop for a shadow position under real exits
SHADOW_EXIT_SL_ATR = SL_ATR_MULT  # P15: shadow stop distance in ATR
SHADOW_EXIT_TP_ATR = 2.0     # P15: shadow take-profit distance in ATR
MIN_TRADES_PER_WINDOW = 20   # P4: ignore windows with fewer trades
BAR_CLOSE_TOLERANCE_SEC = 120  # P8: how long after a bar close we still consider it "just closed"
WLS_HALF_LIFE_BARS = 60      # P14: slope weighting half-life, in bars
REJECT_HORIZON_BARS = 24     # P16: bars after which a rejected candidate's outcome is scored
TF_TO_OANDA = {"M15": "M15", "M30": "M30", "H1": "H1", "H4": "H4", "D1": "D"}

PAIRS_DATA_PATH = os.environ.get("PAIRS_DATA_PATH", str(Path(__file__).resolve().parent / "mc_data.yml"))

# P9 fallback only (used when PAIRS_FROM_FILE=False or yaml missing).
_LEGACY_UNIVERSE = [
    "EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD",
    "NZD_USD", "USD_CAD", "EUR_GBP", "EUR_JPY", "EUR_AUD",
    "EUR_CHF", "EUR_CAD", "EUR_NZD", "GBP_CAD", "GBP_NZD",
    "GBP_JPY", "GBP_AUD", "GBP_CHF", "AUD_JPY", "AUD_CHF",
    "NZD_JPY", "NZD_CHF", "NZD_CAD", "AUD_CAD", "AUD_NZD",
    "CAD_JPY", "CAD_CHF", "CHF_JPY",
]
_OANDA_FX28 = frozenset(_LEGACY_UNIVERSE)   # the only orientations OANDA lists
_CURRENCIES = frozenset(c for p in _OANDA_FX28 for c in p.split("_"))
_UNIVERSE_CACHE: Optional[List[str]] = None


def _normalize_pair(sym) -> Optional[str]:
    """'EURUSD' / 'EUR/USD' / 'eur_usd' -> 'EUR_USD'; None if not a 6-letter pair."""
    s = str(sym).strip().upper().replace("/", "").replace("_", "")
    return f"{s[:3]}_{s[3:]}" if len(s) == 6 and s.isalpha() else None


def load_universe(path: Optional[str] = None) -> List[str]:
    """Read `pairs:` from mc_data.yml. Dedupes (order kept), rejects non-OANDA
    orientations, raises on missing file / empty list / fewer than MIN_COVERAGE valid pairs.
    No silent fallback."""
    if yaml is None:
        raise RuntimeError("pip install pyyaml (needed to load pairs from file)")
    path = path or PAIRS_DATA_PATH
    with open(path) as f:
        cfg = yaml.safe_load(f)
    raw = cfg.get("pairs") if isinstance(cfg, dict) else None
    if not raw:
        raise ValueError(f"{path}: missing or empty 'pairs' list")
    out, seen, dupes, rejected = [], set(), [], []
    for sym in raw:
        inst = _normalize_pair(sym)
        if inst is None or inst not in _OANDA_FX28:
            rejected.append(sym)
            continue
        if inst in seen:
            dupes.append(inst)
            continue
        seen.add(inst)
        out.append(inst)
    if dupes:
        print(f"[Universe] dropped {len(dupes)} duplicate(s): {sorted(set(dupes))}")
    if rejected:
        print(f"[Universe] REJECTED (not an OANDA-listed pair): {rejected}")
    missing = sorted(_OANDA_FX28 - seen)
    if missing:
        print(f"[Universe] WARN missing from the 28: {missing} (skews currency strength)")
    if len(out) < MIN_COVERAGE:
        raise ValueError(f"{path}: only {len(out)} valid pairs < MIN_COVERAGE={MIN_COVERAGE}")
    print(f"[Universe] loaded {len(out)} pairs from {path}")
    return out


def get_universe() -> List[str]:
    global _UNIVERSE_CACHE
    if not SW.PAIRS_FROM_FILE:
        return _LEGACY_UNIVERSE
    if _UNIVERSE_CACHE is None:
        _UNIVERSE_CACHE = load_universe()
    return _UNIVERSE_CACHE

_LOW = {"primary": "H1", "confirm": ["H4", "D1"], "weights": {"M30": 0.2, "H1": 0.3, "H4": 0.5}}
_HIGH = {"primary": "H4", "confirm": ["D1", "H1"], "weights": {"H1": 0.2, "H4": 0.5, "D1": 0.3}}
_COMM = {"primary": "H1", "confirm": ["H4", "D1"], "weights": {"H1": 0.25, "H4": 0.45, "D1": 0.30}}
_JPY = {"primary": "H1", "confirm": ["H4", "D1"], "weights": {"H1": 0.3, "H4": 0.4, "D1": 0.3}}
PAIR_TIMEFRAME_CONFIG = {
    "EUR/USD": _LOW, "USD/CHF": _LOW, "EUR/GBP": _LOW,
    "GBP/JPY": _HIGH, "GBP/AUD": _HIGH, "EUR/JPY": _HIGH,
    "AUD/USD": _COMM, "NZD/USD": _COMM, "USD/CAD": _COMM,
    "USD/JPY": _JPY, "AUD/JPY": _JPY, "NZD/JPY": _JPY,
    "DEFAULT": {"primary": "H1", "confirm": ["H4"], "weights": {"H1": 0.4, "H4": 0.6}},
}


def get_pair_config(pair: str) -> dict:
    return PAIR_TIMEFRAME_CONFIG.get(pair, PAIR_TIMEFRAME_CONFIG["DEFAULT"])


# ================= Data structures =================
@dataclass
class SignalCandidate:
    pair: str                   # "GBP/AUD"
    side: str                   # LONG / SHORT
    raw_score: float            # [0,1]
    space_score: float          # [0,1]
    cost_score: float           # [0,1] higher = cheaper
    timeframe: str              # timeframe that generated the signal
    returns_series: np.ndarray
    atr: float = 0.0
    h1_returns: Optional[np.ndarray] = None   # P12: uniform H1 series for the correlation guard


@dataclass
class Position:
    pair: str
    side: str
    size: float                 # units
    atr: float
    returns_series: np.ndarray
    risk_units: float = 1.0     # P5: assumes fixed-risk sizing (1 trade = 1 unit)
    age: int = 0                # shadow bookkeeping
    h1_returns: Optional[np.ndarray] = None   # P12: uniform H1 series
    entry_price: float = 0.0    # P15: shadow entry
    sl_dist: float = 0.0        # P15: shadow stop distance (price units)
    tp_dist: float = 0.0        # P15: shadow take-profit distance (price units)
    exit_reason: Optional[str] = None         # P15: "stop" / "target" / "horizon" / "reverse"


@dataclass
class RouterParams:
    max_ccy_exposure: float = 1.0   # legacy exposure cap
    max_ccy_units: float = 2.0      # P5 cap in risk units (1.0 forbids any two trades sharing a currency)
    max_ccy_gross_units: float = 3.0  # P10 gross cap (net can hide offsetting currency risk)
    max_corr: float = 0.70
    min_alignment: float = 0.10
    w_raw: float = 0.40
    w_align: float = 0.35
    w_space: float = 0.15
    w_cost: float = 0.10            # doc recommends >= 0.15 intraday; rebalance other weights if raised


@dataclass
class Snapshot:
    momentums: Dict[str, float]
    returns: Dict[str, np.ndarray]
    atrs: Dict[str, float]
    h1_returns: Dict[str, np.ndarray] = field(default_factory=dict)  # P12
    h1_atrs: Dict[str, float] = field(default_factory=dict)          # P13
    prices: Dict[str, float] = field(default_factory=dict)           # P15/P16


# ================= Core router =================
class GlobalPortfolioRouter:
    def __init__(self, params: RouterParams):
        self.p = params
        self.prev_strength: Dict[str, float] = {}
        self.alpha = 0.15           # legacy per-call EMA (~4.3-call half-life)
        self._last_ts: Optional[float] = None
        self._warned_corr = False

    def _postprocess_strength(self, raw: Dict[str, float]) -> Dict[str, float]:
        """Normalize raw per-currency strengths (z-score) then EMA-smooth them.

        Single source of truth for the strength "space": BOTH the computed path
        (from pair momentums) AND an injected path (e.g. the runner's
        ``_global_scores``) flow through here, so a shadow comparison stays
        apples-to-apples (v1.1.5 3a bridge)."""
        vals = np.array(list(raw.values()))
        mu, sigma = vals.mean(), vals.std() + 1e-9
        z = {k: (v - mu) / sigma for k, v in raw.items()}

        now = time.time()
        if SW.TIME_BASED_EMA and self._last_ts is not None:
            alpha = 1 - 0.5 ** (max(now - self._last_ts, 0.0) / HALF_LIFE_SEC)
        else:
            alpha = self.alpha
        self._last_ts = now

        out = {}
        for ccy, v in z.items():
            prev = self.prev_strength.get(ccy, v)
            out[ccy] = alpha * v + (1 - alpha) * prev
        self.prev_strength = out
        return out

    def compute_currency_strength(self, pair_momentums: Dict[str, float]) -> Dict[str, float]:
        buckets: Dict[str, List[float]] = {}
        for pair, mom in pair_momentums.items():
            base, quote = pair.split("/")
            buckets.setdefault(base, []).append(mom)
            buckets.setdefault(quote, []).append(-mom)
        raw = {ccy: float(np.mean(v)) for ccy, v in buckets.items()}
        return self._postprocess_strength(raw)

    def _net_exposure(self, positions: List[Position]) -> Dict[str, float]:
        exp: Dict[str, float] = {}
        for pos in positions:
            base, quote = pos.pair.split("/")
            d = 1 if pos.side == "LONG" else -1
            w = pos.risk_units if SW.RISK_UNIT_EXPOSURE else pos.size * pos.atr
            exp[base] = exp.get(base, 0) + d * w
            exp[quote] = exp.get(quote, 0) - d * w
        return exp

    # kept as the public name used elsewhere / by callers
    _get_weighted_exposure = _net_exposure

    def _gross_exposure(self, positions: List[Position]) -> Dict[str, float]:
        """P10: sum of |exposure| per currency, so an offsetting book (e.g. long EUR/USD
        and short USD/CHF) still shows the real USD risk."""
        gross: Dict[str, float] = {}
        for pos in positions:
            base, quote = pos.pair.split("/")
            w = abs(pos.risk_units if SW.RISK_UNIT_EXPOSURE else pos.size * pos.atr)
            gross[base] = gross.get(base, 0) + w
            gross[quote] = gross.get(quote, 0) + w
        return gross

    def _is_correlated(self, cand: SignalCandidate, positions: List[Position]) -> bool:
        for pos in positions:
            if SW.H1_CORR_GUARD:
                # P12: compare a uniform H1 series; missing/short data blocks the candidate (fail-closed)
                a, b = cand.h1_returns, pos.h1_returns
                if a is None or b is None or len(a) == 0 or len(b) == 0:
                    return True
                n = min(len(a), len(b))
                if n < 20:
                    return True
                a, b = a[-n:], b[-n:]
            elif SW.USE_REAL_POS_DATA:
                a, b = cand.returns_series, pos.returns_series
                n = min(len(a), len(b))
                if n < 20:
                    continue
                a, b = a[-n:], b[-n:]
            else:
                a, b = cand.returns_series, pos.returns_series
                if len(a) != len(b) or len(a) == 0:
                    if len(b) == 0 and not self._warned_corr:
                        print("[WARN] position returns empty -> correlation guard INACTIVE "
                              "(enable USE_REAL_POS_DATA or H1_CORR_GUARD)")
                        self._warned_corr = True
                    continue
            if a.std() == 0 or b.std() == 0:
                continue
            corr = np.corrcoef(a, b)[0, 1]
            if np.isnan(corr):
                continue
            dc = 1 if cand.side == "LONG" else -1
            dp = 1 if pos.side == "LONG" else -1
            if corr * dc * dp > self.p.max_corr:
                return True
        return False

    def _timeframe_penalty(self, cand: SignalCandidate) -> float:
        cfg = get_pair_config(cand.pair)
        if cand.timeframe == cfg["primary"]:
            return 1.0
        if cand.timeframe in cfg["confirm"]:
            return 0.85
        return 0.5

    # Top-1 only is deliberate (v1.1.5 §8 "Known limits"): one position per tick to limit risk.
    def select_best_trade(self, candidates, pair_momentums, open_positions,
                          ccy_strength: Optional[Dict[str, float]] = None):
        # v1.1.5 3a: an optional externally-supplied strength source (e.g. the
        # runner's ``_global_scores``) is fed through the SAME normalization as
        # the computed path, avoiding a second "strength truth".
        if ccy_strength is not None:
            ccy_strength = self._postprocess_strength(dict(ccy_strength))
        else:
            ccy_strength = self.compute_currency_strength(pair_momentums)
        exposure = self._net_exposure(open_positions)
        gross = self._gross_exposure(open_positions) if SW.GROSS_EXPOSURE_CAP else {}
        ranked, rejected = [], []

        for c in candidates:
            base, quote = c.pair.split("/")
            d = 1 if c.side == "LONG" else -1
            alignment = d * (ccy_strength.get(base, 0) - ccy_strength.get(quote, 0))
            if alignment < self.p.min_alignment:
                rejected.append((c.pair, c.side, f"align={alignment:.2f}"))
                continue
            adj_space = c.space_score * self._timeframe_penalty(c)
            final = (self.p.w_raw * c.raw_score
                     + self.p.w_align * np.tanh(alignment)
                     + self.p.w_space * adj_space
                     + self.p.w_cost * c.cost_score)
            ranked.append((final, alignment, c))

        ranked.sort(key=lambda x: x[0], reverse=True)

        if SW.RISK_UNIT_EXPOSURE:
            cap = self.p.max_ccy_units
        else:
            cap = self.p.max_ccy_exposure

        for final, alignment, c in ranked:
            base, quote = c.pair.split("/")
            d = 1 if c.side == "LONG" else -1
            w = 1.0 if SW.RISK_UNIT_EXPOSURE else float(c.returns_series.std())
            if abs(exposure.get(base, 0) + d * w) > cap or abs(exposure.get(quote, 0) - d * w) > cap:
                rejected.append((c.pair, c.side, "ccy_exposure"))
                continue
            if SW.GROSS_EXPOSURE_CAP and SW.RISK_UNIT_EXPOSURE:
                # P10: gross cap treats a currency's total |risk| regardless of sign
                gcap = self.p.max_ccy_gross_units
                if gross.get(base, 0) + w > gcap or gross.get(quote, 0) + w > gcap:
                    rejected.append((c.pair, c.side, "ccy_gross_exposure"))
                    continue
            if self._is_correlated(c, open_positions):
                rejected.append((c.pair, c.side, f"corr>{self.p.max_corr}"))
                continue
            return {
                "pair": c.pair, "side": c.side,
                "final_score": round(float(final), 4),
                "alignment": round(float(alignment), 4),
                "timeframe": c.timeframe,
                "primary_tf": get_pair_config(c.pair)["primary"],
                "strength": {base: round(ccy_strength.get(base, 0), 3),
                             quote: round(ccy_strength.get(quote, 0), 3)},
                "rejected": rejected,
            }
        return {"pair": None, "rejected": rejected}


# ================= Shadow + Meta =================
class ShadowEngine:
    """Runs N parameter sets on the same snapshot. Never places orders."""

    def __init__(self, param_sets: List[RouterParams]):
        self.routers = [GlobalPortfolioRouter(p) for p in param_sets]
        self.histories = [deque(maxlen=500) for _ in param_sets]
        self.sim_positions: List[List[Position]] = [[] for _ in param_sets]

    def _advance(self, sim: List[Position], prices: Optional[Dict[str, float]]) -> List[Position]:
        """P15: with SHADOW_REAL_EXIT a shadow position closes on a real rule (stop /
        take-profit / horizon) instead of the legacy fixed tick count."""
        out = []
        for p in sim:
            p.age += 1
            if SW.SHADOW_REAL_EXIT:
                px = (prices or {}).get(p.pair)
                if px is not None and p.entry_price > 0:
                    d = 1 if p.side == "LONG" else -1
                    move = (px - p.entry_price) / p.entry_price
                    if move * d <= -p.sl_dist / p.entry_price:
                        p.exit_reason = "stop"
                        continue
                    if move * d >= p.tp_dist / p.entry_price:
                        p.exit_reason = "target"
                        continue
                if p.age >= SHADOW_MAX_STEPS:
                    p.exit_reason = "horizon"
                    continue
                out.append(p)
            elif p.age < SHADOW_HOLD_STEPS:
                out.append(p)
        return out

    def _open(self, i: int, c: SignalCandidate, prices: Optional[Dict[str, float]]):
        px = (prices or {}).get(c.pair)
        if SW.SHADOW_REAL_EXIT and px is None:
            return  # a real exit needs an entry price
        atr = c.atr or 0.0
        self.sim_positions[i].append(Position(
            c.pair, c.side, 1.0, c.atr, c.returns_series,
            h1_returns=c.h1_returns,
            entry_price=px or 0.0,
            sl_dist=SHADOW_EXIT_SL_ATR * atr,
            tp_dist=SHADOW_EXIT_TP_ATR * atr))

    def step(self, candidates, pair_momentums, live_positions, prices: Optional[Dict[str, float]] = None):
        results = []
        for i, (router, hist) in enumerate(zip(self.routers, self.histories)):
            if SW.SHADOW_OWN_POSITIONS:
                sim = self._advance(self.sim_positions[i], prices)
                self.sim_positions[i] = sim
                positions = sim
            else:
                positions = live_positions
            r = router.select_best_trade(candidates, pair_momentums, positions)
            hist.append(r)
            results.append(r)
            if SW.SHADOW_OWN_POSITIONS and r.get("pair"):
                # P15: a fresh signal against an existing shadow position closes it (reverse exit)
                if SW.SHADOW_REAL_EXIT:
                    self.sim_positions[i] = [
                        p for p in self.sim_positions[i]
                        if not (p.pair == r["pair"] and p.side != r["side"])]
                c = next((c for c in candidates if c.pair == r["pair"] and c.side == r["side"]), None)
                if c is not None:
                    self._open(i, c, prices)
        return results


class ShadowRejectedBook:
    """P16: rejected-candidate outcome tracking (v1.1.5 §8).
    Records a hypothetical entry, exit rule, horizon and cost for every rejected
    candidate, then scores its hypothetical win/loss once `horizon_bars` elapsed."""

    def __init__(self, horizon_bars: int = REJECT_HORIZON_BARS, exit_rule: str = "fixed_horizon",
                 bar_seconds: float = 3600.0):
        self.horizon_bars = horizon_bars
        self.exit_rule = exit_rule
        self.bar_seconds = bar_seconds
        self.rows: List[dict] = []

    def record(self, pair: str, side: str, entry_price: float, ts: float,
               spread: Optional[float] = None, reason: str = ""):
        self.rows.append({
            "pair": pair, "side": side, "entry_price": entry_price,
            "entry_ts": ts, "horizon_bars": self.horizon_bars, "exit_rule": self.exit_rule,
            "spread": spread, "reason": reason,
            "exit_price": None, "exit_reason": None, "outcome": None,
        })

    def evaluate(self, price_lookup, now_ts: Optional[float] = None) -> Dict[str, float]:
        now_ts = time.time() if now_ts is None else now_ts
        for row in self.rows:
            if row["outcome"] is not None:
                continue
            bars = (now_ts - row["entry_ts"]) / max(self.bar_seconds, 1e-9)
            if bars < row["horizon_bars"]:
                continue
            px = price_lookup(row["pair"])
            if px is None:
                continue
            d = 1 if row["side"] == "LONG" else -1
            row["exit_price"] = px
            row["exit_reason"] = "horizon"
            row["outcome"] = d * (px - row["entry_price"]) / row["entry_price"]
        return self.summary()

    def summary(self) -> Dict[str, float]:
        done = [r for r in self.rows if r["outcome"] is not None]
        wins = [r for r in done if r["outcome"] > 0]
        net = sum(r["outcome"] for r in done)
        cost = sum((r["spread"] or 0.0) / max(r["entry_price"], 1e-9) for r in done)
        return {"n_total": len(self.rows), "n_closed": len(done), "wins": len(wins),
                "win_rate": (len(wins) / len(done) if done else 0.0),
                "net_return": net, "net_of_cost": net - cost}


class MetaController:
    def __init__(self, live_router: GlobalPortfolioRouter, shadow: ShadowEngine,
                 required_wins: int = 3, sharpe_margin: float = 0.10):
        self.live = live_router
        self.shadow = shadow
        self.required_wins = required_wins
        self.sharpe_margin = sharpe_margin
        self.wins = [0] * len(shadow.routers)

    # DEPRECATED: old single-metric *1.1 promotion. Fails loudly if old code still calls it.
    def evaluate_and_maybe_promote(self, live_metric: float, shadow_metrics: List[float]) -> bool:
        raise RuntimeError("evaluate_and_maybe_promote removed (*1.1 rule breaks on negative metrics). "
                           "Use MetaController.evaluate_window() with SW.STRICT_PROMOTION=True")

    # P4: metrics dict = {"sharpe": float, "max_dd": float (positive magnitude), "n_trades": int}
    def evaluate_window(self, live: Dict[str, float], shadows: List[Dict[str, float]],
                        field_name: Optional[str] = None) -> Optional[int]:
        """Call once per window. Promotes only after `required_wins` consecutive winning windows.
        field_name names the single RouterParams field eligible for promotion."""
        if not SW.STRICT_PROMOTION:
            self.wins = [0] * len(self.wins)
            return None
        if len(shadows) != len(self.shadow.routers):
            raise ValueError("shadow metrics count must match configured shadow routers")
        if field_name is None:
            raise ValueError("field_name is required; promote one parameter at a time")
        if field_name not in {f.name for f in dataclasses.fields(RouterParams)}:
            raise ValueError(f"unknown RouterParams field: {field_name}")
        for i, s in enumerate(shadows):
            enough = s.get("n_trades", 0) >= MIN_TRADES_PER_WINDOW and live.get("n_trades", 0) >= MIN_TRADES_PER_WINDOW
            win = (enough
                   and s["sharpe"] > live["sharpe"] + self.sharpe_margin
                   and s["max_dd"] < live["max_dd"])
            self.wins[i] = self.wins[i] + 1 if win else 0
        eligible = [i for i, w in enumerate(self.wins) if w >= self.required_wins]
        if not eligible:
            return None
        best = max(eligible, key=lambda i: shadows[i]["sharpe"])
        src = self.shadow.routers[best].p
        self.live.p = dataclasses.replace(self.live.p, **{field_name: getattr(src, field_name)})
        print(f"[Meta] promoted shadow {best} field={field_name} -> {self.live.p}")
        self.wins = [0] * len(self.wins)
        return best


# ================= OANDA layer =================
_client = None


def get_client():
    global _client
    if oandapyV20 is None:
        raise RuntimeError("pip install oandapyV20")
    if _client is None:
        token = os.environ["OANDA_TOKEN"]
        _client = oandapyV20.API(access_token=token, environment=os.environ.get("OANDA_ENV", "practice"))
    return _client


def account_id() -> str:
    return os.environ["OANDA_ACCOUNT_ID"]


def fetch_candles(inst: str, tf: str, count: int = 100) -> Optional[Dict[str, np.ndarray]]:
    """Returns {'h','l','c'} arrays or None on failure. P2: closed bars only."""
    params = {"granularity": TF_TO_OANDA.get(tf, tf), "count": count + 1, "price": "M"}
    try:
        resp = get_client().request(instruments.InstrumentsCandles(instrument=inst, params=params))
    except Exception as e:
        print(f"[Snapshot] {inst} {tf} failed: {e}")
        return None
    cs = resp.get("candles", [])
    if SW.CLOSED_BARS_ONLY:
        cs = [c for c in cs if c.get("complete")]
    if not cs:
        return None
    return {k: np.array([float(c["mid"][k]) for c in cs]) for k in ("h", "l", "c")}


def compute_atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = 14) -> float:
    if SW.TRUE_ATR:
        pc = c[:-1]
        tr = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - pc), np.abs(l[1:] - pc)])
        return float(tr[-n:].mean()) + 1e-12
    return float(np.std(np.diff(c))) + 1e-9      # legacy proxy


def _wls_slope(y: np.ndarray, half_life_bars: int = WLS_HALF_LIFE_BARS) -> float:
    """P14: time-decayed weighted least squares slope (recent bars weigh more)."""
    n = len(y)
    if n < 2:
        return 0.0
    x = np.arange(n, dtype=float)
    w = 0.5 ** ((n - 1 - x) / max(half_life_bars, 1))
    w = w / w.sum()
    xm = float(np.sum(w * x))
    ym = float(np.sum(w * y))
    var = float(np.sum(w * (x - xm) ** 2))
    if var <= 0:
        return 0.0
    return float(np.sum(w * (x - xm) * (y - ym)) / var)


def _slope(y: np.ndarray) -> float:
    if SW.WLS_SLOPE:
        return _wls_slope(y)
    return float(np.polyfit(np.arange(len(y)), y, 1)[0])   # ordinary least squares


# ================= P8: per-pair bar-close trigger =================
def _is_tf_just_closed(tf: str, now_utc, tol_sec: int = BAR_CLOSE_TOLERANCE_SEC) -> bool:
    """True if tf closed within tol_sec before now_utc (UTC).
    OANDA: M15 at :00/:15/:30/:45, M30 at :00/:30, H1 at :00, H4 at 00/04/08/12/16/20 UTC, D at 00:00 UTC.
    D close is simplified to 00:00 UTC; tune to 21:00/22:00 UTC (17:00 ET) if you need exact NY close.
    """
    import datetime as _dt
    if tol_sec < 0:
        return False
    intervals = {"M15": 15 * 60, "M30": 30 * 60, "H1": 3600,
                 "H4": 4 * 3600, "D1": 24 * 3600, "D": 24 * 3600}
    interval = intervals.get(tf)
    if interval is None:
        return False
    if isinstance(now_utc, (int, float)):
        now_utc = _dt.datetime.fromtimestamp(now_utc, tz=_dt.timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=_dt.timezone.utc)
    else:
        now_utc = now_utc.astimezone(_dt.timezone.utc)
    # OANDA intraday bars align to UTC epoch boundaries; D1 is UTC midnight here.
    timestamp = now_utc.timestamp()
    elapsed = timestamp % interval
    return elapsed <= tol_sec


def get_just_closed_tfs(now_utc=None, tol_sec: int = BAR_CLOSE_TOLERANCE_SEC) -> set:
    import datetime
    if now_utc is None:
        now_utc = datetime.datetime.utcnow()
    # Allow passing timestamp float
    if isinstance(now_utc, (int, float)):
        import datetime as _dt
        now_utc = _dt.datetime.utcfromtimestamp(now_utc)
    closed = set()
    for tf in ("M15", "M30", "H1", "H4", "D1"):
        if _is_tf_just_closed(tf, now_utc, tol_sec):
            closed.add(tf)
            if tf == "D1":
                closed.add("D")  # alias
    return closed


def _tf_close_epoch(tf: str, now_utc) -> Optional[int]:
    """Epoch second for the latest UTC-aligned close of a supported timeframe."""
    import datetime as _dt
    intervals = {"M15": 15 * 60, "M30": 30 * 60, "H1": 3600,
                 "H4": 4 * 3600, "D1": 24 * 3600, "D": 24 * 3600}
    interval = intervals.get(tf)
    if interval is None:
        return None
    if isinstance(now_utc, (int, float)):
        timestamp = float(now_utc)
    else:
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=_dt.timezone.utc)
        else:
            now_utc = now_utc.astimezone(_dt.timezone.utc)
        timestamp = now_utc.timestamp()
    return int(timestamp // interval) * interval


_LAST_PROCESSED_BAR_CLOSE: Dict[str, int] = {}


def fresh_closed_tfs(now_utc=None) -> set:
    """P8/v1.1.5: timeframes that just closed AND have not been processed in this process.
    This is the explicit bar-close trigger; decision_tick() consults it directly."""
    import datetime as _dt
    if now_utc is None:
        now_utc = _dt.datetime.utcnow()
    closed = get_just_closed_tfs(now_utc, BAR_CLOSE_TOLERANCE_SEC)
    fresh = set()
    for tf in closed:
        close_epoch = _tf_close_epoch(tf, now_utc)
        if close_epoch is not None and _LAST_PROCESSED_BAR_CLOSE.get(tf) != close_epoch:
            fresh.add(tf)
            _LAST_PROCESSED_BAR_CLOSE[tf] = close_epoch
    return fresh


def next_bar_close_sec(now_utc=None) -> float:
    """Seconds until the next close among tracked timeframes (caller sleep hint)."""
    import datetime as _dt
    if now_utc is None:
        now_utc = _dt.datetime.utcnow()
    if isinstance(now_utc, (int, float)):
        ts = float(now_utc)
    else:
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=_dt.timezone.utc)
        else:
            now_utc = now_utc.astimezone(_dt.timezone.utc)
        ts = now_utc.timestamp()
    return min(iv - (ts % iv) for iv in (15 * 60, 30 * 60, 3600, 4 * 3600, 24 * 3600))


def pair_metrics(inst: str) -> Optional[dict]:
    """Per-pair metrics on the pair's own timeframes, plus a uniform H1 series/ATR
    (used by the P12 correlation guard, the P13 cost normalization and P15/P16 prices)."""
    pair = inst.replace("_", "/")
    cfg = get_pair_config(pair)
    primary = cfg["primary"]
    weights = cfg["weights"] if SW.MTF_STRENGTH else {primary: 1.0}
    data: Dict[str, Dict[str, np.ndarray]] = {}
    for tf in set(weights) | {primary, "H1"}:
        d = fetch_candles(inst, tf)
        if d is not None and len(d["c"]) >= 50:
            data[tf] = d
    if primary not in data:
        return None
    num = den = 0.0
    for tf, w in weights.items():
        d = data.get(tf)
        if d is None:
            continue
        num += w * _slope(d["c"]) / compute_atr(d["h"], d["l"], d["c"])
        den += w
    if den == 0:
        return None
    p = data[primary]
    h1 = data.get("H1")
    return {
        "momentum": num / den,
        "atr": compute_atr(p["h"], p["l"], p["c"]),
        "returns": np.diff(np.log(p["c"]))[-60:],
        "h1_returns": np.diff(np.log(h1["c"]))[-60:] if h1 is not None else np.array([]),
        "h1_atr": compute_atr(h1["h"], h1["l"], h1["c"]) if h1 is not None else compute_atr(p["h"], p["l"], p["c"]),
        "price": float(p["c"][-1]),
    }


def _currency_coverage(snap: Snapshot) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for pair in snap.momentums:
        base, quote = pair.split("/")
        counts[base] = counts.get(base, 0) + 1
        counts[quote] = counts.get(quote, 0) + 1
    return counts


def coverage_ok(snap: Snapshot, universe: List[str]) -> Tuple[bool, str]:
    """P11: coverage gate for BOTH snapshot paths, by pair count AND by currency
    representation (a bare 24/28 can still leave a currency unrepresented)."""
    if len(snap.momentums) < MIN_COVERAGE:
        return False, f"pair coverage {len(snap.momentums)}/{len(universe)} < {MIN_COVERAGE}"
    counts = _currency_coverage(snap)
    thin = sorted(c for c in _CURRENCIES if counts.get(c, 0) < MIN_CCY_PAIRS)
    if thin:
        return False, f"currency coverage too thin: {thin} (< {MIN_CCY_PAIRS} pairs each)"
    return True, ""


def build_market_snapshot(timeout_sec: float = 2.0) -> Optional[Snapshot]:
    snap = Snapshot({}, {}, {})

    def put(inst, res):
        if res is not None:
            k = inst.replace("_", "/")
            snap.momentums[k] = float(res["momentum"])
            snap.atrs[k] = res["atr"]
            snap.returns[k] = res["returns"]
            snap.h1_returns[k] = res["h1_returns"]
            snap.h1_atrs[k] = res["h1_atr"]
            snap.prices[k] = res["price"]

    universe = get_universe()
    if SW.PARALLEL_SNAPSHOT:
        ex = ThreadPoolExecutor(max_workers=8)
        futs = {ex.submit(pair_metrics, i): i for i in universe}
        try:
            for f in as_completed(futs, timeout=SNAPSHOT_TIMEOUT):
                inst = futs[f]
                try:
                    put(inst, f.result())
                except Exception as e:
                    print(f"[Snapshot] {inst} failed: {e}")
        except Exception as e:  # TimeoutError: retain completed results
            print(f"[Snapshot] partial: {e}")
        ex.shutdown(wait=False, cancel_futures=True)
    else:
        start = time.time()                    # legacy sequential path
        for inst in universe:
            if time.time() - start > timeout_sec:
                print("[Snapshot] timeout, skipping remaining pairs")
                break
            put(inst, pair_metrics(inst))

    # P11: the coverage gate now guards BOTH paths (v1.1.4 only guarded the parallel one).
    if SW.UNIVERSAL_COVERAGE:
        ok, why = coverage_ok(snap, universe)
        if not ok:
            print(f"[Snapshot] {why}: SKIP TICK")
            return None
    elif SW.PARALLEL_SNAPSHOT and len(snap.momentums) < MIN_COVERAGE:
        print(f"[Snapshot] coverage {len(snap.momentums)}/{len(universe)} < {MIN_COVERAGE}: SKIP TICK")
        return None
    return snap


def fetch_spreads(insts: List[str]) -> Dict[str, float]:
    resp = get_client().request(pricing_ep.PricingInfo(account_id(), params={"instruments": ",".join(insts)}))
    out = {}
    for p in resp.get("prices", []):
        try:
            out[p["instrument"].replace("_", "/")] = float(p["asks"][0]["price"]) - float(p["bids"][0]["price"])
        except (KeyError, IndexError):
            pass
    return out


def live_cost_score(spread: float, atr: float) -> float:
    return float(np.clip(1.0 - (spread / max(atr, 1e-12)) / COST_CAP, 0.0, 1.0))


FALLBACK_STATS = {"seen": 0, "fallback": 0}     # positions seen / positions using placeholder ATR+returns


def parse_positions(resp: dict, snap: Optional[Snapshot] = None) -> List[Position]:
    """Pure parser (testable offline). Never drops an open position: hidden exposure is worse
    than missing data. Positions that end up on placeholder values are counted in FALLBACK_STATS."""
    out = []
    for p in resp.get("positions", []):
        net = float(p["long"]["units"]) + float(p["short"]["units"])
        if net == 0:
            continue
        pair = p["instrument"].replace("_", "/")
        atr, rets, h1r, fell_back = 0.001, np.array([]), np.array([]), True   # placeholder = fallback
        if SW.USE_REAL_POS_DATA:
            if snap and pair in snap.atrs:
                atr, rets, fell_back = snap.atrs[pair], snap.returns[pair], False
                h1r = snap.h1_returns.get(pair, np.array([]))
            else:
                res = pair_metrics(p["instrument"])
                if res is not None:
                    atr, rets, fell_back = res["atr"], res["returns"], False
                    h1r = res["h1_returns"]
        FALLBACK_STATS["seen"] += 1
        if fell_back:
            FALLBACK_STATS["fallback"] += 1
            # Placeholder atr=0.001, rets=[]: legacy exposure = size*atr (e.g. 1000 units -> 1.0 per ccy,
            # wrong magnitude), correlation guard skips it. RISK_UNIT_EXPOSURE=True counts it as 1 risk unit.
            print(f"[WARN] FALLBACK_EXPOSURE {pair}: placeholder ATR/returns in use")
        out.append(Position(pair, "LONG" if net > 0 else "SHORT", abs(net), atr, rets, h1_returns=h1r))
    return out


def fetch_open_positions(snap: Optional[Snapshot] = None) -> List[Position]:
    resp = get_client().request(positions_ep.OpenPositions(accountID=account_id()))
    return parse_positions(resp, snap)


def fallback_ratio() -> float:
    return FALLBACK_STATS["fallback"] / FALLBACK_STATS["seen"] if FALLBACK_STATS["seen"] else 0.0


def quote_ccy_of(pair: str) -> str:
    return pair.split("/")[1]


def size_units(risk_amount: float, sl_distance: float,
               account_ccy: Optional[str] = None, quote_ccy: Optional[str] = None,
               quote_rate: Optional[float] = None) -> int:
    """units = risk / stop distance.
    `risk_amount` is expressed in the pair's QUOTE currency. When `account_ccy` differs
    from `quote_ccy`, pass `quote_rate` = units of quote currency per 1 account-currency
    unit; the amount is converted. v1.1.5 §8 gap: raises (fail-closed) if that rate is
    missing rather than silently assuming parity. Raises on bad inputs and when the
    result is under 1 unit (OANDA would reject it)."""
    if sl_distance is None or sl_distance <= 1e-12:
        raise ValueError(f"size_units: invalid sl_distance={sl_distance}")
    if risk_amount is None or risk_amount <= 0:
        raise ValueError(f"size_units: invalid risk_amount={risk_amount}")
    if account_ccy and quote_ccy and account_ccy != quote_ccy:
        if quote_rate is None or quote_rate <= 0:
            raise ValueError(
                f"size_units: need quote_rate (quote ccy per account ccy) for {account_ccy}->{quote_ccy}")
        risk_amount = risk_amount * quote_rate
    units = int(risk_amount / sl_distance)
    if units < 1:
        raise ValueError(f"size_units: risk {risk_amount} / stop {sl_distance} < 1 unit; widen risk or tighten stop")
    return units


def place_market_order(pair_oanda: str, side: str, units: int, sl_distance: Optional[float] = None):
    if SW.REQUIRE_SL and not sl_distance:
        raise ValueError("REQUIRE_SL: refusing order without stop loss")
    if SW.DRY_RUN:
        print(f"[DRY_RUN] {side} {units} {pair_oanda} sl={sl_distance}")
        return None
    if os.environ.get("OANDA_ENV", "practice") != "practice" and os.environ.get("ALLOW_LIVE_ORDERS") != "YES":
        raise RuntimeError("Live environment: set ALLOW_LIVE_ORDERS=YES to place orders")
    order = {"instrument": pair_oanda, "units": str(units if side == "LONG" else -units),
             "type": "MARKET", "positionFill": "DEFAULT"}
    if sl_distance:
        order["stopLossOnFill"] = {"distance": f"{sl_distance:.{3 if 'JPY' in pair_oanda else 5}f}"}
    return get_client().request(orders_ep.OrderCreate(accountID=account_id(), data={"order": order}))


# ================= Decision tick =================
def decision_tick(router: GlobalPortfolioRouter, candidates: List[SignalCandidate],
                  shadow: Optional[ShadowEngine] = None, now_utc=None,
                  rejected_book: Optional["ShadowRejectedBook"] = None):
    # P8 gate: if enabled, only proceed when at least one primary TF just closed
    if SW.PER_PAIR_BAR_CLOSE:
        fresh_tfs = fresh_closed_tfs(now_utc)
        if not fresh_tfs:
            # Not a bar close moment for any tracked TF -> skip tick (caller should sleep)
            return None
        # Filter candidates to those whose primary TF is among the just-closed TFs
        filtered = [c for c in candidates
                    if get_pair_config(c.pair)["primary"] in fresh_tfs
                    or (get_pair_config(c.pair)["primary"] == "D" and "D1" in fresh_tfs)]
        # If no candidate matches a newly closed primary timeframe, skip this tick.
        if not filtered:
            print(f"[BarClose] no candidates for closed TFs {fresh_tfs}: SKIP TICK")
            return None
        candidates = filtered
        print(f"[BarClose] closed TFs {fresh_tfs}, trading {len(candidates)} candidates")

    snap = build_market_snapshot()
    if snap is None:
        return None
    open_positions = fetch_open_positions(snap)
    print(f"[FALLBACK] {FALLBACK_STATS['fallback']}/{FALLBACK_STATS['seen']} positions on placeholders "
          f"({fallback_ratio():.0%}, cumulative)")

    spreads: Dict[str, float] = {}
    for c in candidates:                        # fill gaps from snapshot
        if len(c.returns_series) == 0 and c.pair in snap.returns:
            c.returns_series = snap.returns[c.pair]
        if c.h1_returns is None and c.pair in snap.h1_returns:
            c.h1_returns = snap.h1_returns[c.pair]
        if not c.atr:
            c.atr = snap.atrs.get(c.pair, 0.0)
    if SW.LIVE_COST and candidates:
        spreads = fetch_spreads(sorted({c.pair.replace("/", "_") for c in candidates}))
        for c in candidates:
            cost_atr = snap.h1_atrs.get(c.pair) if SW.H1_COST else c.atr   # P13
            if c.pair in spreads and cost_atr:
                c.cost_score = live_cost_score(spreads[c.pair], cost_atr)

    best = router.select_best_trade(candidates, snap.momentums, open_positions)
    if shadow is not None:
        shadow.step(candidates, snap.momentums, open_positions, prices=snap.prices)

    # P16: log rejected candidates to a shadow book for later outcome scoring
    if SW.SHADOW_REJECTED and rejected_book is not None:
        ts = time.time()
        by_key = {(c.pair, c.side): c for c in candidates}
        for pair, side, reason in best.get("rejected", []):
            c = by_key.get((pair, side))
            px = snap.prices.get(pair) if c is not None else None
            if px is None:
                continue
            rejected_book.record(pair, side, px, ts, spread=spreads.get(pair), reason=reason)

    if not best.get("pair"):
        print(f"[Tick] No trade. Rejected: {best.get('rejected')}")
        return None
    print(f"[Tick] Best: {best}")
    # sl = SL_ATR_MULT * snap.atrs[best["pair"]]
    # units = size_units(RISK_AMOUNT, sl, account_ccy=ACCOUNT_CCY,
    #                    quote_ccy=quote_ccy_of(best["pair"]),
    #                    quote_rate=QUOTE_RATES.get(quote_ccy_of(best["pair"])))
    # place_market_order(best["pair"].replace("/", "_"), best["side"], units=units, sl_distance=sl)
    return best


# ================= Offline self-test (no OANDA needed) =================
if __name__ == "__main__":
    rng = np.random.default_rng(1)
    pairs = [i.replace("_", "/") for i in get_universe()]
    rets = lambda: rng.normal(size=60) * 1e-3

    # --- P5: exposure cap really triggers (EUR/USD strong, 2 risk units already held) ---
    SW.RISK_UNIT_EXPOSURE = SW.USE_REAL_POS_DATA = True
    moms = {p: 0.0 for p in pairs}
    moms["EUR/USD"], moms["GBP/USD"] = 2.0, -2.0
    cands = [SignalCandidate("EUR/USD", "LONG", 0.9, 0.8, 0.7, "H1", rets(), 0.01),
             SignalCandidate("GBP/USD", "LONG", 0.6, 0.5, 0.7, "H1", rets(), 0.01)]
    held = [Position("EUR/USD", "LONG", 1000, 0.01, rets(), risk_units=1.0)] * 2
    o2 = GlobalPortfolioRouter(RouterParams(max_ccy_units=2.0)).select_best_trade(cands, moms, held)
    assert any(x[2] == "ccy_exposure" for x in o2["rejected"]), "P5: cap 2.0 must reject as ccy_exposure"
    o3 = GlobalPortfolioRouter(RouterParams(max_ccy_units=3.0)).select_best_trade(cands, moms, held)
    assert o3["pair"] == "EUR/USD", "P5: cap 3.0 must allow EUR/USD"
    print("P5 test passed")

    # --- fallback counters (parse_positions with no data available) ---
    globals()["pair_metrics"] = lambda inst: None          # simulate failed data fetch
    fake = {"positions": [
        {"instrument": "EUR_USD", "long": {"units": "1000"}, "short": {"units": "0"}},
        {"instrument": "GBP_JPY", "long": {"units": "0"}, "short": {"units": "-500"}},
        {"instrument": "USD_CAD", "long": {"units": "0"}, "short": {"units": "0"}}]}   # flat, ignored
    snap = Snapshot(momentums={}, returns={"EUR/USD": rets()}, atrs={"EUR/USD": 0.002})
    pos = parse_positions(fake, snap)
    assert len(pos) == 2 and pos[0].atr == 0.002 and pos[1].atr == 0.001
    assert FALLBACK_STATS == {"seen": 2, "fallback": 1} and fallback_ratio() == 0.5
    print("fallback counter test passed")

    # --- size_units guards ---
    for bad in [(100, 0), (0, 0.01), (-5, 0.01), (0.001, 0.05)]:
        try:
            size_units(*bad)
            raise AssertionError(f"size_units{bad} should raise")
        except ValueError:
            pass
    assert size_units(100, 0.01) == 10000
    print("size_units test passed")

    # --- P4: promotion needs 3 consecutive wins; one field only ---
    SW.STRICT_PROMOTION = True
    live_r = GlobalPortfolioRouter(RouterParams())
    sh = ShadowEngine([RouterParams(min_alignment=0.2), RouterParams(min_alignment=0.3)])
    meta = MetaController(live_r, sh)
    L = {"sharpe": 1.0, "max_dd": 0.10, "n_trades": 30}
    W = {"sharpe": 1.5, "max_dd": 0.05, "n_trades": 30}
    res = [meta.evaluate_window(L, [W, L], "min_alignment") for _ in range(3)]
    assert res == [None, None, 0] and live_r.p.min_alignment == 0.2 and live_r.p.w_align == 0.35
    try:
        meta.evaluate_and_maybe_promote(1.0, [1.2])
        raise AssertionError("legacy promotion should raise")
    except RuntimeError:
        pass
    print("P4 test passed")

    # --- P3 ---
    SW.REQUIRE_SL = True
    try:
        place_market_order("EUR_USD", "LONG", 1000)
        raise AssertionError("REQUIRE_SL should refuse")
    except ValueError:
        pass
    print("P3 test passed")

    # --- P8: per-pair bar-close trigger ---
    import datetime as _dt
    SW.PER_PAIR_BAR_CLOSE = True
    # Simulate H1 close at 10:00:30 UTC -> should include H1 primary pairs, not H4 unless hour%4==0
    fake_now_h1 = _dt.datetime(2026, 11, 25, 10, 0, 30)  # H1 close, not H4 (10%4=2)
    closed = get_just_closed_tfs(fake_now_h1, tol_sec=120)
    assert "H1" in closed and "H4" not in closed, f"H1 close detection failed: {closed}"
    assert _is_tf_just_closed("H1", fake_now_h1) and not _is_tf_just_closed("H4", fake_now_h1)

    fake_now_h4 = _dt.datetime(2026, 11, 25, 12, 0, 30)  # H4 close (12%4==0)
    closed_h4 = get_just_closed_tfs(fake_now_h4, tol_sec=120)
    assert "H1" in closed_h4 and "H4" in closed_h4, f"H4 close should include H1+H4: {closed_h4}"

    fake_now_none = _dt.datetime(2026, 11, 25, 10, 5, 30)  # 5 min past hour, no close
    assert len(get_just_closed_tfs(fake_now_none)) == 0

    # decision_tick should skip when no TF closed
    SW.PER_PAIR_BAR_CLOSE = True
    # Mock build_market_snapshot and fetch_open_positions to avoid OANDA
    orig_build = globals()["build_market_snapshot"]
    orig_fetch = globals()["fetch_open_positions"]
    globals()["build_market_snapshot"] = lambda timeout_sec=2.0: None
    globals()["fetch_open_positions"] = lambda snap=None: []
    r = GlobalPortfolioRouter(RouterParams())
    res_skip = decision_tick(r, cands, None, now_utc=fake_now_none)
    assert res_skip is None, "P8 should skip tick when no TF closed"
    # Restore
    globals()["build_market_snapshot"] = orig_build
    globals()["fetch_open_positions"] = orig_fetch
    SW.PER_PAIR_BAR_CLOSE = False
    print("P8 test passed")

    # --- P9: pairs from file (dedupe, reject non-OANDA, raise on bad/missing file) ---
    import tempfile
    def _tmp(txt):
        f = tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False)
        f.write(txt); f.close(); return f.name
    good = "pairs:\n" + "".join(f"  - {p.replace('_', '')}\n" for p in _LEGACY_UNIVERSE)
    dirty = good + "  - NZDCHF\n  - EURUSD\n  - CADNZD\n  - FOO\n"      # dupes + inverse + junk
    assert load_universe(_tmp(dirty)) == _LEGACY_UNIVERSE, "P9: dedupe/reject must give the clean 28, same order"
    for bad in ("pairs: []\n", "simulations: 5\n", "pairs:\n  - EURUSD\n  - GBPUSD\n"):
        try:
            load_universe(_tmp(bad))
            raise AssertionError("P9: bad file should raise")
        except ValueError:
            pass
    try:
        load_universe("/nonexistent/mc_data.yml")
        raise AssertionError("P9: missing file should raise")
    except FileNotFoundError:
        pass
    print("P9 test passed")

    # --- P10: gross exposure cap (offsetting book still caps the currency) ---
    SW.RISK_UNIT_EXPOSURE = True
    g_moms = {p: 0.0 for p in pairs}
    g_moms["EUR/USD"] = 2.0
    g_cands = [SignalCandidate("EUR/USD", "LONG", 0.9, 0.8, 0.7, "H1", rets(), 0.01)]
    # 2 gross USD units already (long EUR/USD + short USD/JPY both carry USD)
    g_held = [Position("EUR/USD", "LONG", 1000, 0.01, rets(), risk_units=1.0),
              Position("USD/JPY", "SHORT", 1000, 0.01, rets(), risk_units=1.0)]
    SW.GROSS_EXPOSURE_CAP = True
    gp = RouterParams(max_ccy_units=5.0, max_ccy_gross_units=2.0)
    g_out = GlobalPortfolioRouter(gp).select_best_trade(g_cands, g_moms, g_held)
    assert any(x[2] == "ccy_gross_exposure" for x in g_out["rejected"]), "P10: gross cap must reject"
    gp2 = RouterParams(max_ccy_units=5.0, max_ccy_gross_units=3.0)
    assert GlobalPortfolioRouter(gp2).select_best_trade(g_cands, g_moms, g_held)["pair"] == "EUR/USD"
    SW.GROSS_EXPOSURE_CAP = False
    print("P10 test passed")

    # --- P11: coverage gate checks currency representation, on both paths ---
    full = Snapshot({p.replace("_", "/"): 0.0 for p in _LEGACY_UNIVERSE}, {}, {})
    ok, why = coverage_ok(full, _LEGACY_UNIVERSE)
    assert ok, f"P11: full snapshot must pass: {why}"
    thin = Snapshot({p.replace("_", "/"): 0.0 for p in _LEGACY_UNIVERSE if p.endswith("_JPY")}, {}, {})
    ok2, why2 = coverage_ok(thin, _LEGACY_UNIVERSE)
    assert (not ok2) and ("currency coverage" in why2 or "pair coverage" in why2), f"P11: thin snapshot must fail: {why2}"
    print("P11 test passed")

    # --- P12: uniform-H1 correlation guard is fail-closed on missing data ---
    SW.H1_CORR_GUARD = True
    h_cands = [SignalCandidate("EUR/USD", "LONG", 0.9, 0.8, 0.7, "H1", rets(), 0.01, h1_returns=None)]
    h_held = [Position("GBP/USD", "LONG", 1000, 0.01, rets(), risk_units=1.0, h1_returns=None)]
    h_out = GlobalPortfolioRouter(RouterParams(max_ccy_units=5.0)).select_best_trade(h_cands, moms, h_held)
    assert h_out["pair"] is None and any("corr" in x[2] for x in h_out["rejected"]), "P12: missing data must block"
    # identical series are perfectly correlated -> blocked
    s = rets()
    h_cands2 = [SignalCandidate("EUR/USD", "LONG", 0.9, 0.8, 0.7, "H1", s, 0.01, h1_returns=s)]
    h_held2 = [Position("EUR/USD", "LONG", 1000, 0.01, s, risk_units=1.0, h1_returns=s)]
    h_out2 = GlobalPortfolioRouter(RouterParams(max_ccy_units=5.0)).select_best_trade(h_cands2, moms, h_held2)
    assert h_out2["pair"] is None, "P12: identical series must be blocked"
    SW.H1_CORR_GUARD = False
    print("P12 test passed")

    # --- P13: H1 cost normalization (cost uses ATR_H1 when the switch is on) ---
    snap_h1 = Snapshot({}, {}, {}, h1_atrs={"EUR/USD": 0.02})
    SW.H1_COST = True
    cost_atr = snap_h1.h1_atrs.get("EUR/USD") if SW.H1_COST else 0.01
    assert cost_atr == 0.02, "P13: cost ATR must come from H1 when H1_COST is on"
    assert live_cost_score(0.0, cost_atr) == 1.0
    SW.H1_COST = False
    print("P13 test passed")

    # --- P14: weighted least squares recovers a known slope ---
    y = 2.0 * np.arange(120, dtype=float) + 5.0
    assert abs(_wls_slope(y, half_life_bars=30) - 2.0) < 1e-6, "P14: WLS slope must recover 2.0"
    assert abs(_slope(y) - 2.0) < 1e-6, "P14: OLS path unchanged"
    SW.WLS_SLOPE = True
    assert abs(_slope(y) - 2.0) < 1e-6, "P14: switch must route to WLS"
    SW.WLS_SLOPE = False
    print("P14 test passed")

    # --- P15: shadow positions exit on a real stop, not just the fixed tick count ---
    SW.SHADOW_OWN_POSITIONS = True
    SW.SHADOW_REAL_EXIT = True
    sh2 = ShadowEngine([RouterParams(min_alignment=0.0)])
    s_cands = [SignalCandidate("EUR/USD", "LONG", 0.9, 0.9, 0.9, "H1", rets(), 0.01)]
    s_moms = {p: 0.0 for p in pairs}; s_moms["EUR/USD"] = 2.0
    sh2.step(s_cands, s_moms, [], prices={"EUR/USD": 1.1000})
    assert len(sh2.sim_positions[0]) == 1 and sh2.sim_positions[0][0].entry_price == 1.1000
    # price falls past the stop -> shadow position closes
    sh2.step([], s_moms, [], prices={"EUR/USD": 1.1000 - (SHADOW_EXIT_SL_ATR * 0.01 + 1e-4)})
    assert len(sh2.sim_positions[0]) == 0, "P15: shadow must exit on stop"
    SW.SHADOW_REAL_EXIT = False
    SW.SHADOW_OWN_POSITIONS = False
    print("P15 test passed")

    # --- P16: rejected-candidate outcome book ---
    SW.SHADOW_REJECTED = True
    book = ShadowRejectedBook(horizon_bars=1, bar_seconds=1.0)
    book.record("EUR/USD", "LONG", 1.1000, ts=1000.0, spread=0.0002, reason="align=0.00")
    book.record("GBP/USD", "SHORT", 1.3000, ts=1000.0, spread=0.0003, reason="ccy_exposure")
    # before the horizon has elapsed nothing is scored
    assert book.evaluate(lambda p: 1.2000, now_ts=1000.5)["n_closed"] == 0
    prices_now = {"EUR/USD": 1.2000, "GBP/USD": 1.4000}
    summ = book.evaluate(prices_now.get, now_ts=1002.0)
    assert summ["n_closed"] == 2 and summ["wins"] == 1, f"P16: expected EUR win / GBP loss, got {summ}"
    assert summ["net_return"] > 0
    SW.SHADOW_REJECTED = False
    print("P16 test passed")

    # --- P8 helper: next bar-close delay is positive and bounded by 15m ---
    d = next_bar_close_sec(_dt.datetime(2026, 11, 25, 10, 0, 30))
    assert 0 < d <= 15 * 60, f"next_bar_close_sec out of range: {d}"
    print("bar-close helper test passed\nALL SELF-TESTS PASSED")
