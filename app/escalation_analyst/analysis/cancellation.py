"""Cancellation context shared by a master run and only its own child jobs."""
from contextvars import ContextVar
from typing import Callable


class AnalysisCancelled(Exception):
    pass


current_check: ContextVar[Callable[[], None] | None] = ContextVar("escalation_cancel", default=None)


def check_cancelled() -> None:
    check = current_check.get()
    if check:
        check()
