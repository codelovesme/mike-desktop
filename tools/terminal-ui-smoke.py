#!/usr/bin/python3
"""Drive the real GTK window, as a client of a real scratch Mike service.

Run under Xvfb (and a private D-Bus) with a disposable account. The window
gets a browser handoff, the service pairs, a terminal step arrives through
the gateway and the native dialog is answered. Modes (env, one at a time):

  (none)                approve pwd; activity shows its output
  MIKE_TEST_DECLINE=1   decline; nothing runs
  MIKE_TEST_STOP=1      Stop ends a running command
  MIKE_TEST_SIGNOUT=1   Sign out stops it and signs the service out
  MIKE_TEST_WEB_SIGNOUT=1  signing out in the page does the same
  MIKE_TEST_SWITCH=1    a handoff to a second account (E2E_SECOND_*) isolates it
  MIKE_TEST_REVOKE=1    website revocation stops it and returns to welcome
  MIKE_TEST_CLOSE=1     closing the window leaves the service running: the
                        approved command finishes, a new step waits unrun,
                        and a reopened window presents and runs it
"""

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import time

e2e_spec = importlib.util.spec_from_file_location("service_e2e", Path(__file__).with_name("service-e2e.py"))
e2e = importlib.util.module_from_spec(e2e_spec)
e2e_spec.loader.exec_module(e2e)
api = e2e.api

ROOT = Path(os.environ.get("E2E_DESKTOP_DIR", Path(__file__).resolve().parents[1]))
HOST = os.environ.get("EUGLENA_HOST_URL", "http://127.0.0.1:8923").rstrip("/")
EMAIL, PASSWORD = os.environ.get("E2E_EMAIL", ""), os.environ.get("E2E_PASSWORD", "")
DECLINE = os.environ.get("MIKE_TEST_DECLINE") == "1"
STOP = os.environ.get("MIKE_TEST_STOP") == "1"
SIGNOUT = os.environ.get("MIKE_TEST_SIGNOUT") == "1"
WEB_SIGNOUT = os.environ.get("MIKE_TEST_WEB_SIGNOUT") == "1"
SWITCH = os.environ.get("MIKE_TEST_SWITCH") == "1"
REVOKE = os.environ.get("MIKE_TEST_REVOKE") == "1"
CLOSE = os.environ.get("MIKE_TEST_CLOSE") == "1"
LONG = STOP or SIGNOUT or WEB_SIGNOUT or SWITCH or REVOKE


def main():
    if not EMAIL or not PASSWORD:
        raise SystemExit("Set E2E_EMAIL and E2E_PASSWORD")
    token = e2e.Run.sign_in(EMAIL, PASSWORD)
    second_email = os.environ.get("E2E_SECOND_EMAIL", "")
    second_token = e2e.Run.sign_in(second_email, os.environ["E2E_SECOND_PASSWORD"]) if SWITCH else ""
    known = e2e.Run.devices(token)
    area = Path(tempfile.mkdtemp(prefix="mike-ui-smoke-"))
    service = e2e.Service(area)
    os.environ.update(MIKE_SERVICE_STATE_DIR=service.env["MIKE_SERVICE_STATE_DIR"],
                      MIKE_SERVICE_RUNTIME_DIR=service.env["MIKE_SERVICE_RUNTIME_DIR"])
    service.start()
    sys.path.insert(0, str(ROOT / "ui"))
    from desktop_webview import GLib, Gtk, MikeWindow, activity_text  # noqa: E402

    window = MikeWindow(ROOT, HOST)
    result = {"handoff": False, "sent": False, "accepted": 0, "stopped": False, "step": "", "device": "",
              "error": "", "connects": [], "closed_step": "", "reopened": None, "phase": "first"}
    original = window.service_call

    def traced(action, payload, done):
        if action == "attach" and payload and payload.get("token"):
            result["connects"].append((payload.get("connect"), "expected" if payload["token"] == token else "other"))
        return original(action, payload, done)

    window.service_call = traced

    def answer_dialogs():
        for top in Gtk.Window.list_toplevels():
            if isinstance(top, Gtk.MessageDialog) and top.get_visible():
                result["accepted"] += 1
                top.response(Gtk.ResponseType.NO if DECLINE else Gtk.ResponseType.YES)
        return not result["error"] and result["phase"] != "done"

    def local_state():
        client = e2e.MikeService("smoke-observer", approvals=False, directory=service.run_dir)
        try:
            return client.attach()
        finally:
            client.detach()

    def finish(message):
        print("PASS: " + message)
        result["phase"] = "done"
        if result["reopened"] is not None:
            result["reopened"].close()
        else:
            window.close()
        return False

    def tick():
        try:
            if not window.attached:
                return True
            if not result["handoff"]:
                result["handoff"] = True
                window.accept_handoff(token)
                return True
            if window.current_token != token and not ((SIGNOUT or WEB_SIGNOUT or SWITCH or REVOKE) and result["stopped"]):
                return True
            if window.inserting_token:
                return True
            if not result["sent"]:
                devices = api("mike-interface", {"_class": "MyDevices"}, token)
                new = [one for one in devices["items"] if one["device_id"] not in known]
                if not new or not new[0]["online"] or "terminal" not in new[0]["capabilities"]:
                    return True
                result["device"] = new[0]["device_id"]
                argv = ["sleep", "24.159"] if LONG else (["sh", "-c", "sleep 3; echo after-close"] if CLOSE else ["pwd"])
                started = api("mike-interface", {"_class": "RunTerminalStep", "argv": argv, "cwd": "/tmp", "device_id": result["device"]}, token)
                assert started["_class"] == "InvocationAccepted", started
                result["step"] = started["id"]
                result["sent"] = True
                return True
            running = bool(window.running_work)
            if LONG and running and not result["stopped"]:
                result["stopped"] = True
                if STOP:
                    window.stop_terminal()
                if SIGNOUT:
                    window.sign_out_button.clicked()
                if WEB_SIGNOUT:
                    window.web.run_javascript(
                        "(() => { const profile = document.querySelector('.profile-button'); "
                        "if (!profile) return 'no-profile'; profile.click(); let attempts = 0; "
                        "const timer = setInterval(() => { const signout = document.querySelector('.profile-menu-button.danger'); "
                        "if (signout) { clearInterval(timer); signout.click(); } "
                        "if (++attempts > 50) clearInterval(timer); }, 100); return 'profile-opened'; })()",
                        None, None,
                    )
                if SWITCH:
                    window.accept_handoff(second_token)
                if REVOKE:
                    revoked = api("mike-interface", {"_class": "RevokeDevice", "device_id": result["device"]}, token)
                    assert revoked["_class"] == "DeviceRevoked", revoked
                return True
            if CLOSE and running and not result["stopped"]:
                result["stopped"] = True
                window.close()
                return False
            progress = api("mike-interface", {"_class": "InvocationProgress", "id": result["step"]}, token)
            if progress.get("state") != "completed":
                return True
            outcome = progress["result"]
            assert result["accepted"], "the native dialog was not answered: " + str(progress) + " connects=" + str(result["connects"])
            local = local_state()
            if SIGNOUT or WEB_SIGNOUT:
                assert outcome["state"] == "cancelled", progress
                if local["connection"] != "signed_out" or window.activity_seen:
                    return True
                assert window.current_token == "" and window.require_handoff
                return finish(("web" if WEB_SIGNOUT else "native") + " sign-out stopped the command in the service, Mike received cancellation, the service signed out")
            if SWITCH:
                assert outcome["state"] == "cancelled", progress
                if window.current_token != second_token or local["account"] != second_email:
                    return True
                assert not window.activity_seen, "old account activity remained visible"
                foreign = api("mike-interface", {"_class": "InvocationProgress", "id": result["step"]}, second_token)
                assert foreign["_class"] == "Unknown", foreign
                return finish("switching accounts stopped the old command and isolated its result")
            if REVOKE:
                assert outcome["state"] == "unknown", progress
                if local["connection"] != "revoked" or window.current_token:
                    return True
                assert not window.activity_seen, "revoked account activity remained visible"
                refused = api("mike-interface", {"_class": "RunTerminalStep", "argv": ["pwd"], "cwd": "/tmp", "device_id": result["device"]}, token)
                assert refused["_class"] != "InvocationAccepted", refused
                return finish("revocation stopped the command, the window returned to welcome, later steps are barred")
            if STOP:
                assert outcome["state"] == "cancelled", progress
                return finish("GTK approved the command, Stop ended it in the service, Mike received cancellation")
            if DECLINE:
                assert outcome["state"] == "denied", progress
                return finish("GTK declined the command and nothing ran")
            assert outcome["state"] == "completed" and outcome["stdout"].strip() == "/tmp", progress
            text = activity_text(window.activity_seen)
            if "Result: completed" not in text:
                return True
            assert "Requested: pwd" in text and "stdout:\n/tmp" in text, text
            return finish("GTK asked for approval, the service ran pwd, the gateway received output, activity shows it")
        except Exception as error:
            result["error"] = repr(error)
            window.close()
            return False

    def after_close():
        """The window is gone (its main loop ended); the service is not."""
        assert e2e.mike_service.reachable(service.run_dir), "closing the window stopped the service"
        done = e2e.until("the approved command after close",
                         lambda: (lambda p: p if p.get("state") == "completed" else None)(
                             api("mike-interface", {"_class": "InvocationProgress", "id": result["step"]}, token)), 20)
        assert done["result"]["state"] == "completed" and done["result"]["stdout"] == "after-close\n", done
        marker = area / "while-closed"
        started = api("mike-interface", {"_class": "RunTerminalStep", "argv": ["touch", str(marker)], "cwd": "/tmp",
                                         "device_id": result["device"]}, token)
        result["closed_step"] = started["id"]
        e2e.until("the step to be claimed with no window", lambda: (local_state().get("work") or {}).get("id") == started["id"], 20)
        time.sleep(5)
        listed = api("mike-interface", {"_class": "MyDevices"}, token)
        assert [one for one in listed["items"] if one["device_id"] == result["device"]][0]["online"], "presence lapsed"
        assert not marker.exists(), "a step ran while no window was open"
        assert api("mike-interface", {"_class": "InvocationProgress", "id": result["closed_step"]}, token)["state"] == "pending"
        reopened = MikeWindow(ROOT, HOST)
        result["reopened"] = reopened
        result["accepted"] = 0

        def check():
            try:
                if result["phase"] == "done":
                    return False
                progress = api("mike-interface", {"_class": "InvocationProgress", "id": result["closed_step"]}, token)
                if progress.get("state") != "completed":
                    return True
                assert result["accepted"] == 1 and marker.exists() and progress["result"]["state"] == "completed", progress
                return finish("closing the window kept the service: the approved command finished, a new step waited "
                              "unrun while closed (presence online), and the reopened window presented it and it ran")
            except Exception as error:
                result["error"] = repr(error)
                reopened.close()
                return False

        GLib.timeout_add_seconds(1, check)
        Gtk.main()

    def timeout():
        result["error"] = "timed out: " + str({k: v for k, v in result.items() if k != "reopened"})
        (result["reopened"] or window).close()
        return False

    GLib.timeout_add(100, answer_dialogs)
    GLib.timeout_add_seconds(1, tick)
    GLib.timeout_add_seconds(60, timeout)
    try:
        Gtk.main()
        if CLOSE and result["stopped"] and not result["error"]:
            after_close()
    finally:
        for one in e2e.Run.devices(token) - known:
            api("mike-interface", {"_class": "RevokeDevice", "device_id": one}, token)
        if SWITCH:
            for one in e2e.Run.devices(second_token):
                api("mike-interface", {"_class": "RevokeDevice", "device_id": one}, second_token)
        service.stop()
    if result["error"]:
        raise SystemExit(result["error"])


if __name__ == "__main__":
    main()
