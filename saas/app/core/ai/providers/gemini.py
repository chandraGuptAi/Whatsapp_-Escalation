"""Gemini for media-to-text only (images, documents). Reports never go through Gemini."""
from __future__ import annotations

import base64

import httpx

_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiMediaProvider:
    name = "gemini"

    def __init__(self, api_key: str, model: str, *, timeout: float = 60):
        self.api_key, self.model, self.timeout = api_key, model, timeout

    async def media_to_text(self, data: bytes, mime_type: str, instruction: str) -> tuple[str, dict]:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY must be configured")
        body = {"contents": [{"parts": [
            {"text": instruction},
            {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(data).decode()}},
        ]}]}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(_URL.format(model=self.model), json=body,
                                         headers={"x-goog-api-key": self.api_key})
            response.raise_for_status()
        payload = response.json()
        parts = payload.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        usage = payload.get("usageMetadata", {})
        return "".join(p.get("text", "") for p in parts).strip(), {
            "tokens_in": usage.get("promptTokenCount", 0),
            "tokens_out": usage.get("candidatesTokenCount", 0),
        }
