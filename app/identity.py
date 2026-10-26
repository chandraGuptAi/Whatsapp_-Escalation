"""Identity forwarded by the authenticated ERP/main gateway."""
from dataclasses import dataclass
from fastapi import Header, HTTPException


@dataclass(frozen=True)
class CurrentUser:
    id: str
    name: str


def get_current_user(
    x_user_id: str | None = Header(default=None),
    x_user_name: str | None = Header(default=None),
) -> CurrentUser:
    user_id = (x_user_id or "").strip()
    if not user_id or len(user_id) > 100:
        raise HTTPException(401, "A valid X-User-Id header is required")
    return CurrentUser(user_id, (x_user_name or "").strip()[:200] or user_id)
