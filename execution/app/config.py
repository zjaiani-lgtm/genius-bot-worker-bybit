import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Settings:
    EXCHANGE: str = os.getenv("EXCHANGE", "binance")
    SYMBOLS: tuple = ("BTC/USDT",)
    TIMEFRAME: str = "5m"
    LOOP_INTERVAL: int = 10
    RISK_PER_TRADE: float = 0.01
    MAX_DRAWDOWN: float = 0.2
    MAX_RETRIES: int = 3
    RETRY_DELAY: float = 1.5

settings = Settings()
