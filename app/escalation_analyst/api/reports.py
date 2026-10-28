"""History and master analysis endpoints."""

from __future__ import annotations

import time
import asyncio
import json
from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import StreamingResponse
from .dependencies import runtime, user
from .models import AnalysisInput
from ..utils.ist import date_filter, day, midnight, resolve_window
from ..exports.documents import export
from ..repositories.jobs import STOP_REQUEST
from ..errors import EscalationError

router = APIRouter(dependencies=[Depends(user)], tags=["Escalation reports"])


@router.get("/master/directory")
def master_directory(r=Depends(runtime)) -> dict:
    from ..analysis.master_rows import directory
    return directory(r)


@router.put("/master/directory")
def save_master_directory(body: dict, r=Depends(runtime)) -> dict:
    from ..performance.mappings import validate_mappings
    if len(json.dumps(body).encode()) > 8 * 1024 * 1024:
        raise EscalationError(400, "Directory exceeds 8MB")
    mappings = validate_mappings(body)
    r.db.write("INSERT INTO master_report_directory(id,payload) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
               (json.dumps(mappings),))
    return {"counts": {key: len(value) for key, value in mappings.items()}}


@router.get("/master/reports/{report_id}/rows")
def master_report_rows(report_id: int, r=Depends(runtime)) -> dict:
    from ..analysis.master_rows import HEADERS, report_rows, children_for_report
    report = r.reports.get(report_id)
    children = children_for_report(r, report)
    return dict(headers=HEADERS, rows=report_rows(r, children),
                countScope="Saved report window; one entry per seller message and AWB",
                identityNote="Identities require an ID and name match in the master directory.")


@router.get("/reports/history/summary")
def summary(r=Depends(runtime)) -> dict:
    reports = r.reports.history()
    return dict(
        total_reports=len(reports),
        reports_today=sum(row["created_at"] >= midnight(day(time.time())) for row in reports),
        groups_analyzed=len({row["group_id"] for row in reports if row["scope"] == "group"}),
        latest_report=reports[0] if reports else None,
    )


@router.get("/reports/history")
def history(
    scope: str = Query("all", pattern="^(all|group|master)$"),
    search: str = Query("", max_length=200),
    fromDate: str | None = None,
    toDate: str | None = None,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    r=Depends(runtime),
) -> dict:
    rows = r.reports.history(scope=scope, search=search, window=date_filter(fromDate, toDate))
    return dict(reports=rows[offset : offset + limit], total=len(rows), limit=limit, offset=offset)


@router.get("/reports/{report_id}")
def report(report_id: int, r=Depends(runtime)) -> dict:
    return {"report": r.reports.get(report_id)}


@router.get("/reports/{report_id}/download")
def download(report_id: int, format: str = "md", r=Depends(runtime)) -> Response:
    report = r.reports.get(report_id)
    if format == "pdf":
        from ..exports.report_pdf import report_pdf

        subject = None
        if report["scope"] == "master":
            try:
                subject = r.clusters.get(report.get("cluster_id") or report["group_name"])["name"]
            except Exception:
                subject = None  # A deleted cluster still exports under its stored id.
        # Inline so the browser can preview it; the frontend also offers a download.
        return Response(
            report_pdf(report, subject),
            media_type="application/pdf",
            headers={"Content-Disposition": f'inline; filename="report-{report_id}.pdf"'},
        )
    content, media = export(report["report"], format)
    return Response(
        content,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="report-{report_id}.{format}"'},
    )


@router.get("/master/reports")
def masters(clusterId: str = "all", days: int = Query(7, ge=1, le=90), r=Depends(runtime)) -> dict:
    rows = r.reports.history(
        "__master__:" + clusterId,
        window={"from": int(time.time()) - days * 86400, "to": int(time.time()) + 1},
    )
    running = next((j for j in r.jobs.list("master") if j["status"] == "running"), None)
    return dict(reports=rows, lock={"running": bool(running)}, newSince=None)


@router.get("/master/context-suggestions")
def context(
    clusterId: str = "all", beforeTs: int = Query(ge=0), limit: int = Query(2, ge=1, le=5), r=Depends(runtime)
) -> dict:
    return {"suggestions": r.reports.context("__master__:" + clusterId, beforeTs, limit)}


@router.post("/master/analyse")
def master(body: AnalysisInput, r=Depends(runtime)) -> dict:
    options = body.model_dump(exclude_none=True)
    r.clusters.get(body.clusterId, active=True)
    if body.groupIds is not None:
        allowed = {g["group_id"] for g in r.scope_groups("cluster", body.clusterId) if g["is_active"]}
        selected = set(body.groupIds)
        if not selected or not selected.issubset(allowed):
            raise EscalationError(400, "Select at least one enabled group belonging to this cluster.")
    if not options.get("hours") and not options.get("fromDate"):
        options["mode"] = "today-so-far"
    options.update(resolve_window(options))
    for report_id in body.contextReportIds:
        previous = r.reports.get(report_id)  # 404 when missing.
        if previous["scope"] != "master" or previous["group_id"] != "__master__:" + body.clusterId:
            raise EscalationError(400, f"Report {report_id} is not a master report for cluster {body.clusterId}.")
        if previous["period_start"] >= options["from"]:
            raise EscalationError(400, f"Report {report_id} is not earlier than the selected window.")
    job, created = r.start("master", "master", options, lambda job_id: r.analysis.master(options, job_id))
    return dict(runId=job["id"], status="started" if created else "in_progress")


@router.get("/master/progress")
def progress(runId: int | None = None, clusterId: str = "all", r=Depends(runtime)) -> dict:
    # A run whose worker died (restart/crash) stops heartbeating; release it instead of showing it running.
    r.jobs.recover_expired(r.settings.job_stale_seconds)
    rows = (
        [r.jobs.get(runId, "master")]
        if runId
        else [j for j in r.jobs.list("master") if j["request"].get("clusterId", "all") == clusterId]
    )
    job = rows[0] if rows else None
    run = (
        {**(job["result"] or {}), "id": job["id"], "status": job["status"],
         "stopRequested": job["error"] == STOP_REQUEST,
         "error": None if job["error"] == STOP_REQUEST else job["error"]}
        if job
        else None
    )
    return dict(run=run, lock={"running": any(j["status"] == "running" for j in r.jobs.list("master"))})


@router.post("/master/runs/{run_id}/stop", status_code=202)
def stop_master(run_id: int, r=Depends(runtime)) -> dict:
    job = r.jobs.request_stop(run_id)
    return dict(runId=job["id"], status=job["status"], stopRequested=job["error"] == STOP_REQUEST)


@router.post("/master/runs/{run_id}/retry")
def retry_master(run_id: int, r=Depends(runtime)):
    r.jobs.recover_expired(r.settings.job_stale_seconds)
    old = r.jobs.get(run_id, "master")
    if old["status"] not in {"error", "cancelled"}:
        raise EscalationError(409, "Only failed or stopped runs can be retried")
    # Frozen time window, not a new rolling 'last 24h'; cache reuses completed work.
    options = {k: v for k, v in old["request"].items() if k != "requestKey"}
    options["force"] = False
    job, created = r.start("master", "master", options, lambda job_id: r.analysis.master(options, job_id))
    return dict(runId=job["id"], status="started" if created else "in_progress")


@router.get("/master/runs/{run_id}/events")
def master_events(run_id: int, r=Depends(runtime)):
    r.jobs.get(run_id, "master")

    async def events():
        previous = None
        while True:
            value = await asyncio.to_thread(progress, runId=run_id, r=r)
            data = json.dumps(value, sort_keys=True)
            if data != previous:
                yield "event: progress\ndata: " + data + "\n\n"
                previous = data
            else:
                yield ": heartbeat\n\n"
            if value["run"]["status"] != "running":
                break
            await asyncio.sleep(2)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/master/bulk-export")
def bulk_export(
    reportId: int | None = None,
    runId: int | None = None,
    clusterId: str = "all",
    fromDate: str | None = None,
    toDate: str | None = None,
    r=Depends(runtime),
) -> Response:
    from ..exports.bulk import export_bulk

    window = date_filter(fromDate, toDate)
    report = r.reports.get(reportId) if reportId is not None else None
    if report is not None and report["scope"] == "master":
        children = r.reports.children(reportId)
        if children:
            return Response(export_bulk(children, r), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": 'attachment; filename="master_escalations.xlsx"'})
    if runId is not None:
        checkpoint = r.jobs.get(runId, "master")["result"] or {}
        if "group_report_ids" in checkpoint:
            children = [r.reports.get(i) for i in checkpoint["group_report_ids"].values()]
            return Response(export_bulk(children, r), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            headers={"Content-Disposition": 'attachment; filename="master_escalations.xlsx"'})
    if report is not None and report["scope"] != "master":
        selected = [report]
    else:
        if report is not None:
            clusterId = report.get("cluster_id") or report["group_id"].removeprefix("__master__:")
            window = {"from": report["period_start"], "to": report["period_end"] + 1}
        if runId is not None:
            job = r.jobs.get(runId, "master")
            window = {"from": job["request"]["from"], "to": job["request"]["to"] + 1}
        ids = {g["group_id"] for g in r.scope_groups("cluster", clusterId)}
        selected = [
            row
            for row in r.reports.history(scope="group")
            if row["group_id"] in ids
            and (not window or row["period_start"] >= window["from"] and row["period_end"] < window["to"])
        ]
        latest = {}
        for row in selected:
            latest.setdefault(row["group_id"], row)
        selected = list(latest.values())
    return Response(
        export_bulk(selected, r),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="master_escalations.xlsx"'},
    )
