# execution/signal_generator.py
import os
import time
import uuid
import logging
from datetime import datetime
from typing import Optional, Dict, Any, Tuple, List

import ccxt

from execution.signal_client import append_signal
from execution.db.repository import has_active_oco_for_symbol
from execution.excel_live_core import ExcelLiveCore, CoreInputs

logger = logging.getLogger("gbm")

TIMEFRAME = os.getenv("BOT_TIMEFRAME", "15m")
CANDLE_LIMIT = int(os.getenv("BOT_CANDLE_LIMIT", "80"))
COOLDOWN_SECONDS = int(os.getenv("BOT_SIGNAL_COOLDOWN_SECONDS", "180"))

ALLOW_LIVE_SIGNALS = os.getenv("ALLOW_LIVE_SIGNALS", "false").strip().lower() == "true"

# USDT per trade (prevents NOTIONAL issues when you size in quote)
BOT_QUOTE_PER_TRADE = float(os.getenv("BOT_QUOTE_PER_TRADE", "15"))

# ---- Risk-first gate: block generating new signals if symbol has an active OCO ----
BLOCK_SIGNALS_WHEN_ACTIVE_OCO = os.getenv("BLOCK_SIGNALS_WHEN_ACTIVE_OCO", "true").strip().lower() == "true"

# Debug logs from generator
GEN_DEBUG = os.getenv("GEN_DEBUG", "true").strip().lower() == "true"

# Symbols universe
SYMBOLS_RAW = os.getenv("BOT_SYMBOLS", "BTC/USDT:USDT,ETH/USDT:USDT").strip()
SYMBOLS = [s.strip() for s in SYMBOLS_RAW.split(",") if s.strip()]

# ---- Micro-scalp extra guards ----

# Minimum absolute distance of price from MA20 (in %) to avoid "chop" entries.
MA_GAP_PCT = float(os.getenv("MA_GAP_PCT", "0.15"))

# If your core confidence is below this, we skip (extra guard on top of Excel).
BUY_CONFIDENCE_MIN = float(os.getenv("BUY_CONFIDENCE_MIN", "0.70"))

# Expected round-trip cost model (VERY important for micro-scalps)
ESTIMATED_ROUNDTRIP_FEE_PCT = float(os.getenv("ESTIMATED_ROUNDTRIP_FEE_PCT", "0.20"))

# Spread + slippage safety buffer
ESTIMATED_SLIPPAGE_PCT = float(os.getenv("ESTIMATED_SLIPPAGE_PCT", "0.15"))

# Exchange client (ccxt)
EXCHANGE_ID = os.getenv("EXCHANGE_ID", "bybit").strip().lower()

# Output file (outbox)
DEFAULT_OUTBOX = "/var/data/signal_outbox.json"

_last_emit_ts = 0.0
_core_singleton: Optional[ExcelLiveCore] = None


def _now_utc_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _pct(a: float, b: float) -> float:
    try:
        if b == 0:
            return 0.0
        return (float(a) - float(b)) / float(b) * 100.0
    except Exception:
        return 0.0


def _cooldown_ok() -> bool:
    global _last_emit_ts
    if COOLDOWN_SECONDS <= 0:
        return True
    return (time.time() - _last_emit_ts) >= COOLDOWN_SECONDS


def _core() -> ExcelLiveCore:
    global _core_singleton
    if _core_singleton is None:
        from execution.config import get_excel_model_path
        _core_singleton = ExcelLiveCore(model_path=get_excel_model_path())
    return _core_singleton


def _exchange() -> ccxt.Exchange:
    cls = getattr(ccxt, EXCHANGE_ID)
    ex = cls({"enableRateLimit": True})
    ex.options["defaultType"] = "swap"
    ex.options["defaultSubType"] = "linear"
    return ex


EXCHANGE = _exchange()


def _has_active_oco(symbol: str) -> bool:
    try:
        return bool(has_active_oco_for_symbol(symbol))
    except Exception:
        return False


def _edge_ok(atr_percent: float) -> Tuple[bool, str]:
    """
    Fee-aware edge gate:
    - Need ATR% big enough to plausibly cover (fees + slippage) + some profit buffer.
    """
    cost = float(ESTIMATED_ROUNDTRIP_FEE_PCT) + float(ESTIMATED_SLIPPAGE_PCT)
    # Require ATR% >= cost * 1.2 (small edge buffer)
    need = cost * 1.2
    if float(atr_percent) < float(need):
        return False, f"atr%={atr_percent:.3f} < need={need:.3f} (fee+slip={cost:.3f})"
    return True, "OK"


def _emit(signal: Dict[str, Any], outbox_path: str) -> None:
    global _last_emit_ts
    append_signal(signal, outbox_path)
    _last_emit_ts = time.time()


def _get_outbox_path() -> str:
    # supports both env names
    return (
        os.getenv("OUTBOX_PATH")
        or os.getenv("SIGNAL_OUTBOX_PATH")
        or DEFAULT_OUTBOX
    )


def generate_signal() -> Optional[Dict[str, Any]]:
    """
    Excel Live Core based generator:
    - If no active OCO: emits TRADE only when final_trade_decision == EXECUTE.
    - If active OCO: can emit SELL if risk_state == KILL (protective override).
    """
    outbox_path = _get_outbox_path()

    if not _cooldown_ok():
        return None

    core = _core()

    for symbol in SYMBOLS:
        active_oco = _has_active_oco(symbol)

        try:
            t0 = time.time()
            ohlcv = EXCHANGE.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)
            dt_ms = int((time.time() - t0) * 1000)
            if GEN_DEBUG:
                logger.info(f"[GEN] FETCH_OK | symbol={symbol} tf={TIMEFRAME} candles={len(ohlcv) if ohlcv else 0} dt={dt_ms}ms")
        except Exception as e:
