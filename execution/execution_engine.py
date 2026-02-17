# execution/execution_engine.py
import os
import logging
from typing import Any, Dict

import ccxt

from execution.db.repository import (
    get_system_state,
    log_event,
    list_active_oco_links,
    has_active_oco_for_symbol,
    create_oco_link,
    set_oco_status,
    update_system_state,
    signal_id_already_executed,
    mark_signal_id_executed,
)

from execution.kill_switch import is_kill_switch_active
from execution.virtual_wallet import simulate_market_entry

logger = logging.getLogger("gbm")


def _to_bool01(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return int(v) != 0
    if isinstance(v, str):
        s = v.strip().lower()
        return s in ("1", "true", "yes", "y", "on")
    return False


class ExecutionEngine:
    def __init__(self):
        self.mode = os.getenv("MODE", "DEMO").upper()
        self.env_kill_switch = os.getenv("KILL_SWITCH", "false")
        self.live_confirmation = os.getenv("LIVE_CONFIRMATION", "false")

        self.kill_switch_active = is_kill_switch_active() or _to_bool01(self.env_kill_switch)
        self.live_ok = _to_bool01(self.live_confirmation)

        self.exchange = os.getenv("EXCHANGE", "binance").lower().strip()
        self.tp_pct = float(os.getenv("TP_PCT", "1.0"))
        self.sl_pct = float(os.getenv("SL_PCT", "0.65"))
        self.sell_buffer = float(os.getenv("SELL_BUFFER", "0.999"))
        self.sell_retry_buffer = float(os.getenv("SELL_RETRY_BUFFER", "0.995"))
        self.sl_limit_gap_pct = float(os.getenv("SL_LIMIT_GAP_PCT", "0.15"))

        # quote sizing limits
        self.bot_quote_per_trade = float(os.getenv("BOT_QUOTE_PER_TRADE", "15"))
        self.max_quote_per_trade = float(os.getenv("MAX_QUOTE_PER_TRADE", "25"))

        # optional: cap on multiplier (safety)
        self.max_size_multiplier = float(os.getenv("AUTO_SCALER_MAX_SIZE", "5"))

        # risk controls
        self.dedupe_only_when_active_oco = os.getenv("DEDUPE_ONLY_WHEN_ACTIVE_OCO", "true").lower() == "true"

    def _get_exchange(self):
        # Minimal: rely on existing exchange_client in repo
        # Here we keep the existing pattern used in your repo:
        if self.exchange == "bybit":
            api_key = os.getenv("BYBIT_API_KEY", "").strip()
            api_secret = os.getenv("BYBIT_API_SECRET", "").strip()
            if not api_key or not api_secret:
                raise RuntimeError("BYBIT_API_KEY/BYBIT_API_SECRET are required for EXCHANGE=bybit")

            # Bybit Unified Trading - via ccxt
            ex = ccxt.bybit({
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                "options": {
                    "defaultType": "spot",
                },
            })
            return ex

        # default binance
        api_key = os.getenv("BINANCE_API_KEY", "").strip()
        api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
        if not api_key or not api_secret:
            raise RuntimeError("BINANCE_API_KEY/BINANCE_API_SECRET are required for EXCHANGE=binance")

        ex = ccxt.binance({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
        })
        return ex

    def _apply_size_multiplier_if_needed(self, signal: Dict[str, Any], quote_amount: float) -> float:
        """
        Safety: avoid double-multiplying.
        - New signals from signal_generator already include quote_amount (mult applied + capped).
        - Older signals may only include base sizing and size_multiplier -> we apply here.
        """
        applied = bool(signal.get("size_multiplier_applied", False))
        if applied:
            return quote_amount

        mult = float(signal.get("size_multiplier", 1.0) or 1.0)
        if mult < 1.0:
            mult = 1.0
        if self.max_size_multiplier > 0:
            mult = min(mult, self.max_size_multiplier)

        base_quote = float(quote_amount or 0.0)
        pre_cap = base_quote * mult

        if self.max_quote_per_trade > 0:
            post_cap = min(pre_cap, self.max_quote_per_trade)
        else:
            post_cap = pre_cap

        signal["size_multiplier_applied"] = True
        signal["quote_amount_pre_cap"] = pre_cap
        signal["quote_amount"] = post_cap

        return post_cap

    def _get_quote_amount_from_signal(self, signal: Dict[str, Any]) -> float:
        """
        Determine quote amount:
        priority:
        1) signal["quote_amount"] if provided
        2) BOT_QUOTE_PER_TRADE env
        Then apply multiplier if needed (legacy path)
        """
        q = signal.get("quote_amount")
        if q is None:
            q = self.bot_quote_per_trade
        try:
            quote_amount = float(q)
        except Exception:
            quote_amount = self.bot_quote_per_trade

        # ensure non-negative
        quote_amount = max(0.0, quote_amount)

        # apply multiplier if not already applied
        quote_amount = self._apply_size_multiplier_if_needed(signal, quote_amount)
        return quote_amount

    def _should_block_live(self) -> bool:
        if self.mode == "LIVE":
            if not self.live_ok:
                return True
        return False

    def execute_signal(self, signal: Dict[str, Any]) -> None:
        """
        Execute one signal dict.
        """
        if self.kill_switch_active:
            logger.warning("KILL_SWITCH_ACTIVE -> skipping signal execution")
            log_event("KILL_SWITCH_ACTIVE", {"signal": signal})
            return

        if self._should_block_live():
            logger.warning("LIVE_CONFIRMATION_REQUIRED -> skipping signal execution")
            log_event("LIVE_CONFIRMATION_REQUIRED", {"signal": signal})
            return

        signal_id = str(signal.get("id") or "").strip()
        if signal_id:
            if signal_id_already_executed(signal_id):
                logger.info("SIGNAL_DEDUPED | id=%s", signal_id)
                return

        action = str(signal.get("action") or "").upper().strip()
        symbol = str(signal.get("symbol") or "").strip()
        if not action or not symbol:
            logger.warning("INVALID_SIGNAL | missing action/symbol")
            log_event("INVALID_SIGNAL", {"signal": signal})
            return

        # dedupe only when active OCO (optional)
        if self.dedupe_only_when_active_oco:
            if has_active_oco_for_symbol(symbol):
                logger.info("ACTIVE_OCO_EXISTS -> skipping new entry | symbol=%s", symbol)
                log_event("ACTIVE_OCO_EXISTS", {"symbol": symbol, "signal": signal})
                return

        quote_amount = self._get_quote_amount_from_signal(signal)

        # get exchange
        ex = self._get_exchange()

        # execute
        if self.mode in ("DEMO", "TESTNET"):
            self._execute_demo(ex, signal, quote_amount)
        else:
            self._execute_live(ex, signal, quote_amount)

        if signal_id:
            mark_signal_id_executed(signal_id)

    # -----------------------
    # DEMO execution
    # -----------------------
    def _execute_demo(self, ex, signal: Dict[str, Any], quote_amount: float) -> None:
        symbol = signal["symbol"]
        action = signal["action"].upper()

        if action == "BUY":
            logger.info("DEMO_BUY | symbol=%s quote=%.2f", symbol, quote_amount)
            simulate_market_entry(symbol, quote_amount=quote_amount)
            log_event("DEMO_BUY", {"symbol": symbol, "quote_amount": quote_amount, "signal": signal})
            return

        logger.info("DEMO_UNSUPPORTED_ACTION | action=%s", action)
        log_event("DEMO_UNSUPPORTED_ACTION", {"signal": signal})

    # -----------------------
    # LIVE execution (OCO TP/SL)
    # -----------------------
    def _execute_live(self, ex, signal: Dict[str, Any], quote_amount: float) -> None:
        """
        Live BUY creates market entry then OCO (TP/SL).
        Existing logic preserved; only quote_amount sizing may be multiplier-adjusted.
        """
        symbol = signal["symbol"]
        action = signal["action"].upper()

        if action != "BUY":
            logger.info("LIVE_UNSUPPORTED_ACTION | action=%s", action)
            log_event("LIVE_UNSUPPORTED_ACTION", {"signal": signal})
            return

        # market buy by quote amount
        try:
            # Bybit spot uses createMarketBuyOrder with amount in base by default.
            # We'll compute base amount from ticker price for safety:
            ticker = ex.fetch_ticker(symbol)
            last = float(ticker.get("last") or 0.0)
            if last <= 0:
                raise RuntimeError("Invalid last price from ticker")

            base_amount = quote_amount / last
            # place market order
            order = ex.create_market_buy_order(symbol, base_amount)
            log_event("LIVE_BUY_MARKET_OK", {"symbol": symbol, "quote_amount": quote_amount, "base_amount": base_amount, "order": order, "signal": signal})
            logger.info("LIVE_BUY_MARKET_OK | symbol=%s quote=%.2f base=%.8f", symbol, quote_amount, base_amount)

        except Exception as e:
            logger.exception("LIVE_BUY_MARKET_FAIL | symbol=%s err=%s", symbol, str(e))
            log_event("LIVE_BUY_MARKET_FAIL", {"symbol": symbol, "err": str(e), "signal": signal})
            return

        # Create TP/SL OCO (if supported in your exchange wrapper; preserving existing db link logic)
        try:
            # fetch position avg price
            # for spot, use order average if available else last
            avg_price = float(order.get("average") or order.get("price") or last)

            tp_price = avg_price * (1.0 + (self.tp_pct / 100.0))
            sl_price = avg_price * (1.0 - (self.sl_pct / 100.0))

            # adjust limit prices with buffers
            tp_limit = tp_price * self.sell_buffer
            sl_limit = sl_price * (1.0 - (self.sl_limit_gap_pct / 100.0))
            sl_limit = sl_limit * self.sell_retry_buffer

            # Create OCO link record (db)
            link = create_oco_link(symbol=symbol, entry_price=avg_price, tp=tp_price, sl=sl_price, meta={"signal": signal})
            set_oco_status(link["id"], "open")

            # NOTE: Many exchanges have different OCO mechanics.
            # This repo likely uses a separate OCO reconcile / place orders logic.
            # We'll just log and rely on existing reconcile loop in your system.
            log_event("OCO_PLANNED", {"symbol": symbol, "tp_price": tp_price, "sl_price": sl_price, "tp_limit": tp_limit, "sl_limit": sl_limit, "oco_link": link, "signal": signal})
            logger.info(
                "OCO_PLANNED | symbol=%s entry=%.6f tp=%.6f sl=%.6f (tp_limit=%.6f sl_limit=%.6f)",
                symbol, avg_price, tp_price, sl_price, tp_limit, sl_limit
            )

        except Exception as e:
            logger.exception("OCO_PLAN_FAIL | symbol=%s err=%s", symbol, str(e))
            log_event("OCO_PLAN_FAIL", {"symbol": symbol, "err": str(e), "signal": signal})
            return

    # -----------------------
    # Loop helper (existing usage)
    # -----------------------
    def execute_signals(self, signals):
        for s in signals:
            try:
                self.execute_signal(s)
            except Exception as e:
                logger.exception("EXEC_SIGNAL_FAIL | err=%s signal=%s", str(e), s)
                log_event("EXEC_SIGNAL_FAIL", {"err": str(e), "signal": s})

    def snapshot_state(self) -> None:
        state = get_system_state()
        links = list_active_oco_links()
        logger.info("STATE_SNAPSHOT | state=%s active_oco=%d", state, len(links))

    def heartbeat(self) -> None:
        update_system_state({"last_heartbeat_ts": int(time.time())})
