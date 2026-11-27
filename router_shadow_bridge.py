"""Read-only shadow bridge: GlobalPortfolioRouter (评分前端) vs the runner's
``_pick_global_basket`` (唯一执行器).

Step 3a of docs/Portfolio_Master_Plan.md. HARD GUARANTEES:
  * never places or modifies orders;
  * never mutates runner state (all_results / open_net / held are read-only);
  * any failure returns None and is swallowed by the caller -- execution must
    never be affected.
The only side effect is appending one JSONL record under logs/.

M-2 (裁定): the basket stays the single executor; the router only SCORES.
Data source (裁定): reuses the runner's ``_global_scores`` for currency
strength, fed through the router's own normalization (口径自洽).
Known 3a gaps: ``returns_series`` is still not carried by the runner signal ->
neutral default + explicit data_gaps flag.  Step 3a / B closes the other gap:
``space_score`` / ``cost_score`` are now computed from the signal's real
``atr`` / ``spread`` / ``entry`` instead of being fed neutral 0.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

import global_portfolio_router_v1_1_5 as gpr

_ACTION_TO_SIDE = {"BUY": "LONG", "SELL": "SHORT"}


def _norm_pair(pair: Any) -> str:
    """Runner uses EUR_USD; the router uses EUR/USD."""
    return str(pair).replace("_", "/")


SPACE_REL_CAP = 0.005  # atr/entry == 0.5% -> space_score saturated (Step 3a / B)


def _as_float(x) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def build_candidates(all_results: Sequence[dict]):
    """Map runner group-results into router SignalCandidates.

    Faithful: pair / side / raw_score(=strength_score).
    Step 3a / B: the runner signal now also carries the real ``atr`` / ``spread``
    / ``entry``, so the router is no longer fed neutral scores:
        space_score = clamp((atr/entry) / SPACE_REL_CAP, 0, 1)
        cost_score  = gpr.live_cost_score(spread, atr)
    When ``atr`` is unavailable (e.g. ENABLE_ATR_SLTP=False) both fall back to
    0.0 and are flagged in ``data_gaps["space_cost_missing"]``.
    ``returns_series`` is still not carried by the runner signal -> neutral
    default, counted in ``data_gaps["returns_missing"]``.
    """
    cands: List[gpr.SignalCandidate] = []
    gaps = {"space_cost_missing": 0, "returns_missing": 0, "bad_signal": 0}
    for gr in all_results or []:
        signals = (gr or {}).get("signals", []) or []
        for s in signals:
            s = s or {}
            pair = _norm_pair(s.get("pair", ""))
            side = _ACTION_TO_SIDE.get(s.get("action"))
            if not pair or side is None or "/" not in pair or pair.count("/") != 1:
                gaps["bad_signal"] += 1
                continue
            raw = _as_float(s.get("strength_score"))
            raw = 0.0 if raw is None else raw

            atr = _as_float(s.get("atr"))
            spread = _as_float(s.get("spread"))
            entry = _as_float(s.get("entry"))
            has_atr = bool(atr and atr > 0.0)

            if has_atr and entry and entry > 0.0:
                space_score = _clamp01((atr / entry) / SPACE_REL_CAP)
            else:
                space_score = 0.0
            if has_atr and spread is not None and spread >= 0.0:
                cost_score = gpr.live_cost_score(spread, atr)
            else:
                cost_score = 0.0
            if not has_atr:
                gaps["space_cost_missing"] += 1
            gaps["returns_missing"] += 1

            cands.append(
                gpr.SignalCandidate(
                    pair=pair,
                    side=side,
                    raw_score=raw,
                    space_score=space_score,
                    cost_score=cost_score,
                    timeframe="H1",
                    returns_series=np.array([], dtype=float),
                    atr=atr if has_atr else 0.0,
                )
            )
    return cands, gaps


def build_positions(held: Optional[set]) -> List[gpr.Position]:
    """``held`` is a set of (pair, side) with pair=EUR_USD and side=+/-1 (runner 口径)."""
    positions: List[gpr.Position] = []
    for item in held or set():
        try:
            pair, s = item
        except (TypeError, ValueError):
            continue
        positions.append(
            gpr.Position(
                pair=_norm_pair(pair),
                side="LONG" if int(s) > 0 else "SHORT",
                size=0.0,
                atr=0.0,
                returns_series=np.array([], dtype=float),
                risk_units=1.0,
            )
        )
    return positions


def _basket_view(basket) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for e in basket or []:
        sig = e.get("signal", {}) if isinstance(e, dict) else {}
        action = sig.get("action")
        out.append(
            {
                "pair": _norm_pair(sig.get("pair", "")),
                "side": _ACTION_TO_SIDE.get(action, str(action)),
            }
        )
    return out


def run_shadow(
    all_results,
    global_scores,
    open_net,
    held,
    basket,
    account: Optional[str] = None,
    cycle_id: Optional[str] = None,
    log_dir: Optional[str] = None,
    params=None,
) -> Optional[dict]:
    """Score the same candidates through the router and log the comparison.

    Returns the record (used by tests); returns None on any failure without
    raising. Never touches ``basket``/``all_results``.
    """
    try:
        cands, gaps = build_candidates(all_results)
        positions = build_positions(held)
        router = gpr.GlobalPortfolioRouter(params or gpr.RouterParams())
        best = router.select_best_trade(
            cands,
            pair_momentums={},
            open_positions=positions,
            ccy_strength=dict(global_scores or {}),
        )
        basket_view = _basket_view(basket)
        top_pair, top_side = best.get("pair"), best.get("side")
        agreed = bool(top_pair) and any(
            b["pair"] == top_pair and b["side"] == top_side for b in basket_view
        )
        rejected = best.get("rejected", []) or []
        record = {
            "ts": time.time(),
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "cycle_id": cycle_id or uuid.uuid4().hex,
            "account": account or "unknown",
            "n_candidates": len(cands),
            "n_open_positions": len(positions),
            "runner_open_net": dict(open_net or {}),
            "router": {
                "pair": top_pair,
                "side": top_side,
                "final_score": best.get("final_score"),
                "alignment": best.get("alignment"),
                "strength": best.get("strength"),
                "n_rejected": len(rejected),
                "rejected_head": rejected[:8],
            },
            "basket": basket_view,
            "agreed": agreed,
            "data_gaps": gaps,
            "error": None,
        }
        _append_jsonl(record, account=account, log_dir=log_dir)
        return record
    except Exception as exc:  # read-only: never let the shadow break the cycle
        try:
            _append_jsonl(
                {
                    "ts": time.time(),
                    "ts_utc": datetime.now(timezone.utc).isoformat(),
                    "cycle_id": cycle_id or uuid.uuid4().hex,
                    "account": account or "unknown",
                    "error": repr(exc),
                },
                account=account,
                log_dir=log_dir,
            )
        except Exception:
            pass
        return None


def _append_jsonl(
    record: dict, account: Optional[str] = None, log_dir: Optional[str] = None
) -> str:
    log_dir = log_dir or os.environ.get("ROUTER_SHADOW_LOG_DIR", "logs")
    os.makedirs(log_dir, exist_ok=True)
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    acct = account or "unknown"
    path = os.path.join(log_dir, f"router_shadow_{date}_{acct}.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")
    return path
