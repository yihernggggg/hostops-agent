"""FastAPI service: events in, alerts webhook, incidents, approvals, chaos switches, metrics.

Deliberately thin. All decisions live in pipeline / agent / governance; this file wires HTTP to them.
Triage runs as a background task so intake stays fast even with a slow model.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from starlette.routing import Match

from hostops.agent.llm import make_provider
from hostops.agent.loop import TriageAgent
from hostops.agent.tools import ToolContext
from hostops.calendar_sync import CalendarSync, SyncResult
from hostops.chaos import ChaosSettings, ChaosState
from hostops.config import Settings
from hostops.governance import AuditLog, AuditRecord, Governance, GovernanceError
from hostops.models import Event, EventKind, Incident, IncidentStatus, ProposedAction
from hostops.pipeline import ActionNotFoundError, TriagePipeline
from hostops.retrieval import RunbookIndex
from hostops.store import IncidentNotFoundError, IncidentStore
from hostops.telemetry import HTTP_DURATION, REGISTRY, configure_logging, get_logger, log

_log = get_logger("hostops.service")
CHAOS_EXEMPT = ("/healthz", "/metrics", "/chaos")


# ---------------------------------------------------------------- request / response bodies


class EventIn(BaseModel):
    kind: EventKind = EventKind.GUEST_MESSAGE
    text: str = Field(min_length=1, max_length=4000)
    guest_name: str | None = None
    listing_id: str | None = None
    booking_id: str | None = None
    alertname: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class Accepted(BaseModel):
    incident_id: str
    merged: bool = False


class AlertIn(BaseModel):
    status: str
    labels: dict[str, str]
    annotations: dict[str, str] = Field(default_factory=dict)


class AlertmanagerWebhook(BaseModel):
    """Subset of Alertmanager's webhook payload (version 4) that we use."""

    status: str
    alerts: list[AlertIn]


class AlertsResult(BaseModel):
    created: list[str] = Field(default_factory=list)
    merged: list[str] = Field(default_factory=list)
    resolved: list[str] = Field(default_factory=list)


class Approval(BaseModel):
    incident_id: str
    action: ProposedAction


class ApproveIn(BaseModel):
    actor: str = Field(min_length=1, max_length=100)


class RejectIn(ApproveIn):
    reason: str = Field(min_length=1, max_length=500)


class ChaosUpdate(BaseModel):
    calendar_sync_failing: bool | None = None
    llm_latency_s: float | None = Field(default=None, ge=0, le=60)
    http_error_rate: float | None = Field(default=None, ge=0, le=1)


class Health(BaseModel):
    status: str
    provider: str


# ---------------------------------------------------------------- app factory


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)

    chaos = ChaosState()
    store = IncidentStore()
    audit = AuditLog(settings.audit_path)
    index = RunbookIndex.load()
    agent = TriageAgent(
        make_provider(settings, chaos),
        lambda inc: ToolContext(index=index, incident_id=inc.id),
        audit=audit,
        max_steps=settings.max_agent_steps,
    )
    pipeline = TriagePipeline(store, agent, Governance(audit))
    sync = CalendarSync(chaos)
    rng = random.Random()  # noqa: S311  # chaos sampling, not security

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stop = threading.Event()
        if settings.sync_interval_s > 0:

            def loop() -> None:
                while not stop.wait(settings.sync_interval_s):
                    sync.run()

            threading.Thread(target=loop, name="calendar-sync", daemon=True).start()
        yield
        stop.set()

    app = FastAPI(title="hostops-agent", version="0.1.0", lifespan=lifespan)

    def triage_safely(incident_id: str) -> None:
        try:
            pipeline.triage(incident_id)
        except Exception:  # a crash here must not take the worker down; the incident stays open
            _log.exception("triage failed", extra={"fields": {"incident_id": incident_id}})

    # ------------------------------------------------------------ middleware

    def route_template(request: Request) -> str:
        for route in app.router.routes:
            match, _ = route.matches(request.scope)
            if match is Match.FULL:
                return str(getattr(route, "path", request.url.path))
        return "unmatched"

    @app.middleware("http")
    async def timing_and_chaos(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        start = time.perf_counter()
        route = route_template(request)
        if not route.startswith(CHAOS_EXEMPT) and chaos.should_fail_http(rng):
            response: Response = JSONResponse({"detail": "chaos: injected failure"}, status_code=500)
            log(_log, logging.ERROR, "injected http failure", route=route, component="chaos")
        else:
            response = await call_next(request)
        HTTP_DURATION.labels(route=route, status=str(response.status_code)).observe(
            time.perf_counter() - start
        )
        return response

    @app.exception_handler(IncidentNotFoundError)
    @app.exception_handler(ActionNotFoundError)
    async def not_found(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": f"not found: {exc}"}, status_code=404)

    @app.exception_handler(GovernanceError)
    async def conflict(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    # ------------------------------------------------------------ routes

    @app.get("/healthz")
    def healthz() -> Health:
        return Health(status="ok", provider=agent.provider.name)

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    @app.post("/events", status_code=202)
    def post_event(body: EventIn, tasks: BackgroundTasks) -> Accepted:
        try:
            event = Event.model_validate(body.model_dump())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if event.kind is EventKind.SYSTEM_ALERT:
            incident, merged = pipeline.ingest_alert(event)
        else:
            incident, merged = pipeline.ingest(event), False
        if not merged:
            tasks.add_task(triage_safely, incident.id)
        return Accepted(incident_id=incident.id, merged=merged)

    @app.post("/alerts")
    def post_alerts(body: AlertmanagerWebhook, tasks: BackgroundTasks) -> AlertsResult:
        result = AlertsResult()
        for alert in body.alerts:
            name = alert.labels.get("alertname")
            if not name:
                continue
            if alert.status == "resolved":
                resolved = pipeline.resolve_alert(name)
                if resolved:
                    result.resolved.append(resolved.id)
                continue
            text = alert.annotations.get("summary") or alert.annotations.get("description") or name
            event = Event(kind=EventKind.SYSTEM_ALERT, text=text[:4000], alertname=name, labels=alert.labels)
            incident, merged = pipeline.ingest_alert(event)
            if merged:
                result.merged.append(incident.id)
            else:
                result.created.append(incident.id)
                tasks.add_task(triage_safely, incident.id)
        return result

    @app.get("/incidents")
    def list_incidents(status: IncidentStatus | None = None) -> list[Incident]:
        return store.list_all(status)

    @app.get("/incidents/{incident_id}")
    def get_incident(incident_id: str) -> Incident:
        incident = store.get(incident_id)
        if incident is None:
            raise IncidentNotFoundError(incident_id)
        return incident

    @app.get("/incidents/{incident_id}/audit")
    def get_audit(incident_id: str) -> list[AuditRecord]:
        get_incident(incident_id)
        return audit.read(incident_id)

    @app.get("/approvals")
    def approvals() -> list[Approval]:
        return [Approval(incident_id=i, action=a) for i, a in store.pending_actions()]

    @app.post("/incidents/{incident_id}/actions/{action_id}/approve")
    def approve(incident_id: str, action_id: str, body: ApproveIn) -> ProposedAction:
        return pipeline.approve(incident_id, action_id, actor=body.actor)

    @app.post("/incidents/{incident_id}/actions/{action_id}/reject")
    def reject(incident_id: str, action_id: str, body: RejectIn) -> ProposedAction:
        return pipeline.reject(incident_id, action_id, actor=body.actor, reason=body.reason)

    @app.get("/chaos")
    def get_chaos() -> ChaosSettings:
        return chaos.snapshot()

    @app.put("/chaos")
    def put_chaos(body: ChaosUpdate) -> ChaosSettings:
        updated = chaos.update(**body.model_dump())
        log(_log, logging.WARNING, "chaos updated", component="chaos", **updated.model_dump())
        return updated

    @app.post("/chaos/reset")
    def reset_chaos() -> ChaosSettings:
        chaos.reset()
        return chaos.snapshot()

    @app.post("/calendar-sync/run")
    def run_sync() -> SyncResult:
        return sync.run()

    return app


app = create_app()
