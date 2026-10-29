"""One tenant's WhatsApp connection, owned by an ingest worker.

neonize calls our handlers on its own threads; they parse there and hand the database work to
the worker's event loop. A PostgreSQL advisory lock guarantees one owner per session across all
ingest workers and machines (WhatsApp disconnects a number that is connected twice).
Ported from app/escalation_analyst/whatsapp/native.py of the old service.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from app.core.auth.erp import phone_fields

from . import media, parse, store

logger = logging.getLogger("app.ingest")
FIRST_QR_SECONDS, NEXT_QR_SECONDS = 60, 20


def device_jid_text(value) -> str:
    return f"{value.User}:{value.Device}@{value.Server}"


def parse_device_jid(text: str | None):
    if not text:
        return None
    from neonize.proto.Neonize_pb2 import JID

    user_device, _, server = text.partition("@")
    user, _, device = user_device.partition(":")
    return JID(User=user, Device=int(device or 0), Server=server or "s.whatsapp.net")


def qr_data_url(value: bytes) -> str:
    import segno

    output = io.BytesIO()
    segno.make_qr(value).save(output, kind="svg", scale=5)
    return "data:image/svg+xml;base64," + base64.b64encode(output.getvalue()).decode()


class SessionRunner:
    def __init__(self, rt, session: dict, loop: asyncio.AbstractEventLoop):
        self.rt, self.loop = rt, loop
        self.session_id, self.tenant_id = session["session_id"], session["tenant_id"]
        self.device_jid = session.get("device_jid")
        self.started_at = datetime.now(timezone.utc)
        self.client = None
        self.cache = store.GroupCache()
        self._qr_count = 0
        # One thread is held by the blocking connection; the rest serve downloads and group lookups.
        self._threads = ThreadPoolExecutor(max_workers=4, thread_name_prefix=f"wa-{str(self.session_id)[:8]}")

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        import psycopg

        lock = await psycopg.AsyncConnection.connect(self.rt.settings.database_url, autocommit=True)
        try:
            cur = await lock.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (f"wa-session:{self.session_id}",))
            if not (await cur.fetchone())[0]:
                logger.info("Session %s is owned by another ingest worker", self.session_id)
                return
            await self._state("pending", "Starting WhatsApp...")
            await self.loop.run_in_executor(self._threads, self._connect)
        finally:
            await lock.close()  # releases the advisory lock
            self._threads.shutdown(wait=False)

    def _connect(self) -> None:
        from neonize.client import NewClient

        jid = parse_device_jid(self.device_jid)
        self.client = NewClient(self.rt.settings.wa_store_database, jid=jid, uuid=f"wa-{self.session_id}",
                                new_device=jid is None)
        self._register(self.client)
        self.client.connect()  # blocks until stop()

    def stop(self) -> None:
        if self.client is not None:
            try:
                self.client.stop()
            except Exception:
                logger.exception("Stopping session %s failed", self.session_id)

    # ------------------------------------------------------------------ neonize callbacks (neonize threads)

    def _submit(self, coroutine) -> None:
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        future.add_done_callback(lambda f: f.exception() and logger.error(
            "Session %s background task failed", self.session_id, exc_info=f.exception()))

    def _state_soon(self, status: str, message: str, **fields) -> None:
        self._submit(self._state(status, message, **fields))

    async def _state(self, status: str, message: str, **fields) -> None:
        await store.set_state(self.rt.db, self.tenant_id, self.session_id, status, message, **fields)

    def _register(self, client) -> None:
        from neonize.events import (ClientOutdatedEv, ConnectedEv, ConnectFailureEv, DisconnectedEv, GroupInfoEv,
                                    HistorySyncEv, JoinedGroupEv, LoggedOutEv, MessageEv, PairStatusEv,
                                    StreamReplacedEv)

        client.event.qr(self.on_qr)
        client.event(ConnectedEv)(self.on_connected)
        client.event(MessageEv)(self.on_message)
        client.event(HistorySyncEv)(self.on_history)
        client.event(DisconnectedEv)(lambda c, e: self._state_soon(
            "disconnected", "Connection lost. WhatsApp will try to reconnect; saved messages remain available."))
        client.event(LoggedOutEv)(lambda c, e: self._state_soon(
            "logged_out", "WhatsApp was logged out. Add the number again to scan a fresh QR.", device_jid=None))
        client.event(StreamReplacedEv)(lambda c, e: self._state_soon(
            "error", "This WhatsApp number was opened somewhere else. Reconnect to take it back."))
        client.event(ConnectFailureEv)(lambda c, e: self._state_soon("error", "WhatsApp connection failed. Reconnect."))
        client.event(ClientOutdatedEv)(lambda c, e: self._state_soon("error", "The WhatsApp client needs an update."))
        client.event(PairStatusEv)(lambda c, e: self._state_soon(
            "error", "Pairing failed. Reconnect to get a new QR.") if e.Error else None)
        for event in (JoinedGroupEv, GroupInfoEv):
            client.event(event)(lambda c, e: self._submit(self.refresh_group_names()))

    def on_qr(self, client, value: bytes) -> None:
        self._qr_count += 1
        lifetime = FIRST_QR_SECONDS if self._qr_count == 1 else NEXT_QR_SECONDS
        self._state_soon("pairing", "Scan using WhatsApp > Linked Devices > Link a device.",
                         qr_image=qr_data_url(value),
                         qr_expires_at=datetime.now(timezone.utc) + timedelta(seconds=lifetime))

    def on_connected(self, client, event) -> None:
        me = client.get_me().JID
        self.device_jid = device_jid_text(me)
        phone_hash, phone_enc = phone_fields(self.rt.settings.api_key_pepper, self.rt.services["cipher"],
                                             self.tenant_id, me.User)
        self._state_soon("connected", "Connected", device_jid=self.device_jid, phone_hash=phone_hash,
                         phone_enc=phone_enc)
        self._submit(self.refresh_group_names())

    def on_message(self, client, event) -> None:
        try:
            parsed = parse.parse_live(client, event)
        except Exception:
            logger.exception("WhatsApp message could not be read")
            return
        if parsed is not None:
            self._submit(self.save(parsed))

    def on_history(self, client, event) -> None:
        try:
            groups, messages = parse.parse_history(client, event, self.rt.settings.whatsapp_history_limit)
        except Exception:
            logger.exception("WhatsApp history sync could not be read")
            return
        self._submit(self.save_history(groups, messages))

    # ------------------------------------------------------------------ database work (event loop)

    async def save(self, parsed: parse.ParsedMessage) -> None:
        row = await store.store_message(self.rt.db, self.tenant_id, self.session_id, parsed,
                                        self.rt.settings.api_key_pepper, self.cache)
        if row is not None and parsed.media is not None:
            await self.enrich(row, parsed)

    async def save_history(self, groups, messages) -> None:
        await store.learn_group_names(self.rt.db, self.tenant_id, self.session_id, groups)
        for parsed in messages:
            await self.save(parsed)

    async def enrich(self, row: dict, parsed: parse.ParsedMessage) -> None:
        size = int(getattr(parsed.media, "fileLength", 0) or 0)
        model = None
        if size > self.rt.settings.media_max_bytes:
            text, status = f"[{parsed.wa_kind}: too large to analyse]", "skipped"
        else:
            try:
                raw = await self.loop.run_in_executor(self._threads, self.client.download_any, parsed.raw)
                text, status, model = await media.extract_text(
                    self.rt.db, self.rt.services["media"], self.tenant_id, raw, wa_kind=parsed.wa_kind,
                    mime_type=getattr(parsed.media, "mimetype", "") or "application/octet-stream",
                    filename=getattr(parsed.media, "fileName", "") or "")
            except Exception:
                logger.warning("WhatsApp media download failed (%s)", parsed.wa_kind)
                text, status = f"[{parsed.wa_kind}: media download failed]", "failed"
        await store.save_media_text(self.rt.db, self.tenant_id, row, text=text, status=status, model=model)

    async def refresh_group_names(self) -> None:
        if self.client is None:
            return
        groups = await self.loop.run_in_executor(self._threads, self.client.get_joined_groups)
        await store.learn_group_names(self.rt.db, self.tenant_id, self.session_id,
                                      [(parse.jid(g.JID), g.GroupName.Name) for g in groups])
