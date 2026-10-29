"""The one AWB rule for the whole app: app.shared.logistics.awb.extract_awbs (ported from the old
service). Phone-shaped numbers count only after an explicit AWB/LR/tracking label, and numbers
right after "call", "mobile", "contact" are never AWBs. Getting this right matters twice: AWBs
must reach the model unmasked, phone numbers must not."""
from __future__ import annotations

from app.shared.logistics.awb import extract_awbs


def find_awbs(text: str) -> list[str]:
    return [item["awb"] for item in extract_awbs(text)]
