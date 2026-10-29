"""WhatsApp ingest on real PostgreSQL: storage, session events and the session API.
Skipped unless both URLs are set (see test_rls.py). No phone: neonize events are real protobufs,
the client is a stand-in."""
from __future__ import annotations

import asyncio
import base64
import os
import time
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

pytest.importorskip("neonize")

OWNER_URL, APP_URL = os.getenv("SAAS_TEST_OWNER_URL"), os.getenv("SAAS_TEST_APP_URL")
pytestmark = pytest.mark.skipif(not (OWNER_URL and APP_URL), reason="needs SAAS_TEST_OWNER_URL and SAAS_TEST_APP_URL")


def owner():
    import psycopg

    return psycopg.connect(OWNER_URL, autocommit=True)


@pytest.fixture
def tenant_session():
    tenant, session = uuid.uuid4(), uuid.uuid4()
    with owner() as conn:
        conn.execute("INSERT INTO app.tenants (id, slug, name) VALUES (%s, %s, 'WA tenant')",
                     (tenant, f"wa-{tenant.hex[:8]}"))
        conn.execute("INSERT INTO app.wa_sessions (id, tenant_id) VALUES (%s, %s)", (session, tenant))
    yield tenant, session
    with owner() as conn:
        conn.execute("DELETE FROM app.tenants WHERE id = %s", (tenant,))


def make_settings():
    from tests.conftest import make_settings as base

    return replace(base(), database_url=APP_URL, data_encryption_key=base64.b64encode(os.urandom(32)).decode())


def run(coroutine_factory):
    """Runs async code with an open app_user pool on a selector loop."""
    from app.core.db import Database, use_selector_loop_on_windows

    use_selector_loop_on_windows()

    async def main():
        db = Database(APP_URL, min_size=1, max_size=4)
        await db.open()
        try:
            return await coroutine_factory(db)
        finally:
            await db.close()

    return asyncio.run(main())


def live(token, text="Delivery pending", chat="grp1"):
    from neonize.proto import Neonize_pb2 as n
    from neonize.proto.waE2E import WAWebProtobufsE2E_pb2 as e
    from neonize.utils.jid import build_jid

    return n.Message(Info=n.MessageInfo(ID=token, Timestamp=int(time.time()), Pushname="Seller",
                     MessageSource=n.MessageSource(Chat=build_jid(chat, "g.us"), Sender=build_jid("seller"),
                                                   IsGroup=True)), Message=e.Message(conversation=text))


def test_messages_are_stored_once_and_paused_groups_are_skipped(tenant_session):
    from app.modules.groups.whatsapp import parse, store

    tenant, session = tenant_session

    async def scenario(db):
        cache = store.GroupCache()
        first = await store.store_message(db, tenant, session, parse.parse_live(None, live("m1")), "pepper", cache)
        again = await store.store_message(db, tenant, session, parse.parse_live(None, live("m1")), "pepper", cache)
        with owner() as conn:
            conn.execute("UPDATE app.wa_groups SET is_active = false WHERE tenant_id = %s", (tenant,))
        paused = await store.store_message(db, tenant, session, parse.parse_live(None, live("m2")), "pepper",
                                           store.GroupCache())
        return first, again, paused

    first, again, paused = run(scenario)
    assert first is not None and again is None and paused is None
    with owner() as conn:
        rows = conn.execute("SELECT wa_msg_id, sender_name, sender_hash FROM app.messages WHERE tenant_id = %s",
                            (tenant,)).fetchall()
    assert [r[0] for r in rows] == ["false_grp1@g.us_m1"]
    assert rows[0][1] == "Seller" and "seller" not in rows[0][2]  # sender stored only as a hash


def test_group_names_update_but_admin_override_wins(tenant_session):
    from app.modules.groups.whatsapp import store

    tenant, session = tenant_session

    async def scenario(db):
        await store.learn_group_names(db, tenant, session, [("grp1@g.us", "First name")])
        with owner() as conn:
            conn.execute("UPDATE app.wa_groups SET name_override = 'Admin name' WHERE tenant_id = %s", (tenant,))
        await store.learn_group_names(db, tenant, session, [("grp1@g.us", "Renamed on WhatsApp")])

    run(scenario)
    with owner() as conn:
        name, override = conn.execute("SELECT name, name_override FROM app.wa_groups WHERE tenant_id = %s",
                                      (tenant,)).fetchone()
    assert (name, override) == ("Renamed on WhatsApp", "Admin name")


def test_session_events_qr_then_connected_then_media(tenant_session):
    from neonize.proto import Neonize_pb2 as n
    from neonize.proto.waE2E import WAWebProtobufsE2E_pb2 as e

    from app.core.services import build_services
    from app.modules.groups.whatsapp.session import SessionRunner

    tenant, session = tenant_session
    settings = make_settings()

    async def scenario(db):
        loop = asyncio.get_running_loop()
        rt = SimpleNamespace(db=db, settings=settings, services=build_services(settings))
        runner = SessionRunner(rt, {"session_id": session, "tenant_id": tenant}, loop)
        runner.client = SimpleNamespace(
            get_me=lambda: n.Device(JID=n.JID(User="919800000001", Device=3, Server="s.whatsapp.net")),
            get_joined_groups=lambda: [n.GroupInfo(JID=n.JID(User="grp1", Server="g.us"),
                                                   GroupName=n.GroupName(Name="Joined group"))],
            download_any=lambda message: b"AWB,Status\n1234567890,Delayed\n",
        )

        runner.on_qr(None, b"pair-me")
        await asyncio.sleep(0.3)
        with owner() as conn:
            pairing = conn.execute("SELECT status, qr_image IS NOT NULL FROM app.wa_sessions WHERE id = %s",
                                   (session,)).fetchone()

        runner.on_connected(runner.client, None)
        await asyncio.sleep(0.5)

        sheet = live("sheet")
        sheet.Message.CopyFrom(e.Message(documentMessage=e.DocumentMessage(
            mimetype="text/csv", fileName="awbs.csv", fileLength=40, caption="Pending list")))
        runner.on_message(runner.client, sheet)
        await asyncio.sleep(0.8)
        runner._threads.shutdown(wait=False)
        return pairing

    pairing = run(scenario)
    assert pairing == ("pairing", True)
    with owner() as conn:
        status, qr, device, phone_hash, phone_enc = conn.execute(
            "SELECT status, qr_image, device_jid, phone_hash, phone_enc FROM app.wa_sessions WHERE id = %s",
            (session,)).fetchone()
        group_name = conn.execute("SELECT name FROM app.wa_groups WHERE tenant_id = %s", (tenant,)).fetchone()[0]
        body, media_text, kind = conn.execute(
            "SELECT body, media_text, kind FROM app.messages WHERE tenant_id = %s", (tenant,)).fetchone()
        media_status = conn.execute("SELECT extract_status FROM app.media_items WHERE tenant_id = %s",
                                    (tenant,)).fetchone()[0]
    assert (status, qr, device) == ("connected", None, "919800000001:3@s.whatsapp.net")
    assert phone_hash and b"919800000001" not in bytes(phone_enc)
    assert group_name == "Joined group"
    assert (body, kind, media_status) == ("Pending list", "document", "done")
    assert "1234567890,Delayed" in media_text


def test_session_api_add_number_qr_and_reconnect(tenant_session):
    import jwt
    from fastapi.testclient import TestClient

    from app.core.db import use_selector_loop_on_windows
    from app.main import create_app

    tenant, _ = tenant_session
    with owner() as conn:
        user = conn.execute("INSERT INTO app.users (email) VALUES (%s) RETURNING id",
                            (f"{uuid.uuid4().hex[:8]}@test.local",)).fetchone()[0]
        conn.execute("INSERT INTO app.tenant_users (tenant_id, user_id, role) VALUES (%s, %s, 'owner')",
                     (tenant, user))

    def token(role):
        now = int(time.time())
        return jwt.encode({"sub": str(user), "tid": str(tenant), "role": role, "typ": "access",
                           "aud": "whatsapp-escalation", "iss": "whatsapp-escalation", "iat": now, "exp": now + 60},
                          "x" * 32, algorithm="HS256")

    use_selector_loop_on_windows()
    with TestClient(create_app(make_settings())) as client:
        owner_headers = {"Authorization": f"Bearer {token('owner')}"}
        created = client.post("/v1/groups/sessions", json={"label": "Support number"}, headers=owner_headers)
        assert created.status_code == 201, created.text
        sid = created.json()["id"]
        assert created.json()["status"] == "pending"

        with owner() as conn:  # worker published a QR that already expired
            conn.execute("""UPDATE app.wa_sessions SET status = 'pairing', qr_image = 'data:x',
                            qr_expires_at = now() - interval '1 second' WHERE id = %s""", (sid,))
        assert client.get(f"/v1/groups/sessions/{sid}", headers=owner_headers).json()["qr_image"] is None

        reconnect = client.post(f"/v1/groups/sessions/{sid}/reconnect", headers=owner_headers)
        assert reconnect.status_code == 200 and reconnect.json()["status"] == "pending"
        with owner() as conn:
            assert conn.execute("SELECT restart_requested_at IS NOT NULL FROM app.wa_sessions WHERE id = %s",
                                (sid,)).fetchone()[0]

        viewer = {"Authorization": f"Bearer {token('viewer')}"}
        assert client.post("/v1/groups/sessions", json={}, headers=viewer).status_code == 403
        assert client.get("/v1/groups/sessions", headers=viewer).status_code == 200
