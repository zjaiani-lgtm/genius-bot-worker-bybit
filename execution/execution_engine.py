import os
import time
import logging
from typing import Any, Dict, Optional, Tuple

import ccxt

from execution.db.repository import (
    get_system_state,
    log_event,
    list_active_oco_links,
    create_oco_link,
    set_oco_status,
    update_system_state,
    signal_id_already_executed,
    mark_signal_id_executed,
    has_active_oco_for_symbol,

    # ✅ NEW (performance tracking)
    open_trade,
    get_trade,
    close_trade,
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


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


class ExecutionEngine:
    def __init__(self):
        self.mode = os.getenv("MODE", "DEMO").upper()
        self.env_kill_switch = os.getenv("KILL_SWITCH", "false").lower() == "true"
        self.live_confirmation = os.getenv("LIVE_CONFIRMATION", "false").lower() == "true"

        # public price feed (no keys)
        self.price_feed = ccxt.binance({"enableRateLimit": True})

        self.exchange = None
        if self.mode in ("LIVE", "TESTNET"):
            from execution.exchange_client import BinanceSpotClient
            self.exchange = BinanceSpotClient()

        self.state_debug = os.getenv("STATE_DEBUG", "false").lower() == "true"

        # OCO params
        self.tp_pct = float(os.getenv("TP_PCT", "1.30"))
        self.sl_pct = float(os.getenv("SL_PCT", "0.70"))
        self.sl_limit_gap_pct = float(os.getenv("SL_LIMIT_GAP_PCT", "0.15"))

        self.sell_buffer = float(os.getenv("SELL_BUFFER", "0.999"))
        self.sell_retry_buffer = float(os.getenv("SELL_RETRY_BUFFER", "0.998"))

        # ---- New protective gates (Execution side) ----
        self.max_spread_pct = float(os.getenv("MAX_SPREAD_PCT", "0.12"))  # 0.12% default

        self.estimated_roundtrip_fee_pct = float(os.getenv("ESTIMATED_ROUNDTRIP_FEE_PCT", "0.20"))
        self.estimated_slippage_pct = float(os.getenv("ESTIMATED_SLIPPAGE_PCT", "0.15"))
        self.min_net_profit_pct = float(os.getenv("MIN_NET_PROFIT_PCT", "0.60"))

        # Entry mode: MARKET (default) or MAKER_LIMIT (optional if supported)
        self.entry_mode = os.getenv("ENTRY_MODE", "MARKET").strip().upper()  # MARKET | MAKER_LIMIT
        self.limit_entry_offset_pct = float(os.getenv("LIMIT_ENTRY_OFFSET_PCT", "0.02"))  # 0.02% below best bid
        self.limit_entry_timeout_sec = int(os.getenv("LIMIT_ENTRY_TIMEOUT_SEC", "6"))  # cancel if not filled fast

    # ----------------------------
    # System state
    # ----------------------------
    def _load_system_state(self) -> Dict[str, Any]:
        raw = get_system_state()
        if self.state_debug:
            logger.info(f"SYSTEM_STATE_RAW | type={type(raw)} value={raw}")

        if isinstance(raw, (list, tuple)):
            status = raw[1] if len(raw) > 1 else ""
            sync = raw[2] if len(raw) > 2 else 0
            kill = raw[3] if len(raw) > 3 else 0
            return {
                "status": str(status or "").upper(),
                "startup_sync_ok": _to_bool01(sync),
                "kill_switch": _to_bool01(kill),
            }

        if isinstance(raw, dict):
            return {
                "status": str(raw.get("status") or "").upper(),
                "startup_sync_ok": _to_bool01(raw.get("startup_sync_ok")),
                "kill_switch": _to_bool01(raw.get("kill_switch")),
            }

        return {"status": "", "startup_sync_ok": False, "kill_switch": False}

    # ----------------------------
    # Market micro-helpers
    # ----------------------------
    def _get_spread_pct(self, symbol: str) -> Optional[float]:
        """
        Spread% = (ask - bid) / mid * 100
        Uses public orderbook.
        """
        try:
            ob = self.price_feed.fetch_order_book(symbol, limit=5)
            bids = ob.get("bids") or []
            asks = ob.get("asks") or []
            if not bids or not asks:
                return None
            bid = float(bids[0][0])
            ask = float(asks[0][0])
            mid = (bid + ask) / 2.0 if (bid + ask) else 0.0
            if mid <= 0:
                return None
            return ((ask - bid) / mid) * 100.0
        except Exception as e:
            logger.warning(f"SPREAD_FETCH_FAIL | symbol={symbol} err={e}")
            return None

    def _net_edge_ok(self) -> Tuple[bool, str]:
        """
        Execution-side edge gate: TP must cover costs and leave MIN_NET_PROFIT_PCT.
        """
        cost = self.estimated_roundtrip_fee_pct + self.estimated_slippage_pct
        net = self.tp_pct - cost
        if net < self.min_net_profit_pct:
            return False, f"EDGE_TOO_SMALL tp={self.tp_pct:.2f} cost={cost:.2f} net={net:.2f} < min_net={self.min_net_profit_pct:.2f}"
        return True, "OK"

    # ----------------------------
    # PnL helpers (NEW)
    # ----------------------------
    @staticmethod
    def _exit_price_from_order(o: Dict[str, Any], fallback: float = 0.0) -> float:
        try:
            v = float(o.get("average") or o.get("price") or 0.0)
            return v if v > 0 else float(fallback or 0.0)
        except Exception:
            return float(fallback or 0.0)

    @staticmethod
    def _calc_pnl(quote_in: float, entry: float, exitp: float, qty: float) -> Tuple[float, float]:
        """
        Approx realized PnL:
          pnl_quote = (exit - entry) * qty
          pnl_pct   = pnl_quote / quote_in * 100
        (fees not included here; you can enhance later using order fee fields)
        """
        pnl_quote = (float(exitp) - float(entry)) * float(qty)
        pnl_pct = (pnl_quote / float(quote_in) * 100.0) if float(quote_in) else 0.0
        return float(pnl_quote), float(pnl_pct)

    # ----------------------------
    # OCO reconcile (UPDATED: closes trades)
    # ----------------------------
    def reconcile_oco(self) -> None:
        if self.mode not in ("LIVE", "TESTNET"):
            return
        if self.exchange is None:
            return

        rows = list_active_oco_links(limit=50)
        if not rows:
            return

        CLOSED = {"closed", "filled"}
        CANCELED = {"canceled", "cancelled", "expired", "rejected"}

        for r in rows:
            (
                link_id, signal_id, symbol, base_asset,
                tp_order_id, sl_order_id,
                tp_price, sl_stop_price, sl_limit_price,
                amount, status, created_at, updated_at
            ) = r

            if not tp_order_id or not sl_order_id:
                logger.warning(f"OCO_RECONCILE_SKIP | link={link_id} missing order ids tp='{tp_order_id}' sl='{sl_order_id}'")
                continue

            try:
                tp = self.exchange.fetch_order(tp_order_id, symbol)
                sl = self.exchange.fetch_order(sl_order_id, symbol)

                tp_status = _norm(tp.get("status"))
                sl_status = _norm(sl.get("status"))

                logger.info(
                    f"OCO_RECONCILE | link={link_id} id={signal_id} symbol={symbol} "
                    f"tp={tp_order_id}:{tp_status} sl={sl_order_id}:{sl_status}"
                )

                # ---- SL filled ----
                if sl_status in CLOSED:
                    set_oco_status(link_id, "CLOSED_SL")

                    tr = get_trade(signal_id)
                    exitp = self._exit_price_from_order(sl, fallback=float(sl_stop_price))

                    if tr:
                        # (signal_id, symbol, qty, quote_in, entry_price, opened_at, exit_price, closed_at, outcome, pnl_quote, pnl_pct)
                        _, _, qty, quote_in, entry_price, *_ = tr
                        pnl_quote, pnl_pct = self._calc_pnl(float(quote_in), float(entry_price), float(exitp), float(qty))
                        close_trade(signal_id, exit_price=float(exitp), outcome="SL", pnl_quote=float(pnl_quote), pnl_pct=float(pnl_pct))
                        log_event("TRADE_CLOSED", f"{signal_id} {symbol} SL exit={exitp} pnl_quote={pnl_quote:.4f} pnl_pct={pnl_pct:.3f}")
                    else:
                        log_event("TRADE_CLOSE_WARN", f"{signal_id} {symbol} SL filled but trade row missing")

                    log_event("OCO_CLOSED", f"{signal_id} SL_FILLED sl={sl_order_id} tp={tp_order_id} tp_status={tp_status}")
                    continue

                # ---- TP filled ----
                if tp_status in CLOSED:
                    set_oco_status(link_id, "CLOSED_TP")

                    tr = get_trade(signal_id)
                    exitp = self._exit_price_from_order(tp, fallback=float(tp_price))

                    if tr:
                        _, _, qty, quote_in, entry_price, *_ = tr
                        pnl_quote, pnl_pct = self._calc_pnl(float(quote_in), float(entry_price), float(exitp), float(qty))
                        close_trade(signal_id, exit_price=float(exitp), outcome="TP", pnl_quote=float(pnl_quote), pnl_pct=float(pnl_pct))
                        log_event("TRADE_CLOSED", f"{signal_id} {symbol} TP exit={exitp} pnl_quote={pnl_quote:.4f} pnl_pct={pnl_pct:.3f}")
                    else:
                        log_event("TRADE_CLOSE_WARN", f"{signal_id} {symbol} TP filled but trade row missing")

                    log_event("OCO_CLOSED", f"{signal_id} TP_FILLED tp={tp_order_id} sl={sl_order_id} sl_status={sl_status}")
                    continue

                # One canceled but the other open: ignore (exchange might be processing)
                if (tp_status in CANCELED and sl_status == "open") or (sl_status in CANCELED and tp_status == "open"):
                    continue

                # Both canceled -> failed
                if tp_status in CANCELED and sl_status in CANCELED:
                    set_oco_status(link_id, "FAILED")
                    log_event("OCO_FAILED", f"{signal_id} tp={tp_order_id}:{tp_status} sl={sl_order_id}:{sl_status}")
                    continue

            except Exception as e:
                logger.warning(f"OCO_RECONCILE_FAIL | link={link_id} symbol={symbol} err={e}")

    # ----------------------------
    # SELL (early exit) execution
    # ----------------------------
    def _execute_sell(self, signal_id: str, symbol: str, signal_hash: str = None) -> None:
        logger.info(f"SELL_ENTER | id={signal_id} symbol={symbol} MODE={self.mode}")

        if self.mode == "DEMO":
            log_event("SELL_DEMO", f"{signal_id} DEMO SELL {symbol}")
            mark_signal_id_executed(signal_id, signal_hash=signal_hash, action="SELL_DEMO", symbol=str(symbol))
            return

        if self.exchange is None:
            log_event("SELL_BLOCKED_NO_EXCHANGE", f"{signal_id} {symbol}")
            logger.warning(f"SELL_BLOCKED | exchange client not wired | id={signal_id} symbol={symbol}")
            return

        if is_kill_switch_active():
            logger.error(f"KILL_SWITCH_ACTIVE_LAST_GATE | SELL_BLOCKED | id={signal_id} symbol={symbol}")
            log_event("SELL_BLOCKED_KILL_SWITCH_LAST_GATE", f"{signal_id} {symbol}")
            return

        # cancel OCO legs best-effort
        rows = list_active_oco_links(limit=50)
        rows = [r for r in rows if str(r[2] or "").upper() == str(symbol).upper()]
        CLOSED = {"closed", "filled"}

        for r in rows:
            link_id, oco_signal_id, sym, base_asset, tp_order_id, sl_order_id, *_rest = r
            try:
                tp = self.exchange.fetch_order(tp_order_id, symbol)
                sl = self.exchange.fetch_order(sl_order_id, symbol)
                tp_status = _norm(tp.get("status"))
                sl_status = _norm(sl.get("status"))

                if tp_status in CLOSED:
                    set_oco_status(link_id, "CLOSED_TP")
                    log_event("SELL_SKIP", f"{signal_id} {symbol} already closed by TP (link={link_id})")
                    continue
                if sl_status in CLOSED:
                    set_oco_status(link_id, "CLOSED_SL")
                    log_event("SELL_SKIP", f"{signal_id} {symbol} already closed by SL (link={link_id})")
                    continue

                for oid in (tp_order_id, sl_order_id):
                    if not oid:
                        continue
                    try:
                        self.exchange.cancel_order(str(oid), symbol)
                    except Exception as e:
                        logger.warning(f"SELL_CANCEL_WARN | id={signal_id} symbol={symbol} order_id={oid} err={e}")

                set_oco_status(link_id, "CANCELED_BY_SIGNAL")
                log_event("OCO_CANCELED", f"{signal_id} {symbol} link={link_id} canceled_by_signal")

            except Exception as e:
                logger.warning(f"SELL_OCO_LOOKUP_FAIL | id={signal_id} symbol={symbol} link={link_id} err={e}")

        base_asset = symbol.split("/")[0].upper()
        free_base = float(self.exchange.fetch_balance_free(base_asset))
        sell_amount = self.exchange.floor_amount(symbol, free_base * self.sell_buffer)
        if sell_amount <= 0:
            sell_amount = self.exchange.floor_amount(symbol, free_base * self.sell_retry_buffer)

        if sell_amount <= 0:
            msg = f"SELL_SKIP_NO_FREE_BASE | id={signal_id} symbol={symbol} free_{base_asset}={free_base}"
            logger.warning(msg)
            log_event("SELL_SKIP_NO_FREE_BASE", msg)
            mark_signal_id_executed(signal_id, signal_hash=signal_hash, action="SELL_NO_FREE_BASE", symbol=str(symbol))
            return

        try:
            sell = self.exchange.place_market_sell(symbol=symbol, base_amount=sell_amount)
            avg = float(sell.get("average") or sell.get("price") or 0.0) or self.exchange.fetch_last_price(symbol)
            logger.info(f"SELL_LIVE_OK | id={signal_id} symbol={symbol} amount={sell_amount} avg={avg} order_id={sell.get('id')}")
            log_event("SELL_LIVE_OK", f"{signal_id} {symbol} amount={sell_amount} avg={avg} order_id={sell.get('id')}")
            mark_signal_id_executed(signal_id, signal_hash=signal_hash, action="SELL_LIVE", symbol=str(symbol))
        except Exception as e:
            logger.exception(f"SELL_LIVE_ERROR | id={signal_id} symbol={symbol} err={e}")
            log_event("SELL_LIVE_ERROR", f"{signal_id} {symbol} err={e}")
            return

    # ----------------------------
    # Entry execution helpers
    # ----------------------------
    def _place_entry_buy(self, symbol: str, quote_amount: float) -> Tuple[Dict[str, Any], float]:
        """
        Returns (buy_order, buy_avg_price)

        Supports:
        - MAKER_LIMIT if exchange client supports it (optional)
        - otherwise MARKET by quote
        """
        if self.exchange is None:
            raise RuntimeError("exchange client not wired")

        # spread gate (especially important if we are about to MARKET buy)
        sp = self._get_spread_pct(symbol)
        if sp is not None and sp > self.max_spread_pct:
            raise RuntimeError(f"SPREAD_TOO_WIDE spread%={sp:.4f} > MAX_SPREAD_PCT={self.max_spread_pct:.4f}")

        if self.entry_mode == "MAKER_LIMIT":
            # Optional: only if your BinanceSpotClient has these methods
            if hasattr(self.exchange, "place_limit_buy_postonly") and hasattr(self.exchange, "fetch_order") and hasattr(self.exchange, "cancel_order"):
                ob = self.price_feed.fetch_order_book(symbol, limit=5)
                bid = float((ob.get("bids") or [[0, 0]])[0][0])
                if bid <= 0:
                    raise RuntimeError("NO_BID_FOR_LIMIT_ENTRY")

                # price a bit below bid to avoid taker
                limit_price = bid * (1.0 - (self.limit_entry_offset_pct / 100.0))
                limit_price = self.exchange.floor_price(symbol, limit_price)

                base_amount = quote_amount / limit_price
                base_amount = self.exchange.floor_amount(symbol, base_amount)
                if base_amount <= 0:
                    raise RuntimeError("LIMIT_ENTRY_SIZE_ZERO")

                buy = self.exchange.place_limit_buy_postonly(symbol=symbol, base_amount=base_amount, price=limit_price)
                oid = str(buy.get("id") or "")

                t0 = time.time()
                filled = False
                last_avg = 0.0

                while time.time() - t0 < float(self.limit_entry_timeout_sec):
                    o = self.exchange.fetch_order(oid, symbol)
                    st = _norm(o.get("status"))
                    last_avg = float(o.get("average") or o.get("price") or 0.0) or last_avg
                    if st in ("closed", "filled"):
                        filled = True
                        buy = o
                        break
                    time.sleep(0.35)

                if not filled:
                    try:
                        self.exchange.cancel_order(oid, symbol)
                    except Exception:
                        pass
                    raise RuntimeError(f"LIMIT_ENTRY_TIMEOUT timeout={self.limit_entry_timeout_sec}s")

                buy_avg = float(buy.get("average") or buy.get("price") or 0.0) or last_avg or self.exchange.fetch_last_price(symbol)
                return buy, buy_avg

            # fallback
            logger.warning("MAKER_LIMIT requested but exchange client lacks place_limit_buy_postonly; fallback to MARKET")

        # MARKET
        buy = self.exchange.place_market_buy_by_quote(symbol=symbol, quote_amount=quote_amount)
        buy_avg = float(buy.get("average") or buy.get("price") or 0.0) or self.exchange.fetch_last_price(symbol)
        return buy, buy_avg

    # ----------------------------
    # Main execution
    # ----------------------------
    def execute_signal(self, signal: Dict[str, Any]) -> None:
        signal_id = str(signal.get("signal_id", "UNKNOWN"))
        verdict = str(signal.get("final_verdict", "")).upper()

        logger.info(f"EXEC_ENTER | id={signal_id} verdict={verdict} MODE={self.mode} ENV_KILL_SWITCH={self.env_kill_switch}")

        # idempotency
        try:
            if signal_id_already_executed(signal_id):
                logger.warning(f"EXEC_DEDUPED | duplicate ignored | id={signal_id}")
                log_event("EXEC_DEDUPED", f"id={signal_id}")
                return
        except Exception as e:
            logger.error(f"EXEC_BLOCKED | idempotency_check_failed | id={signal_id} err={e}")
            log_event("EXEC_BLOCKED_IDEMPOTENCY_FAIL", f"{signal_id} err={e}")
            return

        state = self._load_system_state()
        db_status = str(state.get("status") or "").upper()
        db_kill = bool(state.get("kill_switch"))
        sync_ok = bool(state.get("startup_sync_ok"))

        if self.env_kill_switch or db_kill:
            logger.warning(f"EXEC_BLOCKED | KILL_SWITCH=ON | id={signal_id}")
            log_event("EXEC_BLOCKED_KILL_SWITCH", f"{signal_id}")
            return

        if not sync_ok or db_status not in ("ACTIVE", "RUNNING"):
            logger.warning(f"EXEC_BLOCKED | system not ACTIVE/synced | id={signal_id} status={db_status} sync_ok={sync_ok}")
            log_event("EXEC_BLOCKED_SYSTEM_STATE", f"{signal_id} status={db_status} sync_ok={sync_ok}")
            return

        if self.mode == "LIVE" and not self.live_confirmation:
            logger.warning(f"EXEC_BLOCKED | LIVE_CONFIRMATION=OFF | id={signal_id}")
            log_event("EXEC_BLOCKED_LIVE_CONFIRMATION", f"{signal_id}")
            return

        if signal.get("certified_signal") is not True:
            log_event("REJECT_NOT_CERTIFIED", f"{signal_id}")
            return

        execution = signal.get("execution") or {}
        symbol = execution.get("symbol")
        direction = str(execution.get("direction", "")).upper()
        entry = execution.get("entry") or {}
        entry_type = str(entry.get("type", "")).upper()

        position_size = execution.get("position_size")
        quote_amount = execution.get("quote_amount")

        # SELL
        if verdict == "SELL":
            if not symbol or direction != "LONG":
                logger.warning(f"EXEC_REJECT | bad SELL payload | id={signal_id} symbol={symbol} dir={direction}")
                log_event("REJECT_BAD_SELL_PAYLOAD", f"{signal_id} symbol={symbol} dir={direction}")
                return

            signal_hash = signal.get("_fingerprint") or signal.get("signal_hash")
            self._execute_sell(signal_id=signal_id, symbol=str(symbol), signal_hash=signal_hash)
            return

        # only LONG market entry signals are accepted (compat)
        if not symbol or direction != "LONG" or entry_type != "MARKET":
            logger.warning(f"EXEC_REJECT | bad payload | id={signal_id} symbol={symbol} dir={direction} entry={entry_type}")
            log_event("REJECT_BAD_PAYLOAD", f"{signal_id}")
            return

        signal_hash = signal.get("_fingerprint") or signal.get("signal_hash")

        # DEMO
        if self.mode == "DEMO":
            last_price = float(self.price_feed.fetch_ticker(symbol)["last"])
            base_size = float(position_size) if position_size is not None else float(quote_amount) / float(last_price)
            resp = simulate_market_entry(symbol=symbol, side=direction, size=base_size, price=last_price)

            log_event("TRADE_EXECUTED", f"{signal_id} DEMO {symbol} size={base_size} price={last_price}")
            logger.info(f"EXEC_DEMO_OK | id={signal_id} resp={resp}")

            mark_signal_id_executed(signal_id, signal_hash=signal_hash, action="TRADE_DEMO", symbol=str(symbol))
            return

        # LIVE/TESTNET
        if self.exchange is None:
            log_event("EXEC_BLOCKED_NO_EXCHANGE", f"{signal_id}")
            logger.warning(f"EXEC_BLOCKED | exchange client not wired | id={signal_id}")
            return

        # import here
        from execution.exchange_client import LiveTradingBlocked

        try:
            # ---- execution-side edge gate ----
            ok_edge, edge_reason = self._net_edge_ok()
            if not ok_edge:
                msg = f"EXEC_REJECT | EDGE_GATE | id={signal_id} symbol={symbol} {edge_reason}"
                logger.warning(msg)
                log_event("EXEC_REJECT_EDGE_GATE", msg)
                mark_signal_id_executed(signal_id, signal_hash=signal_hash, action="REJECT_EDGE_GATE", symbol=str(symbol))
                return

            # size
            if quote_amount is None:
                last = self.exchange.fetch_last_price(symbol)
                quote_amount = float(position_size) * float(last)
            quote_amount = float(quote_amount)

            # ---- race-condition guard: never buy if active OCO already exists ----
            try:
                if has_active_oco_for_symbol(str(symbol)):
                    msg = f"EXEC_REJECT | ACTIVE_OCO_RACE | id={signal_id} symbol={symbol}"
                    logger.warning(msg)
                    log_event("EXEC_REJECT_ACTIVE_OCO_RACE", msg)
                    mark_signal_id_executed(signal_id, signal_hash=signal_hash, action="REJECT_ACTIVE_OCO_RACE", symbol=str(symbol))
                    return
            except Exception as e:
                msg = f"EXEC_BLOCKED | ACTIVE_OCO_CHECK_FAIL | id={signal_id} symbol={symbol} err={e}"
                logger.warning(msg)
                log_event("EXEC_BLOCKED_ACTIVE_OCO_CHECK_FAIL", msg)
                return

            # MIN_NOTIONAL
            min_notional = 0.0
            try:
                min_notional = float(self.exchange.get_min_notional(symbol))
            except Exception:
                min_notional = 0.0

            if min_notional > 0 and quote_amount < min_notional:
                msg = (
                    f"EXEC_REJECT | MIN_NOTIONAL | id={signal_id} symbol={symbol} "
                    f"quote={quote_amount:.8f} < min_notional={min_notional}"
                )
                logger.warning(msg)
                log_event("EXEC_REJECT_MIN_NOTIONAL", msg)
                mark_signal_id_executed(signal_id, signal_hash=signal_hash, action="REJECT_MIN_NOTIONAL", symbol=str(symbol))
                return

            if is_kill_switch_active():
                logger.error(f"KILL_SWITCH_ACTIVE_LAST_GATE | BUY_BLOCKED | id={signal_id}")
                log_event("EXEC_BLOCKED_KILL_SWITCH_LAST_GATE", f"{signal_id} BUY_BLOCKED")
                return

            # BUY (MARKET or optional MAKER_LIMIT)
            buy, buy_avg = self._place_entry_buy(symbol=str(symbol), quote_amount=quote_amount)

            logger.info(f"EXEC_LIVE_BUY_OK | id={signal_id} symbol={symbol} quote={quote_amount} avg={buy_avg} order_id={buy.get('id')}")
            log_event("TRADE_EXECUTED", f"{signal_id} LIVE BUY {symbol} quote={quote_amount} avg={buy_avg} order_id={buy.get('id')}")

            mark_signal_id_executed(signal_id, signal_hash=signal_hash, action="TRADE_LIVE_BUY", symbol=str(symbol))

            # base amount for OCO
            base_asset = symbol.split("/")[0].upper()
            free_base = float(self.exchange.fetch_balance_free(base_asset))

            sell_amount = self.exchange.floor_amount(symbol, free_base * self.sell_buffer)
            if sell_amount <= 0:
                sell_amount = self.exchange.floor_amount(symbol, free_base * self.sell_retry_buffer)

            if sell_amount <= 0:
                msg = f"OCO_SKIP_NO_FREE_BASE | id={signal_id} free_{base_asset}={free_base}"
                logger.warning(msg)
                log_event("OCO_SKIP_NO_FREE_BASE", msg)
                return

            tp_price = self.exchange.floor_price(symbol, buy_avg * (1.0 + (self.tp_pct / 100.0)))
            sl_stop_price = self.exchange.floor_price(symbol, buy_avg * (1.0 - (self.sl_pct / 100.0)))
            sl_limit_price = self.exchange.floor_price(symbol, sl_stop_price * (1.0 - (self.sl_limit_gap_pct / 100.0)))

            logger.info(
                f"OCO_PREP | id={signal_id} free_{base_asset}={free_base} sell_amount={sell_amount} "
                f"tp={tp_price} sl_stop={sl_stop_price} sl_limit={sl_limit_price}"
            )

            if is_kill_switch_active():
                logger.error(f"KILL_SWITCH_ACTIVE_LAST_GATE | OCO_BLOCKED | id={signal_id}")
                log_event("EXEC_BLOCKED_KILL_SWITCH_LAST_GATE", f"{signal_id} OCO_BLOCKED")
                return

            # OCO place
            oco = self.exchange.place_oco_sell(
                symbol=symbol,
                base_amount=sell_amount,
                tp_price=tp_price,
                sl_stop_price=sl_stop_price,
                sl_limit_price=sl_limit_price,
            )

            raw = oco.get("raw") or {}
            order_reports = raw.get("orderReports") or []

            tp_order_id = None
            sl_order_id = None

            def _otype(rep):
                return str(rep.get("type") or rep.get("orderType") or "").upper()

            for rep in order_reports:
                oid = rep.get("orderId") or rep.get("order_id")
                if not oid:
                    continue
                oid = str(oid)
                typ = _otype(rep)
                if "STOP" in typ:
                    sl_order_id = sl_order_id or oid
                else:
                    tp_order_id = tp_order_id or oid

            orders = raw.get("orders") or []
            if (not tp_order_id or not sl_order_id) and len(orders) >= 2:
                ids = []
                for o in orders:
                    oid = o.get("orderId") or o.get("order_id")
                    if oid:
                        ids.append(str(oid))
                uniq = []
                for x in ids:
                    if x not in uniq:
                        uniq.append(x)
                if len(uniq) >= 2:
                    tp_order_id = tp_order_id or uniq[0]
                    sl_order_id = sl_order_id or uniq[1]

            list_order_id = raw.get("listOrderId") or raw.get("orderListId") or raw.get("list_order_id")

            logger.info(f"OCO_OK | id={signal_id} listOrderId={list_order_id} tp={tp_order_id} sl={sl_order_id}")
            log_event("OCO_ARMED", f"{signal_id} symbol={symbol} listOrderId={list_order_id} tp={tp_order_id} sl={sl_order_id} amount={sell_amount}")

            if (not list_order_id) or (not tp_order_id) or (not sl_order_id) or (str(tp_order_id) == str(sl_order_id)):
                msg = (
                    f"OCO_INVALID | id={signal_id} symbol={symbol} "
                    f"listOrderId={list_order_id} tp={tp_order_id} sl={sl_order_id} -> PROTECTION_FAILED"
                )
                logger.error(msg)
                log_event("OCO_INVALID", msg)

                update_system_state(kill_switch=1)
                logger.error("FAILSAFE | DB kill_switch=1 set due to OCO_INVALID")
                log_event("FAILSAFE_KILL_SWITCH_SET", f"{signal_id} OCO_INVALID")
                return

            create_oco_link(
                signal_id=signal_id,
                symbol=symbol,
                base_asset=base_asset,
                tp_order_id=str(tp_order_id or ""),
                sl_order_id=str(sl_order_id or ""),
                tp_price=float(tp_price),
                sl_stop_price=float(sl_stop_price),
                sl_limit_price=float(sl_limit_price),
                amount=float(sell_amount),
            )

            # ✅ NEW: record trade entry (for ROI/Winrate later)
            try:
                open_trade(
                    signal_id=signal_id,
                    symbol=str(symbol),
                    qty=float(sell_amount),
                    quote_in=float(quote_amount),
                    entry_price=float(buy_avg),
                )
                log_event("TRADE_RECORDED", f"{signal_id} {symbol} entry={buy_avg} qty={sell_amount} quote_in={quote_amount}")
            except Exception as e:
                logger.warning(f"TRADE_RECORD_FAIL | id={signal_id} symbol={symbol} err={e}")

            log_event("TRADE_LIVE_ARMED", f"{signal_id} {symbol} OCO_ARMED listOrderId={list_order_id}")

        except LiveTradingBlocked as e:
            msg = f"EXEC_REJECT | LIVE_BLOCKED | id={signal_id} reason={e}"
            logger.warning(msg)
            log_event("EXEC_REJECT_LIVE_BLOCKED", msg)
            mark_signal_id_executed(signal_id, signal_hash=signal_hash, action="REJECT_LIVE_BLOCKED", symbol=str(symbol))
            return

        except Exception as e:
            logger.exception(f"EXEC_LIVE_ERROR | id={signal_id} err={e}")
            log_event("EXEC_LIVE_ERROR", f"{signal_id} err={e}")
            return
