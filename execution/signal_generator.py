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

# -----------------------------
# ENV
# -----------------------------
MODE = os.getenv("MODE", "DEMO").strip().upper()
TIMEFRAME = os.getenv("BOT_TIMEFRAME", "15m").strip()
CANDLE_LIMIT = int(os.getenv("BOT_CANDLE_LIMIT", "80"))
COOLDOWN_SECONDS = int(os.getenv("BOT_SIGNAL_COOLDOWN_SECONDS", "180"))

ALLOW_LIVE_SIGNALS = os.getenv("ALLOW_LIVE_SIGNALS", "false").strip().lower() == "true"
BOT_QUOTE_PER_TRADE = float(os.getenv("BOT_QUOTE_PER_TRADE", "15"))

BLOCK_SIGNALS_WHEN_ACTIVE_OCO = os.getenv("BLOCK_SIGNALS_WHEN_ACTIVE_OCO", "true").strip().lower() == "true"

GEN_DEBUG = os.getenv("GEN_DEBUG", "true").strip().lower() == "true"
GEN_LOG_EVERY_TICK = os.getenv("GEN_LOG_EVERY_TICK", "true").strip().lower() == "true"
GEN_TEST_SIGNAL = os.getenv("GEN_TEST_SIGNAL", "false").strip().lower() == "true"
TEST_QUOTE_AMOUNT = float(os.getenv("TEST_QUOTE_AMOUNT", "5") or "5")

# Simple extra gates (outside Excel)
MA_GAP_PCT = float(os.getenv("MA_GAP_PCT", "0.15"))          # % gap to avoid chop
BUY_CONFIDENCE_MIN = float(os.getenv("BUY_CONFIDENCE_MIN", "0.70"))  # extra guard

DEFAULT_OUTBOX = "/var/data/signal_outbox.json"

_last_emit_ts: float = 0.0
_CORE: Optional[ExcelLiveCore] = None

# Exchange selection
EXCHANGE_ID = (os.getenv("EXCHANGE") or os.getenv("EXCHANGE_ID") or "bybit").strip().lower()
MARKET_TYPE = os.getenv("MARKET_TYPE", "swap").strip().lower()  # spot|swap
BYBIT_DEFAULT_TYPE = "swap" if MARKET_TYPE == "swap" else "spot"


def _now_utc_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))


def _get_outbox_path(outbox_path: Optional[str]) -> str:
    if outbox_path:
        return str(outbox_path).strip()
    return (os.getenv("OUTBOX_PATH") or os.getenv("SIGNAL_OUTBOX_PATH") or DEFAULT_OUTBOX).strip()


def _parse_symbols_from_env() -> List[str]:
    raw = (os.getenv("BOT_SYMBOLS") or os.getenv("SYMBOL_WHITELIST") or "BTC/USDT:USDT").strip()
    return [s.strip() for s in raw.split(",") if s.strip()]


def _cooldown_ok() -> bool:
    global _last_emit_ts
    if COOLDOWN_SECONDS <= 0:
        return True
    return (time.time() - _last_emit_ts) >= COOLDOWN_SECONDS


def _emit(signal: Dict[str, Any], outbox_path: str) -> None:
    global _last_emit_ts
    append_signal(signal, outbox_path)
    _last_emit_ts = time.time()


def _has_active_oco(symbol: str) -> bool:
    try:
        return bool(has_active_oco_for_symbol(symbol))
    except Exception as e:
        # safer: assume active to avoid uncontrolled trades
        logger.warning(f"[GEN] ACTIVE_OCO_CHECK_FAIL | symbol={symbol} err={e} -> assume active_oco=True")
        return True


def _resolve_excel_path() -> str:
    # 1) config getter (if exists)
    try:
        from execution.config import get_excel_model_path  # type: ignore
        p = str(get_excel_model_path() or "").strip()
        if p and os.path.exists(p):
            return p
        if p:
            logger.warning(f"[GEN] EXCEL_PATH_MISSING_FROM_CONFIG | path={p}")
    except Exception:
        pass

    # 2) env
    env_path = os.getenv("EXCEL_MODEL_PATH", "").strip()
    if env_path.lower().startswith("excel_model_path="):
        env_path = env_path.split("=", 1)[1].strip()

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
        path = _resolve_excel_path()
        logger.info(f"[GEN] EXCEL_CORE_LOADED | path={path}")
        _CORE = ExcelLiveCore(path)
    return _CORE


def _exchange() -> ccxt.Exchange:
    cls = getattr(ccxt, EXCHANGE_ID)

    api_key = os.getenv("BYBIT_API_KEY" if EXCHANGE_ID == "bybit" else "BINANCE_API_KEY", "").strip()
    api_secret = os.getenv("BYBIT_API_SECRET" if EXCHANGE_ID == "bybit" else "BINANCE_API_SECRET", "").strip()

    ex = cls({
        "enableRateLimit": True,
        "apiKey": api_key,
        "secret": api_secret,
        "options": {},
    })

    if EXCHANGE_ID == "bybit":
        ex.options["defaultType"] = BYBIT_DEFAULT_TYPE
        ex.options["defaultSubType"] = "linear"
        if MODE == "TESTNET":
            try:
                ex.set_sandbox_mode(True)
            except Exception:
                pass

    try:
        ex.load_markets()
    except Exception as e:
        logger.warning(f"[GEN] LOAD_MARKETS_WARN | exchange={EXCHANGE_ID} err={e}")

    return ex


EX = _exchange()


def _pct(a: float, b: float) -> float:
    if not b:
        return 0.0
    return (float(a) - float(b)) / float(b) * 100.0


def _sma(vals: List[float], n: int) -> float:
    if not vals:
        return 0.0
    if len(vals) < n:
        return sum(vals) / max(1, len(vals))
    w = vals[-n:]
    return sum(w) / n


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


def _vol_regime(atrp: float) -> str:
    # Excel core expects: LOW / NORMAL / EXTREME
    if atrp <= 0.30:
        return "LOW"
    if atrp >= 2.00:
        return "EXTREME"
    return "NORMAL"


def _ma_gap_ok(last: float, ma20: float) -> bool:
    gap = abs(_pct(last, ma20))
    return gap >= MA_GAP_PCT


def _volume_score(ohlcv: List[List[float]]) -> float:
    # volume ratio last / SMA20(vol) mapped to 0..1
    vols = [float(x[5]) for x in ohlcv if len(x) > 5]
    if not vols or len(vols) < 25:
        return 0.50
    v_last = float(vols[-1])
    v_sma = float(_sma(vols, 20))
    if v_sma <= 0:
        return 0.50
    ratio = v_last / v_sma
    # 1.0 ratio => 0.66, 1.5 ratio => 1.0, 0.5 ratio => 0.33
    return _clamp(ratio / 1.5, 0.0, 1.0)


def _trend_strength(last: float, ma20: float, ma20_prev: float) -> float:
    """
    0..1 proxy:
    - positive distance above MA20
    - plus MA20 slope
    """
    dist = (last - ma20) / ma20 if ma20 else 0.0
    slope = (ma20 - ma20_prev) / ma20_prev if ma20_prev else 0.0

    # scale: 0..1% above MA maps towards 1
    dist_score = _clamp(dist / 0.01, 0.0, 1.0)
    # slope: 0..0.3% slope maps towards 1
    slope_score = _clamp(slope / 0.003, 0.0, 1.0)

    return _clamp(0.7 * dist_score + 0.3 * slope_score, 0.0, 1.0)


def _confidence_score(trend_strength: float, vol_score: float) -> float:
    # simple blended proxy (Excel still computes ai_score using weights)
    return _clamp(0.65 * trend_strength + 0.35 * vol_score, 0.0, 1.0)


def _build_trade_signal(symbol: str, direction: str, quote_amount: float) -> Dict[str, Any]:
    sid = f"DYZEN-{uuid.uuid4().hex[:14]}"
    return {
        "signal_id": sid,
        "ts_utc": _now_utc_iso(),
        "source": "EXCEL_LIVE_CORE",
        "final_verdict": "TRADE",
        "direction": direction,
        "execution": {
            "symbol": symbol,
            "direction": direction,
            "side": "BUY" if direction.upper() == "LONG" else "SELL",
            "quote_amount": float(quote_amount),
        },
    }


def generate_signal(
    outbox_path: Optional[str] = None,
    symbols_override: Optional[List[str]] = None
) -> bool:
    """
    ✅ main.py compatibility:
        created = generate_once(outbox_path, symbols_override=active_symbols)

    Returns True if a signal was emitted, else False.
    """
    outbox = _get_outbox_path(outbox_path)

    if not _cooldown_ok():
        return False

    symbols = symbols_override if (symbols_override and len(symbols_override) > 0) else _parse_symbols_from_env()

    # optional test signal
    if GEN_TEST_SIGNAL:
        sym = symbols[0] if symbols else "ETH/USDT:USDT"
        sid = f"TEST-{uuid.uuid4()}"
        test_sig = {
            "signal_id": sid,
            "ts_utc": _now_utc_iso(),
            "source": "TEST",
            "final_verdict": "TRADE",
            "direction": "LONG",
            "execution": {"symbol": sym, "direction": "LONG", "side": "BUY", "quote_amount": float(TEST_QUOTE_AMOUNT)},
        }
        _emit(test_sig, outbox)
        logger.info(f"[GEN] TEST_SIGNAL_EMITTED | id={sid} symbol={sym} outbox={outbox}")
        return True

    core = _core()

    for symbol in symbols:
        active_oco = _has_active_oco(symbol)
        if BLOCK_SIGNALS_WHEN_ACTIVE_OCO and active_oco:
            if GEN_LOG_EVERY_TICK:
                logger.info(f"[GEN] BLOCK_ACTIVE_OCO | symbol={symbol}")
            continue

        # fetch OHLCV
        try:
            t0 = time.time()
            ohlcv = EX.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)
            dt_ms = int((time.time() - t0) * 1000)
            if GEN_DEBUG:
                logger.info(f"[GEN] FETCH_OK | symbol={symbol} tf={TIMEFRAME} candles={len(ohlcv) if ohlcv else 0} dt={dt_ms}ms")
        except Exception as e:
            logger.warning(f"[GEN] FETCH_FAIL | symbol={symbol} tf={TIMEFRAME} err={e}")
            continue

        if not ohlcv or len(ohlcv) < 25:
            continue

        closes = [float(x[4]) for x in ohlcv]
        last = float(closes[-1])
        ma20 = float(_sma(closes, 20))
        ma20_prev = float(_sma(closes[:-5], 20)) if len(closes) >= 30 else ma20

        atrp = float(_atr_pct(ohlcv, 14))
        vol_reg = _vol_regime(atrp)

        # build CoreInputs for your Excel core (THIS IS THE FIX)
        vol_score = _volume_score(ohlcv)
        trend = _trend_strength(last, ma20, ma20_prev)
        struct_ok = (last > ma20) and _ma_gap_ok(last, ma20)
        conf = _confidence_score(trend, vol_score)

        # extra guard from env (optional)
        if conf < BUY_CONFIDENCE_MIN:
            if GEN_LOG_EVERY_TICK:
                logger.info(f"[GEN] STAND_BY | symbol={symbol} reason=CONF_TOO_LOW conf={conf:.3f} < {BUY_CONFIDENCE_MIN:.3f}")
            continue

        inp = CoreInputs(
            trend_strength=float(trend),
            structure_ok=bool(struct_ok),
            volume_score=float(vol_score),
            risk_state="OK",
            confidence_score=float(conf),
            volatility_regime=str(vol_reg),
            # pass ATR% as a "ratio proxy" (Excel default MIN_VOL_FOR_AGGRESSION=0.10)
            volatility_ratio=float(atrp),
        )

        # core decision (dict)
        try:
            out = core.decide(inp)
            ai = float(out.get("ai_score") or 0.0)
            macro = str(out.get("macro_gate") or "ALLOW")
            strat = str(out.get("active_strategy") or "NO")
            final = str(out.get("final_trade_decision") or "STAND_BY")
            adaptive_gate = float(out.get("adaptive_buy_gate") or 0.0)
        except Exception as e:
            logger.warning(f"[GEN] CORE_FAIL | symbol={symbol} err={e}")
            continue

        if GEN_LOG_EVERY_TICK:
            logger.info(
                f"[GEN] CORE_DECISION | symbol={symbol} ai={ai:.3f} macro={macro} strat={strat} "
                f"final={final} volReg={vol_reg} atr%={atrp:.2f} last={last:.4f} ma20={ma20:.4f} "
                f"trend={trend:.3f} volScore={vol_score:.3f} conf={conf:.3f} gate={adaptive_gate:.3f} outbox={outbox}"
            )

        # gates
        if MODE == "LIVE" and not ALLOW_LIVE_SIGNALS:
            logger.warning(f"[GEN] LIVE_SIGNALS_BLOCKED | ALLOW_LIVE_SIGNALS=false | symbol={symbol}")
            continue

        if final.upper() != "EXECUTE":
            continue

        sig = _build_trade_signal(symbol=symbol, direction="LONG", quote_amount=BOT_QUOTE_PER_TRADE)
        _emit(sig, outbox)
        logger.info(f"[GEN] SIGNAL_EMITTED | id={sig['signal_id']} symbol={symbol} quote={BOT_QUOTE_PER_TRADE} outbox={outbox}")
        return True

    return False
