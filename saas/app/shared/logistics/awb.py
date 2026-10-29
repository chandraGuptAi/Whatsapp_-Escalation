"""AWB candidate rules ported from the Node engine, with ASCII regex semantics."""

from __future__ import annotations

import re
from app.core.errors import EscalationError

# Indian mobile, optionally with the 91 country code (the old rule missed the 12-digit form,
# which then reached the AI unmasked as an "AWB").
PHONE = re.compile(r"^(?:91)?[6-9]\d{9}$", re.ASCII)
EXPLICIT = re.compile(
    r"\b(?:awbs?|lrs?|tracking(?:\s*(?:ids?|nos?|numbers?))?)\s*(?:numbers?|nos?\.?|ids?)?\s*[:#=-]?\s*$", re.I | re.ASCII
)
CONTACT = re.compile(
    r"(?:phone|mobile|contact|call|whatsapp|number pe)\s*(?:no\.?|number)?\s*[:#=-]?\s*$", re.I | re.ASCII
)
TOKEN = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]{2,8}\d{6,16}|\d{8,16})(?![A-Za-z0-9])", re.ASCII)


def extract_awbs(text: str, legacy: bool = False, min_digits: int = 8, max_digits: int = 16,
                 include_phone_candidates: bool = False) -> list[dict]:
    if min_digits < 8 or max_digits > 16 or min_digits > max_digits:
        raise EscalationError(400, "Invalid AWB length configuration")
    text = str(text or "")
    values = {}
    list_label = False
    previous_end = 0
    for match in TOKEN.finditer(text):
        awb = match.group()
        if not min_digits <= sum(c.isdigit() for c in awb) <= max_digits:
            continue
        awb = awb.upper()
        before = text[max(0, match.start() - 40) : match.start()]
        separator = text[previous_end:match.start()]
        explicit = bool(EXPLICIT.search(before)) or bool(list_label and re.fullmatch(r"[\s,;/\\|&]+", separator))
        list_label, previous_end = explicit, match.end()
        phone = bool(PHONE.fullmatch(awb))
        if phone and (legacy or (not explicit and not include_phone_candidates)):
            continue
        if not legacy and CONTACT.search(before) and not explicit:
            continue
        if awb not in values or explicit:
            values[awb] = dict(
                awb=awb.upper(),
                confidence="explicit" if explicit else "candidate",
                phoneShaped=phone,
                rule=(
                    "Explicit AWB/LR/tracking label"
                    if explicit
                    else "8-16 digit numeric candidate; requires review"
                ),
            )
    return list(values.values())
