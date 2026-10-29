from __future__ import annotations

from pydantic import Field

from app.core.reports import GroupRunRequest


class MasterRunRequest(GroupRunRequest):
    title: str = Field(default="Escalation report", max_length=200)
