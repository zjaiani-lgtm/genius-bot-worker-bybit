# execution/signal_generator.py (HEDGE-GRADE HARDENED — FIXED)

import os
import time
import uuid
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List

import ccxt
import openpyxl

from execution.signal_client import append_signal
from execution.db.repository import has_active_oco_for_symbol
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

EXCHANGE_FEED = ccxt.binance({"enableRateLimit": True})

_CORE: Optional[ExcelLiveCore] = None

# ===== HEDGE CACHE =====
_EXCEL_WB = None
_EXCEL_HEADERS = None
_EXCEL_CONF_CACHE: Optional[float] = None
_EXCEL_CONF_TS: float = 0.0
EXCEL_CONF_REFRESH_SEC = float(os.getenv("EXCEL_CONF_REFRESH_SEC", "30"))


# ===================== HELPERS =====================

def _now_utc_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _cooldown_ok() -> bool:
    global _last_emit_ts
    if _last_emit_ts <= 0:
        return True
    return (time.time() - _last_emit_ts) >= COOLDOWN_SECONDS


def _mark_emitted():
    global _last_emit_ts
    _last_emit_ts = time.time()


# ===================== CORE =====================

def _core() -> ExcelLiveCore:
    global _CORE
    if _CORE is None:
        if not EXCEL_MODEL_PATH:
            raise RuntimeError("EXCEL_MODEL_PATH is empty")
        logger.info(f"[GEN] EXCEL_CORE_LOADED | path={EXCEL_MODEL_PATH}")
        _CORE = ExcelLiveCore(workbook_path=EXCEL_MODEL_PATH)
    return _CORE


# ===================== MARKET SNAPSHOT =====================

def _fetch_snapshot(symbol: str) -> Dict[str, Any]:
    candles = EXCHANGE_FEED.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)
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

    trend_strength = 0.0
    if ma20 > 0:
        trend_strength = _clamp(((last - ma20) / ma20) * 10.0, 0.0, 1.0)

    structure_ok = (last > ma20) and (last > prev) and (ma20 >= ma50)

    volume_score = 0.5
    if v20 > 0:
        volume_score = _clamp(vlast / v20, 0.0, 1.5) / 1.5

    # ===== EXCEL CONF =====
    raw_conf = 0.5
    try:
        raw_conf = _core().read_confidence()
    except Exception:
        pass

    confidence_score = _clamp(raw_conf * 1.08, 0.0, 1.0)

    # ===== VOL REGIME =====
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

    return {
        "symbol": symbol,
        "trend_strength": trend_strength,
        "structure_ok": structure_ok,
        "volume_score": volume_score,
        "confidence_score": confidence_score,
        "volatility_regime": volatility_regime,
        "risk_state": "OK",
        "last": last,
        "ma20": ma20,
    }


# ===================== SIGNAL GENERATION =====================

def generate_signals():
    symbols = os.getenv("BOT_SYMBOLS", "BTCUSDT,ETHUSDT").split(",")
    symbols = [s.strip() for s in symbols if s.strip()]

    for symbol in symbols:
        try:
            if not _cooldown_ok():
                continue

            snap = _fetch_snapshot(symbol)

            core = _core()
            decision = core.evaluate(
                CoreInputs(
                    trend_strength=snap["trend_strength"],
                    structure_ok=snap["structure_ok"],
                    volume_score=snap["volume_score"],
                    confidence_score=snap["confidence_score"],
                    volatility_regime=snap["volatility_regime"],
                    risk_state=snap["risk_state"],
                )
            )

            logger.info(
                f"[GEN] CORE_DECISION | symbol={symbol} ai={decision.ai_score:.3f} "
                f"macro={decision.macro_gate} strat={decision.active_strategy} "
                f"final={decision.final_decision} risk={decision.risk_state} "
                f"volReg={snap['volatility_regime']} last={snap['last']:.2f}"
            )

            if not ALLOW_LIVE_SIGNALS:
                continue

            if BLOCK_SIGNALS_WHEN_ACTIVE_OCO and has_active_oco_for_symbol(symbol):
                continue

            if decision.final_decision != "EXECUTE":
                continue

            signal = {
                "id": str(uuid.uuid4()),
                "timestamp": _now_utc_iso(),
                "symbol": symbol,
                "direction": "LONG",
                "confidence": decision.ai_score,
                "quote_amount": BOT_QUOTE_PER_TRADE,
                "risk_state": decision.risk_state,
                "volatility_regime": snap["volatility_regime"],
            }

            append_signal(signal)
            _mark_emitted()

        except Exception as e:
            logger.error(f"[GEN] {symbol} ERROR={e}", exc_info=True)


# ===================== MAIN =====================

def main():
    logger.info("[GEN] Signal generator starting")
    while True:
        generate_signals()
        time.sleep(int(os.getenv("BOT_SIGNAL_LOOP_SLEEP_SECONDS", "30")))


if __name__ == "__main__":
    main()
