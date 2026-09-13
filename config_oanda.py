# config_oanda.py — v7.1.1 | Multi-Account Config + Dynamic Profile System
# ────────────────────────────────────────────────────────────────
"""
Central configuration — edit this file to control all strategy behaviour.
Do not hardcode these values elsewhere in the codebase.
"""
import os
import re
import sys
from dotenv import load_dotenv
from pathlib import Path
import oandapyV20
import oandapyV20.endpoints.accounts as oanda_accounts

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env", override=False)
load_dotenv(PROJECT_ROOT / "run.env", override=True)

# 常量定义
OANDA_ENV_DEMO = "practice"
OANDA_ENV_LIVE = "live"

# 1. 基础 Token 导入
OANDA_API_TOKEN_DEMO = os.getenv("OANDA_API_TOKEN_DEMO", os.getenv("OANDA_API_TOKEN", ""))
OANDA_API_TOKEN_LIVE = os.getenv("OANDA_API_TOKEN_LIVE", "")

# 2. 账号 ID 基础变量映射 (Demo 与 Live)
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_ACCOUNT_ID_DEMO_1 = os.getenv("OANDA_ACCOUNT_ID_DEMO_1", "101-003-39389016-001")
OANDA_ACCOUNT_ID_DEMO_2 = os.getenv("OANDA_ACCOUNT_ID_DEMO_2", "101-003-39389016-002")
OANDA_ACCOUNT_ID_DEMO_3 = os.getenv("OANDA_ACCOUNT_ID_DEMO_3", "101-003-39389016-003")
OANDA_ACCOUNT_ID_DEMO_4 = os.getenv("OANDA_ACCOUNT_ID_DEMO_4", "101-003-39389016-004")

OANDA_ACCOUNT_ID_1_LIVE = os.getenv("OANDA_ACCOUNT_ID_1_LIVE", "001-003-21515688-001")
OANDA_ACCOUNT_ID_2_LIVE = os.getenv("OANDA_ACCOUNT_ID_2_LIVE", "001-003-21515688-002")
OANDA_ACCOUNT_ID_3_LIVE = os.getenv("OANDA_ACCOUNT_ID_3_LIVE", "001-003-21515688-003")
OANDA_ACCOUNT_ID_4_LIVE = os.getenv("OANDA_ACCOUNT_ID_4_LIVE", "001-003-21515688-004")

_is_live_environment = os.getenv("OANDA_ENV", "practice").strip().lower() in {"live", "real"}
if _is_live_environment:
    OANDA_ENV = OANDA_ENV_LIVE
    OANDA_API_TOKEN = OANDA_API_TOKEN_LIVE
    OANDA_ACCOUNT_ID_1 = OANDA_ACCOUNT_ID_1_LIVE
    OANDA_ACCOUNT_ID_2 = OANDA_ACCOUNT_ID_2_LIVE
    OANDA_ACCOUNT_ID_3 = OANDA_ACCOUNT_ID_3_LIVE
    OANDA_ACCOUNT_ID_4 = OANDA_ACCOUNT_ID_4_LIVE
else:
    OANDA_ENV = OANDA_ENV_DEMO
    OANDA_API_TOKEN = OANDA_API_TOKEN_DEMO
    OANDA_ACCOUNT_ID_1 = OANDA_ACCOUNT_ID_DEMO_1
    OANDA_ACCOUNT_ID_2 = OANDA_ACCOUNT_ID_DEMO_2
    OANDA_ACCOUNT_ID_3 = OANDA_ACCOUNT_ID_DEMO_3
    OANDA_ACCOUNT_ID_4 = OANDA_ACCOUNT_ID_DEMO_4

OANDA_ACCOUNT_ID = OANDA_ACCOUNT_ID_1


def get_oanda_profile(env_override: str = None) -> dict:
    """
    根据运行环境动态返回对应配置 Profile。
    :param env_override: "practice" | "demo" | "live" (若为 None 则强制读取 run.env 中的 OANDA_ENV)
    :return: 包含 env, token, api, account_ids 的字典
    """
    # 核心修改 2：实时获取由 run.env 加载的最新 OANDA_ENV，不使用静态缓存
    current_run_env = os.getenv("OANDA_ENV", "practice")
    raw_env = (env_override or current_run_env).strip().lower()
    is_live = raw_env in ["live", "real"]

    selected_env = OANDA_ENV_LIVE if is_live else OANDA_ENV_DEMO
    selected_token = OANDA_API_TOKEN_LIVE if is_live else OANDA_API_TOKEN_DEMO

    # 匹配对应环境下的 Account ID 变量
    var_regex = r"^OANDA_ACCOUNT_ID_\d+_LIVE$" if is_live else r"^OANDA_ACCOUNT_ID_(DEMO_)?\d+$"

    account_ids = []
    for name, value in vars(sys.modules[__name__]).items():
        if isinstance(name, str) and re.fullmatch(var_regex, name):
            if value:
                account_ids.append((name, value))

    account_ids.sort(key=lambda x: x[0])
    account_list = [acc_id for _, acc_id in account_ids]

    # 初始化对应环境的 API Client
    api_client = None
    if selected_token:
        api_client = oandapyV20.API(access_token=selected_token, environment=selected_env)

    return {
        "env": selected_env,
        "token": selected_token,
        "api": api_client,
        "account_ids": account_list,
        "raw_config": account_ids
    }


# 顶层全局对象：保证向后兼容 (直接 import api 时自动使用 run.env 中设置的环境)
default_profile = get_oanda_profile()
api = default_profile["api"]


# ────────────────────────────────────────────────────────────────
# 辅助函数 (用于 CLI 自检与账号发现)
# ────────────────────────────────────────────────────────────────

def _discover_accounts(token: str, env_name: str, label: str):
    if not token:
        print(f"❌ {label}: TOKEN NOT SET")
        return []

    try:
        client = oandapyV20.API(access_token=token, environment=env_name)
        resp = client.request(oanda_accounts.AccountList())
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

    exact = (config_set == discovered_set)
    print(f"\n{'✅' if exact else '❌'} {label}: {'EXACT MATCH' if exact else 'MISMATCH'}")
    return exact


def _fetch_summary(account_id, token, env_name):
    try:
        client = oandapyV20.API(access_token=token, environment=env_name)
        resp = client.request(oanda_accounts.AccountSummary(account_id))["account"]
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


def main(show_summary=False, env_override=None):
    profile = get_oanda_profile(env_override)

    print("=" * 65)
    print(f"🔍 OANDA VALIDATION | Env: {profile['env'].upper()}")
    print("=" * 65)
    print(f"Token Status : {'✅ SET' if profile['token'] else '❌ MISSING'}")
    print(f"Target Accounts Count: {len(profile['account_ids'])}")
    print("─" * 65)

    if not profile["token"]:
        print("❌ API Token 未配置，退出校验")
        return 1

    visible = _discover_accounts(profile["token"], profile["env"], f"{profile['env'].upper()} Token")
    matched_ok = bool(visible) and _compare_accounts(profile["account_ids"], visible, profile["env"].upper())

    if show_summary and visible:
        print(f"\n📋 {profile['env'].upper()} ACCOUNT SUMMARIES")
        for acc in visible:
            _fetch_summary(acc.get("id"), profile["token"], profile["env"])

    print("\n" + "=" * 65)
    print(f"FINAL RESULT → {profile['env'].upper()}: {'✅ PASS' if matched_ok else '❌ FAIL'}")
    return 0 if matched_ok else 1


if __name__ == "__main__":
    show_summary_flag = "--summary" in sys.argv
    if show_summary_flag:
        sys.argv.remove("--summary")

    env_arg = None
    if "--env" in sys.argv:
        idx = sys.argv.index("--env")
        if idx + 1 < len(sys.argv):
            env_arg = sys.argv[idx + 1]
            sys.argv.pop(idx + 1)
            sys.argv.pop(idx)

    raise SystemExit(main(show_summary=show_summary_flag, env_override=env_arg))