"""Never trust the model's facts. Every finding must cite source messages, the cited
messages must exist, and any AWB number it mentions must appear in those messages."""
from __future__ import annotations

from app.shared.awb import find_awbs


def validate_findings(result: dict, source: dict[str, str]) -> list[dict]:
    """result = {"findings": [{"text": ..., "message_ids": [...]}, ...]}; source = message id -> text.
    Returns a list of issues; empty means every claim checked out."""
    issues: list[dict] = []
    findings = result.get("findings")
    if not isinstance(findings, list):
        return [{"finding": None, "issue": "missing findings list"}]
    for index, finding in enumerate(findings):
        ids = [str(i) for i in (finding.get("message_ids") or [])]
        if not ids:
            issues.append({"finding": index, "issue": "no cited messages"})
            continue
        unknown = [i for i in ids if i not in source]
        if unknown:
            issues.append({"finding": index, "issue": "cites unknown messages", "ids": unknown[:5]})
        cited = " ".join(source.get(i, "") for i in ids)
        for awb in find_awbs(str(finding.get("text", ""))):
            if awb not in cited:
                issues.append({"finding": index, "issue": "AWB not in cited messages", "awb": awb})
    return issues
