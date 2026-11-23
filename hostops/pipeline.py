"""Orchestration: ingest an event, run the agent, apply governance, update the store.

Kept separate from the HTTP layer so evals and tests drive exactly the same path the service does.
"""

from __future__ import annotations

from hostops.agent.loop import TriageAgent
from hostops.governance import Governance
from hostops.models import ActionStatus, Event, EventKind, Incident, IncidentStatus, ProposedAction
from hostops.store import IncidentNotFoundError, IncidentStore
from hostops.telemetry import EVENTS_TOTAL, INCIDENTS_TOTAL, PENDING_APPROVALS


class ActionNotFoundError(KeyError):
    pass


class TriagePipeline:
    def __init__(self, store: IncidentStore, agent: TriageAgent, governance: Governance) -> None:
        self.store = store
        self.agent = agent
        self.governance = governance

    @property
    def provider_name(self) -> str:
        return self.agent.provider.name

    # ------------------------------------------------------------ intake

    def ingest(self, event: Event) -> Incident:
        EVENTS_TOTAL.labels(kind=event.kind.value).inc()
        incident = self.store.create(event)
        self.governance.audit.append(
            "incident_created",
            incident_id=incident.id,
            provider=self.provider_name,
            source=incident.source.value,
            kind=event.kind.value,
            alertname=event.alertname,
        )
        return incident

    def ingest_alert(self, event: Event) -> tuple[Incident, bool]:
        """Returns (incident, merged). A firing alert with an open incident of the same name is merged."""
        if event.kind is not EventKind.SYSTEM_ALERT or not event.alertname:
            raise ValueError("ingest_alert needs a system alert with an alertname")
        existing = self.store.find_open_by_alertname(event.alertname)
        if existing is None:
            return self.ingest(event), False
        EVENTS_TOTAL.labels(kind=event.kind.value).inc()
        count = self.store.bump_occurrences(existing.id)
        self.governance.audit.append(
            "alert_merged", incident_id=existing.id, provider=self.provider_name, occurrences=count
        )
        merged = self.store.get(existing.id)
        if merged is None:  # pragma: no cover - store never deletes
            raise IncidentNotFoundError(existing.id)
        return merged, True

    def resolve_alert(self, alertname: str) -> Incident | None:
        existing = self.store.find_open_by_alertname(alertname)
        if existing is None:
            return None
        self.store.set_status(existing.id, IncidentStatus.RESOLVED)
        self.governance.audit.append("alert_resolved", incident_id=existing.id, provider=self.provider_name)
        return self.store.get(existing.id)

    # ------------------------------------------------------------ triage

    def triage(self, incident_id: str) -> Incident:
        incident = self._require(incident_id)
        result = self.agent.run(incident)
        actions = self.governance.apply(
            incident.id, result.provider, result.proposed_actions, step=result.steps, prior=incident.actions
        )
        result = result.model_copy(update={"proposed_actions": actions})
        self.store.set_triage(incident.id, result)
        self.store.add_actions(incident.id, actions)
        self._refresh_status(incident.id, escalated=result.hit_step_cap)
        INCIDENTS_TOTAL.labels(source=incident.source.value, category=result.category.value).inc()
        self.governance.audit.append(
            "triage_complete",
            incident_id=incident.id,
            provider=result.provider,
            step=result.steps,
            category=result.category.value,
            actions={a.type.value: a.status.value for a in actions},
        )
        return self._require(incident.id)

    # ------------------------------------------------------------ human decisions

    def approve(self, incident_id: str, action_id: str, actor: str) -> ProposedAction:
        action = self._find_action(incident_id, action_id)
        prior = self._require(incident_id).actions
        decided = self.governance.approve(
            incident_id, action, actor=actor, provider=self.provider_name, prior=prior
        )
        return self._record_decision(incident_id, decided)

    def reject(self, incident_id: str, action_id: str, actor: str, reason: str) -> ProposedAction:
        action = self._find_action(incident_id, action_id)
        decided = self.governance.reject(
            incident_id, action, actor=actor, reason=reason, provider=self.provider_name
        )
        return self._record_decision(incident_id, decided)

    # ------------------------------------------------------------ internals

    def _record_decision(self, incident_id: str, action: ProposedAction) -> ProposedAction:
        self.store.replace_action(incident_id, action)
        self._refresh_status(incident_id)
        return action

    def _refresh_status(self, incident_id: str, escalated: bool = False) -> None:
        incident = self._require(incident_id)
        if incident.status is not IncidentStatus.RESOLVED:
            if any(a.status is ActionStatus.PENDING_APPROVAL for a in incident.actions):
                status = IncidentStatus.AWAITING_APPROVAL
            elif escalated or incident.status is IncidentStatus.ESCALATED:
                status = IncidentStatus.ESCALATED
            else:
                status = IncidentStatus.TRIAGED
            self.store.set_status(incident_id, status)
        PENDING_APPROVALS.set(len(self.store.pending_actions()))

    def _find_action(self, incident_id: str, action_id: str) -> ProposedAction:
        incident = self._require(incident_id)
        for action in incident.actions:
            if action.id == action_id:
                return action
        raise ActionNotFoundError(f"action {action_id} not found on incident {incident_id}")

    def _require(self, incident_id: str) -> Incident:
        incident = self.store.get(incident_id)
        if incident is None:
            raise IncidentNotFoundError(incident_id)
        return incident
