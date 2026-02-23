import os
import time
import uuid
import logging
from datetime import datetime
from typing import Optional, Dict, Any, Tuple, List
from threading import Lock

import ccxt

from execution.signal_client import append_signal
from execution.db.repository import has_active_oco_for_symbol
from execution.excel_live_core import ExcelLiveCore, CoreInputs

logger = logging.getLogger("gbm")

# ==============================
# ENV
# ==============================
TIMEFRAME = os.getenv("BOT_TIMEFRAME", "15m").strip()
CANDLE_LIMIT = int(os.getenv("BOT_CANDLE_LIMIT", "80"))
COOLDOWN_SECONDS = int(os.getenv("BOT_SIGNAL_COOLDOWN_SECONDS", "180"))

ALLOW_LIVE_SIGNALS = os.getenv("ALLOW_LIVE_SIGNALS", "false").strip().lower() == "true"
BOT_QUOTE_PER_TRADE = float(os.getenv("BOT_QUOTE_PER_TRADE", "15"))

MIN_MOVE_PCT = float(os.getenv("MIN_MOVE_PCT", "0.60"))
ESTIMATED_ROUNDTRIP_FEE_PCT = float(os.getenv("ESTIMATED_ROUNDTRIP_FEE_PCT", "0.20"))
ESTIMATED_SLIPPAGE_PCT = float(os.getenv("ESTIMATED_SLIPPAGE_PCT", "0.15"))
TP_PCT = float(os.getenv("TP_PCT", "1.3"))
MIN_NET_PROFIT_PCT = float(os.getenv("MIN_NET_PROFIT_PCT", "0.60"))
ATR_TO_TP_SANITY_FACTOR = float(os.getenv("ATR_TO_TP_SANITY_FACTOR", "0.20"))

USE_MA_FILTERS = os.getenv("USE_MA_FILTERS", "true").strip().lower() == "true"
MA_GAP_PCT = float(os.getenv("MA_GAP_PCT", "0.15"))
BUY_CONFIDENCE_MIN = float(os.getenv("BUY_CONFIDENCE_MIN", "0.70"))
BLOCK_SIGNALS_WHEN_ACTIVE_OCO = os.getenv("BLOCK_SIGNALS_WHEN_ACTIVE_OCO", "true").strip().lower() == "true"

GEN_DEBUG = os.getenv("GEN_DEBUG", "true").strip().lower() == "true"
GEN_LOG_EVERY_TICK = os.getenv("GEN_LOG_EVERY_TICK", "true").strip().lower() == "true"

EXCEL_MODEL_PATH = os.getenv(
    "EXCEL_MODEL_PATH",
    "/var/data/DYZEN_CAPITAL_OS_AI_LIVE_CORE_READY.xlsx",
).strip()

_last_emit_ts: float = 0.0
_emit_lock = Lock()

# ==============================
# SYMBOLS
# ==============================
def _parse_symbols() -> List[str]:
    raw = os.getenv("BOT_SYMBOLS", "").strip()
    if not raw:
        raw = os.getenv("SYMBOL_WHITELIST", "").strip()
    if not raw:
        raw = os.getenv("BOT_SYMBOL", "BTC/USDT").strip()

    return [s.strip().upper() for s in raw.split(",") if s.strip()]


SYMBOLS = _parse_symbols()

# ==============================
# CORE LOADER (lazy)
# ==============================
_CORE: Optional[ExcelLiveCore] = None


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

# ==============================
# EXCHANGE (lazy singleton)
# ==============================
_EXCHANGE: Optional[ccxt.Exchange] = None


def _build_exchange() -> ccxt.Exchange:
    ex_name = os.getenv("EXCHANGE", "binance").strip().lower()
    market_type = os.getenv("MARKET_TYPE", "spot").strip().lower()

    if ex_name == "bybit":
        return ccxt.bybit({
            "enableRateLimit": True,
            "apiKey": os.getenv("BYBIT_API_KEY", "").strip(),
            "secret": os.getenv("BYBIT_API_SECRET", "").strip(),
            "options": {"defaultType": market_type},
        })

    return ccxt.binance({
        "enableRateLimit": True,
        "apiKey": os.getenv("BINANCE_API_KEY", "").strip(),
        "secret": os.getenv("BINANCE_API_SECRET", "").strip(),
        "options": {"defaultType": market_type},
    })


def _exchange() -> ccxt.Exchange:
    global _EXCHANGE
    if _EXCHANGE is None:
        _EXCHANGE = _build_exchange()
    return _EXCHANGE

# ==============================
# HELPERS
# ==============================
def _now_utc_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _cooldown_ok() -> bool:
    with _emit_lock:
        return (time.time() - _last_emit_ts) >= COOLDOWN_SECONDS


def _emit(signal: Dict[str, Any], outbox_path: str) -> None:
    global _last_emit_ts
    with _emit_lock:
        append_signal(signal, outbox_path)
        _last_emit_ts = time.time()


def _get_outbox_path() -> str:
    return os.getenv("OUTBOX_PATH") or os.getenv("SIGNAL_OUTBOX_PATH") or "/var/data/signal_outbox.json"


def _pct(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return (a - b) / b * 100.0


def _sma(vals: List[float], n: int) -> float:
    if len(vals) < n:
        return sum(vals) / len(vals)
    return sum(vals[-n:]) / n


def _momentum(closes: List[float], n: int) -> float:
    if len(closes) < n + 1:
        return 0.0
    base = closes[-1 - n]
    return (closes[-1] / base) - 1.0 if base else 0.0


def _ups_count(closes: List[float], n: int) -> int:
    return sum(1 for i in range(-n, 0) if closes[i] > closes[i - 1])


def _atr_pct(ohlcv: List[List[float]], n: int = 14) -> float:
    if len(ohlcv) < n + 1:
        return 0.0
    trs = []
    for i in range(-n, 0):
        high = float(ohlcv[i][2])
        low = float(ohlcv[i][3])
        prev_close = float(ohlcv[i - 1][4])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    atr = sum(trs) / n
    last_close = float(ohlcv[-1][4])
    return (atr / last_close) * 100.0 if last_close else 0.0


def _vol_regime(atr_pct: float) -> str:
    if atr_pct >= 2.0:
        return "EXTREME"
    if atr_pct <= 0.30:
        return "LOW"
    return "NORMAL"


def _edge_ok(atr_pct: float) -> Tuple[bool, str]:
    if atr_pct < MIN_MOVE_PCT:
        return False, "ATR_TOO_LOW"

    assumed_cost = ESTIMATED_ROUNDTRIP_FEE_PCT + ESTIMATED_SLIPPAGE_PCT
    assumed_net = TP_PCT - assumed_cost

    if assumed_net < MIN_NET_PROFIT_PCT:
        return False, "EDGE_TOO_SMALL"

    if atr_pct < TP_PCT * ATR_TO_TP_SANITY_FACTOR:
        return False, "ATR_BELOW_TP"

    return True, "OK"

# ==============================
# MAIN
# ==============================
def generate_signal() -> Optional[Dict[str, Any]]:
    if not _cooldown_ok():
        return None

    outbox_path = _get_outbox_path()
    core = _core()
    exchange = _exchange()

    for symbol in SYMBOLS:
        active_oco = has_active_oco_for_symbol(symbol)

        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)
        except Exception as e:
            logger.exception(f"[GEN] FETCH_FAIL | {symbol} | {e}")
            continue

        if not ohlcv or len(ohlcv) < 30:
            continue

        closes = [float(c[4]) for c in ohlcv]
        vols = [float(c[5]) for c in ohlcv]

        last = closes[-1]
        prev = closes[-2]

        atrp = _atr_pct(ohlcv, 14)
        vol_reg = _vol_regime(atrp)

        # ---------------- SIGNAL FEATURES (improved)
        trend = max(0.0, min(1.0, (_momentum(closes, 3) * 80)))
        vol_score = min(1.0, vols[-1] / (sum(vols[-20:]) / 20.0)) if len(vols) >= 20 else 0.0
        conf = (
            0.5 * (1.0 if last > prev else 0.0)
            + 0.3 * trend
            + 0.2 * (1.0 if atrp < 2.0 else 0.0)
        )

        # ATR extreme penalty
        if atrp > 2.5:
            conf *= 0.7

        tmp_inp = CoreInputs(
            trend_strength=trend,
            structure_ok=True,
            volume_score=vol_score,
            risk_state="OK",
            confidence_score=conf,
            volatility_regime=vol_reg,
        )

        tmp_dec = core.decide(tmp_inp)
        ai_score = float(tmp_dec["ai_score"])

        risk = "KILL" if vol_reg == "EXTREME" else ("REDUCE" if ai_score < 0.45 else "OK")

        decision = core.decide(
            CoreInputs(
                trend_strength=trend,
                structure_ok=True,
                volume_score=vol_score,
                risk_state=risk,
                confidence_score=conf,
                volatility_regime=vol_reg,
            )
        )

        # 🚨 Protective SELL (FIXED POSITION)
        if active_oco and risk == "KILL":
            sig = {
                "signal_id": str(uuid.uuid4()),
                "ts_utc": _now_utc_iso(),
                "certified_signal": True,
                "final_verdict": "SELL",
                "meta": {
                    "source": "DYZEN_EXCEL_LIVE_CORE",
                    "symbol": symbol,
                    "reason": "RISK_KILL_OVERRIDE",
                    "decision": decision,
                },
                "execution": {
                    "symbol": symbol,
                    "direction": "LONG",
                    "entry": {"type": "MARKET"},
                },
            }
            _emit(sig, outbox_path)
            return sig

        if active_oco and BLOCK_SIGNALS_WHEN_ACTIVE_OCO:
            continue

        if decision["final_trade_decision"] != "EXECUTE":
            continue

        if conf < BUY_CONFIDENCE_MIN:
            continue

        ok_edge, _ = _edge_ok(atrp)
        if not ok_edge:
            continue

        if not ALLOW_LIVE_SIGNALS:
            continue

        sig = {
            "signal_id": str(uuid.uuid4()),
            "ts_utc": _now_utc_iso(),
            "certified_signal": True,
            "final_verdict": "TRADE",
            "meta": {
                "source": "DYZEN_EXCEL_LIVE_CORE",
                "symbol": symbol,
                "decision": decision,
            },
            "execution": {
                "symbol": symbol,
                "direction": "LONG",
                "entry": {"type": "MARKET"},
                "quote_amount": BOT_QUOTE_PER_TRADE,
            },
        }

        _emit(sig, outbox_path)
        return sig

    return None


def run_once(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return generate_signal()
