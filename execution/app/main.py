import time
from app.config import settings
from app.logger import get_logger
from app.exchange import get_exchange
from app.signal_engine import generate_signal
from app.order_executor import execute_signal
from app.cooldown_manager import CooldownManager
from app.kill_switch import KillSwitch

logger = get_logger(__name__)

def run():
    logger.info("🥋 Mr. JAIANI starting...")
    exchange = get_exchange()
    cooldown = CooldownManager()
    kill_switch = KillSwitch()

    while True:
        try:
            if kill_switch.should_halt():
                logger.warning("🚨 Kill switch active — sleeping")
                time.sleep(30)
                continue

            for symbol in settings.SYMBOLS:
                if cooldown.in_cooldown(symbol):
                    continue

                signal = generate_signal(exchange, symbol)
                if signal is None:
                    continue

                execute_signal(exchange, symbol, signal)
                cooldown.mark_trade(symbol)

            time.sleep(settings.LOOP_INTERVAL)

        except Exception as e:
            logger.exception(f"Main loop error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    run()
