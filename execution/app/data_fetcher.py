from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List

import pandas as pd

from .logger import log


@dataclass
class MarketData:
    df: pd.DataFrame  # ts, open, high, low, close, volume


def ohlcv_to_df(ohlcv: List[List[Any]]) -> pd.DataFrame:
    if not ohlcv:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def fetch_market_data(ex, symbol: str, timeframe: str, limit: int, logger) -> MarketData:
    ohlcv = ex.fetch_ohlcv_safe(symbol, timeframe, limit=limit)
    df = ohlcv_to_df(ohlcv)

    if not df.empty:
        df = df.drop_duplicates(subset=["ts"]).sort_values("ts").reset_index(drop=True)
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["open", "high", "low", "close"])
    else:
        log(logger, "WARNING", "MARKET_DATA_EMPTY", symbol=symbol, timeframe=timeframe)

    return MarketData(df=df)
