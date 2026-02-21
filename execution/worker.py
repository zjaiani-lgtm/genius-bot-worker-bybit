# execution/worker.py
from __future__ import annotations

import logging
import os
import time
from typing import Optional, Dict, Any

from execution.db.db import init_db
from execution.db.repository import (
    get_system_state,
    update_system_state,
    log_event,
    get_trade_stats,
)
from execution.execution_engine import ExecutionEngine
from execution.signal_client import pop_next_signal
from execution.kill_switch import is_kill_switch_active

logger = logging.getLogger("gbm")

# -------------------------
# module-level "singleton" state
# -------------------------
_BOOTSTRAPPED = False
_ENGINE: Optional[ExecutionEngine] = None
_GENERATE_ONCE = None

_OUTBOX_PATH = os.getenv("SIGNAL_OUTBOX_PATH", "/var/data/signal_outbox.json")
_REPORT_EVERY_S = int(os.getenv("REPORT_EVERY_SECONDS", "60"))
_LAST_REPORT_TS = 0.0


def _bootstrap_state_if_needed() -> None:
    raw = get_system_state()
    if not isinstance(raw, (list, tuple)) or len(raw) < 5:
        logger.warning("BOOTSTRAP_STATE | system_state row missing or invalid -> skip")
        return

    status = str(raw[1] or "").upper()
    startup_sync_ok = int(raw[2] or 0)
    kill_switch_db = int(raw[3] or 0)

    env_kill = os.getenv("KILL_SWITCH", "false").lower() == "true"

    logger.info(
        f"BOOTSTRAP_STATE | status={status} startup_sync_ok={startup_sync_ok} "
        f"kill_db={kill_switch_db} env_kill={env_kill}"
    )

    if env_kill or kill_switch_db == 1:
        logger.warning("BOOTSTRAP_STATE | kill switch ON -> skip overrides")
        return

    if status == "PAUSED" or startup_sync_ok == 0:
        logger.warning("BOOTSTRAP_STATE | applying self-heal -> status=RUNNING startup_sync_ok=1 kill_switch=0")
        update_system_state(status="RUNNING", startup_sync_ok=1, kill_switch=0)


def _try_import_generator():
    try:
        from execution.signal_generator import run_once as generate_once
        return generate_once
    except Exception as e:
        logger.error(f"GENERATOR_IMPORT_FAIL | err={e} -> generator disabled (consumer will still run)")
        try:
            log_event("GENERATOR_IMPORT_FAIL", f"err={e}")
        except Exception:
            pass
        return None


def _safe_pop_next_signal(outbox_path: str) -> Optional[Dict[str, Any]]:
    try:
        return pop_next_signal(outbox_path)
    except Exception as e:
        logger.exception(f"OUTBOX_POP_FAIL | path={outbox_path} err={e}")
        try:
            log_event("OUTBOX_POP_FAIL", f"path={outbox_path} err={e}")
        except Exception:
            pass
        return None


def _run_performance_report_safe() -> None:
    try:
        s = get_trade_stats()
        logger.info(
            "PERF_REPORT | closed=%s wins=%s losses=%s winrate=%.2f%% roi=%.2f%% pnl=%.4f quote_in=%.4f pf=%.3f",
            s.get("closed_trades", 0),
            s.get("wins", 0),
            s.get("losses", 0),
            float(s.get("winrate_pct", 0.0)),
            float(s.get("roi_pct", 0.0)),
            float(s.get("pnl_quote_sum", 0.0)),
            float(s.get("quote_in_sum", 0.0)),
            float(s.get("profit_factor", 0.0)),
        )
        try:
            log_event(
                "PERF_REPORT",
                f"closed={s.get('closed_trades',0)} "
                f"winrate={float(s.get('winrate_pct',0.0)):.2f}% "
                f"roi={float(s.get('roi_pct',0.0)):.2f}% "
                f"pnl={float(s.get('pnl_quote_sum',0.0)):.4f}"
            )
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"PERF_REPORT_FAIL | err={e}")


def _ensure_initialized() -> None:
    """
    Called once lazily by run_worker_loop(). Creates DB + engine + generator,
    and performs initial OCO reconcile.
    """
    global _BOOTSTRAPPED, _ENGINE, _GENERATE_ONCE

    if _BOOTSTRAPPED:
        return

    init_db()
    _bootstrap_state_if_needed()

    _ENGINE = ExecutionEngine()

    try:
        _ENGINE.reconcile_oco()
    except Exception as e:
        logger.warning(f"OCO_RECONCILE_START_WARN | err={e}")

    _GENERATE_ONCE = _try_import_generator()

    logger.info("Worker initialized OK")
    logger.info(f"OUTBOX_PATH={_OUTBOX_PATH}")
    logger.info(f"REPORT_EVERY_SECONDS={_REPORT_EVERY_S}")

    _BOOTSTRAPPED = True


def run_worker_loop() -> None:
    """
    Runs ONE iteration of the worker loop.
    Your hardened execution/main.py calls this repeatedly + sleeps outside.
    """
    global _LAST_REPORT_TS

    _ensure_initialized()
    assert _ENGINE is not None

    # 0) ABSOLUTE KILL SWITCH
    if is_kill_switch_active():
        logger.warning("KILL_SWITCH_ACTIVE | worker will not generate/pop/execute signals")
        try:
            log_event("WORKER_KILL_SWITCH_ACTIVE", "blocked before loop actions")
        except Exception:
            pass
        return

    # 1) reconcile OCO
    try:
        _ENGINE.reconcile_oco()
    except Exception as e:
        logger.warning(f"OCO_RECONCILE_LOOP_WARN | err={e}")

    # 2) generate (optional)
    if _GENERATE_ONCE is not None:
        try:
            created = _GENERATE_ONCE(_OUTBOX_PATH)
            if created:
                logger.info("SIGNAL_GENERATOR | signal created")
        except Exception as e:
            logger.exception(f"SIGNAL_GENERATOR_FAIL | err={e}")
            try:
                log_event("SIGNAL_GENERATOR_FAIL", f"err={e}")
            except Exception:
                pass

    # 3) pop + execute
    sig = _safe_pop_next_signal(_OUTBOX_PATH)
    if sig:
        logger.info(f"Signal received | id={sig.get('signal_id')} | verdict={sig.get('final_verdict')}")
        _ENGINE.execute_signal(sig)
    else:
        logger.info("Worker alive, waiting for SIGNAL_OUTBOX...")

    # 4) perf report
    now = time.time()
    if _REPORT_EVERY_S > 0 and (now - _LAST_REPORT_TS) >= _REPORT_EVERY_S:
        _run_performance_report_safe()
        _LAST_REPORT_TS = now
