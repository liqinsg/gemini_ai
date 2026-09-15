"""Post-exit context — derived live from OANDA, no local state directory.

This replaces the retired `state/post_exit_context.py`. The old tracker kept a
local ledger (trade-outcome JSONL + `state/open_clusters.json`) to work out how
a pair last exited and how many losses it had stacked up. That file-backed
approach is gone on purpose:

  * the broker already IS the ledger — OANDA knows when a trade closed, for how
    much, and (via the strategy tag) which strategy closed it;
  * a local `state/` file is CWD-relative by nature: the same runner launched
    from a different working directory silently used a different file, which is
    exactly how duplicate-order guards rot over time.

`PostExitTracker` therefore performs read-only OANDA queries and computes
everything in memory. It writes nothing — there is no `record_exit()` — and it
never raises: any query failure degrades to a neutral context, because its only
consumer (`utils.post_exit_gate`) is an observational/threshold gate and must
never be able to break a trading cycle.

Interface is unchanged for existing callers: `PostExitTracker().get_context(pair)`
returns a dict with `closed_at`, `close_reason`, `tier`, `elapsed_hours` and
`consecutive_failures`.

Attribution rule: a closed trade counts only when its clientExtensions tag
matches this strategy. Trades with NO tag (manual entries, other tooling) are
deliberately NOT attributed here, so the context can read as neutral until this
runner's own tagged trades exist — that is safer than crediting a foreign loss
to this strategy's post-exit hurdle.
"""
from __future__ import annotations

from typing import Any, Optional

import config as _config
from utils import oanda_state


# Canonical strategy tag stamped on this strategy's orders. Matches the runners'
# STRATEGY_TAG_PREFIX; matching is dash/underscore-insensitive inside oanda_state.
STRATEGY_TAG_PREFIX = getattr(_config, "STRATEGY_TAG_PREFIX", oanda_state.STRATEGY_TAG_PREFIX)

NEUTRAL_CLOSE_HISTORY_COUNT = oanda_state.CLOSED_TRADE_HISTORY_COUNT


def _neutral_context(instrument: str, reason: str) -> dict:
    """Context used when the broker cannot be queried this cycle."""
    return {
        "instrument": instrument,
        "closed_at": None,
        "close_reason": None,
        "tier": None,
        "elapsed_hours": 0.0,
        "consecutive_failures": 0,
        "realized_pl": 0.0,
        "source": f"neutral ({reason})",
    }


class PostExitTracker:
    """Read-only, OANDA-backed post-exit context for one account.

    Args:
        strategy_tag: Tag prefix identifying this strategy's trades. Defaults to
            the shared JPY-STRENGTH prefix so trades opened by any version of
            this runner are recognised.
        api_client:   v20 client to query with. Defaults to the shared
                      `utils.trading_core.oanda_client` (resolved lazily so this
                      module stays import-cycle free and test-injectable).
        account_id:   OANDA account id. Defaults to `config.OANDA_ACCOUNT_ID`,
                      resolved at call time because the runners switch accounts
                      per profile.
        count:        How many recent closed trades to inspect per instrument.
    """

    def __init__(
        self,
        strategy_tag: Optional[str] = None,
        api_client: Any = None,
        account_id: Optional[str] = None,
        count: int = NEUTRAL_CLOSE_HISTORY_COUNT,
    ) -> None:
        self.strategy_tag = strategy_tag or STRATEGY_TAG_PREFIX
        self._api_client = api_client
        self._account_id = account_id
        self.count = count

    # ------------------------------------------------------------------
    # Lazy dependencies — never captured at import time
    # ------------------------------------------------------------------
    @property
    def api_client(self):
        if self._api_client is None:
            from utils.trading_core import oanda_client  # local import: avoids cycles

            self._api_client = oanda_client
        return self._api_client

    @property
    def account_id(self) -> str:
        return self._account_id or getattr(_config, "OANDA_ACCOUNT_ID", "")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_context(self, instrument: str) -> dict:
        """Post-exit context for `instrument`, entirely from OANDA's closed trades.

        Never raises: on any failure (network, auth, malformed payload) a neutral
        context is returned so the calling cycle proceeds unaffected.
        """
        try:
            closed_trades = oanda_state.get_closed_trades(
                self.api_client, self.account_id, instrument, count=self.count
            )
            closed_trades = [
                trade
                for trade in closed_trades
                if oanda_state.is_strategy_record(trade, self.strategy_tag)
            ]
        except Exception as exc:  # noqa: BLE001 — observational path must not raise
            print(f"  [POST_EXIT_CONTEXT] {instrument}: OANDA query failed ({exc}) — neutral context")
            return _neutral_context(instrument, "query failed")

        context = oanda_state.build_post_exit_context(closed_trades)
        context["instrument"] = instrument
        return context

    def close_position_is_recent(
        self, instrument: str, window_hours: float = float("inf")
    ) -> bool:
        """True when this strategy closed `instrument` within `window_hours`."""
        context = self.get_context(instrument)
        if context.get("closed_at") is None:
            return False
        return context.get("elapsed_hours", 0.0) <= window_hours
