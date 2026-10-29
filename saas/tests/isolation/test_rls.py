"""Tenant isolation against a real, migrated PostgreSQL. Skipped unless both URLs are set:

    SAAS_TEST_OWNER_URL  owner role (seeds data, bypasses RLS)
    SAAS_TEST_APP_URL    app_user role (what the service uses)

Use a disposable database: the test inserts and deletes its own tenants.
"""
from __future__ import annotations

import os
import uuid

import pytest

OWNER_URL, APP_URL = os.getenv("SAAS_TEST_OWNER_URL"), os.getenv("SAAS_TEST_APP_URL")
pytestmark = pytest.mark.skipif(not (OWNER_URL and APP_URL), reason="needs SAAS_TEST_OWNER_URL and SAAS_TEST_APP_URL")


@pytest.fixture(scope="module")
def tenants():
    import psycopg

    a, b = uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(OWNER_URL, autocommit=True) as conn:
        for tid, slug in ((a, "iso-a"), (b, "iso-b")):
            conn.execute("INSERT INTO app.tenants (id, slug, name) VALUES (%s, %s, %s)",
                         (tid, f"{slug}-{tid.hex[:6]}", slug))
            session = conn.execute("INSERT INTO app.wa_sessions (tenant_id, phone_hash) VALUES (%s, 'h') RETURNING id",
                                   (tid,)).fetchone()[0]
            group = conn.execute("INSERT INTO app.wa_groups (tenant_id, session_id, jid, name) "
                                 "VALUES (%s, %s, 'g@g.us', %s) RETURNING id", (tid, session, slug)).fetchone()[0]
            conn.execute("INSERT INTO app.messages (tenant_id, group_id, wa_msg_id, sender_hash, body, sent_at) "
                         "VALUES (%s, %s, 'm1', 's', 'hello', now())", (tid, group))
            conn.execute("INSERT INTO app.jobs (tenant_id, module_key, kind, idempotency_key) "
                         "VALUES (%s, 'master_report', 'generate', %s)", (tid, f"iso-{tid.hex}"))
    yield a, b
    with psycopg.connect(OWNER_URL, autocommit=True) as conn:
        conn.execute("DELETE FROM app.tenants WHERE id = ANY(%s)", ([a, b],))


def _app(tenant_id=None):
    import psycopg

    conn = psycopg.connect(APP_URL)
    if tenant_id is not None:
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
    return conn


def test_no_tenant_sees_nothing(tenants):
    with _app() as conn:
        assert conn.execute("SELECT count(*) FROM app.wa_groups").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM app.messages").fetchone()[0] == 0


def test_each_tenant_sees_only_its_rows(tenants):
    a, b = tenants
    for tenant, name in ((a, "iso-a"), (b, "iso-b")):
        with _app(tenant) as conn:
            assert [r[0] for r in conn.execute("SELECT name FROM app.wa_groups")] == [name]
            assert conn.execute("SELECT count(*) FROM app.messages").fetchone()[0] == 1


def test_cannot_write_into_another_tenant(tenants):
    import psycopg

    a, b = tenants
    with _app(a) as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("INSERT INTO app.clusters (tenant_id, name) VALUES (%s, 'evil')", (b,))


def test_partitions_are_closed_to_app_user(tenants):
    import psycopg

    with _app() as conn:
        parts = [r[0] for r in conn.execute(
            "SELECT inhrelid::regclass::text FROM pg_inherits WHERE inhparent = 'app.messages'::regclass")]
    for part in parts:
        with _app() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f"SELECT 1 FROM {part} LIMIT 1")


def test_worker_claims_jobs_across_tenants_once(tenants):
    a, b = tenants
    claimed = set()
    with _app() as conn:
        for _ in range(4):
            row = conn.execute("SELECT job_id, tenant_id FROM app.claim_job('test', 60, ARRAY['master_report'])"
                               ).fetchone()
            if row:
                claimed.add(row)
        conn.rollback()
    assert {t for _, t in claimed} == {a, b}
    assert len(claimed) == 2
