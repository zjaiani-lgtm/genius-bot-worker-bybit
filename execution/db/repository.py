# execution/db/repository.py
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional, Iterable, Tuple

from execution.db.db import get_connection


def _utc_now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


# -------------------------
# system_state
# -------------------------
def get_system_state():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, status, startup_sync_ok, kill_switch, updated_at_utc FROM system_state WHERE id=1")
    row = cur.fetchone()
    conn.close()
    return row


def update_system_state(status: str = None, startup_sync_ok: int = None, kill_switch: int = None):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT id, status, startup_sync_ok, kill_switch, updated_at_utc FROM system_state WHERE id=1")
    row = cur.fetchone()

    if not row:
        cur.execute("INSERT INTO system_state (id, status, startup_sync_ok, kill_switch, updated_at_utc) VALUES (1, 'RUNNING', 1, 0, ?)", (_utc_now_iso(),))
        conn.commit()
        conn.close()
        return

    new_status = status if status is not None else row[1]
    new_sync = int(startup_sync_ok) if startup_sync_ok is not None else int(row[2])
    new_kill = int(kill_switch) if kill_switch is not None else int(row[3])

    cur.execute(
        "UPDATE system_state SET status=?, startup_sync_ok=?, kill_switch=?, updated_at_utc=? WHERE id=1",
        (new_status, new_sync, new_kill, _utc_now_iso()),
    )
    conn.commit()
    conn.close()


# -------------------------
# events
# -------------------------
def log_event(event_type: str, details: str = ""):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO events (event_type, details, created_at_utc) VALUES (?, ?, ?)",
        (str(event_type), str(details), _utc_now_iso()),
    )
    conn.commit()
    conn.close()


def count_recent_risk_events(event_types: Iterable[str], window_minutes: int = 60) -> int:
    # treat these as "risk alerts"
    since = datetime.utcnow() - timedelta(minutes=int(window_minutes))
    since_iso = since.isoformat() + "Z"

    types = [str(t).strip() for t in event_types if str(t).strip()]
    if not types:
        return 0

    placeholders = ",".join(["?"] * len(types))

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT COUNT(1) FROM events
        WHERE created_at_utc >= ?
          AND event_type IN ({placeholders})
        """,
        (since_iso, *types),
    )
    row = cur.fetchone()
    conn.close()
    return int(row[0] or 0)


# -------------------------
# executed_signals
# -------------------------
def signal_id_already_executed(signal_id: str, action: str = "EXECUTE") -> bool:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM executed_signals WHERE signal_id=? AND action=? LIMIT 1",
        (str(signal_id), str(action)),
    )
    row = cur.fetchone()
    conn.close()
    return row is not None


def mark_signal_id_executed(signal_id: str, action: str = "EXECUTE", symbol: str = None):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO executed_signals (signal_id, action, symbol, created_at_utc) VALUES (?, ?, ?, ?)",
        (str(signal_id), str(action), str(symbol) if symbol else None, _utc_now_iso()),
    )
    conn.commit()
    conn.close()


# -------------------------
# oco links
# -------------------------
def create_oco_link(link_id: str, symbol: str, tp_order_id: str, sl_order_id: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO oco_links (link_id, symbol, tp_order_id, sl_order_id, status, created_at_utc)
        VALUES (?, ?, ?, ?, 'open', ?)
        """,
        (str(link_id), str(symbol), str(tp_order_id), str(sl_order_id), _utc_now_iso()),
    )
    conn.commit()
    conn.close()


def set_oco_status(link_id: str, status: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE oco_links SET status=? WHERE link_id=?", (str(status), str(link_id)))
    conn.commit()
    conn.close()


def list_active_oco_links() -> List[Tuple]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, link_id, symbol, tp_order_id, sl_order_id, status, created_at_utc FROM oco_links WHERE status='open'")
    rows = cur.fetchall()
    conn.close()
    return rows or []


def has_active_oco_for_symbol(symbol: str) -> bool:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM oco_links WHERE status='open' AND symbol=? LIMIT 1",
        (str(symbol),),
    )
    row = cur.fetchone()
    conn.close()
    return row is not None


# -------------------------
# trade_history (Auto-Scaler metrics)
# -------------------------
def create_trade_open(
    signal_id: str,
    symbol: str,
    quote_amount: float,
    entry_price: float,
    base_amount: float = None,
    entry_order_id: str = None,
):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO trade_history
        (signal_id, symbol, quote_amount, base_amount, entry_price, entry_order_id, opened_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN')
        """,
        (
            str(signal_id),
            str(symbol),
            float(quote_amount),
            float(base_amount) if base_amount is not None else None,
            float(entry_price),
            str(entry_order_id) if entry_order_id else None,
            _utc_now_iso(),
        ),
    )
    conn.commit()
    conn.close()


def get_open_trade_by_signal(signal_id: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, signal_id, symbol, quote_amount, base_amount, entry_price, entry_order_id, opened_at, status
        FROM trade_history
        WHERE status='OPEN' AND signal_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (str(signal_id),),
    )
    row = cur.fetchone()
    conn.close()
    return row


def get_latest_open_trade_for_symbol(symbol: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, signal_id, symbol, quote_amount, base_amount, entry_price, entry_order_id, opened_at, status
        FROM trade_history
        WHERE status='OPEN' AND symbol=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (str(symbol),),
    )
    row = cur.fetchone()
    conn.close()
    return row


def close_trade(
    trade_id: int,
    exit_price: float,
    close_reason: str,
    exit_order_id: str = None,
):
    conn = get_connection()
    cur = conn.cursor()

    # fetch entry
    cur.execute(
        """
        SELECT id, quote_amount, entry_price
        FROM trade_history
        WHERE id=?
        """,
        (int(trade_id),),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return

    quote_amount = float(row[1] or 0.0)
    entry_price = float(row[2] or 0.0)
    exit_price = float(exit_price)

    # approx pnl
    pnl_pct = (exit_price - entry_price) / entry_price if entry_price > 0 else 0.0
    pnl_quote = quote_amount * pnl_pct

    cur.execute(
        """
        UPDATE trade_history
        SET status='CLOSED',
            close_reason=?,
            exit_price=?,
            exit_order_id=?,
            closed_at=?,
            pnl_quote=?,
            pnl_pct=?
        WHERE id=?
        """,
        (
            str(close_reason),
            float(exit_price),
            str(exit_order_id) if exit_order_id else None,
            _utc_now_iso(),
            float(pnl_quote),
            float(pnl_pct),
            int(trade_id),
        ),
    )
    conn.commit()
    conn.close()


def list_recent_closed_trades(limit: int = 20) -> List[Tuple]:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, signal_id, symbol, quote_amount, base_amount, entry_price, exit_price,
               pnl_quote, pnl_pct, close_reason, closed_at
        FROM trade_history
        WHERE status='CLOSED'
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(limit),),
    )
    rows = cur.fetchall()
    conn.close()
    return rows or []
