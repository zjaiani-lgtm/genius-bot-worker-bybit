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

# Test signal (forces one outbox write for debugging)
GEN_TEST_SIGNAL = os.getenv("GEN_TEST_SIGNAL", "false").strip().lower() == "true"

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
# Support both EXCHANGE_ID and EXCHANGE envs
EXCHANGE_ID = (
    os.getenv("EXCHANGE_ID")
    or os.getenv("EXCHANGE")
    or "bybit"
).strip().lower()

# Output file (outbox)
DEFAULT_OUTBOX = "/var/data/signal_outbox.json"

_last_emit_ts = 0.0
_core_singleton: Optional[ExcelLiveCore] = None
_test_signal_sent = False


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

    # Bybit specifics: linear swap
    try:
        ex.options["defaultType"] = "swap"
        ex.options["defaultSubType"] = "linear"
    except Exception:
        pass

    # Load credentials if present (optional)
    api_key = os.getenv("BYBIT_API_KEY") or os.getenv("API_KEY")
    api_secret = os.getenv("BYBIT_API_SECRET") or os.getenv("API_SECRET")
    if api_key and api_secret:
        ex.apiKey = api_key
        ex.secret = api_secret

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
    return (
        os.getenv("OUTBOX_PATH")
        or os.getenv("SIGNAL_OUTBOX_PATH")
        or DEFAULT_OUTBOX
    )


def _maybe_send_test_signal(outbox_path: str) -> None:
    """
    If GEN_TEST_SIGNAL=true, we write exactly one signal to the outbox for sanity-checking worker consumption.
    """
    global _test_signal_sent
    if not GEN_TEST_SIGNAL or _test_signal_sent:
        return

    sig_id = f"TEST-{uuid.uuid4()}"
    sym = SYMBOLS[0] if SYMBOLS else "BTC/USDT:USDT"
    sig = {
        "signal_id": sig_id,
        "ts_utc": _now_utc_iso(),
        "certified_signal": True,
        "final_verdict": "TRADE",
        "meta": {"source": "GEN_TEST_SIGNAL", "symbol": sym},
        "execution": {"symbol": sym, "direction": "LONG", "entry": {"type": "MARKET"}, "quote_amount": 5.0},
    }
    _emit(sig, outbox_path)
    _test_signal_sent = True
    logger.info(f"[GEN] TEST_SIGNAL_EMITTED | id={sig_id} symbol={sym} outbox={outbox_path}")


def generate_signal(
    outbox_path: Optional[str] = None,
    symbols_override: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Excel Live Core based generator:
    - If no active OCO: emits TRADE only when final_trade_decision == EXECUTE.
    - If active OCO: can emit SELL if risk_state == KILL (protective override).

    Params:
      - outbox_path: override path for outbox (if None uses env)
      - symbols_override: optional active basket from auto-scaler (if provided, only those are scanned)
    """
    if outbox_path is None:
        outbox_path = _get_outbox_path()

    # optional test signal
    _maybe_send_test_signal(outbox_path)

    if not _cooldown_ok():
        return None

    core = _core()

    symbols = symbols_override if (symbols_override and len(symbols_override) > 0) else SYMBOLS

    for symbol in symbols:
        active_oco = _has_active_oco(symbol)

        # -------------------------
        # FETCH OHLCV
        # -------------------------
        try:
            t0 = time.time()
            ohlcv = EXCHANGE.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)
            dt_ms = int((time.time() - t0) * 1000)
            if GEN_DEBUG:
                logger.info(
                    f"[GEN] FETCH_OK | symbol={symbol} tf={TIMEFRAME} candles={len(ohlcv) if ohlcv else 0} dt={dt_ms}ms"
                )
        except Exception as e:
            logger.exception(f"[GEN] FETCH_FAIL | symbol={symbol} tf={TIMEFRAME} err={e}")
            continue

        if not ohlcv or len(ohlcv) < 30:
            if GEN_DEBUG:
                logger.info(f"[GEN] SKIP_INSUFFICIENT_CANDLES | symbol={symbol} have={len(ohlcv) if ohlcv else 0}")
            continue

        # Build series
        closes = [float(x[4]) for x in ohlcv]
        highs = [float(x[2]) for x in ohlcv]
        lows = [float(x[3]) for x in ohlcv]
        vols = [float(x[5]) for x in ohlcv]

        last = closes[-1]
        ma20 = sum(closes[-20:]) / 20.0

        # ATR% estimate (simple)
        tr = []
        for i in range(1, len(closes)):
            tr.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
        atr = sum(tr[-14:]) / 14.0 if len(tr) >= 14 else (sum(tr) / len(tr) if tr else 0.0)
        atrp = (atr / last) * 100.0 if last else 0.0

        # Simple trend strength
        trend = _pct(last, ma20)
        trend_strength = min(1.0, abs(trend) / 1.0)  # normalize (1% move ~= 1.0)

        # structure ok (simple)
        struct_ok = abs(trend) >= 0.05

        # volume score
        v_ma = sum(vols[-20:]) / 20.0
        vol_score = min(1.0, (vols[-1] / v_ma) if v_ma else 0.0)

        # risk state (placeholder - your Excel core likely sets this too)
        risk = "OK"

        # confidence score (proxy; ExcelCore likely overrides via adaptive gate)
        conf = min(1.0, max(0.0, (0.5 + (trend / 2.0))))

        # volatility regime (simple)
        vol_reg = "NORMAL"

        inp = CoreInputs(
            symbol=symbol,
            last_price=last,
            ma20=ma20,
            atr_percent=atrp,
            trend_strength=trend_strength,
            structure_ok=struct_ok,
            volume_score=vol_score,
            risk_state=risk,
            confidence_score=conf,
            volatility_regime=vol_reg,
        )

        # -------------------------
        # CORE DECISION
        # -------------------------
        try:
            decision = core.decide(inp)
        except Exception as e:
            logger.exception(f"[GEN] CORE_DECIDE_FAIL | symbol={symbol} err={e}")
            continue

        if GEN_DEBUG:
            reasons = decision.get("reasons", {}) or {}
            failed = []
            for k in ("trend_ok", "structure_ok", "volume_ok", "confidence_ok", "risk_ok", "volband_ok"):
                if k in reasons and reasons.get(k) is False:
                    failed.append(k)

            logger.info(
                f"[GEN] CORE_DECISION | symbol={symbol} "
                f"ai={decision.get('ai_score', 0.0):.3f} macro={decision.get('macro_gate')} "
                f"strat={decision.get('active_strategy')} final={decision.get('final_trade_decision')} "
                f"risk={risk} volReg={vol_reg} atr%={atrp:.2f} "
                f"last={last:.6f} ma20={ma20:.6f} "
                f"sizeMult={decision.get('adaptive_size_mult', 1.0):.2f} "
                f"failed={','.join(failed) if failed else 'none'} outbox={outbox_path}"
            )

            if decision.get("final_trade_decision") != "EXECUTE":
                logger.info(
                    f"[GEN] STAND_BY_BREAKDOWN | symbol={symbol} "
                    f"trend_ok={reasons.get('trend_ok')} struct_ok={reasons.get('structure_ok')} "
                    f"vol_ok={reasons.get('volume_ok')} conf_ok={reasons.get('confidence_ok')} "
                    f"risk_ok={reasons.get('risk_ok')} volband_ok={reasons.get('volband_ok')} "
                    f"conf={reasons.get('confidence_score')} trend={reasons.get('trend_strength')} "
                    f"volScore={reasons.get('volume_score')} vr={reasons.get('volatility_regime')} "
                    f"vrRatio={reasons.get('volatility_ratio')}"
                )

        # Protective SELL if active OCO and risk is KILL (your current risk is "OK" but keep it future-proof)
        if active_oco and risk == "KILL":
            signal_id = str(uuid.uuid4())
            sig = {
                "signal_id": signal_id,
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

        # If active OCO → we do not open new TRADE (risk-first)
        if active_oco and BLOCK_SIGNALS_WHEN_ACTIVE_OCO:
            if GEN_DEBUG:
                logger.info(f"[GEN] BLOCKED_BY_ACTIVE_OCO | symbol={symbol}")
            continue

        # TRADE only if final decision says EXECUTE
        if decision.get("final_trade_decision") != "EXECUTE":
            continue

        # -----------------------------
        # EXTRA LIVE GUARDS (fee-aware)
        # -----------------------------

        # 1) Avoid chop: require distance from MA
        ma_gap_abs = abs(_pct(last, ma20))
        if ma_gap_abs < MA_GAP_PCT:
            if GEN_DEBUG:
                logger.info(
                    f"[GEN] BLOCKED_BY_MA_GAP | symbol={symbol} gap%={ma_gap_abs:.3f} < MA_GAP_PCT={MA_GAP_PCT:.3f}"
                )
            continue

        # 2) Confidence floor (extra check)
        if conf < BUY_CONFIDENCE_MIN:
            if GEN_DEBUG:
                logger.info(
                    f"[GEN] BLOCKED_BY_CONF | symbol={symbol} conf={conf:.3f} < BUY_CONFIDENCE_MIN={BUY_CONFIDENCE_MIN:.3f}"
                )
            continue

        # 3) Fee-aware edge gate
        ok_edge, edge_reason = _edge_ok(atrp)
        if not ok_edge:
            if GEN_DEBUG:
                logger.info(f"[GEN] BLOCKED_BY_EDGE | symbol={symbol} reason={edge_reason}")
            continue

        # Safety: don't emit live trades if not allowed
        if not ALLOW_LIVE_SIGNALS:
            if GEN_DEBUG:
                logger.info(f"[GEN] BLOCKED_BY_ENV | symbol={symbol} reason=ALLOW_LIVE_SIGNALS=false")
            continue

        # build TRADE signal
        signal_id = str(uuid.uuid4())
        sig = {
            "signal_id": signal_id,
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
                "quote_amount": BOT_QUOTE_PER_TRADE,  # size in USDT (helps NOTIONAL)
            },
        }

        _emit(sig, outbox_path)
        return sig

    return None


# -----------------------------
# COMPATIBILITY ENTRYPOINTS
# -----------------------------
def run_once(*args, **kwargs) -> Optional[Dict[str, Any]]:
    """
    Backwards-compatible entrypoint expected by older bootstrap code.
    """
    outbox_path = None
    symbols_override = None

    # Accept positional: run_once(outbox_path, symbols_override)
    if len(args) >= 1:
        outbox_path = args[0]
    if len(args) >= 2:
        symbols_override = args[1]

    # Accept kwargs: run_once(outbox_path=..., symbols_override=...)
    outbox_path = kwargs.get("outbox_path", outbox_path)
    symbols_override = kwargs.get("symbols_override", symbols_override)

    return generate_signal(outbox_path=outbox_path, symbols_override=symbols_override)
