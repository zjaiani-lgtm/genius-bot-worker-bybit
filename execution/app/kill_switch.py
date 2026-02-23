from __future__ import annotations

from dataclasses import dataclass

from .logger import log


@dataclass
class KillState:
    loss_streak: int = 0
    peak_equity: float = 0.0


class KillSwitch:
    def __init__(self, max_drawdown: float, max_loss_streak: int, logger):
        self.max_drawdown = max_drawdown
        self.max_loss_streak = max_loss_streak
        self.logger = logger
        self.state = KillState()

    def update_equity(self, equity: float) -> None:
        if equity <= 0:
            return
        if self.state.peak_equity <= 0:
            self.state.peak_equity = equity
        self.state.peak_equity = max(self.state.peak_equity, equity)

    def mark_trade(self, pnl: float) -> None:
        self.state.loss_streak = (self.state.loss_streak + 1) if pnl < 0 else 0

    def blocked(self, equity: float) -> bool:
        if equity > 0:
            self.update_equity(equity)
            dd = (self.state.peak_equity - equity) / self.state.peak_equity if self.state.peak_equity else 0.0
            if dd >= self.max_drawdown:
                log(self.logger, "ERROR", "KILL_DRAWDOWN", drawdown=dd, equity=equity, peak=self.state.peak_equity)
                return True

        if self.state.loss_streak >= self.max_loss_streak:
            log(self.logger, "ERROR", "KILL_LOSS_STREAK", loss_streak=self.state.loss_streak)
            return True

        return False
