from __future__ import annotations

from pydantic import Field

from app.core.reports import GroupRunRequest


class QuickRunRequest(GroupRunRequest):
    question: str = Field(min_length=5, max_length=2000)
