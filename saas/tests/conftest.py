from __future__ import annotations

import pytest

from app.core.config import Settings


@pytest.fixture
def settings() -> Settings:
    return make_settings()


def make_settings() -> Settings:
    return Settings(
        env="test", database_url="postgresql://unused", db_pool_min=1, db_pool_max=2, redis_url="",
        jwt_secret="x" * 32, api_key_pepper="p" * 32, data_encryption_key="", access_token_minutes=15,
        refresh_token_days=30, rate_limit_per_minute=0, disabled_modules=frozenset(), deepseek_api_key="",
        deepseek_model="deepseek-v4-pro", deepseek_base_url="https://api.deepseek.com", gemini_api_key="",
        gemini_media_model="gemini-3.1-flash-lite", ai_max_input_tokens=60000, worker_concurrency=2,
        job_lease_seconds=60,
    )
