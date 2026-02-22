# execution/main.py
import os
import time
import logging
import signal
from typing import Optional, Dict, Any

from execution.db.db import init_db
from execution.db.repository import get_system_state, update_system_state, log_event
from execution.execution_engine import ExecutionEngine
from execution.signal_client import pop_next_signal
from execution.kill_switch import is_kill_switch_active

logger = logging.getLogger("gbm")

_SHOULD_STOP = False


def _handle_sigterm(signum, frame):
    global _SHOULD_STOP
    _SHOULD_STOP = True
    try:
        logger.warning(f"SIGNAL_RECEIVED | signum={signum} -> stopping loop gracefully")
    except Exception:
        pass


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
        logger.warning(
            "BOOTSTRAP_STATE | applying self-heal -> status=RUNNING startup_sync_ok=1 kill_switch=0"
        )
        update_system_state(status="RUNNING", startup_sync_ok=1, kill_switch=0)


def _try_import_generator():
    try:
        from execution.signal_generator import run_once as generate_once  # type: ignore
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


def main() -> None:
    # logging
    logging.basicConfig(
        level=logging.INFO,
        format='[%(levelname)s] %(asctime)s - %(message)s',
        force=True,
    )

    # graceful stop handling (Render sends SIGTERM on deploy/restart)
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    mode = os.getenv("MODE", "DEMO").upper()

    # Outbox path: support both names (some envs differ)
    outbox_path = os.getenv("SIGNAL_OUTBOX_PATH") or os.getenv("OUTBOX_PATH") or "/var/data/signal_outbox.json"

    # Sleep: support both names
    sleep_s = float(os.getenv("LOOP_SLEEP_SECONDS") or os.getenv("WORKER_SLEEP_SECONDS") or "10")

    # Heartbeat cadence (avoid spamming logs every loop)
    heartbeat_every_s = float(os.getenv("HEARTBEAT_EVERY_SECONDS", "60"))

    logger.info("Worker initialized OK")
    logger.info(f"GENIUS BOT MAN worker starting | MODE={mode}")
    logger.info(f"OUTBOX_PATH={outbox_path}")
    logger.info(f"LOOP_SLEEP_SECONDS={sleep_s}")
    logger.info(f"HEARTBEAT_EVERY_SECONDS={heartbeat_every_s}")

    # init DB + bootstrap state
    try:
        init_db()
        _bootstrap_state_if_needed()
    except Exception as e:
        logger.exception(f"BOOTSTRAP_FATAL | err={e}")
        try:
            log_event("BOOTSTRAP_FATAL", f"err={e}")
        except Exception:
            pass
        # If DB bootstrap fails, no point continuing.
        raise

    # build engine
    try:
        engine = ExecutionEngine()
    except Exception as e:
        logger.exception(f"ENGINE_INIT_FATAL | err={e}")
        try:
            log_event("ENGINE_INIT_FATAL", f"err={e}")
        except Exception:
            pass
        raise

    # initial reconcile (non-fatal)
    try:
        engine.reconcile_oco()
    except Exception as e:
        logger.warning(f"OCO_RECONCILE_START_WARN | err={e}")

    generate_once = _try_import_generator()

    last_heartbeat = 0.0

    # main loop
    while not _SHOULD_STOP:
        loop_started = time.time()
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
                    created = generate_once(outbox_path)
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
                logger.info(
                    f"Signal received | id={sig.get('signal_id')} | verdict={sig.get('final_verdict')}"
                )
                try:
                    engine.execute_signal(sig)
                except Exception as e:
                    logger.exception(f"EXECUTE_SIGNAL_FAIL | err={e}")
                    try:
                        log_event("EXECUTE_SIGNAL_FAIL", f"err={e}")
                    except Exception:
                        pass
            else:
                now = time.time()
                if now - last_heartbeat >= heartbeat_every_s:
                    logger.info("Worker alive, waiting for SIGNAL_OUTBOX...")
                    last_heartbeat = now

        except Exception as e:
            # This should never kill the loop
            logger.exception(f"WORKER_LOOP_ERROR | err={e}")
            try:
                log_event("WORKER_LOOP_ERROR", f"err={e}")
            except Exception:
                pass

        # sleep (keep cadence stable-ish even if loop work takes time)
        elapsed = time.time() - loop_started
        delay = sleep_s - elapsed
        if delay < 0:
            delay = 0.0
        time.sleep(delay)

    logger.warning("WORKER_STOPPED | graceful shutdown complete")


if __name__ == "__main__":
    main()
