"""Feature-flagged hybrid integration, with immutable validated checkpoints."""
from __future__ import annotations
import json
import logging
import time

from . import hybrid
from .cancellation import check_cancelled
from .observe import activity, stats
from .provider import request_options
from ..errors import EscalationError

logger = logging.getLogger("escalation_analyst.hybrid")


class HybridAnalysis:
    def __init__(self, runtime):
        self.r = runtime

    def group(self, group, messages, window, options, system, prompt_version):
        r, s = self.r, self.r.settings
        start = time.monotonic()
        message_hash = hybrid.digest(messages)
        # Full content (including enriched media, author and timestamp), context and all
        # behaviour-affecting configuration. Never key by max(id) or TTL alone.
        context = [r.reports.get(i) for i in options.get("contextReportIds", [])]
        config_hash = hybrid.digest(dict(schema=hybrid.SCHEMA_VERSION, analyzer=hybrid.VERSION,
            prompt=prompt_version, model=s.model, url=s.base_url, reasoning=s.reasoning,
            light=s.hybrid_light_reasoning, deep=s.hybrid_deep_reasoning, tokens=s.hybrid_max_tokens,
            threshold=s.hybrid_deep_awbs, routing=s.ai_routing_enabled, context=context,
            name=group["group_name"], window=window))
        key = hybrid.digest([group["group_id"], message_hash, config_hash])
        cached = None if options.get("force") else r.analysis_cache.get(key)
        if cached:
            result = hybrid.validate(hybrid.Result.model_validate(cached["payload"]))
            report = r.reports.get(cached["report_id"])
            activity("reused", "reusing_saved_report")
            run = stats()
            if run:
                run.count("hybrid_cache_hits")
            return self._public(result, report["id"], report["report"], len(result.evidence), window, True)
        activity("fresh", "normalizing")
        units, reused_units = r.analysis_cache.units(messages) if s.hybrid_incremental_enabled else (None, 0)
        result = hybrid.extract(messages, s.hybrid_deep_awbs, units)
        python_ms = (time.monotonic() - start) * 1000
        ai_ms = 0.0
        if result.route != "deterministic":
            if not s.ai_routing_enabled:
                raise EscalationError(422, "This group needs semantic analysis; enable AI routing or use legacy mode.")
            activity("fresh", "ai_analysis")
            evidence = [e.model_dump(exclude={"author", "wa_id"}) for e in result.evidence]
            for item in evidence:
                item["quote"] = hybrid.ai_quote(item["quote"])
            # Send only evidence, not phone identities or previous generated narratives.
            # Prior reports cannot be silently dropped; unsupported history remains reviewable.
            if context:
                raise EscalationError(422, "Hybrid historical context needs review; use legacy mode for selected prior reports.")
            user = json.dumps(dict(evidence=evidence, extracted_cases=[c.model_dump() for c in result.cases],
                                   required_task="resolve_ambiguity", schema=hybrid.AIResult.model_json_schema()), ensure_ascii=False)
            if len(user) > s.master_prompt_max_chars:
                raise EscalationError(422, "Group evidence exceeds the hybrid input budget; manual review required.")
            token = request_options.set(dict(max_tokens=s.hybrid_max_tokens, json=True, max_retries=0,
                reasoning=s.hybrid_deep_reasoning if result.route == "deep" else s.hybrid_light_reasoning))
            ai_start = time.monotonic()
            # Identical semantic evidence can reuse its paid response across window
            # changes; deterministic totals/window labels are recomputed separately.
            call_key = hybrid.digest([group["group_id"], system, user, s.model, s.base_url,
                                      result.route, s.reasoning, s.hybrid_light_reasoning, s.hybrid_deep_reasoning, s.hybrid_max_tokens])
            try:
                call, created = r.analysis_cache.reserve_ai(call_key)
                if created:
                    raw = r.provider.generate(system, user, dict(scope="group", group_id=group["group_id"], group_name=group["group_name"]))
                    usage = request_options.get().get("usage", {})
                    r.analysis_cache.complete_ai(call_key, raw, usage)
                elif call["status"] == "complete":
                    raw, usage = call["response"], json.loads(call["usage_json"] or "{}")
                else:
                    raise EscalationError(409, "An earlier AI request has an uncertain outcome; review it before another paid call.")
                parsed = hybrid.AIResult.model_validate_json(raw)
                result.ai_usage = usage
                result.cases = parsed.cases
                # Semantic results remain provisional until golden-dataset validation and review.
                result.review_required = True
                result.missing_evidence += parsed.missing_evidence
            except ValueError as exc:
                raise EscalationError(422, "Hybrid analysis failed evidence/schema validation; manual review required.") from exc
            finally:
                request_options.reset(token)
            ai_ms = (time.monotonic() - ai_start) * 1000
        elif context:
            raise EscalationError(422, "Hybrid historical context needs review; use legacy mode for selected prior reports.")
        check_cancelled()
        validation_start = time.monotonic()
        try:
            hybrid.validate(result)
        except ValueError as exc:
            raise EscalationError(422, "Hybrid analysis failed evidence validation; manual review required.") from exc
        validation_ms = (time.monotonic() - validation_start) * 1000
        result.processing_duration_ms = round((time.monotonic() - start) * 1000, 3)
        text = hybrid.render(result)
        report_id = r.reports.save(dict(group_id=group["group_id"], group_name=group["group_name"], report=text,
            message_count=len(result.evidence), window_label=window["label"], period_start=window["from"], period_end=window["to"],
            model=s.model if ai_ms else "python", trigger=options.get("trigger", "dashboard"), scope="group",
            metrics_json=json.dumps(hybrid.metrics(result)), max_message_id=max(m["id"] for m in messages), prompt_version=prompt_version))
        r.analysis_cache.save(key, group["group_id"], message_hash, config_hash, result.model_dump(), report_id)
        observation = dict(route_selected=result.route, route_reason=result.reasons, cache_hit=False,
            job_id=stats().job_id if stats() else None, processing_stage="saved",
            cache_key=key, report_id=report_id, ai_usage=result.ai_usage,
            reused_message_units=reused_units,
            python_processing_ms=round(python_ms, 3), model_latency_ms=round(ai_ms, 3),
            validation_ms=round(validation_ms, 3), processing_ms=round((time.monotonic() - start) * 1000, 3),
            validation_status="review_required" if result.review_required else "validated", message_hash=message_hash,
            model_name=s.model if ai_ms else "python", last_message_timestamp=max(e.timestamp or 0 for e in result.evidence))
        r.analysis_cache.observe(group["group_id"], observation)
        logger.info("Group %s analysis %s", group["group_id"], json.dumps(observation))
        run = stats()
        if run:
            run.count("route_" + result.route)
            run.add("hybrid_group", time.monotonic() - start)
        delivered = False
        if options.get("deliver") and s.recipient:
            r.whatsapp.send(s.recipient, text)
            delivered = True
        return {**self._public(result, report_id, text, len(result.evidence), window, False), "delivered": delivered}

    @staticmethod
    def _public(result, report_id, text, count, window, reused):
        return dict(reportId=report_id, report=text, messageCount=count, windowLabel=window["label"],
                    metrics=hybrid.metrics(result), reuse="existing" if reused else None,
                    newMessageCount=0 if reused else count, reviewRequired=result.review_required,
                    structuredAnalysis=result.model_dump())
