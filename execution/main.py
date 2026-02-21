import logging
import os
import time

# ============================================================
# LOGGING SETUP
# ============================================================

logger = logging.getLogger("gbm")
logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(asctime)s - %(message)s",
)

logger.info("Execution main starting...")

# ============================================================
# SAFE IMPORTS (HARDENED)
# ============================================================

try:
    from execution.db.repository import (
        get_system_state,
        update_system_state,
        log_event,
        get_trade_stats,
    )
    logger.info("Repository import OK")
except Exception as e:
    logger.exception(f"REPOSITORY_IMPORT_FAIL | err={e}")
    raise

try:
    from execution.worker import run_worker_loop
    logger.info("Worker import OK")
except Exception as e:
    logger.exception(f"WORKER_IMPORT_FAIL | err={e}")
    raise

# ============================================================
# ENV
# ============================================================

POLL_INTERVAL = float(os.getenv("WORKER_POLL_INTERVAL", "10"))

# ============================================================
# MAIN LOOP
# ============================================================

def main():
    logger.info("Execution main loop starting...")

    # optional: touch DB to verify connectivity
    try:
        state = get_system_state()
        logger.info(f"DB_CHECK_OK | state={state}")
    except Exception as e:
        logger.warning(f"DB_CHECK_FAIL | err={e}")

    # start worker loop
    while True:
        try:
            run_worker_loop()
        except Exception as e:
            logger.exception(f"WORKER_LOOP_ERROR | err={e}")

        time.sleep(POLL_INTERVAL)


# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    main()
