# /utils/calculate_risk_reward.py
# Calculate Risk/Reward

def calculate_risk_reward(entry_price, sl_price, tp_price):
    """
    Calculate the risk/reward ratio for a trade.

    Parameters:
    entry_price (float): The price at which the trade is entered.
    sl_price (float): The stop-loss price.
    tp_price (float): The take-profit price.

    Returns:
    float: The risk/reward ratio.
    """
    risk_pips = abs(entry_price - sl_price)
    reward_pips = abs(entry_price - tp_price)
    return reward_pips / risk_pips if risk_pips > 0 else 0

def get_rr_ratio(entry_price, sl_price, tp_price):
    """
    Get the risk/reward ratio for a trade.

    Parameters:
    entry_price (float): The price at which the trade is entered.
    sl_price (float): The stop-loss price.
    tp_price (float): The take-profit price.

    Returns:
    float: The risk/reward ratio.
    """
    return calculate_risk_reward(entry_price, sl_price, tp_price)

def is_trade_acceptable(entry_price, sl_price, tp_price, min_rr_ratio=0.0):
    """
    Check if trade meets minimum risk/reward ratio.
    Prints reason automatically — no extra code needed in the bot.
    """
    rr_ratio = get_rr_ratio(entry_price, sl_price, tp_price)
    ok = rr_ratio >= min_rr_ratio
    if not ok:
        print(f"⏭️ R/R {rr_ratio:.2f} < {min_rr_ratio:.2f} — skip")
    return ok

def is_trade_acceptable_with_min_rr(entry_price, sl_price, tp_price, min_rr_ratio=0.0):
    """
    Check if the trade meets the minimum risk/reward ratio.

    Parameters:
    entry_price (float): The price at which the trade is entered.
    sl_price (float): The stop-loss price.
    tp_price (float): The take-profit price.
    min_rr_ratio (float): The minimum acceptable risk/reward ratio.

    Returns:
    bool: True if the trade meets the minimum risk/reward ratio, False otherwise.
    """
    rr_ratio = get_rr_ratio(entry_price, sl_price, tp_price)
    print(f"⏭️ R/R {rr_ratio:.2f} < {min_rr_ratio:.2f} — skip")
    return rr_ratio >= min_rr_ratio

if __name__ == "__main__":
    from config import MIN_REWARD_RISK, PRESET
    # Example usage
    entry_price = 212.431
    sl_price = 212.676
    tp_price = 212.157

    rr_ratio = get_rr_ratio(entry_price, sl_price, tp_price)
    is_acceptable = is_trade_acceptable(entry_price, sl_price, tp_price, MIN_REWARD_RISK)
    print(f"Risk/Reward Ratio: {rr_ratio:.2f}", f"Minimum R/R: {MIN_REWARD_RISK}, Preset: {PRESET}")
    print(f"Is Trade Acceptable: {is_acceptable}")

# # ONLY take trades with R/R ≥ MIN_REWARD_RISK (e.g. 1.2)
# MIN_RR_RATIO = cfg("MIN_REWARD_RISK", 1.2)
# if get_rr_ratio(entry_price, sl_price, tp_price) < MIN_RR_RATIO:
#     print(f"⏭️ SKIP — R/R {get_rr_ratio(entry_price, sl_price, tp_price):.2f} < {MIN_RR_RATIO}")
#     continue