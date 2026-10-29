"""Deterministic facts computed in code, never by the model: AWB numbers and status
counts. These numbers go into the report as-is, so they are exact by construction."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

AWB = re.compile(r"(?<!\d)\d{10,14}(?!\d)")
STATUS_WORDS = {
    "delivered": ("delivered", "pod received"),
    "rto": ("rto", "return to origin"),
    "delay": ("delay", "delayed", "pending since", "not moving"),
    "damage": ("damage", "damaged", "broken"),
    "lost": ("lost", "missing", "not traceable"),
}


@dataclass
class Facts:
    awbs: frozenset[str] = frozenset()
    awb_mentions: dict[str, int] = field(default_factory=dict)
    status_counts: dict[str, int] = field(default_factory=dict)
    message_count: int = 0

    def as_dict(self) -> dict:
        return {"message_count": self.message_count, "unique_awbs": len(self.awbs),
                "top_awbs": sorted(self.awb_mentions.items(), key=lambda kv: -kv[1])[:20],
                "status_counts": self.status_counts}


def extract_facts(rows: list[dict]) -> Facts:
    mentions: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    for row in rows:
        text = f"{row.get('body') or ''} {row.get('media_text') or ''}"
        mentions.update(AWB.findall(text))
        lowered = text.lower()
        for status, words in STATUS_WORDS.items():
            if any(word in lowered for word in words):
                statuses[status] += 1
    return Facts(awbs=frozenset(mentions), awb_mentions=dict(mentions),
                 status_counts=dict(statuses), message_count=len(rows))
