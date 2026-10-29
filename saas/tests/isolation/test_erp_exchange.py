"""ERP sign-in end to end: real PostgreSQL, real app, a fake ERP. Skipped unless both URLs are set
(see test_rls.py). The fake ERP answers like a "current user" API and accepts one token only."""
from __future__ import annotations

import base64
import os
import uuid
from dataclasses import replace

import httpx
import pytest

OWNER_URL, APP_URL = os.getenv("SAAS_TEST_OWNER_URL"), os.getenv("SAAS_TEST_APP_URL")
pytestmark = pytest.mark.skipif(not (OWNER_URL and APP_URL), reason="needs SAAS_TEST_OWNER_URL and SAAS_TEST_APP_URL")

GOOD, INACTIVE = "1656|good-token-abc", "1657|inactive-token"
CONFIG = {"user_id_path": "data.login_user_id", "name_path": "data.user_name", "phone_path": "data.MobileNo",
          "role_path": "data.role", "active_path": "data.is_active_user", "active_value": "1",
          "role_map": {"Super Admin": "owner"}}


def fake_erp(request: httpx.Request) -> httpx.Response:
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    if token == GOOD:
        return httpx.Response(200, json={"data": {"login_user_id": "1", "user_name": "ANKIT JAIN",
                                                  "MobileNo": "8800674795", "role": "Super Admin",
                                                  "is_active_user": "1"}})
    if token == INACTIVE:
        return httpx.Response(200, json={"data": {"login_user_id": "2", "is_active_user": "0"}})
    return httpx.Response(401, json={"message": "Unauthenticated."})


@pytest.fixture(scope="module")
def provider():
    import psycopg
    from psycopg.types.json import Jsonb

    tenant, slug = uuid.uuid4(), f"erp-{uuid.uuid4().hex[:8]}"
    with psycopg.connect(OWNER_URL, autocommit=True) as conn:
        conn.execute("INSERT INTO app.tenants (id, slug, name) VALUES (%s, %s, 'ERP tenant')", (tenant, slug))
        conn.execute("""INSERT INTO app.identity_providers (tenant_id, slug, name, verify_url, config)
                        VALUES (%s, %s, 'Test ERP', 'https://erp.test/api/me', %s)""", (tenant, slug, Jsonb(CONFIG)))
    yield slug, tenant
    with psycopg.connect(OWNER_URL, autocommit=True) as conn:
        conn.execute("DELETE FROM app.users WHERE identity_provider_id IN "
                     "(SELECT id FROM app.identity_providers WHERE tenant_id = %s)", (tenant,))
        conn.execute("DELETE FROM app.tenants WHERE id = %s", (tenant,))


SIMPLE_CONFIG = {"trust_request_identity": True, "user_id_path": "login_user_id", "name_path": "user_name",
                 "phone_path": "MobileNo", "active_path": "is_active_user", "active_value": "1"}
CLAIMED = {"login_user_id": "77", "user_name": "TEST USER", "MobileNo": "9999999999", "role": "Super Admin",
           "is_active_user": "1"}


@pytest.fixture(scope="module")
def simple_provider(provider):
    import psycopg
    from psycopg.types.json import Jsonb

    _, tenant = provider
    slug = f"erp-simple-{uuid.uuid4().hex[:6]}"
    with psycopg.connect(OWNER_URL, autocommit=True) as conn:
        conn.execute("""INSERT INTO app.identity_providers (tenant_id, slug, name, verify_url, config)
                        VALUES (%s, %s, 'Simple ERP', 'https://erp.test/api/escalation-category/list', %s)""",
                     (tenant, slug, Jsonb(SIMPLE_CONFIG)))
    return slug


@pytest.fixture(scope="module")
def client(provider):
    from fastapi.testclient import TestClient

    from app.core.db import use_selector_loop_on_windows
    from app.main import create_app
    from tests.conftest import make_settings

    use_selector_loop_on_windows()
    settings = replace(make_settings(), database_url=APP_URL,
                       data_encryption_key=base64.b64encode(os.urandom(32)).decode())
    app = create_app(settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake_erp)))
    with TestClient(app) as test_client:
        yield test_client


def test_valid_erp_token_gets_module_token_and_role_from_erp(client, provider):
    slug, tenant = provider
    response = client.post(f"/v1/auth/exchange/{slug}", json={"erp_token": GOOD})
    assert response.status_code == 200, response.text
    tokens = response.json()
    me = client.get("/v1/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}).json()
    assert me["tenant"]["id"] == str(tenant)
    assert me["role"] == "owner"


def test_second_sign_in_reuses_the_same_user(client, provider):
    import psycopg

    slug, _ = provider
    for _ in range(2):
        assert client.post(f"/v1/auth/exchange/{slug}", json={"erp_token": GOOD}).status_code == 200
    with psycopg.connect(OWNER_URL) as conn:
        count = conn.execute("SELECT count(*) FROM app.users u JOIN app.identity_providers p "
                             "ON p.id = u.identity_provider_id WHERE p.slug = %s", (slug,)).fetchone()[0]
        phone_enc = conn.execute("SELECT phone_enc FROM app.users u JOIN app.identity_providers p "
                                 "ON p.id = u.identity_provider_id WHERE p.slug = %s", (slug,)).fetchone()[0]
    assert count == 1
    assert b"8800674795" not in bytes(phone_enc)


def test_forged_or_expired_erp_token_is_rejected(client, provider):
    slug, _ = provider
    response = client.post(f"/v1/auth/exchange/{slug}", json={"erp_token": "1656|forged-token"})
    assert response.status_code == 401


def test_inactive_erp_user_is_rejected(client, provider):
    slug, _ = provider
    assert client.post(f"/v1/auth/exchange/{slug}", json={"erp_token": INACTIVE}).status_code == 401


def test_unknown_provider_is_404(client):
    assert client.post("/v1/auth/exchange/no-such-erp", json={"erp_token": GOOD}).status_code == 404


def test_erp_token_itself_cannot_call_module_apis(client):
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {GOOD}"}).status_code == 401


def test_token_without_module_audience_is_rejected(client, provider):
    import time

    import jwt

    _, tenant = provider
    foreign = jwt.encode({"sub": str(uuid.uuid4()), "tid": str(tenant), "role": "owner", "typ": "access",
                          "exp": int(time.time()) + 60}, "x" * 32, algorithm="HS256")
    assert client.get("/v1/me", headers={"Authorization": f"Bearer {foreign}"}).status_code == 401


def test_refresh_rotates_and_logout_revokes(client, provider):
    slug, _ = provider
    first = client.post(f"/v1/auth/exchange/{slug}", json={"erp_token": GOOD}).json()
    second = client.post("/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert second.status_code == 200
    assert client.post("/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}).status_code == 401
    assert client.post("/v1/auth/logout", json={"refresh_token": second.json()["refresh_token"]}).status_code == 204
    assert client.post("/v1/auth/refresh", json={"refresh_token": second.json()["refresh_token"]}).status_code == 401


def test_simple_mode_uses_request_data_but_never_its_role(client, simple_provider):
    response = client.post(f"/v1/auth/exchange/{simple_provider}", json={"erp_token": GOOD, "user": CLAIMED})
    assert response.status_code == 200, response.text
    me = client.get("/v1/me", headers={"Authorization": f"Bearer {response.json()['access_token']}"}).json()
    assert me["role"] == "viewer"  # "Super Admin" from localStorage is ignored


def test_simple_mode_still_needs_a_valid_erp_token(client, simple_provider):
    response = client.post(f"/v1/auth/exchange/{simple_provider}", json={"erp_token": "1656|forged", "user": CLAIMED})
    assert response.status_code == 401


def test_simple_mode_requires_user_data(client, simple_provider):
    assert client.post(f"/v1/auth/exchange/{simple_provider}", json={"erp_token": GOOD}).status_code == 422
