# execution/exchange_client.py
from __future__ import annotations

import os
import time
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

import ccxt

logger = logging.getLogger("gbm")


@dataclass
class OrderResult:
    order_id: str
    raw: Dict[str, Any]


class ExchangeClient:
    """
    Thin wrapper around ccxt exchange instance.
    Provides normalized methods used by ExecutionEngine:
      - create_market_buy(symbol, quote_amount)
      - create_limit_sell(symbol, amount, price)
      - create_stop_limit_sell(symbol, amount, stop_price, limit_price)
      - fetch_ticker(symbol)
      - fetch_order(order_id, symbol)
      - cancel_order(order_id, symbol)
    """

    def __init__(self, ccxt_exchange: Any, market_type: str):
        self.ex = ccxt_exchange
        self.market_type = (market_type or "spot").lower()

    # ---------------------------
    # Helpers
    # ---------------------------
    def _safe_milliseconds(self) -> int:
        return int(time.time() * 1000)

    def _exchange_id(self) -> str:
        try:
            return str(getattr(self.ex, "id", "") or "")
        except Exception:
            return ""

    def _is_bybit(self) -> bool:
        return self._exchange_id() == "bybit"

    def _is_binance(self) -> bool:
        return self._exchange_id() == "binance"

    # ---------------------------
    # Public API
    # ---------------------------
    def fetch_ticker(self, symbol: str) -> Dict[str, Any]:
        return self.ex.fetch_ticker(symbol)

    def fetch_order(self, order_id: str, symbol: str) -> Dict[str, Any]:
        return self.ex.fetch_order(order_id, symbol)

    def cancel_order(self, order_id: str, symbol: str) -> Dict[str, Any]:
        return self.ex.cancel_order(order_id, symbol)

    def create_market_buy(self, symbol: str, quote_amount: float) -> OrderResult:
        """
        Attempts to place a MARKET BUY using quote currency amount (e.g., 7 USDT).
        Different exchanges differ:
          - Binance Spot supports `quoteOrderQty`
          - Bybit usually expects base amount; quote-based market buy may not be supported in CCXT uniformly.
        We do a best-effort:
          1) Try quoteOrderQty (Binance Spot)
          2) Else estimate base amount using last price and submit amount
        """
        symbol = str(symbol)
        quote_amount = float(quote_amount)

        # 1) Try quoteOrderQty style params
        params = {}
        if self._is_binance() and self.market_type == "spot":
            params["quoteOrderQty"] = quote_amount

        if params:
            try:
                o = self.ex.create_order(symbol, "market", "buy", None, None, params)
                return OrderResult(order_id=str(o.get("id")), raw=o)
            except Exception as e:
                logger.warning(f"MARKET_BUY_QUOTE_PARAM_FAIL | symbol={symbol} quote={quote_amount} err={e}")

        # 2) Fallback: estimate base amount using ticker last
        try:
            t = self.ex.fetch_ticker(symbol)
            last = float(t.get("last") or 0.0)
        except Exception:
            last = 0.0

        if last <= 0:
            raise RuntimeError(f"Cannot estimate base amount for market buy: ticker last not available | symbol={symbol}")

        base_amount = quote_amount / last

        # Some exchanges require amount to be rounded to step size — ccxt handles precision if markets loaded
        try:
            if hasattr(self.ex, "amount_to_precision"):
                base_amount = float(self.ex.amount_to_precision(symbol, base_amount))
        except Exception:
            pass

        o = self.ex.create_order(symbol, "market", "buy", base_amount, None, {})
        return OrderResult(order_id=str(o.get("id")), raw=o)

    def create_limit_sell(self, symbol: str, amount: float, price: float) -> OrderResult:
        symbol = str(symbol)
        amount = float(amount)
        price = float(price)

        try:
            if hasattr(self.ex, "amount_to_precision"):
                amount = float(self.ex.amount_to_precision(symbol, amount))
            if hasattr(self.ex, "price_to_precision"):
                price = float(self.ex.price_to_precision(symbol, price))
        except Exception:
            pass

        o = self.ex.create_order(symbol, "limit", "sell", amount, price, {})
        return OrderResult(order_id=str(o.get("id")), raw=o)

    def create_stop_limit_sell(self, symbol: str, amount: float, stop_price: float, limit_price: float) -> OrderResult:
        """
        Stop-Limit SELL.
        CCXT params differ by exchange. We'll try common patterns:
          - Bybit: type can be 'limit' with params {'stopPrice': X, 'triggerPrice': X} or 'stopLimit'
          - Binance Spot: type='STOP_LOSS_LIMIT' (ccxt unified: 'stop_limit' sometimes) with params {'stopPrice': X}
        We do a robust approach:
          1) Try unified 'stop_limit'
          2) Fallback to 'limit' + params stopPrice
        """
        symbol = str(symbol)
        amount = float(amount)
        stop_price = float(stop_price)
        limit_price = float(limit_price)

        try:
            if hasattr(self.ex, "amount_to_precision"):
                amount = float(self.ex.amount_to_precision(symbol, amount))
            if hasattr(self.ex, "price_to_precision"):
                stop_price = float(self.ex.price_to_precision(symbol, stop_price))
                limit_price = float(self.ex.price_to_precision(symbol, limit_price))
        except Exception:
            pass

        # 1) Try unified stop_limit
        try:
            o = self.ex.create_order(symbol, "stop_limit", "sell", amount, limit_price, {"stopPrice": stop_price})
            return OrderResult(order_id=str(o.get("id")), raw=o)
        except Exception as e1:
            logger.warning(f"STOP_LIMIT_UNIFIED_FAIL | symbol={symbol} err={e1}")

        # 2) Fallback patterns
        params = {"stopPrice": stop_price}

        # Bybit sometimes prefers triggerPrice
        if self._is_bybit():
            params["triggerPrice"] = stop_price

        try:
            o = self.ex.create_order(symbol, "limit", "sell", amount, limit_price, params)
            return OrderResult(order_id=str(o.get("id")), raw=o)
        except Exception as e2:
            raise RuntimeError(f"Stop-limit sell failed | symbol={symbol} err={e2}") from e2


def build_exchange_client() -> ExchangeClient:
    """
    Factory expected by execution_engine.py:
      EXCHANGE=bybit|binance (default bybit)
      MARKET_TYPE=spot|swap (default spot)
      API_KEY / API_SECRET
      SANDBOX=true|false (optional; some exchanges support)
      CCXT_ENABLE_RATE_LIMIT=true|false (default true)
    """
    exchange_name = (os.getenv("EXCHANGE", "bybit") or "bybit").strip().lower()
    market_type = (os.getenv("MARKET_TYPE", "spot") or "spot").strip().lower()

    api_key = os.getenv("API_KEY") or os.getenv("EXCHANGE_API_KEY") or ""
    api_secret = os.getenv("API_SECRET") or os.getenv("EXCHANGE_API_SECRET") or ""

    enable_rate_limit = str(os.getenv("CCXT_ENABLE_RATE_LIMIT", "true")).lower() in ("1", "true", "yes", "y")
    sandbox = str(os.getenv("SANDBOX", "false")).lower() in ("1", "true", "yes", "y")

    if not hasattr(ccxt, exchange_name):
        raise RuntimeError(f"Unsupported EXCHANGE='{exchange_name}'. ccxt has no such exchange class.")

    ex_cls = getattr(ccxt, exchange_name)

    options: Dict[str, Any] = {}
    # ccxt uses options['defaultType'] for derivatives on several exchanges (e.g., bybit, okx)
    if market_type in ("swap", "future", "futures"):
        options["defaultType"] = "swap"
    else:
        options["defaultType"] = "spot"

    ex = ex_cls(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": enable_rate_limit,
            "options": options,
        }
    )

    # Some exchanges require explicit sandbox mode call
    try:
        if sandbox and hasattr(ex, "set_sandbox_mode"):
            ex.set_sandbox_mode(True)
    except Exception as e:
        logger.warning(f"SANDBOX_MODE_FAIL | exchange={exchange_name} err={e}")

    # Load markets early so amount/price precision helpers work
    try:
        ex.load_markets()
    except Exception as e:
        logger.warning(f"LOAD_MARKETS_FAIL | exchange={exchange_name} err={e}")

    logger.info(f"EXCHANGE_CLIENT_READY | exchange={exchange_name} market_type={market_type} sandbox={sandbox}")
    return ExchangeClient(ex, market_type=market_type)


# --- Backward compatibility ---
# Some older code might do: from execution.exchange_client import exchange_client
def exchange_client() -> ExchangeClient:
    return build_exchange_client()


__all__ = ["ExchangeClient", "OrderResult", "build_exchange_client", "exchange_client"]
