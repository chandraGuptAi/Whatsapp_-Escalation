"""Escalation-specific streaming client; never modifies the AI Box client or prompts."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import random
from contextvars import ContextVar
from urllib.parse import urlsplit
from openai import AsyncOpenAI
from ..errors import EscalationError
from .cancellation import check_cancelled, AnalysisCancelled
from .observe import stats

logger = logging.getLogger("escalation_analyst.ai")
request_options: ContextVar[dict] = ContextVar("escalation_provider_options", default={})


class Provider:
    def __init__(self, settings, reports):
        self.settings, self.reports = settings, reports
        # Bounds provider calls across every concurrent master worker and dashboard job.
        self.slots = threading.BoundedSemaphore(settings.ai_max_concurrency)
        self.rate_lock = threading.Lock()
        self.next_request = 0.0

    def _pace(self):
        with self.rate_lock:
            delay = max(0.0, self.next_request - time.monotonic())
            self.next_request = max(self.next_request, time.monotonic()) + self.settings.ai_min_interval
        self._pause(delay)

    def _acquire(self) -> None:
        while not self.slots.acquire(timeout=0.5):
            check_cancelled()

    def _pause(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            check_cancelled()
            time.sleep(min(0.5, max(0.0, end - time.monotonic())))

    def _backoff(self, attempt: int, error: Exception) -> float:
        # Honour a provider's Retry-After (rate limits); otherwise back off linearly.
        headers = getattr(getattr(error, "response", None), "headers", None) or {}
        try:
            return min(60.0, float(headers.get("retry-after")))
        except (TypeError, ValueError):
            return min(60.0, 2 ** attempt * self.settings.ai_retry_backoff * random.uniform(0.8, 1.2))

    async def _stream(self, system: str, user: str) -> tuple[str, object]:
        options = request_options.get()
        kwargs = dict(reasoning_effort=self.settings.reasoning, max_tokens=self.settings.max_tokens)
        if options:
            kwargs["max_tokens"] = options.get("max_tokens", self.settings.max_tokens)
            # Verified only for the official DeepSeek v4 Chat Completions endpoint.
            if urlsplit(self.settings.base_url).hostname == "api.deepseek.com" and self.settings.model == "deepseek-v4-pro":
                effort = options.get("reasoning")
                if effort in {"low", "high", "max"}:
                    kwargs.update(reasoning_effort=effort, extra_body={"thinking": {"type": "enabled"}})
                elif effort == "none":
                    kwargs.pop("reasoning_effort", None)
                    kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            if options.get("json") and urlsplit(self.settings.base_url).hostname == "api.deepseek.com":
                kwargs["response_format"] = {"type": "json_object"}
        async with AsyncOpenAI(
            api_key=self.settings.api_key, base_url=self.settings.base_url, max_retries=0, timeout=300
        ) as client:
            # Bound initial thinking, each subsequent chunk, and the entire request separately.
            stream = await asyncio.wait_for(
                client.chat.completions.create(
                    model=self.settings.model,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    stream=True,
                    stream_options={"include_usage": True},
                    **kwargs,
                ),
                300,
            )
            try:
                pieces, usage = [], None
                iterator = stream.__aiter__()
                first = True
                while True:
                    try:
                        chunk = await asyncio.wait_for(anext(iterator), 300 if first else 120)
                    except StopAsyncIteration:
                        break
                    first = False
                    if chunk.usage:
                        usage = chunk.usage
                    for choice in chunk.choices:
                        if choice.delta.content:
                            pieces.append(choice.delta.content)
                        if choice.finish_reason == "length":
                            raise ValueError("Escalation output reached the configured token limit")
                return "".join(pieces), usage
            finally:
                await stream.close()

    def generate(self, system: str, user: str, meta: dict) -> str:
        check_cancelled()
        if not self.settings.api_key or self.settings.api_key.startswith("your_"):
            raise EscalationError(503, "Escalation AI key is not configured")
        error = None
        attempts = request_options.get().get("max_retries", self.settings.ai_max_retries) + 1
        run = stats()
        for attempt in range(attempts):
            self._acquire()
            started = time.monotonic()
            response_received = False
            try:
                self._pace()

                async def bounded():
                    task = asyncio.create_task(asyncio.wait_for(self._stream(system, user), self.settings.ai_timeout))
                    try:
                        while not task.done():
                            check_cancelled()
                            await asyncio.wait({task}, timeout=0.5)
                        check_cancelled()
                        return await task
                    finally:
                        if not task.done():
                            task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

                text, usage = asyncio.run(bounded())
                response_received = True
                if not text.strip():
                    raise ValueError("Empty AI response")
                if run:
                    run.add("ai_request", time.monotonic() - started)
                    if usage:
                        run.count("input_tokens", usage.prompt_tokens)
                        run.count("output_tokens", usage.completion_tokens)
                        details = getattr(usage, "completion_tokens_details", None)
                        reasoning_tokens = getattr(details, "reasoning_tokens", None)
                        if reasoning_tokens is not None:
                            run.count("reasoning_tokens", reasoning_tokens)
                if usage:
                    usd = (
                        usage.prompt_tokens / 1e6 * self.settings.price_input
                        + usage.completion_tokens / 1e6 * self.settings.price_output
                    )
                    options = request_options.get()
                    if options:
                        options["usage"] = dict(input_tokens=usage.prompt_tokens, output_tokens=usage.completion_tokens,
                            reasoning_tokens=getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None),
                            estimated_cost_usd=usd, retry_count=attempt, model_name=self.settings.model)
                    self.reports.usage(
                        dict(
                            **meta,
                            model=self.settings.model,
                            input_tokens=usage.prompt_tokens,
                            output_tokens=usage.completion_tokens,
                            cost_usd=usd,
                            cost_inr=usd * self.settings.usd_inr,
                            reasoning_tokens=getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", None),
                            latency_ms=round((time.monotonic() - started) * 1000, 3),
                            retry_count=attempt,
                        )
                    )
                return text
            except AnalysisCancelled:
                raise
            except Exception as exc:
                error = exc
                logger.warning("Escalation generation attempt %s failed: %s", attempt + 1, type(exc).__name__)
                if run:
                    run.add("ai_request", time.monotonic() - started)
                status = getattr(exc, "status_code", None)
                # A successful billable response followed by a persistence error must
                # never trigger another paid request. Invalid/truncated output needs review.
                if response_received or isinstance(exc, ValueError):
                    break
                if status is not None and status not in {408, 409, 429} and status < 500:
                    break
            finally:
                self.slots.release()
            if attempt < attempts - 1:  # Back off without holding a provider slot.
                check_cancelled()
                if run:
                    run.count("ai_retries")
                    if run.on_retry:
                        run.on_retry(attempt + 1, type(error).__name__)
                self._pause(self._backoff(attempt, error))
        raise EscalationError(502, "Escalation AI generation failed; retry the analysis.") from error
