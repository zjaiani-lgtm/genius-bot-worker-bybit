import os
import math
import logging
from typing import Any, Dict, Optional, Set, Tuple

import ccxt

logger = logging.getLogger("gbm")


class ExchangeClientError(Exception):
    pass


class LiveTradingBlocked(Exception):
    pass


def _to_bool(v: str, default: bool = False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _ceil_to_step(x: float, step: float) -> float:
    if step is None or step <= 0:
        return x
    return math.ceil(x / step) * step


def _infer_amount_step_from_precision(prec: Optional[int]) -> Optional[float]:
    """
    ccxt market['precision']['amount'] is typically number of decimals.
    step = 10^-prec.
    """
    if prec is None:
        return None
    try:
        prec_int = int(prec)
        if prec_int < 0:
            return None
        return 10 ** (-prec_int)
    except Exception:
        return None


class UnifiedClient:
    """
    SPOT-only friendly client, especially for Bybit Spot.

    Controlled by env:
      MODE=DEMO|TESTNET|LIVE
      EXCHANGE=binance|bybit
      MARKET_TYPE=spot (default spot; swap not supported in this version)
    """

    BINANCE_TESTNET_REST_BASE = "https://testnet.binance.vision/api"

    def __init__(self):
        # ---- core guards ----
        self.mode = _env("MODE", "DEMO").upper()  # DEMO | TESTNET | LIVE
        self.kill_switch = _to_bool(_env("KILL_SWITCH", "false"))
        self.live_confirmation = _to_bool(_env("LIVE_CONFIRMATION", "false"))

        # ---- trading constraints ----
        self.max_quote_per_trade = float(_env("MAX_QUOTE_PER_TRADE", "10") or "10")

        # NOTE: store symbols as canonical (ccxt expects case-sensitive sometimes,
        # but uppercase generally fine). We'll keep as user provided, stripped.
        whitelist_raw = [s.strip() for s in _env("SYMBOL_WHITELIST", "BTC/USDT").split(",") if s.strip()]
        self.symbol_whitelist: Set[str] = set(whitelist_raw)

        # ---- exchange selection ----
        self.exchange_name = _env("EXCHANGE", "bybit").lower()  # binance | bybit
        self.market_type = _env("MARKET_TYPE", "spot").lower()  # spot only here

        if self.market_type != "spot":
            raise ExchangeClientError("This build is SPOT-only. Set MARKET_TYPE=spot.")

        # Guard against perpetual symbols in SPOT mode (":USDT" etc.)
        bad = [s for s in self.symbol_whitelist if ":" in s]
        if bad:
            raise ExchangeClientError(
                f"SPOT_MODE_SYMBOL_ERROR | Found perpetual-style symbols in whitelist: {bad}. "
                f"Use spot symbols like ETH/USDT (no ':USDT')."
            )

        # init ccxt exchange
        self.exchange = self._build_exchange()

        # warm up markets for precision helpers
        try:
            self.exchange.load_markets()
        except Exception as e:
            logger.warning(f"LOAD_MARKETS_WARN | exchange={self.exchange_name} err={e}")

    # ----------------------------
    # Build exchange (ccxt)
    # ----------------------------
    def _build_exchange(self):
        if self.exchange_name == "binance":
            api_key = _env("BINANCE_API_KEY", "")
            api_secret = _env("BINANCE_API_SECRET", "")

            if self.mode in ("LIVE", "TESTNET"):
                if not api_key or not api_secret:
                    raise ExchangeClientError("Missing BINANCE_API_KEY / BINANCE_API_SECRET for LIVE/TESTNET.")

            ex = ccxt.binance({
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
            })

            if self.mode == "TESTNET":
                ex.urls["api"] = {
                    "public": self.BINANCE_TESTNET_REST_BASE,
                    "private": self.BINANCE_TESTNET_REST_BASE,
                }
                ex.options["fetchCurrencies"] = False

            return ex

        if self.exchange_name == "bybit":
            api_key = _env("BYBIT_API_KEY", "")
            api_secret = _env("BYBIT_API_SECRET", "")

            if self.mode in ("LIVE", "TESTNET"):
                if not api_key or not api_secret:
                    raise ExchangeClientError("Missing BYBIT_API_KEY / BYBIT_API_SECRET for LIVE/TESTNET.")

            # SPOT ONLY
            ex = ccxt.bybit({
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                "options": {"defaultType": "spot"},
            })

            if self.mode == "TESTNET":
                # Bybit testnet via sandbox mode (depends on ccxt version)
                try:
                    ex.set_sandbox_mode(True)
                except Exception as e:
                    logger.warning(f"BYBIT_SANDBOX_WARN | err={e}")

            return ex

        raise ExchangeClientError(f"Unsupported EXCHANGE={self.exchange_name}. Use binance|bybit.")

    # ----------------------------
    # Bybit V5 spot params helper
    # ----------------------------
    def _bybit_spot_params(self) -> Dict[str, Any]:
        # Bybit v5 sometimes prefers explicit category for spot endpoints
        if self.exchange_name == "bybit":
            return {"category": "spot"}
        return {}

    # ----------------------------
    # Guards / diagnostics
    # ----------------------------
    def _guard(self, symbol: str, quote_amount: Optional[float] = None) -> None:
        if self.kill_switch:
            raise LiveTradingBlocked("KILL_SWITCH is ON.")
        if self.mode == "LIVE" and not self.live_confirmation:
            raise LiveTradingBlocked("LIVE_CONFIRMATION is OFF.")
        if self.mode == "DEMO":
            raise LiveTradingBlocked("MODE=DEMO -> exchange client must not execute real orders.")
        if symbol and symbol not in self.symbol_whitelist:
            raise LiveTradingBlocked(f"Symbol not allowed by whitelist: {symbol}.")
        if ":" in symbol:
            raise LiveTradingBlocked(f"SPOT mode does not allow perpetual symbol format: {symbol}")
        if quote_amount is not None and quote_amount > self.max_quote_per_trade:
            raise LiveTradingBlocked(
                f"quote_amount {quote_amount} exceeds MAX_QUOTE_PER_TRADE={self.max_quote_per_trade}"
            )

    def diagnostics(self) -> Dict[str, Any]:
        try:
            bal = self.exchange.fetch_balance()
            sym = next(iter(self.symbol_whitelist)) if self.symbol_whitelist else "BTC/USDT"
            t = self.exchange.fetch_ticker(sym)
            usdt_free = float((bal.get("free", {}) or {}).get("USDT", 0.0) or 0.0)
            return {
                "exchange": self.exchange_name,
                "market_type": self.market_type,
                "mode": self.mode,
                "kill_switch": self.kill_switch,
                "live_confirmation": self.live_confirmation,
                "symbol_probe": sym,
                "last_price": float(t.get("last") or 0.0),
                "usdt_free": usdt_free,
                "ok": True,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ----------------------------
    # Market helpers
    # ----------------------------
    def fetch_last_price(self, symbol: str) -> float:
        t = self.exchange.fetch_ticker(symbol)
        return float(t["last"])

    def fetch_balance_free(self, asset: str) -> float:
        bal = self.exchange.fetch_balance()
        return float((bal.get("free", {}) or {}).get(asset.upper(), 0.0) or 0.0)

    def fetch_order(self, order_id: str, symbol: str) -> Dict[str, Any]:
        return self.exchange.fetch_order(str(order_id), symbol)

    def cancel_order(self, order_id: str, symbol: str) -> Dict[str, Any]:
        return self.exchange.cancel_order(str(order_id), symbol)

    # ----------------------------
    # Precision helpers
    # ----------------------------
    def floor_amount(self, symbol: str, amount: float) -> float:
        try:
            s = self.exchange.amount_to_precision(symbol, amount)
            return float(s)
        except Exception:
            return float(amount)

    # ----------------------------
    # Orders (SPOT)
    # ----------------------------
    def _min_constraints(self, symbol: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """
        Returns: (min_base_amount, min_cost, amount_step)
        All may be None if exchange doesn't expose.
        """
        try:
            m = self.exchange.market(symbol)
            limits = m.get("limits") or {}
            amt_min = ((limits.get("amount") or {}).get("min"))
            cost_min = ((limits.get("cost") or {}).get("min"))
            prec_amt = (m.get("precision") or {}).get("amount")
            step = _infer_amount_step_from_precision(prec_amt)

            min_base = float(amt_min) if amt_min is not None else None
            min_cost = float(cost_min) if cost_min is not None else None
            return min_base, min_cost, step
        except Exception:
            return None, None, None

    def place_market_buy_by_quote(self, symbol: str, quote_amount: float) -> Dict[str, Any]:
        """
        SPOT market buy by quote:
          - Binance supports quoteOrderQty natively.
          - Bybit SPOT: we compute base qty = quote_amount / last and send amount.
        """
        self._guard(symbol, quote_amount=quote_amount)

        try:
            # Ensure markets loaded
            try:
                if not getattr(self.exchange, "markets", None):
                    self.exchange.load_markets()
            except Exception:
                pass

            if self.exchange_name == "binance":
                params = {"quoteOrderQty": float(quote_amount)}
                return self.exchange.create_order(symbol, "market", "buy", None, None, params)

            # ---- BYBIT SPOT sizing ----
            last = self.fetch_last_price(symbol)
            if last <= 0:
                raise ExchangeClientError("Invalid last price for sizing.")

            min_base, min_cost, step = self._min_constraints(symbol)
            base_raw = float(quote_amount) / float(last)

            # cost check if exposed
            if min_cost is not None and float(quote_amount) < float(min_cost) * 1.01:
                raise ExchangeClientError(
                    f"BUY_BLOCKED_MIN_COST | symbol={symbol} need_quote>={float(min_cost):.4f} "
                    f"have={float(quote_amount):.4f}"
                )

            # min base check if exposed
            if min_base is not None:
                need_quote_for_min = float(min_base) * float(last) * 1.02
                if float(quote_amount) < need_quote_for_min:
                    raise ExchangeClientError(
                        f"BUY_BLOCKED_MIN_AMOUNT | symbol={symbol} min_base={float(min_base):.6f} "
                        f"last={float(last):.6f} need_quote>={need_quote_for_min:.6f} have={float(quote_amount):.6f}"
                    )
                base_raw = max(base_raw, float(min_base))

            base_amt = self.floor_amount(symbol, base_raw)

            # If rounding down broke min_base, bump up
            if min_base is not None and float(base_amt) < float(min_base):
                if step is not None:
                    base_amt = _ceil_to_step(float(base_amt), float(step))
                base_amt = max(float(base_amt), float(min_base))
                base_amt = float(self.exchange.amount_to_precision(symbol, base_amt))

            if float(base_amt) <= 0:
                raise ExchangeClientError("Computed base amount <= 0 after precision.")

            logger.info(
                f"BUY_SIZE_DEBUG | exchange=bybit_spot symbol={symbol} quote={float(quote_amount):.4f} "
                f"last={float(last):.6f} base_raw={base_raw:.8f} base_final={float(base_amt):.8f} "
                f"min_base={min_base} min_cost={min_cost} step={step}"
            )

            params = self._bybit_spot_params()
            return self.exchange.create_order(symbol, "market", "buy", float(base_amt), None, params)

        except LiveTradingBlocked:
            raise
        except Exception as e:
            raise ExchangeClientError(f"Market buy failed: {e}")

    def place_market_sell(self, symbol: str, base_amount: float) -> Dict[str, Any]:
        self._guard(symbol)
        try:
            amt = float(self.exchange.amount_to_precision(symbol, base_amount))
            params = self._bybit_spot_params()
            return self.exchange.create_order(symbol, "market", "sell", float(amt), None, params)
        except LiveTradingBlocked:
            raise
        except Exception as e:
            raise ExchangeClientError(f"Market sell failed: {e}")

    def place_limit_sell_amount(self, symbol: str, base_amount: float, price: float) -> Dict[str, Any]:
        self._guard(symbol)
        try:
            amt = float(self.exchange.amount_to_precision(symbol, base_amount))
            px = float(self.exchange.price_to_precision(symbol, price))
            params = self._bybit_spot_params()
            return self.exchange.create_order(symbol, "limit", "sell", float(amt), float(px), params)
        except LiveTradingBlocked:
            raise
        except Exception as e:
            raise ExchangeClientError(f"Limit sell failed: {e}")


def get_exchange_client() -> UnifiedClient:
    return UnifiedClient()


# Backward compatibility alias
BinanceSpotClient = UnifiedClient
