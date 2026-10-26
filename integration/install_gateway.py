"""Apply only the gateway import change, with backups; never modify .env or data."""
import argparse
import ast
import shutil
from datetime import datetime, timezone
from pathlib import Path


def apply(backend):
    backend = Path(backend).resolve()
    main = backend / "app/main.py"
    destination = backend / "app/api_gateway.py"
    source = Path(__file__).with_name("escalation_gateway.py")
    old = "from app.escalation_analyst.application import install as install_escalation_analyst"
    new = "from app.api_gateway import install as install_escalation_analyst"
    before = main.read_text(encoding="utf-8")
    if old not in before and new not in before and "from app.api_gateway import install as install_api_gateway" not in before:
        raise RuntimeError("Expected escalation installation not found; no files changed")
    if old in before and before.count(old) != 1:
        raise RuntimeError("Ambiguous integration; no files changed")
    after = before.replace(old, new)
    ast.parse(after)
    ast.parse(source.read_text(encoding="utf-8"))
    if destination.exists() and destination.read_bytes() != source.read_bytes():
        raise RuntimeError("Different gateway already exists; no files changed")
    backup = backend / "scratch" / ("gateway-backup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    backup.mkdir(parents=True)
    shutil.copy2(main, backup / "main.py")
    shutil.copy2(source, destination)
    main.write_text(after, encoding="utf-8", newline="")
    print(f"Gateway installed. Main backup: {backup / 'main.py'}")
    print("Existing .env, escalation package, session and databases were preserved.")
    print("Remote mode activates only when ESCALATION_SERVICE_URL and ESCALATION_SERVICE_TOKEN are configured.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", type=Path, required=True)
    apply(parser.parse_args().backend)
