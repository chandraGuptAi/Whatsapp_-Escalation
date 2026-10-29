from __future__ import annotations

import hashlib
import json


def sha256_json(value) -> str:
    """Stable hash of any JSON-like value (key order does not matter)."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
