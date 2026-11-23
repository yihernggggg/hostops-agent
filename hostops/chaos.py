"""Failure switches, flipped via the API or the simulator to rehearse incidents."""

from __future__ import annotations

import random
import threading

from pydantic import BaseModel, Field


class ChaosSettings(BaseModel):
    calendar_sync_failing: bool = False
    llm_latency_s: float = Field(default=0.0, ge=0, le=60)
    http_error_rate: float = Field(default=0.0, ge=0, le=1)


class ChaosState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._settings = ChaosSettings()

    def snapshot(self) -> ChaosSettings:
        with self._lock:
            return self._settings.model_copy()

    def update(
        self,
        *,
        calendar_sync_failing: bool | None = None,
        llm_latency_s: float | None = None,
        http_error_rate: float | None = None,
    ) -> ChaosSettings:
        with self._lock:
            merged = self._settings.model_dump()
            changes = {
                "calendar_sync_failing": calendar_sync_failing,
                "llm_latency_s": llm_latency_s,
                "http_error_rate": http_error_rate,
            }
            merged.update({k: v for k, v in changes.items() if v is not None})
            self._settings = ChaosSettings.model_validate(merged)
            return self._settings.model_copy()

    def reset(self) -> None:
        with self._lock:
            self._settings = ChaosSettings()

    def should_fail_http(self, rng: random.Random) -> bool:
        rate = self.snapshot().http_error_rate
        return rate > 0 and rng.random() < rate
