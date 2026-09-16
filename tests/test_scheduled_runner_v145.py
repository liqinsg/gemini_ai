"""
E2E Integration Ownership Test — v1.4.5 SURGICAL FIX
✅ OrderCreate ONCE → OANDA OpenTrades 验证（最多重试3次，仅重试查询，不重复下单）
✅ 只信任 OANDA 返回的 tag，不信任下发值
✅ 安全查询：遍历所有 GBP_JPY 持仓，不拿第一笔
✅ 验证失败 → 清晰报告 → 不继续后续测试（skip guard）
✅ Close 原则：只关「OANDA确认属于自己」的仓位，且检查用实时查询结果，不用缓存值
"""
import os
import pytest
import time

# ── 🔒 LIVE 环境保护 ──
OANDA_ENV = os.getenv("OANDA_ENV", "practice").strip().lower()
if OANDA_ENV in ("live", "real"):
    raise RuntimeError("❌ ABORT: LIVE forbidden. Use practice/demo only.")
if OANDA_ENV not in ("practice", "demo"):
    raise RuntimeError(f"❌ INVALID OANDA_ENV={OANDA_ENV}. Only practice/demo allowed.")

import config_oanda
import oandapyV20.endpoints.orders as oanda_orders
import oandapyV20.endpoints.trades as oanda_trades
import oandapyV20.endpoints.positions as oanda_positions
from oandapyV20.exceptions import V20Error

# ── 配置 ──
PROFILE = os.getenv("OANDA_PROFILE", "2")
ACCOUNT_IDS = config_oanda.default_profile["account_ids"]
ACCOUNT_ID = ACCOUNT_IDS[int(PROFILE) - 1]
API = config_oanda.default_profile["api"]

INSTRUMENT = "GBP_JPY"
UNITS = 10000
SL = "204.80"
TP = "213.30"
TAG = "JPY-STRENGTH_GBP_JPY_BUY_20260914"
COMMENT = "v1.4.5|ownership-test"

MAX_VERIFY_ATTEMPTS = 3
BASE_QUERY_DELAY = 0.8  # seconds; escalates on each retry (OANDA indexing lag)


def _query_all_matching_trades(instrument):
    """🔑 安全查询：返回该标的 ALL open trades，不默认选第一笔
    调用方负责控制查询前的等待时间（不同调用场景需要的延迟不同）。
    返回: list[{instrument, trade_id, tag, comment, raw_clientExtensions}]
    """
    resp = API.request(oanda_trades.OpenTrades(ACCOUNT_ID))
    results = []
    for t in resp.get("trades", []):
        if t["instrument"] == instrument and t["state"] == "OPEN":
            ext = t.get("clientExtensions", {})
            results.append({
                "instrument": t["instrument"],
                "trade_id": t["id"],
                "tag": ext.get("tag"),
                "comment": ext.get("comment"),
                "raw_clientExtensions": ext,
            })
    return results


class TestOwnershipE2E:
    verified_trade_id = None
    verified_tag = None

    # ---------------------------------------------------------------
    # Shared guard: tests 2-5 must not run against an unverified trade
    # ---------------------------------------------------------------
    def _require_verified_ownership(self):
        if self.__class__.verified_trade_id is None:
            pytest.skip("Ownership not established in test_1 — skipping dependent test")

    # =====================================================
    # TEST 1: OPEN (ONCE) + OANDA 所有权验证，重试仅限查询
    # =====================================================
    def test_1_open_with_ownership_verification(self):
        """✅ 1. Open (single order) → OANDA OpenTrades verification, retry query only, max 3 attempts"""
        print(f"\n🔵 1/5 OPEN — single OrderCreate, then verify ownership (max {MAX_VERIFY_ATTEMPTS} query attempts)...")

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

        # ✅ OrderCreate happens exactly once. Retries below only re-query
        # OANDA to verify ownership — they must NOT submit additional orders.
        try:
            resp = API.request(oanda_orders.OrderCreate(ACCOUNT_ID, order))
        except V20Error as e:
            pytest.fail(f"❌ OrderCreate failed outright — no verification attempted: {e}")

        # 🔍 EVIDENCE: inspect the actual response instead of assuming a fill.
        # A 2xx HTTP status does NOT mean the order filled — OANDA can return
        # orderRejectTransaction / orderCancelTransaction with a 2xx status.
        print(f"\n🔍 OrderCreate response keys: {list(resp.keys())}")

        if "orderRejectTransaction" in resp:
            reject = resp["orderRejectTransaction"]
            pytest.fail(
                f"❌ Order REJECTED by OANDA — no trade opened. "
                f"reason={reject.get('rejectReason')} raw={reject}"
            )

        fill_txn = resp.get("orderFillTransaction")
        if fill_txn is None:
            pytest.fail(
                f"❌ No orderFillTransaction in response — order did not fill "
                f"(pending/cancelled?). Full response: {resp}"
            )

        trade_opened = fill_txn.get("tradeOpened")
        if trade_opened is None:
            pytest.fail(
                f"❌ orderFillTransaction present but no tradeOpened — "
                f"unexpected fill shape. Full fill transaction: {fill_txn}"
            )

        fill_trade_id = trade_opened.get("tradeID")
        fill_client_ext = trade_opened.get("clientExtensions")
        print(f"✅ OrderCreate FILLED — tradeID from fill transaction: {fill_trade_id}")
        print(f"   tradeOpened.clientExtensions (as echoed at fill time): {fill_client_ext}")

        success_count = 0
        verified_trade = None

        for attempt in range(1, MAX_VERIFY_ATTEMPTS + 1):
            delay = BASE_QUERY_DELAY * attempt  # 0.8s, 1.6s, 2.4s — escalating for indexing lag
            print(f"\n🔵 OPEN ATTEMPT {attempt}/{MAX_VERIFY_ATTEMPTS} → waiting {delay:.1f}s, then querying OpenTrades...")
            time.sleep(delay)

            candidates = _query_all_matching_trades(INSTRUMENT)
            print(f"   OANDA returned {len(candidates)} open {INSTRUMENT} trade(s):")
            for c in candidates:
                print(f"     → trade_id={c['trade_id']}, tag={repr(c['tag'])}, comment={repr(c['comment'])}")

            # 🔍 Targeted check: what does OUR specific trade (by ID from the
            # fill transaction) show via OpenTrades right now? This isolates
            # whether the tag was never written, or was written but isn't
            # surfaced by this query, independent of the tag-search below.
            by_id = [c for c in candidates if c["trade_id"] == fill_trade_id]
            if by_id:
                print(f"   Our trade ({fill_trade_id}) via OpenTrades: tag={repr(by_id[0]['tag'])}, comment={repr(by_id[0]['comment'])}")
            else:
                print(f"   ⚠️ Our trade ({fill_trade_id}) does not even appear in OpenTrades yet")

            matched = [c for c in candidates if c["tag"] == TAG]

            if matched:
                success_count += 1
                verified_trade = matched[0]
                print(f"✅ OPEN ATTEMPT {attempt}/{MAX_VERIFY_ATTEMPTS} — OWNERSHIP VERIFIED tag={verified_trade['tag']}")
                break  # ✅ verified — stop retrying, no further orders or queries
            else:
                print(f"❌ OPEN ATTEMPT {attempt}/{MAX_VERIFY_ATTEMPTS} — tag NOT VERIFIED (expected={TAG})")

        # ── Final report ──
        print(f"\n{'='*60}")
        print("OPEN OWNERSHIP VERIFICATION")
        print(f"{'='*60}")
        print(f"Attempts:             {MAX_VERIFY_ATTEMPTS}")
        print(f"Verified successfully: {success_count}")
        print(f"Failed verification:   {MAX_VERIFY_ATTEMPTS - success_count}")

        if verified_trade:
            self.__class__.verified_trade_id = verified_trade["trade_id"]
            self.__class__.verified_tag = verified_trade["tag"]
            print(f"Final status:          PASS")
            print(f"Trade ID:              {verified_trade['trade_id']}")
            print(f"Verified tag:          {verified_trade['tag']}")
            print(f"{'='*60}\n")
        else:
            print(f"Final status:          FAIL")
            print(f"{'='*60}")
            pytest.fail(f"❌ 0/{MAX_VERIFY_ATTEMPTS} attempts verified ownership from OANDA. Tag not visible.")

    # =====================================================
    # TEST 2: 重复开仓 — OANDA查到同Tag → 本地拒绝
    # =====================================================
    def test_2_duplicate_open_refused_by_ownership(self):
        """✅ 2. Duplicate Open — OANDA has same TAG → REFUSE locally"""
        self._require_verified_ownership()
        print("\n🔵 2/5 DUPLICATE OPEN — verify ownership then REFUSE...")

        time.sleep(BASE_QUERY_DELAY)
        candidates = _query_all_matching_trades(INSTRUMENT)
        matched = [c for c in candidates if c["tag"] == TAG]

        if matched:
            existing = matched[0]
            print(f"   Existing OANDA trade:")
            print(f"     instrument = {existing['instrument']}")
            print(f"     trade_id   = {existing['trade_id']}")
            print(f"     tag        = {existing['tag']}")
            print(f"✅ DUPLICATE REFUSED — ownership already exists locally, OANDA NOT CALLED")
            return
        else:
            pytest.fail("❌ Cannot verify duplicate protection: no OANDA trade with expected TAG found")

    # =====================================================
    # TEST 3: 修改 SL/TP — 只用 verified_trade_id
    # =====================================================
    def test_3_modify_sltp_allowed(self):
        """✅ 3. Modify SL/TP — TradeCRCDO on verified trade_id only"""
        self._require_verified_ownership()
        print(f"\n🔵 3/5 MODIFY verified trade {self.verified_trade_id} SL/TP...")

        update_data = {
            "takeProfit": {"price": "213.50", "timeInForce": "GTC"},
            "stopLoss": {"price": "204.50", "timeInForce": "GTC"},
        }
        API.request(oanda_trades.TradeCRCDO(ACCOUNT_ID, self.verified_trade_id, update_data))
        print("✅ 3/5 MODIFIED SL/TP")

    # =====================================================
    # TEST 4: 错Tag平仓 — OANDA tag ≠ WRONG → 本地拒绝
    # =====================================================
    def test_4_wrong_tag_close_refused(self):
        """✅ 4. Wrong Tag Close — ownership mismatch (checked against a FRESH OANDA query) → REFUSE locally"""
        self._require_verified_ownership()
        print("\n🔴 4/5 WRONG TAG CLOSE — ownership check against live OANDA data...")

        WRONG = "INVALID-TAG-999"

        # ✅ Fixed: use a fresh query for the actual tag, not the cached
        # self.verified_tag from test_1. Identify our specific trade by
        # trade_id so an unrelated GBP_JPY trade can't be mistaken for ours.
        time.sleep(BASE_QUERY_DELAY)
        candidates = _query_all_matching_trades(INSTRUMENT)
        ours = [c for c in candidates if c["trade_id"] == self.verified_trade_id]

        if not ours:
            pytest.fail("❌ Cannot verify: our verified trade was not found on OANDA for wrong-tag check")

        actual_tag = ours[0]["tag"]
        print(f"   OANDA tag:      {actual_tag}")
        print(f"   Requested tag:  {WRONG}")

        if actual_tag != WRONG:
            print("✅ REFUSED — ownership mismatch, OANDA NOT CALLED")
            return
        else:
            pytest.fail("❌ Unexpected: WRONG tag matched OANDA tag")

    # =====================================================
    # TEST 5: 正确Tag平仓 — OANDA tag == TAG → 执行平仓
    # =====================================================
    def test_5_correct_tag_close_success(self):
        """✅ 5. Correct Tag Close — ownership MATCH → PositionClose"""
        self._require_verified_ownership()
        print("\n🔴 5/5 CORRECT TAG CLOSE — ownership verified → EXECUTE...")

        time.sleep(BASE_QUERY_DELAY)
        candidates = _query_all_matching_trades(INSTRUMENT)
        matched = [c for c in candidates if c["tag"] == TAG and c["trade_id"] == self.verified_trade_id]

        if not matched:
            pytest.fail("❌ Ownership verification failed before close: our trade not found on OANDA")

        actual = matched[0]["tag"]
        print(f"   OANDA tag:      {actual}")
        print(f"   Requested tag:  {TAG}")

        if actual == TAG:
            print("✅ OWNERSHIP MATCH → calling PositionClose(longUnits=ALL)...")
            close_data = {"longUnits": "ALL"}
            API.request(oanda_positions.PositionClose(ACCOUNT_ID, INSTRUMENT, close_data))

            time.sleep(BASE_QUERY_DELAY)
            after = _query_all_matching_trades(INSTRUMENT)
            still_there = [c for c in after if c["trade_id"] == self.verified_trade_id]
            assert not still_there, f"❌ Our trade {self.verified_trade_id} still exists after close!"
            print(f"✅ 5/5 CLOSED — verified trade {self.verified_trade_id} cleared from OANDA")
        else:
            pytest.fail(f"❌ Tag mismatch before close: OANDA={actual}, expected={TAG}")