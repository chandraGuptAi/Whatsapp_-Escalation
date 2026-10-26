from pathlib import Path
import pytest
from integration.install_gateway import apply


def test_install_changes_only_import_and_preserves_current_edits(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    main = app / "main.py"
    before = '# existing local edit\nfrom app.escalation_analyst.application import install as install_escalation_analyst\ninstall_escalation_analyst(app)\n'
    main.write_text(before, encoding="utf-8")
    (tmp_path / ".env").write_text("LOCAL_SETTINGS=preserve", encoding="utf-8")
    apply(tmp_path)
    assert main.read_text(encoding="utf-8") == before.replace("app.escalation_analyst.application", "app.api_gateway")
    backups = list((tmp_path / "scratch").glob("gateway-backup-*/main.py"))
    assert backups[0].read_text(encoding="utf-8") == before
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "LOCAL_SETTINGS=preserve"
    assert (app / "api_gateway.py").read_bytes() == Path("integration/escalation_gateway.py").read_bytes()


def test_install_unknown_main_does_not_write(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "main.py").write_text("# unknown integration", encoding="utf-8")
    with pytest.raises(RuntimeError):
        apply(tmp_path)
    assert not (app / "api_gateway.py").exists()
    assert not (tmp_path / "scratch").exists()
