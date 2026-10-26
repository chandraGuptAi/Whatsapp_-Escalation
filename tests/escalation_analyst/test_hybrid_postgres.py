"""Opt-in: point ESCALATION_TEST_DATABASE_URL ONLY at a disposable empty database."""
import json
import os
from uuid import uuid4
from dataclasses import replace
from pathlib import Path

import pytest

from app.escalation_analyst.repositories.database import Database
from app.escalation_analyst.repositories.reports import Reports
from app.escalation_analyst.repositories.analysis_cache import AnalysisCache
from app.escalation_analyst.repositories.jobs import Jobs
from app.escalation_analyst.settings import Settings


def test_postgres_migration_checkpoint_ownership_and_down_up():
    url = os.getenv("ESCALATION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires disposable PostgreSQL database")
    db = Database(replace(Settings(), database_url=url))
    db.initialize()
    reports = Reports(db)
    child = reports.save(dict(group_id="g", report="original", scope="group"))
    master = reports.save(dict(group_id="__master__:all", report="master", scope="master", group_reports={"g": child}))
    assert reports.children(master)[0]["id"] == child
    cache = AnalysisCache(db)
    key = str(uuid4())
    cache.save(key, "g", "messages", "config", {"status": "validated"}, child)
    assert AnalysisCache(Database(db.settings)).get(key)["report_id"] == child
    jobs = Jobs(db)
    first, created = jobs.claim("group", "group:" + key, {})
    second, duplicate = jobs.claim("group", "group:" + key, {})
    assert created and not duplicate and first["id"] == second["id"]
    reports.usage(dict(scope="group", reasoning_tokens=17, input_tokens=20, output_tokens=25))
    assert reports.usage_log(1)[0]["reasoning_tokens"] == 17
    down = Path(__file__).resolve().parents[2] / "migrations/002_hybrid_down.postgres.sql"
    with db.connection() as conn:
        for statement in down.read_text().split(";"):
            if statement.strip():
                conn.execute(statement)
    db._ready = False
    db.initialize()
    assert reports.get(child)["report"] == "original"
    assert db.read("SELECT version FROM schema_version WHERE version=2")
