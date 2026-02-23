from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from .excel_bridge import ExcelBridge
from .indicators import Indicators
from .logger import log


@dataclass
class Signal:
    symbol: str
    decision: str  # BUY/SELL/NO
    confidence: float
    reason: str
    meta: Dict[str, Any]


class SignalEngine:
    def __init__(self, excel: ExcelBridge, logger, trend_gate: float = 0.15, min_conf: float = 0.50, min_atr_pct: float = 0.20):
        self.excel = excel
        self.logger = logger
        self.trend_gate = trend_gate
        self.min_conf = min_conf
        self.min_atr_pct = min_atr_pct

    def generate(self, symbol: str, timeframe: str, ind: Indicators) -> Signal:
        atr_ok = ind.atr_pct >= self.min_atr_pct
        trend_ok = abs(ind.trend_score) >= self.trend_gate
        vol_ok = ind.vol_score >= 0.6

        excel_dec = self.excel.evaluate(
            symbol=symbol,
            timeframe=timeframe,
            last=ind.last,
            atr_pct=ind.atr_pct,
            trend_score=ind.trend_score,
            vol_score=ind.vol_score,
        )

        decision = excel_dec.decision
        conf = excel_dec.confidence

        if not (atr_ok and trend_ok and vol_ok):
            decision = "NO"
        if conf < self.min_conf:
            decision = "NO"

        reason = "OK"
        if not atr_ok:
            reason = "ATR_TOO_LOW"
        elif not trend_ok:
            reason = "TREND_WEAK"
        elif not vol_ok:
            reason = "VOL_LOW"
        elif excel_dec.decision == "NO":
            reason = "EXCEL_NO"
        elif conf < self.min_conf:
            reason = "CONF_TOO_LOW"

        meta = {
            "atr_pct": ind.atr_pct,
            "trend": ind.trend_score,
            "vol_score": ind.vol_score,
            "last": ind.last,
            "prev": ind.prev,
            "excel_decision": excel_dec.decision,
        }

        log(self.logger, "INFO", "CORE_DECISION", symbol=symbol, tf=timeframe, ai=conf, decision=decision, reason=reason, **meta)
        return Signal(symbol=symbol, decision=decision, confidence=conf, reason=reason, meta=meta)
