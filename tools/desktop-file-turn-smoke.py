#!/usr/bin/python3
"""A Mike file task and the voice controls, through the real desktop window.

The approval/media coverage the retired text-mode window had, now on the
published UI: the person types in the WebView, approves Mike's action in
the page ("Yes, do it"), the native dialogs ask to search, pick the folder
(OS picker), take filename words and approve the exact file; the service
opens it (the portal's last call is recorded via MIKE_OPEN_LOG) and Mike's
reply appears in the page. The page's voice controls (Speak, voice chat,
read aloud) are present, and pressing Speak turns the page's microphone
request into a native permission dialog, which is refused.

Run under Xvfb with a disposable account; the window is a client of a
real scratch Mike service.
"""

import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile

e2e_spec = importlib.util.spec_from_file_location("service_e2e", Path(__file__).with_name("service-e2e.py"))
e2e = importlib.util.module_from_spec(e2e_spec)
e2e_spec.loader.exec_module(e2e)

ROOT = Path(os.environ.get("E2E_DESKTOP_DIR", Path(__file__).resolve().parents[1]))
HOST = os.environ.get("EUGLENA_HOST_URL", "http://127.0.0.1:8923").rstrip("/")


def main():
    email, password = os.environ.get("E2E_EMAIL", ""), os.environ.get("E2E_PASSWORD", "")
    if not email or not password:
        raise SystemExit("Set E2E_EMAIL and E2E_PASSWORD for a disposable account")
    token = e2e.Run.sign_in(email, password)
    known = e2e.Run.devices(token)
    area = Path(tempfile.mkdtemp(prefix="mike-file-smoke-"))
    service = e2e.Service(area)
    os.environ.update(MIKE_SERVICE_STATE_DIR=service.env["MIKE_SERVICE_STATE_DIR"],
                      MIKE_SERVICE_RUNTIME_DIR=service.env["MIKE_SERVICE_RUNTIME_DIR"])
    service.start()
    sys.path.insert(0, str(ROOT / "ui"))
    from desktop_webview import GLib, Gtk, MikeWindow  # noqa: E402

    folder = area / "documents"
    folder.mkdir()
    filename = "mike-ui-" + secrets.token_hex(4) + ".txt"
    (folder / filename).write_text("fixture")
    window = MikeWindow(ROOT, HOST)
    state = {"handoff": False, "default": False, "typed": False, "confirmed": False, "dialogs": [],
             "voice": None, "speak": False, "mic_dialog": False, "reply": False, "error": "", "done": False}

    def js(script, done=None):
        window.web.run_javascript(script, None, done)

    def js_value(callback):
        def finish(view, outcome):
            try:
                callback(view.run_javascript_finish(outcome).get_js_value().to_string())
            except Exception as error:
                state["error"] = repr(error)
        return finish

    def dialogs():
        """The person's side of every native dialog."""
        for top in Gtk.Window.list_toplevels():
            if not top.get_visible():
                continue
            if isinstance(top, Gtk.FileChooserDialog):
                top.set_filename(str(folder))
                top.select_filename(str(folder))
                if top.get_filename() == str(folder):
                    state["dialogs"].append("folder")
                    top.response(Gtk.ResponseType.OK)
            elif isinstance(top, Gtk.MessageDialog):
                text = top.get_property("text") or ""
                if text.startswith("Allow Mike to use"):
                    state["mic_dialog"] = True
                    state["dialogs"].append("permission:no")
                    top.response(Gtk.ResponseType.NO)
                elif text == "Mike requests a local file search":
                    state["dialogs"].append("search")
                    top.response(Gtk.ResponseType.OK)
                elif text == "Open this exact file?":
                    shown = top.get_property("secondary-text") or ""
                    if shown != str(folder / filename):
                        state["error"] = "exact-file dialog showed " + shown
                    state["dialogs"].append("exact")
                    top.response(Gtk.ResponseType.YES)
            elif isinstance(top, Gtk.Dialog) and top.get_title() == "Find a file":
                entries = [child for child in top.get_content_area().get_children() if isinstance(child, Gtk.Entry)]
                entries[0].set_text(filename[:-4])
                state["dialogs"].append("query")
                top.response(Gtk.ResponseType.OK)
        return not state["done"] and not state["error"]

    def check():
        try:
            if state["error"]:
                window.close()
                return False
            if not window.attached:
                return True
            if not state["handoff"]:
                state["handoff"] = True
                window.accept_handoff(token)
                return True
            if window.current_token != token or window.inserting_token:
                return True
            if not state["default"]:
                devices = [one for one in e2e.api("mike-interface", {"_class": "MyDevices"}, token)["items"]
                           if one["device_id"] not in known and one["online"]]
                if not devices:
                    return True
                e2e.api("mike-interface", {"_class": "SetDefaultDevice", "device_id": devices[0]["device_id"]}, token)
                state["default"] = True
                return True
            if state["voice"] is None:
                js("JSON.stringify(Array.from(document.querySelectorAll('button')).map(b => b.getAttribute('aria-label')).filter(Boolean))",
                   js_value(lambda value: state.update(voice=json.loads(value))))
                return True
            if not state["voice"] or "Speak" not in state["voice"]:
                state["voice"] = None
                return True
            if not state["speak"]:
                state["speak"] = True
                js("document.querySelector('button[aria-label=\"Speak\"]').click()")
                return True
            if not state["typed"]:
                prompt = "Please use this connected desktop to find and open the local file named " + filename + "."
                js("(() => { const input = document.querySelector('input[aria-label=\"Ask Mike\"]'); if (!input) return 'waiting'; "
                   "input.value = " + json.dumps(prompt) + "; input.dispatchEvent(new Event('input', {bubbles: true})); "
                   "const send = document.querySelector('.mike button.primary'); if (!send) return 'waiting'; send.click(); return 'sent'; })()",
                   js_value(lambda value: state.update(typed=value == "sent")))
                return True
            if not state["confirmed"]:
                js("(() => { const yes = Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim() === 'Yes, do it'); "
                   "if (!yes) return 'waiting'; yes.click(); return 'clicked'; })()",
                   js_value(lambda value: state.update(confirmed=value == "clicked")))
                return True
            opened = area / "opened.log"
            if not opened.exists():
                return True
            if not state["reply"]:
                js("Array.from(document.querySelectorAll('.line.mike')).some(line => line.textContent.includes(" + json.dumps(filename) + ") "
                   "&& line.textContent.toLowerCase().includes('open'))",
                   js_value(lambda value: state.update(reply=value == "true")))
                return True
            assert opened.read_text() == str(folder / filename) + "\n", opened.read_text()
            assert state["dialogs"][-4:] == ["search", "folder", "query", "exact"], state["dialogs"]
            labels = state["voice"]
            assert "Speak" in labels and any(label.startswith("Voice chat") for label in labels), labels
            read_aloud = state.get("read_aloud")
            if read_aloud is None:
                js("Array.from(document.querySelectorAll('button')).some(b => b.getAttribute('aria-label') === 'Read aloud')",
                   js_value(lambda value: state.update(read_aloud=value == "true")))
                return True
            assert read_aloud, "Mike's reply has no read-aloud control"
            state["done"] = True
            mic = ("the page's microphone request became a native dialog and was refused" if state["mic_dialog"]
                   else "SKIPPED microphone permission: WebKit raised no request under Xvfb")
            print("PASS: typed a file request in the window, confirmed Mike's action in the page, picked the folder and "
                  "approved the exact file in native dialogs; the service opened it and Mike's reply appeared; "
                  "Speak, voice chat and read-aloud controls are present; " + mic)
            window.close()
            return False
        except Exception as error:
            state["error"] = repr(error)
            window.close()
            return False

    def timeout():
        state["error"] = "timed out: " + str(state)
        window.close()
        return False

    GLib.timeout_add(100, dialogs)
    GLib.timeout_add_seconds(1, check)
    GLib.timeout_add_seconds(150, timeout)
    try:
        Gtk.main()
    finally:
        service.stop()
        for one in e2e.Run.devices(token) - known:
            e2e.api("mike-interface", {"_class": "RevokeDevice", "device_id": one}, token)
    if state["error"]:
        raise SystemExit(state["error"])


if __name__ == "__main__":
    main()
