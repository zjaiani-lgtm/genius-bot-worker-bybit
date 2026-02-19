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
    One client that supports:
      - Binance Spot (native OCO supported)
      - Bybit USDT Perp / Spot (NO native OCO -> TP/SL as 2 reduceOnly orders)

    Controlled by env:
      MODE=DEMO|TESTNET|LIVE
      EXCHANGE=binance|bybit
      MARKET_TYPE=spot|swap  (default spot)
    """

    BINANCE_TESTNET_REST_BASE = "https://testnet.binance.vision/api"

    def __init__(self):
        # ---- core guards ----
        self.mode = _env("MODE", "DEMO").upper()  # DEMO | TESTNET | LIVE
        self.kill_switch = _to_bool(_env("KILL_SWITCH", "false"))
        self.live_confirmation = _to_bool(_env("LIVE_CONFIRMATION", "false"))

        # ---- trading constraints ----
        self.max_quote_per_trade = float(_env("MAX_QUOTE_PER_TRADE", "10") or "10")
        self.symbol_whitelist: Set[str] = set(
            s.strip().upper()
            for s in _env("SYMBOL_WHITELIST", "BTC/USDT").split(",")
            if s.strip()
        )

        # ---- exchange selection ----
        self.exchange_name = _env("EXCHANGE", "binance").lower()  # binance | bybit
        self.market_type = _env("MARKET_TYPE", "spot").lower()    # spot | swap

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
                "options": {"defaultType": "spot"},  # spot-only here
            })

            # TESTNET handling
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

            default_type = "swap" if self.market_type == "swap" else "spot"

            ex = ccxt.bybit({
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                "options": {"defaultType": default_type},
            })

            if self.mode == "TESTNET":
                try:
                    ex.set_sandbox_mode(True)
                except Exception as e:
                    logger.warning(f"BYBIT_SANDBOX_WARN | err={e}")

            return ex

        raise ExchangeClientError(f"Unsupported EXCHANGE={self.exchange_name}. Use binance|bybit.")

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
        if symbol and symbol.upper() not in self.symbol_whitelist:
            raise LiveTradingBlocked(f"Symbol not allowed by whitelist: {symbol}.")
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

    def get_min_notional(self, symbol: str) -> float:
        """
        Return minimum notional/cost. Binance has MIN_NOTIONAL/NOTIONAL filters.
        Bybit mostly exposes min cost in ccxt normalized market limits (if available).
        """
        try:
            m = self.exchange.market(symbol)

            cost_min = (((m.get("limits") or {}).get("cost") or {}).get("min"))
            if cost_min is not None:
                return float(cost_min)

            if self.exchange_name == "binance":
                info = m.get("info") or {}
                filters = info.get("filters") or []
                for f in filters:
                    t = str(f.get("filterType") or "").upper()
                    if t in ("MIN_NOTIONAL", "NOTIONAL"):
                        v = f.get("minNotional")
                        if v is None:
                            v = f.get("minNotionalValue")
                        if v is None:
                            v = f.get("notional")
                        if v is not None:
                            return float(v)
        except Exception as e:
            logger.warning(f"MIN_NOTIONAL_LOOKUP_FAIL | exchange={self.exchange_name} symbol={symbol} err={e}")

        return 0.0

    # ----------------------------
    # Precision helpers
    # ----------------------------
    def floor_amount(self, symbol: str, amount: float) -> float:
        try:
            s = self.exchange.amount_to_precision(symbol, amount)
            return float(s)
        except Exception:
            return float(amount)

    def floor_price(self, symbol: str, price: float) -> float:
        try:
            s = self.exchange.price_to_precision(symbol, price)
            return float(s)
        except Exception:
            return float(price)

    def _amount_str(self, symbol: str, amount: float) -> str:
        return str(self.exchange.amount_to_precision(symbol, amount))

    def _price_str(self, symbol: str, price: float) -> str:
        return str(self.exchange.price_to_precision(symbol, price))

    # ----------------------------
    # Orders
    # ----------------------------
    def _bybit_min_constraints(self, symbol: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
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
        Binance spot: uses quoteOrderQty (best)
        Bybit: calculates base qty = quote_amount / last_price
        Adds: min amount / min cost checks (prevents "precision 0.001" failures)
        """
        self._guard(symbol, quote_amount=quote_amount)

        try:
            # Ensure markets are loaded for limits/precision
            try:
                if not getattr(self.exchange, "markets", None):
                    self.exchange.load_markets()
            except Exception:
                pass

            if self.exchange_name == "binance":
                params = {"quoteOrderQty": float(quote_amount)}
                return self.exchange.create_order(symbol, "market", "buy", None, None, params)

            # ---- BYBIT sizing ----
            last = self.fetch_last_price(symbol)
            if last <= 0:
                raise ExchangeClientError("Invalid last price for sizing.")

            min_base, min_cost, step = self._bybit_min_constraints(symbol)

            base_raw = float(quote_amount) / float(last)

            # If exchange exposes min_cost, enforce it first
            if min_cost is not None:
                # add tiny buffer for price movement
                if float(quote_amount) < float(min_cost) * 1.01:
                    raise ExchangeClientError(
                        f"BUY_BLOCKED_MIN_COST | symbol={symbol} need_quote>={float(min_cost):.4f} "
                        f"have={float(quote_amount):.4f}"
                    )

            # If exchange exposes min_base, ensure quote can reach it
            if min_base is not None:
                need_quote_for_min = float(min_base) * float(last) * 1.02
                if float(quote_amount) < need_quote_for_min:
                    raise ExchangeClientError(
                        f"BUY_BLOCKED_MIN_AMOUNT | symbol={symbol} min_base={float(min_base):.6f} "
                        f"last={float(last):.2f} need_quote>={need_quote_for_min:.2f} have={float(quote_amount):.2f}"
                    )
                base_raw = max(base_raw, float(min_base))

            # Floor to precision (may round DOWN)
            base_amt = self.floor_amount(symbol, base_raw)

            # If rounding down made it smaller than min_base, bump UP to step or min_base
            if min_base is not None and float(base_amt) < float(min_base):
                if step is not None:
                    base_amt = _ceil_to_step(float(base_amt), float(step))
                base_amt = max(float(base_amt), float(min_base))
                # after bump, re-apply precision formatting
                base_amt = float(self.exchange.amount_to_precision(symbol, base_amt))

            if base_amt <= 0:
                raise ExchangeClientError("Computed base amount <= 0 after precision.")

            logger.info(
                f"BUY_SIZE_DEBUG | exchange=bybit symbol={symbol} quote={float(quote_amount):.4f} "
                f"last={float(last):.2f} base_raw={base_raw:.8f} base_final={float(base_amt):.8f} "
                f"min_base={min_base} min_cost={min_cost} step={step}"
            )

            return self.exchange.create_order(symbol, "market", "buy", float(base_amt), None, {})

        except LiveTradingBlocked:
            raise
        except ExchangeClientError:
            raise
        except Exception as e:
            raise ExchangeClientError(f"Market buy failed: {e}")

    def place_market_sell(self, symbol: str, base_amount: float) -> Dict[str, Any]:
        self._guard(symbol)
        try:
            amt = float(self.exchange.amount_to_precision(symbol, base_amount))
            return self.exchange.create_order(symbol, "market", "sell", float(amt), None)
        except LiveTradingBlocked:
            raise
        except Exception as e:
            raise ExchangeClientError(f"Market sell failed: {e}")

    def place_limit_sell_amount(
        self, symbol: str, base_amount: float, price: float, reduce_only: bool = False
    ) -> Dict[str, Any]:
        self._guard(symbol)
        try:
            amt = float(self.exchange.amount_to_precision(symbol, base_amount))
            px = float(self.exchange.price_to_precision(symbol, price))
            params = {}
            if reduce_only:
                params["reduceOnly"] = True
            return self.exchange.create_order(symbol, "limit", "sell", float(amt), float(px), params)
        except LiveTradingBlocked:
            raise
        except Exception as e:
            raise ExchangeClientError(f"Limit sell failed: {e}")

    def place_stop_loss_limit_sell(self, symbol: str, base_amount: float, stop_price: float, limit_price: float) -> Dict[str, Any]:
        """
        Binance-only STOP_LOSS_LIMIT in spot.
        """
        self._guard(symbol)
        if self.exchange_name != "binance":
            raise ExchangeClientError("STOP_LOSS_LIMIT sell is Binance-spot specific. Use place_stop_loss_market_sell for Bybit.")
        try:
            amt = float(self.exchange.amount_to_precision(symbol, base_amount))
            stop_px = float(self.exchange.price_to_precision(symbol, stop_price))
            limit_px = float(self.exchange.price_to_precision(symbol, limit_price))
            params = {"stopPrice": stop_px, "timeInForce": "GTC"}
            return self.exchange.create_order(symbol, "STOP_LOSS_LIMIT", "sell", float(amt), float(limit_px), params)
        except LiveTradingBlocked:
            raise
        except Exception as e:
            raise ExchangeClientError(f"Stop-loss-limit sell failed: {e}")

    def place_stop_loss_market_sell(self, symbol: str, base_amount: float, stop_price: float, reduce_only: bool = True) -> Dict[str, Any]:
        """
        Futures-friendly stop market.
        """
        self._guard(symbol)
        try:
            amt = float(self.exchange.amount_to_precision(symbol, base_amount))
            trigger = float(self.exchange.price_to_precision(symbol, stop_price))
            params = {"reduceOnly": bool(reduce_only), "triggerPrice": trigger}
            params.setdefault("triggerDirection", 2)  # fall for SL on long
            return self.exchange.create_order(symbol, "market", "sell", float(amt), None, params)
        except LiveTradingBlocked:
            raise
        except Exception as e:
            raise ExchangeClientError(f"Stop-loss market sell failed: {e}")

    # ----------------------------
    # Binance native OCO
    # ----------------------------
    def place_oco_sell(self, symbol: str, base_amount: float, tp_price: float, sl_stop_price: float, sl_limit_price: float) -> Dict[str, Any]:
        """
        Native Binance Spot OCO.
        IMPORTANT: use STRING precision to avoid -1111 precision errors.
        """
        self._guard(symbol)

        if self.exchange_name != "binance":
            raise ExchangeClientError("Native OCO is Binance-spot only. Use place_tp_sl_orders_for_long() for Bybit.")

        try:
            qty = self._amount_str(symbol, base_amount)
            price = self._price_str(symbol, tp_price)
            stop_price = self._price_str(symbol, sl_stop_price)
            stop_limit_price = self._price_str(symbol, sl_limit_price)

            payload = {
                "symbol": self.exchange.market_id(symbol),
                "side": "SELL",
                "quantity": qty,
                "price": price,
                "stopPrice": stop_price,
                "stopLimitPrice": stop_limit_price,
                "stopLimitTimeInForce": "GTC",
            }

            res = self.exchange.privatePostOrderOco(payload)
            return {"raw": res}
        except LiveTradingBlocked:
            raise
        except Exception as e:
            raise ExchangeClientError(f"OCO sell failed: {e}")

    # ----------------------------
    # Bybit-friendly TP/SL-first (2 orders)
    # ----------------------------
    def place_tp_sl_orders_for_long(
        self,
        symbol: str,
        base_amount: float,
        tp_price: float,
        sl_stop_price: float,
    ) -> Dict[str, Any]:
        """
        For Bybit (and generally futures): place TP limit sell + SL stop-market sell (reduceOnly).
        Returns: {"tp": <order>, "sl": <order>}
        """
        self._guard(symbol)
        try:
            amt = float(self.exchange.amount_to_precision(symbol, base_amount))
            tp_px = float(self.exchange.price_to_precision(symbol, tp_price))
            sl_trigger = float(self.exchange.price_to_precision(symbol, sl_stop_price))

            tp = self.exchange.create_order(
                symbol, "limit", "sell", float(amt), float(tp_px),
                {"reduceOnly": True}
            )

            sl = self.exchange.create_order(
                symbol, "market", "sell", float(amt), None,
                {
                    "reduceOnly": True,
                    "triggerPrice": sl_trigger,
                    "triggerDirection": 2,  # fall
                }
            )

            return {"tp": tp, "sl": sl}
        except LiveTradingBlocked:
            raise
        except Exception as e:
            raise ExchangeClientError(f"TP/SL orders failed: {e}")


def get_exchange_client() -> UnifiedClient:
    """
    Factory function for the rest of your codebase.
    """
    return UnifiedClient()


# Backward compatibility alias
BinanceSpotClient = UnifiedClient
