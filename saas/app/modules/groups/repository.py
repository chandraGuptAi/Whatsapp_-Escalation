"""SQL for wa_sessions, wa_groups, clusters, messages, media_items. Every call runs on a
tenant_transaction connection, so RLS already limits rows to the caller's tenant."""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

_GROUP = """SELECT id, session_id, cluster_id, jid, coalesce(name_override, name) AS name,
                   is_active, last_message_at FROM app.wa_groups"""


async def list_sessions(conn) -> list[dict]:
    cur = await conn.execute("SELECT id, status, last_seen_at FROM app.wa_sessions ORDER BY created_at")
    return await cur.fetchall()


async def list_groups(conn, *, active: bool | None) -> list[dict]:
    cur = await conn.execute(
        f"{_GROUP} WHERE (%(active)s::boolean IS NULL OR is_active = %(active)s) "
        "ORDER BY last_message_at DESC NULLS LAST, name",
        {"active": active},
    )
    return await cur.fetchall()


async def get_group(conn, group_id: UUID) -> dict | None:
    cur = await conn.execute(f"{_GROUP} WHERE id = %s", (group_id,))
    return await cur.fetchone()


async def update_group(conn, group_id: UUID, changes: dict) -> None:
    allowed = {"name_override", "cluster_id", "is_active"}
    columns = [c for c in changes if c in allowed]
    if not columns:
        return
    assignments = ", ".join(f"{c} = %({c})s" for c in columns)
    await conn.execute(f"UPDATE app.wa_groups SET {assignments} WHERE id = %(id)s", {**changes, "id": group_id})


async def list_messages(conn, group_id: UUID, *, before: datetime | None, limit: int) -> list[dict]:
    cur = await conn.execute(
        """SELECT id, sender_name, kind, body, media_text, sent_at FROM app.messages
            WHERE group_id = %(group)s AND (%(before)s::timestamptz IS NULL OR sent_at < %(before)s)
            ORDER BY sent_at DESC LIMIT %(limit)s""",
        {"group": group_id, "before": before, "limit": limit},
    )
    return await cur.fetchall()


async def list_clusters(conn) -> list[dict]:
    cur = await conn.execute(
        """SELECT c.id, c.name, c.is_active, count(g.id) AS group_count
             FROM app.clusters c LEFT JOIN app.wa_groups g ON g.cluster_id = c.id
            GROUP BY c.id ORDER BY lower(c.name)"""
    )
    return await cur.fetchall()


async def cluster_exists(conn, cluster_id: UUID) -> bool:
    cur = await conn.execute("SELECT 1 FROM app.clusters WHERE id = %s", (cluster_id,))
    return await cur.fetchone() is not None


async def create_cluster(conn, tenant_id: UUID, name: str) -> dict | None:
    cur = await conn.execute(
        """INSERT INTO app.clusters (tenant_id, name) VALUES (%s, %s)
           ON CONFLICT DO NOTHING RETURNING id, name, is_active, 0 AS group_count""",
        (tenant_id, name),
    )
    return await cur.fetchone()


async def insert_message(conn, tenant_id: UUID, group_id: UUID, message: dict) -> bool:
    """Idempotent: the same WhatsApp message delivered twice is stored once. Returns True if new."""
    cur = await conn.execute(
        """INSERT INTO app.messages (tenant_id, group_id, wa_msg_id, sender_hash, sender_name, kind, body,
                                     reply_to, sent_at)
           VALUES (%(tenant)s, %(group)s, %(wa_msg_id)s, %(sender_hash)s, %(sender_name)s, %(kind)s, %(body)s,
                   %(reply_to)s, %(sent_at)s)
           ON CONFLICT DO NOTHING RETURNING id""",
        {"tenant": tenant_id, "group": group_id, **message},
    )
    if await cur.fetchone() is None:
        return False
    await conn.execute(
        "UPDATE app.wa_groups SET last_message_at = greatest(coalesce(last_message_at, %s), %s) WHERE id = %s",
        (message["sent_at"], message["sent_at"], group_id),
    )
    return True


async def pending_media(conn, limit: int) -> list[dict]:
    cur = await conn.execute(
        """SELECT id, message_id, message_sent_at, mime_type, storage_key FROM app.media_items
            WHERE extract_status = 'pending' ORDER BY created_at LIMIT %s""",
        (limit,),
    )
    return await cur.fetchall()


async def save_media_text(conn, media: dict, *, status: str, text: str | None, model: str | None) -> None:
    await conn.execute(
        "UPDATE app.media_items SET extract_status = %s, extracted_text = %s, model = %s WHERE id = %s",
        (status, text, model, media["id"]),
    )
    if text:
        await conn.execute(
            "UPDATE app.messages SET media_text = %s WHERE id = %s AND sent_at = %s",
            (text, media["message_id"], media["message_sent_at"]),
        )
