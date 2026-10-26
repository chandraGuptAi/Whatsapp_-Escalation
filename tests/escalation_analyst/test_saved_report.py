"""The saved-data master view reads saved group reports only: no AI call, no writes."""
import json
import time
from .test_core import client
from .test_master_concurrency import FakeAI, group_report, run_master, seed


def never(*args, **kwargs):
    raise AssertionError("saved-report must not call the AI provider")


def test_saved_report_uses_saved_group_reports_without_ai(client):
    r = client.app.state.runtime
    seed(r, 4)
    r.groups.status("g3@g.us", False)
    now = int(time.time())
    for gid, status, priority in (("g0", "open", "critical"), ("g1", "verified_closed", "normal")):
        r.reports.save(dict(group_id=f"{gid}@g.us", group_name=f"Group {gid}", scope="group", window_label="last 24h",
                            report=group_report("AWB" + gid.upper().rjust(8, "0"), status, priority),
                            metrics_json=json.dumps(dict(raised=1, critical=int(priority == "critical"),
                                                         pending=int(status == "open"),
                                                         verified_closed=int(status == "verified_closed"))),
                            period_start=now - 86400, period_end=now, message_count=1))
    r.provider.generate = never
    reports_before = len(r.reports.history())
    response = client.get("/master/saved-report", params={"hours": 24})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ai_calls"] == 0 and len(r.reports.history()) == reports_before
    assert body["coverage"] == dict(eligible=3, with_saved_report=2, without_saved_report=1, paused=1)
    assert body["missing_groups"] == [dict(group_id="g2@g.us", group_name="Group g2")]
    assert body["totals"]["raised"] == 2 and body["totals"]["critical"] == 1
    assert [g["group_id"] for g in body["groups"]] == ["g0@g.us", "g1@g.us"]
    assert body["worst_groups"][0]["group_id"] == "g0@g.us"
    assert "Saved-data report (no AI)" in body["report"] and "{{G:g0@g.us|Group g0}}" in body["report"]


def test_saved_report_for_a_run_uses_that_runs_window(client):
    r = client.app.state.runtime
    seed(r, 3)
    r.provider.generate = FakeAI()
    run = run_master(client)
    r.provider.generate = never
    body = client.get("/master/saved-report", params={"runId": run["id"]}).json()
    assert body["coverage"]["with_saved_report"] == 3 and body["totals"]["raised"] == 3
    assert body["window"]["from"] == r.jobs.get(run["id"])["request"]["from"]


def test_saved_report_with_nothing_saved(client):
    r = client.app.state.runtime
    seed(r, 2)
    r.provider.generate = never
    body = client.get("/master/saved-report").json()
    assert body["coverage"]["with_saved_report"] == 0 and body["totals"]["raised"] == 0
    assert len(body["missing_groups"]) == 2
