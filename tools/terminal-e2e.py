#!/usr/bin/env python3
"""Exercise real Mike, gateway, and native companion terminal step.

Use a disposable verified account. The test substitutes an explicit bridge
approval for a human click; the worker still runs the real local command.
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
CODE = os.environ.get("CODE_BIN", "/home/cdlvsm/git/codelovesme/code/target/release/code")
EMAIL = os.environ.get("E2E_EMAIL", "")
PASSWORD = os.environ.get("E2E_PASSWORD", "")
MULTI = os.environ.get("MIKE_E2E_MULTI") == "1"


def post(url, payload, headers=None):
    request = Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json", "User-Agent": "Mike-E2E/0.4", **(headers or {})})
    try:
        with urlopen(request, timeout=25) as response:
            body = response.read().decode()
            try:
                return response.status, json.loads(body)
            except ValueError:
                return response.status, body
    except HTTPError as error:
        return error.code, error.read().decode()


def api(app, particle, token=None):
    envelope = {"_class": "Impulse" if token else "Open", "particle": particle}
    if token:
        envelope["token"] = token
    status, result = post(HOST + "/api/" + app, envelope)
    assert status == 200, (app, status, result)
    return result


def until(label, probe, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(1)
    raise AssertionError("timed out waiting for " + label)


def main():
    if not EMAIL or not PASSWORD:
        raise SystemExit("Set E2E_EMAIL and E2E_PASSWORD for a disposable verified account")
    signed = api("auth", {"_class": "Authenticate", "email": EMAIL, "password": PASSWORD})
    assert signed.get("_class") == "SignedIn", signed
    token = signed["token"]
    before = api("mike-interface", {"_class": "MyDevices"}, token)
    known = {item["device_id"] for item in before.get("items", [])}
    with tempfile.TemporaryDirectory(prefix="mike-terminal-e2e-") as temporary:
        area = Path(temporary)
        bridge_dir = area / "bridge"
        bridge_dir.mkdir(mode=0o700)
        secret = secrets.token_urlsafe(48)
        env = dict(os.environ, MIKE_DESKTOP_HOST_URL=HOST, MIKE_DESKTOP_BRIDGE_SECRET=secret,
                   MIKE_DESKTOP_BRIDGE_DIR=str(bridge_dir))
        process = subprocess.Popen([CODE, "run", "main.code"], cwd=ROOT, env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        device_id = ""
        try:
            until("bridge", lambda: (bridge_dir / "port").exists() or process.poll() is not None, 15)
            assert process.poll() is None, "native companion exited"
            base = "http://127.0.0.1:" + (bridge_dir / "port").read_text()
            headers = {"Authorization": "Bearer " + secret}

            def bridge(path, payload=None):
                if payload is not None:
                    return post(base + path, payload, headers)
                request = Request(base + path, headers=headers)
                with urlopen(request, timeout=20) as response:
                    return response.status, json.loads(response.read())

            assert bridge("/session", {"token": token})[0] == 200
            state = bridge("/state")[1]
            assert state["connection"] == "connected", state
            devices = api("mike-interface", {"_class": "MyDevices"}, token)
            new = [item for item in devices["items"] if item["device_id"] not in known]
            assert len(new) == 1, new
            device_id = new[0]["device_id"]
            assert "terminal" in new[0]["capabilities"], new[0]
            assert bridge("/terminal/start", {})[0] == 409
            assert bridge("/command", {"command": "id"})[0] == 404

            marker = "mike-terminal-" + secrets.token_hex(4)
            goal = (
                "On my connected desktop, run pwd, then ls, then ps as three separate terminal commands "
                "with working directory /tmp. Report the working directory, one directory entry, "
                "and one process name from their actual output. "
                if MULTI else
                "On my connected desktop, run the terminal command pwd with working directory /tmp, "
                "then tell me the working directory. "
            )
            started = api("mike", {"_class": "Ask", "text": goal + "This is a terminal task " + marker + "."}, token)
            assert started.get("_class") == "Thinking", started
            turn_id = started["turn_id"]

            seen = []

            def terminal_ready():
                state = bridge("/state")[1]
                if state["mode"] == "terminal_review" and state["task_id"] not in seen:
                    return state
                turn = api("mike", {"_class": "Progress", "turn_id": turn_id}, token)
                if turn.get("stage") == "failed":
                    raise AssertionError("Mike failed: " + str(turn)[:350])
                return None

            expected = ["pwd", "ls", "ps"] if MULTI else ["pwd"]
            for command in expected:
                step = until("Mike's " + command + " step", terminal_ready)
                seen.append(step["task_id"])
                assert Path(step["argv"][0]).name == command, step
                assert step["cwd"] == "/tmp", step
                assert bridge("/terminal/start", {})[0] == 200
                run = subprocess.run(["/usr/bin/python3", str(ROOT / "ui/terminal_worker.py")],
                                     input=json.dumps({"argv": step["argv"], "cwd": step["cwd"], "timeout_seconds": 10}),
                                     text=True, capture_output=True, timeout=15)
                assert run.returncode == 0, run.stderr
                result = json.loads(run.stdout)
                assert result["state"] == "completed" and result["stdout"], result
                if command == "pwd":
                    assert result["stdout"].strip() == "/tmp", result
                result = {key: result.get(key) for key in ("state", "exit_code", "stdout", "stderr")}
                for stream in ("stdout", "stderr"):
                    raw = result[stream].encode()
                    if len(raw) > 4096:
                        result[stream] = raw[:4077].decode("utf-8", "ignore") + "\n[output truncated]"
                result["id"] = step["task_id"]
                completion = bridge("/terminal/complete", result)
                assert completion[0] == 200, (command, completion, len(result["stdout"]), len(result["stderr"]))

                def observed():
                    progress = api("mike-interface", {"_class": "InvocationProgress", "id": step["task_id"]}, token)
                    return progress if progress.get("state") == "completed" else None

                completed = until("gateway " + command + " result", observed)
                assert completed["result"]["state"] == "completed", completed
            def mike_answer():
                turn = api("mike", {"_class": "Progress", "turn_id": turn_id}, token)
                if turn.get("stage") == "done":
                    return turn
                if turn.get("stage") == "failed":
                    raise AssertionError("Mike failed after terminal result: " + str(turn)[:350])
                return None

            answer = until("Mike's final answer", mike_answer)
            assert "/tmp" in answer.get("reply", ""), answer
            print("PASS: Mike proposed " + ", ".join(expected) + ", approved commands ran, and Mike answered from their output")
        finally:
            process.terminate()
            process.wait(timeout=5)
            if device_id:
                api("mike-interface", {"_class": "RevokeDevice", "device_id": device_id}, token)


if __name__ == "__main__":
    main()
