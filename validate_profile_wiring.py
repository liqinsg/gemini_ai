"""
validate_profile_wiring.py — profile #N -> account -> token -> OANDA visibility.

Replicates the EXACT resolution used by scheduled_runner_v1441.py:
  1. config_oanda.get_oanda_profile(env) -> prebuilt oanda_client + token
  2. config_oanda.OANDA_ACCOUNT_ID_<N>_LIVE (or DEMO) -> the account_id
  3. OANDA AccountList confirms token can see that account_id
  4. Show injection into TradingCore V2

Usage:
  python validate_profile_wiring.py -p 3 --live
  python validate_profile_wiring.py -p 1
"""
import os, sys, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", "--profile", type=int, default=3)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--no-summary", action="store_true")
    args = parser.parse_args()

    if args.live:
        os.environ["OANDA_ENV"] = "live"

    import config_oanda as oc
    from utils.trading_core_v2 import TradingCore

    env_label = "live" if args.live else "practice"
    suffix = "_LIVE" if args.live else ""
    account_key = f"OANDA_ACCOUNT_ID_{args.profile}{suffix}"
    account_id = getattr(oc, account_key, "")

    print("=" * 65)
    print(f"PROFILE WIRING VALIDATION  profile=#{args.profile}  env={env_label}")
    print("=" * 65)

    print(f"\n[1] config_oanda module globals")
    print(f"    account_key      = {account_key}")
    print(f"    resolved_account = {account_id}")

    print(f"\n[2] get_oanda_profile(env='{env_label}')")
    profile = oc.get_oanda_profile(env_label)
    print(f"    profile['env']          = {profile['env']}")
    print(f"    profile['token']        = [{'SET' if profile['token'] else 'NONE'}] ({len(profile['token'] or '')}ch)")
    print(f"    profile['oanda_client'] = {profile['oanda_client']!r}")
    print(f"    profile['account_ids']  = {profile['account_ids']}")

    if profile["oanda_client"] is None:
        print("\nFATAL: oanda_client is None - token likely missing")
        return 1

    client = profile["oanda_client"]

    print(f"\n[3] Cross-checks")
    env_ok = profile["env"] == env_label
    acct_in_list = account_id in profile["account_ids"]
    print(f"    profile env matches expected:  {env_ok}")
    print(f"    account_id in profile list:    {acct_in_list}")

    print(f"\n[4] OANDA AccountList")
    try:
        import oandapyV20.endpoints.accounts as oa
        resp = client.request(oa.AccountList())
        visible_ids = [a.get("id", "") for a in resp.get("accounts", []) if a.get("id")]
        print(f"    -> {len(visible_ids)} visible account(s)")
        for vid in visible_ids:
            mark = " <-- TARGET" if vid == account_id else ""
            print(f"       {vid}{mark}")
    except Exception as e:
        print(f"    FAIL: {type(e).__name__}: {e}")
        return 1

    target_visible = account_id in visible_ids
    print(f"\n[5] target account {account_id} visible? {'PASS' if target_visible else 'FAIL'}")

    if not target_visible:
        print("\nFATAL: account_id NOT reachable with this token. Check .env.")
        return 1

    print(f"\n[6] TradingCore V2 injection")
    tc = TradingCore(oanda_client=client, oanda_account_id=account_id)
    print(f"    tc.oanda_client is client       -> {tc.oanda_client is client}")
    print(f"    tc.oanda_account_id == account  -> {tc.oanda_account_id == account_id}")
    print(f"    id(tc.oanda_client) = {id(tc.oanda_client)}")
    print(f"    id(client)          = {id(client)}")

    if not args.no_summary:
        print(f"\n[7] AccountSummary (via injected client)")
        try:
            a = client.request(oa.AccountSummary(account_id)).get("account", {})
            print(f"    Currency       : {a.get('currency', '?')}")
            print(f"    Balance        : {a.get('balance', '?')}")
            print(f"    NAV            : {a.get('nav', '?')}")
            print(f"    UnrealizedPL   : {a.get('unrealizedPL', '?')}")
            print(f"    Margin Used    : {a.get('marginUsed', '?')}")
            print(f"    Margin Avail   : {a.get('marginAvailable', '?')}")
            print(f"    Open Trades    : {a.get('openTradeCount', '?')}")
            print(f"    Open Positions : {a.get('openPositionCount', '?')}")
        except Exception as e:
            print(f"    FAIL: {type(e).__name__}: {e}")

    print(f"\n{'='*65}")
    all_pass = env_ok and acct_in_list and target_visible and tc.oanda_client is client
    print("RESULT:", "ALL CHECKS PASSED" if all_pass else "SOME FAILED")
    print(f"{'='*65}")
    return 0 if all_pass else 1

if __name__ == "__main__":
    sys.exit(main())
