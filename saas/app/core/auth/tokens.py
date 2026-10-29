"""Short-lived JWT access tokens and opaque, rotated refresh tokens."""
from __future__ import annotations

import hashlib
import secrets
import time
from uuid import UUID

from ..errors import Unauthorized
from ..tenancy import ROLE_SCOPES, Principal

_ALGORITHM = "HS256"
# Tokens are valid only for this module; any other service rejects them, and we reject theirs.
AUDIENCE = "whatsapp-escalation"
ISSUER = "whatsapp-escalation"  # tokens we issue; any other issuer is an ERP (see erp_jwt.py)


def issue_access_token(secret: str, minutes: int, *, user_id: UUID, tenant_id: UUID, role: str) -> str:
    import jwt

    now = int(time.time())
    claims = {"sub": str(user_id), "tid": str(tenant_id), "role": role, "typ": "access", "aud": AUDIENCE,
              "iss": ISSUER, "iat": now, "exp": now + minutes * 60}
    return jwt.encode(claims, secret, algorithm=_ALGORITHM)


def decode_access_token(secret: str, token: str) -> Principal:
    import jwt

    try:
        claims = jwt.decode(token, secret, algorithms=[_ALGORITHM], audience=AUDIENCE, issuer=ISSUER,
                            options={"require": ["exp", "sub", "tid", "aud", "iss"]})
    except jwt.PyJWTError:
        raise Unauthorized("Invalid or expired token") from None
    role = claims.get("role", "")
    if claims.get("typ") != "access" or role not in ROLE_SCOPES:
        raise Unauthorized("Invalid token")
    return Principal(tenant_id=UUID(claims["tid"]), user_id=UUID(claims["sub"]), role=role, scopes=ROLE_SCOPES[role])


def new_refresh_token() -> tuple[str, str]:
    """Returns (token for the client, sha256 hash to store)."""
    token = secrets.token_urlsafe(48)
    return token, hash_refresh_token(token)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
