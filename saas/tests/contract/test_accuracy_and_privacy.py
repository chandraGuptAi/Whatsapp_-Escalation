"""Pure checks for the parts that decide report accuracy and what reaches the AI."""
from __future__ import annotations

import asyncio
from uuid import uuid4

from app.core.ai.redact import Redactor
from app.core.ai.validate import validate_findings
from app.core.auth import api_keys
from app.core.reports.rules import extract_facts
from workers.scheduler import run_options


def test_validator_accepts_cited_awb():
    source = {"m1": "AWB 1234567890 delayed at hub"}
    result = {"findings": [{"text": "1234567890 is delayed", "message_ids": ["m1"]}]}
    assert validate_findings(result, source) == []


def test_validator_rejects_invented_awb_unknown_id_and_missing_citation():
    source = {"m1": "AWB 1234567890 delayed"}
    result = {"findings": [
        {"text": "9999999999 is lost", "message_ids": ["m1"]},
        {"text": "something", "message_ids": ["nope"]},
        {"text": "no citation"},
    ]}
    issues = {i["issue"] for i in validate_findings(result, source)}
    assert issues == {"AWB not in cited messages", "cites unknown messages", "no cited messages"}


def test_rules_count_awbs_and_statuses_exactly():
    facts = extract_facts([
        {"body": "AWB 1234567890 delayed", "media_text": None},
        {"body": "1234567890 now delivered, 5555555555 RTO", "media_text": ""},
    ])
    assert facts.awbs == {"1234567890", "5555555555"}
    assert facts.awb_mentions["1234567890"] == 2
    assert facts.status_counts == {"delay": 1, "delivered": 1, "rto": 1}


class _FakeConn:
    """Stands in for the pii_vault table."""

    def __init__(self):
        self.rows = {}

    async def execute(self, sql, params):
        conn = self

        class Cur:
            async def fetchone(self_inner):
                if sql.lstrip().startswith("INSERT"):
                    _, token, kind, value_hash, _ = params
                    if (kind, value_hash) in conn.rows:
                        return None
                    conn.rows[(kind, value_hash)] = token
                    return {"token": token}
                return {"token": conn.rows[(params[0], params[1])]}

        return Cur()


class _FakeCipher:
    def encrypt(self, plaintext, tenant_id):
        return plaintext.encode()


def test_redactor_masks_phone_and_email_but_keeps_known_awbs():
    redactor = Redactor(_FakeConn(), _FakeCipher(), uuid4(), "pepper")
    text = "Call 9876543210 or a@b.com about AWB 9123456789"
    masked = asyncio.run(redactor.redact_text(text, keep=frozenset({"9123456789"})))
    assert "9876543210" not in masked and "a@b.com" not in masked
    assert "9123456789" in masked
    assert "<PHONE_" in masked and "<EMAIL_" in masked


def test_redactor_gives_same_person_same_token():
    redactor = Redactor(_FakeConn(), _FakeCipher(), uuid4(), "pepper")
    first = asyncio.run(redactor.redact_text("+91 98765 43210"))
    second = asyncio.run(redactor.redact_text("9876543210"))
    assert first == second


def test_api_key_hash_round_trip():
    full, prefix, stored = api_keys.generate("pepper")
    assert full.startswith(prefix + ".")
    assert api_keys.hash_secret("pepper", full.split(".", 1)[1]) == stored
    assert api_keys.hash_secret("other", full.split(".", 1)[1]) != stored


def test_scheduled_run_gets_concrete_window():
    from datetime import datetime, timezone

    due = datetime(2026, 10, 29, 9, 0, tzinfo=timezone.utc)
    options = run_options({"group_ids": ["g"], "window_hours": 12}, due)
    assert options["window_end"] == due.isoformat()
    assert options["window_start"] == "2026-10-28T21:00:00+00:00"
    assert "window_hours" not in options
