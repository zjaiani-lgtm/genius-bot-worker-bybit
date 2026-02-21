# execution/db/db.py
import os
import sqlite3

DB_PATH = os.getenv("DB_PATH", "/var/data/gbm.sqlite3")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_connection()
    cur = conn.cursor()

    # -------------------------
    # system_state
    # -------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS system_state (
        id INTEGER PRIMARY KEY CHECK (id=1),
        status TEXT NOT NULL DEFAULT 'RUNNING',
        startup_sync_ok INTEGER NOT NULL DEFAULT 1,
        kill_switch INTEGER NOT NULL DEFAULT 0,
        updated_at_utc TEXT
    )
    """)

    cur.execute("SELECT id FROM system_state WHERE id=1")
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO system_state (id, status, startup_sync_ok, kill_switch, updated_at_utc) "
            "VALUES (1, 'RUNNING', 1, 0, '')"
        )

    # -------------------------
    # events
    # -------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        details TEXT,
        created_at_utc TEXT NOT NULL
    )
    """)

    # -------------------------
    # executed_signals
    # -------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS executed_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id TEXT NOT NULL,
        signal_hash TEXT,
        action TEXT NOT NULL,
        symbol TEXT,
        created_at_utc TEXT NOT NULL
    )
    """)

    # MIGRATION: add signal_hash if old table existed
    cur.execute("PRAGMA table_info(executed_signals)")
    ex_cols = {r[1] for r in (cur.fetchall() or [])}
    if "signal_hash" not in ex_cols:
        cur.execute("ALTER TABLE executed_signals ADD COLUMN signal_hash TEXT")

    # unique index (safe)
    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS ux_executed_signals_signal_id_action
    ON executed_signals(signal_id, action)
    """)

    # -------------------------
    # oco_links (create table first, then migrate columns, then indexes)
    # -------------------------
    # IMPORTANT: keep this minimal to not break old table creation
    cur.execute("""
    CREATE TABLE IF NOT EXISTS oco_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        link_id TEXT,
        symbol TEXT NOT NULL,
        tp_order_id TEXT NOT NULL,
        sl_order_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        created_at_utc TEXT NOT NULL
    )
    """)

    # MIGRATION: add missing columns to old oco_links BEFORE creating indexes that reference them
    cur.execute("PRAGMA table_info(oco_links)")
    oco_cols = {r[1] for r in (cur.fetchall() or [])}

    def _add_col(name: str, ddl: str):
        if name not in oco_cols:
            cur.execute(f"ALTER TABLE oco_links ADD COLUMN {ddl}")

    _add_col("signal_id", "signal_id TEXT")
    _add_col("base_asset", "base_asset TEXT")
    _add_col("tp_price", "tp_price REAL")
    _add_col("sl_stop_price", "sl_stop_price REAL")
    _add_col("sl_limit_price", "sl_limit_price REAL")
    _add_col("amount", "amount REAL")
    _add_col("updated_at_utc", "updated_at_utc TEXT")

    # refresh columns after migration (optional but clearer)
    cur.execute("PRAGMA table_info(oco_links)")
    oco_cols2 = {r[1] for r in (cur.fetchall() or [])}

    # Now it is safe to create indexes referencing migrated columns
    cur.execute("CREATE INDEX IF NOT EXISTS ix_oco_links_status ON oco_links(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_oco_links_symbol ON oco_links(symbol)")
    if "signal_id" in oco_cols2:
        cur.execute("CREATE INDEX IF NOT EXISTS ix_oco_links_signal_id ON oco_links(signal_id)")

    # backfill signal_id from legacy link_id if needed
    cur.execute("UPDATE oco_links SET signal_id = COALESCE(signal_id, link_id) WHERE signal_id IS NULL")

    # -------------------------
    # trade_history
    # -------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS trade_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        quote_amount REAL NOT NULL,
        base_amount REAL,
        entry_price REAL NOT NULL,
        entry_order_id TEXT,
        opened_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'OPEN',
        close_reason TEXT,
        exit_price REAL,
        exit_order_id TEXT,
        closed_at TEXT,
        pnl_quote REAL,
        pnl_pct REAL
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS ix_trade_history_status ON trade_history(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_trade_history_symbol ON trade_history(symbol)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_trade_history_signal_id ON trade_history(signal_id)")

    conn.commit()
    conn.close()
