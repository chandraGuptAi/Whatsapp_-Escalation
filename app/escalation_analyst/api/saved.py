"""Read-only master view built from already-saved group reports: no AI call, no writes."""

from __future__ import annotations

import json
import time
from fastapi import APIRouter, Depends, Query
from .dependencies import runtime, user
from ..analysis import aggregate
from ..utils.ist import format_time, resolve_window

router = APIRouter(dependencies=[Depends(user)], tags=["Escalation saved reports"])


def _cell(value) -> str:
    return str(value).replace("|", "/").replace("\n", " ")


@router.get("/master/saved-report")
def saved_report(
    runId: int | None = None,
    clusterId: str = "all",
    fromDate: str | None = None,
    toDate: str | None = None,
    hours: float | None = Query(default=None, gt=0, le=8760),
    r=Depends(runtime),
) -> dict:
    if runId is not None:
        request = r.jobs.get(runId, "master")["request"]
        clusterId = request.get("clusterId", clusterId)
        window = {"from": request["from"], "to": request["to"], "label": request.get("label") or "run window"}
    else:
        window = resolve_window({"fromDate": fromDate, "toDate": toDate, "hours": hours}
                                if fromDate or toDate else {"hours": hours or 24})
    groups = r.scope_groups("cluster", clusterId)
    eligible = {g["group_id"]: g for g in groups if g["is_active"]}
    rows = r.reports.overlapping_reports(set(eligible), window["from"], window["to"])
    reports = [
        (eligible[row["group_id"]],
         dict(report=row["report"], metrics=json.loads(row["metrics_json"]) if row["metrics_json"] else None))
        for row in rows
    ]
    facts = aggregate.summarize(reports)
    saved_ids = {row["group_id"] for row in rows}
    missing = [dict(group_id=g["group_id"], group_name=g["group_name"]) for gid, g in eligible.items() if gid not in saved_ids]
    coverage = dict(eligible=len(eligible), with_saved_report=len(rows), without_saved_report=len(missing),
                    paused=len(groups) - len(eligible))
    by_group = sorted(
        (dict(group_id=row["group_id"], group_name=eligible[row["group_id"]]["group_name"], report_id=row["id"],
              window_label=row["window_label"], created_at=row["created_at"],
              metrics=json.loads(row["metrics_json"]) if row["metrics_json"] else None) for row in rows),
        key=lambda g: (-((g["metrics"] or {}).get("critical") or 0), -((g["metrics"] or {}).get("raised") or 0),
                       g["group_name"]),
    )
    totals = facts["totals"]
    lines = [
        "# Saved-data report (no AI)",
        "",
        f"Window: {window['label']} · Cluster: {clusterId} · Built {format_time(int(time.time()))} from saved group "
        "reports. No AI call was made; every number below is computed in code.",
        "",
        "## Coverage",
        f"- Eligible groups: {coverage['eligible']} — with a saved report: {coverage['with_saved_report']}, "
        f"without one: {coverage['without_saved_report']}" + (f", paused: {coverage['paused']}" if coverage["paused"] else "") + ".",
        "- Groups without a saved report either had no messages or have not been analysed yet; they are not counted.",
        *[f"- {note}" for note in facts["validation"]],
        "",
        "## Totals",
        f"- Raised: {totals['raised']} · Pending: {totals['pending']} · Promised not done: {totals['promised_not_done']} · "
        f"Claimed, unconfirmed: {totals['claimed_closed_unconfirmed']} · Verified closed: {totals['verified_closed']}",
        f"- Critical: {totals['critical']} · Abuse/legal: {totals['abuse_legal']} · First mile: {totals['first_mile']} · "
        f"Last mile: {totals['last_mile']} · Missed responses: {totals['missed']}",
    ]
    if facts["worst_groups"]:
        lines += ["", "## Worst groups", "| Group | Raised | Critical | Open | Verified closed |", "| --- | --- | --- | --- | --- |"]
        lines += [f"| {{{{G:{g['group_id']}|{_cell(g['group_name'])}}}}} | {g['raised']} | {g['critical']} | "
                  f"{g['open_total']} | {g['verified_closed']} |" for g in facts["worst_groups"]]
    if facts["recurring_categories"]:
        lines += ["", "## Recurring categories"]
        lines += [f"- {_cell(c['category'])}: {c['groups']} groups, {c['rows']} cases" for c in facts["recurring_categories"]]
    if facts["cross_group_awbs"]:
        lines += ["", "## AWBs raised in more than one group", ", ".join(facts["cross_group_awbs"][:50])]
    if by_group:
        lines += ["", "## Group reports used", "| Group | Report window | Raised | Critical | Saved |", "| --- | --- | --- | --- | --- |"]
        lines += [f"| {{{{G:{g['group_id']}|{_cell(g['group_name'])}}}}} | {_cell(g['window_label'])} | "
                  f"{(g['metrics'] or {}).get('raised', '—')} | {(g['metrics'] or {}).get('critical', '—')} | "
                  f"{format_time(g['created_at'])} |" for g in by_group]
    return dict(
        clusterId=clusterId,
        window=window,
        coverage=coverage,
        totals=totals,
        worst_groups=facts["worst_groups"],
        recurring_categories=facts["recurring_categories"],
        cross_group_awbs=facts["cross_group_awbs"],
        validation=facts["validation"],
        groups=by_group,
        missing_groups=missing,
        ai_calls=0,
        report="\n".join(lines),
    )
