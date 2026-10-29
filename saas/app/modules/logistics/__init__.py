"""Logistics: AWB and TAT logistics reports."""
from app.core.modules import Module

from . import api, jobs

MODULE = Module(key="logistics", name="Logistics", router=api.router, jobs={"generate": jobs.generate})
