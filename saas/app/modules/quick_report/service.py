from __future__ import annotations

from functools import lru_cache
from pathlib import Path

KEY = "quick_report"
PROMPT_VERSION = "quick-v1"


@lru_cache
def system_prompt() -> str:
    return (Path(__file__).parent / "prompts" / "quick.md").read_text(encoding="utf-8")
