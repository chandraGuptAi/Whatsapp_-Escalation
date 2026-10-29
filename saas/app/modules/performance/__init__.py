"""Performance: Team performance workbooks from group conversations."""
from app.core.modules import Module

from . import api, jobs

MODULE = Module(key="performance", name="Performance", router=api.router, jobs={"generate": jobs.generate})
