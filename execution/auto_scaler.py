import os
import logging
from dataclasses import dataclass
from typing import List, Optional, Set

from execution.db.repository import (
    list_recent_closed_trades,
    count_recent_risk_events,
    get_system_state,
)

logger = logging.getLogger("gbm")


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "y", "on")


def _env_float(name: str, default: str) -> float:
    try:
        return float(os.getenv(name, default))
    except Exception:
        return float(default)


def _env_int(name: str, default: str) -> int:
    try:
        return int(float(os.getenv(name, default)))
    except Exception:
        return int(float(default))


def _env_csv(name: str, default: str = "") -> List[str]:
    raw = os.getenv(name, default).strip()
    if not raw:
        return []
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


@dataclass
class AutoScalerConfig:
    enabled: bool
    start_size: int
    max_size: int
    winrate_up: float
    winrate_down: float
    dd_down_limit: float
    dd_up_limit: float
    min_trades: int
    lookback: int
    risk_spike_count: int
    risk_spike_window_min: int
    risk_spike_event_types: List[str]
    universe: List[str]

    @staticmethod
    def from_env() -> "AutoScalerConfig":
        enabled = _env_bool("AUTO_SCALER_ENABLED", "false")
        start_size = _env_int("AUTO_SCALER_START_SIZE", "2")
        max_size = _env_int("AUTO_SCALER_MAX_SIZE", "5")
        winrate_up = _env_float("AUTO_SCALER_WINRATE_UP", "0.55")
        winrate_down = _env_float("AUTO_SCALER_WINRATE_DOWN", "0.45")

        dd_down = _env_float("AUTO_SCALER_DD_LIMIT", "0.04")
        dd_up = _env_float("AUTO_SCALER_DD_UP", str(max(0.0, dd_down / 2.0)))

        min_trades = _env_int("AUTO_SCALER_MIN_TRADES", "5")
        lookback = _env_int("AUTO_SCALER_LOOKBACK_TRADES", "20")

        risk_spike_count = _env_int("AUTO_SCALER_RISK_SPIKE_COUNT", "0")
        risk_spike_window = _env_int("AUTO_SCALER_RISK_SPIKE_WINDOW_MIN", "60")
        risk_events = _env_csv(
            "AUTO_SCALER_RISK_SPIKE_EVENTS",
            "EXEC_REJECT_MIN_NOTIONAL,OCO_INVALID,EXEC_BLOCKED_KILL_SWITCH_LAST_GATE",
        )

        universe = _env_csv("AUTO_SCALER_UNIVERSE", "")
        if not universe:
            universe = _env_csv("SYMBOL_WHITELIST", "")
        if not universe:
            universe = _env_csv("BOT_SYMBOLS", "")

        return AutoScalerConfig(
            enabled=enabled,
            start_size=max(1, start_size),
            max_size=max(1, max_size),
            winrate_up=winrate_up,
            winrate_down=winrate_down,
            dd_down_limit=dd_down,
            dd_up_limit=dd_up,
            min_trades=max(1, min_trades),
            lookback=max(1, lookback),
            risk_spike_count=max(0, risk_spike_count),
            risk_spike_window_min=max(1, risk_spike_window),
            risk_spike_event_types=risk_events,
            universe=universe,
        )


@dataclass
class AutoScalerMetrics:
    trades: int
    win_rate: float
    max_drawdown: float
    risk_spike: bool
    system_health_ok: bool


def _prioritize_universe(universe: List[str]) -> List[str]:
    """Keep original order but force BTC/ETH first when present."""
    if not universe:
        return []
    u = [s.upper() for s in universe]
    pri = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    out: List[str] = []
    seen: Set[str] = set()
    for p in pri:
        if p in u and p not in seen:
            out.append(p)
            seen.add(p)
    for s in u:
        if s not in seen:
            out.append(s)
            seen.add(s)
    return out


def _compute_metrics(cfg: AutoScalerConfig, exchange_diag_ok: Optional[bool] = None) -> AutoScalerMetrics:
    rows = list_recent_closed_trades(limit=cfg.lookback)
    rows = list(reversed(rows))  # equity curve order

    trades = len(rows)
    wins = 0
    total_quote = 0.0
    cum_pnl = 0.0
    peak_return = 0.0
    max_dd = 0.0

    for r in rows:
        # (id, signal_id, symbol, quote_amount, entry_price, exit_price, pnl_quote, pnl_pct, close_reason, closed_at)
        quote_amount = float(r[3] or 0.0)
        pnl_quote = float(r[6] or 0.0)

        total_quote += max(0.0, quote_amount)
        cum_pnl += pnl_quote

        ret = (cum_pnl / total_quote) if total_quote > 0 else 0.0
        peak_return = max(peak_return, ret)
        max_dd = max(max_dd, peak_return - ret)

        if pnl_quote > 0:
            wins += 1

    win_rate = (wins / trades) if trades > 0 else 0.0

    risk_spike = False
    if cfg.risk_spike_count > 0:
        try:
            n = count_recent_risk_events(cfg.risk_spike_event_types, window_minutes=cfg.risk_spike_window_min)
            risk_spike = n >= cfg.risk_spike_count
        except Exception as e:
            logger.warning(f"AUTO_SCALER_RISK_SPIKE_CHECK_FAIL | err={e}")
            risk_spike = True  # safe

    system_health_ok = True
    try:
        st = get_system_state()
        if isinstance(st, (list, tuple)) and len(st) >= 4:
            status = str(st[1] or "").upper()
            startup_sync_ok = int(st[2] or 0)
            kill_switch = int(st[3] or 0)
            if kill_switch == 1 or startup_sync_ok == 0 or status not in ("RUNNING", "ACTIVE"):
                system_health_ok = False
        else:
            system_health_ok = False
    except Exception:
        system_health_ok = False

    if exchange_diag_ok is not None and exchange_diag_ok is False:
        system_health_ok = False

    return AutoScalerMetrics(
        trades=trades,
        win_rate=float(win_rate),
        max_drawdown=float(max_dd),
        risk_spike=bool(risk_spike),
        system_health_ok=bool(system_health_ok),
    )


class AutoScaler:
    """Production-safe basket scaler (Phase 1)."""

    def __init__(self, config: Optional[AutoScalerConfig] = None):
        self.cfg = config or AutoScalerConfig.from_env()
        self.universe = _prioritize_universe(self.cfg.universe)

    def metrics(self, exchange_diag_ok: Optional[bool] = None) -> AutoScalerMetrics:
        return _compute_metrics(self.cfg, exchange_diag_ok=exchange_diag_ok)

    def target_basket_size(self, m: AutoScalerMetrics) -> int:
        cfg = self.cfg

        if not cfg.enabled:
            return min(cfg.max_size, len(self.universe))

        if (not m.system_health_ok) or m.risk_spike:
            return min(cfg.start_size, len(self.universe))

        if m.trades < cfg.min_trades:
            return min(cfg.start_size, len(self.universe))

        if (m.win_rate < cfg.winrate_down) or (m.max_drawdown > cfg.dd_down_limit):
            return min(cfg.start_size, len(self.universe))

        if (m.win_rate >= cfg.winrate_up) and (m.max_drawdown <= cfg.dd_up_limit):
            return min(cfg.max_size, len(self.universe))

        return min(cfg.start_size, len(self.universe))

    def active_symbols(self, exchange_diag_ok: Optional[bool] = None) -> List[str]:
        if not self.universe:
            return []
        m = self.metrics(exchange_diag_ok=exchange_diag_ok)
        n = self.target_basket_size(m)
        active = self.universe[: max(1, n)]
        logger.info(
            f"AUTO_SCALER | enabled={self.cfg.enabled} universe={len(self.universe)} active={len(active)} "
            f"trades={m.trades} win_rate={m.win_rate:.3f} max_dd={m.max_drawdown:.3f} "
            f"risk_spike={int(m.risk_spike)} health_ok={int(m.system_health_ok)}"
        )
        return active
