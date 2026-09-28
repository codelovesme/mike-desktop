#!/usr/bin/env python3
"""Live Mike turn through the WebView companion's authenticated local bridge.

Use a disposable verified account. The real Mike and device gateway run behind
EUGLENA_HOST_URL; only xdg-open is replaced with a recorder.
"""

import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(os.environ.get("E2E_DESKTOP_DIR", Path(__file__).resolve().parents[1]))
HOST = os.environ.get("EUGLENA_HOST_URL", "http://127.0.0.1:8923").rstrip("/")
EMAIL = os.environ.get("E2E_EMAIL", "")
PASSWORD = os.environ.get("E2E_PASSWORD", "")
CODE = os.environ.get("CODE_BIN", "/home/cdlvsm/git/codelovesme/code/target/release/code")


def post(url, payload, headers=None, timeout=30):
    request = Request(url, json.dumps(payload).encode(),
                      {"Content-Type": "application/json", **(headers or {})})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode()
    except HTTPError as error:
        return error.code, error.read().decode()


def api(app, particle, token=None):
    envelope = {"_class": "Impulse" if token else "Open", "particle": particle}
    if token:
        envelope["token"] = token
    status, body = post(HOST + "/api/" + app, envelope, {"User-Agent": "Mike-E2E/0.3"})
    assert status == 200, (app, status, body[:200])
    return json.loads(body)


def until(label, check, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        answer = check()
        if answer:
            return answer
        time.sleep(1)
    raise AssertionError("timed out waiting for " + label)


def main():
    if not EMAIL or not PASSWORD:
        raise SystemExit("Set E2E_EMAIL and E2E_PASSWORD for a disposable verified account.")
    signed = api("auth", {"_class": "Authenticate", "email": EMAIL, "password": PASSWORD})
    assert signed.get("_class") == "SignedIn", signed
    token = signed["token"]
    before = api("mike-interface", {"_class": "MyDevices"}, token)
    known = {item["device_id"] for item in before.get("items", [])}
    with tempfile.TemporaryDirectory(prefix="mike-bridge-e2e-") as temporary:
        area = Path(temporary)
        chosen = area / "chosen"
        chosen.mkdir()
        filename = "mike-bridge-" + secrets.token_hex(5) + ".txt"
        exact = chosen / filename
        exact.write_text("Mike desktop bridge fixture\n")
        opened = area / "opened"
        bin_dir = area / "bin"
        bin_dir.mkdir()
        opener = bin_dir / "xdg-open"
        opener.write_text('#!/bin/sh\nprintf "%s\\n" "$1" > "$MIKE_OPEN_LOG"\n')
        opener.chmod(0o700)
        bridge_dir = area / "bridge"
        bridge_dir.mkdir(mode=0o700)
        secret = secrets.token_urlsafe(48)
        env = os.environ.copy()
        env.update(MIKE_DESKTOP_HOST_URL=HOST, MIKE_DESKTOP_BRIDGE_SECRET=secret,
                   MIKE_DESKTOP_BRIDGE_DIR=str(bridge_dir), MIKE_OPEN_LOG=str(opened),
                   PATH=str(bin_dir) + os.pathsep + env.get("PATH", ""))
        process = subprocess.Popen([CODE, "run", "main.code"], cwd=ROOT, env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            port_file = bridge_dir / "port"
            until("bridge port", lambda: port_file.exists() or process.poll() is not None, 10)
            assert process.poll() is None and port_file.exists(), "companion failed to start"
            base = "http://127.0.0.1:" + port_file.read_text()
            headers = {"Authorization": "Bearer " + secret}

            def bridge(path, payload=None):
                if payload is not None:
                    return post(base + path, payload, headers)
                request = Request(base + path, headers=headers)
                with urlopen(request, timeout=20) as response:
                    return response.status, response.read().decode()

            status, _ = post(base + "/session", {"token": token})
            assert status == 403, "bridge accepted a request without its local secret"
            status, body = bridge("/task/decide", {"yes": True})
            assert status == 401, (status, body)
            status, body = bridge("/command", {"command": "touch /tmp/should-not-exist"})
            assert status == 401, (status, body)
            status, body = bridge("/session", {"token": token})
            assert status == 200, (status, body)
            state = json.loads(bridge("/state")[1])
            assert state["connection"] == "connected", state
            status, _ = bridge("/command", {"command": "touch /tmp/should-not-exist"})
            assert status == 404, "the bridge accepted a command route"
            status, _ = bridge("/task/search", {"folder": "/tmp", "query": "invoice"})
            assert status == 409, "search began without a claimed task"

            started = api("mike", {"_class": "Ask", "text":
                "Please use this connected desktop to find and open the local file named " + filename + "."}, token)
            assert started.get("_class") == "Thinking", started
            turn_id = started["turn_id"]

            def pending():
                answer = api("mike", {"_class": "Progress", "turn_id": turn_id}, token)
                if answer.get("stage") == "confirm":
                    return answer
                if answer.get("stage") == "failed":
                    raise AssertionError("Mike's turn failed: " + str(answer)[:400])
                return None

            until("Mike's approval", pending, 120)
            confirmed = api("mike", {"_class": "Confirm", "turn_id": turn_id, "yes": True}, token)
            assert confirmed.get("_class") not in ("Denied", "Forbidden"), confirmed

            def task():
                state = json.loads(bridge("/state")[1])
                return state if state["task_id"] else None

            state = until("desktop task", task, 60)
            assert state["mode"] in ("root_task", "query"), state
            status, body = bridge("/task/search", {"folder": str(chosen), "query": filename})
            assert status == 200, (status, body)
            state = json.loads(bridge("/state")[1])
            assert state["mode"] == "file_confirm" and state["file"] == str(exact), state
            assert not opened.exists(), "search opened a file before native confirmation"
            status, _ = bridge("/task/decide", {"yes": "true"})
            assert status == 409 and not opened.exists(), "a string bypassed native confirmation"
            status, body = bridge("/task/decide", {"yes": True})
            assert status == 200, (status, body)
            until("exact file open", lambda: opened.exists(), 15)
            assert opened.read_text().strip() == str(exact), "wrong file opened"
            devices = api("mike-interface", {"_class": "MyDevices"}, token)
            current = [item for item in devices.get("items", []) if item["active"] and item["device_id"] not in known]
            assert current, "the companion did not pair a new device"
            api("mike-interface", {"_class": "RevokeDevice", "device_id": current[-1]["device_id"]}, token)
            until("revoked state", lambda: json.loads(bridge("/state")[1])["connection"] == "revoked", 15)
            status, body = bridge("/session", {"token": token})
            assert status == 200, (status, body)
            assert json.loads(bridge("/state")[1])["connection"] == "connected"
            print("PASS: Mike requested, web approval reached the gateway, and native confirmation opened", filename)
        finally:
            process.terminate()
            process.wait(timeout=5)
            after = api("mike-interface", {"_class": "MyDevices"}, token)
            for item in after.get("items", []):
                if item["device_id"] not in known:
                    api("mike-interface", {"_class": "RevokeDevice", "device_id": item["device_id"]}, token)


if __name__ == "__main__":
    main()
