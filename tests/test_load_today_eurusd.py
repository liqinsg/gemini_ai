import sys
from datetime import datetime
from pathlib import Path

# Add project root (gemini_ai) to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))

from utils.mc_loader_local import get_latest_mc_local, DEFAULT_RESULTS_DIR

def test_today_mc_eurusd():
    pair = "EURUSD=X"
    today_str = datetime.now().strftime("%Y%m%d")
    
    print(f"--- Testing MC Local Loader for {pair} ---")
    print(f"Target Date         : {today_str}")
    print(f"Searching Directory : {DEFAULT_RESULTS_DIR}\n")

    # 1. Fetch the latest MC data
    data = get_latest_mc_local(pair=pair, day=True)

    if not data:
        print(f"❌ FAIL: No MC data returned for {pair}.")
        return

    # 2. Extract timestamp/file metadata from data if present, or verify structure
    p_up = data.get("p_up")
    p_down = data.get("p_down")
    regime = data.get("regime")
    
    print("✅ SUCCESS: Successfully loaded MC result!")
    print(f"  • Pair     : {data.get('pair', pair)}")
    print(f"  • Timeframe: {data.get('timeframe', 'D')}")
    print(f"  • P(UP)    : {p_up}%")
    print(f"  • P(DOWN)  : {p_down}%")
    print(f"  • Regime   : {regime}")

    # 3. Optional: Verify if the returned data matches today's date pattern
    # Assuming daily_results files are named like 'daily_mc_EURUSD_YYYYMMDD_HHMM.json'
    matching_today = list(DEFAULT_RESULTS_DIR.glob(f"daily_mc_EURUSD_{today_str}_*.json"))
    if matching_today:
        print(f"\n🎯 Confirmed today's file found on disk: {matching_today[-1].name}")
    else:
        print(f"\n⚠️ Notice: Returned latest file, but no file generated specifically for today ({today_str}) yet.")


if __name__ == "__main__":
    test_today_mc_eurusd()