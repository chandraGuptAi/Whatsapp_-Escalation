"""Group report reuse, incremental prompts and deterministic master metrics."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import contextvars
import hashlib
import json
import logging
import statistics
import threading
from datetime import datetime
from pathlib import Path
import time
from ..errors import EscalationError
from .metrics import parse_metrics
from ..utils.ist import IST, resolve_window
from .cancellation import check_cancelled, current_check, AnalysisCancelled
from . import aggregate
from .provider import request_options
from urllib.parse import urlsplit
from .observe import RunStats, activity, current_activity, current_group_name, current_stats, stats
from .progress import Progress, TAIL_PHASES

PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
logger = logging.getLogger("escalation_analyst.master")


def prompt(name: str) -> tuple[str, str]:
    """Prompt text (re-read each run so edits apply) and its short content version."""
    text = (PROMPTS / name).read_text(encoding="utf-8")
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def transcript(messages: list[dict]) -> str:
    rows = []
    for m in messages:
        stamp = (
            datetime.fromtimestamp(m["timestamp"], IST)
            .strftime("%d %b, %I:%M %p")
            .replace("AM", "am")
            .replace("PM", "pm")
        )
        rows.append(
            f"[{stamp}] {m.get('author_name') or m.get('author') or 'Unknown'}: {m.get('body') or ''}"
        )
    text = "\n".join(rows)
    return "...(earlier messages trimmed)...\n" + text[-600000:] if len(text) > 600000 else text


class Analysis:
    def __init__(self, runtime):
        self.r = runtime

    def group_for_master(self, group_id: str, options: dict) -> dict | None:
        # Master and dashboard requests must share the same durable per-group claim.
        while not self.r.stop.is_set():
            check_cancelled()
            job, created = self.r.run_inline(
                "group", "group:" + group_id, options, lambda _: self.group(group_id, options)
            )
            # Another caller holds this group's claim: wait for it, then claim our own window.
            while job["status"] == "running" and not self.r.stop.wait(0.2):
                check_cancelled()
                job = self.r.jobs.get(job["id"], "group")
            if self.r.stop.is_set():
                raise EscalationError(503, "Report worker is shutting down")
            if job["status"] == "cancelled":
                raise AnalysisCancelled()
            if not created:
                continue  # A different window finished; acquire and resolve our own window now.
            if job["status"] == "error":
                raise EscalationError(job.get("error_status") or 502, job["error"] or "Group analysis failed")
            return None if job["status"] == "empty" else job["result"]
        raise EscalationError(503, "Report worker is shutting down")

    def group(self, group_id: str, options: dict) -> dict | None:
        result = self._group(group_id, options)
        if result and self.r.settings.engine_mode == "shadow" and self.r.settings.shadow_enabled:
            try:
                from .hybrid import extract, metrics
                window = resolve_window(options)
                messages = self.r.groups.messages(group_id, {**window, "inclusive": True})
                shadow = extract(messages, self.r.settings.hybrid_deep_awbs)
                proposed, actual = metrics(shadow), result.get("metrics")
                self.r.analysis_cache.observe(group_id, dict(mode="shadow", route_selected=shadow.route,
                    route_reason=shadow.reasons, proposed_metrics=proposed, legacy_metrics=actual,
                    metrics_differ=proposed != actual, report_id=result["reportId"], review_required=True))
            except Exception:
                logger.exception("Shadow comparison failed for %s; published legacy result retained", group_id)
        return result

    def _group(self, group_id: str, options: dict) -> dict | None:
        check_cancelled()
        run = stats()
        started = time.monotonic()
        group = self.r.groups.get(group_id)
        window = resolve_window(options)
        messages = self.r.groups.messages(group_id, {**window, "inclusive": True})
        if not messages:
            return None
        if self.r.settings.engine_mode == "hybrid":
            from .hybrid_service import HybridAnalysis
            system, version = prompt("hybrid-analyst.md")
            return HybridAnalysis(self.r).group(group, messages, window, options, system, version)
        system, version = prompt("escalation-analyst.md")
        from .hybrid import digest
        version = digest([version, self.r.settings.model, self.r.settings.base_url, request_options.get().get("reasoning") or self.r.settings.reasoning,
                          self.r.settings.max_tokens, group["group_name"], options.get("contextReportIds", [])])
        base = None if options.get("force") else self.r.reports.base(group_id, window, messages, version)
        # A lower-effort request can reuse a compatible higher-effort report, never the reverse.
        # This preserves completed work when applying the speed profile to a stopped run.
        effort = request_options.get().get("reasoning") or self.r.settings.reasoning
        levels = {"low": 0, "high": 1, "max": 2}
        if not base and not options.get("force") and levels.get(effort, -1) >= 0:
            for higher in ("high", "max"):
                if levels[higher] <= levels[effort]:
                    continue
                higher_version = digest([prompt("escalation-analyst.md")[1], self.r.settings.model,
                    self.r.settings.base_url, higher, self.r.settings.max_tokens,
                    group["group_name"], options.get("contextReportIds", [])])
                base = self.r.reports.base(group_id, window, messages, higher_version)
                if base:
                    break
        if run:
            run.add("db_read", time.monotonic() - started)
        fresh = messages
        if base:
            until = (
                base["period_end"]
                if base["max_message_id"] is not None
                else min(base["period_end"], base["created_at"])
            )
            max_id = (
                base["max_message_id"]
                if base["max_message_id"] is not None
                else max(m["id"] for m in messages)
            )
            fresh = [m for m in messages if m["timestamp"] > until or m["id"] > max_id]
            if not fresh:
                activity("reused", "reusing_saved_report")
                return dict(
                    reportId=base["id"],
                    report=base["report"],
                    messageCount=base["message_count"],
                    windowLabel=base["window_label"],
                    metrics=json.loads(base["metrics_json"]) if base["metrics_json"] else None,
                    reuse="existing",
                    baseReport={"id": base["id"], "windowLabel": base["window_label"]},
                    newMessageCount=0,
                )
            if not self.r.settings.incremental_enabled:
                base, fresh = None, messages
        activity("incremental" if base else "fresh", "ai_analysis")
        name = group["group_name"]
        user = f"Group: {name}\nWindow: {window['label']}\nTotal messages in window: {len(messages)}\n\n"
        for report_id in options.get("contextReportIds", []):
            previous = self.r.reports.get(report_id)
            if previous["group_id"] != group_id:
                continue
            user += f"Previous report ({previous['window_label']}):\n{previous['report']}\n\n"
        if base:
            user += f"MODE: INCREMENTAL UPDATE\nBase report covers: {base['window_label']} ({base['message_count']} messages, already analysed)\nBase report:\n\"\"\"\n{base['report']}\n\"\"\"\nNew transcript (messages the base report has not seen):\n"
        else:
            user += "Transcript:\n"
        user += '"""\n' + transcript(fresh) + '\n"""'
        ai_started = time.monotonic()
        text = self.r.provider.generate(system, user, dict(scope="group", group_id=group_id, group_name=name))
        if run:
            run.add("group_ai", time.monotonic() - ai_started)
        check_cancelled()
        activity("incremental" if base else "fresh", "saving_report")
        metrics = parse_metrics(text)
        values = dict(
            group_id=group_id,
            group_name=name,
            report=text,
            message_count=len(messages),
            window_label=window["label"],
            period_start=window["from"],
            period_end=window["to"],
            model=self.r.settings.model,
            trigger=options.get("trigger", "dashboard"),
            scope="group",
            metrics_json=json.dumps(metrics) if metrics else None,
            max_message_id=max(m["id"] for m in messages),
            base_report_id=base["id"] if base else None,
            prompt_version=version,
            message_hash=digest(messages),
        )
        report_id = self.r.reports.save(values)
        delivered = False
        if options.get("deliver") and self.r.settings.recipient:
            try:
                self.r.whatsapp.send(self.r.settings.recipient, text)
                delivered = True
            except Exception:
                pass  # Saved analysis remains successful when outbound delivery fails.
        return dict(
            reportId=report_id,
            report=text,
            messageCount=len(messages),
            windowLabel=window["label"],
            metrics=metrics,
            reuse="extended" if base else None,
            baseReport={"id": base["id"], "windowLabel": base["window_label"]} if base else None,
            newMessageCount=len(fresh),
            delivered=delivered,
        )

    # --- master run ---------------------------------------------------------------------------

    def master(self, options: dict, job_id: int) -> dict:
        run = RunStats(job_id)
        stats_token = current_stats.set(run)
        progress = Progress(self.r.jobs, job_id, self.r.settings, self._tail_history(), lambda: run.mean("ai_request"))
        progress.state["execution"] = dict(master_workers=self.r.settings.master_concurrency,
            ai_max_concurrency=self.r.settings.ai_max_concurrency,
            group_reasoning=self.r.settings.master_group_reasoning or self.r.settings.reasoning,
            master_reasoning=self.r.settings.reasoning)
        run.on_retry = lambda attempt, error: progress.event(
            "retry", f"{current_group_name.get() or 'Master report'}: AI retry {attempt} after {error}"
        )
        started = time.monotonic()
        outcome = "error"
        try:
            result = self._master(options, job_id, progress, run)
            outcome = "complete"
            return result
        except AnalysisCancelled:
            outcome = "cancelled"
            progress.close("cancelled", "Run stopped; completed group reports were saved.")
            raise
        except Exception as exc:
            message = exc.message if isinstance(exc, EscalationError) else "Report generation failed; see server logs."
            progress.close("failed", message)
            raise
        finally:
            current_stats.reset(stats_token)
            state = progress.snapshot()
            logger.info("Escalation master run %s metrics %s", job_id, json.dumps(dict(
                outcome=outcome,
                duration_seconds=round(time.monotonic() - started, 3),
                groups_selected=state["total_groups"],
                groups_eligible=state["total_eligible_groups"],
                groups_processed=state["completed_groups"],
                **state["counts"],
                phase_seconds=state["phase_seconds"],
                **run.summary(),
            )))

    def _master(self, options: dict, job_id: int, progress: Progress, run: RunStats) -> dict:
        cluster_id = options.get("clusterId", "all")
        self.r.clusters.get(cluster_id, active=True)
        window = resolve_window(options)
        with run.timed("db_read"):
            groups = self.r.scope_groups("cluster", cluster_id)
        if options.get("groupIds") is not None:
            selected = set(options["groupIds"])
            groups = [g for g in groups if g["group_id"] in selected]
        eligible = [g for g in groups if g["is_active"]]
        with run.timed("db_read"):
            counts = self.r.groups.window_counts([g["group_id"] for g in eligible], window)
        progress.groups(len(groups), len(eligible))
        progress.workload(counts)
        progress.phase("groups", f"Processing {len(eligible)} eligible groups")
        # Master report ids are master-level context; they never match a group's own reports.
        group_options = {k: v for k, v in {**options, **window}.items() if k != "contextReportIds"}
        work, empty = [], []
        for group in eligible:
            if counts.get(group["group_id"]):
                work.append(group)
            else:
                empty.append(group)
        progress.skip_empty(empty)
        reports, failures = self._run_groups(work, group_options, progress)
        coverage = dict(eligible=len(eligible), paused=len(groups) - len(eligible),
                        **progress.snapshot()["counts"], failed_groups=failures)
        if not reports:
            if failures:
                raise EscalationError(502, f"All {len(failures)} group analyses failed; retry the analysis.")
            return progress.close("complete", "No messages in active groups", note="No messages in active groups")

        progress.phase("aggregating", "Aggregating verified group results")
        with run.timed("aggregation"):
            facts = aggregate.summarize(reports)
            history, carry = self._history(options, cluster_id, {g["group_id"] for g in groups}, reports)
            system, version = prompt("master-analyst.md")
            header = (
                f"Window: {window['label']}\n"
                "Verified facts (computed in code; authoritative for rankings, coverage and carry-over; "
                "do not restate grand totals):\n" + aggregate.prompt_facts(facts, coverage, carry) + "\n\n"
            )
            if history:
                header += (
                    "HISTORICAL CONTEXT: earlier master reports for earlier windows. Use it only to classify "
                    "carry-over (still open, newly opened, verified closed). Never add its numbers to this "
                    "window.\n" + history + "\n\n"
                )
            batches = self._batches(reports, self.r.settings.master_prompt_max_chars - len(header))
        check_cancelled()

        progress.phase("master", "Generating master narrative")
        meta = dict(scope="master", group_id="__master__:" + cluster_id, group_name=cluster_id)
        with run.timed("master_narrative"):
            if self.r.settings.engine_mode == "hybrid":
                text = aggregate.hybrid_narrative(reports, facts, carry)
            else:
                if len(batches) > 1:
                    batches = self._digest(system, batches, meta)
                check_cancelled()
                text = self.r.provider.generate(system, header + "Per-group reports:\n" + "\n".join(batches), meta)
        check_cancelled()

        progress.phase("validation", "Validating narrative and totals")
        with run.timed("validation"):
            ids = {g["group_id"] for g, _ in reports}
            text, notes = aggregate.validate_narrative(text, ids, self.r.settings.strict_validation)
            totals = facts["totals"]
            text += "\n\n" + aggregate.render_sections(facts, coverage, carry, notes)
            text += "\n\n## Computed totals\n```json\n" + json.dumps(totals, indent=2) + "\n```"
        check_cancelled()
        report_id = self.r.reports.save(
            dict(
                group_id="__master__:" + cluster_id,
                group_name=cluster_id,
                scope="master",
                cluster_id=cluster_id,
                report=text,
                message_count=sum(r["messageCount"] for _, r in reports),
                window_label=window["label"],
                period_start=window["from"],
                period_end=window["to"],
                model=self.r.settings.model,
                trigger=options.get("trigger", "dashboard"),
                metrics_json=json.dumps(totals),
                prompt_version=version,
                group_reports={g["group_id"]: result["reportId"] for g, result in reports},
            )
        )
        return progress.close("complete", "Master report saved", master_report_id=report_id,
                              validation_notes=facts["validation"] + notes)

    def _run_groups(self, groups: list[dict], options: dict, progress: Progress) -> tuple[list, list]:
        """Analyse groups with at most `master_concurrency` in flight; one failure never aborts others."""
        results: dict[str, dict] = {}
        failures: list[dict] = []
        abort = threading.Event()
        master_check = current_check.get()

        def check():
            if abort.is_set():
                raise AnalysisCancelled()
            if master_check:
                master_check()

        def worker(group: dict):
            current_check.set(check)
            current_activity.set(lambda kind, operation: progress.activity(group, kind, operation))
            current_group_name.set(group["group_name"])
            progress.begin(group)
            # Use the faster effort only on the verified DeepSeek v4 provider path.
            # Hybrid already owns its own per-route effort. Final master calls inherit normal settings.
            effort = self.r.settings.master_group_reasoning
            override = (effort and self.r.settings.engine_mode != "hybrid"
                        and urlsplit(self.r.settings.base_url).hostname == "api.deepseek.com"
                        and self.r.settings.model == "deepseek-v4-pro")
            token = request_options.set({"reasoning": effort}) if override else None
            try:
                return self.group_for_master(group["group_id"], options)
            except (AnalysisCancelled, EscalationError) as exc:
                if isinstance(exc, AnalysisCancelled) or self.r.stop.is_set():
                    progress.release(group)
                raise
            finally:
                if token is not None:
                    request_options.reset(token)

        order = {g["group_id"]: i for i, g in enumerate(groups)}
        queue = iter(groups)
        limit = max(1, self.r.settings.master_concurrency)
        with ThreadPoolExecutor(max_workers=limit, thread_name_prefix="escalation-master") as pool:
            running = {}

            def schedule(count: int) -> None:
                for group in queue:
                    running[pool.submit(contextvars.copy_context().run, worker, group)] = group
                    count -= 1
                    if count <= 0:
                        return

            def settle(future) -> None:
                group = running.pop(future)
                try:
                    result = future.result()
                except AnalysisCancelled:
                    raise
                except Exception as exc:
                    # 503 = the service cannot analyse anything (e.g. AI key missing, shutting down):
                    # stop the run now instead of failing every remaining group the same way.
                    if self.r.stop.is_set() or isinstance(exc, EscalationError) and exc.status == 503:
                        raise
                    message = exc.message if isinstance(exc, EscalationError) else "analysis failed"
                    if not isinstance(exc, EscalationError):
                        logger.exception("Escalation group %s failed in master run", group["group_id"])
                    failures.append(dict(group_id=group["group_id"], group_name=group["group_name"], error=message))
                    progress.finish(group, "failed", message)
                    return
                if result is None:
                    progress.finish(group, "skipped", "no messages in window")
                elif result.get("reuse") == "existing":
                    results[group["group_id"]] = result
                    progress.checkpoint(group["group_id"], result["reportId"])
                    progress.finish(group, "reused", "saved analysis reused")
                else:
                    results[group["group_id"]] = result
                    progress.checkpoint(group["group_id"], result["reportId"])
                    kind = "incremental" if result.get("reuse") == "extended" else "fresh"
                    progress.finish(group, "analysed", f"{kind} analysis saved")

            try:
                schedule(limit)
                while running:
                    done, _ = wait(running, timeout=0.5, return_when=FIRST_COMPLETED)
                    for future in done:  # Count finished groups before honouring a stop.
                        settle(future)
                    check_cancelled()
                    schedule(len(done))
            except BaseException:
                abort.set()  # Stop scheduling and make active workers stop at their next check.
                for future in list(running):
                    if future.cancel():
                        running.pop(future)
                pool.shutdown(wait=True)
                # Groups that saved their report while the stop propagated still count as processed.
                for future in list(running):
                    if future.exception() is None:
                        settle(future)
                raise
        by_id = {g["group_id"]: g for g in groups}
        ordered = sorted(results, key=order.get)
        return [(by_id[i], results[i]) for i in ordered], failures

    def _batches(self, reports: list[tuple[dict, dict]], limit: int) -> list[str]:
        """Full reports when they fit; else compact ones; else size-bounded batches for digests."""

        def blocks(shrink):
            return [
                f"### GROUP id={g['group_id']} name=\"{g['group_name']}\"\n(When citing this group, tag it EXACTLY as {{{{G:{g['group_id']}|{g['group_name']}}}}})\n{shrink(r['report'])}"
                for g, r in reports
            ]

        selected = blocks(lambda text: text)
        if sum(len(b) + 1 for b in selected) > limit:
            selected = blocks(aggregate.compact)
        batches, current = [], ""
        for block in selected:
            if current and len(current) + len(block) > limit:
                batches.append(current)
                current = ""
            current += block + "\n"
        if current:
            batches.append(current)
        return batches

    def _digest(self, system: str, batches: list[str], meta: dict) -> list[str]:
        """Digest oversized input in parallel (bounded by the provider's own limit), keeping order."""
        workers = max(1, min(len(batches), self.r.settings.ai_max_concurrency))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="escalation-digest") as pool:
            futures = [
                pool.submit(
                    contextvars.copy_context().run,
                    self.r.provider.generate,
                    system,
                    "Produce a partial digest; preserve every {{G:...}} token.\n" + batch,
                    meta,
                )
                for batch in batches
            ]
            return [f"### BATCH DIGEST {i + 1}\n{f.result()}" for i, f in enumerate(futures)]

    def _history(self, options: dict, cluster_id: str, group_ids: set[str], reports: list) -> tuple[str, dict | None]:
        """Compact context from selected previous master reports, plus code-computed carry-over."""
        texts, previous_groups = [], []
        selected = sorted((self.r.reports.get(i) for i in options.get("contextReportIds", [])),
                          key=lambda row: (row["period_end"], row["id"]))  # Latest status wins in carry-over.
        for previous in selected:
            if previous["scope"] != "master" or previous["group_id"] != "__master__:" + cluster_id:
                continue  # Validated at submission; never mix clusters if data changed since.
            narrative = previous["report"].split("\n## Coverage and validation")[0].split("\n## Computed totals")[0]
            texts.append(
                f"### PREVIOUS MASTER {previous['window_label']} (report {previous['id']})\n"
                f"Code-computed totals then: {previous['metrics_json'] or 'unavailable'}\n"
                + narrative[:8000]
                + ("\n…(trimmed)" if len(narrative) > 8000 else "")
            )
            previous_groups += self.r.reports.window_reports(
                group_ids, previous["period_start"], previous["period_end"] + 1
            )
        if not texts:
            return "", None
        return "\n\n".join(texts), aggregate.carry_over(previous_groups, reports)

    def _tail_history(self) -> float | None:
        """Median measured aggregation + master + validation time of recent completed runs."""
        tails = []
        for job in self.r.jobs.list("master")[:20]:
            seconds = (job["result"] or {}).get("phase_seconds") or {}
            if job["status"] == "complete" and all(p in seconds for p in TAIL_PHASES):
                tails.append(sum(seconds[p] for p in TAIL_PHASES))
            if len(tails) == 5:
                break
        return statistics.median(tails) if tails else None
