import pandas as pd
from app.config import settings
from app.exchange import fetch_ohlcv_safe

def fetch_ohlcv_df(exchange, symbol, limit=200):
    ohlcv = fetch_ohlcv_safe(exchange, symbol, settings.TIMEFRAME, limit=limit)
    df = pd.DataFrame(
        ohlcv,
        columns=["ts","open","high","low","close","volume"]
    )
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    return df
