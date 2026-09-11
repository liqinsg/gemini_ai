# -----------------------------------------------------------------------------
# Helper: v6.7 Weighted Score Calculator — TEST MODE RELAXED
# -----------------------------------------------------------------------------
def cfg_bot(name, default):
    """Read from config_bot.py first → fall back to config.py → then default."""
    return getattr(config_bot, name, getattr(config, name, default))


def calc_weighted_score(direction: str, gap: float, rsi_val: float, adx_val: float,
                         xgb_prob: float, mc_pct: float, is_test: bool = False) -> dict:

    """
    Compute FINAL_SCORE using 2-Coins weighted formula.
    TEST MODE: relax ADX, XGB, and threshold for easier signal generation.
    LIVE MODE: strict rules — ADX<25→0, NO_SIG→0, FINAL≥40, Gap≤1.0
    """
    # ─── THRESHOLDS ───
    if is_test:
        MIN_FINAL_SCORE = 25.0   # ✅ Lower bar for testing
        ADX_PASS_LEVEL = 0.0      # ✅ No ADX filter in test mode
        GAP_BLOCK_THRESHOLD = 2.0 # ✅ Wider gap tolerance in test
    else:
        MIN_FINAL_SCORE = min_conv  # from config_bot.py (default 40.0)
        ADX_PASS_LEVEL = 25.0       # ❌ Strict trend requirement in live
        GAP_BLOCK_THRESHOLD = cfg_bot("STRENGTH_SIGNAL_BLOCK_THRESHOLD", 1.0)

    # ─── 1. STRENGTH SCORE (0–100) ───
    max_expected_gap = 3.5
    if direction == "BUY":
        strength_raw = max(0.0, min(100.0, (-gap / max_expected_gap) * 100.0))
    else:  # SELL
        strength_raw = max(0.0, min(100.0, (gap / max_expected_gap) * 100.0))
    S = strength_raw

    # ─── 2. RSI SCORE (0–100) ───
    rsi = max(0.0, min(100.0, rsi_val))
    if direction == "BUY":
        if rsi < 30:
            R = 100.0
        elif rsi > 70:
            R = 0.0
        else:
            R = 100.0 - ((rsi - 30) / 40.0) * 100.0
    else:  # SELL
        if rsi > 70:
            R = 100.0
        elif rsi < 30:
            R = 0.0
        else:
            R = ((rsi - 30) / 40.0) * 100.0

    # ─── 3. ADX SCORE ───
    adx = max(0.0, min(100.0, adx_val))
    if adx >= ADX_PASS_LEVEL:
        A = adx
    else:
        A = 0.0

    # ─── 4. XGBOOST SCORE ───
    X = max(0.0, min(100.0, xgb_prob * 100.0 if xgb_prob else 0.0))
    # TEST MODE: if model silent → use Strength+RSI fallback instead of 0
    if X == 0.0 and is_test:
        X = max(0.0, min(100.0, S * 0.3 + R * 0.3))

    # ─── 5. MC SCORE ───
    M = max(0.0, min(100.0, mc_pct if mc_pct is not None else 50.0))

    # ─── FINAL WEIGHTED SUM ───
    FINAL = S*W_S + R*W_R + A*W_A + X*W_X + M*W_M
    PASS = FINAL >= MIN_FINAL_SCORE

    return {
        "S": round(S,1), "R": round(R,1), "A": round(A,1),
        "X": round(X,1), "M": round(M,1),
        "FINAL": round(FINAL,1), "PASS": PASS,
        "THRESHOLD": round(MIN_FINAL_SCORE,1),
        "GAP_THRESHOLD": round(GAP_BLOCK_THRESHOLD,1),
        "MODE": "TEST" if is_test else "LIVE"
    }