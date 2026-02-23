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
# ==============================
# EXCEL BRIDGE READER
# ==============================
def _read_excel_bridge(core: ExcelLiveCore) -> dict:
    """
    Read values from Excel PYTHON_BRIDGE.
    Safe: returns {} on any failure.
    """
    try:
        wb = getattr(core, "workbook", None)
        if wb is None:
            return {}

        if "PYTHON_BRIDGE" not in wb.sheetnames:
            return {}

        ws = wb["PYTHON_BRIDGE"]

        out = {}
        for r in range(2, ws.max_row + 1):
            key = ws.cell(r, 1).value
            val = ws.cell(r, 2).value
            if key:
                out[str(key)] = val
        return out
    except Exception:
        return {}

# -----------------------------
# ENV
# -----------------------------
TIMEFRAME = os.getenv("BOT_TIMEFRAME", "15m").strip()
CANDLE_LIMIT = int(os.getenv("BOT_CANDLE_LIMIT", "80"))
COOLDOWN_SECONDS = int(os.getenv("BOT_SIGNAL_COOLDOWN_SECONDS", "180"))

ALLOW_LIVE_SIGNALS = os.getenv("ALLOW_LIVE_SIGNALS", "false").strip().lower() == "true"

BOT_QUOTE_PER_TRADE = float(os.getenv("BOT_QUOTE_PER_TRADE", "15"))

# Fee-aware edge gate
MIN_MOVE_PCT = float(os.getenv("MIN_MOVE_PCT", "0.60"))
ESTIMATED_ROUNDTRIP_FEE_PCT = float(os.getenv("ESTIMATED_ROUNDTRIP_FEE_PCT", "0.20"))
ESTIMATED_SLIPPAGE_PCT = float(os.getenv("ESTIMATED_SLIPPAGE_PCT", "0.15"))
TP_PCT = float(os.getenv("TP_PCT", "1.3"))
MIN_NET_PROFIT_PCT = float(os.getenv("MIN_NET_PROFIT_PCT", "0.60"))

# ✅ NEW: ATR sanity factor (replaces hardcoded 0.75)
# Example: TP_PCT=1.0 and factor=0.20 means require atr% >= 0.20 (plus MIN_MOVE_PCT check)
ATR_TO_TP_SANITY_FACTOR = float(os.getenv("ATR_TO_TP_SANITY_FACTOR", "0.20"))

# Optional MA filters (FULL OFF switch)
USE_MA_FILTERS = os.getenv("USE_MA_FILTERS", "true").strip().lower() == "true"
MA_GAP_PCT = float(os.getenv("MA_GAP_PCT", "0.15"))  # used only if USE_MA_FILTERS=true

# Extra confidence guard (on top of Excel)
BUY_CONFIDENCE_MIN = float(os.getenv("BUY_CONFIDENCE_MIN", "0.70"))

BLOCK_SIGNALS_WHEN_ACTIVE_OCO = os.getenv("BLOCK_SIGNALS_WHEN_ACTIVE_OCO", "true").strip().lower() == "true"

GEN_DEBUG = os.getenv("GEN_DEBUG", "true").strip().lower() == "true"
GEN_LOG_EVERY_TICK = os.getenv("GEN_LOG_EVERY_TICK", "true").strip().lower() == "true"

# Excel model path
EXCEL_MODEL_PATH = os.getenv("EXCEL_MODEL_PATH", "/var/data/DYZEN_CAPITAL_OS_AI_LIVE_CORE_READY.xlsx").strip()
if EXCEL_MODEL_PATH.lower().startswith("excel_model_path="):
    EXCEL_MODEL_PATH = EXCEL_MODEL_PATH.split("=", 1)[1].strip()

_last_emit_ts: float = 0.0

# -----------------------------
# HELPERS
# -----------------------------
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
        # safe default: assume active OCO to prevent uncontrolled trading
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


_CORE: Optional[ExcelLiveCore] = None


def _core() -> ExcelLiveCore:
    global _CORE
    if _CORE is None:
        resolved = _resolve_excel_path(EXCEL_MODEL_PATH)
        logger.info(
            f"[GEN] EXCEL_PATH | env={EXCEL_MODEL_PATH} resolved={resolved} exists_env={os.path.exists(EXCEL_MODEL_PATH)}"
        )
        _CORE = ExcelLiveCore(resolved)
        logger.info(f"[GEN] EXCEL_CORE_LOADED | path={resolved}")
    return _CORE


def _pct(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return (a - b) / b * 100.0


def _sma(vals: List[float], n: int) -> float:
    if not vals:
        return 0.0
    if len(vals) < n:
        return sum(vals) / len(vals)
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
    """
    Fee-aware edge gate.

    Requirements:
      1) atr% >= MIN_MOVE_PCT
      2) TP_PCT must cover (fees + slippage) + MIN_NET_PROFIT_PCT
      3) atr% should be at least TP_PCT * ATR_TO_TP_SANITY_FACTOR
         (previously hardcoded to 0.75, now env-controlled)
    """
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

    min_atr_for_tp = assumed_gross_edge * ATR_TO_TP_SANITY_FACTOR
    if atr_pct < min_atr_for_tp:
        return False, (
            f"ATR_BELOW_TP atr%={atr_pct:.2f} < TP_PCT*ATR_TO_TP_SANITY_FACTOR={min_atr_for_tp:.2f} "
            f"(TP_PCT={assumed_gross_edge:.2f} factor={ATR_TO_TP_SANITY_FACTOR:.2f})"
        )

    return True, "OK"


def _cooldown_ok() -> bool:
    global _last_emit_ts
    return (time.time() - _last_emit_ts) >= COOLDOWN_SECONDS


def _emit(signal: Dict[str, Any], outbox_path: str) -> None:
    global _last_emit_ts
    append_signal(signal, outbox_path)
    _last_emit_ts = time.time()


def _get_outbox_path() -> str:
    return os.getenv("OUTBOX_PATH") or os.getenv("SIGNAL_OUTBOX_PATH") or "/var/data/signal_outbox.json"


def _tf_seconds(tf: str) -> int:
    tf = (tf or "").strip().lower()
    try:
        if tf.endswith("m"):
            return max(1, int(tf[:-1])) * 60
        if tf.endswith("h"):
            return max(1, int(tf[:-1])) * 3600
        if tf.endswith("d"):
            return max(1, int(tf[:-1])) * 86400
    except Exception:
        pass
    # fallback 15m
    return 900


def _drop_unclosed_candle(ohlcv: List[List[float]], timeframe: str) -> Tuple[List[List[float]], bool]:
    if not ohlcv:
        return ohlcv, False
    last_ts_ms = int(ohlcv[-1][0])
    now_ms = int(time.time() * 1000)
    tf_ms = _tf_seconds(timeframe) * 1000
    # if last candle start is too recent, it's likely still forming
    if now_ms - last_ts_ms < tf_ms:
        return ohlcv[:-1], True
    return ohlcv, False


# -----------------------------
# EXCHANGE BUILDER
# -----------------------------
def _build_exchange() -> ccxt.Exchange:
    ex_name = os.getenv("EXCHANGE", "binance").strip().lower()
    market_type = os.getenv("MARKET_TYPE", "spot").strip().lower()

    if ex_name == "bybit":
        api_key = os.getenv("BYBIT_API_KEY", "").strip()
        api_secret = os.getenv("BYBIT_API_SECRET", "").strip()
        return ccxt.bybit({
            "enableRateLimit": True,
            "apiKey": api_key,
            "secret": api_secret,
            "options": {"defaultType": market_type},  # "spot" / "swap"
        })

    api_key = os.getenv("BINANCE_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
    return ccxt.binance({
        "enableRateLimit": True,
        "apiKey": api_key,
        "secret": api_secret,
        "options": {"defaultType": market_type},
    })


EXCHANGE = _build_exchange()


# -----------------------------
# FEATURE CALCS (NO-MA READY)
# -----------------------------
def _momentum(closes: List[float], n: int) -> float:
    if len(closes) < n + 1:
        return 0.0
    base = closes[-1 - n]
    if base == 0:
        return 0.0
    return (closes[-1] / base) - 1.0


def _slope_sma(closes: List[float]) -> float:
    # normalized slope: SMA5 vs SMA10
    if len(closes) < 10:
        return 0.0
    s5 = _sma(closes, 5)
    s10 = _sma(closes, 10)
    if s10 == 0:
        return 0.0
    return (s5 / s10) - 1.0


def _ups_count(closes: List[float], n: int) -> int:
    if len(closes) < n + 1:
        return 0
    ups = 0
    for i in range(-n, 0):
        if closes[i] > closes[i - 1]:
            ups += 1
    return ups


def _trend_strength(closes: List[float], use_ma: bool) -> float:
    """
    Returns 0..1
    If USE_MA_FILTERS: include last vs MA20 separation
    Else: rely on slope + momentum
    """
    if len(closes) < 20:
        return 0.0

    last = closes[-1]
    prev = closes[-2]
    mom1 = _momentum(closes, 1)        # ~0.001 == 0.1%
    slope = _slope_sma(closes)         # ~0.001 == +0.1%
    ups3 = _ups_count(closes, 3)

    base = 0.0
    base += 0.35 * (1.0 if last > prev else 0.0)
    base += 0.25 * max(0.0, min(1.0, (mom1 / 0.003)))     # 0.3% -> 1.0
    base += 0.20 * max(0.0, min(1.0, (slope / 0.003)))    # 0.3% -> 1.0
    base += 0.20 * (ups3 / 3.0)

    if use_ma:
        ma20 = _sma(closes, 20)
        gap_pct = _pct(last, ma20)  # %
        base += 0.15 * max(0.0, min(1.0, gap_pct / 0.6))   # 0.6% above MA -> +0.15

    return max(0.0, min(1.0, base))


def _structure_ok(closes: List[float], use_ma: bool) -> Tuple[bool, str]:
    """
    If MA disabled, require:
      - SMA5 > SMA10
      - at least 2 up candles in last 3
      - mom10 not strongly negative
      - last > prev
    """
    if len(closes) < 20:
        return False, "len<20"

    last = closes[-1]
    prev = closes[-2]
    s5 = _sma(closes, 5)
    s10 = _sma(closes, 10)
    ups3 = _ups_count(closes, 3)
    mom10 = _momentum(closes, 10)

    c_last_prev = last > prev
    c_sma = s5 > s10
    c_ups = ups3 >= 2
    c_mom10 = mom10 > -0.002  # not worse than -0.2% over 10 candles

    if use_ma:
        ma20 = _sma(closes, 20)
        c_ma = last > ma20
        ok = c_last_prev and c_sma and c_ups and c_ma and c_mom10
        reason = (
            f"last>prev={int(c_last_prev)} sma5>sma10={int(c_sma)} ups3>=2={int(c_ups)} "
            f"last>ma20={int(c_ma)} mom10_ok={int(c_mom10)}"
        )
        return ok, reason

    ok = c_last_prev and c_sma and c_ups and c_mom10
    reason = f"last>prev={int(c_last_prev)} sma5>sma10={int(c_sma)} ups3>=2={int(c_ups)} mom10_ok={int(c_mom10)}"
    return ok, reason


def _volume_score(vols: List[float]) -> Tuple[float, float]:
    """
    Return (vol_score 0..1, vRatio)
    score ~= vRatio clamped 0..1 (so 0.80 stays 0.80 and can pass vol_th=0.46)
    """
    if len(vols) < 20:
        return 0.0, 0.0
    v_last = vols[-1]
    v_avg = sum(vols[-20:]) / 20.0
    if v_avg <= 0:
        return 0.0, 0.0
    v_ratio = v_last / v_avg  # 1.0 normal
    score = max(0.0, min(1.0, v_ratio))
    return score, v_ratio


def _confidence_score(closes: List[float], ohlcv: List[List[float]], use_ma: bool) -> float:
    """
    Returns 0..1
    If MA disabled: confidence mainly from last>prev + slope + non-extreme ATR
    If MA enabled: include last>ma20 condition too
    """
    if len(closes) < 20 or len(ohlcv) < 20:
        return 0.0

    last = closes[-1]
    prev = closes[-2]
    atrp = _atr_pct(ohlcv, 14)
    slope = _slope_sma(closes)

    cond_last_prev = 1.0 if last > prev else 0.0
    cond_atr = 1.0 if atrp < 2.0 else 0.0
    cond_slope = max(0.0, min(1.0, slope / 0.003))  # 0.3% -> 1.0

    if use_ma:
        ma20 = _sma(closes, 20)
        cond_ma = 1.0 if last > ma20 else 0.0
        return (0.35 * cond_ma) + (0.35 * cond_last_prev) + (0.20 * cond_slope) + (0.10 * cond_atr)

    return (0.45 * cond_last_prev) + (0.35 * cond_slope) + (0.20 * cond_atr)


def _risk_state(vol_regime: str, ai_score: float) -> str:
    if vol_regime == "EXTREME":
        return "KILL"
    if ai_score < 0.45:
        return "REDUCE"
    return "OK"


def generate_signal() -> Optional[Dict[str, Any]]:
    outbox_path = _get_outbox_path()

    if not _cooldown_ok():
        return None

    core = _core()

    for symbol in SYMBOLS:
        active_oco = _has_active_oco(symbol)

        try:
            ohlcv = EXCHANGE.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)
        except Exception as e:
            logger.exception(f"[GEN] FETCH_FAIL | symbol={symbol} tf={TIMEFRAME} err={e}")
            continue

        if not ohlcv or len(ohlcv) < 30:
            if GEN_LOG_EVERY_TICK:
                logger.info(
                    f"[GEN] NO_SIGNAL | symbol={symbol} reason=not_enough_candles got={len(ohlcv) if ohlcv else 0} need>=30"
                )
            continue

        ohlcv, dropped = _drop_unclosed_candle(ohlcv, TIMEFRAME)
        if len(ohlcv) < 30:
            continue

        closes = [float(c[4]) for c in ohlcv]
        vols = [float(c[5]) for c in ohlcv]

        last = closes[-1]
        prev = closes[-2]
        atrp = _atr_pct(ohlcv, 14)
        vol_reg = _vol_regime(atrp)

        trend = _trend_strength(closes, USE_MA_FILTERS)
        struct_ok, struct_reason = _structure_ok(closes, USE_MA_FILTERS)
        vol_score, v_ratio = _volume_score(vols)
        conf = _confidence_score(closes, ohlcv, USE_MA_FILTERS)

        # first pass
        tmp_inp = CoreInputs(
            trend_strength=trend,
            structure_ok=struct_ok,
            volume_score=vol_score,
            risk_state="OK",
            confidence_score=conf,
            volatility_regime=vol_reg,
        )
        tmp_dec = core.decide(tmp_inp)
        ai_score = float(tmp_dec["ai_score"])
        risk = _risk_state(vol_reg, ai_score)

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
                f"[GEN] CORE_DECISION | symbol={symbol} ai={decision['ai_score']:.3f} macro={decision['macro_gate']} "
                f"strat={decision['active_strategy']} final={decision['final_trade_decision']} risk={risk} "
                f"volReg={vol_reg} atr%={atrp:.2f} last={last:.6f} prev={prev:.6f} "
                f"dropped_last_candle={dropped} outbox={outbox_path}"
            )

            mom1 = _momentum(closes, 1)
            mom10 = _momentum(closes, 10)
            slope = _slope_sma(closes)
            ups3 = _ups_count(closes, 3)
            v5 = sum(vols[-5:]) / 5.0 if len(vols) >= 5 else 0.0
            v20 = sum(vols[-20:]) / 20.0 if len(vols) >= 20 else 0.0

            if USE_MA_FILTERS:
                s5 = _sma(closes, 5)
                s10 = _sma(closes, 10)
                ma20 = _sma(closes, 20)
                ma_gap_abs = abs(_pct(last, ma20))
                logger.info(
                    f"[GEN] DIAG | symbol={symbol} trend={trend:.3f} conf={conf:.3f} struct={struct_ok} "
                    f"vol_score={vol_score:.3f} struct_reason={struct_reason} "
                    f"mom1={mom1:.6f} mom10={mom10:.6f} slope={slope:.6f} ups3={ups3} "
                    f"sma5={s5:.6f} sma10={s10:.6f} ma_gap%={ma_gap_abs:.3f} "
                    f"v5={v5:.3f} v20={v20:.3f} vRatio={v_ratio:.3f} use_ma={USE_MA_FILTERS}"
                )
            else:
                logger.info(
                    f"[GEN] DIAG | symbol={symbol} trend={trend:.3f} conf={conf:.3f} struct={struct_ok} "
                    f"vol_score={vol_score:.3f} struct_reason={struct_reason} "
                    f"mom1={mom1:.6f} mom10={mom10:.6f} slope={slope:.6f} ups3={ups3} "
                    f"v5={v5:.3f} v20={v20:.3f} vRatio={v_ratio:.3f} use_ma={USE_MA_FILTERS}"
                )

# Protective SELL if active OCO and risk is KILL
if active_oco and risk and str(risk).upper() == "KILL":
    signal_id = str(uuid.uuid4())
    logger.info(f"[SIG] symbol={symbol}")

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

        if active_oco and BLOCK_SIGNALS_WHEN_ACTIVE_OCO:
            continue

        if decision["final_trade_decision"] != "EXECUTE":
            continue

        # -----------------------------
        # EXTRA LIVE GUARDS
        # -----------------------------
        if USE_MA_FILTERS:
            ma20 = _sma(closes, 20)
            ma_gap_abs = abs(_pct(last, ma20))
            if ma_gap_abs < MA_GAP_PCT:
                if GEN_DEBUG:
                    logger.info(
                        f"[GEN] BLOCKED_BY_MA_GAP | symbol={symbol} gap%={ma_gap_abs:.3f} < MA_GAP_PCT={MA_GAP_PCT:.3f}"
                    )
                continue

        if conf < BUY_CONFIDENCE_MIN:
            if GEN_DEBUG:
                logger.info(
                    f"[GEN] BLOCKED_BY_CONF | symbol={symbol} conf={conf:.3f} < BUY_CONFIDENCE_MIN={BUY_CONFIDENCE_MIN:.3f}"
                )
            continue

        ok_edge, edge_reason = _edge_ok(atrp)
        if not ok_edge:
            if GEN_DEBUG:
                logger.info(f"[GEN] BLOCKED_BY_EDGE | symbol={symbol} reason={edge_reason}")
            continue

        if not ALLOW_LIVE_SIGNALS:
            if GEN_DEBUG:
                logger.info(f"[GEN] BLOCKED_BY_ENV | symbol={symbol} reason=ALLOW_LIVE_SIGNALS=false")
            continue

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
                "quote_amount": BOT_QUOTE_PER_TRADE,
            }
        }

        _emit(sig, outbox_path)
        return sig

    return None


def run_once(*args, **kwargs) -> Optional[Dict[str, Any]]:
    return generate_signal()




