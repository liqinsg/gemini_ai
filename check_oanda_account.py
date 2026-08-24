import oandapyV20
from oandapyV20.endpoints.accounts import AccountSummary
from utils.oanda_execution import api
from config_oanda import OANDA_ACCOUNT_ID
print(OANDA_ACCOUNT_ID)
try:
    r = AccountSummary(OANDA_ACCOUNT_ID)
    resp = api.request(r)
    print('✅ ACCOUNT OK:', resp['account']['id'], '—', resp['account']['currency'])
except Exception as e:
    print('❌ FORBIDDEN / MISMATCH:', str(e))
