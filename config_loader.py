import os
from dotenv import load_dotenv

# Priority chain (live only — demo keeps using the existing profile-based units untouched):
#   1. CLI --lots
#   2. run.env LIVE_LOT_SIZE
#   3. fallback_units (RISK_PROFILE[RISK_LEVEL]["units"])

def resolve_lot_size(cli_lots: int | None, is_live: bool, fallback_units: int) -> int:
    """Resolve lot size (units) respecting precedence: CLI → run.env (live only) → profile default."""
    load_dotenv("run.env", override=True)

    if cli_lots is not None and cli_lots > 0:
        return cli_lots

    if is_live:
        env_units = os.getenv("LIVE_LOT_SIZE")
        if env_units:
            return int(env_units)

    return fallback_units
