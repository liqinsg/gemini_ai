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
    """

    def __init__(self, oanda_client: Any, oanda_account_id: str):
        self.oanda_client = oanda_client
        self.oanda_account_id = oanda_account_id

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
        if not instrument or not action or action == "HOLD":
            print("[EXEC] No action")
            return False

        if self.get_open_position(instrument):
            print("[EXEC] Already have position")
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
