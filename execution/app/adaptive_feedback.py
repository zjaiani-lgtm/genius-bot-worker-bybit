from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AdaptiveState:
    recent_winrate: float = 0.5


class AdaptiveFeedback:
    def __init__(self):
        self.state = AdaptiveState()

    def adjust_min_conf(self, base: float) -> float:
        wr = self.state.recent_winrate
        if wr < 0.45:
            return min(0.70, base + 0.03)
        if wr > 0.60:
            return max(0.40, base - 0.02)
        return base
