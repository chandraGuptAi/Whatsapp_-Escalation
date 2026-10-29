"""DeepSeek through its OpenAI-compatible API, JSON output only."""
from __future__ import annotations

import json


class DeepSeekProvider:
    name = "deepseek"

    def __init__(self, api_key: str, model: str, base_url: str, *, timeout: float = 180):
        self.model = model
        self._args = {"api_key": api_key, "base_url": base_url, "timeout": timeout}
        self._client = None

    def _get_client(self):
        if self._client is None:
            if not self._args["api_key"]:
                raise RuntimeError("DEEPSEEK_API_KEY must be configured")
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(**self._args)
        return self._client

    async def complete_json(self, system_prompt: str, user_content: str) -> tuple[dict, dict]:
        text, usage = await self._complete(system_prompt, user_content, {"type": "json_object"})
        return json.loads(text or "{}"), usage

    async def complete_text(self, system_prompt: str, user_content: str) -> tuple[str, dict]:
        return await self._complete(system_prompt, user_content, None)

    async def _complete(self, system_prompt: str, user_content: str, response_format: dict | None):
        extra = {"response_format": response_format} if response_format else {}
        response = await self._get_client().chat.completions.create(
            model=self.model,
            temperature=0,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}],
            **extra,
        )
        usage = response.usage
        return response.choices[0].message.content or "", {
            "tokens_in": getattr(usage, "prompt_tokens", 0) or 0,
            "tokens_out": getattr(usage, "completion_tokens", 0) or 0,
        }
