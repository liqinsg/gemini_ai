from utils import get_support_resistance
from utils.schemas import TradeSignal

STRATEGY_LABELS = {
    1: "S1 Trend combined",
    2: "S2 Range reversion",
    3: "S3 Breakout confirm",
    4: "S4 Trend pullback",
}


def run_trend_pullback(pair: str, action: str, profile: dict, metrics: dict) -> TradeSignal | None:
    """
    In an uptrend, buy when price pulls back near support.
    In a downtrend, sell when price pulls back near resistance.
    """

    levels = get_support_resistance(pair)

    support = levels["support"]
    resistance = levels["resistance"]
    current_price = levels["current_price"]

    dp = 3 if pair.endswith("_JPY") else 5
    pip = 0.01 if pair.endswith("_JPY") else 0.0001

    direction = str(metrics.get("direction", "")).upper()
    if not direction:
        pdi = metrics.get("plus_di", 0)
        mdi = metrics.get("minus_di", 0)
        direction = "BULLISH" if pdi > mdi else "BEARISH"

    near_buffer = pip * 10   # price must be within 10 pips of S/R
    sl_buffer = pip * 5      # SL 5 pips beyond S/R

    if action == "BUY" and direction != "BULLISH":
        print(
            f"  [S4] BUY skipped. Regime direction is {direction}, not BULLISH.")
        return None

    if action == "SELL" and direction != "BEARISH":
        print(
            f"  [S4] SELL skipped. Regime direction is {direction}, not BEARISH.")
        return None

    if action == "BUY":
        distance_to_support = abs(current_price - support)

        if distance_to_support > near_buffer:
            print(
                f"  [S4] BUY trend pullback not ready. "
                f"Price {current_price} not near support {support}."
            )
            return None

        sl = round(support - sl_buffer, dp)
        tp = round(resistance - sl_buffer, dp)

        if not sl < current_price < tp:
            print(
                f"  [S4] Invalid BUY setup: "
                f"SL={sl}, price={current_price}, TP={tp}"
            )
            return None

    else:
        distance_to_resistance = abs(current_price - resistance)

        if distance_to_resistance > near_buffer:
            print(
                f"  [S4] SELL trend pullback not ready. "
                f"Price {current_price} not near resistance {resistance}."
            )
            return None

        sl = round(resistance + sl_buffer, dp)
        tp = round(support + sl_buffer, dp)

        if not tp < current_price < sl:
            print(
                f"  [S4] Invalid SELL setup: "
                f"TP={tp}, price={current_price}, SL={sl}"
            )
            return None

    print(
        f"  [S4] Trend pullback confirmed: {action} {pair} | "
        f"price={current_price}, support={support}, resistance={resistance}, "
        f"SL={sl}, TP={tp}"
    )

    return TradeSignal(
        pair_to_trade=pair,
        action=action,
        confidence_score=0.72,
        stop_loss=sl,
        take_profit=tp,
        reasoning=(
            f"[TREND PULLBACK] {action} {pair}: price pulled back near "
            f"{'support' if action == 'BUY' else 'resistance'} in trend direction. "
            f"Support={support}, Resistance={resistance}."
        )
    )
