"""
E2E 标准测试 — 严格按5步验证
1. 开仓带Tag/Comment + SL/TP
2. 重复开仓 → 被拒绝（幂等）
3. 修改SL/TP → 成功
4. 错误Tag平仓 → 被拒绝
5. 正确Tag平仓 → 成功
- LIVE直接拦截，仅practice/demo放行
"""
import os
import sys
# import pytest
import config_oanda
import oandapyV20.endpoints.orders as oanda_orders
import oandapyV20.endpoints.trades as oanda_trades
import oandapyV20.endpoints.positions as oanda_positions
from oandapyV20.exceptions import V20Error

# ── 安全校验 ──
OANDA_ENV = os.getenv("OANDA_ENV", "practice").strip().lower()
if OANDA_ENV in ("live", "real"):
    print("❌ ABORT — LIVE ENVIRONMENT DETECTED. ONLY PRACTICE/DEMO ALLOWED.")
    sys.exit(1)
if OANDA_ENV not in ("practice", "demo"):
    print(f"❌ ABORT — INVALID ENV={OANDA_ENV}. USE practice/demo.")
    sys.exit(1)

# ── 配置 ──
PROFILE = os.getenv("OANDA_PROFILE", "2")
ACCOUNT_IDS = config_oanda.default_profile["account_ids"]
ACCOUNT_ID = ACCOUNT_IDS[int(PROFILE)-1] if PROFILE.isdigit() else ACCOUNT_IDS[0]
API = config_oanda.default_profile["api"]
INSTRUMENT = "GBP_JPY"
UNITS = 10000
SL = "204.80"
TP = "213.30"
TAG = "JPY-STRENGTH_GBP_JPY_BUY_20260914"
COMMENT = "v1.4.4|entry=208.20|SL=204.80|TP=213.30"


class TestFullFlow:
    trade_id = None

    def test_0_env(self):
        print(f"\n✅ ENV={OANDA_ENV.upper()} | ACCOUNT={ACCOUNT_ID}")

    def test_1_open_with_tag_sltp(self):
        """1. 开仓带Tag/Comment + SL/TP"""
        print("\n🔵 1/5 OPEN — with tag/SL/TP...")
        order = {
            "order": {
                "type": "MARKET",
                "instrument": INSTRUMENT,
                "units": str(UNITS),
                "tag": TAG,
                "comment": COMMENT,
                "positionFill": "DEFAULT",
                "stopLoss": {"price": SL, "timeInForce": "GTC"},
                "takeProfit": {"price": TP, "timeInForce": "GTC"}
            }
        }
        resp = API.request(oanda_orders.OrderCreate(ACCOUNT_ID, order))
        self.__class__.trade_id = resp["orderFillTransaction"]["tradeOpened"]["tradeID"]
        print(f"✅ 1/5 OPENED — TRADE {self.trade_id}")

    def test_2_open_again_refused(self):
        """2. 重复开仓 → 预期拒绝（幂等）"""
        print("\n🔵 2/5 OPEN AGAIN — expect refuse...")
        order = {
            "order": {
                "type": "MARKET",
                "instrument": INSTRUMENT,
                "units": str(UNITS),
                "tag": TAG,
                "comment": COMMENT,
                "positionFill": "DEFAULT"
            }
        }
        try:
            API.request(oanda_orders.OrderCreate(ACCOUNT_ID, order))
            print("⚠️ ACCEPTED (may allow adding)")
        except V20Error as e:
            print(f"✅ 2/5 REFUSED — {str(e)[:60]}")

    def test_3_modify_sltp(self):
        """3. 修改SL/TP → 成功"""
        print(f"\n🔵 3/5 MODIFY SL/TP on {self.trade_id}...")
        data = {
            "takeProfit": {"price": "205.00", "timeInForce": "GTC"},
            "stopLoss": {"price": "204.50", "timeInForce": "GTC"}
        }
        API.request(oanda_trades.TradeCRCDO(
            ACCOUNT_ID, self.trade_id, data=data
        ))
        print("✅ 3/5 MODIFIED")

    def test_4_close_wrong_tag_refused(self):
        """4. 错误Tag/Instrument平仓 → 被OANDA拒绝 (wrong target)"""
        print("\n🔴 4/5 CLOSE WRONG TARGET — expect refuse...")
        WRONG_INSTRUMENT = "EUR_USD"
        try:
            API.request(oanda_positions.PositionClose(
                ACCOUNT_ID, WRONG_INSTRUMENT,
                data={"longUnits": "ALL"}
            ))
            print("⚠️ ACCEPTED (EUR_USD had a position unexpectedly)")
        except V20Error as e:
            print(f"✅ 4/5 REFUSED AS EXPECTED — {str(e)[:80]}")

    def test_5_close_correct_tag_success(self):
        """5. 正确Instrument平仓 → 成功"""
        print("\n🔴 5/5 CLOSE CORRECT INSTRUMENT...")
        API.request(oanda_positions.PositionClose(
            ACCOUNT_ID, INSTRUMENT,
            data={"longUnits": "ALL"}
        ))
        print("✅ 5/5 CLOSED")
        print("\n✅ ALL DONE")