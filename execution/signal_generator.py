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

BLOCK_SIGNALS_WHEN_ACTIVE_OCO = os.getenv("BLOCK_SIGNALS_WHEN_ACTIVE_OCO", "true").strip().lower() == "true"

GEN_DEBUG = os.getenv("GEN_DEBUG", "true").strip().lower() == "true"
GEN_LOG_EVERY_TICK = os.getenv("GEN_LOG_EVERY_TICK", "true").strip().lower() == "true"

# ---- Excel model path (sanitized) ----
EXCEL_MODEL_PATH = os.getenv("EXCEL_MODEL_PATH", "/var/data/DYZEN_CAPITAL_OS_AI_LIVE_CORE_READY.xlsx").strip()

# sanitize common misconfig like: EXCEL_MODEL_PATH=EXCEL_MODEL_PATH=/opt/render/...xlsx
if EXCEL_MODEL_PATH.lower().startswith("excel_model_path="):
    EXCEL_MODEL_PATH = EXCEL_MODEL_PATH.split("=", 1)[1].strip()

_last_emit_ts: float = 0.0
_last_signature: Optional[Tuple[str, str]] = None  # reserved (if you later want de-dup)

# ---- Exchange (Binance) ----
# For public fetch_ohlcv, keys are not required, but for LIVE execution elsewhere they usually are.
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()

EXCHANGE = ccxt.binance({
    "enableRateLimit": True,
    "apiKey": BINANCE_API_KEY,
    "secret": BINANCE_API_SECRET,
})

# Load Excel core once
_CORE: Optional[ExcelLiveCore] = None


def _now_utc_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _csv_symbols_from_env(name: str, fallback: str = "") -> List[str]:
    raw = os.getenv(name, fallback).strip()
    if not raw:
        return []
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def _get_symbols(symbols_override: Optional[List[str]] = None) -> List[str]:
    """
    Universe list for generator. Can be overridden at runtime by Auto-Scaler.
    """
    if symbols_override:
        return [s.strip().upper() for s in symbols_override if str(s).strip()]

    # fallback to env
    env_syms = _csv_symbols_from_env("BOT_SYMBOLS", "")
    if env_syms:
        return env_syms

    wl = _csv_symbols_from_env("SYMBOL_WHITELIST", "")
    return wl


def _cooldown_ok() -> bool:
    global _last_emit_ts
    if _last_emit_ts <= 0:
        return True
    return (time.time() - _last_emit_ts) >= COOLDOWN_SECONDS


def _mark_emitted():
    global _last_emit_ts
    _last_emit_ts = time.time()


def _core() -> ExcelLiveCore:
    global _CORE
    if _CORE is None:
        logger.info(f"EXCEL_CORE | loading model={EXCEL_MODEL_PATH}")
        _CORE = ExcelLiveCore(model_path=EXCEL_MODEL_PATH)
    return _CORE


def _fetch_snapshot(symbol: str) -> Dict[str, Any]:
    candles = EXCHANGE.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)
    if not candles or len(candles) < 10:
        raise RuntimeError(f"not enough candles: got={0 if not candles else len(candles)}")

    closes = [float(c[4]) for c in candles]
    last = closes[-1]
    prev = closes[-2]
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else sum(closes) / len(closes)

    return {
        "symbol": symbol,
        "timeframe": TIMEFRAME,
        "last": last,
        "prev": prev,
        "ma20": ma20,
        "candles": len(candles),
    }


def _make_signal(symbol: str, snap: Dict[str, Any], excel_out: Dict[str, Any]) -> Dict[str, Any]:
    signal_id = f"GBM-{uuid.uuid4().hex[:12]}"
    verdict = str(excel_out.get("final_verdict") or "").upper()

    # Your execution layer expects these
    execution = {
        "exchange": os.getenv("EXCHANGE", "bybit").upper(),
        "symbol": symbol,
        "quote_per_trade": float(BOT_QUOTE_PER_TRADE),
        "timeframe": TIMEFRAME,
        "mode": os.getenv("MODE", "DEMO").upper(),
    }

    meta = {
        "created_at_utc": _now_utc_iso(),
        "generator": "signal_generator",
        "model": "excel_live_core",
        "inputs": snap,
        "excel_out": excel_out,
    }

    return {
        "signal_id": signal_id,
        "final_verdict": verdict,
        "execution": execution,
        "meta": meta,
    }


def generate_signal(symbols_override: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """
    Generates exactly one signal max per tick, based on Excel decision.
    Will return dict if appended; otherwise None.
    """
    if not ALLOW_LIVE_SIGNALS:
        if GEN_DEBUG:
            logger.info("GEN | ALLOW_LIVE_SIGNALS=False -> skip generation")
        return None

    if not _cooldown_ok():
        if GEN_LOG_EVERY_TICK:
            logger.info("GEN | cooldown active -> skip")
        return None

    symbols = _get_symbols(symbols_override=symbols_override)
    if not symbols:
        logger.warning("GEN | no symbols configured (BOT_SYMBOLS/SYMBOL_WHITELIST empty)")
        return None

    # try symbols in order; first BUY wins
    for symbol in symbols:
        try:
            # optional: block when active OCO exists for symbol
            if BLOCK_SIGNALS_WHEN_ACTIVE_OCO and has_active_oco_for_symbol(symbol):
                if GEN_LOG_EVERY_TICK:
                    logger.info(f"GEN | skip symbol={symbol} reason=active_oco")
                continue

            snap = _fetch_snapshot(symbol)

            # Build Excel inputs (you can extend mapping as needed)
            inputs = CoreInputs(
                symbol=symbol,
                timeframe=TIMEFRAME,
                last=float(snap["last"]),
                prev=float(snap["prev"]),
                ma20=float(snap["ma20"]),
            )
            excel_out = _core().evaluate(inputs)

            verdict = str(excel_out.get("final_verdict") or "").upper()
            if GEN_LOG_EVERY_TICK:
                logger.info(
                    f"GEN | symbol={symbol} last={snap['last']:.6f} prev={snap['prev']:.6f} "
                    f"ma20={snap['ma20']:.6f} verdict={verdict}"
                )

            if verdict != "BUY":
                continue

            sig = _make_signal(symbol, snap, excel_out)
            append_signal(sig, outbox_path=os.getenv("SIGNAL_OUTBOX_PATH", "/var/data/signal_outbox.json"))
            _mark_emitted()
            return sig

        except Exception as e:
            logger.warning(f"GEN | symbol={symbol} err={e}")
            continue

    return None


def run_once(*args, **kwargs) -> Optional[Dict[str, Any]]:
    """
    Backwards-compatible entrypoint expected by bootstrap:
    some versions do: `from execution.signal_generator import run_once`.
    We ignore args/kwargs intentionally.
    """
    # supports: run_once(outbox_path) OR run_once(outbox_path, symbols_override=[...])
    symbols_override = kwargs.get("symbols_override")
    return generate_signal(symbols_override=symbols_override)
