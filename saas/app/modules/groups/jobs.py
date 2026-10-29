"""Background work for groups: turn images/documents into text with Gemini.
One media_extract job per tenant at a time (idempotency key), so no row locking is needed,
and no database connection is held while Gemini runs."""
from __future__ import annotations

import logging

from app.core.queue import JobContext

from . import repository

logger = logging.getLogger("app.groups")
BATCH = 20
INSTRUCTION = ("Extract the text and the shipment facts (AWB numbers, statuses, dates) from this file. "
               "Reply in plain text only.")


async def media_extract(ctx: JobContext) -> None:
    storage = ctx.services.get("storage")
    media_provider = ctx.services["media"]
    async with ctx.tenant() as conn:
        items = await repository.pending_media(conn, BATCH)
    for media in items:
        status, text, model = "skipped", None, None
        if storage is not None and media["storage_key"]:
            try:
                data = await storage.read(ctx.tenant_id, media["storage_key"])
                text, _ = await media_provider.media_to_text(data, media["mime_type"], INSTRUCTION)
                status, model = "done", media_provider.model
            except Exception:
                logger.exception("Media %s extraction failed", media["id"])
                status = "failed"
        async with ctx.tenant() as conn:
            await repository.save_media_text(conn, media, status=status, text=text, model=model)
