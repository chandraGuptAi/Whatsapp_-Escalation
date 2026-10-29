"""Master report: scheduled or on-demand escalation analysis across selected groups."""
from app.core.modules import Module

from . import api, jobs

MODULE = Module(key="master_report", name="Master reports", router=api.router, jobs={"generate": jobs.generate})
