"""
Manual smoke test for order execution — bypasses the AI ensemble entirely.
Run this AFTER confirming your .env points at your demo account/token.
This will place a REAL (demo) market order if it succeeds.
"""
from utils.schemas import TradeSignal
from trading_core execute_market_trade

# Adjust stop_loss / take_profit to sane levels for whatever pair you test with.
# These are placeholder price levels — check the current market price first
# (e.g. via OANDA's HUB or a quick candle fetch) so your stop/TP aren't
# absurdly far from or behind current price.
test_signal = TradeSignal(
    pair_to_trade="USD_JPY",
    action="BUY",
    confidence_score=1.0,
    stop_loss=160.500,     # below current price (~162) for a BUY
    take_profit=163.500,   # above current price (~162) for a BUY
    reasoning="Manual smoke test of order execution path."
)

print(f"Submitting test order: {test_signal.action} {test_signal.pair_to_trade}")
print(f"  Stop Loss:  {test_signal.stop_loss}")
print(f"  Take Profit: {test_signal.take_profit}")

# confirm = input("Type 'yes' to confirm and place this order on your DEMO account: ", "yes")
confirm = "yes"
if True or confirm.strip().lower() == "yes":
    execute_market_trade(test_signal)
else:
    print("Aborted — no order placed.")