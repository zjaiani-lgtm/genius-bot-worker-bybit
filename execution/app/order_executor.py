from app.logger import get_logger
from app.exchange import create_order_safe, fetch_balance_safe
from app.config import settings
from app.risk_manager import calc_position_size

logger = get_logger(__name__)

def _round_amount(exchange, symbol, amount):
    try:
        return float(exchange.amount_to_precision(symbol, amount))
    except Exception:
        return amount

def _get_min_notional(exchange, symbol):
    try:
        market = exchange.market(symbol)
        limits = market.get("limits", {})
        cost = limits.get("cost", {})
        return cost.get("min")
    except Exception:
        return None

def execute_signal(exchange, symbol, signal):
    side = "buy" if signal["action"] == "BUY" else "sell"

    # --- balance aware sizing ---
    try:
        balance = fetch_balance_safe(exchange)
        usdt_balance = balance.get("total", {}).get("USDT", 0)
    except Exception:
        usdt_balance = 0

    price = signal.get("price") or signal.get("last_price") or 1
    stop_distance = signal.get("stop_distance") or (price * 0.01)

    min_notional = _get_min_notional(exchange, symbol)

    raw_size = calc_position_size(
        balance=usdt_balance,
        risk_per_trade=settings.RISK_PER_TRADE,
        stop_distance=stop_distance,
        min_notional=min_notional,
        price=price,
    )

    amount = _round_amount(exchange, symbol, raw_size)

    if amount <= 0:
        logger.warning("Calculated order size is zero — skipping trade")
        return

    logger.info(f"Executing {side} on {symbol} size={amount}")
    create_order_safe(exchange, symbol, "market", side, amount)
