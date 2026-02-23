import threading
import xlwings as xw
from app.logger import get_logger

logger = get_logger(__name__)

class ExcelBridge:
    def __init__(self, path):
        self.path = path
        self.book = None
        self.sheet = None
        self._lock = threading.Lock()
        self._connect()

    def _connect(self):
        try:
            self.book = xw.Book(self.path)
            self.sheet = self.book.sheets["CORE"]
        except Exception as e:
            logger.error(f"Excel connect failed: {e}")
            self.book = None
            self.sheet = None

    def ensure_connection(self):
        if self.book is None or self.sheet is None:
            self._connect()

    def write_inputs(self, data: dict):
        with self._lock:
            self.ensure_connection()
            if not self.sheet:
                return
            for k, v in data.items():
                try:
                    self.sheet.range(k).value = v
                except Exception as e:
                    logger.warning(f"Excel write failed {k}: {e}")

    def read_decision(self):
        with self._lock:
            self.ensure_connection()
            if not self.sheet:
                return {"action": "HOLD", "confidence": 0}
            try:
                return {
                    "action": self.sheet.range("AI_DECISION").value,
                    "confidence": self.sheet.range("CONFIDENCE").value,
                }
            except Exception as e:
                logger.warning(f"Excel read failed: {e}")
                return {"action": "HOLD", "confidence": 0}
