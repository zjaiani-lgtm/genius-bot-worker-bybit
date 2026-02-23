from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional


@dataclass
class LogEvent:
    ts: float
    level: str
    msg: str
    fields: Dict[str, Any]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = {
            "ts": time.time(),
            "level": record.levelname,
            "msg": record.getMessage(),
            "logger": record.name,
        }
        if hasattr(record, "fields") and isinstance(record.fields, dict):
            base.update(record.fields)
        if record.exc_info:
            base["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(base, ensure_ascii=False)


def bootstrap_logger(name: str = "genius_bot") -> logging.Logger:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if logger.handlers:
        return logger

    handler = logging.StreamHandler(sys.stdout)
    fmt = os.getenv("LOG_FORMAT", "JSON").upper()
    if fmt == "JSON":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("[%(levelname)s] %(asctime)s - %(message)s"))

    logger.addHandler(handler)
    logger.propagate = False
    return logger


def log(logger: logging.Logger, level: str, msg: str, **fields: Any) -> None:
    lvl = getattr(logging, level.upper(), logging.INFO)
    logger.log(lvl, msg, extra={"fields": fields})
