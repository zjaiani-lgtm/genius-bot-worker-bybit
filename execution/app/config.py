from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

from .utils import env_bool, env_float, env_int


@dataclass(frozen=True)
class Config:
    # Exchange
    exchange_id: str = "bybit"
    api_key: str = ""
    api_secret: str = ""
    testnet: bool = False
    enable_rate_limit: bool = True
    exchange_timeout_ms: int = 20000

    # Trading
    symbols: List[str] = field(default_factory=list)
    timeframe: str = "15m"
    ohlcv_limit: int = 120

    # Risk
    quote_per_trade: float = 7.0
    position_size: float = 0.0  # if >0 use fixed base size, else quote_per_trade / price
    max_open_positions: int = 3
    max_drawdown: float = 0.04
    max_loss_streak: int = 4

    # Cooldown
    cooldown_seconds: int = 120
    post_loss_cooldown_seconds: int = 300

    # Excel Brain
    excel_path: str = ""
    excel_sheet: str = "CORE"
    excel_named_inputs_prefix: str = "CORE_"
    excel_named_outputs_prefix: str = "OUT_"
    excel_recalc_each_call: bool = True

    # Feature flags
    allow_live_signals: bool = False
    dry_run: bool = True
    adaptive_feedback_enabled: bool = False

    # Loop
    loop_sleep_seconds: float = 10.0
    report_every_seconds: int = 60


def load_config() -> Config:
    load_dotenv(override=False)

    symbols_raw = os.getenv("BOT_SYMBOLS") or os.getenv("AUTO_SCALER_UNIVERSE") or "BTC/USDT"
    symbols = [s.strip() for s in symbols_raw.split(",") if s.strip()]

    return Config(
        exchange_id=os.getenv("EXCHANGE_ID", "bybit"),
        api_key=os.getenv("BYBIT_API_KEY", os.getenv("API_KEY", "")),
        api_secret=os.getenv("BYBIT_API_SECRET", os.getenv("API_SECRET", "")),
        testnet=env_bool("BYBIT_TESTNET", False),
        enable_rate_limit=env_bool("ENABLE_RATE_LIMIT", True),
        exchange_timeout_ms=env_int("EXCHANGE_TIMEOUT_MS", 20000),

        symbols=symbols,
        timeframe=os.getenv("BOT_TIMEFRAME", "15m"),
        ohlcv_limit=env_int("BOT_OHLCV_LIMIT", 120),

        quote_per_trade=env_float("BOT_QUOTE_PER_TRADE", 7.0),
        position_size=env_float("BOT_POSITION_SIZE", 0.0),
        max_open_positions=env_int("AUTO_SCALER_MAX_SIZE", 3),
        max_drawdown=env_float("AUTO_SCALER_DD_LIMIT", 0.04),
        max_loss_streak=env_int("MAX_LOSS_STREAK", 4),

        cooldown_seconds=env_int("BOT_SIGNAL_COOLDOWN_SECONDS", 120),
        post_loss_cooldown_seconds=env_int("POST_LOSS_COOLDOWN_SECONDS", 300),

        excel_path=os.getenv("EXCEL_PATH", os.getenv("BOT_EXCEL_PATH", "")),
        excel_sheet=os.getenv("EXCEL_SHEET", "CORE"),
        excel_named_inputs_prefix=os.getenv("EXCEL_INPUT_PREFIX", "CORE_"),
        excel_named_outputs_prefix=os.getenv("EXCEL_OUTPUT_PREFIX", "OUT_"),
        excel_recalc_each_call=env_bool("EXCEL_RECALC_EACH_CALL", True),

        allow_live_signals=env_bool("ALLOW_LIVE_SIGNALS", False),
        dry_run=env_bool("DRY_RUN", True),
        adaptive_feedback_enabled=env_bool("ADAPTIVE_FEEDBACK_ENABLED", False),

        loop_sleep_seconds=env_float("LOOP_SLEEP_SECONDS", 10.0),
        report_every_seconds=env_int("REPORT_EVERY_SECONDS", 60),
    )
