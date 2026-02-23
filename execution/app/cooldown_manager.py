from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

from .utils import now_ts


@dataclass
class CooldownState:
    last_signal_ts: Dict[str, float] = field(default_factory=dict)
    last_loss_ts: Dict[str, float] = field(default_factory=dict)


class CooldownManager:
    def __init__(self, cooldown_seconds: int, post_loss_cooldown_seconds: int):
        self.cooldown_seconds = cooldown_seconds
        self.post_loss_cooldown_seconds = post_loss_cooldown_seconds
        self.state = CooldownState()

    def mark_signal(self, symbol: str) -> None:
        self.state.last_signal_ts[symbol] = now_ts()

    def mark_loss(self, symbol: str) -> None:
        self.state.last_loss_ts[symbol] = now_ts()

    def allowed(self, symbol: str) -> bool:
        t = now_ts()
        last_sig = self.state.last_signal_ts.get(symbol, 0.0)
        last_loss = self.state.last_loss_ts.get(symbol, 0.0)

        if t - last_sig < self.cooldown_seconds:
            return False
        if last_loss and (t - last_loss < self.post_loss_cooldown_seconds):
            return False
        return True
