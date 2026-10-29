"""Quick report: a free-form question answered from selected groups."""
from app.core.modules import Module

from . import api, jobs

MODULE = Module(key="quick_report", name="Quick reports", router=api.router, jobs={"generate": jobs.generate})
