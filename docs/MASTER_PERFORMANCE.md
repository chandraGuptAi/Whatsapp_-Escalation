# Master report performance

Local read-only investigation on 2026-10-26: run 397 completed 25 group reports in about 26.5 minutes, with 235 empty groups skipped. The preceding run saved 45 group reports in about 40 minutes. Both reported zero reused groups. Across 70 local group usage records, average completion usage was about 11,479 tokens, maximum 44,040. Completion usage may include thinking tokens; the older local schema has no separate reasoning/latency columns. Do not present this as measured reasoning-token usage.

The local configuration used deepseek-v4-pro, high reasoning, an 80,000-token output ceiling and the default four master/provider slots. Empty groups are preflighted without paid calls. The dominant observed workload is model generation, not the progress UI. The ceiling does not mean every call generates 80,000 tokens.

## Performance configuration

- MASTER_ANALYSIS_CONCURRENCY=8: at most eight master group workers.
- MASTER_AI_MAX_CONCURRENCY=8: at most eight provider requests across all escalation callers. Existing rate pacing and Retry-After/backoff remain enabled. Reduce these together if the account encounters rate limits.
- MASTER_GROUP_REASONING=low: master group workers use lower thinking effort only for the official DeepSeek v4-pro endpoint. Blank inherits global reasoning. Final master/digest and standalone group reports retain global settings. Hybrid retains its existing route-specific controls.

Lower reasoning is a quality/latency tradeoff, not a validated quality-equivalent setting. All original messages, prompts and the full output budget are retained. The cache version includes effective reasoning, so a low-effort report is not silently reused for a high-effort request.

These settings are read when the backend runtime starts. Do not restart an active job solely to apply them: it can interrupt that job. Apply during the next controlled restart. No live paid run was launched for this change. Mock-provider regression tests validate scoping, cache compatibility, bounds, reuse and cancellation; they cannot establish real latency or report quality. Compare wall time, failure/rate-limit counts and reviewed reports on the same fixed window before making speed claims.

Provider reference: https://api-docs.deepseek.com/guides/thinking_mode/
