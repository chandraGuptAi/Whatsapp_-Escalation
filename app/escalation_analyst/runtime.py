"""Module-owned services and workers; no dependency on existing report business logic."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import threading
from .repositories.database import Database
from .repositories.groups import Groups
from .repositories.clusters import Clusters
from .repositories.reports import Reports
from .repositories.jobs import Jobs
from .repositories.analysis_cache import AnalysisCache
from .repositories.settings import Preferences
from .analysis.provider import Provider
from .analysis.reports import Analysis
from .whatsapp.client import WhatsAppClient
from .errors import EscalationError
from .analysis.cancellation import current_check, check_cancelled, AnalysisCancelled

logger = logging.getLogger("escalation_analyst")


class Runtime:
    def __init__(self, settings):
        self.settings = settings
        self.db = Database(settings)
        self.groups = Groups(self.db)
        self.clusters = Clusters(self.db)
        self.reports = Reports(self.db)
        self.jobs = Jobs(self.db)
        self.analysis_cache = AnalysisCache(self.db)
        self.preferences = Preferences(self.db)
        self.provider = Provider(settings, self.reports)
        self.analysis = Analysis(self)
        self.executor = ThreadPoolExecutor(
            max_workers=max(2, settings.concurrency), thread_name_prefix="escalation"
        )
        self.stop = threading.Event()
        self.scheduler = None
        self._heartbeats = []
        self.whatsapp = WhatsAppClient(self)

    def scope_groups(self, scope: str = "all", scope_id: str = "") -> list[dict]:
        groups = self.groups.list()
        if scope == "group":
            return [self.groups.get(scope_id)]
        if scope == "cluster":
            self.clusters.get(scope_id, active=True)
            mapping = self.clusters.map()
            return [g for g in groups if scope_id == "all" or mapping.get(g["group_id"]) == scope_id]
        return groups

    def start(self, kind: str, lock_key: str, options: dict, work) -> tuple[dict, bool]:
        job, created = self._claim(kind, lock_key, options)
        if created:
            execute, heartbeat_stop = self._runner(job, work, current_check.get())
            try:
                self.executor.submit(execute)
            except RuntimeError:
                heartbeat_stop.set()
                self.jobs.update(job["id"], {}, "error", "Worker unavailable")
                raise EscalationError(503, "Report worker is unavailable")
        return job, created

    def run_inline(self, kind: str, lock_key: str, options: dict, work) -> tuple[dict, bool]:
        """Claim exactly like start(), but run the work in the calling thread.

        A master run's own workers use this, so its group jobs keep the shared durable claim
        without occupying (or waiting for) the shared executor's threads.
        """
        job, created = self._claim(kind, lock_key, options)
        if created:
            execute, _ = self._runner(job, work, current_check.get())
            status = execute()
            job = self.jobs.get(job["id"])
            job["error_status"] = status
        return job, created

    def _claim(self, kind: str, lock_key: str, options: dict) -> tuple[dict, bool]:
        self.jobs.recover_expired(self.settings.job_stale_seconds)
        return self.jobs.claim(kind, lock_key, options)

    def _runner(self, job: dict, work, parent_check):
        heartbeat_stop = threading.Event()
        self._heartbeats.append(heartbeat_stop)

        def heartbeat():
            while not heartbeat_stop.wait(10):
                try:
                    self.jobs.heartbeat(job["id"])
                except Exception:
                    logger.exception("Escalation heartbeat failed")

        threading.Thread(target=heartbeat, daemon=True, name="escalation-heartbeat").start()

        def execute():
            def check():
                self.jobs.check_cancelled(job["id"])
                if parent_check:
                    parent_check()

            token = current_check.set(check)
            try:
                check_cancelled()
                result = work(job["id"])
                # Finished work stands even if a parent run stopped meanwhile; update() itself
                # refuses to complete a job whose own stop was requested.
                if not self.jobs.update(job["id"], result or {}, "complete" if result is not None else "empty"):
                    check_cancelled()
            except AnalysisCancelled:
                self.jobs.cancelled(job["id"])
            except Exception as exc:
                logger.exception("Escalation job %s failed", job["id"])
                message = (
                    exc.message
                    if isinstance(exc, EscalationError)
                    else "Report generation failed; see server logs."
                )
                # Keep the job's persisted progress so a failed run still shows what was done.
                self.jobs.fail(job["id"], message)
                if self.jobs.get(job["id"])["error"] == "Stop requested by user":
                    self.jobs.cancelled(job["id"])
                # Inline callers get the HTTP status, which the persisted error text cannot carry.
                return exc.status if isinstance(exc, EscalationError) else None
            finally:
                current_check.reset(token)
                heartbeat_stop.set()
                self._heartbeats.remove(heartbeat_stop)

        return execute, heartbeat_stop

    def close(self) -> None:
        self.stop.set()
        self.whatsapp.close()
        self.executor.shutdown(wait=True, cancel_futures=True)
        for event in self._heartbeats:
            event.set()

    def start_background(self) -> None:
        if self.settings.whatsapp_autostart:
            self.whatsapp.reconnect()
        if self.settings.scheduler_enabled and self.scheduler is None:
            from .scheduler import run

            self.scheduler = threading.Thread(
                target=run, args=(self,), name="escalation-scheduler", daemon=True
            )
            self.scheduler.start()
