"""
A conservative helper module implementing safety features:
- per-account advisory lock
- lightweight broker-idempotency checks (open trades / pending orders)
- short-poll mitigation helper
- SL/TP guardian scaffold
- verified execute_market_trade wrapper (best-effort)

This module attempts to call existing helpers in `utils.trading_core` if available.
"""

import fcntl
import os
import time
from datetime import datetime
from typing import Optional

from pprint import pprint

try:
    from utils import trading_core
except Exception:
    trading_core = None


def acquire_account_lock(account_id: str) -> Optional[int]:
    """Acquire a non-blocking advisory lock for account. Returns open file descriptor if acquired.
    Caller must keep FD open for process lifetime.
    """
    if not account_id:
        print("[LOCK] no account_id provided; skipping lock")
        return None
    lock_path = f"/tmp/runner_{account_id}.lock"
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(f"[LOCK] acquired {lock_path}")
        return fd
    except BlockingIOError:
        print(f"[LOCK] another process holds lock {lock_path}; exiting")
        try:
            fd.close()
        except Exception:
            pass
        return None


def build_client_extensions(pair: str, action: str, signal_data: dict, version: str = "v3.5") -> dict:
    """Return a simple clientExtensions dict for OANDA orders.
    Tag is deterministic to allow broker-authoritative idempotency checks.
    """
    date = datetime.utcnow().strftime("%Y%m%d")
    tag = f"JPY-STRENGTH_{pair}_{action}_{date}"
    comment = (
        f"{version}|entry={signal_data.get('entry')}|SL={signal_data.get('stop_loss')}"
        f"|TP={signal_data.get('take_profit')}|sig={signal_data.get('reasoning','')[:120]}"
    )
    return {"tag": tag, "comment": comment}


def _get_oanda_open_trades_and_orders(oanda_client, account_id: str) -> tuple:
    """Best-effort fetch of open trades and pending orders. Returns (trades, orders) lists.
    This function tries a few likely wrappers in the repo.
    """
    trades = []
    orders = []
    try:
        if trading_core and hasattr(trading_core, "get_open_trades"):
            trades = trading_core.get_open_trades(account_id)
        elif hasattr(oanda_client, "get_open_trades"):
            trades = oanda_client.get_open_trades(account_id)
    except Exception:
        trades = []
    try:
        if trading_core and hasattr(trading_core, "get_pending_orders"):
            orders = trading_core.get_pending_orders(account_id)
        elif hasattr(oanda_client, "get_pending_orders"):
            orders = oanda_client.get_pending_orders(account_id)
    except Exception:
        orders = []
    return trades, orders


def has_existing_strategy_order(oanda_client, account_id: str, pair: str, action: str, tag_prefix: str) -> bool:
    """Return True if an open trade or pending order exists matching our strategy tag prefix.
    This is a conservative, best-effort check; it looks at tag/comment fields when available.
    """
    try:
        trades, orders = _get_oanda_open_trades_and_orders(oanda_client, account_id)
        tag_check = lambda text: text and text.startswith(tag_prefix)
        # inspect trades
        for t in trades:
            ce = t.get("clientExtensions") or {}
            if tag_check(ce.get("tag")):
                return True
        # inspect pending orders
        for o in orders:
            ce = o.get("clientExtensions") or {}
            if tag_check(ce.get("tag")):
                return True
    except Exception as e:
        print(f"[IDEMP] has_existing_strategy_order check failed: {e}")
    return False


def wait_for_no_existing_order(oanda_client, account_id: str, pair: str, action: str, tag_prefix: str, attempts: int = 3, delay: float = 0.5) -> bool:
    """Short-poll loop: return True when no existing order found; otherwise False after attempts."""
    for i in range(attempts):
        if not has_existing_strategy_order(oanda_client, account_id, pair, action, tag_prefix):
            return True
        time.sleep(delay)
    return False


def execute_market_trade_verified(signal, units_override: int, client_extensions: dict = None, account_id: str = None, oanda_client=None) -> dict:
    """Best-effort wrapper to execute a market trade and then verify SL/TP were attached.
    Returns a status dict with keys: success(bool), info(dict).

    This wrapper calls existing `utils.trading_core.execute_market_trade` if available.
    """
    result = {"success": False, "info": {}}
    try:
        # try to call existing execute_market_trade with client_extensions if supported
        if trading_core and hasattr(trading_core, "execute_market_trade"):
            fn = trading_core.execute_market_trade
            try:
                # preferred call signature
                trade_res = fn(signal, units_override=units_override, client_extensions=client_extensions)
            except TypeError:
                # fallback: older signature without client_extensions
                trade_res = fn(signal, units_override=units_override)
            result["info"]["order_result"] = trade_res
        else:
            print("[EXEC] trading_core.execute_market_trade not found; cannot execute trade here")
            return result

        # Post-fill verification (best-effort): try to attach SL/TP if missing.
        if not oanda_client and trading_core and hasattr(trading_core, "get_oanda_client"):
            try:
                oanda_client = trading_core.get_oanda_client()
            except Exception:
                oanda_client = None

        # If trading_core offers attach helper, use it to ensure SL/TP exist for the pair/trade
        pair = getattr(signal, "pair_to_trade", None) or signal.get("pair_to_trade") if isinstance(signal, dict) else None
        expected_sl = getattr(signal, "stop_loss", None) or (signal.get("stop_loss") if isinstance(signal, dict) else None)
        expected_tp = getattr(signal, "take_profit", None) or (signal.get("take_profit") if isinstance(signal, dict) else None)

        # Wait briefly for state to settle
        time.sleep(0.5)

        if trading_core and hasattr(trading_core, "attach_sl_tp_to_open_trade") and pair and (expected_sl or expected_tp):
            # Find trades for this pair and attempt to attach
            try:
                updated = 0
                trades = []
                if hasattr(trading_core, "get_open_trades"):
                    trades = trading_core.get_open_trades(account_id)
                # fallback: try trading_core.get_open_positions or get_open_trades_for_instrument
                for t in trades:
                    instr = t.get("instrument")
                    if instr == pair:
                        trade_id = t.get("id") or t.get("tradeID") or t.get("tradeId")
                        if trade_id:
                            ok = trading_core.attach_sl_tp_to_open_trade(account_id, trade_id, expected_sl, expected_tp)
                            if ok:
                                updated += 1
                result["info"]["sltp_attached_count"] = updated
            except Exception as e:
                result["info"]["sltp_attach_error"] = str(e)

        result["success"] = True
    except Exception as e:
        result["info"]["error"] = str(e)
    return result


def run_sltp_guardian(oanda_client, account_id: str, managed_pairs: list, sl_pips: int = None, tp_pips: int = None) -> dict:
    """Scan open trades and attempt to attach missing SL/TP legs to trades that belong to our strategy.
    This is intentionally conservative and returns a small summary dict.
    """
    summary = {"checked": 0, "updated": 0, "failed": 0}
    try:
        trades = []
        if trading_core and hasattr(trading_core, "get_open_trades"):
            trades = trading_core.get_open_trades(account_id)
        else:
            # best-effort attempt via oanda_client wrappers
            if oanda_client and hasattr(oanda_client, "get_open_trades"):
                trades = oanda_client.get_open_trades(account_id)

        for t in trades:
            summary["checked"] += 1
            instr = t.get("instrument")
            ce = t.get("clientExtensions") or {}
            tag = ce.get("tag") or ""
            # only manage trades for known instruments or with our strategy tag
            if instr not in managed_pairs and not tag.startswith("JPY-STRENGTH_"):
                continue
            trade_id = t.get("id") or t.get("tradeID") or t.get("tradeId")
            sl = t.get("stopLossOrder")
            tp = t.get("takeProfitOrder")
            sl_missing = not sl
            tp_missing = not tp
            if sl_missing or tp_missing:
                # compute expected from entry price + pips if helper available
                entry_price = float(t.get("price") or t.get("openPrice") or 0)
                expected_sl = None
                expected_tp = None
                if sl_pips and tp_pips and entry_price:
                    # naive price math; real code should use instrument precision
                    expected_sl = entry_price - (sl_pips * 0.01)
                    expected_tp = entry_price + (tp_pips * 0.01)
                try:
                    if trading_core and hasattr(trading_core, "attach_sl_tp_to_open_trade") and trade_id:
                        ok = trading_core.attach_sl_tp_to_open_trade(account_id, trade_id, expected_sl, expected_tp)
                        if ok:
                            summary["updated"] += 1
                        else:
                            summary["failed"] += 1
                except Exception:
                    summary["failed"] += 1
    except Exception as e:
        print(f"[GUARDIAN] error: {e}")
    return summary
