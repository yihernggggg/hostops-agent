"""Deterministic pre-screen for prompt injection in guest messages.

This is a tripwire, not the defence. The real defences are structural: guest text is wrapped in
delimited tags as data, and governance enforces policy after the agent, whatever the model was told.
A flagged message gets `injection_suspected="true"` in the prompt, a note on the incident, and a
visible flag for the human approver.
"""

from __future__ import annotations

import re

_PATTERNS = [
    r"\b(ignore|disregard|forget|override)\b.{0,30}\b(instructions?|rules|prompt|guidelines|policy|policies)\b",
    r"\byou are now\b",
    r"\b(developer|admin|god|jailbreak) mode\b",
    r"(^|\n)\s*(system|assistant|developer)\s*:",
    r"\bsystem prompt\b",
    r"</?\s*(guest_message|system_alert|system|instructions?)\s*>",
    r"\bact as (an? )?(admin|administrator|host|system)\b",
    r"\b(approve|issue|process)\b.{0,30}\brefund\b.{0,40}\bwithout\b",
]
_REGEX = re.compile("|".join(f"(?:{p})" for p in _PATTERNS), re.IGNORECASE)


def looks_like_injection(text: str) -> bool:
    return _REGEX.search(text) is not None
