from utils.trading_core import diagnose_pair_position

if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python diagnose_pair.py USD_JPY")
        raise SystemExit(2)

    pair = sys.argv[1].upper()

    allowed = {"USD_JPY", "GBP_JPY"}

    if pair not in allowed:
        print(f"ERROR: pair must be one of: {', '.join(sorted(allowed))}")
        raise SystemExit(2)

    diagnose_pair_position(pair)
