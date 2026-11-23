"""Settings, from environment variables only (prefix HOSTOPS_). No config files, no secrets in code."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ProviderName = Literal["offline", "anthropic", "openai"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HOSTOPS_", extra="ignore")

    provider: ProviderName = "offline"
    anthropic_model: str = "claude-opus-5-5"
    # Opus 5.5 defaults to medium; set explicitly so the eval baseline is reproducible.
    anthropic_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    # Any OpenAI-compatible endpoint. Ollama serves one at /v1.
    openai_base_url: str = "http://localhost:11434/v1"
    openai_model: str = "llama3.1"
    llm_timeout_s: float = 30.0

    max_agent_steps: int = 6
    audit_path: Path = Path("audit.jsonl")
    log_buffer_size: int = 5000
    log_level: str = "INFO"
    # Background calendar sync cadence; 0 disables the loop (tests, evals).
    sync_interval_s: float = 15.0
