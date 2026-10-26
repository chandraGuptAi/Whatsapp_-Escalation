"""Module settings; imports never connect or change AI Box settings."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path


def env(name: str, default: str = "") -> str:
    return os.getenv(name, "").strip() or default


@dataclass(frozen=True)
class Settings:
    engine_mode: str = field(default_factory=lambda: env("ESCALATION_ENGINE_MODE", "legacy"))
    hybrid_enabled: bool = field(default_factory=lambda: env("ESCALATION_HYBRID_ENABLED", "false").lower() == "true")
    hybrid_incremental_enabled: bool = field(default_factory=lambda: env("ESCALATION_INCREMENTAL_ENABLED", "false").lower() == "true")
    ai_routing_enabled: bool = field(default_factory=lambda: env("ESCALATION_AI_ROUTING_ENABLED", "false").lower() == "true")
    hybrid_light_reasoning: str = field(default_factory=lambda: env("ESCALATION_LIGHT_REASONING", ""))
    hybrid_deep_reasoning: str = field(default_factory=lambda: env("ESCALATION_COMPLEX_REASONING", ""))
    hybrid_max_tokens: int = field(default_factory=lambda: max(256, int(env("ESCALATION_HYBRID_MAX_TOKENS", "8192"))))
    hybrid_deep_awbs: int = field(default_factory=lambda: max(2, int(env("ESCALATION_DEEP_AWB_THRESHOLD", "4"))))
    ai_min_interval: float = field(default_factory=lambda: max(0, float(env("ESCALATION_AI_MIN_INTERVAL_SECONDS", "0"))))
    shadow_enabled: bool = field(default_factory=lambda: env("ESCALATION_SHADOW_ENABLED", "false").lower() == "true")

    def __post_init__(self):
        if self.master_group_reasoning not in {"", "low", "high", "max"}:
            raise ValueError("MASTER_GROUP_REASONING must be blank, low, high or max")
        if self.engine_mode not in {"legacy", "shadow", "hybrid"}:
            raise ValueError("ESCALATION_ENGINE_MODE must be legacy, shadow or hybrid")
        if self.engine_mode == "hybrid" and not self.hybrid_enabled:
            raise ValueError("Hybrid mode requires ESCALATION_HYBRID_ENABLED=true")
    whatsapp_autostart: bool = field(default_factory=lambda: env("WHATSAPP_AUTOSTART", "false").lower() == "true")
    whatsapp_session_path: str = field(default_factory=lambda: env("WHATSAPP_SESSION_PATH", str(Path(__file__).resolve().parents[2] / "data" / "whatsapp-session.sqlite3")))
    whatsapp_history_limit: int = field(default_factory=lambda: int(env("WHATSAPP_HISTORY_LIMIT", "50")))
    database_url: str = field(default_factory=lambda: env("ESCALATION_DATABASE_URL", ""))
    sqlite_path: str = field(
        default_factory=lambda: env(
            "ESCALATION_SQLITE_PATH", str(Path(__file__).resolve().parents[2] / "data" / "escalation.sqlite3")
        )
    )
    ingest_secret: str = field(default_factory=lambda: env("ESCALATION_INGEST_SECRET", ""), repr=False)
    api_key: str = field(
        default_factory=lambda: env(
            "ESCALATION_DEEPSEEK_API_KEY", env("DEEPSEEK_API_KEY", env("Ai_box_key", ""))
        ),
        repr=False,
    )
    model: str = field(default_factory=lambda: env("ESCALATION_DEEPSEEK_MODEL", "deepseek-v4-pro"))
    base_url: str = field(
        default_factory=lambda: env("ESCALATION_DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    )
    max_tokens: int = field(default_factory=lambda: int(env("ESCALATION_DEEPSEEK_MAX_TOKENS", "80000")))
    reasoning: str = field(default_factory=lambda: env("ESCALATION_DEEPSEEK_REASONING", "max"))
    recipient: str = field(default_factory=lambda: env("RECIPIENT_NUMBER", ""))
    gemini_key: str = field(default_factory=lambda: env("GEMINI_API_KEY", ""), repr=False)
    gemini_model: str = field(default_factory=lambda: env("GEMINI_MEDIA_MODEL", "gemini-3.1-flash-lite"))
    concurrency: int = 4
    # Master runs: groups analysed at once, provider calls in flight (all escalation callers) and retries.
    master_concurrency: int = field(default_factory=lambda: max(1, int(env("MASTER_ANALYSIS_CONCURRENCY", "4"))))
    ai_max_concurrency: int = field(default_factory=lambda: max(1, int(env("MASTER_AI_MAX_CONCURRENCY", "4"))))
    # Blank inherits the normal provider effort. Only master group calls use this override.
    master_group_reasoning: str = field(default_factory=lambda: env("MASTER_GROUP_REASONING", ""))
    ai_max_retries: int = field(default_factory=lambda: max(0, int(env("MASTER_AI_MAX_RETRIES", "2"))))
    ai_retry_backoff: float = field(default_factory=lambda: float(env("MASTER_AI_RETRY_BACKOFF_SECONDS", "2")))
    ai_timeout: float = field(default_factory=lambda: float(env("ESCALATION_AI_TIMEOUT_SECONDS", "1500")))
    progress_poll_hint: float = field(default_factory=lambda: float(env("MASTER_PROGRESS_POLL_INTERVAL_HINT", "2")))
    incremental_enabled: bool = field(
        default_factory=lambda: env("MASTER_ANALYSIS_INCREMENTAL_ENABLED", "true").lower() == "true"
    )
    strict_validation: bool = field(
        default_factory=lambda: env("MASTER_ANALYSIS_STRICT_VALIDATION", "true").lower() == "true"
    )
    master_prompt_max_chars: int = field(default_factory=lambda: int(env("MASTER_PROMPT_MAX_CHARS", "600000")))
    # A running job whose heartbeat (every 10s) is older than this belongs to a dead worker.
    job_stale_seconds: int = field(default_factory=lambda: max(60, int(env("ESCALATION_JOB_STALE_SECONDS", "300"))))
    retention_days: int = field(default_factory=lambda: int(env("ESCALATION_RETENTION_DAYS", "30")))
    scheduler_enabled: bool = field(
        default_factory=lambda: env("ESCALATION_SCHEDULER_ENABLED", "false").lower() == "true"
    )
    price_input: float = 0.435
    price_output: float = 0.87
    usd_inr: float = 94
