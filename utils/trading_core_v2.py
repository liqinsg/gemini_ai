# utils/trading_core_v2.py
from __future__ import annotations

import importlib
from typing import Any


class TradingCore:
    """
    Execution / Operation-only component.

    This class ONLY communicates with an already-constructed OANDA client
    to place orders, manage positions, and perform execution operations.

    BOUNDARY — TradingCore does NOT:
      - create OANDA clients
      - handle or resolve credentials (token / environment)
      - obtain market data (candles / prices)
      - generate, fetch, or refresh operational datasets
      - generate trading signals
      - decide BUY / SELL / HOLD
      - run strategies or orchestration cycles
      - call Google / Gemini / AI model providers
      - re-validate caller-provided data against live market prices

    CALLER owns:
      - OANDA credential sourcing
      - OANDA client construction
      - account_id selection
      - dataset preparation
      - signal / trading decision
      - operation parameters (instrument, action, units, SL, TP, etc.)

    TradingCore receives everything from the caller and operates only
    on caller-provided instructions and the already-wired OANDA client.
    Attributes:
        oanda_client (Any): The OANDA client used for executing trades.
        oanda_account_id (str): The OANDA account ID associated with the client.
        dry_run (bool): If True, no actual trades will be executed.
        market_closed (bool): If True, indicates that the market is currently closed.

    Note: These attributes are set during initialization and control the behavior of the TradingCore instance.
    """
    def __init__(self, oanda_client: Any, oanda_account_id: str, dry_run: bool = False, market_closed: bool = False, **kwargs):
        self.oanda_client = oanda_client
        self.oanda_account_id = oanda_account_id
        self.dry_run = dry_run
        self.market_closed = market_closed

    @staticmethod
    def format_price_for_instrument(price: Any, instrument: str) -> str:
        try:
            numeric_price = float(price)
        except (TypeError, ValueError):
            return str(price)

        return (
            f"{numeric_price:.3f}"
            if instrument.endswith("_JPY")
            else f"{numeric_price:.5f}"
        )

    def get_all_open_positions(self) -> list[dict]:
        positions_module = importlib.import_module("oandapyV20.endpoints.positions")
        req = positions_module.OpenPositions(accountID=self.oanda_account_id)
        self.oanda_client.request(req)
        return req.response.get("positions", [])

    def get_open_position(self, instrument: str) -> dict | None:
        return next(
            (
                p
                for p in self.get_all_open_positions()
                if p.get("instrument") == instrument
            ),
            None,
        )

    def get_all_open_trades(self) -> list[dict]:
        trades_module = importlib.import_module("oandapyV20.endpoints.trades")
        req = trades_module.OpenTrades(accountID=self.oanda_account_id)
        self.oanda_client.request(req)
        return req.response.get("trades", [])

    def get_open_trades_for_instrument(self, instrument: str) -> list[dict]:
        return [
            t for t in self.get_all_open_trades() if t.get("instrument") == instrument
        ]

    def get_trade_details(self, trade_id: str) -> dict:
        trades_module = importlib.import_module("oandapyV20.endpoints.trades")
        resp = self.oanda_client.request(
            trades_module.TradeDetails(self.oanda_account_id, trade_id)
        )
        return self._extract_trade_from_trade_details_response(resp) or {}

    def get_pending_orders(self) -> list[dict]:
        orders_module = importlib.import_module("oandapyV20.endpoints.orders")
        req = orders_module.PendingOrders(accountID=self.oanda_account_id)
        self.oanda_client.request(req)
        return req.response.get("orders", [])

    def attach_sl_tp_to_open_trade(
        self,
        instrument: str,
        stop_loss: float,
        take_profit: float,
        dry_run: bool = False,
    ) -> bool:
        if self.dry_run or self.market_closed:
            reason = "dry-run mode" if self.dry_run else "market closed"
            print(f"⏭️ Skipped: {reason} — [attach_sl_tp_to_open_trade]")
            return
        position = self.get_open_position(instrument)
        if not position:
            print(f"[EXEC] No open position for {instrument}")
            return False

        trade_ids: list[str] = []
        for side in (position.get("long", {}), position.get("short", {})):
            trade_ids.extend(side.get("tradeIDs", []))
        if not trade_ids:
            return False

        trade_id = trade_ids[0]
        sl_str = self.format_price_for_instrument(stop_loss, instrument)
        tp_str = self.format_price_for_instrument(take_profit, instrument)

        if dry_run:
            print(
                f"[DRY-RUN] Would attach SL/TP to {instrument} trade={trade_id} SL={sl_str} TP={tp_str}"
            )
            return True

        trades_mod = importlib.import_module("oandapyV20.endpoints.trades")
        payload = {
            "stopLoss": {"price": sl_str, "timeInForce": "GTC"},
            "takeProfit": {"price": tp_str, "timeInForce": "GTC"},
        }

        try:
            self.oanda_client.request(
                trades_mod.TradeCRCDO(self.oanda_account_id, trade_id, data=payload)
            )
            print(f"[EXEC] SL/TP attached to {instrument}")
            return True
        except Exception as e:
            print(f"[EXEC ERROR] {e}")
            return False

    def attach_sl_tp_to_trade_id(
        self,
        trade_id: str,
        instrument: str,
        stop_loss: float,
        take_profit: float,
        *,
        dry_run: bool = False,
        client_request_id: str | None = None,
    ) -> bool:
        """Attach SL/TP to a SINGLE specific trade by trade_id (SAFE version).

        Unlike attach_sl_tp_to_open_trade which closes by instrument (position-level),
        this method operates on a concrete trade_id and will NEVER touch sibling trades
        on the same instrument that belong to manual entry.
        """
        if self.dry_run or self.market_closed:
            reason = "dry-run mode" if self.dry_run else "market closed"
            print(f"⏭️ Skipped: {reason} — [attach_sl_tp_to_trade_id T{trade_id}]")
            return False

        sl_str = self.format_price_for_instrument(stop_loss, instrument)
        tp_str = self.format_price_for_instrument(take_profit, instrument)

        if dry_run:
            print(
                f"[DRY-RUN] Would attach SL/TP to T{trade_id} {instrument} "
                f"SL={sl_str} TP={tp_str}"
            )
            return True

        trades_mod = importlib.import_module("oandapyV20.endpoints.trades")
        payload: dict[str, Any] = {
            "stopLoss": {"price": sl_str, "timeInForce": "GTC"},
            "takeProfit": {"price": tp_str, "timeInForce": "GTC"},
        }
        if client_request_id:
            payload["clientExtensions"] = {"id": str(client_request_id)}

        try:
            self.oanda_client.request(
                trades_mod.TradeCRCDO(self.oanda_account_id, trade_id, data=payload)
            )
            print(
                f"[EXEC] SL/TP attached to T{trade_id} {instrument} "
                f"(idempotency={client_request_id})"
            )
            return True
        except Exception as e:
            print(f"[EXEC ERROR] attach_sl_tp_to_trade_id T{trade_id}: {e}")
            return False

    def verify_sl_tp_on_trade(self, trade_id: str, instrument: str) -> None:
        try:
            trade = self.get_trade_details(trade_id)
            sl = trade.get("stopLossOrder", {})
            tp = trade.get("takeProfitOrder", {})
            if sl or tp:
                print(f"[VERIFY] SL={sl.get('price')}, TP={tp.get('price')}")
            else:
                print("[VERIFY] No SL/TP found")
        except Exception as e:
            print(f"[VERIFY ERROR] {e}")

    def _extract_trade_from_trade_details_response(self, resp_td: dict) -> dict | None:
        if not isinstance(resp_td, dict):
            return None
        if "trade" in resp_td:
            return resp_td["trade"]
        if (
            "response" in resp_td
            and isinstance(resp_td["response"], dict)
            and "trade" in resp_td["response"]
        ):
            return resp_td["response"]["trade"]
        return resp_td

    def execute_market_trade(
        self,
        instrument: str,
        action: str,
        units: int,
        stop_loss: float,
        take_profit: float,
        *,
        dry_run: bool = False,
        client_extensions: dict | None = None,
    ):
        if self.dry_run or self.market_closed:
            reason = "dry-run mode" if self.dry_run else "market closed"
            print(f"⏭️ Skipped: {reason} — [execute_market_trade]")
            return
        if not instrument or not action or action == "HOLD":
            print("[EXEC] No action")
            return False

        signed_units = str(units) if action == "BUY" else str(-abs(units))

        sl_str = self.format_price_for_instrument(stop_loss, instrument)
        tp_str = self.format_price_for_instrument(take_profit, instrument)

        if dry_run:
            print(
                f"[DRY-RUN] Would {action} {instrument} units={signed_units} SL={sl_str} TP={tp_str}"
            )
            return True

        orders_mod = importlib.import_module("oandapyV20.endpoints.orders")
        payload = {
            "order": {
                "units": signed_units,
                "instrument": instrument,
                "timeInForce": "FOK",
                "type": "MARKET",
                "stopLossOnFill": {"price": sl_str},
                "takeProfitOnFill": {"price": tp_str},
                "clientExtensions": client_extensions or {},
                "tradeClientExtensions": client_extensions or {},
            }
        }

        try:
            resp = self.oanda_client.request(
                orders_mod.OrderCreate(self.oanda_account_id, payload)
            )

            if "orderFillTransaction" not in resp:
                print(f"[EXEC] Order sent but no fill transaction in response: {resp}")
                return False

            fill = resp["orderFillTransaction"]
            trade_id = None
            if fill.get("tradeOpened"):
                trade_id = fill["tradeOpened"].get("tradeID")
            elif fill.get("tradeReduced"):
                trade_id = fill["tradeReduced"].get("tradeID")

            print(
                f"[EXEC] Filled {action} {instrument} @ {fill.get('price')} "
                f"(order id {fill.get('id')}, trade {trade_id})"
            )

            sl_order = fill.get("stopLossOrder") or {}
            tp_order = fill.get("takeProfitOrder") or {}
            if sl_order or tp_order:
                print(
                    f"[EXEC] OANDA returned SL={sl_order.get('price')} TP={tp_order.get('price')}"
                )
            else:
                print(
                    "[EXEC] OANDA fill did NOT include attached SL/TP in response — will verify and attempt repair"
                )

            try:
                trades_mod = importlib.import_module("oandapyV20.endpoints.trades")
                trade_info = None
                if trade_id:
                    resp_td = self.oanda_client.request(
                        trades_mod.TradeDetails(self.oanda_account_id, trade_id)
                    )
                    trade_info = self._extract_trade_from_trade_details_response(
                        resp_td
                    )

                verified_sl = trade_info.get("stopLossOrder", {}) if trade_info else {}
                verified_tp = (
                    trade_info.get("takeProfitOrder", {}) if trade_info else {}
                )

                missing_sl = (not verified_sl) or (not verified_sl.get("id"))
                missing_tp = (not verified_tp) or (not verified_tp.get("id"))

                if missing_sl or missing_tp:
                    if trade_id:
                        attached = self.attach_sl_tp_to_trade_id(
                            trade_id=trade_id,
                            instrument=instrument,
                            stop_loss=stop_loss,
                            take_profit=take_profit,
                            dry_run=dry_run,
                            client_request_id=f"POSTFILL_SLTP_{instrument}_T{trade_id}",
                        )
                    else:
                        attached = self.attach_sl_tp_to_open_trade(
                            instrument, stop_loss, take_profit, dry_run=dry_run
                        )
                    if attached:
                        print(
                            f"[EXEC] SL/TP attach attempt succeeded for {instrument} (trade {trade_id})"
                        )
                    else:
                        print(
                            f"[EXEC] SL/TP attach attempt FAILED for {instrument} (trade {trade_id})"
                        )

                    if trade_id:
                        resp_td2 = self.oanda_client.request(
                            trades_mod.TradeDetails(self.oanda_account_id, trade_id)
                        )
                        trade_info2 = self._extract_trade_from_trade_details_response(
                            resp_td2
                        )
                        sl2 = (
                            trade_info2.get("stopLossOrder", {}) if trade_info2 else {}
                        )
                        tp2 = (
                            trade_info2.get("takeProfitOrder", {})
                            if trade_info2
                            else {}
                        )
                        print(
                            f"[EXEC VERIFY] After attach: SL={sl2.get('price')} TP={tp2.get('price')}"
                        )
            except Exception as e:
                print(f"[EXEC VERIFY ERROR] {e}")

            return True

        except Exception as e:
            print(f"[EXEC ERROR] {e}")
            return False

    def close_position(self, instrument: str, dry_run: bool = False) -> bool:
        if self.dry_run or self.market_closed:
            reason = "dry-run mode" if self.dry_run else "market closed"
            print(f"⏭️ Skipped: {reason} — [close_position]")
            return
        positions_mod = importlib.import_module("oandapyV20.endpoints.positions")
        try:
            pos_req = positions_mod.OpenPositions(accountID=self.oanda_account_id)
            self.oanda_client.request(pos_req)

            position = next(
                (
                    p
                    for p in pos_req.response.get("positions", [])
                    if p.get("instrument") == instrument
                ),
                None,
            )
            if not position:
                print(f"[CLOSE] No open position for {instrument}")
                return False

            long_units = int(float(position.get("long", {}).get("units", 0)))
            short_units = int(float(position.get("short", {}).get("units", 0)))

            payload = {}
            if long_units > 0:
                payload["longUnits"] = str(long_units)
            if short_units < 0:
                payload["shortUnits"] = str(abs(short_units))

            if dry_run:
                print(f"[DRY-RUN] Would CLOSE {instrument} payload={payload}")
                return True

            req = positions_mod.PositionClose(
                accountID=self.oanda_account_id,
                instrument=instrument,
                data=payload,
            )
            self.oanda_client.request(req)
            print(f"[CLOSE] Closed {instrument}")
            return True

        except Exception as e:
            print(f"[CLOSE ERROR] {e}")
            return False

    def close_trade_by_id(self, trade_id: str, client_request_id: str | None = None
                          ) -> tuple[bool, dict[str, Any]]:
        """
        Close a SINGLE open trade by its trade_id (not position_id / instrument).

        Returns: (success: bool, info: dict)
            info may include keys:
              realizedPL, units, price, instrument, full_response
            On failure info contains {"error": ...}.
        """
        if self.dry_run or self.market_closed:
            reason = "dry-run mode" if self.dry_run else "market closed"
            print(f"⏭️ Skipped: {reason} — [close_trade_by_id T{trade_id}]")
            return False, {"skipped": reason}

        trades_mod = importlib.import_module("oandapyV20.endpoints.trades")
        data: dict[str, Any] = {"units": "ALL"}
        params = {"data": data}
        if client_request_id:
            headers = {"oanda-client-extension-id": str(client_request_id)}
            params["headers"] = headers

        try:
            req = trades_mod.TradeClose(
                accountID=self.oanda_account_id,
                tradeID=trade_id,
                **params,
            )
            resp = self.oanda_client.request(req)
            print(f"[EXEC] Close trade T{trade_id} sent (idempotency={client_request_id})")
            info: dict[str, Any] = {"full_response": resp}
            try:
                tc = resp.get("tradesClosed") if isinstance(resp, dict) else None
                if isinstance(tc, list) and len(tc) > 0:
                    first = tc[0]
                    info["realizedPL"] = first.get("realizedPL")
                    info["units"] = first.get("units")
                    info["price"] = first.get("price")
                lo = resp.get("longOrderFillTransaction") if isinstance(resp, dict) else None
                so = resp.get("shortOrderFillTransaction") if isinstance(resp, dict) else None
                for fill in (lo, so):
                    if not isinstance(fill, dict):
                        continue
                    if "pl" in fill and "realizedPL" not in info:
                        info["realizedPL"] = fill.get("pl")
                    if "instrument" in fill and "instrument" not in info:
                        info["instrument"] = fill.get("instrument")
            except Exception:
                pass
            return True, info
        except Exception as e:
            print(f"[EXEC ERROR] close_trade_by_id T{trade_id}: {e}")
            return False, {"error": str(e)}

    def update_trade_sl_only(self, trade_id: str, new_sl: float, client_request_id: str | None = None) -> bool:
        """Update ONLY the StopLoss order on a trade by trade_id. Uses TradeCRCDO PUT."""
        if self.dry_run or self.market_closed:
            reason = "dry-run mode" if self.dry_run else "market closed"
            print(f"⏭️ Skipped: {reason} — [update_trade_sl_only T{trade_id}]")
            return False

        try:
            trade = self.get_trade_details(trade_id)
            instrument = trade.get("instrument", "")
        except Exception:
            instrument = ""

        sl_str = self.format_price_for_instrument(new_sl, instrument) if instrument else f"{new_sl:.5f}"

        trades_mod = importlib.import_module("oandapyV20.endpoints.trades")
        payload: dict[str, Any] = {
            "stopLoss": {"price": sl_str, "timeInForce": "GTC"},
        }
        if client_request_id:
            payload["clientExtensions"] = {"id": str(client_request_id)}

        try:
            self.oanda_client.request(
                trades_mod.TradeCRCDO(
                    self.oanda_account_id, trade_id, data=payload
                )
            )
            print(f"[EXEC] SL updated on T{trade_id} → {sl_str} (idempotency={client_request_id})")
            return True
        except Exception as e:
            print(f"[EXEC ERROR] update_trade_sl_only T{trade_id} SL={sl_str}: {e}")
            return False

    def get_instrument_spec_raw(self, instrument: str) -> dict:
        """OANDA v20 0.7.2 适配版 — AccountInstruments 端点 / fail-fast / 统一兜底"""
        accounts_mod = importlib.import_module("oandapyV20.endpoints.accounts")
        try:
            req = accounts_mod.AccountInstruments(
                self.oanda_account_id,
                params={"instruments": instrument},
            )
            self.oanda_client.request(req)
            instruments = req.response.get("instruments", [])
            if not instruments:
                raise ValueError("Empty instruments list returned")
            info = instruments[0]
            tick_size = float(info.get("tickSize", 0))
            min_stop_dist = float(info.get("minimumStopLossDistance", 0))
            if tick_size <= 0 or min_stop_dist <= 0:
                raise ValueError(
                    f"invalid spec values: tick={tick_size}, min_stop={min_stop_dist}"
                )
            return {
                "tick_size": tick_size,
                "min_stop_distance": min_stop_dist,
            }
        except Exception as e:
            if "JPY" in instrument:
                fallback_tick, fallback_dist = 0.01, 0.05
            else:
                fallback_tick, fallback_dist = 0.0001, 0.0005
            print(f"[SPEC-FALLBACK] {instrument}: using defaults tick={fallback_tick} dist={fallback_dist} — reason: {e}")
            return {
                "tick_size": fallback_tick,
                "min_stop_distance": fallback_dist,
            }

    def get_bid_ask(self, instrument: str) -> tuple[float | None, float | None]:
        """Return (bid, ask) for an instrument via OANDA pricing endpoint."""
        pricing_mod = importlib.import_module("oandapyV20.endpoints.pricing")
        try:
            req = pricing_mod.PricingInfo(
                accountID=self.oanda_account_id,
                params={"instruments": instrument},
            )
            self.oanda_client.request(req)
            prices = req.response.get("prices", [])
            if not prices:
                return None, None
            p = prices[0]
            bids = p.get("bids", [])
            asks = p.get("asks", [])
            bid = float(bids[0]["price"]) if bids else None
            ask = float(asks[0]["price"]) if asks else None
            return bid, ask
        except Exception as e:
            print(f"[EXEC WARN] get_bid_ask {instrument}: {e}")
            return None, None

    def query_trade_raw(self, trade_id: str) -> tuple[str, float]:
        """Low-level state query: returns ('EXISTS'|'CLOSED'|'UNKNOWN', remaining_units)."""
        trades_mod = importlib.import_module("oandapyV20.endpoints.trades")
        try:
            resp = self.oanda_client.request(
                trades_mod.TradeDetails(self.oanda_account_id, trade_id)
            )
            trade = self._extract_trade_from_trade_details_response(resp) or {}
            state_raw = str(trade.get("state", "")).upper()
            units = float(trade.get("currentUnits", 0) or 0)
            if state_raw == "CLOSED" or units == 0.0:
                return "CLOSED", 0.0
            if state_raw in ("OPEN",):
                return "EXISTS", units
            if state_raw:
                return "EXISTS", units
            return "UNKNOWN", units
        except Exception as e:
            msg = str(e).lower()
            if "404" in msg or "not found" in msg or "no such" in msg or "does not exist" in msg:
                return "CLOSED", 0.0
            print(f"[EXEC WARN] query_trade_raw T{trade_id}: {e} → UNKNOWN")
            return "UNKNOWN", 0.0

    def diagnose_pair_position(self, pair: str):
        print(f"\n{'=' * 60}")
        print(f"[DIAGNOSTIC] {pair}")
        print(f"{'=' * 60}")

        try:
            trades_module = importlib.import_module("oandapyV20.endpoints.trades")
            req = trades_module.OpenTrades(accountID=self.oanda_account_id)
            self.oanda_client.request(req)
            trades = req.response.get("trades", [])
            print(f"\n[OPEN TRADES] Total: {len(trades)}")
            for trade in trades:
                if trade.get("instrument") != pair:
                    continue
                print(
                    {
                        "id": trade.get("id"),
                        "instrument": trade.get("instrument"),
                        "currentUnits": trade.get("currentUnits"),
                        "openTime": trade.get("openTime"),
                        "clientExtensions": trade.get("clientExtensions"),
                        "stopLossOrder": trade.get("stopLossOrder"),
                        "takeProfitOrder": trade.get("takeProfitOrder"),
                    }
                )
        except Exception as exc:
            print(f"[OPEN TRADES ERROR] {exc}")

        try:
            positions_module = importlib.import_module("oandapyV20.endpoints.positions")
            req = positions_module.OpenPositions(accountID=self.oanda_account_id)
            self.oanda_client.request(req)
            positions = req.response.get("positions", [])
            print(f"\n[OPEN POSITIONS] Total: {len(positions)}")
            for position in positions:
                if position.get("instrument") != pair:
                    continue
                print(
                    {
                        "instrument": position.get("instrument"),
                        "long": position.get("long"),
                        "short": position.get("short"),
                    }
                )
        except Exception as exc:
            print(f"[OPEN POSITIONS ERROR] {exc}")


def close_pair_position(trading_core: TradingCore, instrument: str) -> tuple[bool, dict]:
    pos = trading_core.get_open_position(instrument)
    if not pos:
        return True, {"status": "already_flat"}
    ok = trading_core.close_position(instrument=instrument)
    if ok:
        return True, {"status": "closed"}
    return False, {"status": "failed"}


def emergency_close_all_jpy(
    trading_core: TradingCore,
    require_practice_check: bool = True,
    env: str = "practice",
    set_lock: bool = True,
    lock_setter=None,
) -> dict:
    acct = trading_core.oanda_account_id
    if not acct:
        print("  [EXEC][EMERGENCY] missing account id for emergency close")
        return {"found": 0, "closed": 0, "failed": 0, "details": []}

    if require_practice_check and env != "practice":
        print(
            f"  [EXEC][EMERGENCY] WARNING: env={env} (not practice). Aborting emergency close."
        )
        return {"found": 0, "closed": 0, "failed": 0, "details": []}

    print("\n" + "!" * 60)
    print(
        "[EXEC][EMERGENCY] Initiating EMERGENCY CLOSE ALL JPY positions — BYPASSING strategy filters (v144)"
    )
    print(f"[EXEC][EMERGENCY] Account: {acct}")

    found = closed = failed = 0
    details = []
    try:
        open_positions = trading_core.get_all_open_positions()
        for position in open_positions:
            instrument = position.get("instrument")
            if not instrument:
                continue
            if "_JPY" not in instrument:
                details.append({"instrument": instrument, "status": "skipped_not_jpy"})
                continue
            found += 1
            long_units = int(float(position.get("long", {}).get("units", 0)))
            short_units = int(float(position.get("short", {}).get("units", 0)))
            if long_units == 0 and short_units == 0:
                details.append({"instrument": instrument, "status": "already_flat"})
                continue

            try:
                print(f"  [EXEC][EMERGENCY] Closing {instrument}")
                ok = trading_core.close_position(instrument=instrument)
                if ok:
                    print(f"  [EXEC][EMERGENCY] Closed {instrument}")
                    closed += 1
                    details.append({"instrument": instrument, "status": "closed"})
                else:
                    print(f"  [EXEC][EMERGENCY] Failed to close {instrument}")
                    failed += 1
                    details.append({"instrument": instrument, "status": "failed"})
            except Exception as exc:
                print(f"  [EXEC][EMERGENCY] Exception closing {instrument}: {exc}")
                failed += 1
                details.append(
                    {"instrument": instrument, "status": "failed", "error": str(exc)}
                )
    except Exception as exc:
        print(f"  [EXEC][EMERGENCY] emergency enumeration failed: {exc}")
        return {"found": found, "closed": closed, "failed": failed, "details": details}

    if set_lock and lock_setter:
        try:
            lock_setter(info=f"emergency_close_all_jpy_v144 account={acct}")
        except Exception:
            pass

    print("\n" + "!" * 60)
    print("EMERGENCY CLOSE REPORT (v144)")
    print("!" * 60)
    print(f"  Account: {acct}")
    print(f"  JPY instruments found: {found} | Closed: {closed} | Failed: {failed}")
    for detail in details:
        print(f"    - {detail.get('instrument', '?')}: {detail.get('status')}")

    return {"found": found, "closed": closed, "failed": failed, "details": details}
