# execution/execution_engine.py
from __future__ import annotations

import logging
import os
from typing import Any, Dict

from execution.exchange_client import build_exchange_client
from execution.db.repository import (
    signal_id_already_executed,
    mark_signal_id_executed,
    create_oco_link,
    list_active_oco_links,
    set_oco_status,
    has_active_oco_for_symbol,
    open_trade,
    close_trade,
)

logger = logging.getLogger("gbm")


class ExecutionEngine:
    """
    Worker API:
      - execute_signal(sig: dict)
      - reconcile_oco()
    """

    def __init__(self):
        self.mode = os.getenv("MODE", "DEMO").upper()
        self.exchange = build_exchange_client()

    def execute_signal(self, sig: Dict[str, Any]) -> None:
        signal_id = str(sig.get("signal_id") or sig.get("id") or "")
        symbol = str(sig.get("symbol") or "")
        verdict = str(sig.get("final_verdict") or sig.get("verdict") or "").upper()

        if not signal_id or not symbol:
            logger.warning(f"SIGNAL_INVALID | signal_id={signal_id} symbol={symbol}")
            return

        if signal_id_already_executed(signal_id, action="EXECUTE"):
            logger.info(f"DEDUPED | signal_id={signal_id} action=EXECUTE")
            return

        if has_active_oco_for_symbol(symbol):
            logger.info(f"SKIP | active OCO exists | symbol={symbol}")
            return

        if verdict not in ("BUY", "SELL"):
            logger.info(f"SKIP | verdict={verdict} | signal_id={signal_id}")
            mark_signal_id_executed(
                signal_id,
                signal_hash=str(sig.get("signal_hash") or ""),
                action="SKIP",
                symbol=symbol,
            )
            return

        if verdict == "SELL":
            logger.info(f"SKIP_SELL_SIGNAL | signal_id={signal_id} (not implemented)")
            mark_signal_id_executed(
                signal_id,
                signal_hash=str(sig.get("signal_hash") or ""),
                action="SKIP_SELL",
                symbol=symbol,
            )
            return

        quote_amount = float(sig.get("quote_amount") or sig.get("quote_in") or 0.0)
        if quote_amount <= 0:
            quote_amount = float(os.getenv("BOT_QUOTE_PER_TRADE", "7"))

        logger.info(f"EXECUTE_BUY | signal_id={signal_id} symbol={symbol} quote={quote_amount}")
        buy = self.exchange.create_market_buy(symbol, quote_amount)

        entry_price = None
        try:
            entry_price = float(buy.raw.get("average") or buy.raw.get("price") or 0.0) or None
        except Exception:
            entry_price = None

        if entry_price is None:
            try:
                t = self.exchange.fetch_ticker(symbol)
                entry_price = float(t.get("last") or 0.0) or 0.0
            except Exception:
                entry_price = 0.0

        qty = None
        try:
            qty = float(buy.raw.get("filled") or buy.raw.get("amount") or 0.0) or None
        except Exception:
            qty = None

        if qty is None or qty <= 0:
            qty = (quote_amount / entry_price) if entry_price and entry_price > 0 else 0.0

        open_trade(
            signal_id=signal_id,
            symbol=symbol,
            qty=float(qty),
            quote_in=float(quote_amount),
            entry_price=float(entry_price or 0.0),
        )

        tp_price = sig.get("tp_price")
        sl_stop_price = sig.get("sl_stop_price")
        sl_limit_price = sig.get("sl_limit_price")

        if tp_price is None or sl_stop_price is None:
            logger.warning(f"OCO_SKIP | missing tp/sl prices | signal_id={signal_id}")
            mark_signal_id_executed(
                signal_id,
                signal_hash=str(sig.get("signal_hash") or ""),
                action="EXECUTE",
                symbol=symbol,
            )
            return

        tp_price = float(tp_price)
        sl_stop_price = float(sl_stop_price)
        sl_limit_price = float(sl_limit_price) if sl_limit_price is not None else float(sl_stop_price)

        logger.info(
            f"OCO_CREATE | signal_id={signal_id} symbol={symbol} qty={qty} tp={tp_price} sl_stop={sl_stop_price} sl_limit={sl_limit_price}"
        )

        tp = self.exchange.create_limit_sell(symbol, float(qty), float(tp_price))
        sl = self.exchange.create_stop_limit_sell(symbol, float(qty), float(sl_stop_price), float(sl_limit_price))

        create_oco_link(
            signal_id=signal_id,
            symbol=symbol,
            base_asset=None,
            tp_order_id=str(tp.order_id),
            sl_order_id=str(sl.order_id),
            tp_price=float(tp_price),
            sl_stop_price=float(sl_stop_price),
            sl_limit_price=float(sl_limit_price),
            amount=float(qty),
        )

        mark_signal_id_executed(
            signal_id,
            signal_hash=str(sig.get("signal_hash") or ""),
            action="EXECUTE",
            symbol=symbol,
        )
        logger.info(f"EXECUTE_DONE | signal_id={signal_id} symbol={symbol}")

    def reconcile_oco(self) -> None:
        links = list_active_oco_links(limit=50)
        if not links:
            return

        for row in links:
            try:
                (
                    link_db_id,
                    signal_id,
                    symbol,
                    base_asset,
                    tp_order_id,
                    sl_order_id,
                    tp_price,
                    sl_stop_price,
                    sl_limit_price,
                    amount,
                    status,
                    created_at_utc,
                    updated_at_utc,
                ) = row
            except Exception:
                logger.warning(f"OCO_ROW_UNPACK_FAIL | row={row}")
                continue

            tp = None
            sl = None

            try:
                tp = self.exchange.fetch_order(str(tp_order_id), str(symbol))
            except Exception as e:
                logger.debug(f"OCO_TP_FETCH_FAIL | id={tp_order_id} symbol={symbol} err={e}")

            try:
                sl = self.exchange.fetch_order(str(sl_order_id), str(symbol))
            except Exception as e:
                logger.debug(f"OCO_SL_FETCH_FAIL | id={sl_order_id} symbol={symbol} err={e}")

            tp_status = (tp or {}).get("status")
            sl_status = (sl or {}).get("status")

            if tp_status == "closed":
                logger.info(f"OCO_HIT_TP | signal_id={signal_id} symbol={symbol} tp_order={tp_order_id}")
                try:
                    if sl_status == "open":
                        self.exchange.cancel_order(str(sl_order_id), str(symbol))
                except Exception:
                    pass

                exit_price = float((tp or {}).get("average") or (tp or {}).get("price") or tp_price or 0.0)
                close_trade(signal_id=str(signal_id), exit_price=exit_price, outcome="TP", pnl_quote=0.0, pnl_pct=0.0)
                set_oco_status(int(link_db_id), "closed")
                continue

            if sl_status == "closed":
                logger.info(f"OCO_HIT_SL | signal_id={signal_id} symbol={symbol} sl_order={sl_order_id}")
                try:
                    if tp_status == "open":
                        self.exchange.cancel_order(str(tp_order_id), str(symbol))
                except Exception:
                    pass

                exit_price = float((sl or {}).get("average") or (sl or {}).get("price") or sl_limit_price or 0.0)
                close_trade(signal_id=str(signal_id), exit_price=exit_price, outcome="SL", pnl_quote=0.0, pnl_pct=0.0)
                set_oco_status(int(link_db_id), "closed")
                continue


# compatibility
execution_engine = ExecutionEngine
__all__ = ["ExecutionEngine", "execution_engine"]
