import time
import ccxt
from app.config import settings
from app.logger import get_logger

logger = get_logger(__name__)
_exchange = None

def _retry_call(fn, *args, **kwargs):
    last_err = None
    base_delay = settings.RETRY_DELAY
    for attempt in range(settings.MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_err = e
            sleep_time = base_delay * (2 ** attempt)  # exponential backoff
            logger.warning(
                f"CCXT retry {attempt+1}/{settings.MAX_RETRIES} in {sleep_time:.2f}s: {e}"
            )
            time.sleep(sleep_time)
    raise last_err

def get_exchange():
    global _exchange
    if _exchange:
        return _exchange

    exchange_class = getattr(ccxt, settings.EXCHANGE)
    _exchange = exchange_class({
        "enableRateLimit": True,
        "options": {"defaultType": "future"},
    })
    _exchange.load_markets()
    return _exchange

def fetch_ohlcv_safe(exchange, *args, **kwargs):
    return _retry_call(exchange.fetch_ohlcv, *args, **kwargs)

def create_order_safe(exchange, *args, **kwargs):
    return _retry_call(exchange.create_order, *args, **kwargs)

def fetch_balance_safe(exchange, *args, **kwargs):
    return _retry_call(exchange.fetch_balance, *args, **kwargs)
