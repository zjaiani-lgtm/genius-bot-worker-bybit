from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .logger import log


@dataclass
class OrderResult:
    ok: bool
    entry_order: Optional[Dict[str, Any]]
    sl_order: Optional[Dict[str, Any]]
    tp_order: Optional[Dict[str, Any]]
    error: Optional[str] = None


class OrderExecutor:
    def __init__(self, ex, logger, dry_run: bool = True):
        self.ex = ex
        self.logger = logger
        self.dry_run = dry_run

    def place_bracket_market(self, symbol: str, side: str, amount: float, sl: float, tp: float) -> OrderResult:
        side = side.upper()
        if side not in ("BUY", "SELL"):
            return OrderResult(False, None, None, None, "invalid_side")

        if self.dry_run:
            log(self.logger, "INFO", "DRY_RUN_ORDER", symbol=symbol, side=side, amount=amount, sl=sl, tp=tp)
            return OrderResult(True, entry_order={"dry_run": True}, sl_order=None, tp_order=None)

        try:
            entry = self.ex.create_order_safe(symbol, "market", side.lower(), amount, None, params={})
            if not entry:
                return OrderResult(False, None, None, None, "entry_failed")

            sl_order = None
            tp_order = None

            # Fallback separate reduce-only orders
            opp = "sell" if side == "BUY" else "buy"

            tp_order = self.ex.create_order_safe(symbol, "limit", opp, amount, tp, params={"reduceOnly": True})
            sl_params = {"reduceOnly": True, "stopPrice": sl}
            sl_order = self.ex.create_order_safe(symbol, "stop_market", opp, amount, None, params=sl_params)

            log(self.logger, "INFO", "BRACKET_PLACED", symbol=symbol, side=side, amount=amount, sl=sl, tp=tp)
            return OrderResult(True, entry, sl_order, tp_order, None)
        except Exception as e:
            log(self.logger, "ERROR", "ORDER_EXEC_FAIL", symbol=symbol, error=str(e))
            return OrderResult(False, None, None, None, str(e))
