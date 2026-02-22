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

# -----------------------------
# SAFETY / EDGE GATES
# -----------------------------
DEFAULT_MIN_MOVE_PCT = 0.45
HARD_OVERRIDE_MIN_MOVE_PCT = os.getenv("HARD_OVERRIDE_MIN_MOVE_PCT", "false").strip().lower() == "true"
if HARD_OVERRIDE_MIN_MOVE_PCT:
    MIN_MOVE_PCT = DEFAULT_MIN_MOVE_PCT
else:
    MIN_MOVE_PCT = float(os.getenv("MIN_MOVE_PCT", str(DEFAULT_MIN_MOVE_PCT)))

MA_GAP_PCT = float(os.getenv("MA_GAP_PCT", "0.15"))
BUY_CONFIDENCE_MIN = float(os.getenv("BUY_CONFIDENCE_MIN", "0.64"))

ESTIMATED_ROUNDTRIP_FEE_PCT = float(os.getenv("ESTIMATED_ROUNDTRIP_FEE_PCT", "0.20"))
ESTIMATED_SLIPPAGE_PCT = float(os.getenv("ESTIMATED_SLIPPAGE_PCT", "0.15"))

TP_PCT = float(os.getenv("TP_PCT", "1.3"))
MIN_NET_PROFIT_PCT = float(os.getenv("MIN_NET_PROFIT_PCT", "0.60"))

ATR_TO_TP_SANITY_FACTOR = float(os.getenv("ATR_TO_TP_SANITY_FACTOR", "0.50"))

BLOCK_SIGNALS_WHEN_ACTIVE_OCO = os.getenv("BLOCK_SIGNALS_WHEN_ACTIVE_OCO", "true").strip().lower() == "true"

GEN_DEBUG = os.getenv("GEN_DEBUG", "true").strip().lower() == "true"
GEN_LOG_EVERY_TICK = os.getenv("GEN_LOG_EVERY_TICK", "true").strip().lower() == "true"
GEN_LOG_REASONS = os.getenv("GEN_LOG_REASONS", "true").strip().lower() == "true"
GEN_LOG_LOCAL_GATES = os.getenv("GEN_LOG_LOCAL_GATES", "true").strip().lower() == "true"

# NEW: deep diagnostics
GEN_LOG_DIAGNOSTICS = os.getenv("GEN_LOG_DIAGNOSTICS", "true").strip().lower() == "true"
GEN_LOG_THRESHOLDS_ONCE = os.getenv("GEN_LOG_THRESHOLDS_ONCE", "true").strip().lower() == "true"

# -----------------------------
# VOLUME RECALIBRATION
# -----------------------------
VOLUME_SCORE_SCALE = float(os.getenv("VOLUME_SCORE_SCALE", "2.5"))
VOLUME_SCORE_FLOOR = float(os.getenv("VOLUME_SCORE_FLOOR", "0.20"))
VOLUME_SCORE_CAP = float(os.getenv("VOLUME_SCORE_CAP", "1.00"))

# -----------------------------
# STRUCTURE SOFT OVERRIDE
# -----------------------------
STRUCT_SOFT_OVERRIDE = os.getenv("STRUCT_SOFT_OVERRIDE", "true").strip().lower() == "true"
STRUCT_SOFT_MIN_TREND = float(os.getenv("STRUCT_SOFT_MIN_TREND", "0.68"))
STRUCT_SOFT_MIN_MA_GAP = float(os.getenv("STRUCT_SOFT_MIN_MA_GAP", "0.25"))
STRUCT_SOFT_REQUIRE_LAST_UP = int(os.getenv("STRUCT_SOFT_REQUIRE_LAST_UP", "2"))  # last N closes rising

EXCEL_MODEL_PATH = os.getenv("EXCEL_MODEL_PATH", "/var/data/DYZEN_CAPITAL_OS_AI_LIVE_CORE_READY.xlsx").strip()
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
_THRESHOLDS_LOGGED = False


def _now_utc_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _parse_symbols() -> List[str]:
    raw = os.getenv("BOT_SYMBOLS", "").strip()
    if not raw:
        raw = os.getenv("SYMBOL_WHITELIST", "").strip()
    if not raw:
        raw = os.getenv("BOT_SYMBOL", "BTC/USDT").strip()

    syms: List[str] = []
    for s in raw.split(","):
        s = s.strip()
        if not s:
            continue
        syms.append(s.upper())
    return syms


SYMBOLS = _parse_symbols()


def _has_active_oco(symbol: str) -> bool:
    try:
        return has_active_oco_for_symbol(symbol)
    except Exception as e:
        logger.warning(f"[GEN] ACTIVE_OCO_CHECK_FAIL | symbol={symbol} err={e} -> assume active_oco=True")
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
    global _CORE, _THRESHOLDS_LOGGED
    if _CORE is None:
        resolved = _resolve_excel_path(EXCEL_MODEL_PATH)
        logger.info(
            f"[GEN] EXCEL_PATH | env={EXCEL_MODEL_PATH} resolved={resolved} exists_env={os.path.exists(EXCEL_MODEL_PATH)}"
        )
        _CORE = ExcelLiveCore(resolved)
        logger.info(f"[GEN] EXCEL_CORE_LOADED | path={resolved}")

    # Log core thresholds/weights once for debugging
    if _CORE is not None and GEN_LOG_THRESHOLDS_ONCE and not _THRESHOLDS_LOGGED:
        try:
            th = _CORE.thresholds or {}
            w = _CORE.weights or {}
            trend_th = (th.get("trend strength", {}) or {}).get("num")
            vol_th = (th.get("volume confirmation", {}) or {}).get("num")
            conf_th = (th.get("confidence score", {}) or {}).get("num")
            logger.info(
                "[GEN] CORE_THRESHOLDS | "
                f"trend_th={trend_th} vol_th={vol_th} conf_th={conf_th} "
                f"softVol(enabled={_CORE.enable_soft_volume_override} ai_min={_CORE.soft_volume_ai_min} relax={_CORE.soft_volume_relax} requireVolBand={_CORE.soft_volume_require_volband})"
            )
            logger.info(
                "[GEN] CORE_WEIGHTS | "
                f"trend={w.get('trend strength')} struct={w.get('structure validation')} volconf={w.get('volume confirmation')} "
                f"risk={w.get('risk state modifier')} conf={w.get('confidence score')} volReg={w.get('volatility regime')}"
            )
        except Exception as e:
            logger.warning(f"[GEN] CORE_THRESHOLDS_LOG_FAIL | err={e}")
        _THRESHOLDS_LOGGED = True

    return _CORE


def _pct(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return (a - b) / b * 100.0


def _sma(vals: List[float], n: int) -> float:
    if len(vals) < n:
        return sum(vals) / max(1, len(vals))
    w = vals[-n:]
    return sum(w) / n


def _atr_pct(ohlcv: List[List[float]], n: int = 14) -> float:
    if len(ohlcv) < n + 1:
        return 0.0
    trs: List[float] = []
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

    if atr_pct < (assumed_gross_edge * ATR_TO_TP_SANITY_FACTOR):
        return False, (
            f"ATR_BELOW_TP atr%={atr_pct:.2f} < "
            f"{ATR_TO_TP_SANITY_FACTOR:.2f}*TP_PCT={assumed_gross_edge*ATR_TO_TP_SANITY_FACTOR:.2f}"
        )

    return True, "OK"


def _trend_strength(last: float, ma20: float) -> float:
    gap_pct = _pct(last, ma20)
    x = (gap_pct / 1.0)
    return max(0.0, min(1.0, 0.5 + (x * 0.4)))


def _structure_ok_strict(closes: List[float]) -> bool:
    if len(closes) < 10:
        return False
    last = closes[-1]
    ma20 = _sma(closes, 20)
    prev = closes[-2]
    last5 = closes[-5:]
    last10 = closes[-10:]
    return (last > ma20) and (last > prev) and (sum(last5) / 5.0 > sum(last10) / 10.0)


def _structure_ok_soft(closes: List[float], last: float, ma20: float, trend_strength: float) -> Tuple[bool, str]:
    """
    Soft structure override:
    If strict structure fails but market is clearly trending and price is cleanly away from MA,
    allow structure_ok=True to avoid perma-standby.

    Returns: (ok, reason)
    """
    if not STRUCT_SOFT_OVERRIDE:
        return False, "disabled"

    ma_gap_abs = abs(_pct(last, ma20))
    if trend_strength < STRUCT_SOFT_MIN_TREND:
        return False, f"trend<{STRUCT_SOFT_MIN_TREND:.2f}"

    if ma_gap_abs < STRUCT_SOFT_MIN_MA_GAP:
        return False, f"ma_gap<{STRUCT_SOFT_MIN_MA_GAP:.2f}"

    n = max(1, STRUCT_SOFT_REQUIRE_LAST_UP)
    ok_up = True
    for i in range(1, n + 1):
        if len(closes) < (i + 1):
            ok_up = False
            break
        if closes[-i] <= closes[-i - 1]:
            ok_up = False
            break
    if not ok_up:
        return False, f"last_up<{n}"

    return True, f"SOFT_OK trend>={STRUCT_SOFT_MIN_TREND:.2f} ma_gap>={STRUCT_SOFT_MIN_MA_GAP:.2f} last_up={n}"


def _volume_score_raw(vols: List[float]) -> float:
    if len(vols) < 20:
        return 0.0
    v_last3 = sum(vols[-3:]) / 3.0
    v_avg20 = sum(vols[-20:]) / 20.0
    if v_avg20 <= 0:
        return 0.0
    ratio = v_last3 / v_avg20
    return max(0.0, min(1.0, ratio / 2.0))


def _volume_score(vols: List[float]) -> float:
    raw = _volume_score_raw(vols)
    scaled = raw * VOLUME_SCORE_SCALE
    floored = max(VOLUME_SCORE_FLOOR, scaled)
    return max(0.0, min(VOLUME_SCORE_CAP, floored))


def _confidence_components(closes: List[float], ohlcv: List[List[float]]) -> Tuple[float, int, int, int]:
    last = closes[-1]
    prev = closes[-2]
    ma20 = _sma(closes, 20)
    atrp = _atr_pct(ohlcv, 14)

    cond1 = 1 if last > ma20 else 0
    cond2 = 1 if last > prev else 0
    cond3 = 1 if atrp < 2.0 else 0
    score = (0.45 * cond1) + (0.35 * cond2) + (0.20 * cond3)
    return score, cond1, cond2, cond3


def _confidence_score(closes: List[float], ohlcv: List[List[float]]) -> float:
    score, _, _, _ = _confidence_components(closes, ohlcv)
    return score


def _risk_state(vol_regime: str, ai_score: float) -> str:
    if vol_regime == "EXTREME":
        return "KILL"
    if ai_score < 0.45:
        return "REDUCE"
    return "OK"


def _cooldown_ok() -> bool:
    global _last_emit_ts
    return (time.time() - _last_emit_ts) >= COOLDOWN_SECONDS


def _emit(signal: Dict[str, Any], outbox_path: str) -> None:
    global _last_emit_ts
    append_signal(signal, outbox_path)
    _last_emit_ts = time.time()


def _get_outbox_path() -> str:
    return os.getenv("OUTBOX_PATH") or os.getenv("SIGNAL_OUTBOX_PATH") or "/var/data/signal_outbox.json"


def _log_local_gates(symbol: str, *, active_oco: bool, cooldown_ok: bool, allow_live: bool,
                     ma_gap_abs: float, conf: float, edge_ok: bool, edge_reason: str) -> None:
    if not GEN_LOG_LOCAL_GATES:
        return
    logger.info(
        "[GEN] LOCAL_GATES | "
        f"symbol={symbol} cooldown_ok={cooldown_ok} active_oco={active_oco} allow_live={allow_live} "
        f"ma_gap%={ma_gap_abs:.3f}/{MA_GAP_PCT:.3f} conf={conf:.3f}/{BUY_CONFIDENCE_MIN:.3f} "
        f"edge_ok={edge_ok} edge_reason={edge_reason}"
    )


def _diagnostic_dump(symbol: str, *, last: float, prev: float, ma20: float, atrp: float,
                     trend: float, vol_score: float, vol_raw: float,
                     conf: float, c1: int, c2: int, c3: int,
                     struct_strict: bool, struct_soft_ok: bool, struct_soft_reason: str,
                     decision: Dict[str, Any], outbox_path: str) -> None:
    if not GEN_LOG_DIAGNOSTICS:
        return

    gap_pct = _pct(last, ma20)
    ma_gap_abs = abs(gap_pct)
    ok_edge, edge_reason = _edge_ok(atrp)

    # If we can infer thresholds from core, log them (best-effort)
    trend_th = None
    conf_th = None
    vol_th = None
    try:
        core = _core()
        th = getattr(core, "thresholds", {}) or {}
        trend_th = (th.get("trend strength", {}) or {}).get("num")
        conf_th = (th.get("confidence score", {}) or {}).get("num")
        vol_th = (th.get("volume confirmation", {}) or {}).get("num")
    except Exception:
        pass

    # Why not execute (human readable)
    active = decision.get("active_strategy")
    final = decision.get("final_trade_decision")
    ai = float(decision.get("ai_score", 0.0))
    macro = decision.get("macro_gate")
    reasons = decision.get("reasons") or {}

    blockers = []
    if reasons.get("trend_ok") is False:
        blockers.append("trend_ok=FALSE")
    if reasons.get("confidence_ok") is False:
        blockers.append("confidence_ok=FALSE")
    if reasons.get("volume_ok") is False:
        blockers.append("volume_ok=FALSE")
    if reasons.get("volband_ok") is False:
        blockers.append("volband_ok=FALSE")
    if reasons.get("risk_ok") is False:
        blockers.append("risk_ok=FALSE")
    if macro != "ALLOW":
        blockers.append(f"macro_gate={macro}")
    if active != "YES":
        blockers.append(f"active_strategy={active}")
    if final != "EXECUTE":
        blockers.append(f"final={final}")
    if ai <= 0.60:
        blockers.append(f"ai_score={ai:.3f}<=0.60")

    logger.info(
        "[GEN] DIAG | "
        f"symbol={symbol} last={last:.6f} prev={prev:.6f} ma20={ma20:.6f} gap%={gap_pct:.3f} ma_gap_abs={ma_gap_abs:.3f} "
        f"atr%={atrp:.2f} edge_ok={ok_edge} edge_reason={edge_reason} "
        f"trend={trend:.3f} trend_th={trend_th} "
        f"conf={conf:.3f} conf_th={conf_th} c1(last>ma20)={c1} c2(last>prev)={c2} c3(atr<2)={c3} "
        f"vol_raw={vol_raw:.3f} vol_score={vol_score:.3f} vol_th={vol_th} "
        f"struct_strict={struct_strict} struct_soft={struct_soft_ok} struct_soft_reason={struct_soft_reason} "
        f"ai={ai:.3f} macro={macro} active={active} final={final} blockers={blockers} outbox={outbox_path}"
    )


def generate_signal() -> Optional[Dict[str, Any]]:
    outbox_path = _get_outbox_path()

    cd_ok = _cooldown_ok()
    if not cd_ok:
        if GEN_LOG_EVERY_TICK and GEN_LOG_LOCAL_GATES:
            logger.info(f"[GEN] LOCAL_GATES | cooldown_ok=False (cooldown={COOLDOWN_SECONDS}s)")
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
            logger.exception(f"[GEN] FETCH_FAIL | symbol={symbol} tf={TIMEFRAME} err={e}")
            continue

        if not ohlcv or len(ohlcv) < 30:
            if GEN_LOG_EVERY_TICK:
                logger.info(f"[GEN] NO_SIGNAL | symbol={symbol} reason=not_enough_candles got={len(ohlcv) if ohlcv else 0} need>=30")
            continue

        closes = [float(c[4]) for c in ohlcv]
        vols = [float(c[5]) for c in ohlcv]
        last = closes[-1]
        prev = closes[-2]
        ma20 = _sma(closes, 20)
        atrp = _atr_pct(ohlcv, 14)
        vol_reg = _vol_regime(atrp)

        trend = _trend_strength(last, ma20)

        # strict structure first
        struct_strict = _structure_ok_strict(closes)
        struct_ok = struct_strict

        # SOFT structure override if strict fails
        struct_soft_ok = False
        struct_soft_reason = ""
        if not struct_ok:
            struct_soft_ok, struct_soft_reason = _structure_ok_soft(closes, last, ma20, trend)
            if struct_soft_ok:
                struct_ok = True
                if GEN_DEBUG:
                    logger.info(f"[GEN] STRUCT_OVERRIDE | symbol={symbol} applied=True reason={struct_soft_reason}")

        vol_raw = _volume_score_raw(vols)
        vol_score = _volume_score(vols)

        conf, c1, c2, c3 = _confidence_components(closes, ohlcv)

        # First pass AI score with risk_state="OK" (like your original)
        tmp_inp = CoreInputs(
            trend_strength=trend,
            structure_ok=struct_ok,
            volume_score=vol_score,
            risk_state="OK",
            confidence_score=conf,
            volatility_regime=vol_reg,
        )
        tmp_dec = core.decide(tmp_inp)
        ai_score_pre = float(tmp_dec.get("ai_score", 0.0))

        risk = _risk_state(vol_reg, ai_score_pre)

        inp = CoreInputs(
            trend_strength=trend,
            structure_ok=struct_ok,
            volume_score=vol_score,
            risk_state=risk,
            confidence_score=conf,
            volatility_regime=vol_reg,
        )
        decision = core.decide(inp)

        if GEN_DEBUG:
            logger.info(
                f"[GEN] CORE_DECISION | symbol={symbol} "
                f"ai={decision['ai_score']:.3f} macro={decision['macro_gate']} strat={decision['active_strategy']} "
                f"final={decision['final_trade_decision']} risk={risk} volReg={vol_reg} atr%={atrp:.2f} "
                f"last={last:.6f} ma20={ma20:.6f} outbox={outbox_path}"
            )

        if GEN_DEBUG and GEN_LOG_REASONS:
            try:
                # Also include structure_ok for clarity
                rs = decision.get("reasons") or {}
                rs = dict(rs)
                rs["structure_ok"] = bool(struct_ok)
                rs["structure_strict_ok"] = bool(struct_strict)
                rs["structure_soft_ok"] = bool(struct_soft_ok)
                rs["structure_soft_reason"] = struct_soft_reason
                logger.info(f"[GEN] STRAT_REASONS | symbol={symbol} reasons={rs}")
            except Exception as e:
                logger.warning(f"[GEN] STRAT_REASONS_FAIL | symbol={symbol} err={e}")

        # Deep diag line (why blocked)
        _diagnostic_dump(
            symbol,
            last=last, prev=prev, ma20=ma20, atrp=atrp,
            trend=trend, vol_score=vol_score, vol_raw=vol_raw,
            conf=conf, c1=c1, c2=c2, c3=c3,
            struct_strict=struct_strict, struct_soft_ok=struct_soft_ok, struct_soft_reason=struct_soft_reason,
            decision=decision, outbox_path=outbox_path
        )

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
                }
            }
            _emit(sig, outbox_path)
            return sig

        if active_oco and BLOCK_SIGNALS_WHEN_ACTIVE_OCO:
            ma_gap_abs = abs(_pct(last, ma20))
            ok_edge, edge_reason = _edge_ok(atrp)
            _log_local_gates(symbol, active_oco=active_oco, cooldown_ok=True, allow_live=ALLOW_LIVE_SIGNALS,
                             ma_gap_abs=ma_gap_abs, conf=conf, edge_ok=ok_edge, edge_reason=edge_reason)
            continue

        if decision["final_trade_decision"] != "EXECUTE":
            ma_gap_abs = abs(_pct(last, ma20))
            ok_edge, edge_reason = _edge_ok(atrp)
            _log_local_gates(symbol, active_oco=active_oco, cooldown_ok=True, allow_live=ALLOW_LIVE_SIGNALS,
                             ma_gap_abs=ma_gap_abs, conf=conf, edge_ok=ok_edge, edge_reason=edge_reason)
            continue

        ma_gap_abs = abs(_pct(last, ma20))
        if ma_gap_abs < MA_GAP_PCT:
            if GEN_DEBUG:
                logger.info(f"[GEN] BLOCKED_BY_MA_GAP | symbol={symbol} gap%={ma_gap_abs:.3f} < MA_GAP_PCT={MA_GAP_PCT:.3f}")
            ok_edge, edge_reason = _edge_ok(atrp)
            _log_local_gates(symbol, active_oco=active_oco, cooldown_ok=True, allow_live=ALLOW_LIVE_SIGNALS,
                             ma_gap_abs=ma_gap_abs, conf=conf, edge_ok=ok_edge, edge_reason=edge_reason)
            continue

        if conf < BUY_CONFIDENCE_MIN:
            if GEN_DEBUG:
                logger.info(f"[GEN] BLOCKED_BY_CONF | symbol={symbol} conf={conf:.3f} < BUY_CONFIDENCE_MIN={BUY_CONFIDENCE_MIN:.3f}")
            ok_edge, edge_reason = _edge_ok(atrp)
            _log_local_gates(symbol, active_oco=active_oco, cooldown_ok=True, allow_live=ALLOW_LIVE_SIGNALS,
                             ma_gap_abs=ma_gap_abs, conf=conf, edge_ok=ok_edge, edge_reason=edge_reason)
            continue

        ok_edge, edge_reason = _edge_ok(atrp)
        if not ok_edge:
            if GEN_DEBUG:
                logger.info(f"[GEN] BLOCKED_BY_EDGE | symbol={symbol} reason={edge_reason}")
            _log_local_gates(symbol, active_oco=active_oco, cooldown_ok=True, allow_live=ALLOW_LIVE_SIGNALS,
                             ma_gap_abs=ma_gap_abs, conf=conf, edge_ok=ok_edge, edge_reason=edge_reason)
            continue

        if not ALLOW_LIVE_SIGNALS:
            if GEN_DEBUG:
                logger.info(f"[GEN] BLOCKED_BY_ENV | symbol={symbol} reason=ALLOW_LIVE_SIGNALS=false")
            _log_local_gates(symbol, active_oco=active_oco, cooldown_ok=True, allow_live=ALLOW_LIVE_SIGNALS,
                             ma_gap_abs=ma_gap_abs, conf=conf, edge_ok=ok_edge, edge_reason=edge_reason)
            continue

        signal_id = str(uuid.uuid4())
        sig = {
            "signal_id": signal_id,
            "ts_utc": _now_utc_iso(),
            "certified_signal": True,
            "final_verdict": "TRADE",
            "meta": {"source": "DYZEN_EXCEL_LIVE_CORE", "symbol": symbol, "decision": decision},
            "execution": {
                "symbol": symbol,
                "direction": "LONG",
                "entry": {"type": "MARKET"},
                "quote_amount": BOT_QUOTE_PER_TRADE,
            }
        }
        _emit(sig, outbox_path)
        return sig

    return None


def run_once(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return generate_signal()
