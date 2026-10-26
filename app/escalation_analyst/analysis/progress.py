"""Persisted master-run progress in the additive shape documented by the Angular dashboard.

Every value comes from work this process actually did; nothing is inferred from wall-clock guesses.
`completed_groups = analysed + reused + skipped + failed` over eligible (active) groups only.
"""

from __future__ import annotations

import threading
import time

PHASES = ["preparing", "groups", "aggregating", "master", "validation", "complete"]
TAIL_PHASES = ("aggregating", "master", "validation")
MAX_EVENTS = 50
MAX_SAMPLES = 12


class Progress:
    def __init__(self, jobs, job_id: int, settings, tail_history: float | None = None, ai_mean=None):
        self.jobs, self.job_id = jobs, job_id
        self._lock = threading.Lock()
        self._seq = 0
        self._tail_history = tail_history
        self._ai_mean = ai_mean or (lambda: None)
        self._phase_started = time.monotonic()
        self._tail_started: float | None = None
        now = round(time.time(), 3)
        self.state = dict(
            phase="preparing",
            phases=PHASES,
            completed_phases=[],
            total_groups=0,
            total_eligible_groups=0,
            completed_groups=0,
            remaining_groups=None,
            progress_percent=None,
            counts=dict(analysed=0, reused=0, skipped=0, failed=0),
            current_group=None,
            active_groups=[],
            failed_groups=[],
            events=[],
            progress_samples=[],
            phase_seconds={},
            group_report_ids={},
            started_at=now,
            finished_at=None,
            last_progress_at=now,
            updated_at=now,
            remaining_phase_estimate_seconds=None,
            estimated_seconds_remaining=None,
            poll_interval_hint=settings.progress_poll_hint,
        )

    # --- mutations (each persists one snapshot) ---------------------------------------------

    def phase(self, name: str, message: str | None = None) -> None:
        with self._lock:
            previous = self.state["phase"]
            if previous != name:
                self.state["phase_seconds"][previous] = round(time.monotonic() - self._phase_started, 3)
                if previous not in self.state["completed_phases"]:
                    self.state["completed_phases"].append(previous)
                self._phase_started = time.monotonic()
            self.state["phase"] = name
            if name in TAIL_PHASES and self._tail_started is None:
                self._tail_started = time.monotonic()
            if name != "groups":
                self.state["active_groups"], self.state["current_group"] = [], None
            self._event("phase", message or f"Phase: {name}")
            self._persist(progressed=True)

    def groups(self, selected: int, eligible: int) -> None:
        with self._lock:
            self.state.update(total_groups=selected, total_eligible_groups=eligible)
            self._sample()
            self._persist()

    def workload(self, counts: dict[str, int]) -> None:
        with self._lock:
            self._message_counts = dict(counts)
            self.state.update(report_groups_total=sum(n > 0 for n in counts.values()),
                              report_messages_total=sum(counts.values()), report_messages_completed=0)
            self._persist()

    def begin(self, group: dict, analysis_type: str = "pending", operation: str = "loading_messages") -> None:
        with self._lock:
            active = [a for a in self.state["active_groups"] if a["group_id"] != group["group_id"]]
            active.append(dict(group_id=group["group_id"], group_name=group["group_name"],
                               analysis_type=analysis_type, operation=operation))
            self.state["active_groups"] = active
            self.state["current_group"] = group["group_name"]
            self._persist()

    def activity(self, group: dict, analysis_type: str, operation: str) -> None:
        with self._lock:
            for active in self.state["active_groups"]:
                if active["group_id"] == group["group_id"]:
                    active.update(analysis_type=analysis_type, operation=operation)
            self._persist()

    def finish(self, group: dict, outcome: str, message: str) -> None:
        """Count one eligible group as analysed, reused, skipped or failed."""
        with self._lock:
            self.state["active_groups"] = [a for a in self.state["active_groups"] if a["group_id"] != group["group_id"]]
            self.state["current_group"] = (self.state["active_groups"] or [{}])[-1].get("group_name")
            if outcome in {"analysed", "reused"} and hasattr(self, "_message_counts"):
                self.state["report_messages_completed"] += self._message_counts.get(group["group_id"], 0)
            self.state["counts"][outcome] += 1
            self.state["completed_groups"] += 1
            if outcome == "failed":
                self.state["failed_groups"].append(dict(group_id=group["group_id"], group_name=group["group_name"],
                                                        error=message))
            self._sample()
            self._event(outcome, f"{group['group_name']}: {message}")
            self._persist(progressed=True)

    def skip_empty(self, groups: list[dict]) -> None:
        """Persist the preflight empty-window results once, without hundreds of DB commits."""
        if not groups:
            return
        with self._lock:
            self.state["counts"]["skipped"] += len(groups)
            self.state["completed_groups"] += len(groups)
            self.state["skip_reasons"] = {"no_messages_in_window": len(groups)}
            self._sample()
            for group in groups[-MAX_EVENTS:]:
                self._event("skipped", f"{group['group_name']}: no messages in window")
            self._persist(progressed=True)

    def release(self, group: dict) -> None:
        """Drop a group interrupted by cancellation/shutdown without counting it."""
        with self._lock:
            self.state["active_groups"] = [a for a in self.state["active_groups"] if a["group_id"] != group["group_id"]]
            self._persist()

    def checkpoint(self, group_id: str, report_id: int):
        with self._lock:
            self.state["group_report_ids"][group_id] = report_id
            self._persist()

    def event(self, kind: str, message: str) -> None:
        with self._lock:
            self._event(kind, message)
            self._persist()

    def close(self, kind: str, message: str, **extra) -> dict:
        """Final snapshot for a complete, failed or cancelled run."""
        with self._lock:
            phase = self.state["phase"]
            self.state["phase_seconds"][phase] = round(time.monotonic() - self._phase_started, 3)
            if kind == "complete":
                for name in (phase, "complete"):
                    if name not in self.state["completed_phases"]:
                        self.state["completed_phases"].append(name)
                self.state["phase"] = "done"  # Legacy terminal phase kept for existing clients.
            self.state.update(active_groups=[], current_group=None, finished_at=round(time.time(), 3),
                              remaining_phase_estimate_seconds=0 if kind == "complete" else None,
                              estimated_seconds_remaining=0 if kind == "complete" else None, **extra)
            self._event(kind, message)
            self._persist(progressed=True)
            return dict(self.state)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.state)

    # --- internals (caller holds the lock) ----------------------------------------------------

    def _event(self, kind: str, message: str) -> None:
        self._seq += 1
        self.state["events"] = (self.state["events"] + [
            dict(id=f"{self.job_id}-{self._seq}", timestamp=round(time.time(), 3), type=kind, message=message)
        ])[-MAX_EVENTS:]

    def _sample(self) -> None:
        samples = self.state["progress_samples"]
        sample = dict(timestamp=round(time.time(), 3), processed=self.state["completed_groups"])
        if samples and samples[-1]["timestamp"] == sample["timestamp"]:
            samples[-1] = sample
        else:
            samples.append(sample)
        self.state["progress_samples"] = samples[-MAX_SAMPLES:]

    def _tail_estimate(self) -> float | None:
        """Seconds of aggregation, master generation and validation still ahead."""
        # Prefer measured tails of earlier runs; else this run's mean AI call as a proxy for the merge.
        estimate = self._tail_history if self._tail_history is not None else self._ai_mean()
        if estimate is None:
            return None
        if self._tail_started is not None:
            left = estimate - (time.monotonic() - self._tail_started)
            return round(left, 1) if left > 0 else None  # Overran its estimate: unknown, never zero.
        return round(estimate, 1)

    def _persist(self, progressed: bool = False) -> None:
        state = self.state
        now = round(time.time(), 3)
        eligible, done = state["total_eligible_groups"], state["completed_groups"]
        state["remaining_groups"] = max(0, eligible - done) if eligible else None
        state["progress_percent"] = round(done / eligible * 100, 2) if eligible else None
        tail = self._tail_estimate() if state["finished_at"] is None else state["remaining_phase_estimate_seconds"]
        if state["finished_at"] is None:
            state["remaining_phase_estimate_seconds"] = tail
            state["estimated_seconds_remaining"] = self._eta(tail)
        if progressed:
            state["last_progress_at"] = now
        state["updated_at"] = max(now, state["updated_at"] + 0.001)
        self.jobs.save_progress(self.job_id, state)

    def _eta(self, tail: float | None) -> float | None:
        if tail is None:
            return None
        if self.state["phase"] in TAIL_PHASES:
            return tail
        samples = self.state["progress_samples"][-6:]
        remaining = self.state["remaining_groups"]
        if remaining is None or len(samples) < 3:
            return None
        first, last = samples[0], samples[-1]
        if last["timestamp"] <= first["timestamp"] or last["processed"] <= first["processed"]:
            return None
        rate = (last["timestamp"] - first["timestamp"]) / (last["processed"] - first["processed"])
        return round(remaining * rate + tail, 1)
