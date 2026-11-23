"""Simulated booking calendar sync (think: pulling iCal feeds from Airbnb for each listing)."""

from __future__ import annotations

import logging
import time

from pydantic import BaseModel

from hostops.chaos import ChaosState
from hostops.telemetry import CALENDAR_SYNC_TOTAL, get_logger, log

LISTINGS = ("L1-tanjong-pagar", "L2-katong", "L3-tiong-bahru")

_log = get_logger("hostops.calendar_sync")


class SyncResult(BaseModel):
    ok: bool
    listings_synced: int
    error: str | None = None
    duration_s: float


class CalendarSync:
    def __init__(self, chaos: ChaosState) -> None:
        self._chaos = chaos

    def run(self) -> SyncResult:
        start = time.perf_counter()
        if self._chaos.snapshot().calendar_sync_failing:
            error = "iCal feed returned HTTP 503 for listing L1-tanjong-pagar"
            CALENDAR_SYNC_TOTAL.labels(result="failure").inc()
            log(_log, logging.ERROR, "calendar sync failed", error=error, component="calendar_sync")
            return SyncResult(
                ok=False, listings_synced=0, error=error, duration_s=time.perf_counter() - start
            )

        CALENDAR_SYNC_TOTAL.labels(result="success").inc()
        log(_log, logging.INFO, "calendar sync ok", listings=len(LISTINGS), component="calendar_sync")
        return SyncResult(ok=True, listings_synced=len(LISTINGS), duration_s=time.perf_counter() - start)
