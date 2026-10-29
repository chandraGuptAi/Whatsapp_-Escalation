"""Saving what the WhatsApp connection receives. Every write goes through tenant_transaction,
so RLS keeps one tenant's messages away from every other tenant."""
from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timezone
from uuid import UUID

from app.core.db import Database, tenant_transaction

from .. import repository
from .parse import ParsedMessage

GROUP_CACHE_SECONDS = 60
_UNSET = object()


class GroupCache:
    """jid -> (group id, is_active), refreshed every minute so a paused group stops capturing quickly."""

    def __init__(self):
        self._rows: dict[str, tuple[float, dict]] = {}

    def get(self, chat: str) -> dict | None:
        hit = self._rows.get(chat)
        return hit[1] if hit and hit[0] > time.monotonic() else None

    def put(self, chat: str, row: dict) -> None:
        self._rows[chat] = (time.monotonic() + GROUP_CACHE_SECONDS, row)


def sender_hash(pepper: str, sender: str) -> str:
    return hmac.new(pepper.encode(), f"sender:{sender}".encode(), hashlib.sha256).hexdigest()


async def _ensure_group(conn, tenant_id: UUID, session_id: UUID, chat: str, name: str) -> dict:
    cur = await conn.execute("SELECT id, is_active FROM app.wa_groups WHERE jid = %s", (chat,))
    row = await cur.fetchone()
    if row is None:
        await conn.execute(
            """INSERT INTO app.wa_groups (tenant_id, session_id, jid, name) VALUES (%s, %s, %s, %s)
               ON CONFLICT (tenant_id, jid) DO NOTHING""",
            (tenant_id, session_id, chat, name),
        )
        cur = await conn.execute("SELECT id, is_active FROM app.wa_groups WHERE jid = %s", (chat,))
        row = await cur.fetchone()
    return row


async def store_message(db: Database, tenant_id: UUID, session_id: UUID, parsed: ParsedMessage, pepper: str,
                        cache: GroupCache) -> dict | None:
    """Returns {"id", "sent_at"} for a newly stored message; None if duplicate or the group is paused."""
    async with tenant_transaction(db, tenant_id) as conn:
        group = cache.get(parsed.chat)
        if group is None:
            group = await _ensure_group(conn, tenant_id, session_id, parsed.chat, parsed.group_name)
            cache.put(parsed.chat, group)
        if not group["is_active"]:
            return None
        row = await repository.insert_message(conn, tenant_id, group["id"], {
            "wa_msg_id": parsed.key, "sender_hash": sender_hash(pepper, parsed.sender),
            "sender_name": parsed.sender_name[:200], "kind": parsed.kind, "body": parsed.body,
            "reply_to": parsed.reply_to, "sent_at": datetime.fromtimestamp(parsed.sent_at, timezone.utc),
        })
        if row is not None and parsed.media is not None:
            await conn.execute(
                """INSERT INTO app.media_items (tenant_id, message_id, message_sent_at, mime_type, size_bytes)
                   VALUES (%s, %s, %s, %s, %s)""",
                (tenant_id, row["id"], row["sent_at"], getattr(parsed.media, "mimetype", "") or
                 "application/octet-stream", int(getattr(parsed.media, "fileLength", 0) or 0)),
            )
        return row


async def save_media_text(db: Database, tenant_id: UUID, message: dict, *, text: str, status: str,
                          model: str | None) -> None:
    async with tenant_transaction(db, tenant_id) as conn:
        await conn.execute(
            """UPDATE app.media_items SET extract_status = %s, extracted_text = %s, model = %s
                WHERE message_id = %s AND message_sent_at = %s""",
            (status, text, model, message["id"], message["sent_at"]),
        )
        await conn.execute("UPDATE app.messages SET media_text = %s WHERE id = %s AND sent_at = %s",
                           (text, message["id"], message["sent_at"]))


async def learn_group_names(db: Database, tenant_id: UUID, session_id: UUID, groups: list[tuple[str, str]]) -> None:
    """Group names from WhatsApp; a name set by an admin (name_override) is never touched."""
    if not groups:
        return
    async with tenant_transaction(db, tenant_id) as conn, conn.cursor() as cur:
        await cur.executemany(
            """INSERT INTO app.wa_groups (tenant_id, session_id, jid, name) VALUES (%s, %s, %s, %s)
               ON CONFLICT (tenant_id, jid) DO UPDATE SET name = EXCLUDED.name
               WHERE EXCLUDED.name <> '' AND app.wa_groups.name IS DISTINCT FROM EXCLUDED.name""",
            [(tenant_id, session_id, chat, name) for chat, name in groups],
        )


async def set_state(db: Database, tenant_id: UUID, session_id: UUID, status: str, message: str | None = None, *,
                    qr_image=_UNSET, qr_expires_at=_UNSET, device_jid=_UNSET, phone_hash=_UNSET,
                    phone_enc=_UNSET) -> None:
    """Records the connection state the API shows (status, message, QR while pairing)."""
    changes = {"status": status, "status_message": message}
    for column, value in (("qr_image", qr_image), ("qr_expires_at", qr_expires_at), ("device_jid", device_jid),
                          ("phone_hash", phone_hash), ("phone_enc", phone_enc)):
        if value is not _UNSET:
            changes[column] = value
    if status != "pairing":
        changes.setdefault("qr_image", None)
        changes.setdefault("qr_expires_at", None)
    assignments = ", ".join(f"{column} = %({column})s" for column in changes)
    async with tenant_transaction(db, tenant_id) as conn:
        await conn.execute(f"UPDATE app.wa_sessions SET {assignments}, last_seen_at = now() WHERE id = %(id)s",
                           {**changes, "id": session_id})
