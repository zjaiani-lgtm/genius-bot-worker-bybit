# execution/signal_generator.py
import os
import time
import uuid
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List

import ccxt

from execution.signal_client import append_signal
from execution.db.repository import has_active_oco_for_symbol, log_event
from execution.excel_live_core import ExcelLiveCore, CoreInputs

logger = logging.getLogger("gbm")

TIMEFRAME = os.getenv("BOT_TIMEFRAME", "15m")
CANDLE_LIMIT = int(os.getenv("BOT_CANDLE_LIMIT", "120"))
COOLDOWN_SECONDS = int(os.getenv("BOT_SIGNAL_COOLDOWN_SECONDS", "180"))

ALLOW_LIVE_SIGNALS = os.getenv("ALLOW_LIVE_SIGNALS", "false").strip().lower() == "true"
BOT_QUOTE_PER_TRADE = float(os.getenv("BOT_QUOTE_PER_TRADE", "15"))

BLOCK_SIGNALS_WHEN_ACTIVE_OCO = os.getenv("BLOCK_SIGNALS_WHEN_ACTIVE_OCO", "true").strip().lower() == "true"

EXCEL_MODEL_PATH = os.getenv("EXCEL_MODEL_PATH", "").strip()
if EXCEL_MODEL_PATH.lower().startswith("excel_model_path="):
    EXCEL_MODEL_PATH = EXCEL_MODEL_PATH.split("=", 1)[1].strip()

_last_emit_ts: float = 0.0

# Public market data feed (Binance is fine for candles)
EXCHANGE_FEED = ccxt.binance({"enableRateLimit": True})

_CORE: Optional[ExcelLiveCore] = None


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name, "true" if default else "false").strip().lower()
    return v in ("1", "true", "yes", "y", "on")


MAX_QUOTE_PER_TRADE = float(os.getenv("MAX_QUOTE_PER_TRADE", "25"))


def _now_utc_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _csv_symbols_from_env(name: str, fallback: str = "") -> List[str]:
    raw = os.getenv(name, fallback).strip()
    if not raw:
        return []
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def _get_symbols(symbols_override: Optional[List[str]] = None) -> List[str]:
    if symbols_override:
        return [str(s).strip().upper() for s in symbols_override if str(s).strip()]
    syms = _csv_symbols_from_env("BOT_SYMBOLS", "")
    if syms:
        return syms
    return _csv_symbols_from_env("SYMBOL_WHITELIST", "")


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
        if not EXCEL_MODEL_PATH:
            raise RuntimeError("EXCEL_MODEL_PATH is empty")
        logger.info(f"EXCEL_CORE | loading workbook={EXCEL_MODEL_PATH}")
        _CORE = ExcelLiveCore(workbook_path=EXCEL_MODEL_PATH)
    return _CORE


def _fetch_snapshot(symbol: str) -> Dict[str, Any]:
    candles = EXCHANGE_FEED.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)
    if not candles or len(candles) < 30:
        raise RuntimeError(f"not enough candles: got={0 if not candles else len(candles)}")

    closes = [float(c[4]) for c in candles]
    vols = [float(c[5]) for c in candles]

    last = closes[-1]
    prev = closes[-2]

    def sma(arr, n):
        if len(arr) < n:
            return sum(arr) / len(arr)
        return sum(arr[-n:]) / n

    ma20 = sma(closes, 20)
    ma50 = sma(closes, 50)

    v20 = sma(vols, 20)
    vlast = vols[-1]

    # trend_strength: how far above MA20 (scaled)
    trend_strength = 0.0
    if ma20 > 0:
        trend_strength = _clamp(((last - ma20) / ma20) * 10.0, 0.0, 1.0)

    # structure_ok: simple structure confirmation
    structure_ok = (last > ma20) and (last > prev) and (ma20 >= ma50)

    # volume_score: last volume vs avg20
    volume_score = 0.5
    if v20 > 0:
        volume_score = _clamp(vlast / v20, 0.0, 1.5) / 1.5

    # volatility_regime: Excel-like mapping using a short/long volatility ratio proxy
    abs_rets = []
    for i in range(1, len(closes)):
        prev_c = closes[i - 1]
        if prev_c > 0:
            abs_rets.append(abs(closes[i] - prev_c) / prev_c)

    short_n = 14
    long_n = 50
    atr_pct = sum(abs_rets[-short_n:]) / min(len(abs_rets), short_n) if abs_rets else 0.0
    atr_ma = sum(abs_rets[-long_n:]) / min(len(abs_rets), long_n) if abs_rets else 0.0
    vol_ratio = (atr_pct / atr_ma) if atr_ma > 0 else 0.0

    if vol_ratio > 1.5:
        volatility_regime = "HIGH"
    elif vol_ratio < 0.7:
        volatility_regime = "LOW"
    else:
        volatility_regime = "NORMAL"

    # confidence_score: deterministic mapping (0..1)
    confidence_score = _clamp(0.50 + (0.50 * trend_strength), 0.0, 1.0)
    if structure_ok:
        confidence_score = _clamp(confidence_score + 0.10, 0.0, 1.0)

    risk_state = "OK"

    return {
        "symbol": symbol,
        "timeframe": TIMEFRAME,
        "last": last,
        "prev": prev,
        "ma20": ma20,
        "ma50": ma50,
        "vlast": vlast,
        "v20": v20,
        "trend_strength": trend_strength,
        "structure_ok": structure_ok,
        "volume_score": volume_score,
        "confidence_score": confidence_score,
        "volatility_regime": volatility_regime,
        "volatility_ratio": vol_ratio,
        "atr_pct": atr_pct,
        "atr_ma": atr_ma,
        "candles": len(candles),
        "risk_state": risk_state,
    }


def _make_signal(symbol: str, snap: Dict[str, Any], excel_out: Dict[str, Any]) -> Dict[str, Any]:
    signal_id = f"GBM-{uuid.uuid4().hex[:12]}"

    final_decision = str(excel_out.get("final_trade_decision") or "").upper()
    verdict = "BUY" if final_decision == "EXECUTE" else "HOLD"

    # --- Adaptive size multiplier (from ExcelLiveCore / patched AI_MASTER_LIVE_DECISION) ---
    base_quote = float(BOT_QUOTE_PER_TRADE)
    mult = float(excel_out.get("adaptive_size_mult") or 1.0)
    quote_amount = base_quote * mult
    # Safety cap
    if MAX_QUOTE_PER_TRADE > 0:
        quote_amount = min(quote_amount, float(MAX_QUOTE_PER_TRADE))

    execution = {
        "exchange": os.getenv("EXCHANGE", "bybit").upper(),
        "symbol": symbol,
        "direction": "LONG",
        "entry": {"type": "MARKET"},
        "quote_amount": float(quote_amount),
        "base_quote_amount": float(base_quote),
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
        "certified_signal": True,
        "size_multiplier": float(mult),
        "size_multiplier_applied": True,
        "execution": execution,
        "meta": meta,
    }


def generate_signal(symbols_override: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    if not ALLOW_LIVE_SIGNALS:
        logger.info("GEN | ALLOW_LIVE_SIGNALS=False -> skip generation")
        return None

    if not _cooldown_ok():
        logger.info("GEN | cooldown active -> skip")
        return None

    symbols = _get_symbols(symbols_override=symbols_override)
    if not symbols:
        logger.warning("GEN | no symbols configured (BOT_SYMBOLS/SYMBOL_WHITELIST empty)")
        return None

    for symbol in symbols:
        try:
            if BLOCK_SIGNALS_WHEN_ACTIVE_OCO and has_active_oco_for_symbol(symbol):
                logger.info(f"GEN | skip symbol={symbol} reason=active_oco")
                continue

            snap = _fetch_snapshot(symbol)

            inp = CoreInputs(
                trend_strength=float(snap["trend_strength"]),
                structure_ok=bool(snap["structure_ok"]),
                volume_score=float(snap["volume_score"]),
                risk_state=str(snap["risk_state"]),
                confidence_score=float(snap["confidence_score"]),
                volatility_regime=str(snap["volatility_regime"]),
                volatility_ratio=float(snap.get("volatility_ratio") or 0.0),
            )

            excel_out = _core().decide(inp)
            final_decision = str(excel_out.get("final_trade_decision") or "").upper()

            logger.info(
                f"GEN | symbol={symbol} last={snap['last']:.6f} ma20={snap['ma20']:.6f} "
                f"trend={snap['trend_strength']:.3f} vol={snap['volume_score']:.3f} "
                f"conf={float(snap.get('confidence_score') or 0.0):.3f} vol_reg={str(snap.get('volatility_regime') or 'NORMAL').upper()} "
                f"decision={final_decision}"
            )

            # Excel must say EXECUTE
            if final_decision != "EXECUTE":
                continue

            sig = _make_signal(symbol, snap, excel_out)
            append_signal(sig, outbox_path=os.getenv("SIGNAL_OUTBOX_PATH", "/var/data/signal_outbox.json"))
            _mark_emitted()
            return sig

        except Exception as e:
            logger.warning(f"GEN | symbol={symbol} err={e}")
            continue

    return None


def run_once(outbox_path: str = None, symbols_override: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    return generate_signal(symbols_override=symbols_override)
