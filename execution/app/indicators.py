from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd


@dataclass
class Indicators:
    atr_pct: float
    ema_fast: float
    ema_slow: float
    trend_score: float
    vol_score: float
    last: float
    prev: float


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def compute_indicators(df: pd.DataFrame) -> Optional[Indicators]:
    if df is None or df.empty or len(df) < 30:
        return None

    close = df["close"]
    ema_fast = _ema(close, 20)
    ema_slow = _ema(close, 50)
    atr = _atr(df, 14)

    last = float(close.iloc[-1])
    prev = float(close.iloc[-2])

    atr_last = float(atr.iloc[-1]) if not pd.isna(atr.iloc[-1]) else 0.0
    atr_pct = (atr_last / last) * 100.0 if last else 0.0

    dist = float((ema_fast.iloc[-1] - ema_slow.iloc[-1]) / last) if last else 0.0
    trend_score = max(-1.0, min(1.0, dist * 20.0))

    vol = df["volume"].fillna(0.0)
    vol_mean = vol.rolling(30).mean().iloc[-1]
    vol_score = float(vol.iloc[-1] / vol_mean) if vol_mean and vol_mean > 0 else 0.0
    vol_score = max(0.0, min(5.0, vol_score))

    return Indicators(
        atr_pct=atr_pct,
        ema_fast=float(ema_fast.iloc[-1]),
        ema_slow=float(ema_slow.iloc[-1]),
        trend_score=trend_score,
        vol_score=vol_score,
        last=last,
        prev=prev,
    )
