"""Saved report PDF export: valid PDF, Markdown structures render, unknown report is 404."""

from unittest.mock import Mock

from app.escalation_analyst.exports.report_pdf import report_pdf

from .test_core import client, ingest  # noqa: F401  (fixture re-export)

MARKDOWN = """# Master summary
Three groups raised **14 pending escalations** with *mostly* delayed pickups and `RTO` disputes.

## Key issues
| AWB | Group | Issue | Status |
| --- | --- | --- | --- |
| 12345678 | Agra Retailers | Delayed pickup for a very long time with many follow ups from the seller | Open |
| 87654321 | Delhi NCR Sellers | RTO dispute ₹1,200 | Escalated |

1. Chase pickup team
2. Refund seller

- Bullet with <angle> & ampersand
---
```json
{"raised": 14, "closed": 3}
```
"""


def test_report_pdf_renders_markdown():
    pdf = report_pdf(
        dict(scope="master", group_name="all", created_at=1761475200, window_label="Last 24 hours",
             message_count=1234, report=MARKDOWN * 6),
        "All clusters",
    )
    assert pdf.startswith(b"%PDF") and len(pdf) > 3000
    assert pdf.count(b"/Type /Page") >= 2  # long reports flow onto more pages


def test_group_tokens_stay_in_one_table_cell():
    from reportlab.platypus import LongTable
    from app.escalation_analyst.exports.report_pdf import _styles, markdown_story

    text = "| Group | Raised |\n| --- | --- |\n| {{G:1@g.us|BIGSHIP || YOUTH ROBE}} | 38 |"
    table = next(f for f in markdown_story(text, 500, _styles("Helvetica", "Helvetica-Bold")) if isinstance(f, LongTable))
    assert len(table._cellvalues[1]) == 2
    assert "BIGSHIP || YOUTH ROBE" in table._cellvalues[1][0].text and "1@g.us" not in table._cellvalues[1][0].text


def test_download_pdf_endpoint(client):
    ingest(client)
    r = client.app.state.runtime
    r.provider.generate = Mock(return_value=MARKDOWN)
    report_id = r.analysis.group("test@g.us", {"hours": 24})["reportId"]
    response = client.get(f"/reports/{report_id}/download?format=pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline")
    assert response.content.startswith(b"%PDF")
    assert client.get("/reports/999999/download?format=pdf").status_code == 404
