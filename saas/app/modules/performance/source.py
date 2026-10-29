"""Adapter: app.messages rows -> the message dicts the old engines read, so the engines stay unchanged.

Old shape (repositories/groups.py): id (int), wa_id, group_id (jid), group_name, author, author_name,
body, msg_type, timestamp (epoch seconds), quoted_wa_id, has_media, created_at.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

ID_LOW_BITS = 21
LOAD_BATCH = 50  # groups per query; progress is reported between batches


async def tenant_groups(conn) -> list[dict]:
    cur = await conn.execute(
        """SELECT id, jid, coalesce(nullif(name_override, ''), nullif(name, ''), jid) AS name
             FROM app.wa_groups ORDER BY jid"""
    )
    return await cur.fetchall()


def message_id(row_id: UUID, ts: int) -> int:
    """Stable integer id for a message (the old engines need ints: sorting, evidence references,
    ERP mapping responseMessageId/closureMessageId must be 0 < id < 2**53). Ordered by second like the
    old autoincrement ids; same-second messages are ordered by the uuid instead of insertion order."""
    return (ts << ID_LOW_BITS) | (int.from_bytes(row_id.bytes[:8], "big") & ((1 << ID_LOW_BITS) - 1))


def to_old(row: dict) -> dict:
    ts = int(row["sent_at"].timestamp())
    media = row["media_text"]
    body = (row["body"] + " " + media).strip() if media else row["body"]
    return dict(
        id=message_id(row["id"], ts),
        wa_id=row["wa_msg_id"],
        group_id=row["jid"],
        group_name=row["group_name"],
        author=row["sender_hash"],
        author_name=row["sender_name"] or None,
        body=body,
        msg_type="chat" if row["kind"] == "text" else row["kind"],
        timestamp=ts,
        quoted_wa_id=row["reply_to"],
        has_media=int(row["kind"] != "text"),
        created_at=int(row["ingested_at"].timestamp()),
    )


async def load_messages(conn, group_ids: list[UUID], start: int, end: int) -> list[dict]:
    """Messages of the given groups with start <= timestamp < end (epoch seconds), in the old order."""
    if not group_ids:
        return []
    cur = await conn.execute(
        """SELECT m.id, m.wa_msg_id, m.sender_hash, m.sender_name, m.kind, m.body, m.media_text, m.reply_to,
                  m.sent_at, m.ingested_at, g.jid,
                  coalesce(nullif(g.name_override, ''), nullif(g.name, ''), g.jid) AS group_name
             FROM app.messages m JOIN app.wa_groups g ON g.id = m.group_id
            WHERE m.group_id = ANY(%s) AND m.sent_at >= %s AND m.sent_at < %s""",
        (list(group_ids), datetime.fromtimestamp(start, timezone.utc), datetime.fromtimestamp(end, timezone.utc)),
    )
    return [to_old(r) for r in await cur.fetchall()]


def finalize(messages: list[dict]) -> list[dict]:
    """Old order (timestamp, id); an id collision (same second, 1 in 2M) moves to the next free id."""
    messages.sort(key=lambda m: (m["timestamp"], m["id"], m["wa_id"]))
    used = set()
    for m in messages:
        while m["id"] in used:
            m["id"] += 1
        used.add(m["id"])
    return messages
