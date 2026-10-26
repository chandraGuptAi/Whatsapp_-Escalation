"""Offline extraction/regression harness; no credentials, network or live database access.

Optional dataset: JSON list of {group_id, messages, expected_awbs?, legacy_report?}.
Only an independently reviewed dataset can establish precision/recall quality gates.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.escalation_analyst.analysis import hybrid
from app.escalation_analyst.analysis.observe import percentile
from app.escalation_analyst.analysis.metrics import parse_metrics


def benchmark(dataset):
    durations, routes, differences = [], {}, []
    tp = fp = fn = 0
    labelled = 0
    started = time.perf_counter()
    for group in dataset:
        before = time.perf_counter()
        result = hybrid.validate(hybrid.extract(group["messages"]))
        durations.append((time.perf_counter() - before) * 1000)
        routes[result.route] = routes.get(result.route, 0) + 1
        proposed = hybrid.metrics(result)
        if "expected_awbs" in group:
            labelled += 1
            actual = {c.awb for c in result.cases if c.awb}
            expected = set(group["expected_awbs"])
            tp += len(actual & expected)
            fp += len(actual - expected)
            fn += len(expected - actual)
        old = parse_metrics(group.get("legacy_report", ""))
        if old is not None and old != proposed:
            differences.append(dict(group_id=group["group_id"], legacy=old, proposed=proposed,
                                    review_required=True, proposed_route=result.route))
    return dict(kind="offline_extraction_only", groups=len(dataset), elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
        latency_ms=dict(p50=percentile(durations, .5), p95=percentile(durations, .95), p99=percentile(durations, .99)),
        routes=routes, actual_ai_calls=0, labelled_groups=labelled,
        awb_extraction_precision=tp / (tp + fp) if tp + fp else None,
        awb_extraction_recall=tp / (tp + fn) if tp + fn else None,
        escalation_detection_precision=None, escalation_detection_recall=None,
        differences=differences, quality_note="AWB extraction is not escalation detection accuracy. Ambiguous cases require AI/review; no paid calls run.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    data = json.loads(args.dataset.read_text(encoding="utf-8")) if args.dataset else [
        dict(group_id=f"synthetic-{i}", messages=[dict(id=i + 1, wa_id=f"m-{i}", timestamp=1792972800,
             author="synthetic", body=f"AWB {12345678+i} delayed")], expected_awbs=[str(12345678+i)]) for i in range(107)]
    output = benchmark(data)
    output["dataset"] = str(args.dataset) if args.dataset else "107 synthetic factual groups; NOT human reviewed"
    output["legacy_prompt_bytes"] = len((Path(hybrid.__file__).parents[1] / "prompts/escalation-analyst.md").read_bytes())
    output["hybrid_prompt_bytes"] = len((Path(hybrid.__file__).parents[1] / "prompts/hybrid-analyst.md").read_bytes())
    text = json.dumps(output, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
