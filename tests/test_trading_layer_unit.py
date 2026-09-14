"""
Unit Test — Trading Layer Ownership & Lifecycle
✅ Mock OANDA, NO network, NO real account
✅ Verify: idempotency + ownership check BEFORE calling OANDA
"""
import pytest
from unittest.mock import MagicMock, patch

# ── 被测 Trading Layer 封装 ──
TAG = "JPY-STRENGTH_GBP_JPY_BUY_20260914"
COMMENT = "v1.4.4|entry=208.20|SL=204.80|TP=213.30"
INSTRUMENT = "GBP_JPY"
UNITS = 10000
SL = "204.80"
TP = "213.30"

class TradingLayer:
    """Bot trading layer — YOUR idempotency + ownership logic"""
    def __init__(self, api):
        self.api = api
        self.open_trades = {}  # instrument → {trade_id, tag, comment}

    def open_trade(self):
        """1. Open: validate → build order → call OANDA"""
        if INSTRUMENT in self.open_trades:
            return None, "IDEMPOTENT_REJECT"  # Bot层直接拒，不打OANDA

        order = {
            "order": {
                "type": "MARKET",
                "instrument": INSTRUMENT,
                "units": str(UNITS),
                "positionFill": "DEFAULT",
                "clientExtensions": {"tag": TAG, "comment": COMMENT},
                "stopLossOnFill": {"price": SL, "timeInForce": "GTC"},
                "takeProfitOnFill": {"price": TP, "timeInForce": "GTC"},
            }
        }
        resp = self.api.request(order)
        trade_id = resp["tradeOpened"]["tradeID"]
        self.open_trades[INSTRUMENT] = {"trade_id": trade_id, "tag": TAG, "comment": COMMENT}
        return trade_id, "SUCCESS"

    def modify_sltp(self, new_sl, new_tp):
        """3. Modify: ownership check → call OANDA (no entry re-validate)"""
        if INSTRUMENT not in self.open_trades:
            return "NO_TRADE", None
        t = self.open_trades[INSTRUMENT]
        body = {"stopLoss": {"price": new_sl}, "takeProfit": {"price": new_tp}}
        resp = self.api.request({"action": "MODIFY", "trade_id": t["trade_id"], "body": body})
        return "SUCCESS", resp

    def close_trade(self, req_tag, req_comment):
        """4/5. Close: OWNERSHIP check FIRST → only call OANDA if match"""
        if INSTRUMENT not in self.open_trades:
            return "NO_TRADE", None
        t = self.open_trades[INSTRUMENT]
        if t["tag"] != req_tag or t["comment"] != req_comment:
            return "OWNERSHIP_REJECT", None  # ❌ Bot层拒，不打OANDA
        # ✅ Match → proceed to OANDA
        resp = self.api.request({"action": "CLOSE", "instrument": INSTRUMENT})
        del self.open_trades[INSTRUMENT]
        return "SUCCESS", resp

# ── MOCKED UNIT TESTS ──
class TestTradingLayerUnit:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.mock_api = MagicMock()
        self.trade = TradingLayer(self.mock_api)
        self.mock_api.request.return_value = {"tradeOpened": {"tradeID": "6999"}}

    def test1_open_sets_client_extensions(self):
        """✅ 1. Order has correct clientExtensions + SL/TP fields"""
        tid, status = self.trade.open_trade()
        assert status == "SUCCESS" and tid == "6999"
        order_sent = self.mock_api.request.call_args[0][0]
        assert order_sent["order"]["clientExtensions"]["tag"] == TAG
        assert order_sent["order"]["stopLossOnFill"]["price"] == SL
        assert order_sent["order"]["takeProfitOnFill"]["price"] == TP

    def test2_duplicate_open_rejected_no_oanda_call(self):
        """✅ 2. Duplicate OPEN → Bot rejects, NO second OANDA call"""
        self.trade.open_trade()
        call_count_after_first = self.mock_api.request.call_count
        # Attempt duplicate
        tid, status = self.trade.open_trade()
        assert status == "IDEMPOTENT_REJECT"
        assert self.mock_api.request.call_count == call_count_after_first, \
            "❌ SHOULD NOT CALL OANDA TWICE"

    def test3_modify_allowed_calls_oanda(self):
        """✅ 3. Modify allowed → calls OANDA, no entry re-check"""
        self.trade.open_trade()
        self.mock_api.request.reset_mock()
        status, _ = self.trade.modify_sltp("204.50", "213.00")
        assert status == "SUCCESS"
        assert self.mock_api.request.call_count == 1, "❌ MODIFY SHOULD CALL OANDA"

    def test4_wrong_tag_close_rejected_no_oanda_call(self):
        """✅ 4. Wrong tag → Bot rejects, NO OANDA CLOSE CALLED"""
        self.trade.open_trade()
        self.mock_api.request.reset_mock()
        status, _ = self.trade.close_trade("WRONG-TAG", "bad-comment")
        assert status == "OWNERSHIP_REJECT"
        assert self.mock_api.request.call_count == 0, "❌ WRONG-TAG SHOULD NOT CALL OANDA"

    def test5_correct_tag_close_calls_oanda(self):
        """✅ 5. Correct tag → passes check, CALLS OANDA CLOSE"""
        self.trade.open_trade()
        self.mock_api.request.reset_mock()
        status, _ = self.trade.close_trade(TAG, COMMENT)
        assert status == "SUCCESS"
        assert self.mock_api.request.call_count == 1, "❌ CORRECT-TAG SHOULD CALL OANDA"
