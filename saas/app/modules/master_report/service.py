"""Master report rules: which prompt, which version. Bump PROMPT_VERSION whenever the
prompt changes, so cached results from the old prompt are never reused."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

KEY = "master_report"
PROMPT_VERSION = "master-v1"


@lru_cache
def system_prompt() -> str:
    return (Path(__file__).parent / "prompts" / "master.md").read_text(encoding="utf-8")
