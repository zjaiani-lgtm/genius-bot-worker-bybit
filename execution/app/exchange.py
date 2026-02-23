from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import ccxt  # type: ignore

from .logger import log


class ExchangeClient:
    def __init__(self, exchange, logger):
        self.exchange = exchange
        self.logger = logger

    def health_check(self) -> bool:
        try:
            self.exchange.fetch_time()
            return True
        except Exception as e:
            log(self.logger, "WARNING", "EXCHANGE_HEALTH_FAIL", error=str(e))
            return False

    def fetch_ohlcv_safe(self, symbol: str, timeframe: str, limit: int = 120) -> List[List[Any]]:
        for attempt in range(1, 4):
            try:
                return self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            except Exception as e:
                log(self.logger, "WARNING", "FETCH_OHLCV_FAIL", symbol=symbol, timeframe=timeframe, attempt=attempt, error=str(e))
                time.sleep(0.6 * attempt)
        return []

    def fetch_balance_safe(self) -> Dict[str, Any]:
        for attempt in range(1, 4):
            try:
                return self.exchange.fetch_balance()
            except Exception as e:
                log(self.logger, "WARNING", "FETCH_BALANCE_FAIL", attempt=attempt, error=str(e))
                time.sleep(0.6 * attempt)
        return {}

    def create_order_safe(self, symbol: str, type_: str, side: str, amount: float, price: Optional[float] = None, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        params = params or {}
        for attempt in range(1, 4):
            try:
                return self.exchange.create_order(symbol, type_, side, amount, price, params)
            except Exception as e:
                log(self.logger, "WARNING", "CREATE_ORDER_FAIL", symbol=symbol, type=type_, side=side, amount=amount, attempt=attempt, error=str(e))
                time.sleep(0.7 * attempt)
        return None

    def fetch_positions_safe(self) -> List[Dict[str, Any]]:
        if not hasattr(self.exchange, "fetch_positions"):
            return []
        for attempt in range(1, 3):
            try:
                return self.exchange.fetch_positions()
            except Exception as e:
                log(self.logger, "WARNING", "FETCH_POSITIONS_FAIL", attempt=attempt, error=str(e))
                time.sleep(0.7 * attempt)
        return []


def init_exchange(cfg, logger) -> ExchangeClient:
    ex_class = getattr(ccxt, cfg.exchange_id)
    exchange = ex_class({
        "apiKey": cfg.api_key,
        "secret": cfg.api_secret,
        "enableRateLimit": cfg.enable_rate_limit,
        "timeout": cfg.exchange_timeout_ms,
        "options": {"defaultType": "swap"},
    })
    if cfg.testnet and hasattr(exchange, "set_sandbox_mode"):
        exchange.set_sandbox_mode(True)

    try:
        exchange.load_markets()
    except Exception as e:
        log(logger, "WARNING", "LOAD_MARKETS_FAIL", error=str(e))

    return ExchangeClient(exchange, logger)
