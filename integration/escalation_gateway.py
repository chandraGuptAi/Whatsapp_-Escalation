"""Install in the main backend as app/api_gateway.py.

No escalation runtime, native SDK, database or scheduler is imported in remote mode.
"""
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI
from starlette.responses import JSONResponse, StreamingResponse


HOP_HEADERS = {b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization",
               b"te", b"trailer", b"transfer-encoding", b"upgrade", b"host"}


def filtered_headers(raw):
    connection_tokens = {token.strip().lower() for key, value in raw if key.lower() == b"connection"
                         for token in value.split(b",")}
    return [(key, value) for key, value in raw if key.lower() not in HOP_HEADERS | connection_tokens]


class EscalationGateway:
    def __init__(self, base_url, token, timeout, client=None):
        self.base_url, self.token, self.timeout = base_url.rstrip("/"), token, timeout
        self.client = client

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        if self.client is None:
            return await JSONResponse({"error": "Escalation gateway is not ready"}, 503)(scope, receive, send)
        # Preserve encoded path/query exactly, including repeated query parameters.
        raw_path = scope.get("raw_path", scope["path"].encode()).split(b"?", 1)[0]
        url = httpx.URL(self.base_url).copy_with(raw_path=raw_path +
                     (b"?" + scope["query_string"] if scope.get("query_string") else b""))
        headers = [(k, v) for k, v in filtered_headers(scope["headers"])
                   if k.lower() != b"x-escalation-service-token"]
        headers.append((b"x-escalation-service-token", self.token.encode()))

        async def body():
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    raise httpx.ReadError("Caller disconnected")
                if message.get("body"):
                    yield message["body"]
                if not message.get("more_body", False):
                    break

        try:
            request = self.client.build_request(scope["method"], url, headers=headers, content=body(), timeout=self.timeout)
            upstream = await self.client.send(request, stream=True, follow_redirects=False)
        except httpx.TimeoutException:
            return await JSONResponse({"error": "Escalation service timed out"}, 504)(scope, receive, send)
        except httpx.RequestError:
            return await JSONResponse({"error": "Escalation service unavailable"}, 502)(scope, receive, send)

        async def stream():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        response = StreamingResponse(stream(), status_code=upstream.status_code)
        response.raw_headers = filtered_headers(upstream.headers.raw)
        # Rewrite private redirects to the original public path.
        response.raw_headers = [(k, v.replace(self.base_url.encode(), b"", 1)
                                 if k.lower() == b"location" and v.startswith(self.base_url.encode() + b"/") else v)
                                for k, v in response.raw_headers]
        try:
            await response(scope, receive, send)
        finally:
            await upstream.aclose()


def install(host: FastAPI):
    base_url = os.getenv("ESCALATION_SERVICE_URL", "").strip().rstrip("/")
    if not base_url:
        # Preserve the current deployment until remote mode is explicitly enabled.
        from app.escalation_analyst.application import install as install_local
        return install_local(host)
    parsed = urlsplit(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username:
        raise ValueError("ESCALATION_SERVICE_URL must be an HTTP(S) origin, e.g. http://127.0.0.1:8201")
    token = os.getenv("ESCALATION_SERVICE_TOKEN", "").strip()
    if not token:
        raise ValueError("ESCALATION_SERVICE_TOKEN is required in remote mode")
    timeout = httpx.Timeout(float(os.getenv("ESCALATION_GATEWAY_TIMEOUT_SECONDS", "1800")), connect=5.0)
    gateway = EscalationGateway(base_url, token, timeout)
    previous_lifespan = host.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with previous_lifespan(app) as state:
            async with httpx.AsyncClient(trust_env=False) as client:
                gateway.client = client
                try:
                    yield state
                finally:
                    gateway.client = None

    host.router.lifespan_context = lifespan
    host.mount("/api/escalation", gateway, name="escalation_gateway")
    return gateway
