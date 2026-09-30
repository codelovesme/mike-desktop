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
WEB_SIGNOUT = os.environ.get("MIKE_TEST_WEB_SIGNOUT") == "1"
SWITCH = os.environ.get("MIKE_TEST_SWITCH") == "1"
REVOKE = os.environ.get("MIKE_TEST_REVOKE") == "1"
CLOSE = os.environ.get("MIKE_TEST_CLOSE") == "1"


def main():
    if not EMAIL or not PASSWORD:
        raise SystemExit("Set E2E_EMAIL and E2E_PASSWORD")
    signed = api("auth", {"_class": "Authenticate", "email": EMAIL, "password": PASSWORD})
    assert signed["_class"] == "SignedIn", signed
    token = signed["token"]
    second_token = ""
    second_email = os.environ.get("E2E_SECOND_EMAIL", "")
    if SWITCH:
        assert second_email and os.environ.get("E2E_SECOND_PASSWORD"), "set second test account"
        second = api("auth", {"_class": "Authenticate", "email": second_email,
                              "password": os.environ["E2E_SECOND_PASSWORD"]})
        assert second["_class"] == "SignedIn", second
        second_token = second["token"]
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
            if window.current_token != token and not ((SIGNOUT or WEB_SIGNOUT or SWITCH) and result["stopped"]):
                return True
            if window.inserting_token:
                return True
            if not result["sent"]:
                devices = api("mike-interface", {"_class": "MyDevices"}, token)
                new = [one for one in devices["items"] if one["device_id"] not in known]
                if not new or not new[0]["online"] or "terminal" not in new[0]["capabilities"]:
                    return True
                result["device"] = new[0]["device_id"]
                started = api("mike-interface", {"_class": "RunTerminalStep", "argv": ["sleep", "30"] if STOP or SIGNOUT or WEB_SIGNOUT or SWITCH or REVOKE or CLOSE else ["pwd"],
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
            if WEB_SIGNOUT and window.terminal_worker is not None and not result["stopped"]:
                result["stopped"] = True
                window.web.run_javascript(
                    "(() => { const profile = document.querySelector('.profile-button'); "
                    "if (!profile) return 'no-profile'; profile.click(); let attempts = 0; "
                    "const timer = setInterval(() => { const signout = document.querySelector('.profile-menu-button.danger'); "
                    "if (signout) { clearInterval(timer); signout.click(); } "
                    "if (++attempts > 50) clearInterval(timer); }, 100); return 'profile-opened'; })()",
                    None, None,
                )
            if SWITCH and window.terminal_worker is not None and not result["stopped"]:
                result["stopped"] = True
                window.accept_handoff(second_token)
            if REVOKE and window.terminal_worker is not None and not result["stopped"]:
                result["stopped"] = True
                revoked = api("mike-interface", {"_class": "RevokeDevice", "device_id": result["device"]}, token)
                assert revoked["_class"] == "DeviceRevoked", revoked
            if CLOSE and window.terminal_worker is not None and not result["stopped"]:
                result["stopped"] = True
                worker = window.terminal_worker
                window.close()
                worker.wait(timeout=4)
                assert worker.poll() is not None, "worker survived window close"
                return False
            progress = api("mike-interface", {"_class": "InvocationProgress", "id": result["step"]}, token)
            if progress.get("state") == "completed":
                assert result["accepted"], "the native dialog was not approved: " + str(progress) + " sessions=" + str(result["sessions"])
                if SIGNOUT or WEB_SIGNOUT:
                    assert result["stopped"] and window.current_token == "", "sign-out did not reach the native controller"
                    assert window.activity_log == "", "old output remained visible after sign-out"
                    assert progress["result"]["state"] == "cancelled", progress
                    assert window.terminal_worker is None or window.terminal_worker.poll() is not None, "worker survived sign-out"
                    local = json.loads(window.bridge("/state")[1])
                    if local["connection"] != "sign in":
                        return True
                    assert local["connection"] == "sign in" and local["task_id"] == "", local
                    print("PASS: " + ("web" if WEB_SIGNOUT else "native") + " sign-out stopped the worker and Mike received cancellation")
                    window.close()
                    return False
                if SWITCH:
                    assert result["stopped"] and progress["result"]["state"] == "cancelled", progress
                    if window.current_token != second_token:
                        return True
                    local = json.loads(window.bridge("/state")[1])
                    assert local["connection"] == "connected" and local["owner"] == second_email, local
                    assert window.activity_log == "", "old account activity remained visible"
                    foreign = api("mike-interface", {"_class": "InvocationProgress", "id": result["step"]}, second_token)
                    assert foreign["_class"] == "Unknown", foreign
                    assert window.terminal_worker is None or window.terminal_worker.poll() is not None
                    print("PASS: switching accounts stopped the old command and isolated its result")
                    window.close()
                    return False
                if REVOKE:
                    assert result["stopped"] and progress["result"]["state"] == "unknown", progress
                    local = json.loads(window.bridge("/state")[1])
                    if local["connection"] != "revoked" or window.terminal_worker is not None and window.terminal_worker.poll() is None:
                        return True
                    assert window.activity_log == "", "revoked account activity remained visible"
                    refused = api("mike-interface", {"_class": "RunTerminalStep", "argv": ["pwd"],
                                                       "cwd": "/tmp", "device_id": result["device"]}, token)
                    assert refused["_class"] != "InvocationAccepted", refused
                    print("PASS: revocation stopped the command and barred later steps")
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
                if "Result: completed" not in window.activity_log:
                    return True
                assert "Requested: pwd" in window.activity_log and "stdout:\n/tmp" in window.activity_log
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
        if CLOSE:
            progress = api("mike-interface", {"_class": "InvocationProgress", "id": result["step"]}, token)
            assert result["stopped"] and progress["state"] == "completed" and progress["result"]["state"] in ("cancelled", "unknown"), progress
            print("PASS: closing Mike stopped the worker and exposed a cancelled or unknown remote outcome")
    if SWITCH:
        devices = api("mike-interface", {"_class": "MyDevices"}, second_token)
        for device in devices.get("items", []):
            if device.get("active"):
                api("mike-interface", {"_class": "RevokeDevice", "device_id": device["device_id"]}, second_token)
    if result["error"]:
        raise SystemExit(result["error"])


if __name__ == "__main__":
    main()
