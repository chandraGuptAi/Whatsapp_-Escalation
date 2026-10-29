"""When the ingest worker starts, restarts and stops sessions. No database, no WhatsApp."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from workers import ingest_worker


class FakeRunner:
    instances: list["FakeRunner"] = []

    def __init__(self, rt, session, loop):
        self.session_id = session["session_id"]
        self.started_at = datetime.now(timezone.utc)
        self.stopped = asyncio.Event()
        FakeRunner.instances.append(self)

    async def run(self):
        await self.stopped.wait()

    def stop(self):
        self.stopped.set()


@pytest.fixture(autouse=True)
def fake_runner(monkeypatch):
    FakeRunner.instances = []
    monkeypatch.setattr(ingest_worker, "SessionRunner", FakeRunner)


def session(sid, status="pending", restart=None):
    return {"session_id": sid, "tenant_id": uuid4(), "status": status, "device_jid": None,
            "restart_requested_at": restart}


async def settle():
    for _ in range(3):
        await asyncio.sleep(0)


def test_new_session_starts_once():
    async def scenario():
        sup, sid = ingest_worker.Supervisor(rt=None), uuid4()
        await sup.scan([session(sid)])
        await sup.scan([session(sid)])
        assert len(FakeRunner.instances) == 1
        await sup.stop_all()
    asyncio.run(scenario())


def test_reconnect_button_restarts_a_running_session():
    async def scenario():
        sup, sid = ingest_worker.Supervisor(rt=None), uuid4()
        await sup.scan([session(sid, "connected")])
        first = FakeRunner.instances[0]
        await sup.scan([session(sid, "pending", restart=first.started_at + timedelta(seconds=1))])
        assert first.stopped.is_set()
        await settle()
        await sup.scan([session(sid, "pending", restart=first.started_at + timedelta(seconds=1))])
        assert len(FakeRunner.instances) == 2
        await sup.stop_all()
    asyncio.run(scenario())


def test_session_no_longer_listed_is_stopped():
    async def scenario():
        sup, sid = ingest_worker.Supervisor(rt=None), uuid4()
        await sup.scan([session(sid)])
        await sup.scan([])  # logged out or tenant suspended
        assert FakeRunner.instances[0].stopped.is_set()
    asyncio.run(scenario())


def test_error_and_pairing_wait_for_the_button_but_disconnects_retry():
    async def scenario():
        sup = ingest_worker.Supervisor(rt=None)
        old = datetime.now(timezone.utc) - timedelta(minutes=5)
        for status in ("error", "pairing", "disconnected"):
            sid = uuid4()
            sup.last_start[sid] = old
            await sup.scan([session(sid, status)])
        assert len(FakeRunner.instances) == 1  # only the disconnected one
        await sup.stop_all()
    asyncio.run(scenario())
