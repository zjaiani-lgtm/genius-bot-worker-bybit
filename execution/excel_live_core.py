# execution/excel_live_core.py
from __future__ import annotations

import os
import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from openpyxl import load_workbook


def _to_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if s == "":
            return default
        # allow "+0.005"
        try:
            return float(s)
        except Exception:
            return default
    return default


def _to_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return int(v) != 0
    if isinstance(v, str):
        s = v.strip().lower()
        return s in ("1", "true", "yes", "y", "on")
    return default


def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    if b == 0:
        return default
    return a / b


@dataclass
class LiveCoreConfig:
    # base confidence buy threshold (from Excel CONFIG_CORE)
    ai_conf_buy_min: float = 0.545

    # adaptive confidence deltas by volatility regime
    adaptive_conf_enabled: bool = True
    adaptive_conf_high_vol_delta: float = 0.005
    adaptive_conf_low_vol_delta: float = -0.005

    # aggression scaler for size multiplier (from Excel CONFIG_CORE or safe defaults)
    min_vol_for_aggression: float = 1.50
    aggression_size_boost: float = 2.0

    # optional: sell threshold if needed later
    ai_conf_sell_min: float = 0.54


@dataclass
class LiveDecisionRow:
    symbol: str
    timeframe: str

    # score/confidence from Excel decision engine
    ai_score: float
    final_trade_decision: str  # EXECUTE / STAND_BY (Excel-driven)

    # patched Excel additions
    adaptive_buy_gate: float
    adaptive_size_mult: float

    # extra context for transparency
    volatility_ratio: float
    volatility_regime: str  # LOW / NORMAL / HIGH


class ExcelLiveCore:
    """
    Reads a specific Excel workbook (path from EXCEL_MODEL_PATH) and returns a LiveDecisionRow.
    """

    def __init__(self, excel_path: Optional[str] = None):
        self.excel_path = excel_path or os.getenv("EXCEL_MODEL_PATH", "").strip()
        if not self.excel_path:
            raise ValueError("EXCEL_MODEL_PATH is required")

    # ---------------------------
    # Workbook / Sheet helpers
    # ---------------------------

    def _load(self):
        # data_only=True reads cached formula values IF present; but we cannot rely on it.
        # We still use it to read raw inputs; then compute the patched logic ourselves.
        wb = load_workbook(self.excel_path, data_only=True, read_only=True)
        return wb

    def _sheet(self, wb, name: str):
        if name not in wb.sheetnames:
            raise KeyError(f"Sheet not found: {name}. Available: {wb.sheetnames}")
        return wb[name]

    def _get_cell(self, ws, cell: str) -> Any:
        return ws[cell].value

    # ---------------------------
    # CONFIG_CORE mapping
    # ---------------------------

    def _read_config_core(self, wb) -> LiveCoreConfig:
        """
        CONFIG_CORE expected cells:
        - AI_CONFIDENCE_BUY_MIN:    B2
        - AI_CONFIDENCE_SELL_MIN:   B3
        - ADAPTIVE_CONFIDENCE_ENABLED: B4
        - ADAPTIVE_CONF_HIGH_VOL_DELTA: B5
        - ADAPTIVE_CONF_LOW_VOL_DELTA:  B6
        - MIN_VOL_FOR_AGGRESSION:   B7
        - AGGRESSION_SIZE_BOOST:    B8

        If your Excel uses different cells, change here ONCE.
        """
        ws = self._sheet(wb, "CONFIG_CORE")

        cfg = LiveCoreConfig()
        cfg.ai_conf_buy_min = _to_float(self._get_cell(ws, "B2"), cfg.ai_conf_buy_min)
        cfg.ai_conf_sell_min = _to_float(self._get_cell(ws, "B3"), cfg.ai_conf_sell_min)

        cfg.adaptive_conf_enabled = _to_bool(self._get_cell(ws, "B4"), cfg.adaptive_conf_enabled)
        cfg.adaptive_conf_high_vol_delta = _to_float(self._get_cell(ws, "B5"), cfg.adaptive_conf_high_vol_delta)
        cfg.adaptive_conf_low_vol_delta = _to_float(self._get_cell(ws, "B6"), cfg.adaptive_conf_low_vol_delta)

        cfg.min_vol_for_aggression = _to_float(self._get_cell(ws, "B7"), cfg.min_vol_for_aggression)
        cfg.aggression_size_boost = _to_float(self._get_cell(ws, "B8"), cfg.aggression_size_boost)

        # safety clamps
        cfg.ai_conf_buy_min = float(max(0.0, min(cfg.ai_conf_buy_min, 1.0)))
        cfg.ai_conf_sell_min = float(max(0.0, min(cfg.ai_conf_sell_min, 1.0)))

        cfg.adaptive_conf_high_vol_delta = float(max(-0.20, min(cfg.adaptive_conf_high_vol_delta, 0.20)))
        cfg.adaptive_conf_low_vol_delta = float(max(-0.20, min(cfg.adaptive_conf_low_vol_delta, 0.20)))

        cfg.min_vol_for_aggression = float(max(0.50, min(cfg.min_vol_for_aggression, 10.0)))
        cfg.aggression_size_boost = float(max(1.0, min(cfg.aggression_size_boost, 10.0)))

        return cfg

    # ---------------------------
    # Volatility regime (Excel-like proxy)
    # ---------------------------

    def _compute_volatility_regime(self, short_vol: float, long_vol: float) -> Tuple[float, str]:
        """
        Compute volatility_ratio and classify:
        - HIGH if ratio >= 1.50
        - LOW if ratio <= 0.70
        - else NORMAL
        """
        ratio = _safe_div(short_vol, long_vol, default=1.0)
        # clamp to reasonable numeric space
        if not math.isfinite(ratio):
            ratio = 1.0

        regime = "NORMAL"
        if ratio >= 1.50:
            regime = "HIGH"
        elif ratio <= 0.70:
            regime = "LOW"
        return ratio, regime

    # ---------------------------
    # Patched Excel logic
    # ---------------------------

    def _adaptive_buy_gate(self, cfg: LiveCoreConfig, vol_regime: str) -> float:
        """
        E column in patched Excel:
        Adaptive Buy Gate = base_buy_min + delta_by_regime (if enabled)
        """
        gate = cfg.ai_conf_buy_min
        if cfg.adaptive_conf_enabled:
            if vol_regime == "HIGH":
                gate += cfg.adaptive_conf_high_vol_delta
            elif vol_regime == "LOW":
                gate += cfg.adaptive_conf_low_vol_delta
        # clamp
        gate = float(max(0.0, min(gate, 1.0)))
        return gate

    def _adaptive_size_mult(self, cfg: LiveCoreConfig, volatility_ratio: float) -> float:
        """
        F column in patched Excel:
        Adaptive Size Mult = IF(volatility_ratio >= MIN_VOL_FOR_AGGRESSION, AGGRESSION_SIZE_BOOST, 1)
        """
        mult = 1.0
        if volatility_ratio >= cfg.min_vol_for_aggression:
            mult = cfg.aggression_size_boost
        # clamp
        mult = float(max(1.0, min(mult, 10.0)))
        return mult

    # ---------------------------
    # AI_MASTER_LIVE_DECISION mapping
    # ---------------------------

    def _read_live_decision_inputs(self, wb) -> Dict[str, Any]:
        """
        AI_MASTER_LIVE_DECISION expected inputs:
        - Symbol:            B2
        - Timeframe:         C2
        - AIScore:           D2
        - ShortVol:          H2
        - LongVol:           I2

        (We do NOT rely on E2/F2 computed in Excel because formulas may not be cached.)

        If your sheet uses different cells, adjust here.
        """
        ws = self._sheet(wb, "AI_MASTER_LIVE_DECISION")
        data = {
            "symbol": self._get_cell(ws, "B2"),
            "timeframe": self._get_cell(ws, "C2"),
            "ai_score": self._get_cell(ws, "D2"),
            "short_vol": self._get_cell(ws, "H2"),
            "long_vol": self._get_cell(ws, "I2"),
        }
        return data

    # ---------------------------
    # Public API
    # ---------------------------

    def read_live_decision(self) -> LiveDecisionRow:
        wb = self._load()
        try:
            cfg = self._read_config_core(wb)
            inputs = self._read_live_decision_inputs(wb)

            symbol = str(inputs.get("symbol") or "").strip()
            tf = str(inputs.get("timeframe") or "").strip()

            ai_score = _to_float(inputs.get("ai_score"), 0.0)
            short_vol = _to_float(inputs.get("short_vol"), 0.0)
            long_vol = _to_float(inputs.get("long_vol"), 0.0)

            volatility_ratio, vol_regime = self._compute_volatility_regime(short_vol, long_vol)
            adaptive_gate = self._adaptive_buy_gate(cfg, vol_regime)
            size_mult = self._adaptive_size_mult(cfg, volatility_ratio)

            # Final Decision (patched Excel): EXECUTE if ai_score >= adaptive_gate else STAND_BY
            final_decision = "EXECUTE" if ai_score >= adaptive_gate else "STAND_BY"

            return LiveDecisionRow(
                symbol=symbol,
                timeframe=tf,
                ai_score=ai_score,
                final_trade_decision=final_decision,
                adaptive_buy_gate=adaptive_gate,
                adaptive_size_mult=size_mult,
                volatility_ratio=volatility_ratio,
                volatility_regime=vol_regime,
            )
        finally:
            wb.close()
