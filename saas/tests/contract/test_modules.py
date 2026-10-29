"""The module contract: every folder under app/modules is a valid, removable module."""
from __future__ import annotations

import pytest

from app.core.modules import Module, discover, job_handlers
from app.main import create_app

EXPECTED = ["groups", "logistics", "master_report", "performance", "quick_report"]


def test_every_module_folder_is_discovered():
    modules = discover()
    assert [m.key for m in modules] == EXPECTED
    assert all(isinstance(m, Module) for m in modules)


def test_only_groups_is_core():
    assert [m.key for m in discover() if m.core] == ["groups"]


def test_disabling_a_module_removes_it_and_its_jobs():
    modules = discover({"performance", "logistics"})
    assert "performance" not in [m.key for m in modules]
    assert all(module != "performance" for module, _ in job_handlers(modules))


def test_report_modules_register_generate_jobs():
    handlers = job_handlers(discover())
    for key in ("master_report", "quick_report", "performance", "logistics"):
        assert (key, "generate") in handlers
    assert ("groups", "media_extract") in handlers


def _paths(app) -> set[str]:
    return set(app.openapi()["paths"])


@pytest.mark.parametrize("module", EXPECTED)
def test_app_mounts_module_routes(settings, module):
    assert any(p.startswith(f"/v1/{module}") for p in _paths(create_app(settings)))


def test_app_without_a_module_has_no_routes_for_it(settings):
    from dataclasses import replace

    app = create_app(replace(settings, disabled_modules=frozenset({"logistics"})))
    assert not any(p.startswith("/v1/logistics") for p in _paths(app))


def test_every_module_route_requires_auth(settings):
    from fastapi.testclient import TestClient

    app = create_app(settings)
    app.router.lifespan_context = None  # no DB needed: auth fails before any query
    client = TestClient(app)
    for path in ("/v1/groups", "/v1/reports", "/v1/me"):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "unauthorized"
