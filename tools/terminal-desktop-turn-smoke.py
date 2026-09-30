#!/usr/bin/python3
"""Start a Mike terminal turn inside the real desktop WebView."""

import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys

e2e_spec = importlib.util.spec_from_file_location("terminal_e2e", Path(__file__).with_name("terminal-e2e.py"))
e2e = importlib.util.module_from_spec(e2e_spec)
e2e_spec.loader.exec_module(e2e)

ROOT = Path(os.environ.get("E2E_DESKTOP_DIR", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT / "ui"))
from desktop_webview import Gdk, GLib, Gtk, MikeWindow  # noqa: E402

HOST = os.environ.get("EUGLENA_HOST_URL", "http://127.0.0.1:8923").rstrip("/")
CODE = os.environ.get("CODE_BIN", "/home/cdlvsm/.local/bin/cdlvsm-code")
EMAIL = e2e.EMAIL
PASSWORD = e2e.PASSWORD


def main():
    if not EMAIL or not PASSWORD:
        raise SystemExit("Set E2E_EMAIL and E2E_PASSWORD for a disposable account")
    signed = e2e.api("auth", {"_class": "Authenticate", "email": EMAIL, "password": PASSWORD})
    assert signed["_class"] == "SignedIn", signed
    token = signed["token"]
    before = e2e.api("mike-interface", {"_class": "MyDevices"}, token)
    known = {one["device_id"] for one in before["items"]}
    window = MikeWindow(ROOT, CODE, HOST)
    marker = "desktop-turn-" + secrets.token_hex(5)
    state = {"handoff": False, "submitting": False, "submitted": False, "approved": False,
             "display_pending": False, "display_ready": False, "step": "", "error": "", "done": False}

    def submit(view, outcome):
        try:
            answer = view.run_javascript_finish(outcome).get_js_value().to_string()
            if answer == "sent":
                state["submitted"] = True
            elif answer == "waiting":
                state["submitting"] = False
            else:
                raise AssertionError("Mike input was unavailable: " + answer)
        except Exception as error:
            state["error"] = str(error)

    def visible_reply(view, outcome):
        state["display_pending"] = False
        try:
            state["display_ready"] = view.run_javascript_finish(outcome).get_js_value().to_boolean()
        except Exception as error:
            state["error"] = str(error)

    def check():
        try:
            if state["error"]:
                window.close()
                return False
            if window.port is None:
                return True
            if not state["handoff"]:
                state["handoff"] = True
                window.accept_handoff(token)
                return True
            if window.current_token != token or window.inserting_token:
                return True
            if not state["submitted"] and not state["submitting"]:
                state["submitting"] = True
                prompt = "On my connected desktop, run pwd in working directory /tmp and tell me the result. " + marker
                window.web.run_javascript(
                    "(() => { const input = document.querySelector('input[aria-label=\"Ask Mike\"]'); "
                    "if (!input) return 'waiting'; input.value = " + json.dumps(prompt) + "; "
                    "input.dispatchEvent(new Event('input', {bubbles: true})); "
                    "const send = document.querySelector('.mike button.primary'); "
                    "if (!send) return 'waiting'; send.click(); return 'sent'; })()",
                    None, submit,
                )
                return True
            local = json.loads(window.bridge("/state")[1])
            if local.get("task_id"):
                state["step"] = local["task_id"]
            if state["step"]:
                progress = e2e.api("mike-interface", {"_class": "InvocationProgress", "id": state["step"]}, token)
                if progress.get("state") == "completed":
                    assert progress["result"]["state"] == "completed", progress
                    assert progress["result"]["stdout"].strip() == "/tmp", progress
                    recent = e2e.api("mike", {"_class": "RecentConversation", "limit": 5}, token)
                    for turn in recent.get("turns", []):
                        if marker in turn.get("text", "") and "/tmp" in turn.get("reply", ""):
                            if "Result: completed" not in window.activity_log:
                                return True
                            if not state["display_ready"]:
                                if not state["display_pending"]:
                                    state["display_pending"] = True
                                    window.web.run_javascript(
                                        "Array.from(document.querySelectorAll('.line.mike')).some(line => line.textContent.includes('/tmp'))",
                                        None, visible_reply,
                                    )
                                return True
                            assert state["approved"] and "stdout:\n/tmp" in window.activity_log
                            shot = os.environ.get("MIKE_SCREENSHOT_PATH")
                            if shot:
                                surface = window.window.get_window()
                                picture = Gdk.pixbuf_get_from_window(surface, 0, 0, surface.get_width(), surface.get_height())
                                picture.savev(shot, "png", [], [])
                            state["done"] = True
                            print("PASS: user typed in Mike desktop, approved pwd, and saw its result")
                            window.close()
                            return False
        except Exception as error:
            state["error"] = str(error)
            window.close()
            return False
        return True

    def approve():
        for top in Gtk.Window.list_toplevels():
            if isinstance(top, Gtk.MessageDialog) and top.get_visible():
                state["approved"] = True
                top.response(Gtk.ResponseType.YES)
        return not state["done"] and not state["error"]

    def timeout():
        state["error"] = "desktop turn timed out: " + str(state)
        window.close()
        return False

    GLib.timeout_add_seconds(1, check)
    GLib.timeout_add(100, approve)
    GLib.timeout_add_seconds(120, timeout)
    Gtk.main()
    after = e2e.api("mike-interface", {"_class": "MyDevices"}, token)
    for one in after.get("items", []):
        if one["device_id"] not in known:
            e2e.api("mike-interface", {"_class": "RevokeDevice", "device_id": one["device_id"]}, token)
    if state["error"]:
        raise SystemExit(state["error"])
    assert state["done"], state


if __name__ == "__main__":
    main()
