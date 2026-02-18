# execution/signal_generator.py (HEDGE-GRADE HARDENED)

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


# ===================== HEDGE EXCEL READER =====================

def _read_excel_confidence() -> float:
    global _EXCEL_WB, _EXCEL_HEADERS, _EXCEL_CONF_CACHE, _EXCEL_CONF_TS

    now = time.time()

    # ===== CACHE HIT =====
    if _EXCEL_CONF_CACHE is not None and (now - _EXCEL_CONF_TS) < EXCEL_CONF_REFRESH_SEC:
        return _EXCEL_CONF_CACHE

    try:
        if not EXCEL_MODEL_PATH:
            return 0.5

        # ===== OPEN ONCE =====
        if _EXCEL_WB is None:
            _EXCEL_WB = openpyxl.load_workbook(EXCEL_MODEL_PATH, data_only=True)
            ws = _EXCEL_WB["AI_MASTER_LIVE_DECISION"]

            headers = {}
            for c in range(1, ws.max_column + 1):
                v = ws.cell(1, c).value
                if v:
                    headers[str(v).strip().lower()] = c
            _EXCEL_HEADERS = headers

        ws = _EXCEL_WB["AI_MASTER_LIVE_DECISION"]

        # ===== FIND CONF COLUMN =====
        key = None
        for k in _EXCEL_HEADERS.keys():
            if "confidence" in k:
                key = k
                break

        if not key:
            logger.warning("EXCEL_CONF | column not found -> fallback 0.5")
            return 0.5

        col = _EXCEL_HEADERS[key]
        val = ws.cell(2, col).value

        conf = float(val) if val is not None else 0.5
        conf = _clamp(conf, 0.0, 1.0)

        _EXCEL_CONF_CACHE = conf
        _EXCEL_CONF_TS = now
        return conf

    except Exception as e:
        logger.warning(f"EXCEL_CONF | err={e} -> fallback")
        return _EXCEL_CONF_CACHE if _EXCEL_CONF_CACHE is not None else 0.5


# ===================== CORE =====================

def _core() -> ExcelLiveCore:
    global _CORE
    if _CORE is None:
        if not EXCEL_MODEL_PATH:
            raise RuntimeError("EXCEL_MODEL_PATH is empty")
        logger.info(f"EXCEL_CORE | loading workbook={EXCEL_MODEL_PATH}")
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

    # ✅ Excel-driven confidence (hedge-grade)
    confidence_score = _read_excel_confidence()

    # volatility proxy
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
        "volatility_ratio": vol_ratio,
        "risk_state": "OK",
        "last": last,
        "ma20": ma20,
    }
