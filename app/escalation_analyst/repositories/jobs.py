"""Durable job state, atomic single-flight claims and idempotency keys."""

from __future__ import annotations

import json
import time
from ..errors import EscalationError
from ..analysis.cancellation import AnalysisCancelled

STOP_REQUEST = "Stop requested by user"


class Jobs:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def decode(row: dict) -> dict:
        return {
            **row,
            "request": json.loads(row["request"]),
            "result": json.loads(row["result"]) if row["result"] else None,
        }

    def get(self, job_id: int, kind: str | None = None) -> dict:
        rows = self.db.read("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not rows or (kind and rows[0]["kind"] != kind):
            raise EscalationError(404, "Run not found")
        return self.decode(rows[0])

    def list(self, kind: str, lock_key: str | None = None) -> list[dict]:
        rows = self.db.read(
            "SELECT * FROM jobs WHERE kind=?" + (" AND lock_key=?" if lock_key else "") + " ORDER BY id DESC",
            (kind, lock_key) if lock_key else (kind,),
        )
        return [self.decode(row) for row in rows]

    def claim(self, kind: str, lock_key: str, request: dict) -> tuple[dict, bool]:
        now = int(time.time())
        # Failed insert is handled by querying the winning claim in a new transaction.
        try:
            job_id = self.db.write(
                "INSERT INTO jobs(kind,lock_key,status,request_key,request,created_at,updated_at) VALUES(?,?,'running',?,?,?,?)",
                (kind, lock_key, request.get("requestKey"), json.dumps(request), now, now),
                True,
            )
            return self.get(job_id), True
        except Exception:
            if request.get("requestKey"):
                rows = self.db.read(
                    "SELECT * FROM jobs WHERE kind=? AND request_key=?", (kind, request["requestKey"])
                )
                if rows:
                    row = self.decode(rows[0])
                    if row["request"] != request:
                        raise EscalationError(409, "requestKey was already used with different inputs")
                    return row, False
            rows = self.db.read("SELECT * FROM jobs WHERE lock_key=? AND status='running'", (lock_key,))
            if rows:
                return self.decode(rows[0]), False
            raise

    def update(self, job_id: int, result: dict, status: str = "running", error: str | None = None) -> int:
        return self.db.write(
            "UPDATE jobs SET result=?,status=?,error=?,updated_at=? WHERE id=? AND status='running' "
            "AND (error IS NULL OR error<>?)",
            (json.dumps(result), status, error, int(time.time()), job_id, STOP_REQUEST),
        )

    def save_progress(self, job_id: int, result: dict) -> int:
        """Persist a running job's progress snapshot, including while a stop is pending."""
        return self.db.write(
            "UPDATE jobs SET result=?,updated_at=? WHERE id=? AND status='running'",
            (json.dumps(result), int(time.time()), job_id),
        )

    def fail(self, job_id: int, error: str) -> int:
        """Mark a running job failed and keep its last persisted progress."""
        return self.db.write(
            "UPDATE jobs SET status='error',error=?,updated_at=? WHERE id=? AND status='running' "
            "AND (error IS NULL OR error<>?)",
            (error, int(time.time()), job_id, STOP_REQUEST),
        )

    def request_stop(self, job_id: int) -> dict:
        self.get(job_id, "master")
        self.db.write(
            "UPDATE jobs SET error=?,updated_at=? WHERE id=? AND status='running'",
            (STOP_REQUEST, int(time.time()), job_id),
        )
        return self.get(job_id, "master")

    def check_cancelled(self, job_id: int) -> None:
        job = self.get(job_id)
        if job["status"] == "cancelled" or job["error"] == STOP_REQUEST:
            raise AnalysisCancelled()

    def cancelled(self, job_id: int) -> None:
        # Keep completed group counts and saved reports; release this job's claim.
        self.db.write(
            "UPDATE jobs SET status='cancelled',error=NULL,updated_at=? WHERE id=? AND status='running'",
            (int(time.time()), job_id),
        )

    def recover_expired(self, seconds: int = 1800) -> int:
        return self.db.write(
            "UPDATE jobs SET status='error',error='Interrupted or expired worker' WHERE status='running' AND updated_at<?",
            (int(time.time()) - seconds,),
        )

    def heartbeat(self, job_id: int) -> None:
        self.db.write(
            "UPDATE jobs SET updated_at=? WHERE id=? AND status='running'", (int(time.time()), job_id)
        )
