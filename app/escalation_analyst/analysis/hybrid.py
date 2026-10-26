"""Conservative evidence extraction. Unrecognised language always goes to review/AI.

This grammar deliberately covers fewer cases than the legacy analyst. In particular,
speaker identity, intent, and closure are never inferred from a name or a keyword.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..logistics.awb import extract_awbs
from ..utils.ist import IST
from .metrics import METRIC_KEYS, FLOAT_KEYS

VERSION = "hybrid-1"
SCHEMA_VERSION = 1
COURIERS = ("delhivery", "bluedart", "ecom express", "xpressbees", "dtdc", "ekart", "shadowfax")
HEADER = "| Time | Seller | AWB | Mile | Category/Subcategory | Priority | Staff response | Seller follow-ups | Staff responses | Status | Time to close | Terminal reached |"
GRAMMAR = re.compile(r"(?:awb|tracking(?: id| number)?)\s*[:#=-]?\s*(\d{8,16})\s*[:,-]?\s*(delayed|in transit|out for delivery|pickup pending|not picked|delivery pending)[.!]?", re.I)


def message_facts(quote: str) -> dict:
    exact = GRAMMAR.fullmatch(quote.strip())
    return dict(refs=extract_awbs(quote), exact_status=exact[2].lower() if exact else None,
                complex=bool(re.search(r"\b(?:but|again|partial|reopen|not received|legal|consumer court)\b|नहीं|दोबारा", quote, re.I)))


def unit_key(quote: str) -> str:
    return digest([VERSION, quote])


def ai_quote(quote: str) -> str:
    """Remove contact details that are not explicitly labelled shipment evidence."""
    explicit = {r["awb"] for r in extract_awbs(quote) if r["confidence"] == "explicit"}
    quote = re.sub(r"\b[6-9]\d{9}\b", lambda m: m[0] if m[0] in explicit else "[contact redacted]", quote)
    return re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[email redacted]", quote)


class Typed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(Typed):
    message_id: int
    wa_id: str | None = None
    timestamp: int | None
    author: str
    quote: str


class Case(Typed):
    awb: str | None = None
    category: str = "Uncategorized"
    mile: Literal["First", "Last", "—"] = "—"
    status: Literal["open", "promised_not_done", "claimed_unconfirmed", "verified_closed"] = "open"
    priority: Literal["normal", "high", "critical"] = "normal"
    courier: str | None = None
    evidence_ids: list[int] = Field(min_length=1)
    # Closure requires a trusted externally supplied identity/proof; AI cannot supply it.
    closure_evidence_id: int | None = None
    confidence: float = Field(default=0.5, ge=0, le=1)


class Result(Typed):
    schema_version: int = SCHEMA_VERSION
    analyzer_version: str = VERSION
    route: Literal["deterministic", "light", "deep"]
    reasons: list[str]
    cases: list[Case]
    evidence: list[Evidence]
    review_required: bool = False
    missing_evidence: list[str] = Field(default_factory=list)
    duplicates: int = 0
    processing_duration_ms: float | None = None
    ai_usage: dict = Field(default_factory=dict)


class AIResult(Typed):
    cases: list[Case]
    review_required: bool
    missing_evidence: list[str]


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def normalize(messages: list[dict]) -> tuple[list[Evidence], int]:
    seen, rows = set(), []
    for m in sorted(messages, key=lambda m: (m.get("timestamp") or 0, m["id"])):
        # Same text at another time is a follow-up, not a duplicate.
        identity = ("wa", m["wa_id"]) if m.get("wa_id") else ("content", m.get("author"), m.get("timestamp"), m.get("body"))
        if identity in seen:
            continue
        seen.add(identity)
        rows.append(Evidence(message_id=m["id"], wa_id=m.get("wa_id"), timestamp=m.get("timestamp"),
                             author=m.get("author") or "Unknown", quote=m.get("body") or ""))
    return rows, len(messages) - len(rows)


def extract(messages: list[dict], deep_awb_threshold: int = 4, units: dict | None = None) -> Result:
    evidence, duplicates = normalize(messages)
    cases: dict[str, Case] = {}
    reasons, ambiguous = [], False
    states: dict[str, set[str]] = {}
    # Only fully matched statements can bypass semantic analysis. No substring shortcuts.
    complex_language = False
    for e in evidence:
        facts = (units or {}).get(unit_key(e.quote)) or message_facts(e.quote)
        refs, exact = facts["refs"], facts["exact_status"]
        complex_language |= facts["complex"]
        if not exact or not e.timestamp or len(refs) != 1 or refs[0]["confidence"] != "explicit":
            ambiguous = True
        for ref in refs:
            awb = ref["awb"]
            if awb not in cases:
                cases[awb] = Case(awb=awb, evidence_ids=[e.message_id], confidence=1.0)
            case = cases[awb]
            if e.message_id not in case.evidence_ids:
                case.evidence_ids.append(e.message_id)
            names = [c for c in COURIERS if re.search(r"\b" + re.escape(c) + r"\b", e.quote, re.I)]
            if len(names) == 1:
                case.courier = names[0]
            if exact:
                status = exact
                states.setdefault(awb, set()).add(status)
                case.mile = "First" if status in ("pickup pending", "not picked") else "Last"
                case.category = ("Issues with Pickup (First Mile)/Delay Picked" if status in ("pickup pending", "not picked")
                                 else "Uncategorized/Shipment status update")
    conflict = any(len(values) > 1 for values in states.values())
    if conflict:
        reasons.append("Multiple status updates require lifecycle interpretation")
    if ambiguous:
        reasons.append("Messages outside the exact factual grammar require semantic analysis")
    deep = conflict or complex_language or len(cases) >= deep_awb_threshold
    route = "deep" if deep else "light" if ambiguous else "deterministic"
    if not reasons:
        reasons.append("Every message is an explicit AWB status statement")
    return Result(route=route, reasons=reasons, cases=list(cases.values()), evidence=evidence,
                  duplicates=duplicates, review_required=ambiguous or deep,
                  missing_evidence=["Seller/staff identities and meaningful response evidence unavailable"] if cases else [])


def validate(result: Result) -> Result:
    """Reject unsupported facts and incomplete AWB coverage before persisting a result."""
    by_id = {e.message_id: e for e in result.evidence}
    covered = set()
    for case in result.cases:
        if len(case.evidence_ids) != len(set(case.evidence_ids)):
            raise ValueError("Duplicate case evidence")
        if any(i not in by_id for i in case.evidence_ids):
            raise ValueError("Unknown message evidence")
        quotes = "\n".join(by_id[i].quote for i in case.evidence_ids)
        if case.awb and not re.search(r"(?<![A-Za-z0-9])" + re.escape(case.awb) + r"(?![A-Za-z0-9])", quotes):
            raise ValueError("AWB has no source evidence")
        candidates = {r["awb"] for r in extract_awbs(quotes)}
        if case.awb and case.awb not in candidates:
            labelled = re.search(r"\b(?:awb|tracking(?: id| number)?|order id)\s*[:#=-]?\s*" + re.escape(case.awb) + r"\b", quotes, re.I)
            if not labelled or len(case.awb) < 6 or not any(c.isdigit() for c in case.awb):
                raise ValueError("AWB is not a supported shipment reference")
        if case.awb:
            if case.awb in covered:
                raise ValueError("Duplicate AWB case")
            covered.add(case.awb)
        if case.courier and (case.courier not in COURIERS or not re.search(r"\b" + re.escape(case.courier) + r"\b", quotes, re.I)):
            raise ValueError("Courier has no source evidence")
        if case.status == "verified_closed" or case.closure_evidence_id is not None:
            raise ValueError("Verified closure requires authoritative seller/proof linkage; manual review needed")
        if case.confidence < 0.9:
            result.review_required = True
    observed = {r["awb"] for e in result.evidence for r in extract_awbs(e.quote) if r["confidence"] == "explicit"}
    if observed - covered:
        raise ValueError("AI omitted explicit shipment references")
    return result


def metrics(result: Result) -> dict:
    out = {key: None if key in FLOAT_KEYS else 0 for key in METRIC_KEYS}
    out["raised"] = len(result.cases)
    for c in result.cases:
        key = {"open": "pending", "claimed_unconfirmed": "claimed_closed_unconfirmed"}.get(c.status, c.status)
        out[key] += 1
        out["critical"] += c.priority == "critical"
        out["first_mile"] += c.mile == "First"
        out["last_mile"] += c.mile == "Last"
        out["worst_case_count"] += c.priority == "critical" or c.status == "promised_not_done"
    out["closed"] = out["verified_closed"]
    return out


def safe(value) -> str:
    return str(value).replace("|", " / ").replace("\n", " ").replace("\r", " ")


def render(result: Result) -> str:
    by_id = {e.message_id: e for e in result.evidence}
    lines = ["## 1. Machine counts", "```json", json.dumps(metrics(result)), "```", "", "## 2. Escalation table", HEADER,
             "| " + " | ".join(["---"] * 12) + " |"]
    for c in result.cases:
        first = min((by_id[i] for i in c.evidence_ids), key=lambda e: (e.timestamp or 0, e.message_id))
        stamp = datetime.fromtimestamp(first.timestamp, IST).strftime("%d %b %H:%M") if first.timestamp else "Unknown"
        lines.append("| " + " | ".join(map(safe, [stamp, "Unknown", c.awb or "—", c.mile, c.category, c.priority,
                                                   "none", 0, 0, c.status, "—", "no"])) + " |")
    lines += ["", "## 3. AWB journey"]
    for c in result.cases:
        if c.awb:
            lines.append(f"- **{safe(c.awb)}**: {c.status}; terminal completion not verified.")
    lines += ["", "## 4. Qualitative flags"]
    if result.review_required:
        lines.append("- **Review required**: Evidence is incomplete; findings are provisional.")
    if result.missing_evidence:
        lines.append("- **Missing evidence**: " + safe("; ".join(result.missing_evidence)))
    lines += ["", "## Source evidence"]
    for e in result.evidence:
        lines.append(f"- Message {e.message_id}: {safe(e.quote)}")
    return "\n".join(lines)
