from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class RiskPlan:
    amount: float
    sl: float
    tp: float
    meta: dict


def build_risk_plan(
    side: str,
    last: float,
    atr_pct: float,
    quote_per_trade: float,
    fixed_amount: float = 0.0,
    sl_atr_mult: float = 1.5,
    tp_atr_mult: float = 2.0,
) -> Optional[RiskPlan]:
    if not last or last <= 0:
        return None

    amount = fixed_amount if fixed_amount and fixed_amount > 0 else (quote_per_trade / last)

    atr_dist = (atr_pct / 100.0) * last
    sl_dist = max(atr_dist * sl_atr_mult, last * 0.001)
    tp_dist = max(atr_dist * tp_atr_mult, last * 0.0015)

    if side.upper() == "BUY":
        sl = last - sl_dist
        tp = last + tp_dist
    else:
        sl = last + sl_dist
        tp = last - tp_dist

    return RiskPlan(
        amount=float(amount),
        sl=float(sl),
        tp=float(tp),
        meta={"atr_pct": atr_pct, "sl_atr_mult": sl_atr_mult, "tp_atr_mult": tp_atr_mult},
    )
