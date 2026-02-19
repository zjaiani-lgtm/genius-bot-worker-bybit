# execution/signal_client.py
import json
import os
import hashlib
import logging
from typing import Any, Dict, List, Optional
from tempfile import NamedTemporaryFile

logger = logging.getLogger("gbm")

OUTBOX_DEBUG = os.getenv("OUTBOX_DEBUG", "false").strip().lower() == "true"


def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _fingerprint(signal: Dict[str, Any]) -> str:
    """
    Stable fingerprint for idempotency.
    IMPORTANT: do NOT use uuid/signal_id since those change.
    """
    execution = signal.get("execution") or {}
    meta = signal.get("meta") or {}

    parts = [
        str(signal.get("final_verdict") or ""),
        str(execution.get("symbol") or ""),
        str(execution.get("direction") or ""),
        str((execution.get("entry") or {}).get("type") or ""),
        str(_safe_float(execution.get("quote_amount")) or ""),
        str(_safe_float(execution.get("position_size")) or ""),
        str(meta.get("source") or ""),
    ]
    raw = "|".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with NamedTemporaryFile("w", delete=False, dir=os.path.dirname(path), suffix=".tmp") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        tmp = f.name
    os.replace(tmp, path)


def _read_outbox(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"signals": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {"signals": []}
    except Exception:
        return {"signals": []}


def validate_signal(signal: Dict[str, Any]) -> None:
    if not isinstance(signal, dict):
        raise ValueError("signal must be dict")
    if not signal.get("signal_id"):
        raise ValueError("signal_id missing")
    if not signal.get("final_verdict"):
        raise ValueError("final_verdict missing")
    execution = signal.get("execution") or {}
    if not execution.get("symbol"):
        raise ValueError("execution.symbol missing")


def append_signal(signal: Dict[str, Any], outbox_path: str) -> None:
    validate_signal(signal)

    fp = _fingerprint(signal)
    signal["_fingerprint"] = fp

    data = _read_outbox(outbox_path)
    signals: List[Dict[str, Any]] = data.get("signals", [])

    # soft dedupe in outbox (DB dedupe is the real safety net)
    if any((s.get("_fingerprint") == fp) for s in signals[-50:]):
        logger.info(f"OUTBOX_DEDUPED | fingerprint={fp}")
        return

    signals.append(signal)
    data["signals"] = signals
    _atomic_write_json(outbox_path, data)
    if OUTBOX_DEBUG:
        logger.info(
            f"OUTBOX_APPEND_OK | id={signal.get('signal_id')} verdict={signal.get('final_verdict')} "
            f"fp={fp} size={len(signals)} path={outbox_path}"
        )


def pop_next_signal(outbox_path: str) -> Optional[Dict[str, Any]]:
    """
    Pops FIFO: takes the oldest signal from outbox.
    Atomic rewrite.
    """
    data = _read_outbox(outbox_path)
    signals: List[Dict[str, Any]] = data.get("signals", [])
    if not signals:
        return None

    sig = signals.pop(0)
    data["signals"] = signals
    _atomic_write_json(outbox_path, data)
    if OUTBOX_DEBUG:
        logger.info(
            f"OUTBOX_POP_OK | id={sig.get('signal_id')} verdict={sig.get('final_verdict')} "
            f"remaining={len(signals)} path={outbox_path}"
        )
    return sig
