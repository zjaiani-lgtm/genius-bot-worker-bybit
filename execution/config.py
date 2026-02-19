# execution/config.py
import os
from pathlib import Path


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "y", "on")


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


# რეჟიმი: DEMO | TESTNET | LIVE
MODE = _env_str("MODE", "DEMO").upper()
if MODE not in ("DEMO", "TESTNET", "LIVE"):
    MODE = "DEMO"

# LIVE/TESTNET-ზე დამატებითი დაცვა
LIVE_CONFIRMATION = _env_bool("LIVE_CONFIRMATION", "false")

# Startup sync gate
STARTUP_SYNC_ENABLED = _env_bool("STARTUP_SYNC_ENABLED", "true")

# DEMO ბალანსი
VIRTUAL_START_BALANCE = float(_env_str("VIRTUAL_START_BALANCE", "100000") or "100000")

# Exchange არჩევანი
EXCHANGE = _env_str("EXCHANGE", "binance").lower()        # binance | bybit
MARKET_TYPE = _env_str("MARKET_TYPE", "spot").lower()     # spot | swap

# Binance keys (TESTNET/LIVE-ზე უნდა იყოს)
BINANCE_API_KEY = _env_str("BINANCE_API_KEY", "")
BINANCE_API_SECRET = _env_str("BINANCE_API_SECRET", "")

# Bybit keys (TESTNET/LIVE-ზე უნდა იყოს)
BYBIT_API_KEY = _env_str("BYBIT_API_KEY", "")
BYBIT_API_SECRET = _env_str("BYBIT_API_SECRET", "")

# Kill switch (შენთან ENV-ში false გაქვს, აქ default-ს ვტოვებ უსაფრთხოდ true-ზე)
KILL_SWITCH = _env_bool("KILL_SWITCH", "true")

# Persistent DB path (Render disk)
DB_PATH = Path(_env_str("DB_PATH", "/var/data/genius_bot.db") or "/var/data/genius_bot.db")

# Outbox path
DEFAULT_OUTBOX = "/var/data/signal_outbox.json"


def get_outbox_path() -> str:
    return _env_str("OUTBOX_PATH") or _env_str("SIGNAL_OUTBOX_PATH") or DEFAULT_OUTBOX


def get_excel_model_path() -> str:
    """
    Used by signal_generator.py / ExcelLiveCore.
    Required in LIVE/TESTNET, optional in DEMO (depends on your design).
    """
    p = _env_str("EXCEL_MODEL_PATH", "")
    if not p:
        raise RuntimeError("EXCEL_MODEL_PATH is not set (required for ExcelLiveCore).")
    return p
