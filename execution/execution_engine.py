# execution/exchange_client.py
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import ccxt


class ExchangeClientError(Exception):
    pass


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name, default)
    if v is None:
        return None
    v = str(v).strip()
    return v if v else None


def _is_true(name: str, default: str = "false") -> bool:
    return str(os.getenv(name, default)).lower() == "true"


@dataclass
class OrderResult:
    order_id: str
    raw: Dict[str, Any]


class BaseSpotClient:
    """
    Minimal spot interface used by ExecutionEngine:
      - create_market_buy(symbol, quote_amount)
      - create_market_sell(symbol, base_amount)
      - create_limit_sell(symbol, base_amount, price)
      - create_stop_limit_sell(symbol, base_amount, stop_price, limit_price)
      - cancel_order(order_id, symbol)
      - fetch_order(order_id, symbol)
      - fetch_ticker(symbol)
    """

    def __init__(self):
        self.exchange = None  # set by subclass

    def _sleep_rate(self):
        time.sleep(0.2)

    def fetch_ticker(self, symbol: str) -> Dict[str, Any]:
        self._sleep_rate()
        return self.exchange.fetch_ticker(symbol)

    def fetch_order(self, order_id: str, symbol: str) -> Dict[str, Any]:
        self._sleep_rate()
        return self.exchange.fetch_order(order_id, symbol)

    def cancel_order(self, order_id: str, symbol: str) -> Dict[str, Any]:
        self._sleep_rate()
        return self.exchange.cancel_order(order_id, symbol)

    # --- simplified wrappers ---
    def create_market_buy(self, symbol: str, quote_amount: float) -> OrderResult:
        self._sleep_rate()
        # ccxt spot market buy can accept quoteOrderQty on some exchanges; safest is params
        params = {}
        # many exchanges support quoteOrderQty; if not, engine should compute base qty.
        params["quoteOrderQty"] = float(quote_amount)
        o = self.exchange.create_order(symbol, "market", "buy", None, None, params)
        return OrderResult(order_id=str(o.get("id")), raw=o)

    def create_market_sell(self, symbol: str, base_amount: float) -> OrderResult:
        self._sleep_rate()
        o = self.exchange.create_order(symbol, "market", "sell", float(base_amount), None, {})
        return OrderResult(order_id=str(o.get("id")), raw=o)

    def create_limit_sell(self, symbol: str, base_amount: float, price: float) -> OrderResult:
        self._sleep_rate()
        o = self.exchange.create_order(symbol, "limit", "sell", float(base_amount), float(price), {})
        return OrderResult(order_id=str(o.get("id")), raw=o)

    def create_stop_limit_sell(
        self, symbol: str, base_amount: float, stop_price: float, limit_price: float
    ) -> OrderResult:
        """
        Exchange-specific params differ. We'll map for binance/bybit spot.
        """
        raise NotImplementedError


class BinanceSpotClient(BaseSpotClient):
    def __init__(self):
        super().__init__()
        api_key = _env("BINANCE_API_KEY")
        api_secret = _env("BINANCE_API_SECRET")

        mode = str(os.getenv("MODE", "DEMO")).upper()
        live_or_test = mode in ("LIVE", "TESTNET")

        if live_or_test and (not api_key or not api_secret):
            raise ExchangeClientError("Missing BINANCE_API_KEY / BINANCE_API_SECRET for LIVE/TESTNET.")

        self.exchange = ccxt.binance({
            "apiKey": api_key or "",
            "secret": api_secret or "",
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        })

        # Binance spot testnet is not fully supported via ccxt in all cases; keep off unless you know it works.
        # If you do use testnet, you'd set urls manually here.

    def create_stop_limit_sell(self, symbol: str, base_amount: float, stop_price: float, limit_price: float) -> OrderResult:
        self._sleep_rate()
        params = {"stopPrice": float(stop_price)}
        o = self.exchange.create_order(symbol, "limit", "sell", float(base_amount), float(limit_price), params)
        return OrderResult(order_id=str(o.get("id")), raw=o)


class BybitSpotClient(BaseSpotClient):
    def __init__(self):
        super().__init__()
        api_key = _env("BYBIT_API_KEY")
        api_secret = _env("BYBIT_API_SECRET")

        mode = str(os.getenv("MODE", "DEMO")).upper()
        live_or_test = mode in ("LIVE", "TESTNET")

        if live_or_test and (not api_key or not api_secret):
            raise ExchangeClientError("Missing BYBIT_API_KEY / BYBIT_API_SECRET for LIVE/TESTNET.")

        self.exchange = ccxt.bybit({
            "apiKey": api_key or "",
            "secret": api_secret or "",
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        })

        # Bybit testnet: ccxt supports "testnet" via urls for some markets.
        # We'll only enable if user sets BYBIT_TESTNET=true
        if _is_true("BYBIT_TESTNET", "false"):
            self.exchange.set_sandbox_mode(True)

    def create_stop_limit_sell(self, symbol: str, base_amount: float, stop_price: float, limit_price: float) -> OrderResult:
        self._sleep_rate()
        # Bybit spot conditional orders are exchange-specific; ccxt may require params.
        # This is a best-effort mapping:
        params = {
            "stopPrice": float(stop_price),
            # some bybit implementations accept "triggerPrice"
            "triggerPrice": float(stop_price),
        }
        o = self.exchange.create_order(symbol, "limit", "sell", float(base_amount), float(limit_price), params)
        return OrderResult(order_id=str(o.get("id")), raw=o)


def build_exchange_client() -> BaseSpotClient:
    """
    Factory: chooses exchange based on ENV EXCHANGE.
    Supported: bybit, binance (default).
    """
    ex = str(os.getenv("EXCHANGE", "binance")).strip().lower()
    market = str(os.getenv("MARKET_TYPE", "spot")).strip().lower()

    if market != "spot":
        # your worker currently is spot-based; keep strict
        raise ExchangeClientError(f"Unsupported MARKET_TYPE={market}. Only spot is supported in this worker.")

    if ex == "bybit":
        return BybitSpotClient()

    # default
    return BinanceSpotClient()
