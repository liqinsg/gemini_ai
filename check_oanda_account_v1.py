import oandapyV20
from oandapyV20.endpoints.accounts import AccountSummary
from utils.oanda_execution import api, OANDA_ACCOUNT_ID

print("OANDA_ACCOUNT_ID repr =", repr(OANDA_ACCOUNT_ID))
print("OANDA_ACCOUNT_ID str  =", str(OANDA_ACCOUNT_ID))
print("len =", len(str(OANDA_ACCOUNT_ID)))

try:
    r = AccountSummary(OANDA_ACCOUNT_ID)
    resp = api.request(r)
    print('✅ ACCOUNT OK:', resp['account']['id'], '—', resp['account']['currency'])
except Exception as e:
    print('❌ FORBIDDEN / MISMATCH:', str(e))