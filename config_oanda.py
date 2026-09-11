# config_oanda.py — v6.8.4 | Multi-Account Config + Self-Validation (CLI)
# ────────────────────────────────────────────────────────────────
"""
Central configuration — edit this file to control all strategy behaviour.
Do not hardcode these values elsewhere in the codebase.
"""
import os
import re
import sys
from dotenv import load_dotenv
import oandapyV20
import oandapyV20.endpoints.accounts as oanda_accounts

OANDA_ENV_DEMO = "practice"
OANDA_ENV_LIVE = "live"

load_dotenv()
OANDA_API_TOKEN = os.getenv("OANDA_API_TOKEN", "")
OANDA_API_TOKEN_LIVE = os.getenv("OANDA_API_TOKEN_LIVE", "")

OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_ACCOUNT_ID_1 = os.getenv("OANDA_ACCOUNT_ID_1", "101-003-39389016-001")
OANDA_ACCOUNT_ID_2 = os.getenv("OANDA_ACCOUNT_ID_2", "101-003-39389016-002")
OANDA_ACCOUNT_ID_3 = os.getenv("OANDA_ACCOUNT_ID_3", "101-003-39389016-003")
OANDA_ACCOUNT_ID_4 = os.getenv("OANDA_ACCOUNT_ID_4", "101-003-39389016-004")
OANDA_ACCOUNT_ID_1_LIVE = os.getenv("OANDA_ACCOUNT_ID_1_LIVE", "001-003-21515688-001")
OANDA_ACCOUNT_ID_2_LIVE = os.getenv("OANDA_ACCOUNT_ID_2_LIVE", "001-003-21515688-002")
OANDA_ACCOUNT_ID_3_LIVE = os.getenv("OANDA_ACCOUNT_ID_3_LIVE", "001-003-21515688-003")
OANDA_ACCOUNT_ID_4_LIVE = os.getenv("OANDA_ACCOUNT_ID_4_LIVE", "001-003-21515688-004")


def _collect_account_vars(var_regex: str):
    out = []
    for name, value in vars(sys.modules[__name__]).items():
        if isinstance(name, str) and re.fullmatch(var_regex, name):
            out.append((name, value))
    out.sort(key=lambda x: x[0])
    return out


def _discover_accounts(token: str, env_name: str, label: str):
    if not token:
        print(f"❌ {label}: TOKEN NOT SET")
        return []

    api = oandapyV20.API(access_token=token, environment=env_name)
    try:
        resp = api.request(oanda_accounts.AccountList())
        accounts = resp.get("accounts", [])
        print(f"✅ {label}: TOKEN VALID → {len(accounts)} account(s) visible")
        for acc in accounts:
            aid = acc.get("id", "?")
            tags = acc.get("tags", [])
            print(f"   └─ {aid}{f' | tags: {tags}' if tags else ''}")
        return accounts
    except Exception as e:
        print(f"❌ {label}: TOKEN/API FAILED")
        print(f"    ⚠️ Error: {str(e)[:220]}")
        return []


def _compare_accounts(config_ids, discovered_accounts, label: str):
    config_set = set(config_ids)
    discovered_set = {acc.get("id") for acc in discovered_accounts if acc.get("id")}
    matched = config_set & discovered_set
    missing = config_set - discovered_set
    extra = discovered_set - config_set

    print(f"\n═══ {label} ACCOUNT COMPARISON ═══")
    print(f"Configured : {len(config_set)}")
    print(f"OANDA sees : {len(discovered_set)}")
    print(f"✅ Matched : {len(matched)}")
    print(f"❌ Missing : {len(missing)}")
    print(f"⚠️ Extra    : {len(extra)}")

    if matched:
        print("\n✅ Matched accounts:")
        for aid in sorted(matched):
            print(f"   {aid}")
    if missing:
        print("\n❌ Configured but NOT visible:")
        for aid in sorted(missing):
            print(f"   {aid}")
    if extra:
        print("\n⚠️ Visible but NOT in config:")
        for aid in sorted(extra):
            print(f"   {aid}")

    exact = config_set == discovered_set
    print(f"\n{'✅' if exact else '❌'} {label}: {'EXACT MATCH' if exact else 'MISMATCH'}")
    return exact


def _fetch_summary(account_id, token, env_name):
    api = oandapyV20.API(access_token=token, environment=env_name)
    try:
        resp = api.request(oanda_accounts.AccountSummary(account_id))["account"]
        print(f"\n   📊 {account_id}")
        print(f"      Currency    : {resp.get('currency', '?')}")
        print(f"      Balance     : {resp.get('balance', '?')}")
        print(f"      NAV         : {resp.get('nav', '?')}")
        print(f"      UnrealizedPL: {resp.get('unrealizedPL', '?')}")
        print(f"      MarginUsed  : {resp.get('marginUsed', '?')}")
        print(f"      MarginAvail : {resp.get('marginAvailable', '?')}")
        print(f"      OpenTrades  : {resp.get('openTradeCount', '?')}")
        return True
    except Exception as e:
        print(f"\n   ❌ {account_id} — Summary failed: {str(e)[:120]}")
        return False


def main(show_summary=False):
    show_summary = show_summary or "--summary" in sys.argv
    if "--summary" in sys.argv:
        sys.argv.remove("--summary")

    print("=" * 65)
    print("🔍 OANDA VALIDATION v7.0.0 | Token → Discovery → Compare")
    print("=" * 65)
    print(f"Demo Token : {'✅ SET' if OANDA_API_TOKEN else '❌ MISSING'}")
    print(f"Live Token : {'✅ SET' if OANDA_API_TOKEN_LIVE else '❌ MISSING'}")
    print("─" * 65)

    demo_cfg = _collect_account_vars(r"^OANDA_ACCOUNT_ID_\d+$")
    demo_visible = _discover_accounts(OANDA_API_TOKEN, OANDA_ENV_DEMO, "Demo Token")
    demo_ok = bool(demo_visible) and _compare_accounts([acc_id for _, acc_id in demo_cfg if acc_id], demo_visible, "DEMO")
    if show_summary and demo_visible:
        print("\n📋 DEMO SUMMARIES")
        for acc in demo_visible:
            _fetch_summary(acc.get("id"), OANDA_API_TOKEN, OANDA_ENV_DEMO)

    live_cfg = _collect_account_vars(r"^OANDA_ACCOUNT_ID_\d+_LIVE$")
    live_visible = _discover_accounts(OANDA_API_TOKEN_LIVE, OANDA_ENV_LIVE, "Live Token")
    live_ok = bool(live_visible) and _compare_accounts([acc_id for _, acc_id in live_cfg if acc_id], live_visible, "LIVE")
    if show_summary and live_visible:
        print("\n📋 LIVE SUMMARIES")
        for acc in live_visible:
            _fetch_summary(acc.get("id"), OANDA_API_TOKEN_LIVE, OANDA_ENV_LIVE)

    print("\n" + "=" * 65)
    print(f"FINAL → DEMO: {'✅ PASS' if demo_ok else '❌ FAIL'}  |  LIVE: {'✅ PASS' if live_ok else '❌ FAIL'}")
    if demo_ok and live_ok:
        print("🎯 ALL OK")
        return 0
    print("⚠️ CHECK LIVE TOKEN / ACCOUNT-ID / OANDA PERMISSIONS")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
