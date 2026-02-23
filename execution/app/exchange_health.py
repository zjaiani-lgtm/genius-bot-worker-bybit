from __future__ import annotations

from dataclasses import dataclass


@dataclass
class HealthScore:
    ok: bool
    reason: str


def check_exchange_health(ex, logger) -> HealthScore:
    ok = ex.health_check()
    if ok:
        return HealthScore(True, "OK")
    return HealthScore(False, "FETCH_TIME_FAIL")
