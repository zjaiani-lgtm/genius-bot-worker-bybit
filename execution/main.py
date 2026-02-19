# execution/main.py
import os
import time
import logging
from typing import Optional, Dict, Any

from execution.db.db import init_db
from execution.db.repository import get_system_state, update_system_state, log_event
from execution.execution_engine import ExecutionEngine
from execution.signal_client import pop_next_signal
from execution.kill_switch import is_kill_switch_active
from execution.auto_scaler import AutoScaler
from execution.db.repository import mark_signal_id_executed

logger = logging.getLogger("gbm")

WORKER_DEBUG = os.getenv("WORKER_DEBUG", "false").strip().lower() == "true"


def _bootstrap_state_if_needed() -> None:
    raw = get_system_state()
    if not isinstance(raw, (list, tuple)) or len(raw) < 5:
        logger.warning("BOOTSTRAP_STATE | system_state row missing or invalid -> skip")
        return

    status = str(raw[1] or "").upper()
    startup_sync_ok = int(raw[2] or 0)
    kill_switch_db = int(raw[3] or 0)

    env_kill = os.getenv("KILL_SWITCH", "false").strip().lower() == "true"

    merged_kill = 1 if (kill_switch_db == 1 or env_kill) else 0

    logger.info(
        f"BOOTSTRAP_STATE | status={status} startup_sync_ok={startup_sync_ok} kill_db={kill_switch_db} env_kill={env_kill}"
    )

    # Keep DB status stable; only adjust kill switch if env wants it.
    try:
        update_system_state(status=status or "RUNNING", kill_switch=merged_kill)
    except Exception as e:
        logger.warning(f"BOOTSTRAP_STATE_WARN | update_system_state failed err={e}")


def _safe_pop_next_signal(outbox_path: str) -> Optional[Dict[str, Any]]:
    try:
        return pop_next_signal(outbox_path)
    except Exception as e:
        logger.warning(f"OUTBOX_POP_WARN | err={e}")
        return None


def main() -> None:
    # log level
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, log_level, logging.INFO))

    # init db
    init_db()
    _bootstrap_state_if_needed()

    engine = ExecutionEngine()

    # outbox path
    outbox_path = os.getenv("OUTBOX_PATH") or os.getenv("SIGNAL_OUTBOX_PATH") or "/var/data/signal_outbox.json"

    # loop sleep
    sleep_s = float(os.getenv("LOOP_SLEEP_SECONDS", "10"))

    # generator (optional)
    generate_enabled = os.getenv("ENABLE_GENERATOR", "true").strip().lower() == "true"
    generate_once = None
    if generate_enabled:
        try:
            from execution.signal_generator import generate_signal as generate_once
        except Exception as e:
            logger.warning(f"GENERATOR_IMPORT_FAIL | err={e}")
            generate_once = None

    auto_scaler = AutoScaler()

    logger.info(f"GENIUS BOT MAN worker starting | MODE={engine.mode}")
    logger.info(f"OUTBOX_PATH={outbox_path}")
    logger.info(f"LOOP_SLEEP_SECONDS={sleep_s}")

    while True:
        try:
            # 0) ABSOLUTE KILL SWITCH (before everything)
            if is_kill_switch_active():
                logger.warning("KILL_SWITCH_ACTIVE | worker will not generate/pop/execute signals")
                try:
                    log_event("WORKER_KILL_SWITCH_ACTIVE", "blocked before loop actions")
                except Exception:
                    pass
                time.sleep(sleep_s)
                continue

            # 1) reconcile OCO
            try:
                engine.reconcile_oco()
            except Exception as e:
                logger.warning(f"OCO_RECONCILE_LOOP_WARN | err={e}")

            # 2) generate (optional)
            if generate_once is not None:
                try:
                    diag_ok = None
                    try:
                        if engine.exchange is not None:
                            diag_ok = bool((engine.exchange.diagnostics() or {}).get("ok"))
                    except Exception:
                        diag_ok = False

                    active_symbols = auto_scaler.active_symbols(exchange_diag_ok=diag_ok)
                    created = generate_once(outbox_path, symbols_override=active_symbols)
                    if created:
                        logger.info("SIGNAL_GENERATOR | signal created")
                except Exception as e:
                    logger.exception(f"SIGNAL_GENERATOR_FAIL | err={e}")
                    try:
                        log_event("SIGNAL_GENERATOR_FAIL", f"err={e}")
                    except Exception:
                        pass

            # 3) pop + execute
            sig = _safe_pop_next_signal(outbox_path)
            if sig:
                # High-signal log (always)
                logger.info(
                    f"SIGNAL_RECEIVED | id={sig.get('signal_id')} verdict={sig.get('final_verdict')} "
                    f"symbol={(sig.get('execution') or {}).get('symbol')} dir={(sig.get('execution') or {}).get('direction')}"
                )
                if WORKER_DEBUG:
                    logger.info(f"SIGNAL_META | id={sig.get('signal_id')} meta={sig.get('meta')}")

                # ✅ basket enforcement (in case basket shrank between generate and execute)
                try:
                    diag_ok = None
                    try:
                        if engine.exchange is not None:
                            diag_ok = bool((engine.exchange.diagnostics() or {}).get("ok"))
                    except Exception:
                        diag_ok = False

                    active_symbols = set(auto_scaler.active_symbols(exchange_diag_ok=diag_ok))
                    sym = str(((sig.get("execution") or {}).get("symbol")) or "").upper()
                    if active_symbols and sym and sym not in active_symbols:
                        logger.warning(f"EXEC_REJECT | symbol not in ACTIVE basket | symbol={sym}")
                        log_event("EXEC_REJECT_NOT_IN_ACTIVE_BASKET", f"id={sig.get('signal_id')} symbol={sym}")
                        # mark as executed so we don't loop on the same popped signal
                        mark_signal_id_executed(str(sig.get("signal_id")), action="REJECT_ACTIVE_BASKET", symbol=sym)
                    else:
                        t_exec0 = time.time()
                        try:
                            engine.execute_signal(sig)
                            dt_ms = int((time.time() - t_exec0) * 1000)
                            logger.info(f"EXEC_DONE | id={sig.get('signal_id')} dt={dt_ms}ms")
                        except Exception as e:
                            dt_ms = int((time.time() - t_exec0) * 1000)
                            logger.exception(f"EXEC_FAIL | id={sig.get('signal_id')} dt={dt_ms}ms err={e}")
                            raise
                except Exception as e:
                    logger.warning(f"ACTIVE_BASKET_ENFORCE_WARN | err={e} -> fallback execute")
                    t_exec0 = time.time()
                    try:
                        engine.execute_signal(sig)
                        dt_ms = int((time.time() - t_exec0) * 1000)
                        logger.info(f"EXEC_DONE | id={sig.get('signal_id')} dt={dt_ms}ms")
                    except Exception as ee:
                        dt_ms = int((time.time() - t_exec0) * 1000)
                        logger.exception(f"EXEC_FAIL | id={sig.get('signal_id')} dt={dt_ms}ms err={ee}")
                        raise
            else:
                logger.info("Worker alive, waiting for SIGNAL_OUTBOX...")

        except Exception as e:
            logger.exception(f"WORKER_LOOP_ERROR | err={e}")
            try:
                log_event("WORKER_LOOP_ERROR", f"err={e}")
            except Exception:
                pass

        time.sleep(sleep_s)


if __name__ == "__main__":
    main()
