#!/usr/bin/env python3
"""Assemble a CDLVSM bundle: the Desktop client and the shared Mike service.

The service (service/, a native Euglena application) is staged with the
exact modules in service/.code/lock.json. The Desktop window and the service
control tool are Python clients of it. Until generic package dependencies
(ticket 163 / M19) exist, both ship in one bundle; the service directory is
self-contained so it can become its own package.
"""

import hashlib
import json
from pathlib import Path
import shutil
import sys
import tarfile


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: tools/package.py <version> <out-dir>")
    version, destination = sys.argv[1:]
    root = Path(__file__).resolve().parents[1]
    service = root / "service"
    if not (service / "main.code").is_file():
        raise SystemExit("run euglena test in service/ first to generate service/main.code")
    out = Path(destination).resolve()
    out.mkdir(parents=True, exist_ok=True)
    name = f"mike-desktop-{version}-x86_64-linux"
    stage = out / name
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir()
    shutil.copy2(root / "README.md", stage / "README.md")
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(root / "ui", stage / "ui", ignore=ignore)
    shutil.copytree(root / "client", stage / "client", ignore=ignore)
    shutil.copytree(root / "docs", stage / "docs", ignore=shutil.ignore_patterns("tickets", "plans"))
    staged_service = stage / "service"
    staged_service.mkdir()
    for entry in ("main.code", "manifest.json"):
        shutil.copy2(service / entry, staged_service / entry)
    shutil.copytree(service / "src", staged_service / "src")
    shutil.copytree(service / "adapters", staged_service / "adapters", ignore=ignore)
    shutil.copy2(root / "bin/mike-desktop", stage / "mike-desktop")
    shutil.copy2(root / "bin/mike-desktop", stage / "mike")
    shutil.copy2(root / "bin/mike-service", stage / "mike-service")
    (stage / "VERSION").write_text(version + "\n")
    (stage / "app.info").write_text(
        "name=Mike\n"
        "comment=Your assistant and desktop interface to Euglena\n"
        "terminal=false\n"
        "icon=computer\n"
        "categories=Utility;\n"
        "keywords=Mike;assistant;desktop;Euglena;\n"
    )
    locked = json.loads((service / ".code/lock.json").read_text())["modules"]
    for module, pin in locked.items():
        filename = f"{module}-linux-x86_64.so"
        path = service / ".code/modules" / module / pin["version"] / filename
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != pin["sha256"]:
            raise SystemExit(f"{module}: installed module differs from lockfile")
        shutil.copy2(path, staged_service / f"{module}.so")
    archive = out / (name + ".tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(stage, arcname=name)
    print(archive)


if __name__ == "__main__":
    main()
