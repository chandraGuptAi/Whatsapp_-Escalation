from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class SessionOut(BaseModel):
    id: UUID
    status: str
    last_seen_at: datetime | None


class GroupOut(BaseModel):
    id: UUID
    session_id: UUID
    cluster_id: UUID | None
    jid: str
    name: str
    is_active: bool
    last_message_at: datetime | None


class GroupUpdate(BaseModel):
    name_override: str | None = Field(default=None, max_length=200)
    cluster_id: UUID | None = None
    is_active: bool | None = None


class MessageOut(BaseModel):
    id: UUID
    sender_name: str
    kind: str
    body: str
    media_text: str | None
    sent_at: datetime


class MessagePage(BaseModel):
    items: list[MessageOut]
    next_before: datetime | None


class ClusterIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ClusterOut(BaseModel):
    id: UUID
    name: str
    is_active: bool
    group_count: int
