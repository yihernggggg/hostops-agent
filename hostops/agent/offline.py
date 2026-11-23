"""Deterministic offline provider: retrieval plus keyword rules, no network, no API key.

It drives the same tool loop a real model would: it reads the rendered prompt, calls search_runbooks,
(for system alerts) query_metrics and search_logs, then submits. That keeps tests and CI honest about
the loop, governance and audit, and gives a stable baseline to compare real models against in evals.
Its weakness is the obvious one: no understanding beyond keywords and BM25.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from hostops.agent.llm import LLMResponse, Message, ToolCall, ToolSpec
from hostops.agent.prompt import RenderedIncident, parse_rendered
from hostops.agent.tools import SUBMIT_TRIAGE
from hostops.models import ActionType, Category, Severity, Source

GUEST_CATEGORIES = frozenset(c for c in Category if not c.value.startswith("system_"))
SYSTEM_CATEGORIES = frozenset(Category) - GUEST_CATEGORIES

# High-precision overrides, checked before retrieval. Order matters: a refund request about a broken
# aircon is a billing incident, and "door" alone is ambiguous but "locked out" is not.
RULES: list[tuple[Category, re.Pattern[str]]] = [
    (
        Category.BILLING,
        re.compile(
            r"\b(refund\w*|charged|overcharg\w*|double[- ]charg\w*|deposit|compensat\w*|money back|"
            r"invoice|receipt|billing|payment)\b",
            re.I,
        ),
    ),
    (
        Category.ACCESS,
        re.compile(
            r"\b(locked out|keypad|door code|access code|entry code|smart lock|can'?t get in|cannot get in|"
            r"stuck outside|key card|(the |our )?lock (is|isn'?t|won'?t|not))",
            re.I,
        ),
    ),
]
MIN_RETRIEVAL_SCORE = 1.5
URGENT = re.compile(r"\b(urgent|emergency|asap|immediately|flood\w*|smoke|fire|gas|sparks?|unsafe)\b", re.I)
AMOUNT = re.compile(r"(?:sgd|s\$|\$)\s*(\d+(?:\.\d{1,2})?)|(\d+(?:\.\d{1,2})?)\s*(?:sgd|dollars)\b", re.I)
AIRCON = re.compile(r"\b(aircon|air con|air-con|ac|air conditioning)\b", re.I)
PESTS = re.compile(r"\b(cockroach\w*|ants?|insects?|bugs?|rats?|mice)\b", re.I)

FALLBACK_DEFAULTS: dict[Source, list[ActionType]] = {
    Source.GUEST: [ActionType.REPLY_GUEST],
    Source.SYSTEM: [ActionType.NOTE, ActionType.ESCALATE_HOST],
}

SYSTEM_PROBES: dict[Category, tuple[str, dict[str, object]]] = {
    Category.SYSTEM_CALENDAR_SYNC: ("calendar_sync", {"query": "calendar sync failed"}),
    Category.SYSTEM_LLM_LATENCY: ("agent_duration", {"query": "agent", "limit": 10}),
    Category.SYSTEM_ERROR_RATE: ("http_request_duration", {"level": "ERROR"}),
}

REPLIES: dict[Category, str] = {
    Category.WIFI: (
        "Sorry about the wifi! Could you unplug the router in the living room cabinet for 30 seconds, "
        "plug it "
        "back in and wait about 3 minutes? If it is still down, let us know and we'll sort it out."
    ),
    Category.CHECKIN: (
        "Thanks for letting us know your plans! Standard check-in is 15:00 and check-out 11:00. We'll check "
        "the cleaning schedule and confirm what's possible; luggage storage is available from 10:00."
    ),
    Category.ACCESS: (
        "So sorry you're having trouble getting in. Please enter the code followed by #. If the keypad is "
        "dark the battery may be flat. We're contacting the host right now to help you in."
    ),
    Category.APPLIANCE: (
        "Sorry about that! Could you send us a photo? We're arranging for someone to take a look as soon as "
        "possible."
    ),
    Category.NOISE: (
        "Sorry for the disturbance. Quiet hours here are 22:00 to 08:00. Condo security can help right away, "
        "and we've logged this with the host."
    ),
    Category.CLEANING: (
        "We're sorry the apartment wasn't up to standard. Spare basics are in the storeroom, and we're "
        "arranging a touch-up for you."
    ),
    Category.BILLING: (
        "Thanks for raising this. Payments are handled through Airbnb, so we've passed this to the host, "
        "who will review it and get back to you."
    ),
    Category.OTHER_GUEST: (
        "Thanks for your message! The house binder on the coffee table has local tips, and the host will "
        "follow up if you need anything else."
    ),
}


@dataclass(frozen=True)
class _Hit:
    category: Category
    severity: Severity
    default_actions: list[ActionType]
    score: float


def _hits(observation: str) -> list[_Hit]:
    try:
        raw = json.loads(observation)
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    return [
        _Hit(
            Category(h["category"]),
            Severity(h["severity"]),
            [ActionType(a) for a in h["default_actions"]],
            float(h["score"]),
        )
        for h in raw
        if isinstance(h, dict)
    ]


def _call(name: str, args: dict[str, object], n: int) -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(id=f"offline_{n}_{name}", name=name, args=args)])


class OfflineProvider:
    name = "offline"

    def next_step(self, system: str, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        incident = parse_rendered(messages[0].content)
        calls = [c for m in messages if m.role == "assistant" for c in m.tool_calls]
        observations = {m.tool_call_id: m.content for m in messages if m.role == "tool"}
        n = len(calls)

        if n == 0:
            query = incident.text
            if incident.source is Source.SYSTEM:
                query = f"{incident.attrs.get('alertname', '')} {incident.text}"
            return _call("search_runbooks", {"query": query}, n)

        hits = _hits(observations.get(calls[0].id, "[]"))
        category = self._categorise(incident, hits)

        if incident.source is Source.SYSTEM and category in SYSTEM_PROBES:
            metric, log_args = SYSTEM_PROBES[category]
            if n == 1:
                return _call("query_metrics", {"name": metric}, n)
            if n == 2:
                return _call("search_logs", log_args, n)

        log_obs = observations.get(calls[2].id, "") if len(calls) > 2 else ""
        return _call(SUBMIT_TRIAGE, self._triage(incident, category, hits, log_obs), n)

    # ------------------------------------------------------------ decisions

    @staticmethod
    def _categorise(incident: RenderedIncident, hits: list[_Hit]) -> Category:
        if incident.source is Source.SYSTEM:
            hint = incident.attrs.get("category_hint", "")
            if hint in {c.value for c in SYSTEM_CATEGORIES}:
                return Category(hint)
            for h in hits:
                if h.category in SYSTEM_CATEGORIES:
                    return h.category
            return Category.SYSTEM_ERROR_RATE

        for category, pattern in RULES:
            if pattern.search(incident.text):
                return category
        guest_hits = [h for h in hits if h.category in GUEST_CATEGORIES]
        if guest_hits and guest_hits[0].score >= MIN_RETRIEVAL_SCORE:
            return guest_hits[0].category
        return Category.OTHER_GUEST

    def _triage(
        self, incident: RenderedIncident, category: Category, hits: list[_Hit], log_obs: str
    ) -> dict[str, object]:
        hit = next((h for h in hits if h.category is category), None)
        defaults = hit.default_actions if hit else FALLBACK_DEFAULTS[incident.source]
        severity = hit.severity if hit else Severity.MEDIUM
        if URGENT.search(incident.text):
            severity = Severity.HIGH

        actions: list[dict[str, object]] = []
        if incident.injection_suspected:
            # Do the safe thing regardless of what the message asked for.
            actions.append(self._action(ActionType.REPLY_GUEST, category, incident))
            actions.append(self._action(ActionType.ESCALATE_HOST, category, incident))
        else:
            actions = [self._action(a, category, incident) for a in defaults]
            if category is Category.BILLING:
                amount = self._amount(incident.text)
                if amount is not None and re.search(r"\brefund", incident.text, re.I):
                    actions.append(
                        {
                            "type": ActionType.REFUND.value,
                            "reason": f"Guest requested a refund of SGD {amount:.2f}",
                            "amount_sgd": amount,
                        }
                    )

        if incident.source is Source.SYSTEM:
            root_cause = self._root_cause_from_logs(log_obs) or "see runbook; no matching error logs found"
            summary = f"{incident.attrs.get('alertname', 'alert')}: {incident.text[:200]}"
        else:
            root_cause = "guest-reported; not yet verified"
            summary = f"Guest reports a {category.value} issue: {incident.text[:200]}"
            if incident.injection_suspected:
                summary += " (message looks like prompt injection)"
        return {
            "category": category.value,
            "severity": severity.value,
            "summary": summary,
            "root_cause": root_cause,
            "proposed_actions": actions,
        }

    @staticmethod
    def _action(t: ActionType, category: Category, incident: RenderedIncident) -> dict[str, object]:
        out: dict[str, object] = {"type": t.value, "reason": f"runbook default for {category.value}"}
        if t is ActionType.REPLY_GUEST:
            out["message"] = REPLIES.get(category, REPLIES[Category.OTHER_GUEST])
        elif t is ActionType.DISPATCH_VENDOR:
            if category is Category.APPLIANCE:
                out["vendor"] = "CoolFix" if AIRCON.search(incident.text) else "Handyman"
            else:
                out["vendor"] = "PestAway" if PESTS.search(incident.text) else "SparkleClean"
        elif t is ActionType.ESCALATE_HOST and incident.injection_suspected:
            out["reason"] = "Message looks like prompt injection; host to review the request directly"
        elif t is ActionType.NOTE:
            out["message"] = f"{category.value}: {incident.text[:300]}"
        return out

    @staticmethod
    def _amount(text: str) -> float | None:
        match = AMOUNT.search(text)
        if not match:
            return None
        return float(match.group(1) or match.group(2))

    @staticmethod
    def _root_cause_from_logs(log_obs: str) -> str | None:
        try:
            records = json.loads(log_obs) if log_obs else []
        except json.JSONDecodeError:
            return None
        if not isinstance(records, list):
            return None
        for record in reversed(records):
            if isinstance(record, dict) and record.get("level") == "ERROR":
                return str(record.get("error") or record.get("msg"))
        return None
