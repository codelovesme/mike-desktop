#!/usr/bin/env python3
"""Assemble a CDLVSM release with the exact modules in .code/lock.json."""

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
    source = root / "main.code"
    if not source.is_file():
        raise SystemExit("run euglena test first to generate main.code")
    out = Path(destination).resolve()
    out.mkdir(parents=True, exist_ok=True)
    name = f"mike-desktop-{version}-x86_64-linux"
    stage = out / name
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir()
    for entry in ("main.code", "README.md"):
        shutil.copy2(root / entry, stage / entry)
    shutil.copytree(root / "src", stage / "src")
    shutil.copy2(root / "bin/mike-desktop", stage / "mike-desktop")
    shutil.copy2(root / "bin/mike-desktop", stage / "mike")
    (stage / "VERSION").write_text(version + "\n")
    (stage / "app.info").write_text(
        "name=Mike\n"
        "comment=Your assistant and desktop interface to Euglena\n"
        "terminal=false\n"
        "icon=computer\n"
        "categories=Utility;\n"
        "keywords=Mike;assistant;desktop;Euglena;\n"
    )
    locked = json.loads((root / ".code/lock.json").read_text())["modules"]
    for module, pin in locked.items():
        filename = f"{module}-linux-x86_64.so"
        path = root / ".code/modules" / module / pin["version"] / filename
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != pin["sha256"]:
            raise SystemExit(f"{module}: installed module differs from lockfile")
        shutil.copy2(path, stage / f"{module}.so")
    archive = out / (name + ".tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(stage, arcname=name)
    print(archive)


if __name__ == "__main__":
    main()
