from app.data_fetcher import fetch_ohlcv_df
from app.excel_bridge import ExcelBridge
from app.logger import get_logger

logger = get_logger(__name__)
excel = ExcelBridge("DYZEN_CAPITAL_OS_AI_LIVE_CORE_READY.xlsx")

def generate_signal(exchange, symbol):
    df = fetch_ohlcv_df(exchange, symbol)
    latest = df.iloc[-1]

    excel.write_inputs({
        "PRICE": latest["close"],
        "VOLUME": latest["volume"],
    })

    decision = excel.read_decision()
    if decision["action"] == "HOLD":
        return None

    logger.info(f"Signal {symbol}: {decision}")
    return decision
