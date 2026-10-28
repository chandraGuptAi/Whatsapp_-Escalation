"""Concurrent master runs: bounded workers, reuse, failure isolation, progress contract, context."""
import io
import json
import threading
import time
from dataclasses import replace
import pytest
from openpyxl import load_workbook
from app.escalation_analyst.analysis import aggregate
from app.escalation_analyst.analysis import reports as analysis_reports
from app.escalation_analyst.errors import EscalationError
from app.escalation_analyst.utils.ist import day
from .test_core import client, message
from .test_cancellation import wait_until

HEADER = "| Time | Seller | AWB | Mile | Category/Subcategory | Priority | Staff response | Seller follow-ups | Staff responses | Status | Time to close | Terminal reached |\n|---|---|---|---|---|---|---|---|---|---|---|---|\n"


def group_report(awb, status="open", priority="normal", category="Pickup/Delayed"):
    counts = dict(raised=1, closed=int(status == "verified_closed"), verified_closed=int(status == "verified_closed"),
                  pending=int(status == "open"), critical=int(priority == "critical"))
    return ("```json\n" + json.dumps(counts) + "\n```\n" + HEADER
            + f"| 01 Jan 10:00 | Seller | {awb} | First | {category} | {priority} | meaningful | 1 | 1 | {status} | — | no |\n"
            + "\n## 3. AWB journey\n**AWB detail** long narrative\n\n## 4. Qualitative flags\n- **Critical**: none\n")


def seed(r, count, prefix="g", timestamp=None):
    for i in range(count):
        row = message()
        row.update(wa_id=f"{prefix}{i}", group_id=f"{prefix}{i}@g.us", group_name=f"Group {prefix}{i}")
        if timestamp:
            row["timestamp"] = timestamp
        r.groups.ingest(row)


class FakeAI:
    """Thread-safe stand-in for Provider.generate that records concurrency and calls."""

    def __init__(self, delay=0.0, fail=(), status=lambda gid: "open"):
        self.delay, self.fail, self.status = delay, set(fail), status
        self.lock = threading.Lock()
        self.active = self.peak = 0
        self.calls = []

    def __call__(self, system, user, meta):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls.append((meta["scope"], meta["group_id"], user))
        try:
            time.sleep(self.delay)
            if meta["group_id"] in self.fail:
                raise EscalationError(502, "Escalation AI generation failed; retry the analysis.")
            if meta["scope"] == "master":
                return "**Executive overview** cluster ok {{G:g0@g.us|Group g0}}"
            return group_report("AWB" + meta["group_id"].split("@")[0].upper().rjust(8, "0"),
                                 self.status(meta["group_id"]))
        finally:
            with self.lock:
                self.active -= 1

    def scopes(self, scope):
        return [c for c in self.calls if c[0] == scope]


def run_master(client, body=None):
    r = client.app.state.runtime
    response = client.post("/master/analyse", json=body or {"hours": 24})
    assert response.status_code == 200, response.text
    run = response.json()["runId"]
    wait_until(lambda: r.jobs.get(run)["status"] != "running", timeout=60)
    return client.get("/master/progress", params={"runId": run}).json()["run"]


def test_349_groups_run_concurrently_within_the_bound(client):
    r = client.app.state.runtime
    seed(r, 349)
    ai = FakeAI(delay=0.01)
    r.provider.generate = ai
    run = run_master(client)
    assert run["status"] == "complete" and run["master_report_id"]
    assert 1 < ai.peak <= r.settings.master_concurrency
    assert len(ai.scopes("group")) == 349 and len(ai.scopes("master")) == 1
    assert run["counts"] == dict(analysed=349, reused=0, skipped=0, failed=0)
    assert run["completed_groups"] == run["total_eligible_groups"] == 349
    report = r.reports.get(run["master_report_id"])
    assert json.loads(report["metrics_json"])["raised"] == 349
    assert report["report"].rstrip().endswith("```")  # Code-computed totals stay the last block.
    assert len({row["group_id"] for row in r.reports.history(scope="group")}) == 349


def test_progress_contract_matches_angular_fields(client):
    r = client.app.state.runtime
    seed(r, 6)
    r.groups.status("g5@g.us", False)  # Paused groups are excluded from the eligible denominator.
    r.provider.generate = FakeAI()
    run = run_master(client)
    assert run["total_groups"] == 6 and run["total_eligible_groups"] == 5
    assert run["completed_groups"] == sum(run["counts"].values()) == 5
    assert run["remaining_groups"] == 0 and run["progress_percent"] == 100
    assert run["completed_phases"] == ["preparing", "groups", "aggregating", "master", "validation", "complete"]
    assert run["phase"] == "done" and run["active_groups"] == [] and run["current_group"] is None
    assert run["started_at"] <= run["last_progress_at"] <= run["finished_at"] <= run["updated_at"]
    assert all({"id", "timestamp", "type", "message"} <= set(e) for e in run["events"])
    assert len({e["id"] for e in run["events"]}) == len(run["events"])
    assert run["progress_samples"][-1]["processed"] == 5
    assert set(run["phase_seconds"]) >= {"groups", "aggregating", "master", "validation"}
    assert run["poll_interval_hint"] == r.settings.progress_poll_hint
    assert not run["stopRequested"] and run["error"] is None


def test_second_run_reuses_and_empty_groups_skip_without_ai(client):
    r = client.app.state.runtime
    seed(r, 4)
    seed(r, 2, prefix="old", timestamp=int(time.time()) - 3 * 86400)  # Outside the 24h window.
    ai = FakeAI()
    r.provider.generate = ai
    first = run_master(client, {"fromDate": day(time.time()), "toDate": day(time.time())})
    assert first["counts"] == dict(analysed=4, reused=0, skipped=2, failed=0)
    second = run_master(client, {"fromDate": day(time.time()), "toDate": day(time.time())})
    assert second["counts"] == dict(analysed=0, reused=4, skipped=2, failed=0)
    assert len(ai.scopes("group")) == 4  # The second run made no group AI calls.
    assert all("old" not in c[1] for c in ai.calls)


def test_one_group_failure_does_not_abort_others(client):
    r = client.app.state.runtime
    seed(r, 5)
    r.provider.generate = FakeAI(fail={"g2@g.us"})
    run = run_master(client)
    assert run["status"] == "complete"
    assert run["counts"] == dict(analysed=4, reused=0, skipped=0, failed=1)
    assert run["failed_groups"][0]["group_id"] == "g2@g.us"
    report = r.reports.get(run["master_report_id"])
    assert json.loads(report["metrics_json"])["raised"] == 4
    assert "Failed groups (not included in totals): Group g2" in report["report"]


def test_all_groups_failing_fails_the_run_and_keeps_progress(client):
    r = client.app.state.runtime
    seed(r, 3)
    r.provider.generate = FakeAI(fail={f"g{i}@g.us" for i in range(3)})
    run = run_master(client)
    assert run["status"] == "error" and "All 3 group analyses failed" in run["error"]
    assert run["counts"]["failed"] == 3 and run["finished_at"]
    assert not r.reports.history(scope="master")


def test_incompatible_prompt_version_is_not_reused(client, monkeypatch):
    r = client.app.state.runtime
    seed(r, 1)
    ai = FakeAI()
    r.provider.generate = ai
    options = {"fromDate": day(time.time()), "toDate": day(time.time())}
    first = r.analysis.group("g0@g.us", options)
    assert r.reports.get(first["reportId"])["prompt_version"]
    assert r.analysis.group("g0@g.us", options)["reuse"] == "existing"
    original = analysis_reports.prompt
    monkeypatch.setattr(analysis_reports, "prompt", lambda name: (original(name)[0], "different"))
    assert r.analysis.group("g0@g.us", options)["reuse"] is None
    assert len(ai.scopes("group")) == 2


def test_previous_master_context_is_validated_and_drives_carry_over(client):
    r = client.app.state.runtime
    seed(r, 2)
    today = int(time.time())
    start, end = today - 3 * 86400, today - 2 * 86400
    # Previous window: g0's AWB was open, g1's AWB was open.
    for gid in ("g0", "g1"):
        r.reports.save(dict(group_id=f"{gid}@g.us", group_name=f"Group {gid}", scope="group", window_label="old",
                            report=group_report("AWB" + gid.upper().rjust(8, "0")), period_start=start,
                            period_end=end, message_count=1))
    previous = r.reports.save(dict(group_id="__master__:all", group_name="all", scope="master", cluster_id="all",
                                   report="Old narrative\n\n## Computed totals\n```json\n{}\n```",
                                   metrics_json=json.dumps({"raised": 2}), window_label="old",
                                   period_start=start, period_end=end, message_count=2))
    group_only = r.reports.history(scope="group")[0]["id"]
    assert client.post("/master/analyse", json={"hours": 24, "contextReportIds": [group_only]}).status_code == 400
    assert client.post("/master/analyse", json={"hours": 24, "contextReportIds": [99999]}).status_code == 404
    # Current window: g0 is now verified closed, g1 is still open.
    ai = FakeAI(status=lambda gid: "verified_closed" if gid == "g0@g.us" else "open")
    r.provider.generate = ai
    run = run_master(client, {"hours": 24, "contextReportIds": [previous]})
    master_prompt = ai.scopes("master")[0][2]
    assert "HISTORICAL CONTEXT" in master_prompt and "Old narrative" in master_prompt
    assert all("Old narrative" not in c[2] for c in ai.scopes("group"))
    text = r.reports.get(run["master_report_id"])["report"]
    assert "Verified closed since: 1 — AWB000000G0 (g0@g.us)" in text
    assert "Still open: 1 — AWB000000G1 (g1@g.us)" in text
    assert json.loads(r.reports.get(run["master_report_id"])["metrics_json"])["raised"] == 2  # No history added.


def test_provider_bounds_concurrency_and_retries_transient_errors(client):
    r = client.app.state.runtime
    provider = r.provider
    provider.settings = replace(r.settings, api_key="test-key", ai_max_concurrency=2, ai_retry_backoff=0)
    provider.slots = threading.BoundedSemaphore(2)
    lock = threading.Lock()
    state = dict(active=0, peak=0, calls=0)

    async def stream(system, user):
        import asyncio
        with lock:
            state["calls"] += 1
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            first = state["calls"] == 1
        try:
            await asyncio.sleep(0.05)
            if first:
                raise TimeoutError("transient")
            return "ok", None
        finally:
            with lock:
                state["active"] -= 1

    provider._stream = stream
    threads = [threading.Thread(target=provider.generate, args=("s", "u", {"scope": "group"})) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert state["peak"] == 2
    assert state["calls"] == 7  # One retried call.


def test_stale_running_master_is_released_for_restart(client):
    r = client.app.state.runtime
    job, created = r.jobs.claim("master", "master", {"clusterId": "all"})
    assert created
    r.db.write("UPDATE jobs SET updated_at=? WHERE id=?", (int(time.time()) - 3600, job["id"]))
    run = client.get("/master/progress", params={"runId": job["id"]}).json()["run"]
    assert run["status"] == "error" and run["error"] == "Interrupted or expired worker"
    _, created = r.jobs.claim("master", "master", {})
    assert created


def test_bulk_export_after_concurrent_run(client):
    r = client.app.state.runtime
    seed(r, 3)
    r.provider.generate = FakeAI()
    run = run_master(client)
    response = client.get("/master/bulk-export", params={"runId": run["id"]})
    assert response.status_code == 200, response.text
    rows = list(load_workbook(io.BytesIO(response.content), data_only=True).active.values)
    assert len(rows) == 4 and rows[0][:5] == ("Company", "Time", "Seller", "No. of Escalation Entries", "AWB")


def test_aggregate_validation_and_compaction():
    rows = [({"group_id": "a@g.us", "group_name": "A"},
             {"report": group_report("AWB00000001", priority="critical"), "metrics": {"raised": 2, "critical": 1}}),
            ({"group_id": "b@g.us", "group_name": "B"},
             {"report": group_report("AWB00000001"), "metrics": {"raised": 1, "pending": 1}})]
    facts = aggregate.summarize(rows)
    assert facts["totals"]["raised"] == 3
    assert facts["validation"] == ["A: counts say raised=2 but the table has 1 rows."]
    assert facts["cross_group_awbs"] == ["AWB00000001"]
    assert [g["group_id"] for g in facts["worst_groups"]] == ["a@g.us", "b@g.us"]
    assert facts["recurring_categories"][0]["category"] == "Pickup/Delayed"
    statuses = aggregate.summarize([({"group_id": "c@g.us", "group_name": "C"},
                                     {"report": group_report("AWB00000002", status="**promised_not_done**"),
                                      "metrics": {"raised": 1}})])["table_status_counts"]
    assert statuses == {"promised_not_done": 1}
    compacted = aggregate.compact(group_report("AWB1"))
    assert "AWB journey" not in compacted and "long narrative" not in compacted
    assert "Qualitative flags" in compacted and "| AWB |" in compacted
    text, notes = aggregate.validate_narrative(
        'See {{G:x@g.us|X}}\n```json\n{"raised": 9}\n```', {"a@g.us"}, strict=True)
    assert '"raised"' not in text and len(notes) == 2


def test_missing_ai_key_stops_the_run_instead_of_failing_every_group(client):
    r = client.app.state.runtime
    seed(r, 20)
    r.provider.settings = replace(r.settings, api_key="")
    calls = []
    original = r.provider._stream
    r.provider._stream = lambda *a: calls.append(a) or original(*a)
    run = run_master(client)
    assert run["status"] == "error" and run["error"] == "Escalation AI key is not configured"
    assert run["counts"]["failed"] == 0 and run["completed_groups"] < 20
    assert not calls and not r.reports.history(scope="master")


def test_master_explicit_group_selection_and_validation(client):
    r = client.app.state.runtime
    seed(r, 3)
    ai = FakeAI()
    r.provider.generate = ai
    for ids in ([], ["missing@g.us"]):
        assert client.post("/master/analyse", json={"hours": 24, "groupIds": ids}).status_code == 400
    run = run_master(client, {"hours": 24, "groupIds": ["g1@g.us"]})
    assert run["status"] == "complete"
    assert run["total_eligible_groups"] == 1
    assert run["report_groups_total"] == 1
    assert run["report_messages_total"] == run["report_messages_completed"] == 1
    assert [call[1] for call in ai.scopes("group")] == ["g1@g.us"]


def test_empty_preflight_persists_once():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from app.escalation_analyst.analysis.progress import Progress
    progress = Progress(Mock(), 1, SimpleNamespace(progress_poll_hint=2.5))
    progress._persist = Mock()
    progress.skip_empty([{"group_id": str(i), "group_name": str(i)} for i in range(235)])
    assert progress.state["counts"]["skipped"] == 235
    assert progress.state["completed_groups"] == 235
    assert len(progress.state["events"]) <= 50
    progress._persist.assert_called_once_with(progressed=True)


@pytest.mark.parametrize("url,model,effort,expected", [
    ("https://api.deepseek.com", "deepseek-v4-pro", "low", "low"),
    ("https://api.deepseek.com", "deepseek-v4-pro", "", None),
    ("https://other.example/v1", "deepseek-v4-pro", "low", None),
    ("https://api.deepseek.com", "other-model", "low", None),
])
def test_master_effort_override_is_scoped_and_provider_specific(client, url, model, effort, expected):
    from app.escalation_analyst.analysis.provider import request_options
    r = client.app.state.runtime
    r.settings = replace(r.settings, master_group_reasoning=effort, base_url=url, model=model)
    seed(r, 1)
    ai = FakeAI()
    seen = []
    def generate(system, user, meta):
        seen.append((meta["scope"], request_options.get().get("reasoning")))
        return ai(system, user, meta)
    r.provider.generate = generate
    run = run_master(client)
    assert run["status"] == "complete"
    assert seen == [("group", expected), ("master", None)]
    assert request_options.get() == {}


def test_master_effort_change_invalidates_cached_group_analysis(client):
    r = client.app.state.runtime
    r.settings = replace(r.settings, master_group_reasoning="low")
    seed(r, 1)
    ai = FakeAI()
    r.provider.generate = ai
    now = int(time.time())
    options = {"fromDate": day(now), "toDate": day(now)}
    assert run_master(client, options)["counts"]["analysed"] == 1
    assert run_master(client, options)["counts"]["reused"] == 1
    r.settings = replace(r.settings, master_group_reasoning="high")
    assert run_master(client, options)["counts"]["analysed"] == 1
    assert len(ai.scopes("group")) == 2


def test_eight_master_workers_remain_bounded(client):
    r = client.app.state.runtime
    r.settings = replace(r.settings, master_concurrency=8)
    seed(r, 16)
    ai = FakeAI(delay=.1)
    # Synchronize each wave: SQLite progress writes can otherwise stagger tiny
    # fake calls enough to hide the real worker capacity on a busy machine.
    barrier = threading.Barrier(8, timeout=20)
    def generate(system, user, meta):
        if meta["scope"] == "group":
            barrier.wait()
        return ai(system, user, meta)
    r.provider.generate = generate
    run = run_master(client)
    assert run["status"] == "complete"
    assert 4 < ai.peak <= 8
    assert run["counts"]["analysed"] == 16


def test_speed_profile_reuses_compatible_high_effort_work(client):
    r = client.app.state.runtime
    r.settings = replace(r.settings, master_group_reasoning="high")
    seed(r, 1)
    ai = FakeAI()
    r.provider.generate = ai
    options = {"fromDate": day(int(time.time())), "toDate": day(int(time.time()))}
    assert run_master(client, options)["counts"]["analysed"] == 1
    r.settings = replace(r.settings, master_group_reasoning="low")
    second = run_master(client, options)
    assert second["counts"]["reused"] == 1
    assert len(ai.scopes("group")) == 1
    assert second["execution"]["group_reasoning"] == "low"
