#!/usr/bin/python3
"""Check the real WebKit window accepts a browser handoff and pairs Euglena.

Run under Xvfb with a disposable verified account. Set MIKE_SCREENSHOT_PATH
to save the resulting signed-in window.
"""

import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(os.environ.get("E2E_DESKTOP_DIR", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT / "ui"))
from desktop_webview import GLib, Gtk, MikeWindow  # noqa: E402

HOST = os.environ.get("MIKE_DESKTOP_HOST_URL", "http://127.0.0.1:8923").rstrip("/")
EMAIL = os.environ.get("E2E_EMAIL", "")
PASSWORD = os.environ.get("E2E_PASSWORD", "")
CODE = os.environ.get("CODE_BIN", "/home/cdlvsm/git/codelovesme/code/target/release/code")


def main():
    if not EMAIL or not PASSWORD:
        raise SystemExit("Set E2E_EMAIL and E2E_PASSWORD for a disposable verified account.")
    envelope = {"_class": "Open", "particle": {"_class": "Authenticate", "email": EMAIL, "password": PASSWORD}}
    request = Request(HOST + "/api/auth", json.dumps(envelope).encode(),
                      {"Content-Type": "application/json", "User-Agent": "Mike-E2E/0.3"})
    with urlopen(request, timeout=20) as response:
        signed = json.load(response)
    assert signed["_class"] == "SignedIn", signed
    token = signed["token"]
    window = MikeWindow(ROOT, CODE, HOST)
    state = secrets.token_urlsafe(32)
    window.connect_state = state
    result = {"status": 0, "error": ""}

    def handoff():
        try:
            body = urlencode({"state": state, "token": token}).encode()
            request = Request(
                "http://127.0.0.1:" + str(window.callback.server_port) + "/callback", body,
                {"Content-Type": "application/x-www-form-urlencoded", "Origin": HOST},
            )
            with urlopen(request, timeout=10) as response:
                result["status"] = response.status
            try:
                urlopen(request, timeout=10)
                result["error"] = "browser callback accepted a replay"
            except HTTPError as error:
                if error.code != 403:
                    result["error"] = "browser callback replay returned " + str(error.code)
        except Exception as error:
            result["error"] = str(error)

    threading.Thread(target=handoff, daemon=True).start()

    def verify():
        try:
            assert result["status"] == 200, result
            assert not result["error"], result
            assert window.stack.get_visible_child_name() == "conversation", "conversation did not open"
            status, body = window.bridge("/state")
            state = json.loads(body)
            assert status == 200 and state["connection"] == "connected", state
            assert state["owner"] == EMAIL, "paired under the wrong account"
            shot = os.environ.get("MIKE_SCREENSHOT_PATH")
            if shot:
                subprocess.run(["xwd", "-name", "Mike", "-silent", "-out", shot], check=True)
            print("PASS: browser callback connected the WebKit window and native Euglena companion")
        except Exception as error:
            print("FAIL:", str(error), file=sys.stderr)
            result["error"] = str(error)
        finally:
            window.close()
        return False

    GLib.timeout_add_seconds(9, verify)
    Gtk.main()
    if result["error"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
