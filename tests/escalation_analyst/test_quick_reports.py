"""Quick report contract: validation, owner scoping, file extraction, run lifecycle and exports."""

import base64
import io
import time
import zipfile
from unittest.mock import Mock

from openpyxl import Workbook

from .test_core import client, ingest  # noqa: F401  (fixture re-export)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def wait(client, run_id, headers=None):
    for _ in range(100):
        run = client.get(f"/quick-reports/{run_id}", headers=headers).json()["run"]
        if run["status"] != "running":
            return run
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def xlsx() -> bytes:
    book = Workbook()
    book.active.append(["AWB", "Issue"])
    book.active.append(["12345678", "Delayed pickup"])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def docx() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Seller escalation notes</w:t></w:r></w:p></w:body></w:document>",
        )
    return out.getvalue()


REPORT = "Summary\n\n| AWB | Issue |\n| --- | --- |\n| 12345678 | Delayed |"


def test_validation(client):
    assert client.post("/quick-reports", json={"prompt": "Summarise"}).status_code == 400
    assert client.post("/quick-reports", json={"prompt": "Summarise", "groupIds": ["missing@g.us"]}).status_code == 404
    bad = {"prompt": "Summarise", "files": [{"name": "scan.pdf", "data": b64(b"%PDF")}]}
    response = client.post("/quick-reports", json=bad)
    assert response.status_code == 400 and "unsupported" in response.json()["error"]


def test_group_run_lifecycle_owner_scope_and_exports(client):
    ingest(client)
    r = client.app.state.runtime
    r.provider.generate = Mock(return_value=REPORT)
    started = client.post(
        "/quick-reports",
        json={"prompt": "Find key issues", "groupIds": ["test@g.us"], "hours": 24, "voice": True, "outputFormat": "web_excel"},
    )
    assert started.status_code == 202
    run = wait(client, started.json()["id"])
    assert run["status"] == "complete" and run["report"] == REPORT
    assert run["inputs"] == ["prompt", "groups", "voice"] and run["groupNames"] == ["Test group"]
    system, user_text, _ = r.provider.generate.call_args.args
    assert "Find key issues" in user_text and "AWB 12345678 delayed" in user_text

    listed = client.get("/quick-reports").json()
    assert listed["total"] == 1 and listed["runs"][0]["id"] == run["id"]
    assert client.get("/quick-reports", headers={"X-User-Id": "other"}).json()["total"] == 0
    assert client.get(f"/quick-reports/{run['id']}", headers={"X-User-Id": "other"}).status_code == 404

    assert client.get(f"/quick-reports/{run['id']}/download?format=doc").status_code == 200
    excel = client.get(f"/quick-reports/{run['id']}/download?format=xlsx")
    assert excel.status_code == 200 and excel.content[:2] == b"PK"

    again = client.post(f"/quick-reports/{run['id']}/retry")
    assert again.status_code == 202 and again.json()["id"] != run["id"]
    assert wait(client, again.json()["id"])["status"] == "complete"


def test_files_only_run_extracts_excel_and_word(client):
    r = client.app.state.runtime
    r.provider.generate = Mock(return_value="File summary")
    started = client.post(
        "/quick-reports",
        json={
            "prompt": "Summarise the attachments",
            "files": [{"name": "awbs.xlsx", "data": b64(xlsx())}, {"name": "notes.docx", "data": b64(docx())}],
        },
    )
    run = wait(client, started.json()["id"])
    assert run["status"] == "complete" and run["fileNames"] == ["awbs.xlsx", "notes.docx"]
    user_text = r.provider.generate.call_args.args[1]
    assert "12345678 | Delayed pickup" in user_text and "Seller escalation notes" in user_text


def test_groups_without_messages_finish_empty(client):
    ingest(client)
    r = client.app.state.runtime
    r.provider.generate = Mock(return_value=REPORT)
    started = client.post(
        "/quick-reports",
        json={"prompt": "Summarise", "groupIds": ["test@g.us"], "fromDate": "2020-01-01", "toDate": "2020-01-02"},
    )
    assert wait(client, started.json()["id"])["status"] == "empty"
    r.provider.generate.assert_not_called()
