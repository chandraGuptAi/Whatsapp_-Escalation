"""Deterministic master aggregation over saved group reports.

The group prompt fixes the escalation-table columns and a machine-counts JSON block. Everything
numeric in the master report is computed here; the AI only writes the narrative around it.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import json
import re
from ..exports.documents import tables
from .metrics import sum_metrics

OPEN_STATUSES = {"open", "promised_not_done", "claimed_unconfirmed"}
CLOSED_STATUS = "verified_closed"
GROUP_TOKEN = re.compile(r"\{\{G:([^|}]+)\|[^}]*\}\}")
# Section headings only (`## 3. AWB journey`, `**4. Qualitative flags**`), not bold AWB lines inside a section.
HEADING = re.compile(r"^\s*(#{1,6}\s|\*\*\s*\d+\.\s)")


def _clean(value: str) -> str:
    # Strip emphasis markers but keep inner underscores (statuses such as promised_not_done).
    return re.sub(r"[*`]", "", value or "").strip().strip("_").strip()


def escalation_rows(report: str) -> list[dict] | None:
    """Rows of the group's escalation table, keyed by lower-case header; None when absent."""
    for table in tables(report or ""):
        header = [_clean(cell).lower() for cell in table[0]]
        if "awb" in header and "status" in header:
            return [dict(zip(header, (_clean(cell) for cell in row))) for row in table[1:]]
    return None


def awb_key(value: str) -> str | None:
    key = re.sub(r"[^A-Za-z0-9]", "", value or "").upper()
    return key if len(key) >= 6 else None


def status_of(row: dict) -> str:
    return re.sub(r"[\s-]+", "_", row.get("status", "").lower())


def compact(report: str) -> str:
    """Drop the per-AWB journey narrative; keep counts, the escalation table and flags."""
    out, skipping = [], False
    for line in (report or "").splitlines():
        if HEADING.match(line):
            skipping = bool(re.search(r"awb\s+journey", line, re.I))
        if not skipping:
            out.append(line)
    return "\n".join(out)


def summarize(reports: list[tuple[dict, dict]]) -> dict:
    """Python-verified master facts from (group, result) pairs."""
    groups, validation = [], []
    categories: dict[str, set] = defaultdict(set)
    category_rows: Counter = Counter()
    awb_groups: dict[str, set] = defaultdict(set)
    statuses: Counter = Counter()
    for group, result in reports:
        name = group["group_name"]
        metrics = result.get("metrics")
        rows = escalation_rows(result.get("report", ""))
        if metrics is None:
            validation.append(f"{name}: no machine-counts block; excluded from totals.")
        if rows is None:
            validation.append(f"{name}: no escalation table found.")
        elif metrics is not None and metrics.get("raised") != len(rows):
            validation.append(f"{name}: counts say raised={metrics.get('raised')} but the table has {len(rows)} rows.")
        for row in rows or []:
            statuses[status_of(row)] += 1
            category = row.get("category/subcategory", "").strip()
            if category and category not in ("—", "-"):
                categories[category].add(group["group_id"])
                category_rows[category] += 1
            key = awb_key(row.get("awb", ""))
            if key:
                awb_groups[key].add(group["group_id"])
        if metrics is not None:
            groups.append(dict(
                group_id=group["group_id"], group_name=name,
                raised=metrics.get("raised") or 0, critical=metrics.get("critical") or 0,
                pending=metrics.get("pending") or 0,
                promised_not_done=metrics.get("promised_not_done") or 0,
                claimed_closed_unconfirmed=metrics.get("claimed_closed_unconfirmed") or 0,
                verified_closed=metrics.get("verified_closed") or 0,
                abuse_legal=metrics.get("abuse_legal") or 0, missed=metrics.get("missed") or 0,
            ))
    for g in groups:
        g["open_total"] = g["pending"] + g["promised_not_done"] + g["claimed_closed_unconfirmed"]
    worst = sorted(groups, key=lambda g: (-g["critical"], -g["open_total"], -g["raised"], g["group_name"]))
    recurring = sorted(
        ((c, len(ids), category_rows[c]) for c, ids in categories.items() if len(ids) > 1),
        key=lambda item: (-item[1], -item[2], item[0]),
    )
    return dict(
        totals=sum_metrics([r["metrics"] for _, r in reports if r.get("metrics")]),
        groups_with_metrics=len(groups),
        table_status_counts=dict(sorted(statuses.items())),
        worst_groups=[g for g in worst if g["critical"] or g["open_total"]][:10],
        recurring_categories=[dict(category=c, groups=n, rows=rows) for c, n, rows in recurring[:10]],
        cross_group_awbs=sorted(k for k, ids in awb_groups.items() if len(ids) > 1),
        validation=validation,
    )


def carry_over(previous: list[dict], current: list[tuple[dict, dict]]) -> dict:
    """Compare AWB statuses in the previous window's group reports with the current window's."""
    def statuses(rows_by_group):
        out = {}
        for group_id, report in rows_by_group:
            for row in escalation_rows(report) or []:
                key = awb_key(row.get("awb", ""))
                if key:
                    out[(group_id, key)] = status_of(row)
        return out

    before = statuses((r["group_id"], r["report"]) for r in previous)
    now = statuses((g["group_id"], r.get("report", "")) for g, r in current)
    was_open = {k for k, s in before.items() if s in OPEN_STATUSES}
    is_open = {k for k, s in now.items() if s in OPEN_STATUSES}
    closed = {k for k in was_open if now.get(k) == CLOSED_STATUS}
    label = lambda keys: sorted(f"{awb} ({group})" for group, awb in keys)
    return dict(
        previous_open=len(was_open),
        still_open=label(was_open & is_open),
        verified_closed_since=label(closed),
        newly_opened=label(is_open - set(before)),
        not_mentioned_now=len(was_open - set(now)),
    )


def validate_narrative(text: str, group_ids: set[str], strict: bool) -> tuple[str, list[str]]:
    """Check AI prose against the rules the code relies on; strict mode removes AI totals blocks."""
    notes = []
    unknown = sorted({gid for gid in GROUP_TOKEN.findall(text)} - group_ids)
    if unknown:
        notes.append(f"Narrative cites {len(unknown)} group id(s) not in this run: {', '.join(unknown[:5])}.")
    fences = [f for f in re.findall(r"```json\s*[\s\S]*?```", text, re.I) if re.search(r'"raised"\s*:', f)]
    if fences:
        notes.append("Narrative contained its own totals JSON; code-computed totals are authoritative.")
        if strict:
            for fence in fences:
                text = text.replace(fence, "")
    return text, notes


def _cap(items: list[str], limit: int = 25) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" … (+{len(items) - limit} more)" if len(items) > limit else "") if items else "none"


def hybrid_narrative(reports, facts, carry):
    """Templates over source-checked objects; the master needs no additional AI call."""
    from .hybrid import safe
    tag = lambda g: "{{G:" + g["group_id"] + "|" + safe(g["group_name"]) + "}}"
    reviewed = [tag(g) for g, r in reports if r.get("reviewRequired")]
    sections = {
        "Executive overview": "Evidence-backed group findings follow. " + ("Provisional findings need review in: " + ", ".join(reviewed) if reviewed else "Extracted statements passed source checks; missing evidence remains unknown."),
        "Worst groups (ranked)": "\n".join("- " + tag(g) + f": {g['critical']} critical; {g['open_total']} open." for g in facts["worst_groups"]) or "No supported ranking available.",
        "Cross-group patterns": "\n".join(f"- {safe(c['category'])}: {c['groups']} groups, {c['rows']} cases." for c in facts["recurring_categories"]) or "No recurring supported category found.",
        "First vs Last Mile": f"First: {facts['totals']['first_mile']}; Last: {facts['totals']['last_mile']}. Unknown legs remain unclassified.",
        "Critical roll-up": "\n".join("- " + tag(g) + f": {g['critical']} critical." for g in facts["worst_groups"] if g["critical"]) or "No critical classification established; missing response evidence prevents SLA conclusions.",
        "Broken promises & fake closures": f"Promises without verified completion: {facts['totals']['promised_not_done']}; unconfirmed claims: {facts['totals']['claimed_closed_unconfirmed']}.",
        "Abuse / legal roll-up": "Review source evidence; absence of a classified flag does not establish absence of abuse.",
        "Staff responsiveness & engagement": "Unknown without linked seller/staff identities and meaningful-response evidence. No SLA or response-time estimate is inferred.",
        "Carry-over": "See the comparison with selected previous reports below." if carry else "No earlier report context selected; prior-window status is unknown.",
        "Recommendations": "Review provisional cases and missing evidence before operational decisions. Verify completion with the seller or authoritative shipment proof.",
    }
    return "\n\n".join("## " + heading + "\n" + body for heading, body in sections.items())


def render_sections(facts: dict, coverage: dict, carry: dict | None, notes: list[str]) -> str:
    lines = ["## Coverage and validation", ""]
    lines.append(
        f"- Eligible groups: {coverage['eligible']} — analysed {coverage['analysed']}, reused {coverage['reused']}, "
        f"no messages {coverage['skipped']}, failed {coverage['failed']}."
    )
    if coverage["failed_groups"]:
        lines.append("- Failed groups (not included in totals): "
                     + _cap([f"{g['group_name']} ({g['error']})" for g in coverage["failed_groups"]], 10) + ".")
    if coverage["paused"]:
        lines.append(f"- Paused groups not analysed: {coverage['paused']}.")
    lines.append(f"- Groups with machine counts in totals: {facts['groups_with_metrics']}.")
    lines.extend(f"- {note}" for note in facts["validation"] + notes)
    lines += ["", "## Verified aggregates (computed in code)", ""]
    if facts["worst_groups"]:
        lines.append("| Group | Raised | Critical | Open (pending + promised + unconfirmed) | Verified closed |")
        lines.append("| --- | --- | --- | --- | --- |")
        lines.extend(
            f"| {{{{G:{g['group_id']}|{g['group_name']}}}}} | {g['raised']} | {g['critical']} | {g['open_total']} | "
            f"{g['verified_closed']} |" for g in facts["worst_groups"]
        )
    if facts["recurring_categories"]:
        lines.append("")
        lines.append("Recurring categories across groups: " + "; ".join(
            f"{c['category']} — {c['groups']} groups, {c['rows']} rows" for c in facts["recurring_categories"]) + ".")
    lines.append("")
    lines.append(f"AWBs raised in more than one group (possible duplicates): {_cap(facts['cross_group_awbs'])}.")
    if carry is not None:
        lines += ["", "## Carry-over vs selected previous master reports (computed in code)", ""]
        lines.append(f"- Open in previous window: {carry['previous_open']}.")
        lines.append(f"- Still open: {len(carry['still_open'])} — {_cap(carry['still_open'])}.")
        lines.append(f"- Verified closed since: {len(carry['verified_closed_since'])} — "
                     f"{_cap(carry['verified_closed_since'])}.")
        lines.append(f"- Newly opened: {len(carry['newly_opened'])} — {_cap(carry['newly_opened'])}.")
        lines.append(f"- Previously open, not mentioned in this window (status unknown): {carry['not_mentioned_now']}.")
    return "\n".join(lines)


def prompt_facts(facts: dict, coverage: dict, carry: dict | None) -> str:
    """Compact JSON of verified facts for the master prompt (no grand totals to restate)."""
    return json.dumps(dict(
        coverage={k: v for k, v in coverage.items() if k != "failed_groups"},
        failed_groups=[g["group_name"] for g in coverage["failed_groups"]],
        worst_groups=facts["worst_groups"],
        recurring_categories=facts["recurring_categories"],
        cross_group_awbs=facts["cross_group_awbs"][:50],
        carry_over=None if carry is None else {
            k: (len(v) if isinstance(v, list) else v) for k, v in carry.items()
        },
    ), ensure_ascii=False)
