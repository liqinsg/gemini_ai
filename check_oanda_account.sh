source .env
#curl -H "Authorization: Bearer $OANDA_API_TOKEN_LIVE" https://api-fxtrade.oanda.com/v3/accounts
curl -H "Authorization: Bearer $OANDA_API_TOKEN" https://api-fxpractice.oanda.com/v3/accounts
