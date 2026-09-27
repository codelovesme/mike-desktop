#!/usr/bin/env python3
"""Run Mike -> gateway -> native desktop -> approved file open against a live host.

Provide E2E_EMAIL and E2E_PASSWORD for a disposable signed-in account.
The opener is a private test executable; every other boundary is real.
"""

import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


DESKTOP = Path(os.environ.get("E2E_DESKTOP_DIR", str(Path(__file__).resolve().parents[1])))
CODE = Path(os.environ.get("CODE_BIN", str(DESKTOP.parents[1] / "code/target/debug/code")))
HOST = os.environ.get("EUGLENA_HOST_URL", "http://127.0.0.1:8899").rstrip("/")
EMAIL = os.environ.get("E2E_EMAIL", "")
PASSWORD = os.environ.get("E2E_PASSWORD", "")


def code_string(value):
    return json.dumps(value).replace("$", r"\$")


def request(app, particle, token=None):
    envelope = {"_class": "Open", "particle": particle}
    if token:
        envelope = {"_class": "Impulse", "token": token, "particle": particle}
    body = json.dumps(envelope).encode()
    req = Request(HOST + "/" + app, body, {"Content-Type": "application/json"})
    with urlopen(req, timeout=25) as response:
        return json.load(response)


def module(name):
    bundled = DESKTOP / (name + ".so")
    if bundled.is_file():
        return str(bundled)
    return str(DESKTOP / ".code/modules" / name / "2.11.0" / (name + "-linux-x86_64.so"))


def fixture(email, password, root, filename, exact_path, token):
    lines = []
    for alias in ("auth", "mike", "interface"):
        lines.append('link ' + code_string(module("net_client")) + " as " + alias)
    for name, alias in (("process", "proc"), ("strings", "strs"),
                        ("timer", "clock"), ("window", "win"), ("env", "system_env")):
        lines.append('link ' + code_string(module(name)) + " as " + alias)
    lines.extend([
        'link ' + code_string(str(DESKTOP / "src/files.gene.code")),
        'link ' + code_string(str(DESKTOP / "src/desktop.gene.code")),
    ])
    for alias, app in (("auth", "auth"), ("mike", "mike"),
                       ("interface", "mike-interface")):
        lines.append("emit Config { url = " + code_string(HOST + "/" + app) + " } to " + alias)
    lines.extend([
        "emit StartDesktop { headless = true } to this get desktop",
        "assert desktop ∈ DesktopStarted",
    ])

    def enter(value):
        lines.extend([
            'emit Key { id = desktop.id, name = "text", text = ' + code_string(value) + " } to this",
            'emit Key { id = desktop.id, name = "enter", text = "" } to this',
        ])

    enter(email)
    enter(password)
    enter(root)
    lines.append("emit RefreshDesktop to this")
    enter("Please use this connected desktop to find and open the local file named " + filename + ".")

    def wait_for(row, needle, limit):
        lines.extend([
            "attempts = 0",
            "loop",
            "    attempts += 1",
            "    emit RefreshDesktop to this",
            "    emit Text { id = desktop.id } to win get shown",
            "    emit Contains { text = shown.rows[" + str(row) + "], needle = " + code_string(needle) + " } to this get visible",
            "    if visible.yes",
            "        break",
            "    assert attempts < " + str(limit),
            '    emit Run { command = "sleep", args = ["1"] } to proc',
        ])

    wait_for(10, "Allow Mike's task?", 90)
    lines.append('emit Key { id = desktop.id, name = "y", text = "y" } to this')
    wait_for(10, "Filename words to search", 40)
    enter(filename)
    lines.extend([
        "emit Text { id = desktop.id } to win get matched",
        "emit Contains { text = matched.rows[5], needle = " + code_string(exact_path) + " } to this get selected",
        "assert selected.yes",
        'emit Key { id = desktop.id, name = "y", text = "y" } to this',
    ])
    wait_for(8, "Desktop task: opened", 15)
    wait_for(7, "opened", 90)
    lines.extend([
        "emit Send { particle = Impulse { token = " + code_string(token)
        + ", particle = RecentConversation { limit = 1 } } } to mike get recent",
        "assert recent ∈ Recent",
        "assert recent.ok",
        "assert recent.turns ≠ []",
        "assert recent.turns[0].reply ∈ String",
        "emit Contains { text = recent.turns[0].text, needle = " + code_string(filename) + " } to this get stored_request",
        "assert stored_request.yes",
        'emit Contains { text = recent.turns[0].reply, needle = "opened" } to this get stored_result',
        "assert stored_result.yes",
        'emit CloseRequested { id = desktop.id } to this',
    ])
    return "\n".join(lines) + "\n"


def main():
    if not EMAIL or not PASSWORD:
        raise SystemExit("Set E2E_EMAIL and E2E_PASSWORD for a disposable account.")
    target = urlsplit(HOST)
    if target.scheme != "http" or target.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("Use the local host or an encrypted tunnel bound to loopback.")
    signed = request("auth", {"_class": "Authenticate", "email": EMAIL, "password": PASSWORD})
    if signed.get("_class") != "SignedIn":
        raise SystemExit("The disposable account could not sign in.")
    token = signed["token"]
    before = request("mike-interface", {"_class": "MyDevices"}, token)
    known = {one["device_id"] for one in before.get("items", [])}

    try:
        with tempfile.TemporaryDirectory(prefix="mike-desktop-e2e-") as temporary:
            area = Path(temporary)
            root = area / "chosen"
            root.mkdir()
            filename = "mike-test-invoice-" + secrets.token_hex(5) + ".txt"
            exact = root / filename
            exact.write_text("Mike desktop fixture\n")
            bin_dir = area / "bin"
            bin_dir.mkdir()
            opener = bin_dir / "xdg-open"
            opener.write_text('#!/bin/sh\nprintf "%s\\n" "$1" > "$MIKE_OPEN_LOG"\n')
            opener.chmod(0o700)
            opened_log = area / "opened.txt"
            source = area / "desktop.code"
            source.write_text(fixture(EMAIL, PASSWORD, str(root), filename, str(exact), token))
            source.chmod(0o600)
            env = os.environ.copy()
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
            env["MIKE_OPEN_LOG"] = str(opened_log)
            result = subprocess.run([str(CODE), "run", str(source)], env=env,
                                    capture_output=True, text=True, timeout=240)
            if result.returncode:
                detail = (result.stderr + result.stdout).replace(PASSWORD, "<redacted>")
                detail = detail.replace(token, "<redacted>").replace(EMAIL, "<redacted>")
                raise RuntimeError("Desktop fixture failed: " + detail[:1200])
            if not opened_log.exists() or opened_log.read_text().strip() != str(exact):
                raise AssertionError("The approved exact file was not opened.")
            print("PASS: Mike requested, the user confirmed twice, and the desktop opened", filename)
    finally:
        after = request("mike-interface", {"_class": "MyDevices"}, token)
        for one in after.get("items", []):
            if one["device_id"] not in known:
                request("mike-interface", {"_class": "RevokeDevice",
                                            "device_id": one["device_id"]}, token)


if __name__ == "__main__":
    main()
