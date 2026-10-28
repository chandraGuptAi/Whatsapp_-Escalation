"""Offline regression tests. Synthetic cases are NOT a human-reviewed golden set."""
import io
import json
import time
from dataclasses import replace
from unittest.mock import Mock

import pytest
from openpyxl import load_workbook

from app.escalation_analyst.analysis import hybrid
from app.escalation_analyst.repositories.analysis_cache import AnalysisCache
from app.escalation_analyst.repositories.database import Database
from app.escalation_analyst.settings import Settings
from app.escalation_analyst.utils.ist import day
from .test_core import client, ingest
from .test_master_concurrency import run_master, seed


def row(body="AWB 12345678 delayed", **kw):
    return dict(dict(id=1, wa_id="a", body=body, timestamp=100, author="seller"), **kw)


@pytest.mark.parametrize("text", ["AWB 12345678 delayed", "Tracking ID: 12345678 in transit", "AWB 12345678 pickup pending"])
def test_exact_factual_grammar_no_ai(text):
    result = hybrid.validate(hybrid.extract([row(text)]))
    assert result.route == "deterministic"
    assert hybrid.metrics(result)["raised"] == 1
    assert hybrid.metrics(result)["closed"] == 0


@pytest.mark.parametrize("text", ["AWB 12345678 ho jayega kal", "AWB 12345678 डिलीवरी नहीं मिली",
    "AWB 12345678 delivered", "AWB 12345678 not delivered", "AWB 12345678 received thanks",
    "AWB 12345678 partial received", "Recharge not credited", "hello", "call 9876543210"])
def test_ambiguity_never_uses_deterministic_shortcut(text):
    result = hybrid.extract([row(text)])
    assert result.route in {"light", "deep"}
    assert result.review_required
    assert hybrid.metrics(result)["closed"] == 0


def test_out_of_order_duplicates_and_recurrence():
    messages = [row(id=3, wa_id="c", timestamp=130), row(), row(), row(id=2, wa_id="b", timestamp=120)]
    result = hybrid.validate(hybrid.extract(messages))
    assert [e.message_id for e in result.evidence] == [1, 2, 3]
    assert result.duplicates == 1
    assert hybrid.metrics(result)["raised"] == 1
    assert result.cases[0].evidence_ids == [1, 2, 3]


def test_message_volume_is_not_complexity_and_missing_timestamp_is_reviewable():
    messages = [row(id=i, wa_id=str(i), timestamp=100 + i) for i in range(1, 301)]
    assert hybrid.extract(messages).route == "deterministic"
    assert hybrid.extract([row(timestamp=None)]).route == "light"
    assert hybrid.extract([]).cases == []
    assert hybrid.extract([row(), row("AWB 12345678 out for delivery", id=2, wa_id="b")]).route == "deep"


@pytest.mark.parametrize("change", [dict(awb="87654321"), dict(courier="dtdc"), dict(evidence_ids=[99]),
    dict(status="verified_closed"), dict(closure_evidence_id=1), dict(evidence_ids=[1, 1])])
def test_unsupported_facts_rejected(change):
    result = hybrid.extract([row()])
    result.cases[0] = hybrid.Case.model_validate({**result.cases[0].model_dump(), **change})
    with pytest.raises(ValueError):
        hybrid.validate(result)


def test_omitted_explicit_awb_rejected():
    result = hybrid.extract([row("AWB 12345678 and AWB 87654321 delayed")])
    result.cases.pop()
    with pytest.raises(ValueError, match="omitted"):
        hybrid.validate(result)


def enable(r, **kw):
    r.settings = replace(r.settings, engine_mode="hybrid", hybrid_enabled=True, **kw)


def test_107_groups_zero_ai_and_reuse_after_restart(client):
    r = client.app.state.runtime
    enable(r)
    seed(r, 107)
    r.provider.generate = Mock(side_effect=AssertionError("No AI expected"))
    body = {"fromDate": day(time.time()), "toDate": day(time.time())}
    first = run_master(client, body)
    assert first["status"] == "complete"
    assert first["counts"]["analysed"] == 107
    master = r.reports.get(first["master_report_id"])
    assert json.loads(master["metrics_json"])["raised"] == 107
    assert len(r.reports.children(master["id"])) == 107
    r.analysis_cache = AnalysisCache(Database(r.settings))
    second = run_master(client, body)
    assert second["counts"]["reused"] == 107
    assert not r.provider.generate.called


def test_hybrid_content_config_window_invalidation_and_history_immutable(client):
    ingest(client)
    r = client.app.state.runtime
    enable(r)
    options = {"hours": 24, "from": int(time.time()) - 3600, "to": int(time.time()), "label": "fixed"}
    first = r.analysis.group("test@g.us", options)
    assert r.analysis.group("test@g.us", options)["reportId"] == first["reportId"]
    source = r.groups.messages("test@g.us")[0]
    r.groups.update_body(source["id"], "AWB 12345678 pickup pending")
    edited = r.analysis.group("test@g.us", options)
    assert edited["reportId"] != first["reportId"]
    r.settings = replace(r.settings, hybrid_max_tokens=4096)
    assert r.analysis.group("test@g.us", options)["reportId"] != edited["reportId"]
    assert r.reports.get(first["reportId"])["report"] == first["report"]
    assert r.analysis.group("test@g.us", {**options, "to": options["to"] + 1})["reuse"] is None


def test_legacy_content_edits_invalidate_existing_cache(client):
    ingest(client)
    r = client.app.state.runtime
    r.provider.generate = Mock(return_value='```json\n{"raised":1}\n```')
    options = {"from": int(time.time()) - 3600, "to": int(time.time()), "label": "fixed"}
    first = r.analysis.group("test@g.us", options)
    source = r.groups.messages("test@g.us")[0]
    r.groups.update_body(source["id"], "AWB 12345678 delivered")
    assert r.analysis.group("test@g.us", options)["reportId"] != first["reportId"]
    assert r.provider.generate.call_count == 2


def test_shadow_no_additional_ai_or_production_result_changes(client):
    ingest(client)
    r = client.app.state.runtime
    r.settings = replace(r.settings, engine_mode="shadow", shadow_enabled=True)
    r.provider.generate = Mock(return_value="Original report")
    result = r.analysis.group("test@g.us", {"hours": 24})
    assert result["report"] == "Original report"
    assert r.provider.generate.call_count == 1
    observations = r.db.read("SELECT payload FROM analysis_observations")
    assert json.loads(observations[0]["payload"])["mode"] == "shadow"


def test_semantic_json_source_check_and_invalid_result_not_cached(client):
    ingest(client, dict(wa_id="one", group_id="test@g.us", group_name="Test", author="seller",
                        body="AWB 12345678 kal tak ho jayega", msg_type="chat", timestamp=int(time.time())))
    r = client.app.state.runtime
    enable(r, ai_routing_enabled=True)
    evidence_id = r.groups.messages("test@g.us")[0]["id"]
    r.provider.generate = Mock(return_value=json.dumps(dict(cases=[dict(awb="12345678", status="promised_not_done", evidence_ids=[evidence_id])], review_required=True, missing_evidence=["Identity unknown"])))
    result = r.analysis.group("test@g.us", {"hours": 24})
    assert result["reviewRequired"] and result["metrics"]["promised_not_done"] == 1
    r.analysis.group("test@g.us", {"from": int(time.time()) - 3600, "to": int(time.time()) + 1, "label": "different window"})
    assert r.provider.generate.call_count == 1  # Identical semantic evidence reuses the paid response.
    r.provider.generate.return_value = '{"cases":[],"review_required":false,"missing_evidence":[]}'
    r.settings = replace(r.settings, hybrid_max_tokens=4096)
    from app.escalation_analyst.errors import EscalationError
    with pytest.raises(EscalationError, match="evidence validation"):
        r.analysis.group("test@g.us", {"hours": 24, "force": True})
    assert len(r.db.read("SELECT * FROM analysis_cache")) == 2


def test_master_export_stable_after_new_group_report_and_membership_change(client):
    r = client.app.state.runtime
    enable(r)
    seed(r, 1)
    run = run_master(client)
    r.reports.save(dict(group_id="g0@g.us", group_name="New", scope="group", report="| AWB | Status |\n| --- | --- |\n| 87654321 | open |", period_start=0, period_end=int(time.time())))
    response = client.get("/master/bulk-export", params={"reportId": run["master_report_id"]})
    assert response.status_code == 200
    rows = list(load_workbook(io.BytesIO(response.content), data_only=True).active.values)
    assert rows[1][4] == "12345678"


def test_sse_auth_terminal_and_retry_frozen_window(client):
    r = client.app.state.runtime
    assert client.get('/master/runs/1/events', headers={"X-User-Id": ""}).status_code == 401
    start, end = int(time.time()) - 3600, int(time.time())
    job, _ = r.jobs.claim("master", "master", dict(clusterId="all", **{"from": start, "to": end}, label="fixed"))
    r.jobs.fail(job["id"], "Interrupted worker")
    event = client.get(f'/master/runs/{job["id"]}/events')
    assert event.status_code == 200 and "event: progress" in event.text
    enable(r)
    result = client.post(f'/master/runs/{job["id"]}/retry').json()
    retried = r.jobs.get(result["runId"])
    assert retried["request"]["from"] == start and retried["request"]["to"] == end
    assert not retried["request"]["force"]


def test_rollout_configuration_fails_closed():
    with pytest.raises(ValueError):
        replace(Settings(), engine_mode="hybrid", hybrid_enabled=False)
    with pytest.raises(ValueError):
        replace(Settings(), engine_mode="unknown")


def test_contact_redaction_preserves_explicit_awb():
    assert hybrid.ai_quote("call 9876543210, AWB 9876543211 user@example.com") == "call [contact redacted], AWB 9876543211 [email redacted]"


def test_sqlite_reverse_migration_preserves_historical_reports(client):
    from pathlib import Path
    r = client.app.state.runtime
    report_id = r.reports.save(dict(group_id="g", report="historical", scope="group"))
    script = Path(__file__).resolve().parents[2] / "migrations/002_hybrid_down.sqlite.sql"
    with r.db.connection() as conn:
        conn.executescript(script.read_text())
    r.db._ready = False
    r.db.initialize()
    assert r.reports.get(report_id)["report"] == "historical"


def test_incremental_units_only_reextract_changed_content(client, monkeypatch):
    r = client.app.state.runtime
    messages = [row(), row("AWB 87654321 pickup pending", id=2, wa_id="b")]
    units, reused = r.analysis_cache.units(messages)
    assert reused == 0 and len(units) == 2
    original = hybrid.message_facts
    calls = []
    monkeypatch.setattr(hybrid, "message_facts", lambda quote: calls.append(quote) or original(quote))
    messages.append(row("AWB 11112222 delayed", id=3, wa_id="c"))
    units, reused = r.analysis_cache.units(messages)
    assert reused == 2 and calls == ["AWB 11112222 delayed"]
    assert hybrid.extract(messages, units=units) == hybrid.extract(messages)


def test_usage_reasoning_metrics_and_no_retry_after_persistence_failure(client):
    from types import SimpleNamespace
    from app.escalation_analyst.errors import EscalationError
    r = client.app.state.runtime
    p = r.provider
    p.settings = replace(p.settings, api_key="test-key")
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=80,
                            completion_tokens_details=SimpleNamespace(reasoning_tokens=60))
    calls = []
    async def stream(*_):
        calls.append(1)
        return "ok", usage
    p._stream = stream
    p.generate("s", "u", {"scope": "group"})
    saved = r.reports.usage_log(1)[0]
    assert saved["reasoning_tokens"] == 60 and saved["output_tokens"] == 80
    r.reports.usage = Mock(side_effect=RuntimeError("database unavailable"))
    with pytest.raises(EscalationError):
        p.generate("s", "u", {"scope": "group"})
    assert len(calls) == 2


def test_uncertain_ai_outcome_prevents_duplicate_charge_after_restart(client):
    from app.escalation_analyst.errors import EscalationError
    ingest(client, dict(wa_id="uncertain", group_id="test@g.us", group_name="Test", author="seller",
                        body="AWB 12345678 kal tak ho jayega", msg_type="chat", timestamp=int(time.time())))
    r = client.app.state.runtime
    enable(r, ai_routing_enabled=True)
    r.provider.generate = Mock(side_effect=EscalationError(502, "Timeout"))
    options = {"from": int(time.time()) - 3600, "to": int(time.time()), "label": "fixed"}
    with pytest.raises(EscalationError):
        r.analysis.group("test@g.us", options)
    r.analysis_cache = AnalysisCache(Database(r.settings))
    with pytest.raises(EscalationError, match="uncertain outcome"):
        r.analysis.group("test@g.us", options)
    assert r.provider.generate.call_count == 1


def test_provider_429_retry_after_deadline_and_nonretryable(client):
    from app.escalation_analyst.errors import EscalationError
    from types import SimpleNamespace
    r = client.app.state.runtime
    p = r.provider
    p.settings = replace(p.settings, api_key="test-key", ai_timeout=.03, ai_max_retries=1, ai_retry_backoff=0)
    assert p._backoff(0, SimpleNamespace(response=SimpleNamespace(headers={"retry-after": "7"}))) == 7
    calls = []
    async def timeout(*_):
        import asyncio
        calls.append(1)
        await asyncio.sleep(1)
    p._stream = timeout
    with pytest.raises(EscalationError):
        p.generate("s", "u", {})
    assert len(calls) == 2
    class BadRequest(Exception):
        status_code = 400
    async def bad(*_):
        calls.append(1)
        raise BadRequest()
    p._stream = bad
    with pytest.raises(EscalationError):
        p.generate("s", "u", {})
    assert len(calls) == 3
