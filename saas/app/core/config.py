"""Every setting comes from the environment; no other module reads os.environ."""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be configured")
    return value


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Settings:
    env: str
    database_url: str
    db_pool_min: int
    db_pool_max: int
    redis_url: str
    jwt_secret: str
    api_key_pepper: str
    data_encryption_key: str
    access_token_minutes: int
    refresh_token_days: int
    rate_limit_per_minute: int
    disabled_modules: frozenset[str]
    deepseek_api_key: str
    deepseek_model: str
    deepseek_base_url: str
    gemini_api_key: str
    gemini_media_model: str
    ai_max_input_tokens: int
    worker_concurrency: int
    job_lease_seconds: int
    wa_store_database: str = "data/wa-store.sqlite3"
    whatsapp_history_limit: int = 50
    media_max_bytes: int = 6 * 1024 * 1024

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
        return cls(
            env=os.getenv("APP_ENV", "dev"),
            database_url=_required("DATABASE_URL"),
            db_pool_min=_int("DB_POOL_MIN", 2),
            db_pool_max=_int("DB_POOL_MAX", 20),
            redis_url=os.getenv("REDIS_URL", ""),
            jwt_secret=_required("JWT_SECRET"),
            api_key_pepper=_required("API_KEY_PEPPER"),
            data_encryption_key=os.getenv("DATA_ENCRYPTION_KEY", ""),
            access_token_minutes=_int("ACCESS_TOKEN_MINUTES", 15),
            refresh_token_days=_int("REFRESH_TOKEN_DAYS", 30),
            rate_limit_per_minute=_int("RATE_LIMIT_PER_MINUTE", 120),
            disabled_modules=frozenset(
                key.strip() for key in os.getenv("DISABLED_MODULES", "").split(",") if key.strip()
            ),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", ""),
            deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"),
            deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
            gemini_media_model=os.getenv("GEMINI_MEDIA_MODEL", "gemini-3.1-flash-lite"),
            ai_max_input_tokens=_int("AI_MAX_INPUT_TOKENS", 60000),
            worker_concurrency=_int("WORKER_CONCURRENCY", 4),
            job_lease_seconds=_int("JOB_LEASE_SECONDS", 900),
            wa_store_database=os.getenv("WA_STORE_DATABASE", "data/wa-store.sqlite3"),
            whatsapp_history_limit=_int("WHATSAPP_HISTORY_LIMIT", 50),
            media_max_bytes=_int("MEDIA_MAX_BYTES", 6 * 1024 * 1024),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()
