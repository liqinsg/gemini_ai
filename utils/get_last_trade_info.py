# utils/get_last_trade_info.py
from datetime import datetime, timezone
from oandapyV20.endpoints.trades import TradesList
from oandapyV20.endpoints.positions import PositionDetails
from oanda_config import oanda_config
from utils.trading_core import api

def check_recent_closed_no_open(instrument, cooldown_mins=60):
    """
    ✅ NO JSON FILE — PULLS LIVE FROM OANDA
    Returns: (has_no_open_position, in_cooldown, minutes_since_close, last_direction)
    """
    now = datetime.now(timezone.utc)

    # Step 1: Check if position is OPEN
    try:
        pos = api.request(PositionDetails(oanda_config.ACCOUNT_ID, instrument=instrument))
        pos_data = pos.get("position", {})
        long_units = abs(float(pos_data.get("long", {}).get("units", 0)))
        short_units = abs(float(pos_data.get("short", {}).get("units", 0)))
        has_no_open = (long_units == 0) and (short_units == 0)
    except Exception:
        has_no_open = True

    # Step 2: Get ALL trades (open + closed) for this instrument, latest first
    try:
        resp = api.request(TradesList(
            oanda_config.ACCOUNT_ID,
            params={"instrument": instrument, "count": 5}
        ))
        trades = resp.get("trades", [])
    except Exception:
        trades = []

    # Step 3: Find most RECENTLY CLOSED trade
    last_closed_time = None
    last_dir = None
    for t in trades:
        if t.get("state") == "CLOSED":
            last_closed_time = datetime.fromisoformat(t["closeTime"].replace("Z", "+00:00"))
            last_dir = "LONG" if float(t.get("initialUnits", 0)) > 0 else "SHORT"
            break

    # Step 4: Calculate cooldown
    if last_closed_time and has_no_open:
        diff = now - last_closed_time
        mins_ago = diff.total_seconds() / 60
        in_cooldown = mins_ago < cooldown_mins
    else:
        mins_ago = 9999
        in_cooldown = False

    return has_no_open, in_cooldown, mins_ago, last_dir