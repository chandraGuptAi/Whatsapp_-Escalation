from __future__ import annotations

from app.core.queue import JobContext, PermanentJobError

from . import service


async def generate(ctx: JobContext) -> None:
    raise PermanentJobError(f"The {service.KEY} engine is not ported yet (source: {service.SOURCE})")
