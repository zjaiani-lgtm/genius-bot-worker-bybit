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
    # executed_signals (idempotency)  ✅ now supports signal_hash
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
    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS ux_executed_signals_signal_id_action
    ON executed_signals(signal_id, action)
    """)

    # MIGRATION: add signal_hash if old table existed
    cur.execute("PRAGMA table_info(executed_signals)")
    cols = {r[1] for r in (cur.fetchall() or [])}
    if "signal_hash" not in cols:
        cur.execute("ALTER TABLE executed_signals ADD COLUMN signal_hash TEXT")

    # -------------------------
    # oco_links  ✅ engine-compatible schema + migrations
    # -------------------------
    cur.execute("""
    CREATE TABLE IF NOT EXISTS oco_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        link_id TEXT,              -- legacy (we also set it = signal_id)
        signal_id TEXT,            -- ✅ engine uses this
        symbol TEXT NOT NULL,
        base_asset TEXT,
        tp_order_id TEXT NOT NULL,
        sl_order_id TEXT NOT NULL,
        tp_price REAL,
        sl_stop_price REAL,
        sl_limit_price REAL,
        amount REAL,
        status TEXT NOT NULL DEFAULT 'open',
        created_at_utc TEXT NOT NULL,
        updated_at_utc TEXT
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS ix_oco_links_status ON oco_links(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_oco_links_symbol ON oco_links(symbol)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_oco_links_signal_id ON oco_links(signal_id)")

    # MIGRATION: add missing columns if old oco_links existed
    cur.execute("PRAGMA table_info(oco_links)")
    oco_cols = {r[1] for r in (cur.fetchall() or [])}

    def _add_col(name: str, ddl: str):
        if name not in oco_cols:
            cur.execute(f"ALTER TABLE oco_links ADD COLUMN {ddl}")

    _add_col("link_id", "link_id TEXT")
    _add_col("signal_id", "signal_id TEXT")
    _add_col("base_asset", "base_asset TEXT")
    _add_col("tp_price", "tp_price REAL")
    _add_col("sl_stop_price", "sl_stop_price REAL")
    _add_col("sl_limit_price", "sl_limit_price REAL")
    _add_col("amount", "amount REAL")
    _add_col("updated_at_utc", "updated_at_utc TEXT")

    # backfill signal_id from legacy link_id if needed
    cur.execute("UPDATE oco_links SET signal_id = COALESCE(signal_id, link_id) WHERE signal_id IS NULL")

    # -------------------------
    # trade_history ✅ used for performance stats + close by OCO
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
