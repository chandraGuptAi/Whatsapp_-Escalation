from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class QuickFile(Input):
    name: str = Field(min_length=1, max_length=200)
    data: str = Field(min_length=1, max_length=14_000_000)  # base64 of at most 10 MB


class QuickRunRequest(Input):
    """Field names follow the old service so the existing frontend keeps working."""

    prompt: str = Field(min_length=3, max_length=4000)
    groupIds: list[UUID] = Field(default_factory=list, max_length=50)
    hours: float | None = Field(default=None, gt=0, le=8760)
    fromDate: str | None = None
    toDate: str | None = None
    contextReportIds: list[UUID] = Field(default_factory=list, max_length=5)
    outputFormat: Literal["web", "web_excel", "word", "detailed"] = "web"
    voice: bool = False
    files: list[QuickFile] = Field(default_factory=list, max_length=5)
    # Same key from the same user returns the same run (double click, client retry).
    requestKey: str | None = Field(default=None, min_length=1, max_length=64)
