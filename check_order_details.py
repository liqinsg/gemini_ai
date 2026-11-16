# check_order_details.py — FIXED: Shows SL/TP Order IDs
import os
import sys
from datetime import datetime, timezone

# 可选参数：a/c id（如 101-003-39389016-006）或 1-based 序号。
# v7 通过 OANDA_ACCOUNT_CLI 桥接进 get_oanda_profile()，优先级最高；
# 必须在下面解析 Profile 的 import 之前写入环境变量。
if len(sys.argv) > 1 and sys.argv[1].strip():
    os.environ["OANDA_ACCOUNT_CLI"] = sys.argv[1].strip()

try:
    # gemini_ai 项目：utils.oanda_execution 暴露模块级 api / OANDA_ACCOUNT_ID
    from utils.oanda_execution import api, OANDA_ACCOUNT_ID
except ImportError:
    # forex_oanda_v7 项目：沿用其原生 Profile（config.config_oanda.get_oanda_profile）
    from config.config_oanda import get_oanda_profile
    _profile = get_oanda_profile()
    api = _profile["oanda_client"]
    OANDA_ACCOUNT_ID = _profile["account_id"]

from oandapyV20.endpoints.positions import OpenPositions
from oandapyV20.endpoints.trades import TradeDetails
from oandapyV20.endpoints.orders import OrderList  # ✅ Added

def get_position_win_loss_message() -> str:
    resp = api.request(OpenPositions(OANDA_ACCOUNT_ID))

    # ✅ Fetch ALL open orders ONCE (faster than per-trade queries)
    all_orders_resp = api.request(OrderList(OANDA_ACCOUNT_ID, params={"state": "PENDING"}))
    all_orders = all_orders_resp.get("orders", [])

    # ✅ Build lookup: tradeID → {sl_order, tp_order}
    trade_orders = {}
    for ord in all_orders:
        tid = ord.get("tradeID")
        if not tid:
            continue
        otype = ord.get("type")  # STOP_LOSS / TAKE_PROFIT / TRAILING_STOP_LOSS
        price = ord.get("price")
        oid = ord.get("id")
        if tid not in trade_orders:
            trade_orders[tid] = {"sl_price": "NONE", "sl_id": "NONE", "tp_price": "NONE", "tp_id": "NONE"}
        if otype == "STOP_LOSS":
            trade_orders[tid]["sl_price"] = price
            trade_orders[tid]["sl_id"] = oid
        elif otype == "TAKE_PROFIT":
            trade_orders[tid]["tp_price"] = price
            trade_orders[tid]["tp_id"] = oid

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = []
    lines.append("=" * 120)
    lines.append(f"📊 OANDA OPEN POSITIONS — DETAILED WIN/LOSS (UNREALIZED P&L) for OANDA A/C {OANDA_ACCOUNT_ID}")
    lines.append(f"🕒 {now}")
    lines.append("=" * 120)

    grand_unrealized = 0.0
    positions_count = 0

    for pos in resp.get("positions", []):
        instr = pos["instrument"]

        for side in ("long", "short"):
            units = float(pos[side]["units"])
            if units == 0:
                continue

            positions_count += 1

            unrealized_pl = float(pos[side].get("unrealizedPL", 0.0))
            grand_unrealized += unrealized_pl

            trade_ids = pos[side].get("tradeIDs", [])
            if not trade_ids:
                lines.append(f"\n➡️ {instr} {side.upper()} has no tradeIDs!")
                continue

            tid = trade_ids[0]
            trade = api.request(TradeDetails(OANDA_ACCOUNT_ID, tid))["trade"]

            entry_price = trade.get("price", "NONE")
            sl = trade.get("stopLossOrder", {}).get("price", "NONE")
            tp = trade.get("takeProfitOrder", {}).get("price", "NONE")

            # ✅ Use lookup from OrderList
            orders_info = trade_orders.get(tid, {})
            sl_id = orders_info.get("sl_id", "NONE")
            tp_id = orders_info.get("tp_id", "NONE")
            sl = orders_info.get("sl_price", sl)
            tp = orders_info.get("tp_price", tp)

            side_label = "LONG" if side == "long" else "SHORT"

            lines.append(f"\n➡️ {instr} {side_label}")
            lines.append(f"   Units (abs): {abs(int(units))} | signed: {units}")
            lines.append(f"   TradeID: {tid}")
            lines.append(f"   Entry: {entry_price}")
            lines.append(f"   SL: {sl}  (orderID: {sl_id})")
            lines.append(f"   TP: {tp}  (orderID: {tp_id})")
            lines.append(f"   Unrealized P&L: {unrealized_pl:.2f}")

    lines.append("\n" + "=" * 120)
    lines.append(f"Summary: positions counted = {positions_count}")
    lines.append(f"Grand TOTAL unrealized P&L: {grand_unrealized:.2f}")
    lines.append("=" * 120)

    return "\n".join(lines)

if __name__ == "__main__":
    # from telegram_message import send_telegram_message
    msg = get_position_win_loss_message()
    print(msg)
    # send_telegram_message(msg)
    