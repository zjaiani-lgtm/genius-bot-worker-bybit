import time
import threading

class CooldownManager:
    def __init__(self):
        self.last_trade = {}
        self.cooldown_sec = 60
        self._lock = threading.Lock()

    def in_cooldown(self, symbol):
        with self._lock:
            t = self.last_trade.get(symbol)
            if not t:
                return False
            return time.time() - t < self.cooldown_sec

    def mark_trade(self, symbol):
        with self._lock:
            self.last_trade[symbol] = time.time()
