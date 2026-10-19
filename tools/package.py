#!/usr/bin/env python3
"""Stage Mike's packages from this checkout (no release, no upload).

  tools/package.py <version> <out-dir>              Desktop bundle (interim)
  tools/package.py --service-only <version> <out>   the shared service alone

The service package (docs/service-package.md) is independently installable:
its launcher, lifecycle control, local-protocol client, Euglena application
and exact modules from service/.code/lock.json — and nothing of the Desktop
UI. The Desktop bundle carries the same service tree beside the window until
generic package dependencies (ticket 163) let Desktop declare it instead.
"""

import hashlib
import json
from pathlib import Path
import shutil
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def stage_service(stage: Path, version: str) -> None:
    """Everything the shared service needs, at the root of `stage`."""
    service = ROOT / "service"
    if not (service / "main.code").is_file():
        raise SystemExit("run euglena test in service/ first to generate service/main.code")
    staged = stage / "service"
    staged.mkdir(parents=True)
    for entry in ("main.code", "manifest.json"):
        shutil.copy2(service / entry, staged / entry)
    shutil.copytree(service / "src", staged / "src")
    shutil.copytree(service / "adapters", staged / "adapters", ignore=IGNORE)
    locked = json.loads((service / ".code/lock.json").read_text())["modules"]
    for module, pin in locked.items():
        path = service / ".code/modules" / module / pin["version"] / f"{module}-linux-x86_64.so"
        if hashlib.sha256(path.read_bytes()).hexdigest() != pin["sha256"]:
            raise SystemExit(f"{module}: installed module differs from lockfile")
        shutil.copy2(path, staged / f"{module}.so")
    client = stage / "client"
    client.mkdir()
    for name in ("mike_service.py", "service_control.py"):
        shutil.copy2(ROOT / "client" / name, client / name)
    docs = stage / "docs"
    docs.mkdir(exist_ok=True)
    for name in ("local-service-protocol.md", "service-package.md"):
        shutil.copy2(ROOT / "docs" / name, docs / name)
    shutil.copy2(ROOT / "bin/mike-service", stage / "mike-service")
    (stage / "VERSION").write_text(version + "\n")


def archive(out: Path, stage: Path) -> Path:
    path = out / (stage.name + ".tar.gz")
    with tarfile.open(path, "w:gz") as tar:
        tar.add(stage, arcname=stage.name)
    return path


def fresh(out: Path, name: str) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    stage = out / name
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir()
    return stage


def main():
    args = sys.argv[1:]
    service_only = args[:1] == ["--service-only"]
    if service_only:
        args = args[1:]
    if len(args) != 2:
        raise SystemExit(__doc__)
    version, destination = args
    out = Path(destination).resolve()
    if service_only:
        stage = fresh(out, f"mike-service-{version}-x86_64-linux")
        stage_service(stage, version)
        (stage / "README.md").write_text(
            "# Mike service\n\nThe shared local Mike service, without the Desktop window.\n"
            "See docs/service-package.md (install and launch contract) and\n"
            "docs/local-service-protocol.md (clients, lifecycle, systemd).\n"
        )
        print(archive(out, stage))
        return
    stage = fresh(out, f"mike-desktop-{version}-x86_64-linux")
    stage_service(stage, version)
    shutil.copy2(ROOT / "README.md", stage / "README.md")
    shutil.copytree(ROOT / "ui", stage / "ui", ignore=IGNORE)
    shutil.copy2(ROOT / "bin/mike-desktop", stage / "mike-desktop")
    shutil.copy2(ROOT / "bin/mike-desktop", stage / "mike")
    (stage / "app.info").write_text(
        "name=Mike\n"
        "comment=Your assistant and desktop interface to Euglena\n"
        "terminal=false\n"
        "icon=computer\n"
        "categories=Utility;\n"
        "keywords=Mike;assistant;desktop;Euglena;\n"
    )
    print(archive(out, stage))


if __name__ == "__main__":
    main()
