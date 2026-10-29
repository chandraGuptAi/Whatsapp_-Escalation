"""Finding AWB numbers in chat text, deterministically.

A 10-digit number starting 6-9 (optionally with a 91 prefix) is an Indian mobile number unless
an AWB word comes right before it ("AWB 9123456789", "tracking no: 9123456789"). Every other
10-14 digit number is an AWB, as are letter-prefixed ones like BGSC0078363792.
Getting this right matters twice: AWBs must reach the model unmasked, phone numbers must not.
"""
from __future__ import annotations

import re

NUMBER = re.compile(r"(?<![\dA-Za-z])(\d{10,14})(?!\d)")
LETTER_PREFIXED = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,6}\d{6,14})(?![A-Za-z0-9])")
MOBILE = re.compile(r"^(?:91)?[6-9]\d{9}$")
AWB_WORD_BEFORE = re.compile(
    r"(?:awb|a\.w\.b\.?|tracking|track|docket|waybill|way\s*bill|consignment|\blr|\bcn)"
    r"\s*(?:no\.?|number|num|id|#)?\s*[:\-#]?\s*$",
    re.IGNORECASE,
)


def find_awbs(text: str) -> list[str]:
    found = []
    for match in NUMBER.finditer(text):
        value = match.group(1)
        if MOBILE.match(value) and not AWB_WORD_BEFORE.search(text[max(0, match.start() - 24):match.start()]):
            continue  # a phone number
        found.append(value)
    found.extend(LETTER_PREFIXED.findall(text))
    return found
