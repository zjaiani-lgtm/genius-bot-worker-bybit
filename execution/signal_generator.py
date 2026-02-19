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

BOT_QUOTE_PER_TRADE = float(os.getenv("BOT_QUOTE_PER_TRADE", "15"))

MIN_MOVE_PCT = float(os.getenv("MIN_MOVE_PCT", "0.60"))
MA_GAP_PCT = float(os.getenv("MA_GAP_PCT", "0.15"))
BUY_CONFIDENCE_MIN = float(os.getenv("BUY_CONFIDENCE_MIN", "0.52"))
ESTIMATED_ROUNDTRIP_FEE_PCT = float(os.getenv("ESTIMATED_ROUNDTRIP_FEE_PCT", "0.20"))
ESTIMATED_SLIPPAGE_PCT = float(os.getenv("ESTIMATED_SLIPPAGE_PCT", "0.15"))
TP_PCT = float(os.getenv("TP_PCT", "1.3"))
MIN_NET_PROFIT_PCT = float(os.getenv("MIN_NET_PROFIT_PCT", "0.60"))

BLOCK_SIGNALS_WHEN_ACTIVE_OCO = os.getenv("BLOCK_SIGNALS_WHEN_ACTIVE_OCO", "true").strip().lower() == "true"
GEN_DEBUG = os.getenv("GEN_DEBUG", "true").strip().lower() == "true"
GEN_LOG_EVERY_TICK = os.getenv("GEN_LOG_EVERY_TICK", "true").strip().lower() == "true"

EXCEL_MODEL_PATH = os.getenv(
    "EXCEL_MODEL_PATH",
    "/var/data/DYZEN_CAPITAL_OS_AI_LIVE_CORE_READY.xlsx",
).strip()

if EXCEL_MODEL_PATH.lower().startswith("excel_model_path="):
    EXCEL_MODEL_PATH = EXCEL_MODEL_PATH.split("=", 1)[1].strip()

_last_emit_ts: float = 0.0

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()

EXCHANGE = ccxt.binance({
    "enableRateLimit": True,
    "apiKey": BINANCE_API_KEY,
    "secret": BINANCE_API_SECRET,
})

_CORE: Optional[ExcelLiveCore] = None


def _now_utc_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _parse_symbols() -> List[str]:
    raw = os.getenv("BOT_SYMBOLS", "").strip()
    if not raw:
        raw = os.getenv("SYMBOL_WHITELIST", "").strip()
    if not raw:
        raw = os.getenv("BOT_SYMBOL", "BTC/USDT").strip()

    syms = []
    for s in raw.split(","):
        s = s.strip()
        if s:
            syms.append(s.upper())
    return syms


SYMBOLS = _parse_symbols()


def _has_active_oco(symbol: str) -> bool:
    try:
        return has_active_oco_for_symbol(symbol)
    except Exception as e:
        logger.warning(
            f"[GEN] ACTIVE_OCO_CHECK_FAIL | symbol={symbol} err={e} -> assume active_oco=True"
        )
        return True


def _resolve_excel_path(env_path: str) -> str:
    candidates = [
        env_path,
        "/var/data/DYZEN_CAPITAL_OS_AI_LIVE_CORE_READY.xlsx",
        "/opt/render/project/src/assets/DYZEN_CAPITAL_OS_AI_LIVE_CORE_READY.xlsx",
    ]

    for p in candidates:
        if p and os.path.exists(p):
            return p

    raise FileNotFoundError(f"EXCEL_MODEL_NOT_FOUND | env={env_path}")


def _core() -> ExcelLiveCore:
    global _CORE
    if _CORE is None:
        resolved = _resolve_excel_path(EXCEL_MODEL_PATH)
        logger.info(f"[GEN] EXCEL_CORE_LOADED | path={resolved}")
        _CORE = ExcelLiveCore(resolved)
    return _CORE


def _pct(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return (a - b) / b * 100.0


def _sma(vals: List[float], n: int) -> float:
    if len(vals) < n:
        return sum(vals) / max(1, len(vals))
    return sum(vals[-n:]) / n


def _atr_pct(ohlcv: List[List[float]], n: int = 14) -> float:
    if len(ohlcv) < n + 1:
        return 0.0
    trs = []
    for i in range(-n, 0):
        high = float(ohlcv[i][2])
        low = float(ohlcv[i][3])
        prev_close = float(ohlcv[i - 1][4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    atr = sum(trs) / n
    last_close = float(ohlcv[-1][4])
    return (atr / last_close) * 100.0 if last_close else 0.0


def _vol_regime(atr_pct: float) -> str:
    if atr_pct >= 2.0:
        return "EXTREME"
    if atr_pct <= 0.30:
        return "LOW"
    return "NORMAL"


# ======================
# FIXED EDGE FUNCTION 🔥
# ======================
def _edge_ok(atr_pct: float) -> Tuple[bool, str]:
    """Fee-aware edge gate (clean + parser-safe)."""

    # Gate 1 — minimum movement
    ATR_TOLERANCE = 0.85
    if atr_pct < MIN_MOVE_PCT * ATR_TOLERANCE:
        return False, (
            f"ATR_TOO_LOW atr%={atr_pct:.2f} "
            f"< MIN_MOVE_PCT={MIN_MOVE_PCT:.2f}"
        )

    # Cost model
    assumed_gross_edge = TP_PCT
    assumed_cost = ESTIMATED_ROUNDTRIP_FEE_PCT + ESTIMATED_SLIPPAGE_PCT
    assumed_net = assumed_gross_edge - assumed_cost

    logger.info(
        f"[EDGE_CHECK] atr={atr_pct:.3f} min_move={MIN_MOVE_PCT:.3f} "
        f"net={assumed_net:.3f} min_net={MIN_NET_PROFIT_PCT:.3f}"
    )

    # Gate 2 — net edge
    EDGE_TOLERANCE = 0.85
    if assumed_net < MIN_NET_PROFIT_PCT * EDGE_TOLERANCE:
        return False, (
            "EDGE_TOO_SMALL "
            f"TP_PCT={assumed_gross_edge:.2f} "
            f"cost={assumed_cost:.2f} "
            f"net={assumed_net:.2f} "
            f"< MIN_NET_PROFIT_PCT={MIN_NET_PROFIT_PCT:.2f}"
        )

    # Gate 3 — ATR sanity vs TP
    ATR_SANITY_TOLERANCE = 0.85
    if atr_pct < (assumed_gross_edge * 0.75 * ATR_SANITY_TOLERANCE):
        return False, (
            f"ATR_BELOW_TP atr%={atr_pct:.2f} "
            f"< 0.75*TP_PCT={assumed_gross_edge * 0.75:.2f}"
        )

    return True, "OK"
# ======================


def _trend_strength(last: float, ma20: float) -> float:
    gap_pct = _pct(last, ma20)
    x = (gap_pct / 1.0)
    return max(0.0, min(1.0, 0.5 + (x * 0.4)))


def _structure_ok(closes: List[float]) -> bool:
    if len(closes) < 10:
        return False
    last = closes[-1]
    ma20 = _sma(closes, 20)
    prev = closes[-2]
    last5 = closes[-5:]
    last10 = closes[-10:]
    return (last > ma20) and (last > prev) and (sum(last5) / 5.0 > sum(last10) / 10.0)


def _volume_score(vols: List[float]) -> float:
    if len(vols) < 20:
        return 0.0
    v_last = vols[-1]
    v_avg = sum(vols[-20:]) / 20.0
    if v_avg <= 0:
        return 0.0
    ratio = v_last / v_avg
    return max(0.0, min(1.0, ratio / 2.0))


def _confidence_score(closes: List[float], ohlcv: List[List[float]]) -> float:
    last = closes[-1]
    prev = closes[-2]
    ma20 = _sma(closes, 20)
    atrp = _atr_pct(ohlcv, 14)

    cond1 = 1.0 if last > ma20 else 0.0
    cond2 = 1.0 if last > prev else 0.0
    cond3 = 1.0 if atrp < 2.0 else 0.0

    return (0.45 * cond1) + (0.35 * cond2) + (0.20 * cond3)


def _risk_state(vol_regime: str, ai_score: float) -> str:
    if vol_regime == "EXTREME":
        return "KILL"

    AI_RISK_TOLERANCE = 0.95
    if ai_score < (0.45 * AI_RISK_TOLERANCE):
        return "REDUCE"

    return "OK"
