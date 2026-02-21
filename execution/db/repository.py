# execution/db/repository.py
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Tuple, Optional

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
    if row is None:
        return None
    try:
        return tuple(row)  # important: makes bootstrap/state parsing consistent
    except Exception:
        return row


def update_system_state(status: str = None, startup_sync_ok: int = None, kill_switch: int = None):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT id, status, startup_sync_ok, kill_switch, updated_at_utc FROM system_state WHERE id=1")
    row = cur.fetchone()

    if not row:
        cur.execute(
            "INSERT INTO system_state (id, status, startup_sync_ok, kill_switch, updated_at_utc) "
            "VALUES (1, 'RUNNING', 1, 0, ?)",
            (_utc_now_iso(),),
        )
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
    return int((row[0] if row else 0) or 0)


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


def mark_signal_id_executed(
    signal_id: str,
    signal_hash: str = None,
    action: str = "EXECUTE",
    symbol: str = None,
):
    """
    Engine calls: mark_signal_id_executed(signal_id, signal_hash=..., action=..., symbol=...)
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR IGNORE INTO executed_signals (signal_id, signal_hash, action, symbol, created_at_utc)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            str(signal_id),
            str(signal_hash) if signal_hash else None,
            str(action),
            str(symbol) if symbol else None,
            _utc_now_iso(),
        ),
    )
    conn.commit()
    conn.close()


# -------------------------
# oco links (engine-compatible)
# -------------------------
def create_oco_link(
    signal_id: str,
    symbol: str,
    base_asset: str = None,
    tp_order_id: str = None,
    sl_order_id: str = None,
    tp_price: float = None,
    sl_stop_price: float = None,
    sl_limit_price: float = None,
    amount: float = None,
):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO oco_links
        (link_id, signal_id, symbol, base_asset, tp_order_id, sl_order_id,
         tp_price, sl_stop_price, sl_limit_price, amount, status, created_at_utc, updated_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
        """,
        (
            str(signal_id),  # legacy
            str(signal_id),  # engine
            str(symbol),
            str(base_asset) if base_asset else None,
            str(tp_order_id or ""),
            str(sl_order_id or ""),
            float(tp_price) if tp_price is not None else None,
            float(sl_stop_price) if sl_stop_price is not None else None,
            float(sl_limit_price) if sl_limit_price is not None else None,
            float(amount) if amount is not None else None,
            _utc_now_iso(),
            _utc_now_iso(),
        ),
    )
    conn.commit()
    conn.close()


def set_oco_status(link_id: int, status: str):
    """
    Engine passes link_id as integer primary key (the first column returned from list_active_oco_links).
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE oco_links SET status=?, updated_at_utc=? WHERE id=?",
        (str(status), _utc_now_iso(), int(link_id)),
    )
    conn.commit()
    conn.close()


def list_active_oco_links(limit: int = 50) -> List[Tuple]:
    """
    Must return tuples in this exact order for engine unpack:
    (
      id, signal_id, symbol, base_asset,
      tp_order_id, sl_order_id,
      tp_price, sl_stop_price, sl_limit_price,
      amount, status, created_at_utc, updated_at_utc
    )
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            id,
            signal_id,
            symbol,
            base_asset,
            tp_order_id,
            sl_order_id,
            tp_price,
            sl_stop_price,
            sl_limit_price,
            amount,
            status,
            created_at_utc,
            updated_at_utc
        FROM oco_links
        WHERE status='open'
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(limit),),
    )
    rows = cur.fetchall()
    conn.close()

    out: List[Tuple] = []
    for r in rows or []:
        try:
            out.append(tuple(r))
        except Exception:
            out.append(r)
    return out


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
# trade_history (core)
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


def _close_trade_by_id(
    trade_id: int,
    exit_price: float,
    close_reason: str,
    pnl_quote: float = 0.0,
    pnl_pct: float = 0.0,
    exit_order_id: str = None,
):
    conn = get_connection()
    cur = conn.cursor()
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


# -------------------------
# trade_history (engine-compatible API)
# -------------------------
def open_trade(signal_id: str, symbol: str, qty: float, quote_in: float, entry_price: float):
    """
    Engine calls: open_trade(signal_id=..., symbol=..., qty=..., quote_in=..., entry_price=...)
    We store qty in base_amount and quote_in in quote_amount.
    """
    create_trade_open(
        signal_id=str(signal_id),
        symbol=str(symbol),
        quote_amount=float(quote_in),
        entry_price=float(entry_price),
        base_amount=float(qty),
        entry_order_id=None,
    )


def get_trade(signal_id: str):
    """
    Engine expects tuple:
    (signal_id, symbol, qty, quote_in, entry_price, opened_at, exit_price, closed_at, outcome, pnl_quote, pnl_pct)
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            signal_id,
            symbol,
            base_amount AS qty,
            quote_amount AS quote_in,
            entry_price,
            opened_at,
            exit_price,
            closed_at,
            close_reason AS outcome,
            pnl_quote,
            pnl_pct
        FROM trade_history
        WHERE signal_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (str(signal_id),),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    try:
        return tuple(row)
    except Exception:
        return row


def close_trade(signal_id: str, exit_price: float, outcome: str, pnl_quote: float = 0.0, pnl_pct: float = 0.0):
    """
    Engine calls: close_trade(signal_id, exit_price=..., outcome="TP/SL", pnl_quote=..., pnl_pct=...)
    We close the latest OPEN trade row for that signal_id.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id
        FROM trade_history
        WHERE signal_id=? AND status='OPEN'
        ORDER BY id DESC
        LIMIT 1
        """,
        (str(signal_id),),
    )
    r = cur.fetchone()
    conn.close()
    if not r:
        return

    trade_id = int(r[0])
    _close_trade_by_id(
        trade_id=trade_id,
        exit_price=float(exit_price),
        close_reason=str(outcome),
        pnl_quote=float(pnl_quote),
        pnl_pct=float(pnl_pct),
    )


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


def get_trade_stats() -> Dict[str, Any]:
    """
    Used by execution/main.py PERF_REPORT
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            COUNT(1) AS closed_trades,
            SUM(CASE WHEN COALESCE(pnl_quote, 0) > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN COALESCE(pnl_quote, 0) <= 0 THEN 1 ELSE 0 END) AS losses,
            SUM(COALESCE(pnl_quote, 0)) AS pnl_quote_sum,
            SUM(COALESCE(quote_amount, 0)) AS quote_in_sum,
            SUM(CASE WHEN COALESCE(pnl_quote, 0) > 0 THEN COALESCE(pnl_quote, 0) ELSE 0 END) AS gross_profit,
            ABS(SUM(CASE WHEN COALESCE(pnl_quote, 0) < 0 THEN COALESCE(pnl_quote, 0) ELSE 0 END)) AS gross_loss
        FROM trade_history
        WHERE status='CLOSED'
        """
    )
    row = cur.fetchone()
    conn.close()

    closed = int((row["closed_trades"] if row else 0) or 0)
    wins = int((row["wins"] if row else 0) or 0)
    losses = int((row["losses"] if row else 0) or 0)
    pnl_sum = float((row["pnl_quote_sum"] if row else 0.0) or 0.0)
    quote_sum = float((row["quote_in_sum"] if row else 0.0) or 0.0)
    gross_profit = float((row["gross_profit"] if row else 0.0) or 0.0)
    gross_loss = float((row["gross_loss"] if row else 0.0) or 0.0)

    winrate = (wins / closed * 100.0) if closed > 0 else 0.0
    roi = (pnl_sum / quote_sum * 100.0) if quote_sum > 0 else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

    return {
        "closed_trades": closed,
        "wins": wins,
        "losses": losses,
        "winrate_pct": winrate,
        "roi_pct": roi,
        "pnl_quote_sum": pnl_sum,
        "quote_in_sum": quote_sum,
        "profit_factor": profit_factor,
    }
