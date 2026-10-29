"""ERP-signed tokens end to end on real PostgreSQL. Skipped unless both URLs are set (see test_rls.py)."""
from __future__ import annotations

import base64
import os
import time
import uuid
from dataclasses import replace

import pytest

OWNER_URL, APP_URL = os.getenv("SAAS_TEST_OWNER_URL"), os.getenv("SAAS_TEST_APP_URL")
pytestmark = pytest.mark.skipif(not (OWNER_URL and APP_URL), reason="needs SAAS_TEST_OWNER_URL and SAAS_TEST_APP_URL")


def _rsa_pair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public


PRIVATE, PUBLIC = _rsa_pair()
OTHER_PRIVATE, _ = _rsa_pair()
ISSUER = f"test-erp-{uuid.uuid4().hex[:6]}"


def sign(private=PRIVATE, ttl=60, **overrides):
    import jwt

    now = int(time.time())
    claims = {"iss": ISSUER, "aud": "whatsapp-escalation", "sub": "1", "name": "ANKIT JAIN",
              "mobile": "8800674795", "role": "Super Admin", "iat": now, "exp": now + ttl, **overrides}
    return jwt.encode(claims, private, algorithm="RS256")


@pytest.fixture(scope="module")
def tenant():
    import psycopg
    from psycopg.types.json import Jsonb

    tid = uuid.uuid4()
    config = {"issuer": ISSUER, "algorithm": "RS256", "public_key": PUBLIC, "max_ttl_seconds": 300,
              "role_map": {"Super Admin": "owner"}}
    with psycopg.connect(OWNER_URL, autocommit=True) as conn:
        conn.execute("INSERT INTO app.tenants (id, slug, name) VALUES (%s, %s, 'JWT tenant')",
                     (tid, f"jwt-{tid.hex[:8]}"))
        conn.execute("""INSERT INTO app.identity_providers (tenant_id, slug, name, kind, config)
                        VALUES (%s, %s, 'JWT ERP', 'erp_jwt', %s)""", (tid, f"jwt-{tid.hex[:8]}", Jsonb(config)))
    yield tid
    with psycopg.connect(OWNER_URL, autocommit=True) as conn:
        conn.execute("DELETE FROM app.users WHERE identity_provider_id IN "
                     "(SELECT id FROM app.identity_providers WHERE tenant_id = %s)", (tid,))
        conn.execute("DELETE FROM app.tenants WHERE id = %s", (tid,))


@pytest.fixture(scope="module")
def client(tenant):
    from fastapi.testclient import TestClient

    from app.core.db import use_selector_loop_on_windows
    from app.main import create_app
    from tests.conftest import make_settings

    use_selector_loop_on_windows()
    settings = replace(make_settings(), database_url=APP_URL,
                       data_encryption_key=base64.b64encode(os.urandom(32)).decode())
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def me(client, token):
    return client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})


def test_signed_token_works_on_every_api_and_maps_role(client, tenant):
    response = me(client, sign())
    assert response.status_code == 200, response.text
    assert response.json()["tenant"]["id"] == str(tenant)
    assert response.json()["role"] == "owner"
    assert client.get("/v1/groups", headers={"Authorization": f"Bearer {sign()}"}).status_code == 200


def test_many_requests_create_one_user(client, tenant):
    import psycopg

    for _ in range(3):
        assert me(client, sign()).status_code == 200
    with psycopg.connect(OWNER_URL) as conn:
        n = conn.execute("SELECT count(*) FROM app.users u JOIN app.identity_providers p "
                         "ON p.id = u.identity_provider_id WHERE p.tenant_id = %s", (tenant,)).fetchone()[0]
    assert n == 1


@pytest.mark.parametrize("token", [
    pytest.param(lambda: sign(private=OTHER_PRIVATE), id="signed-with-another-key"),
    pytest.param(lambda: sign(ttl=-30), id="expired"),
    pytest.param(lambda: sign(ttl=3600), id="lifetime-too-long"),
    pytest.param(lambda: sign(aud="some-other-service"), id="wrong-audience"),
    pytest.param(lambda: sign(iss="unknown-erp"), id="unknown-issuer"),
    pytest.param(lambda: sign(sub=""), id="empty-user"),
])
def test_bad_tokens_are_rejected(client, token):
    assert me(client, token()).status_code == 401


def test_algorithm_confusion_is_rejected(client):
    """Signing with HS256 using the (public) key as the secret must not be accepted."""
    import hashlib
    import hmac
    import json

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    now = int(time.time())
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps({"iss": ISSUER, "aud": "whatsapp-escalation", "sub": "1", "role": "Super Admin",
                              "iat": now, "exp": now + 60}).encode())
    signature = b64(hmac.new(PUBLIC.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    assert me(client, f"{header}.{payload}.{signature}").status_code == 401
