#!/usr/bin/python3
"""Use the real GTK confirmation dialog and worker for a terminal step."""

import os
import importlib.util
import json
from pathlib import Path
import sys

e2e_spec = importlib.util.spec_from_file_location("terminal_e2e", Path(__file__).with_name("terminal-e2e.py"))
e2e = importlib.util.module_from_spec(e2e_spec)
e2e_spec.loader.exec_module(e2e)
api, EMAIL, PASSWORD = e2e.api, e2e.EMAIL, e2e.PASSWORD

ROOT = Path(os.environ.get("E2E_DESKTOP_DIR", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT / "ui"))
from desktop_webview import GLib, Gtk, MikeWindow  # noqa: E402

CODE = os.environ.get("CODE_BIN", "/home/cdlvsm/git/codelovesme/code/target/release/code")
HOST = os.environ.get("EUGLENA_HOST_URL", "http://127.0.0.1:8923").rstrip("/")
DECLINE = os.environ.get("MIKE_TEST_DECLINE") == "1"
STOP = os.environ.get("MIKE_TEST_STOP") == "1"
SIGNOUT = os.environ.get("MIKE_TEST_SIGNOUT") == "1"


def main():
    if not EMAIL or not PASSWORD:
        raise SystemExit("Set E2E_EMAIL and E2E_PASSWORD")
    signed = api("auth", {"_class": "Authenticate", "email": EMAIL, "password": PASSWORD})
    assert signed["_class"] == "SignedIn", signed
    token = signed["token"]
    before = api("mike-interface", {"_class": "MyDevices"}, token)
    known = {item["device_id"] for item in before.get("items", [])}
    window = MikeWindow(ROOT, CODE, HOST)
    result = {"handoff": False, "sent": False, "accepted": False, "stopped": False,
              "step": "", "device": "", "error": "", "sessions": []}
    original_bridge = window.async_bridge

    def traced_bridge(path, payload, done):
        if path == "/session":
            result["sessions"].append("expected" if payload.get("token") == token else "other")
        return original_bridge(path, payload, done)

    window.async_bridge = traced_bridge

    def approve():
        for top in Gtk.Window.list_toplevels():
            if isinstance(top, Gtk.MessageDialog) and top.get_visible():
                result["accepted"] = True
                top.response(Gtk.ResponseType.NO if DECLINE else Gtk.ResponseType.YES)
        return not result["accepted"]

    def tick():
        try:
            if window.port is None:
                return True
            if not result["handoff"]:
                result["handoff"] = True
                window.accept_handoff(token)
                return True
            if window.current_token != token:
                return True
            if window.inserting_token:
                return True
            if not result["sent"]:
                devices = api("mike-interface", {"_class": "MyDevices"}, token)
                new = [one for one in devices["items"] if one["device_id"] not in known]
                if not new or not new[0]["online"] or "terminal" not in new[0]["capabilities"]:
                    return True
                result["device"] = new[0]["device_id"]
                started = api("mike-interface", {"_class": "RunTerminalStep", "argv": ["sleep", "30"] if STOP or SIGNOUT else ["pwd"],
                                                   "cwd": "/tmp", "device_id": result["device"]}, token)
                assert started["_class"] == "InvocationAccepted", started
                result["step"] = started["id"]
                result["sent"] = True
                GLib.timeout_add(100, approve)
                return True
            if STOP and window.terminal_worker is not None and not result["stopped"]:
                result["stopped"] = True
                window.stop_terminal()
            if SIGNOUT and window.terminal_worker is not None and not result["stopped"]:
                result["stopped"] = True
                window.sign_out_button.clicked()
            progress = api("mike-interface", {"_class": "InvocationProgress", "id": result["step"]}, token)
            if progress.get("state") == "completed":
                assert result["accepted"], "the native dialog was not approved: " + str(progress) + " sessions=" + str(result["sessions"])
                if SIGNOUT:
                    assert result["stopped"] and window.current_token == "", "sign-out did not reach the native controller"
                    assert progress["result"]["state"] == "cancelled", progress
                    assert window.terminal_worker is None or window.terminal_worker.poll() is not None, "worker survived sign-out"
                    local = json.loads(window.bridge("/state")[1])
                    assert local["connection"] == "sign in" and local["task_id"] == "", local
                    print("PASS: signing out stopped the worker and Mike received cancellation")
                    window.close()
                    return False
                if STOP:
                    assert result["stopped"], "worker finished before Stop"
                    assert progress["result"]["state"] == "cancelled", progress
                    print("PASS: GTK approved sleep, Stop ended the worker, and Mike received cancellation")
                    window.close()
                    return False
                if DECLINE:
                    assert progress["result"]["state"] == "denied", progress
                    assert window.terminal_worker is None, "a denied command started a worker"
                    print("PASS: GTK declined the command and no local worker ran")
                    window.close()
                    return False
                assert progress["result"]["state"] == "completed", str(progress) + " sessions=" + str(result["sessions"])
                assert progress["result"]["stdout"].strip() == "/tmp", progress
                print("PASS: GTK asked for approval, the local worker ran pwd, and the gateway received output")
                window.close()
                return False
        except Exception as error:
            result["error"] = str(error)
            window.close()
            return False
        return True

    def timeout():
        try:
            state = window.bridge("/state")[1]
            observed = json.loads(state)
            diagnostic = {key: observed.get(key) for key in ("connection", "mode", "task_id")}
        except Exception as error:
            diagnostic = str(error)
        progress = api("mike-interface", {"_class": "InvocationProgress", "id": result["step"]}, token) if result["step"] else {}
        result["error"] = "timed out waiting for terminal UI flow: " + str(result) + " / " + str(diagnostic) + " / progress=" + str(progress)[:300] + " / native_session=" + str(bool(window.current_token)) + " inserting=" + str(window.inserting_token) + " worker=" + str(window.terminal_worker is not None)
        window.close()
        return False

    GLib.timeout_add_seconds(1, tick)
    GLib.timeout_add_seconds(35, timeout)
    Gtk.main()
    if result["device"]:
        api("mike-interface", {"_class": "RevokeDevice", "device_id": result["device"]}, token)
    if result["error"]:
        raise SystemExit(result["error"])


if __name__ == "__main__":
    main()
