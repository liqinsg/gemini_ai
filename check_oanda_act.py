# inside check_oanda_account.py

from utils.oanda_execution import api

print("API object:", api)
print("Environment:", api.environment)
print("Token prefix:", api.access_token[:10])
