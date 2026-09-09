# config_oanda.py
"""
Central configuration — edit this file to control all strategy behaviour.
Do not hardcode these values elsewhere in the codebase.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env", override=False)

OANDA_ENV = os.getenv("OANDA_ENV", "practice")
OANDA_API_TOKEN = os.getenv("OANDA_API_TOKEN", "")
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "")
OANDA_ACCOUNT_ID_1 = os.getenv("OANDA_ACCOUNT_ID_1", "")
OANDA_ACCOUNT_ID_2 = os.getenv("OANDA_ACCOUNT_ID_2", "")
OANDA_ACCOUNT_ID_3 = os.getenv("OANDA_ACCOUNT_ID_3", "")
OANDA_ACCOUNT_ID_4 = os.getenv("OANDA_ACCOUNT_ID_4", "")
