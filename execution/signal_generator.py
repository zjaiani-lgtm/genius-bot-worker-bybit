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

# Guards / Edge
MIN_MOVE_PCT = float(os.getenv("MIN_MOVE_PCT", "0.60"))
MA_GAP_PCT = float(os.getenv("MA_GAP_PCT", "0.15"))
BUY_CONFIDENCE_MIN = float(os.getenv("BUY_CONFIDENCE_MIN", "0.70"))
ESTIMATED_ROUNDTRIP_FEE_PCT = float(os.getenv("ESTIMATED_ROUNDTRIP_FEE_PCT", "0.20"))
ESTIMATED_SLIPPAGE_PCT = float(os.getenv("ESTIMATED_SLIPPAGE_PCT", "0.15"))
TP_PCT = float(os.getenv("TP_PCT", "1.3"))
MIN_NET_PROFIT_PCT = float(os.getenv("MIN_NET_PROFIT_PCT", "0.60"))

DEFAULT_OUTBOX = "/var/data/signal_outbox.json"

_last_emit_ts: float = 0.0
_CORE: Optional[ExcelLiveCore] = None

# Exchange selection
EXCHANGE_ID = (os.getenv("EXCHANGE") or os.getenv("EXCHANGE_ID") or "bybit").strip().lower()
MARKET_TYPE = os.getenv("MARKET_TYPE", "swap").strip().lower()  # spot|swap
BYBIT_DEFAULT_TYPE = "swap" if MARKET_TYPE == "swap" else "spot"


def _now_utc_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _get_outbox_path() -> str:
    return (os.getenv("OUTBOX_PATH") or os.getenv("SIGNAL_OUTBOX_PATH") or DEFAULT_OUTBOX).strip()


def _parse_symbols() -> List[str]:
    raw = (os.getenv("BOT_SYMBOLS") or os.getenv("SYMBOL_WHITELIST") or "BTC/USDT:USDT").strip()
    syms = []
    for s in raw.split(","):
        s = s.strip()
        if s:
            syms.append(s)
    return syms


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
        # safer: assume OCO active to avoid uncontrolled trades
        logger.warning(f"[GEN] ACTIVE_OCO_CHECK_FAIL | symbol={symbol} err={e} -> assume active_oco=True")
        return True


def _resolve_excel_path() -> str:
    """
    Prefer config.get_excel_model_path() if available, else EXCEL_MODEL_PATH env,
    and fallback to known locations.
    """
    # 1) config getter (your fixed config.py can provide it)
    try:
        from execution.config import get_excel_model_path  # type: ignore
        p = str(get_excel_model_path() or "").strip()
        if p and os.path.exists(p):
            return p
        if p:
            # config returned but doesn't exist -> continue to fallback
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

    # debug info
    try:
        assets_list = os.listdir("/opt/render/project/src/assets")
    except Exception:
        assets_list = []
    try:
        var_data_list = os.listdir("/var/data")
    except Exception:
        var_data_list = []

    raise FileNotFoundError(
        f"EXCEL_MODEL_NOT_FOUND | env={env_path} | assets={assets_list} | var_data={var_data_list}"
    )


def _core() -> ExcelLiveCore:
    global _CORE
    if _CORE is None:
        path = _resolve_excel_path()
        logger.info(f"[GEN] EXCEL_CORE_LOADED | path={path}")
        _CORE = ExcelLiveCore(path)
    return _CORE


def _exchange() -> ccxt.Exchange:
    cls = getattr(ccxt, EXCHANGE_ID)

    # Keys are optional for OHLCV, but keep them if set (for some endpoints)
    api_key = os.getenv("BYBIT_API_KEY" if EXCHANGE_ID == "bybit" else "BINANCE_API_KEY", "").strip()
    api_secret = os.getenv("BYBIT_API_SECRET" if EXCHANGE_ID == "bybit" else "BINANCE_API_SECRET", "").strip()

    ex = cls({
        "enableRateLimit": True,
        "apiKey": api_key,
        "secret": api_secret,
        "options": {},
    })

    # Bybit market type tuning for symbols like BTC/USDT:USDT
    if EXCHANGE_ID == "bybit":
        ex.options["defaultType"] = BYBIT_DEFAULT_TYPE
        # these help with linear USDT perp in some ccxt versions
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


def _vol_regime(atr_pct: float) -> str:
    if atr_pct >= 2.0:
        return "EXTREME"
    if atr_pct <= 0.30:
        return "LOW"
    return "NORMAL"


def _edge_ok(atr_pct: float) -> Tuple[bool, str]:
    if atr_pct < MIN_MOVE_PCT:
        return False, f"ATR_TOO_LOW atr%={atr_pct:.2f} < MIN_MOVE_PCT={MIN_MOVE_PCT:.2f}"

    assumed_gross_edge = TP_PCT
    assumed_cost = ESTIMATED_ROUNDTRIP_FEE_PCT + ESTIMATED_SLIPPAGE_PCT
    assumed_net = assumed_gross_edge - assumed_cost

    if assumed_net < MIN_NET_PROFIT_PCT:
        return False, (
            "EDGE_TOO_SMALL "
            f"TP_PCT={assumed_gross_edge:.2f} cost={assumed_cost:.2f} net={assumed_net:.2f} "
            f"< MIN_NET_PROFIT_PCT={MIN_NET_PROFIT_PCT:.2f}"
        )

    if atr_pct < (assumed_gross_edge * 0.75):
        return False, f"ATR_BELOW_TP atr%={atr_pct:.2f} < 0.75*TP_PCT={assumed_gross_edge * 0.75:.2f}"

    return True, "OK"


def _ma_gap_ok(last: float, ma20: float) -> Tuple[bool, str]:
    gap = abs(_pct(last, ma20))
    if gap < MA_GAP_PCT:
        return False, f"MA_GAP_TOO_SMALL gap%={gap:.3f} < MA_GAP_PCT={MA_GAP_PCT:.3f}"
    return True, "OK"


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
            "side": "BUY" if direction.upper() == "LONG" else "SELL",
            "quote_amount": float(quote_amount),
        },
    }


def generate_signal() -> Optional[Dict[str, Any]]:
    """
    Legacy API (some main.py expects this).
    Creates at most 1 signal per call.
    """
    outbox_path = _get_outbox_path()

    if not _cooldown_ok():
        return None

    symbols = _parse_symbols()
    core = _core()

    for symbol in symbols:
        active_oco = _has_active_oco(symbol)

        # Optional risk-first gate
        if BLOCK_SIGNALS_WHEN_ACTIVE_OCO and active_oco:
            if GEN_LOG_EVERY_TICK:
                logger.info(f"[GEN] BLOCK_ACTIVE_OCO | symbol={symbol}")
            continue

        # Fetch OHLCV
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
        atrp = float(_atr_pct(ohlcv, 14))
        vol_reg = _vol_regime(atrp)

        edge_ok, edge_reason = _edge_ok(atrp)
        mag_ok, mag_reason = _ma_gap_ok(last, ma20)

        # Excel decision
        try:
            inputs = CoreInputs(
                symbol=symbol,
                timeframe=TIMEFRAME,
                last=last,
                ma20=ma20,
                atr_pct=atrp,
            )
            core_out = core.decide(inputs)
            # expected keys: ai_confidence, macro_policy, strategy_flag, final_trade_decision, risk_state
            ai = float(getattr(core_out, "ai_confidence", 0.0) or 0.0)
            macro = str(getattr(core_out, "macro_policy", "ALLOW") or "ALLOW")
            strat = str(getattr(core_out, "strategy_flag", "NO") or "NO")
            final = str(getattr(core_out, "final_trade_decision", "STAND_BY") or "STAND_BY")
            risk = str(getattr(core_out, "risk_state", "OK") or "OK")
        except Exception as e:
            logger.warning(f"[GEN] CORE_FAIL | symbol={symbol} err={e}")
            continue

        # Macro + risk gate
        if str(macro).upper() not in ("ALLOW", "ON", "TRUE"):
            if GEN_LOG_EVERY_TICK:
                logger.info(f"[GEN] STAND_BY | symbol={symbol} reason=MACRO_BLOCK macro={macro}")
            continue
        if str(risk).upper() not in ("OK", "SAFE", "GREEN"):
            if GEN_LOG_EVERY_TICK:
                logger.info(f"[GEN] STAND_BY | symbol={symbol} reason=RISK_BLOCK risk={risk}")
            continue

        # Edge gates
        if not edge_ok:
            if GEN_LOG_EVERY_TICK:
                logger.info(f"[GEN] STAND_BY | symbol={symbol} reason={edge_reason} atr%={atrp:.2f} volReg={vol_reg}")
            continue
        if not mag_ok:
            if GEN_LOG_EVERY_TICK:
                logger.info(f"[GEN] STAND_BY | symbol={symbol} reason={mag_reason} last={last:.4f} ma20={ma20:.4f}")
            continue

        # Confidence gate
        if ai < BUY_CONFIDENCE_MIN:
            if GEN_LOG_EVERY_TICK:
                logger.info(f"[GEN] STAND_BY | symbol={symbol} reason=AI_TOO_LOW ai={ai:.3f} < {BUY_CONFIDENCE_MIN:.3f}")
            continue

        # Final decision gate
        if str(final).upper() != "EXECUTE":
            if GEN_LOG_EVERY_TICK:
                logger.info(
                    f"[GEN] CORE_DECISION | symbol={symbol} ai={ai:.3f} macro={macro} strat={strat} "
                    f"final={final} risk={risk} volReg={vol_reg} atr%={atrp:.2f} last={last:.4f} ma20={ma20:.4f} "
                    f"outbox={outbox_path}"
                )
            continue

        # Live generation gate
        if MODE == "LIVE" and not ALLOW_LIVE_SIGNALS:
            logger.warning(f"[GEN] LIVE_SIGNALS_BLOCKED | ALLOW_LIVE_SIGNALS=false | symbol={symbol}")
            continue

        sig = _build_trade_signal(symbol=symbol, direction="LONG", quote_amount=BOT_QUOTE_PER_TRADE)
        _emit(sig, outbox_path)
        logger.info(f"[GEN] SIGNAL_EMITTED | id={sig['signal_id']} symbol={symbol} quote={BOT_QUOTE_PER_TRADE} outbox={outbox_path}")
        return sig

    return None


def run_once(outbox_path: str, symbols_override: Optional[List[str]] = None) -> bool:
    """
    New API (your newer main.py uses this).
    Returns True if a signal was created.
    """
    # override outbox if passed
    if outbox_path:
        os.environ["SIGNAL_OUTBOX_PATH"] = str(outbox_path)

    if symbols_override:
        # turn list into BOT_SYMBOLS for this tick only
        os.environ["BOT_SYMBOLS"] = ",".join([str(s).strip() for s in symbols_override if str(s).strip()])

    # test signal (optional)
    if GEN_TEST_SIGNAL:
        sid = f"TEST-{uuid.uuid4()}"
        sym = (symbols_override[0] if symbols_override else (_parse_symbols()[0] if _parse_symbols() else "BTC/USDT:USDT"))
        test_sig = {
            "signal_id": sid,
            "ts_utc": _now_utc_iso(),
            "source": "TEST",
            "final_verdict": "TRADE",
            "direction": "LONG",
            "execution": {"symbol": sym, "side": "BUY", "quote_amount": float(os.getenv("TEST_QUOTE_AMOUNT", "5") or "5")},
        }
        _emit(test_sig, _get_outbox_path())
        logger.info(f"[GEN] TEST_SIGNAL_EMITTED | id={sid} symbol={sym} outbox={_get_outbox_path()}")
        return True

    sig = generate_signal()
    return bool(sig)


# Backward-compat alias (some older code imports generate_once)
generate_once = run_once
