"""AES-256-GCM for the *_enc columns. The tenant id is bound as associated data, so a
ciphertext copied into another tenant's row fails to decrypt."""
from __future__ import annotations

import base64
import os
from uuid import UUID

_NONCE = 12


class Cipher:
    def __init__(self, b64_key: str, *, version: int = 1):
        key = base64.b64decode(b64_key) if b64_key else b""
        if len(key) != 32:
            raise RuntimeError("DATA_ENCRYPTION_KEY must be base64 of exactly 32 bytes")
        self._key, self.version = key, version

    def _aead(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        return AESGCM(self._key)

    def encrypt(self, plaintext: str, tenant_id: UUID) -> bytes:
        nonce = os.urandom(_NONCE)
        sealed = self._aead().encrypt(nonce, plaintext.encode(), tenant_id.bytes)
        return bytes([self.version]) + nonce + sealed

    def decrypt(self, blob: bytes, tenant_id: UUID) -> str:
        if not blob or blob[0] != self.version:
            raise ValueError("Unknown encryption key version")
        nonce, sealed = blob[1 : 1 + _NONCE], blob[1 + _NONCE :]
        return self._aead().decrypt(nonce, sealed, tenant_id.bytes).decode()
