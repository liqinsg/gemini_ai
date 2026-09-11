def check_reverse_and_close(oanda_sym, position_type, current_p_up, current_p_down, base_cur, quote_cur, top_n, bottom_n):
    # 如果是多单，但强弱关系颠倒或上涨概率剧降，提前主动平仓
    if position_type == "LONG":
        if base_cur in bottom_n or quote_cur in top_n or current_p_up < 40.0:
            print(f"⚠️ {oanda_sym}: LONG 信号反转，主动平仓！")
            close_oanda_position(oanda_sym)
            return True
            
    # 如果是空单，但强弱关系颠倒或下跌概率剧降
    elif position_type == "SHORT":
        if base_cur in top_n or quote_cur in bottom_n or current_p_down < 40.0:
            print(f"⚠️ {oanda_sym}: SHORT 信号反转，主动平仓！")
            close_oanda_position(oanda_sym)
            return True
            
    return False