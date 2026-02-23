from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class PositionInfo:
    symbol: str
    size: float
    side: str  # long/short/none


class PositionManager:
    def __init__(self, ex, logger):
        self.ex = ex
        self.logger = logger

    def list_positions(self) -> List[PositionInfo]:
        positions = []
        raw = self.ex.fetch_positions_safe()
        for p in raw:
            sym = p.get("symbol")
            contracts = p.get("contracts") or p.get("contractSize") or p.get("size") or 0
            side = p.get("side") or "none"
            try:
                size = float(contracts)
            except Exception:
                size = 0.0
            if sym:
                positions.append(PositionInfo(symbol=sym, size=size, side=str(side)))
        return positions

    def open_positions_count(self) -> int:
        return sum(1 for p in self.list_positions() if abs(p.size) > 0)

    def has_position(self, symbol: str) -> bool:
        return any(p.symbol == symbol and abs(p.size) > 0 for p in self.list_positions())
