#!/usr/bin/env python3
"""Exercise the shared Mike service against a real host and gateway.

Every boundary is real: the native service (scratch state and runtime
directories), Mike Interface, Mike's model for the `turn` scenario, the
terminal worker and the descriptor-checking file opener (whose final portal
call is replaced by a recorder via MIKE_OPEN_LOG). Test clients stand in for
the person's clicks through the same local protocol Desktop uses.

  E2E_EMAIL/E2E_PASSWORD           a disposable verified account
  E2E_SECOND_EMAIL/E2E_SECOND_PASSWORD   a second one (account switch)
  EUGLENA_HOST_URL                 default http://127.0.0.1:8923
  CODE_BIN                         Code able to run the pinned modules
  MIKE_E2E_ONLY=a,b                run only these scenarios

Every device the run pairs is revoked at the end.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
HOST = os.environ.get("EUGLENA_HOST_URL", "http://127.0.0.1:8923").rstrip("/")
CODE = os.environ.get("CODE_BIN", "/home/cdlvsm/git/codelovesme/code/target/release/code")
sys.path.insert(0, str(ROOT / "client"))
import mike_service  # noqa: E402
from mike_service import MikeService, ServiceError  # noqa: E402


def post(url, payload):
    request = Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json", "User-Agent": "Mike-Service-E2E/1"})
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except HTTPError as error:
        raise AssertionError(f"{url}: HTTP {error.code}") from None


def api(app, particle, token=None):
    envelope = {"_class": "Impulse", "token": token, "particle": particle} if token else {"_class": "Open", "particle": particle}
    return post(HOST + "/api/" + app, envelope)


def alive(pattern):
    return subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", pattern], capture_output=True).returncode == 0


def until(label, probe, timeout=60, every=0.5):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = probe()
        if last:
            return last
        time.sleep(every)
    raise AssertionError("timed out waiting for " + label + " (last: " + str(last)[:300] + ")")


class Service:
    """One scratch service instance (as systemd would run it, but in a test dir)."""

    def __init__(self, area: Path, extra: dict | None = None):
        self.area = area
        self.run_dir = area / "run"
        self.env = dict(os.environ, MIKE_SERVICE_STATE_DIR=str(area / "state"), MIKE_SERVICE_RUNTIME_DIR=str(self.run_dir),
                        MIKE_SERVICE_HOST_URL=HOST, MIKE_OPEN_LOG=str(area / "opened.log"), **(extra or {}))
        self.process = None
        self.log = open(area / "service.log", "a")

    def start(self):
        self.process = subprocess.Popen([CODE, "run", "main.code"], cwd=ROOT / "service", env=self.env,
                                        stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT)
        until("service", lambda: mike_service.reachable(self.run_dir) or self.process.poll() is not None, 20, 0.1)
        assert self.process.poll() is None, "service exited: " + (self.area / "service.log").read_text()[-400:]

    def client(self, name, approvals=True):
        return MikeService(name, approvals=approvals, directory=self.run_dir)

    def kill(self):
        self.process.send_signal(signal.SIGKILL)
        self.process.wait(timeout=5)

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


class Run:
    def __init__(self):
        self.email = os.environ.get("E2E_EMAIL", "")
        password = os.environ.get("E2E_PASSWORD", "")
        if not self.email or not password:
            raise SystemExit("Set E2E_EMAIL and E2E_PASSWORD for a disposable verified account")
        self.token = self.sign_in(self.email, password)
        self.second_email = os.environ.get("E2E_SECOND_EMAIL", "")
        self.second_token = self.sign_in(self.second_email, os.environ["E2E_SECOND_PASSWORD"]) if self.second_email else ""
        self.known = self.devices(self.token) | (self.devices(self.second_token) if self.second_token else set())
        self.temporary = tempfile.TemporaryDirectory(prefix="mike-service-e2e-")
        self.results: list[tuple[str, str, str]] = []

    @staticmethod
    def sign_in(email, password):
        signed = api("auth", {"_class": "Authenticate", "email": email, "password": password})
        assert signed.get("_class") == "SignedIn", signed
        return signed["token"]

    @staticmethod
    def devices(token):
        listed = api("mike-interface", {"_class": "MyDevices"}, token)
        return {one["device_id"] for one in listed.get("items", [])}

    def service(self, name, extra=None) -> Service:
        area = Path(self.temporary.name) / name
        area.mkdir()
        service = Service(area, extra)
        service.start()
        return service

    def paired(self, client, token=None):
        state = client.attach(token or self.token, "auto")
        assert state["connection"] == "connected" and state["paired"], state
        return state

    def device_of(self, token=None):
        listed = api("mike-interface", {"_class": "MyDevices"}, token or self.token)
        new = [one for one in listed["items"] if one["device_id"] not in self.known and one.get("active")]
        assert len(new) == 1, new
        return new[0]

    def step(self, argv, device, token=None, cwd="/tmp"):
        started = api("mike-interface", {"_class": "RunTerminalStep", "argv": argv, "cwd": cwd, "device_id": device}, token or self.token)
        assert started.get("_class") == "InvocationAccepted", started
        return started["id"]

    def progress(self, step, token=None):
        return api("mike-interface", {"_class": "InvocationProgress", "id": step}, token or self.token)

    def finished(self, step, token=None, timeout=60):
        return until("gateway result for " + step,
                     lambda: (lambda p: p if p.get("state") == "completed" else None)(self.progress(step, token)), timeout)

    @staticmethod
    def work(client, step, stage=None, timeout=30):
        def probe():
            work = client.state().get("work")
            if work and work["id"] == step and (stage is None or work["stage"] == stage):
                return work
            return None
        return until(f"work {step} {stage or ''}", probe, timeout)

    def secrets_of(self, service: Service) -> list[str]:
        values = []
        for name in ("credential",):
            path = service.area / "state" / name
            if path.exists():
                values.append(path.read_text())
        return values + [self.token] + ([self.second_token] if self.second_token else [])

    def assert_private(self, service, *payloads):
        hidden = self.secrets_of(service)
        for payload in payloads:
            text = json.dumps(payload)
            for value in hidden:
                assert value and value not in text, "a credential or account token reached a client"

    # ---- Scenarios ----

    def scenario_turn(self):
        """Mike proposes commands in a real turn; the service runs each after a
        client relays approval; Mike answers from the output. MIKE_E2E_MULTI=1
        asks for pwd, ls and ps as three separate steps."""
        multi = os.environ.get("MIKE_E2E_MULTI") == "1"
        service = self.service("turn")
        try:
            desktop = service.client("desktop")
            self.paired(desktop)
            device = self.device_of()
            assert "terminal" in device["capabilities"], device
            marker = "mike-service-" + secrets.token_hex(4)
            goal = ("On my connected desktop, run pwd, then ls, then ps as three separate terminal commands with working "
                    "directory /tmp. Report the working directory, one directory entry, and one process name from their "
                    "actual output. " if multi else
                    "On my connected desktop, run the terminal command pwd with working directory /tmp, then tell me the "
                    "working directory. ")
            asked = api("mike", {"_class": "Ask", "text": goal + "This is terminal task " + marker + "."}, self.token)
            assert asked.get("_class") == "Thinking", asked
            turn = asked["turn_id"]
            seen = []

            def proposed():
                work = desktop.state().get("work")
                if work and work["stage"] == "awaiting_approval" and work["id"] not in seen:
                    return work
                stage = api("mike", {"_class": "Progress", "turn_id": turn}, self.token)
                assert stage.get("stage") != "failed", stage
                return None

            for command in (["pwd", "ls", "ps"] if multi else ["pwd"]):
                work = until("Mike's " + command + " proposal", proposed, 150, 1)
                seen.append(work["id"])
                assert Path(work["argv"][0]).name == command and work["cwd"] == "/tmp", work
                desktop.claim(work["approval_id"])
                assert desktop.answer(work["approval_id"], "approve")["stage"] == "running"
                result = self.finished(work["id"])
                assert result["result"]["state"] == "completed" and result["result"]["stdout"], result
                if command == "pwd":
                    assert result["result"]["stdout"].strip() == "/tmp", result

            def answered():
                stage = api("mike", {"_class": "Progress", "turn_id": turn}, self.token)
                assert stage.get("stage") != "failed", stage
                return stage if stage.get("stage") == "done" else None

            final = until("Mike's answer", answered, 150, 1)
            assert "/tmp" in final.get("reply", ""), final
            self.assert_private(service, desktop.state())
            return "Mike proposed " + ", ".join(["pwd", "ls", "ps"] if multi else ["pwd"]) + \
                "; the service ran each after client approval; Mike answered from /tmp"
        finally:
            service.stop()

    def scenario_file_turn(self):
        """A real Mike turn asks this computer to find and open a file: Mike's
        own confirmation, then the local folder/search and exact-file
        approvals, then Mike's reply about the outcome."""
        service = self.service("file-turn")
        try:
            desktop = service.client("desktop")
            self.paired(desktop)
            device = self.device_of()["device_id"]
            api("mike-interface", {"_class": "SetDefaultDevice", "device_id": device}, self.token)
            filename = "mike-e2e-" + secrets.token_hex(4) + ".txt"
            folder = service.area / "documents"
            folder.mkdir()
            (folder / filename).write_text("fixture")
            asked = api("mike", {"_class": "Ask", "text": "Please use this connected desktop to find and open the local file named "
                                 + filename + "."}, self.token)
            assert asked.get("_class") == "Thinking", asked
            turn = asked["turn_id"]

            def next_step():
                work = desktop.state().get("work")
                if work and work["stage"] == "awaiting_approval":
                    return ("work", work)
                stage = api("mike", {"_class": "Progress", "turn_id": turn}, self.token)
                assert stage.get("stage") != "failed", stage
                if stage.get("stage") == "confirm":
                    return ("confirm", stage)
                return None

            kind, value = until("Mike's file request", next_step, 150, 1)
            if kind == "confirm":
                api("mike", {"_class": "Confirm", "turn_id": turn, "yes": True}, self.token)
                kind, value = until("the local file task", next_step, 150, 1)
            assert kind == "work" and value["kind"] == "file", value
            found = desktop.answer(value["approval_id"], "approve", folder=str(folder), query=filename[:-4])
            assert found["stage"] == "file_confirm" and found["file"] == str(folder / filename), found
            desktop.answer(found["approval_id"], "approve")
            result = self.finished(value["id"])
            assert result["result"]["outcome"] == "opened", result
            assert (service.area / "opened.log").read_text() == str(folder / filename) + "\n"

            def answered():
                stage = api("mike", {"_class": "Progress", "turn_id": turn}, self.token)
                assert stage.get("stage") != "failed", stage
                return stage if stage.get("stage") == "done" else None

            final = until("Mike's answer", answered, 150, 1)
            assert "open" in final.get("reply", "").lower(), final
            return "Mike's real turn: action confirmation, local folder search and exact-file approval, opened, Mike replied"
        finally:
            service.stop()

    def scenario_window_absent(self):
        """Close/reopen: the service keeps pairing, presence and task receipt;
        an approval-required step waits, unexecuted, until a client returns."""
        service = self.service("absent")
        try:
            desktop = service.client("desktop")
            self.paired(desktop)
            device = self.device_of()["device_id"]
            desktop.detach()
            observer = service.client("observer", approvals=False)
            observer.attach()
            marker = service.area / "ran-while-absent"
            step = self.step(["touch", str(marker)], device)
            work = self.work(observer, step, "awaiting_approval")
            assert work["approval_id"] == "", "a client without approvals saw an approval id"
            time.sleep(6)
            listed = api("mike-interface", {"_class": "MyDevices"}, self.token)
            online = [one for one in listed["items"] if one["device_id"] == device][0]
            assert online["online"], "presence lapsed while no window was open"
            assert not marker.exists(), "the step ran without an approving client"
            assert self.progress(step)["state"] == "pending"
            assert observer.state()["approval_clients"] == 0
            reopened = service.client("desktop")
            reopened.attach(self.token, "auto")
            again = self.work(reopened, step, "awaiting_approval")
            reopened.answer(again["approval_id"], "approve")
            done = self.finished(step)
            assert done["result"]["state"] == "completed" and marker.exists(), done
            # Closing the approving window does not stop approved work.
            later = self.step(["sh", "-c", "sleep 2; echo finished"], device)
            work = self.work(reopened, later, "awaiting_approval")
            reopened.answer(work["approval_id"], "approve")
            reopened.detach()
            done = self.finished(later)
            assert done["result"]["state"] == "completed" and done["result"]["stdout"] == "finished\n", done
            return ("with no window the step was claimed and waited (presence stayed online, nothing ran); a reopened "
                    "client approved it; an approved command finished after its window detached")
        finally:
            service.stop()

    def scenario_two_clients(self):
        """Two approval-capable clients: one claim, no cross-talk, one execution;
        a second service start does not create a second executor."""
        service = self.service("two")
        try:
            first, second = service.client("desktop"), service.client("cli")
            self.paired(first)
            second.attach(self.token, "auto")
            device = self.device_of()["device_id"]
            duplicate = subprocess.run([CODE, "run", "main.code"], cwd=ROOT / "service", env=service.env,
                                       capture_output=True, text=True, timeout=30)
            assert duplicate.returncode != 0 and "already running" in duplicate.stdout + duplicate.stderr, duplicate
            listed = api("mike-interface", {"_class": "MyDevices"}, self.token)["items"]
            assert len([one for one in listed if one["device_id"] not in self.known]) == 1, listed
            counter = service.area / "count"
            step = self.step(["sh", "-c", "echo once >> \"$0\"", str(counter)], device)
            work = self.work(first, step, "awaiting_approval")
            assert self.work(second, step)["approval_id"] == work["approval_id"]
            first.claim(work["approval_id"])
            for attempt in (lambda: second.claim(work["approval_id"]), lambda: second.answer(work["approval_id"], "approve")):
                try:
                    attempt()
                    raise AssertionError("a second client took over a claimed approval")
                except ServiceError as error:
                    assert error.error == "claimed_elsewhere", error
            assert second.state()["work"]["claim"] == "other"
            first.answer(work["approval_id"], "approve")
            try:
                first.answer(work["approval_id"], "approve")
                raise AssertionError("a repeated answer was accepted")
            except ServiceError as error:
                assert error.error == "already_decided", error
            done = self.finished(step)
            assert done["result"]["state"] == "completed", done
            time.sleep(3)
            assert counter.read_text() == "once\n", counter.read_text()
            self.assert_private(service, first.state(), second.state())
            return "one claim, claimed_elsewhere/already_decided for the rest, one execution, duplicate start refused, one device"
        finally:
            service.stop()

    def scenario_stop_and_decline(self):
        service = self.service("stop")
        try:
            desktop = service.client("desktop")
            self.paired(desktop)
            device = self.device_of()["device_id"]
            declined = self.step(["touch", str(service.area / "declined")], device)
            work = self.work(desktop, declined, "awaiting_approval")
            desktop.answer(work["approval_id"], "decline")
            result = self.finished(declined)
            assert result["result"]["state"] == "denied" and not (service.area / "declined").exists(), result
            step = self.step(["sleep", "29.159"], device)
            work = self.work(desktop, step, "awaiting_approval")
            desktop.answer(work["approval_id"], "approve")
            self.work(desktop, step, "running")
            until("the command to start", lambda: alive("^sleep 29.159$"), 10, 0.1)
            began = time.monotonic()
            desktop.stop(step)
            result = self.finished(step, timeout=20)
            assert result["result"]["state"] == "cancelled" and time.monotonic() - began < 10, result
            until("the command to end", lambda: not alive("^sleep 29.159$"), 5, 0.1)
            return "decline reported denied with nothing run; Stop ended the running command and Mike received cancelled"
        finally:
            service.stop()

    def scenario_file(self):
        service = self.service("file")
        try:
            desktop = service.client("desktop")
            self.paired(desktop)
            device = self.device_of()["device_id"]
            folder = service.area / "chosen"
            (folder / "inside").mkdir(parents=True)
            target = folder / "inside" / "invoice-159.txt"
            target.write_text("reviewed")
            started = api("mike-interface", {"_class": "RunInterfaceTask", "goal": "find and open invoice-159", "device_id": device}, self.token)
            assert started.get("_class") == "InvocationAccepted", started
            work = self.work(desktop, started["id"], "awaiting_approval")
            assert work["kind"] == "file" and work["goal"] == "find and open invoice-159", work
            found = desktop.answer(work["approval_id"], "approve", folder=str(folder), query="invoice-159")
            assert found["stage"] == "file_confirm" and found["file"] == str(target), found
            confirm = self.work(desktop, started["id"], "file_confirm")
            assert confirm["approval_id"] != work["approval_id"]
            assert not (service.area / "opened.log").exists(), "opened before the exact file was approved"
            desktop.answer(confirm["approval_id"], "approve")
            result = self.finished(started["id"])
            assert result["result"] == {"_class": "InterfaceTaskResult", "outcome": "opened", "detail": str(target)} or \
                (result["result"]["outcome"] == "opened" and result["result"]["detail"] == str(target)), result
            assert (service.area / "opened.log").read_text() == str(target) + "\n"
            return "guided file: folder+query search, exact-file approval, descriptor-checked open, outcome opened"
        finally:
            service.stop()

    def scenario_account_switch(self):
        if not self.second_token:
            return None
        service = self.service("switch")
        try:
            desktop = service.client("desktop")
            self.paired(desktop)
            device = self.device_of()["device_id"]
            watcher = service.client("watcher", approvals=False)
            watcher.attach()
            step = self.step(["sleep", "28.159"], device)
            work = self.work(desktop, step, "awaiting_approval")
            desktop.answer(work["approval_id"], "approve")
            self.work(desktop, step, "running")
            until("the command to start", lambda: alive("^sleep 28.159$"), 10, 0.1)
            try:
                service.client("cli").attach(self.second_token, "auto")
                raise AssertionError("a non-explicit attach switched accounts")
            except ServiceError as error:
                assert error.error == "account_mismatch", error
            switcher = service.client("desktop")
            state = switcher.attach(self.second_token, "switch")
            assert state["account"] == self.second_email and state["activity"] == [] and state["work"] is None, state
            result = self.finished(step)
            assert result["result"]["state"] == "cancelled" and "account" in result["result"]["stderr"], result
            assert not alive("^sleep 28.159$"), "account 1's command kept running"
            for old in (desktop, watcher):
                try:
                    old.state()
                    raise AssertionError("an old-account session survived the switch")
                except ServiceError as error:
                    assert error.error == "session_ended" and error.body["reason"] == "account_changed", error
            foreign = self.progress(step, self.second_token)
            assert foreign.get("_class") == "Unknown", foreign
            second_device = self.device_of(self.second_token)
            assert second_device["online"]
            self.assert_private(service, state)
            return "switch stopped account 1's command, reported cancelled to account 1, ended its sessions; account 2 sees none of it"
        finally:
            service.stop()

    def scenario_revoke(self):
        service = self.service("revoke")
        try:
            desktop = service.client("desktop")
            self.paired(desktop)
            device = self.device_of()["device_id"]
            step = self.step(["sleep", "27.159"], device)
            work = self.work(desktop, step, "awaiting_approval")
            desktop.answer(work["approval_id"], "approve")
            self.work(desktop, step, "running")
            until("the command to start", lambda: alive("^sleep 27.159$"), 10, 0.1)
            revoked = api("mike-interface", {"_class": "RevokeDevice", "device_id": device}, self.token)
            assert revoked.get("_class") == "DeviceRevoked", revoked

            def ended():
                try:
                    desktop.state()
                    return None
                except ServiceError as error:
                    return error.body if error.error == "session_ended" else None

            body = until("revocation to reach the service", ended, 20)
            assert body["reason"] == "device_revoked", body
            state = desktop.attach()
            assert state["connection"] == "revoked" and not state["paired"] and state["activity"] == [], state
            assert not (service.area / "state/credential").exists(), "the revoked credential stayed on disk"
            result = self.finished(step)
            assert result["result"]["state"] == "unknown", result
            until("the command to end", lambda: not alive("^sleep 27.159$"), 5, 0.1)
            try:
                desktop.attach(self.token, "auto")
                raise AssertionError("revocation was undone without an explicit connect")
            except ServiceError as error:
                assert error.error == "connect_required", error
            refused = api("mike-interface", {"_class": "RunTerminalStep", "argv": ["pwd"], "cwd": "/tmp", "device_id": device}, self.token)
            assert refused.get("_class") != "InvocationAccepted", refused
            return "website revocation stopped the running command, deleted the pairing, ended sessions; re-pairing needs switch"
        finally:
            service.stop()

    def scenario_sign_out(self):
        service = self.service("signout")
        try:
            desktop = service.client("desktop")
            self.paired(desktop)
            device = self.device_of()["device_id"]
            step = self.step(["touch", str(service.area / "never")], device)
            self.work(desktop, step, "awaiting_approval")
            other = service.client("cli")
            other.attach()
            other.sign_out()
            result = self.finished(step)
            assert result["result"]["state"] == "cancelled" and "signed out" in result["result"]["stderr"], result
            assert not (service.area / "never").exists()
            try:
                desktop.state()
                raise AssertionError("a session survived sign-out")
            except ServiceError as error:
                assert error.body.get("reason") == "signed_out", error
            state = desktop.attach(self.token, "auto")
            assert state["connection"] == "connected", state
            assert self.device_of()["device_id"] == device, "signing in again paired a second device"
            revoked = desktop.revoke()
            assert revoked["revoked"] and revoked["confirmed_by_gateway"], revoked
            listed = api("mike-interface", {"_class": "MyDevices"}, self.token)
            assert not [one for one in listed["items"] if one["device_id"] == device and one.get("active")], listed
            return "sign-out cancelled the pending step for every client and kept the pairing; client revoke ended it at the gateway"
        finally:
            service.stop()

    def scenario_crash(self):
        """A service killed mid-command restarts, reports the outcome as
        unknown once a client reconnects, and never repeats the command."""
        service = self.service("crash")
        try:
            desktop = service.client("desktop")
            self.paired(desktop)
            device = self.device_of()["device_id"]
            counter = service.area / "count"
            step = self.step(["sh", "-c", "echo once >> \"$0\"; sleep 26.159", str(counter)], device)
            work = self.work(desktop, step, "awaiting_approval")
            desktop.answer(work["approval_id"], "approve")
            until("command start", counter.exists, 10, 0.1)
            service.kill()
            # Outside systemd the orphaned worker is still bounded by its own
            # 60 s limit; under the unit, systemd stops the whole cgroup.
            subprocess.run(["pkill", "-TERM", "-f", "^sleep 26.159$"], capture_output=True)
            service.start()
            assert self.progress(step)["state"] == "pending"
            restarted = service.client("desktop")
            state = restarted.attach()
            assert state["connection"] == "signed_out", state
            restarted.attach(self.token, "auto")
            result = self.finished(step, timeout=30)
            assert result["result"]["state"] == "failed" and "unknown" in result["result"]["stderr"], result
            time.sleep(3)
            assert counter.read_text() == "once\n", "the interrupted command was replayed"
            assert restarted.state()["work"] is None
            return "after kill -9 and restart the step was reported failed/unknown on reconnect and not replayed"
        finally:
            service.stop()

    def retire(self):
        """Revoke every device a scenario paired, so the next one starts clean."""
        for token in filter(None, (self.token, self.second_token)):
            for device in self.devices(token) - self.known:
                api("mike-interface", {"_class": "RevokeDevice", "device_id": device}, token)
                self.known.add(device)

    def cleanup(self):
        self.retire()
        self.temporary.cleanup()


def main():
    run = Run()
    only = set(filter(None, os.environ.get("MIKE_E2E_ONLY", "").split(",")))
    order = ["two_clients", "window_absent", "stop_and_decline", "file", "sign_out", "revoke", "account_switch", "crash",
             "turn", "file_turn"]
    failed = False
    try:
        for name in order:
            if only and name not in only:
                continue
            began = time.monotonic()
            try:
                detail = getattr(run, "scenario_" + name)()
                verdict = "SKIP" if detail is None else "PASS"
                detail = detail or "needs E2E_SECOND_EMAIL/E2E_SECOND_PASSWORD"
            except Exception as error:
                failed = True
                verdict, detail = "FAIL", f"{error}\n{traceback.format_exc(limit=3)}"
            finally:
                run.retire()
            print(f"{verdict} {name} ({time.monotonic() - began:.1f}s): {detail}", flush=True)
    finally:
        run.cleanup()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
