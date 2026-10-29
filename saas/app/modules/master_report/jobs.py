from __future__ import annotations

from app.core.queue import JobContext
from app.core.reports import generate_group_report

from . import service


async def generate(ctx: JobContext) -> None:
    await generate_group_report(ctx, purpose="master_report", prompt_version=service.PROMPT_VERSION,
                                system_prompt=service.system_prompt(),
                                title=ctx.options.get("title", "Escalation report"))
