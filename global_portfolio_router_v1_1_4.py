"""
Global Portfolio Router v1.1.4 - patched build (all patches switch-gated, default OFF)

SWITCHES (class SW) - enable one at a time in practice, in this order:
  CLOSED_BARS_ONLY     P2  drop OANDA's in-progress candle
  USE_REAL_POS_DATA    P1  real ATR + returns for open positions (never drops a position)
  REQUIRE_SL           P3  every order must carry a stop loss
  STRICT_PROMOTION     P4  3 consecutive windows, Sharpe AND drawdown, additive margin
  RISK_UNIT_EXPOSURE   P5  exposure in risk units (1 unit = one fixed-risk trade)
  PARALLEL_SNAPSHOT    P6  parallel fetch, skip tick if coverage < MIN_COVERAGE
  MTF_STRENGTH         P7  use per-pair timeframe weights in momentum
  TRUE_ATR             P7  true-range ATR instead of std(diff)
  TIME_BASED_EMA       P7  strength EMA by elapsed time, not per call
  LIVE_COST            P7  cost_score from live OANDA spread / ATR
  SHADOW_OWN_POSITIONS P7  shadow routers track their own simulated positions
  DRY_RUN              orders are only printed (ON by default)

ROLLOUT (practice account only until every step is clean):
  P8 PER_PAIR_BAR_CLOSE: off by default = tick every call (H1). On = only when primary TF closed.
  Week 1  DRY_RUN on:  P2 alone -> P1+P5 together (P5 is needed regardless: legacy exposure units are
          inconsistent) -> P3. Watch the [FALLBACK] ratio logged each tick. When clean, set
          DRY_RUN=False on the PRACTICE environment: first real test of the stop-loss path.
  Week 2  P6 first (the coverage gate only exists with it), then P7 one switch at a time.
          Run ShadowEngine alongside; P4 (evaluate_window) belongs here, shadows are needed for it.

Credentials come from env vars: OANDA_TOKEN, OANDA_ACCOUNT_ID, OANDA_ENV (default "practice").
Orders on a non-practice environment additionally require ALLOW_LIVE_ORDERS=YES.
"""
from __future__ import annotations

import dataclasses
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

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
    TIME_BASED_EMA = False
    LIVE_COST = False
    SHADOW_OWN_POSITIONS = False
    PER_PAIR_BAR_CLOSE = False  # P8: only trade pairs whose primary TF just closed
    DRY_RUN = True


# ---- tunables ----
MIN_COVERAGE = 24            # P6: min pairs (of 28) needed to Z-score
SNAPSHOT_TIMEOUT = 5.0       # P6 parallel timeout (legacy sequential keeps 2.0)
HALF_LIFE_SEC = 6 * 3600     # P7: strength half-life (6 H1 ticks)
COST_CAP = 0.25              # P7: spread/ATR at which cost_score hits 0 (assumption, tune)
SL_ATR_MULT = 2.0            # P3: stop distance = SL_ATR_MULT * ATR
SHADOW_HOLD_STEPS = 12       # P7: simulated shadow position lifetime (ticks)
MIN_TRADES_PER_WINDOW = 20   # P4: ignore windows with fewer trades
BAR_CLOSE_TOLERANCE_SEC = 120  # P8: how long after a bar close we still consider it "just closed"
TF_TO_OANDA = {"M15": "M15", "M30": "M30", "H1": "H1", "H4": "H4", "D1": "D"}

UNIVERSE = [
    "EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "NZD_USD", "USD_CAD",
    "EUR_GBP", "EUR_JPY", "EUR_AUD", "EUR_CHF", "EUR_CAD", "EUR_NZD",
    "GBP_JPY", "GBP_AUD", "GBP_CHF", "GBP_CAD", "GBP_NZD",
    "AUD_JPY", "AUD_CHF", "AUD_CAD", "AUD_NZD",
    "NZD_JPY", "NZD_CHF", "NZD_CAD",
    "CAD_JPY", "CAD_CHF", "CHF_JPY",
]

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


@dataclass
class Position:
    pair: str
    side: str
    size: float                 # units
    atr: float
    returns_series: np.ndarray
    risk_units: float = 1.0     # P5: assumes fixed-risk sizing (1 trade = 1 unit)
    age: int = 0                # shadow bookkeeping


@dataclass
class RouterParams:
    max_ccy_exposure: float = 1.0   # legacy exposure cap
    max_ccy_units: float = 2.0      # P5 cap in risk units (1.0 forbids any two trades sharing a currency)
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


# ================= Core router =================
class GlobalPortfolioRouter:
    def __init__(self, params: RouterParams):
        self.p = params
        self.prev_strength: Dict[str, float] = {}
        self.alpha = 0.15           # legacy per-call EMA (~4.3-call half-life)
        self._last_ts: Optional[float] = None
        self._warned_corr = False

    def compute_currency_strength(self, pair_momentums: Dict[str, float]) -> Dict[str, float]:
        buckets: Dict[str, List[float]] = {}
        for pair, mom in pair_momentums.items():
            base, quote = pair.split("/")
            buckets.setdefault(base, []).append(mom)
            buckets.setdefault(quote, []).append(-mom)
        raw = {ccy: float(np.mean(v)) for ccy, v in buckets.items()}
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

    def _get_weighted_exposure(self, positions: List[Position]) -> Dict[str, float]:
        exp: Dict[str, float] = {}
        for pos in positions:
            base, quote = pos.pair.split("/")
            d = 1 if pos.side == "LONG" else -1
            w = pos.risk_units if SW.RISK_UNIT_EXPOSURE else pos.size * pos.atr
            exp[base] = exp.get(base, 0) + d * w
            exp[quote] = exp.get(quote, 0) - d * w
        return exp

    def _is_correlated(self, cand: SignalCandidate, positions: List[Position]) -> bool:
        for pos in positions:
            # Limitation: the series can use different pair-primary timeframes (e.g. H1 vs H4),
            # so equal array lengths do not guarantee aligned observation horizons.
            a, b = cand.returns_series, pos.returns_series
            if SW.USE_REAL_POS_DATA:
                n = min(len(a), len(b))
                if n < 20:
                    continue
                a, b = a[-n:], b[-n:]
            elif len(a) != len(b) or len(a) == 0:
                if len(b) == 0 and not self._warned_corr:
                    print("[WARN] position returns empty -> correlation guard INACTIVE "
                          "(enable USE_REAL_POS_DATA)")
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

    def select_best_trade(self, candidates, pair_momentums, open_positions):
        ccy_strength = self.compute_currency_strength(pair_momentums)
        exposure = self._get_weighted_exposure(open_positions)
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

    def step(self, candidates, pair_momentums, live_positions):
        results = []
        for i, (router, hist) in enumerate(zip(self.routers, self.histories)):
            if SW.SHADOW_OWN_POSITIONS:
                sim = [p for p in self.sim_positions[i] if p.age < SHADOW_HOLD_STEPS]
                for p in sim:
                    p.age += 1
                self.sim_positions[i] = sim
                positions = sim
            else:
                positions = live_positions
            r = router.select_best_trade(candidates, pair_momentums, positions)
            hist.append(r)
            results.append(r)
            if SW.SHADOW_OWN_POSITIONS and r.get("pair"):
                c = next((c for c in candidates if c.pair == r["pair"] and c.side == r["side"]), None)
                if c is not None:
                    self.sim_positions[i].append(
                        Position(c.pair, c.side, 1.0, c.atr, c.returns_series))
        return results


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


def pair_metrics(inst: str) -> Optional[Tuple[float, float, np.ndarray]]:
    """(momentum, atr, returns[-60:]) on the pair's own timeframes."""
    pair = inst.replace("_", "/")
    cfg = get_pair_config(pair)
    primary = cfg["primary"]
    weights = cfg["weights"] if SW.MTF_STRENGTH else {primary: 1.0}
    data: Dict[str, Dict[str, np.ndarray]] = {}
    for tf in set(weights) | {primary}:
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
        slope = np.polyfit(np.arange(len(d["c"])), d["c"], 1)[0]
        num += w * slope / compute_atr(d["h"], d["l"], d["c"])
        den += w
    p = data[primary]
    atr = compute_atr(p["h"], p["l"], p["c"])
    return num / den, atr, np.diff(np.log(p["c"]))[-60:]


def build_market_snapshot(timeout_sec: float = 2.0) -> Optional[Snapshot]:
    snap = Snapshot({}, {}, {})

    def put(inst, res):
        if res is not None:
            k = inst.replace("_", "/")
            snap.momentums[k], snap.atrs[k], snap.returns[k] = float(res[0]), res[1], res[2]

    if SW.PARALLEL_SNAPSHOT:
        ex = ThreadPoolExecutor(max_workers=8)
        futs = {ex.submit(pair_metrics, i): i for i in UNIVERSE}
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
        if len(snap.momentums) < MIN_COVERAGE:
            print(f"[Snapshot] coverage {len(snap.momentums)}/{len(UNIVERSE)} < {MIN_COVERAGE}: SKIP TICK")
            return None
        return snap

    start = time.time()                        # legacy sequential path
    for inst in UNIVERSE:
        if time.time() - start > timeout_sec:
            print("[Snapshot] timeout, skipping remaining pairs")
            break
        put(inst, pair_metrics(inst))
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
_LAST_PROCESSED_BAR_CLOSE: Dict[str, int] = {}


def parse_positions(resp: dict, snap: Optional[Snapshot] = None) -> List[Position]:
    """Pure parser (testable offline). Never drops an open position: hidden exposure is worse
    than missing data. Positions that end up on placeholder values are counted in FALLBACK_STATS."""
    out = []
    for p in resp.get("positions", []):
        net = float(p["long"]["units"]) + float(p["short"]["units"])
        if net == 0:
            continue
        pair = p["instrument"].replace("_", "/")
        atr, rets, fell_back = 0.001, np.array([]), True    # placeholder = fallback
        if SW.USE_REAL_POS_DATA:
            if snap and pair in snap.atrs:
                atr, rets, fell_back = snap.atrs[pair], snap.returns[pair], False
            else:
                res = pair_metrics(p["instrument"])
                if res is not None:
                    atr, rets, fell_back = res[1], res[2], False
        FALLBACK_STATS["seen"] += 1
        if fell_back:
            FALLBACK_STATS["fallback"] += 1
            # Placeholder atr=0.001, rets=[]: legacy exposure = size*atr (e.g. 1000 units -> 1.0 per ccy,
            # wrong magnitude), correlation guard skips it. RISK_UNIT_EXPOSURE=True counts it as 1 risk unit.
            print(f"[WARN] FALLBACK_EXPOSURE {pair}: placeholder ATR/returns in use")
        out.append(Position(pair, "LONG" if net > 0 else "SHORT", abs(net), atr, rets))
    return out


def fetch_open_positions(snap: Optional[Snapshot] = None) -> List[Position]:
    resp = get_client().request(positions_ep.OpenPositions(accountID=account_id()))
    return parse_positions(resp, snap)


def fallback_ratio() -> float:
    return FALLBACK_STATS["fallback"] / FALLBACK_STATS["seen"] if FALLBACK_STATS["seen"] else 0.0


def size_units(risk_amount: float, sl_distance: float) -> int:
    """units = risk / stop distance. risk_amount must be in the pair's QUOTE currency;
    convert yourself for pairs whose quote is not your account currency.
    Raises on bad inputs and when the result is under 1 unit (OANDA would reject it)."""
    if sl_distance is None or sl_distance <= 1e-12:
        raise ValueError(f"size_units: invalid sl_distance={sl_distance}")
    if risk_amount is None or risk_amount <= 0:
        raise ValueError(f"size_units: invalid risk_amount={risk_amount}")
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
                  shadow: Optional[ShadowEngine] = None, now_utc=None):
    # P8 gate: if enabled, only proceed when at least one primary TF just closed
    if SW.PER_PAIR_BAR_CLOSE:
        import datetime as _dt
        if now_utc is None:
            now_utc = _dt.datetime.utcnow()
        closed_tfs = get_just_closed_tfs(now_utc, BAR_CLOSE_TOLERANCE_SEC)
        if not closed_tfs:
            # Not a bar close moment for any tracked TF -> skip tick (caller should sleep)
            return None
        # The caller may invoke decision_tick repeatedly inside the tolerance window.
        # Route each actual bar close at most once per process.
        fresh_tfs = set()
        for tf in closed_tfs:
            close_epoch = _tf_close_epoch(tf, now_utc)
            if close_epoch is not None and _LAST_PROCESSED_BAR_CLOSE.get(tf) != close_epoch:
                fresh_tfs.add(tf)
                _LAST_PROCESSED_BAR_CLOSE[tf] = close_epoch
        if not fresh_tfs:
            return None
        # Filter candidates to those whose primary TF is among just-closed TFs
        # If filtering leaves zero candidates, skip tick as well
        filtered = [c for c in candidates if get_pair_config(c.pair)["primary"] in fresh_tfs or get_pair_config(c.pair)["primary"] == "D" and "D1" in fresh_tfs]
        # If no candidate matches a newly closed primary timeframe, skip this tick.
        if not filtered:
            print(f"[BarClose] no candidates for closed TFs {closed_tfs}: SKIP TICK")
            return None
        candidates = filtered
        print(f"[BarClose] closed TFs {fresh_tfs}, trading {len(candidates)} candidates")

    snap = build_market_snapshot()
    if snap is None:
        return None
    open_positions = fetch_open_positions(snap)
    print(f"[FALLBACK] {FALLBACK_STATS['fallback']}/{FALLBACK_STATS['seen']} positions on placeholders "
          f"({fallback_ratio():.0%}, cumulative)")

    for c in candidates:                        # fill gaps from snapshot
        if len(c.returns_series) == 0 and c.pair in snap.returns:
            c.returns_series = snap.returns[c.pair]
        if not c.atr:
            c.atr = snap.atrs.get(c.pair, 0.0)
    if SW.LIVE_COST and candidates:
        spreads = fetch_spreads(sorted({c.pair.replace("/", "_") for c in candidates}))
        for c in candidates:
            if c.pair in spreads and c.atr:
                c.cost_score = live_cost_score(spreads[c.pair], c.atr)

    best = router.select_best_trade(candidates, snap.momentums, open_positions)
    if shadow is not None:
        shadow.step(candidates, snap.momentums, open_positions)

    if not best.get("pair"):
        print(f"[Tick] No trade. Rejected: {best.get('rejected')}")
        return None
    print(f"[Tick] Best: {best}")
    # sl = SL_ATR_MULT * snap.atrs[best["pair"]]
    # place_market_order(best["pair"].replace("/", "_"), best["side"],
    #                    units=size_units(RISK_AMOUNT, sl), sl_distance=sl)
    return best


# ================= Offline self-test (no OANDA needed) =================
if __name__ == "__main__":
    rng = np.random.default_rng(1)
    pairs = [i.replace("_", "/") for i in UNIVERSE]
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
    print("P8 test passed\nALL SELF-TESTS PASSED")
