"""Per-run performance counters; carries timings and token counts, never message content."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time


def percentile(values, q):
    ordered = sorted(values)
    if not ordered:
        return None
    index = (len(ordered) - 1) * q
    low = int(index)
    return round(ordered[low] + (ordered[min(low + 1, len(ordered) - 1)] - ordered[low]) * (index - low), 6)


class RunStats:
    def __init__(self, job_id=None):
        self.job_id = job_id
        self._lock = threading.Lock()
        self.durations: dict[str, list[float]] = {}
        self.counters: dict[str, int] = {}
        self.on_retry = None  # Callable(attempt, error_name) for progress events.

    def add(self, key: str, seconds: float) -> None:
        with self._lock:
            self.durations.setdefault(key, []).append(seconds)

    def count(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self.counters[key] = self.counters.get(key, 0) + amount

    def mean(self, key: str) -> float | None:
        with self._lock:
            values = self.durations.get(key) or []
            return sum(values) / len(values) if values else None

    @contextmanager
    def timed(self, key: str):
        start = time.monotonic()
        try:
            yield
        finally:
            self.add(key, time.monotonic() - start)

    def summary(self) -> dict:
        with self._lock:
            return {
                **self.counters,
                **{
                    key + "_seconds": dict(count=len(v), total=round(sum(v), 3), max=round(max(v), 3),
                                               p50=percentile(v, .5), p95=percentile(v, .95), p99=percentile(v, .99))
                    for key, v in self.durations.items()
                },
            }


current_stats: ContextVar[RunStats | None] = ContextVar("escalation_stats", default=None)
# Set by a master worker: callable(analysis_type, operation) reporting what its group is doing.
current_activity: ContextVar = ContextVar("escalation_activity", default=None)
current_group_name: ContextVar[str | None] = ContextVar("escalation_group_name", default=None)


def stats() -> RunStats | None:
    return current_stats.get()


def activity(analysis_type: str, operation: str) -> None:
    notify = current_activity.get()
    if notify:
        notify(analysis_type, operation)
