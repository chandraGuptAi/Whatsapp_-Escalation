"""Builds the shared services once per process (API or worker)."""
from __future__ import annotations

from .ai import AIGateway
from .ai.providers import DeepSeekProvider, GeminiMediaProvider
from .config import Settings
from .security import Cipher


def build_services(settings: Settings) -> dict:
    cipher = Cipher(settings.data_encryption_key) if settings.data_encryption_key else None
    deepseek = DeepSeekProvider(settings.deepseek_api_key, settings.deepseek_model, settings.deepseek_base_url)
    return {
        "settings": settings,
        "cipher": cipher,
        "ai": AIGateway(deepseek, cipher, settings.api_key_pepper, settings.ai_max_input_tokens),
        "media": GeminiMediaProvider(settings.gemini_api_key, settings.gemini_media_model),
    }
