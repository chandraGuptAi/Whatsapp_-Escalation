import asyncio
from dataclasses import replace
from contextlib import asynccontextmanager
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import Response

from integration.escalation_gateway import EscalationGateway, install
from app.main import build_app
from app.escalation_analyst.settings import Settings


def test_service_requires_postgres_when_no_explicit_test_settings(monkeypatch):
    monkeypatch.setenv("ESCALATION_DATABASE_URL", "")
    with pytest.raises(RuntimeError, match="ESCALATION_DATABASE_URL"):
        build_app(service_token="private-secret")


def test_service_secret_identity_and_existing_api(tmp_path):
    settings = replace(Settings(), database_url="", sqlite_path=str(tmp_path / "service.db"),
                       whatsapp_autostart=False, scheduler_enabled=False)
    app = build_app(settings, service_token="private-secret")
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/escalation/groups", headers={"X-User-Id": "a"}).status_code == 401
        client.headers["X-Escalation-Service-Token"] = "private-secret"
        assert client.get("/api/escalation/groups").status_code == 401
        assert client.get("/api/escalation/groups", headers={"X-User-Id": "a"}).json() == {"groups": []}
        assert client.get("/api/escalation/whatsapp/status", headers={"X-User-Id": "a"}).status_code == 200


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def test_proxy_preserves_contract(method):
    seen = []

    async def upstream(request):
        seen.append((request, await request.aread()))
        class ExportStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"\x00binary-export\xff"
        return httpx.Response(207, stream=ExportStream(), headers={
            "Content-Disposition": "attachment; filename=report.xlsx", "X-Request-Id": "test-id",
            "Connection": "x-private", "x-private": "remove-me"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            gateway = EscalationGateway("http://127.0.0.1:8201", "real-secret", 30, client)
            host = FastAPI()
            host.mount("/api/escalation", gateway)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host), base_url="http://main:8200") as caller:
                response = await caller.request(method, "/api/escalation/groups/a%40g.us?x=1&x=2&name=a%2Bb",
                    content=b"raw-body", headers={"X-User-Id": "operator", "X-Ingest-Secret": "ingest",
                                                 "X-Escalation-Service-Token": "forged"})
            assert response.status_code == 207
            if method != "HEAD":
                assert response.content == b"\x00binary-export\xff"
            assert response.headers["content-disposition"].endswith("report.xlsx")
            assert "x-private" not in response.headers
        request, body = seen[0]
        assert request.url.raw_path == b"/api/escalation/groups/a%40g.us?x=1&x=2&name=a%2Bb"
        assert body == b"raw-body"
        assert request.headers["X-User-Id"] == "operator"
        assert request.headers["X-Ingest-Secret"] == "ingest"
        assert request.headers["X-Escalation-Service-Token"] == "real-secret"

    asyncio.run(run())


@pytest.mark.parametrize("error,code", [(httpx.ConnectError("offline"), 502), (httpx.ReadTimeout("slow"), 504)])
def test_service_failure_isolated(error, code):
    async def upstream(request):
        raise error

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            host = FastAPI()
            host.mount("/api/escalation", EscalationGateway("http://private:8201", "token", 30, client))
            @host.get("/health")
            def health():
                return {"status": "ok"}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host), base_url="http://main") as caller:
                assert (await caller.get("/api/escalation/groups")).status_code == code
                assert (await caller.get("/health")).status_code == 200
    asyncio.run(run())


def test_remote_install_preserves_lifespan_without_local_runtime(monkeypatch):
    import builtins
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name.startswith("app.escalation_analyst"):
            raise AssertionError("Gateway must not import local escalation")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setenv("ESCALATION_SERVICE_URL", "http://127.0.0.1:8201")
    monkeypatch.setenv("ESCALATION_SERVICE_TOKEN", "token")
    events = []
    @asynccontextmanager
    async def lifespan(app):
        events.append("start")
        yield {"original": True}
        events.append("stop")
    host = FastAPI(lifespan=lifespan)
    gateway = install(host)
    with TestClient(host) as caller:
        assert gateway.client is not None
        assert caller.app_state["original"] is True
    assert gateway.client is None
    assert events == ["start", "stop"]


def test_fail_closed_without_service_secret(monkeypatch):
    monkeypatch.setenv("ESCALATION_SERVICE_URL", "http://127.0.0.1:8201")
    monkeypatch.delenv("ESCALATION_SERVICE_TOKEN", raising=False)
    with pytest.raises(ValueError):
        install(FastAPI())
    with pytest.raises(RuntimeError):
        build_app(service_token="")


def test_gateway_to_real_service_routes(tmp_path):
    service = build_app(replace(Settings(), database_url="", sqlite_path=str(tmp_path / "remote.db"),
                               whatsapp_autostart=False, scheduler_enabled=False), service_token="private")
    async def run():
        async with service.router.lifespan_context(service):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=service)) as upstream:
                main = FastAPI()
                main.mount("/api/escalation", EscalationGateway("http://private:8201", "private", 30, upstream))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main), base_url="http://main:8200") as caller:
                    assert (await caller.get("/api/escalation/groups")).status_code == 401
                    response = await caller.get("/api/escalation/groups", headers={"X-User-Id": "operator"})
                    assert response.status_code == 200
                    assert response.json() == {"groups": []}
                    assert (await caller.get("/api/escalation/unknown", headers={"X-User-Id": "operator"})).status_code == 404
    asyncio.run(run())
