"""In-memory incident store.

One lock guards every read and write. Callers get deep copies, so nothing outside
the store can mutate shared state without going through a method (and the lock).
Swapping this for Postgres later only touches this file.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from hostops.models import (
    ActionStatus,
    Event,
    Incident,
    IncidentStatus,
    ProposedAction,
    TriageResult,
)


class IncidentNotFoundError(KeyError):
    pass


class IncidentStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._incidents: dict[str, Incident] = {}

    def create(self, event: Event) -> Incident:
        incident = Incident(source=event.source, event=event, alertname=event.alertname)
        with self._lock:
            self._incidents[incident.id] = incident
            return incident.model_copy(deep=True)

    def get(self, incident_id: str) -> Incident | None:
        with self._lock:
            found = self._incidents.get(incident_id)
            return found.model_copy(deep=True) if found else None

    def list_all(self, status: IncidentStatus | None = None) -> list[Incident]:
        with self._lock:
            items = [i for i in self._incidents.values() if status is None or i.status is status]
            return [i.model_copy(deep=True) for i in sorted(items, key=lambda i: i.created_at)]

    def find_open_by_alertname(self, alertname: str) -> Incident | None:
        with self._lock:
            for inc in self._incidents.values():
                if inc.alertname == alertname and inc.status is not IncidentStatus.RESOLVED:
                    return inc.model_copy(deep=True)
            return None

    def bump_occurrences(self, incident_id: str) -> int:
        with self._lock:
            inc = self._require(incident_id)
            inc.occurrences += 1
            inc.updated_at = datetime.now(UTC)
            return inc.occurrences

    def set_status(self, incident_id: str, status: IncidentStatus) -> None:
        with self._lock:
            inc = self._require(incident_id)
            inc.status = status
            inc.updated_at = datetime.now(UTC)

    def set_triage(self, incident_id: str, triage: TriageResult) -> None:
        with self._lock:
            inc = self._require(incident_id)
            inc.triage = triage
            inc.updated_at = datetime.now(UTC)

    def add_actions(self, incident_id: str, actions: list[ProposedAction]) -> None:
        with self._lock:
            inc = self._require(incident_id)
            inc.actions.extend(a.model_copy(deep=True) for a in actions)
            inc.updated_at = datetime.now(UTC)

    def replace_action(self, incident_id: str, action: ProposedAction) -> None:
        with self._lock:
            inc = self._require(incident_id)
            for idx, existing in enumerate(inc.actions):
                if existing.id == action.id:
                    inc.actions[idx] = action.model_copy(deep=True)
                    inc.updated_at = datetime.now(UTC)
                    return
            raise IncidentNotFoundError(f"action {action.id} not on incident {incident_id}")

    def pending_actions(self) -> list[tuple[str, ProposedAction]]:
        with self._lock:
            return [
                (inc.id, a.model_copy(deep=True))
                for inc in sorted(self._incidents.values(), key=lambda i: i.created_at)
                for a in inc.actions
                if a.status is ActionStatus.PENDING_APPROVAL
            ]

    def _require(self, incident_id: str) -> Incident:
        try:
            return self._incidents[incident_id]
        except KeyError:
            raise IncidentNotFoundError(incident_id) from None
