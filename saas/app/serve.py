"""Local single-process launcher that works on Windows too:  python -m app.serve [port]

uvicorn's own CLI picks the Proactor loop on Windows in single-process mode, which psycopg's
async driver cannot use. Production (Linux containers) uses the uvicorn CLI in deploy/Dockerfile.
"""
from __future__ import annotations

import asyncio
import sys

import uvicorn

from app.core.db import use_selector_loop_on_windows


def main() -> None:
    use_selector_loop_on_windows()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    config = uvicorn.Config("app.main:create_app", factory=True, host="127.0.0.1", port=port, loop="none")
    asyncio.run(uvicorn.Server(config).serve())


if __name__ == "__main__":
    main()
