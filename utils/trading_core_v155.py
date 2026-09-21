"""
scheduled_runner_v155.py — Class 适配版
判断集中在这里 → 传给 OANDAConnection → 拿到即用
"""
import argparse
import os
from dotenv import load_dotenv

load_dotenv()

# ===== 1. 解析参数 + 选定配置 =====
parser = argparse.ArgumentParser(description='JPY Forex Bot v1.5.5')
parser.add_argument('-p', '--profile', type=int, default=1)
parser.add_argument('--dry-run', action='store_true')
parser.add_argument('--live', action='store_true', help='使用实盘配置')
parser.add_argument('--debug', type=int, default=1)
args = parser.parse_args()

# ===== 2. 唯一判断点：主程序决定一切 =====
if args.live:
    # --live 模式 → 全部从 _LIVE 读取
    chosen_env = os.getenv("OANDA_ENV_LIVE", "live")
    chosen_token = os.getenv("OANDA_API_TOKEN_LIVE", "")
    chosen_account = os.getenv("OANDA_ACCOUNT_ID_LIVE", "")
    env_label = f"{chosen_env.upper()} (--live 强制)"
else:
    # 默认模式 → 标准配置
    chosen_env = os.getenv("OANDA_ENV", "practice")
    chosen_token = os.getenv("OANDA_API_TOKEN", "")
    chosen_account = os.getenv("OANDA_ACCOUNT_ID", "")
    env_label = chosen_env.upper()

# ===== 3. 传给 Class：就 3 个参数，干净直接 =====
from utils.trading_core_v155 import OANDAConnection

conn = OANDAConnection(
    env=chosen_env,
    token=chosen_token,
    account_id=chosen_account
).connect()  # 建立连接

# 后续代码直接用 conn.client / conn.env / conn.account_id
oanda_client = conn.client
info = conn.display_info

# ===== 4. Banner 一目了然 =====
print("=" * 60)
print("JPY STRENGTH TRADING BOT — SCHEDULED RUNNER v1.5.5")
print("=" * 60)
print(f"  OANDA profile: #{args.profile} ({info['account']})")
print(f"  Dry run: {'ENABLED' if args.dry_run else 'OFF — REAL TRADING'}")
print(f"  OANDA Env  : {env_label} 🔗 {info['host']}")
print(f"  Token Valid: ✅ {info['token_len']} chars — ends: …{info['token_tail']}")
print("=" * 60)
