from __future__ import annotations

import json
import os
import time
import logging
from dataclasses import asdict
from typing import Any, Dict, Optional, Tuple

from execution.config import SETTINGS
from execution.excel_live_core import ExcelLiveCore, LiveDecisionRow

logger = logging.getLogger("gbm")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _to_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if s == "":
            return default
        try:
            return float(s)
        except Exception:
            return default
    return default


def _safe_symbol(symbol: str) -> str:
    return (symbol or "").strip()


def _safe_tf(tf: str) -> str:
    return (tf or "").strip()


def _write_outbox(path: str, payload: Dict[str, Any]) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class SignalGenerator:
    """
    Generates trading signals and writes them into SIGNAL_OUTBOX_PATH in JSON format.

    It is intentionally conservative:
    - If Excel says STAND_BY -> no trade.
    - If Excel says EXECUTE -> produce BUY signal with size_multiplier.
    """

    def __init__(self):
        self.outbox_path = os.getenv("SIGNAL_OUTBOX_PATH", "/var/data/signal_outbox.json")
        self.excel_enabled = True  # default true
        self.excel_path = os.getenv("EXCEL_MODEL_PATH", "").strip()
        self.gen_test_signal = os.getenv("GEN_TEST_SIGNAL", "false").lower() == "true"

    def _compute_quote_amount(self, base_quote: float, size_mult: float) -> Tuple[float, float, float]:
        """
        Applies size multiplier and caps with MAX_QUOTE_PER_TRADE.
        Returns (base_quote, pre_cap, post_cap)
        """
        base_quote = float(max(0.0, base_quote))
        size_mult = float(max(1.0, min(size_mult, 10.0)))

        pre_cap = base_quote * size_mult
        max_quote = float(getattr(SETTINGS, "MAX_QUOTE_PER_TRADE", 0.0) or 0.0)
        if max_quote > 0:
            post_cap = min(pre_cap, max_quote)
        else:
            post_cap = pre_cap

        # avoid tiny orders
        post_cap = float(max(0.0, post_cap))
        return base_quote, pre_cap, post_cap

    def _make_buy_signal_from_excel(self, row: LiveDecisionRow) -> Optional[Dict[str, Any]]:
        symbol = _safe_symbol(row.symbol)
        tf = _safe_tf(row.timeframe) or SETTINGS.BOT_TIMEFRAME

        if not symbol:
            logger.warning("EXCEL_SIGNAL_SKIP | reason=empty_symbol")
            return None

        if row.final_trade_decision != "EXECUTE":
            logger.info(
                "EXCEL_DECISION | decision=%s ai_score=%.6f gate=%.6f vol_ratio=%.4f vol_regime=%s -> NO_SIGNAL",
                row.final_trade_decision,
                row.ai_score,
                row.adaptive_buy_gate,
                row.volatility_ratio,
                row.volatility_regime,
            )
            return None

        # Excel-driven multiplier (default safe)
        size_mult = float(max(1.0, row.adaptive_size_mult or 1.0))
        base_quote = float(getattr(SETTINGS, "BOT_QUOTE_PER_TRADE", 0.0) or 0.0)
        base_quote, pre_cap, quote_amount = self._compute_quote_amount(base_quote, size_mult)

        signal: Dict[str, Any] = {
            "id": f"EXCEL-{symbol.replace('/','')}-{_now_ms()}",
            "source": "excel_live_core",
            "ts_ms": _now_ms(),
            "symbol": symbol,
            "timeframe": tf,
            "action": "BUY",
            # We treat ai_score as confidence proxy for now.
            "confidence": float(_clamp(row.ai_score, 0.0, 1.0)),
            # Minimal Safe integration: Excel controls aggression
            "size_multiplier": float(size_mult),
            "size_multiplier_applied": True,
            # Quote sizing details (transparent)
            "quote_amount": float(quote_amount),
            "quote_amount_base": float(base_quote),
            "quote_amount_pre_cap": float(pre_cap),
            # Excel transparency fields
            "adaptive_buy_gate": float(row.adaptive_buy_gate),
            "adaptive_size_mult": float(row.adaptive_size_mult),
            "volatility_ratio": float(row.volatility_ratio),
            "volatility_regime": str(row.volatility_regime),
        }

        logger.info(
            "EXCEL_SIGNAL_BUY | symbol=%s tf=%s ai_score=%.6f gate=%.6f vol_ratio=%.4f mult=%.2f quote=%.2f (base=%.2f pre_cap=%.2f)",
            symbol,
            tf,
            row.ai_score,
            row.adaptive_buy_gate,
            row.volatility_ratio,
            size_mult,
            quote_amount,
            base_quote,
            pre_cap,
        )
        return signal

    def run_once(self) -> None:
        """
        Main entry: produce outbox json with 'signals': []
        """
        signals = []

        if self.gen_test_signal:
            # keep it safe; only used for testing wiring.
            symbol = (SETTINGS.BOT_SYMBOLS.split(",")[0] if SETTINGS.BOT_SYMBOLS else "BTC/USDT:USDT").strip()
            base_quote = float(getattr(SETTINGS, "BOT_QUOTE_PER_TRADE", 15.0) or 15.0)
            signal = {
                "id": f"TEST-{symbol.replace('/','')}-{_now_ms()}",
                "source": "test",
                "ts_ms": _now_ms(),
                "symbol": symbol,
                "timeframe": SETTINGS.BOT_TIMEFRAME,
                "action": "BUY",
                "confidence": 0.99,
                "size_multiplier": 1.0,
                "size_multiplier_applied": True,
                "quote_amount": base_quote,
                "quote_amount_base": base_quote,
                "quote_amount_pre_cap": base_quote,
            }
            signals.append(signal)
            _write_outbox(self.outbox_path, {"signals": signals})
            logger.info("TEST_SIGNAL_WRITTEN | path=%s", self.outbox_path)
            return

        # Excel mode (preferred)
        if self.excel_enabled and self.excel_path:
            try:
                core = ExcelLiveCore(self.excel_path)
                row = core.read_live_decision()
                sig = self._make_buy_signal_from_excel(row)
                if sig:
                    # optional: restrict to whitelist
                    if SETTINGS.SYMBOL_WHITELIST:
                        wl = [s.strip() for s in SETTINGS.SYMBOL_WHITELIST.split(",") if s.strip()]
                        if sig["symbol"] not in wl:
                            logger.info("EXCEL_SIGNAL_SKIP | reason=not_in_whitelist symbol=%s", sig["symbol"])
                        else:
                            signals.append(sig)
                    else:
                        signals.append(sig)

                _write_outbox(self.outbox_path, {"signals": signals})
                logger.info("OUTBOX_WRITTEN | path=%s signals=%d", self.outbox_path, len(signals))
                return
            except Exception as e:
                logger.exception("EXCEL_SIGNAL_ERROR | err=%s -> fallback_no_signal", str(e))

        # Fallback: no signal (minimal safe)
        _write_outbox(self.outbox_path, {"signals": []})
        logger.info("OUTBOX_WRITTEN | path=%s signals=0", self.outbox_path)
