#!/usr/bin/env python3
"""Standalone trigger script to run close-all on managed JPY pairs.

Usage examples:
  python close_all_runner.py --close-all
  python close_all_runner.py --close-all --account 101-003-39389016-001 --pairs EUR_JPY,AUD_JPY --cooldown 600
"""
import argparse
import config as _config
from scheduled_runner_jcs_claude import close_all_positions, emergency_close_all_jpy, _clear_emergency_lock
from scheduled_runner_v144 import emergency_close_all_jpy_v144, _clear_emergency_lock_v144

parser = argparse.ArgumentParser(description="Trigger close-all for managed JPY pairs")
parser.add_argument("--close-all", action="store_true", help="Perform close-all action now")
parser.add_argument("--emergency-close-all-jpy", action="store_true", help="Perform emergency close of ALL JPY positions (bypass tags)")
parser.add_argument("--clear-emergency-lock", action="store_true", help="Clear the emergency lock to allow normal trading to resume")
parser.add_argument("--account", type=str, help="OANDA account id to operate on (overrides config)")
parser.add_argument("--pairs", type=str, help="Comma-separated list of instrument pairs to close (e.g. EUR_JPY,AUD_JPY)")
parser.add_argument("--cooldown", type=int, default=900, help="Cooldown seconds to prevent immediate re-entry (default 900)")
args = parser.parse_args()

if not (args.close_all or args.emergency_close_all_jpy or args.clear_emergency_lock):
    print("No action specified. Use --close-all or --emergency-close-all-jpy to trigger closing positions.")
    exit(1)

acct = args.account or getattr(_config, 'OANDA_ACCOUNT_ID', None)
if not acct:
    print("ERROR: No account id provided and no OANDA_ACCOUNT_ID in config. Aborting.")
    exit(2)

pairs = None
if args.pairs:
    pairs = [p.strip() for p in args.pairs.split(',') if p.strip()]

print(f"Triggering close-all on account: {acct}")
print(f"Pairs: {pairs if pairs else 'ALL managed pairs'} | Cooldown: {args.cooldown}s")

if args.clear_emergency_lock:
    # clear both v3 (jcs_claude) and v144 emergency locks if present
    try:
        _clear_emergency_lock()
    except Exception:
        pass
    try:
        _clear_emergency_lock_v144()
    except Exception:
        pass
    print("Done.")
    exit(0)

if args.emergency_close_all_jpy:
    print("EMERGENCY close ALL JPY positions — this bypasses strategy filters and will create an emergency lock requiring manual removal.")
    # Run v144 emergency close by default for v144 runner
    try:
        res = emergency_close_all_jpy_v144(account_id=acct, require_practice_check=True, set_lock=True)
    except Exception:
        # fallback to jcs_claude emergency if v144 call isn't available
        res = emergency_close_all_jpy(account_id=acct, require_practice_check=True, set_lock=True)
    print("\nEmergency finished. Summary:")
    print(res)
    exit(0)

res = close_all_positions(account_id=acct, pairs=pairs, cooldown_seconds=args.cooldown)
print("\nFinished. Summary:")
print(res)
