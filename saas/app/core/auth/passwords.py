"""Argon2id password hashing."""
from __future__ import annotations

from functools import lru_cache


@lru_cache
def _hasher():
    from argon2 import PasswordHasher

    return PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher().hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    from argon2.exceptions import Argon2Error

    if not password_hash:
        return False
    try:
        return _hasher().verify(password_hash, password)
    except Argon2Error:
        return False
