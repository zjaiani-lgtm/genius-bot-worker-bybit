# execution/db/db.py
import os
import sqlite3
from typing import Optional

DB_PATH = os.getenv("DB_PATH", "/var/data/gbm.sqlite3")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_connection()
    cur = conn.cursor()

    # system_state
    cur.execute("""
    CREATE TABLE IF NOT EXISTS system_state (
        id INTEGER PRIMARY KEY CHECK (id=1),
        status TEXT NOT NULL DEFAULT 'RUNNING',
        startup_sync_ok INTEGER NOT NULL DEFAULT 1,
        kill_switch INTEGER NOT NULL DEFAULT 0,
        updated_at_utc TEXT
    )
    """)

    # ensure row exists
    cur.execute("SELECT id FROM system_state WHERE id=1")
    if cur.fetchone() is None:
        cur.execute("INSERT INTO system_state (id, status, startup_sync_ok, kill_switch, updated_at_utc) VALUES (1, 'RUNNING', 1, 0, '')")

    # events
    cur.execute("""
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        details TEXT,
        created_at_utc TEXT NOT NULL
    )
    """)

    # executed signals (idempotency)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS executed_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id TEXT NOT NULL,
        action TEXT NOT NULL,
        symbol TEXT,
        created_at_utc TEXT NOT NULL
    )
    """)
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_executed_signals_signal_id_action ON executed_signals(signal_id, action)")

    # oco links
    cur.execute("""
    CREATE TABLE IF NOT EXISTS oco_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        link_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        tp_order_id TEXT NOT NULL,
        sl_order_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        created_at_utc TEXT NOT NULL
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS ix_oco_links_status ON oco_links(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_oco_links_symbol ON oco_links(symbol)")

    # ✅ trade history (for Auto-Scaler metrics)
    # One row per executed BUY. Later closed by OCO TP/SL or by SELL signal.
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

    conn.commit()
    conn.close()
