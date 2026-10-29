"""Groups: WhatsApp sessions, groups, clusters and messages. Core module, always enabled."""
from app.core.modules import Module

from . import api, jobs

MODULE = Module(key="groups", name="Groups", router=api.router, jobs={"media_extract": jobs.media_extract},
                core=True)
