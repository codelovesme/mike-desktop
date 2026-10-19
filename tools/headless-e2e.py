#!/usr/bin/env python3
"""Ticket 160 end to end: Mike web → a headless server's Mike service.

Runs against a scratch stack (my-euglena-apps/tools/mike-scratch-stack.py up):
its own auth/Mike/gateway builds, keys, databases and push recorder. The
service runs from this checkout with `env -i` (no display, no session bus),
through a local TCP relay the run can cut to stand in for the network.
Model turns use the real local model through the real Mike page (Chromium,
my-euglena-apps/tools/browser/mike-headless.mjs); everything else uses the
same particles the page and the model send.

  STACK_DIR         default /var/tmp/m160/stack
  E2E_AREA          default /var/tmp/m160/e2e (state, runtime, project; removed)
  MIKE_SERVICE_CODE_BIN   Code for the service (default CDLVSM's 2.14.5)
  MIKE_E2E_ONLY=a,b       run only these scenarios (setup always runs)

Prints one line per check and a summary; never prints a credential, token
or code it read from private files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
APPS = ROOT.parent / "my-euglena-apps"
STACK = json.loads((Path(os.environ.get("STACK_DIR", "/var/tmp/m160/stack")) / "stack.json").read_text())
WEB = STACK["web"]
AREA = Path(os.environ.get("E2E_AREA", "/var/tmp/m160/e2e"))
CODE = os.environ.get("MIKE_SERVICE_CODE_BIN", str(Path.home() / ".local/share/cdlvsm/packages/code/v2.14.5/code"))
ONLY = {one for one in os.environ.get("MIKE_E2E_ONLY", "").split(",") if one}
sys.path.insert(0, str(ROOT / "client"))
import mike_service  # noqa: E402

SERVER = "build-server-m160"
PASSWORD = "QaProbe!2026"
results: list[tuple[str, bool, str]] = []
secrets_seen: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)
    return ok


def post(url: str, payload: dict, timeout: float = 30) -> dict:
    request = Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json"})
    with urlopen(request, timeout=timeout) as answer:
        return json.loads(answer.read())


def api(app: str, particle: dict, token: str | None = None) -> dict:
    envelope = {"_class": "Impulse", "token": token, "particle": particle} if token else {"_class": "Open", "particle": particle}
    return post(f"{WEB}/api/{app}", envelope)


def until(label: str, probe, timeout: float = 60, every: float = 0.5):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = probe()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f"timed out waiting for {label} (last: {str(last)[:300]})")


def mongo(script: str) -> str:
    uri = ""
    for line in (APPS / "mike-api/.env").read_text().splitlines():
        if line.startswith("MONGO_URI="):
            uri = line.split("=", 1)[1]
    done = subprocess.run(["docker", "exec", "mongodb", "mongosh", uri, "--quiet", "--eval", script],
                          capture_output=True, text=True, timeout=60)
    return done.stdout.strip()


def account(name: str) -> tuple[str, str]:
    email = f"{name}-{int(time.time())}@example.invalid"
    made = api("auth", {"_class": "SignUp", "email": email, "password": PASSWORD})
    assert made.get("_class") == "SignedUp", made
    code = mongo(f'const r=db.getSiblingDB("auth_m160").state.findOne({{_id:"verify_{email}"}}); print(r ? r.value.code : "")')
    proved = api("auth", {"_class": "VerifyEmail", "email": email, "code": code})
    assert proved.get("_class") == "Verified", proved
    signed = api("auth", {"_class": "Authenticate", "email": email, "password": PASSWORD})
    assert signed.get("_class") == "SignedIn", signed
    secrets_seen.append(signed["token"])
    return email, signed["token"]


def browser(phase: str, **fields) -> dict:
    done = subprocess.run(["node", "mike-headless.mjs", phase, json.dumps({"base": WEB, **fields})],
                          cwd=APPS / "tools/browser", capture_output=True, text=True, timeout=900)
    try:
        return json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ok": False, "error": (done.stdout + done.stderr)[-800:]}


class Relay:
    """127.0.0.1:port → the shell. `cut` drops the listener and every open
    connection: to the service, the network is gone."""

    def __init__(self, target: int):
        self.target = target
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.open = True
        self.connections: list[socket.socket] = []
        self.listener.listen(32)
        threading.Thread(target=self.accept, daemon=True).start()

    def accept(self):
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            if not self.open:
                client.close()
                continue
            try:
                upstream = socket.create_connection(("127.0.0.1", self.target), timeout=10)
            except OSError:
                client.close()
                continue
            self.connections += [client, upstream]
            for a, b in ((client, upstream), (upstream, client)):
                threading.Thread(target=self.pipe, args=(a, b), daemon=True).start()

    @staticmethod
    def pipe(a, b):
        try:
            while data := a.recv(65536):
                b.sendall(data)
        except OSError:
            pass
        for one in (a, b):
            try:
                one.close()
            except OSError:
                pass

    def cut(self):
        self.open = False
        for one in self.connections:
            try:
                one.shutdown(socket.SHUT_RDWR)
                one.close()
            except OSError:
                pass
        self.connections = []

    def restore(self):
        self.open = True


class HeadlessService:
    def __init__(self, relay: Relay):
        self.state = AREA / "state"
        self.run_dir = AREA / "run"
        self.log_path = AREA / "service.log"
        self.env = {"HOME": str(AREA / "home"), "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
                    "MIKE_SERVICE_STATE_DIR": str(self.state), "MIKE_SERVICE_RUNTIME_DIR": str(self.run_dir),
                    "MIKE_SERVICE_HOST_URL": f"http://127.0.0.1:{relay.port}", "MIKE_SERVICE_CODE_BIN": CODE}
        (AREA / "home").mkdir(parents=True, exist_ok=True)
        self.process: subprocess.Popen | None = None

    def start(self):
        log = open(self.log_path, "a")
        # env -i: no DISPLAY, WAYLAND_DISPLAY, DBUS_SESSION_BUS_ADDRESS or XDG_RUNTIME_DIR.
        self.process = subprocess.Popen(["env", "-i", *[f"{k}={v}" for k, v in self.env.items()], str(ROOT / "bin/mike-service"), "run"],
                                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        until("service", lambda: mike_service.reachable(self.run_dir) or self.process.poll() is not None, 30, 0.2)
        assert self.process.poll() is None, "service exited: " + self.log_path.read_text()[-400:]

    def ctl(self, *args: str) -> tuple[int, str]:
        done = subprocess.run(["env", "-i", *[f"{k}={v}" for k, v in self.env.items()], str(ROOT / "bin/mike-service"), *args],
                              capture_output=True, text=True, timeout=120)
        return done.returncode, done.stdout + done.stderr

    def client(self) -> mike_service.MikeService:
        one = mike_service.MikeService("e2e", approvals=True, directory=self.run_dir)
        one.attach()
        return one

    def view(self) -> dict:
        one = mike_service.MikeService("e2e-view", approvals=False, directory=self.run_dir)
        try:
            return one.attach()
        finally:
            one.detach()

    def kill(self):
        os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait(timeout=5)

    def stop(self):
        if self.process and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)


def project() -> Path:
    proj = AREA / "proj"
    (proj / "tools").mkdir(parents=True)
    (proj / "sub").mkdir()
    (proj / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (proj / "test_calc.py").write_text("import unittest\nimport calc\n\nclass T(unittest.TestCase):\n"
                                       "    def test_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n")
    # One observable write per run: a line, then (optionally) a pause.
    (proj / "tools/slow_write.py").write_text("import sys, time\nopen('effects.log', 'a').write(sys.argv[1] + '\\n')\n"
                                              "time.sleep(float(sys.argv[2]))\nprint('wrote', sys.argv[1])\n")
    (proj / "tools/noisy.py").write_text("print('x' * 100000)\n")
    (proj / "link").symlink_to("/etc")
    for command in (["git", "init", "-q"], ["git", "-c", "user.email=e2e@example.invalid", "-c", "user.name=e2e", "add", "."],
                    ["git", "-c", "user.email=e2e@example.invalid", "-c", "user.name=e2e", "commit", "-qm", "start"]):
        subprocess.run(command, cwd=proj, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "mike@example.invalid"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.name", "Mike m160"], cwd=proj, check=True)
    return proj


def step(token: str, argv: list[str], cwd: str, device: str) -> dict:
    return api("mike-interface", {"_class": "RunTerminalStep", "argv": argv, "cwd": cwd, "device_id": device}, token)


def outcome(token: str, invocation: str, timeout: float = 90) -> dict:
    def done():
        progress = api("mike-interface", {"_class": "InvocationProgress", "id": invocation}, token)
        return progress if progress.get("state") == "completed" else None
    return until("step result", done, timeout, 1)["result"]


def effects(proj: Path, mark: str) -> int:
    log = proj / "effects.log"
    return log.read_text().splitlines().count(mark) if log.exists() else 0


def scenario(name: str):
    def wrap(fn):
        def run(*args):
            if ONLY and name not in ONLY:
                return
            try:
                fn(*args)
            except Exception as error:  # noqa: BLE001
                check(name, False, f"{type(error).__name__}: {error}")
                traceback.print_exc()
        run.__name__ = name
        return run
    return wrap


def main() -> int:
    if AREA.exists():
        shutil.rmtree(AREA)
    AREA.mkdir(parents=True, mode=0o700)
    proj = project()
    relay = Relay(int(WEB.rsplit(":", 1)[1]))
    service = HeadlessService(relay)
    ada, ada_token = account("ada")
    bob, bob_token = account("bob")
    ctx: dict = {"proj": proj, "relay": relay, "service": service, "ada": ada, "ada_token": ada_token,
                 "bob": bob, "bob_token": bob_token}
    try:
        setup(ctx)
        for one in (web_turn, input_by_notification, out_of_scope, local_enforcement, cross_owner, answers,
                    bounds, network_loss, crash_and_duplicate, offline_target, expiry, web_revocation, hygiene, unenroll):
            one(ctx)
    finally:
        service.stop()
        relay.cut()
    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (f"; failed: {', '.join(failed)}" if failed else ""))
    if not os.environ.get("E2E_KEEP"):
        shutil.rmtree(AREA, ignore_errors=True)
    return 1 if failed else 0


def setup(ctx):
    service: HeadlessService = ctx["service"]
    service.start()
    pid = service.process.pid  # env and the launcher exec: this is the Code process
    maps = Path(f"/proc/{pid}/maps").read_text()
    gui = [lib for lib in ("gtk", "webkit", "libX11", "wayland", "gdk") if lib in maps.lower()]
    check("headless:no_display_libraries", not gui, f"code pid {pid}; none of gtk/webkit/X11/wayland/gdk mapped")
    environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    check("headless:no_display_environment", not any(v.startswith((b"DISPLAY=", b"WAYLAND_DISPLAY=", b"DBUS_SESSION_BUS_ADDRESS=")) for v in environ))
    listening = subprocess.run(["ss", "-ltnpH"], capture_output=True, text=True).stdout
    mine = [line.split()[3] for line in listening.splitlines() if f"pid={pid}," in line]
    check("headless:loopback_only_listener", mine and all(addr.startswith("127.0.0.1:") for addr in mine), ", ".join(mine))

    # A code confirmed by the wrong account is revoked at once.
    rc, text = service.ctl("enroll", "--account", ctx["ada"], "--name", SERVER, "--no-wait")
    code = next((line.strip() for line in text.splitlines() if line.startswith("    ")), "")
    check("enroll:code_printed", rc == 0 and len(code) == 9, f"rc={rc}")
    wrong = api("mike-interface", {"_class": "ConfirmEnrollment", "user_code": code}, ctx["bob_token"])
    check("enroll:other_account_can_type_code", wrong.get("_class") == "DeviceEnrolled")
    refused = until("refusal", lambda: (service.view().get("enrollment") or {}).get("state") == "refused" and service.view(), 20)
    bobs = api("mike-interface", {"_class": "MyDevices"}, ctx["bob_token"])["items"]
    check("enroll:unexpected_account_revoked", not refused["paired"] and bobs and not bobs[0]["active"], refused["enrollment"]["detail"])

    rc, text = service.ctl("enroll", "--account", ctx["ada"], "--name", SERVER, "--no-wait")
    code = next((line.strip() for line in text.splitlines() if line.startswith("    ")), "")
    shown = browser("enroll", email=ctx["ada"], password=PASSWORD, code=code)
    check("enroll:confirmed_in_mike_web", shown.get("ok"), shown.get("notice") or shown.get("error", ""))
    state = until("enrolled", lambda: service.view().get("connection") == "connected" and service.view(), 30)
    check("enroll:headless_device_online", state["headless"] and state["capabilities"] == ["terminal"], f"account={state['account']}")
    devices = api("mike-interface", {"_class": "MyDevices"}, ctx["ada_token"])["items"]
    server = next(one for one in devices if one["name"] == SERVER and one["active"])
    ctx["device"] = server["device_id"]
    check("enroll:shown_as_server", server["headless"] and server["online"] and server["capabilities"] == ["terminal"])
    credential = (service.state / "credential").read_text()
    secrets_seen.append(credential)
    modes = {name: oct((service.state / name).stat().st_mode & 0o777) for name in ("credential", "owner", "device_id", "mode")}
    check("enroll:private_files", all(mode == "0o600" for mode in modes.values()) and oct(service.state.stat().st_mode & 0o777) == "0o700", str(modes))

    pairing_only = step(ctx["ada_token"], ["ls"], str(ctx["proj"]), ctx["device"])
    check("grant:pairing_alone_runs_nothing", pairing_only.get("_class") == "Rejected", pairing_only.get("reason", ""))

    proj = str(ctx["proj"])
    rc, text = service.ctl("grant", "add", "--root", proj, "--run", "ls", "--run", "cat", "--run", "git status",
                           "--run", "git log", "--run", "python3 -m unittest", "--run", "python3 tools/slow_write.py",
                           "--run", "python3 tools/noisy.py", "--run", "sleep", "--run", "m160-missing-tool",
                           "--ask", "git commit", "--minutes", "120", "--timeout", "20")
    check("grant:added_by_os_user", rc == 0 and "not installed here: m160-missing-tool" in text and "not a sandbox" in text)
    grants = until("advertised grant", lambda: next((one["grants"] for one in api("mike-interface", {"_class": "MyDevices"}, ctx["ada_token"])["items"]
                                                     if one["device_id"] == ctx["device"] and one["grants"]), None), 20)
    ctx["grant"] = grants[0]["id"]
    check("grant:advertised_to_gateway", grants[0]["root"] == proj and grants[0]["missing"] == ["m160-missing-tool"])
    panel = browser("panel", email=ctx["ada"], password=PASSWORD, topic="m160-private-topic-" + str(os.getpid()))
    check("web:device_panel_shows_server_and_grant", panel.get("ok") and any(SERVER in row and "server" in row for row in panel.get("rows", [])),
          json.dumps(panel.get("rows") or panel.get("error"))[:400])


@scenario("web_turn")
def web_turn(ctx):
    proj = ctx["proj"]
    text = (f"On my server {SERVER} (device_id {ctx['device']}), run the command `python3 -m unittest -q` in the "
            f"working directory {proj}. Tell me the exit code and end your reply with the word FINISHED160.")
    shown = browser("ask", email=ctx["ada"], password=PASSWORD, text=text, wait="FINISHED160")
    check("web:turn_reply", shown.get("ok"), (shown.get("transcript") or shown.get("error", ""))[-300:].replace("\n", " | "))
    found = mongo('const r=db.getSiblingDB("mike_interfaces_m160").state.find({_id:/^task:/}).toArray()'
                  '.map(d=>d.value).filter(v=>v.argv && v.argv.join(" ").includes("unittest")); '
                  'print(JSON.stringify(r.map(v=>({state:v.state, scope:v.scope, grant:v.grant_id, result:Object.assign({}, v.result, {exit_code: v.result ? Number(v.result.exit_code) : null})}))))')
    tasks = json.loads(found or "[]")
    ran = [one for one in tasks if one["grant"] == ctx["grant"] and one["state"] == "completed" and one["result"]["state"] == "completed"]
    check("web:step_ran_under_grant_without_local_approval", ran and ran[0]["scope"] == "run" and ran[0]["grant"] == ctx["grant"]
          and ran[0]["result"]["exit_code"] == 0 and "OK" in ran[0]["result"]["stderr"],
          json.dumps(ran[0]["result"] if ran else tasks)[:300])


@scenario("input_by_notification")
def input_by_notification(ctx):
    proj = ctx["proj"]
    (proj / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n")
    pushes = Path(STACK["pushes"])
    before = len(pushes.read_text().splitlines()) if pushes.exists() else 0
    text = (f"On my server {SERVER} (device_id {ctx['device']}), in {proj}, run exactly this command: "
            "git commit -am \"m160 add sub\" — then tell me its output and end with FINISHED161.")
    sent = browser("ask", email=ctx["ada"], password=PASSWORD, text=text, close=True)
    check("input:page_closed_after_sending", sent.get("ok"), sent.get("error", ""))
    pushed = until("push", lambda: pushes.exists() and len(pushes.read_text().splitlines()) > before and pushes.read_text().splitlines()[-1], 300, 2)
    record = json.loads(pushed)
    check("input:notification_sent_with_page_closed", record["title"] == "Mike needs your answer" and SERVER in record["message"]
          and "git" not in record["message"] and record["click"].endswith("#/mike"), json.dumps(record))
    check("input:nothing_ran_before_answer", "m160 add sub" not in subprocess.run(["git", "log", "--format=%s"], cwd=proj, capture_output=True, text=True).stdout)
    answered = browser("answer", email=ctx["ada"], password=PASSWORD, expect="git commit", yes=True)
    check("input:reopened_page_shows_question_and_yes_continues", answered.get("ok"),
          (answered.get("question") or answered.get("error", ""))[:200].replace("\n", " | "))
    log = subprocess.run(["git", "log", "--format=%s"], cwd=proj, capture_output=True, text=True).stdout.splitlines()
    check("input:observable_result", log[0] == "m160 add sub" and log.count("m160 add sub") == 1, " / ".join(log[:3]))
    after = len(pushes.read_text().splitlines())
    check("input:one_push_per_question", after == before + 1, f"{after - before} push(es)")


@scenario("out_of_scope")
def out_of_scope(ctx):
    token, device, proj = ctx["ada_token"], ctx["device"], str(ctx["proj"])
    cases = {"unlisted_command": (["rm", "-rf", "calc.py"], proj), "other_directory": (["ls"], "/tmp"),
             "sibling_prefix": (["ls"], proj + "-other"), "dotdot": (["ls"], proj + "/../"),
             "subcommand_not_granted": (["git", "push"], proj)}
    for name, (argv, cwd) in cases.items():
        answer = step(token, argv, cwd, device)
        check(f"scope:{name}_rejected_before_sending", answer.get("_class") == "Rejected", answer.get("reason", "")[:120])
    absent = step(token, ["m160-missing-tool"], proj, device)
    check("scope:absent_tool_reported", absent.get("_class") == "Rejected" and "not installed" in absent.get("reason", ""), absent.get("reason", ""))
    check("scope:calc_untouched", (ctx["proj"] / "calc.py").exists())


@scenario("local_enforcement")
def local_enforcement(ctx):
    # The gateway sees /proj/link as inside the project; the server resolves it to /etc and refuses.
    accepted = step(ctx["ada_token"], ["ls"], str(ctx["proj"] / "link"), ctx["device"])
    check("local:gateway_routed_symlinked_dir", accepted.get("_class") == "InvocationAccepted")
    result = outcome(ctx["ada_token"], accepted["id"])
    check("local:server_refused_outside_root", result["state"] == "rejected" and "resolves outside" in result["stderr"], result["stderr"][:160])


@scenario("cross_owner")
def cross_owner(ctx):
    other = step(ctx["bob_token"], ["ls"], str(ctx["proj"]), ctx["device"])
    check("owner:other_account_cannot_target_server", other.get("_class") == "InterfaceUnavailable", other.get("reason", ""))
    revoke = api("mike-interface", {"_class": "RevokeGrant", "device_id": ctx["device"], "grant_id": ctx["grant"]}, ctx["bob_token"])
    check("owner:other_account_cannot_revoke_grant", revoke.get("_class") == "Forbidden")


@scenario("answers")
def answers(ctx):
    token, device, proj, service = ctx["ada_token"], ctx["device"], str(ctx["proj"]), ctx["service"]
    asked = step(token, ["git", "commit", "--allow-empty", "-m", "m160 concurrent"], proj, device)
    question = until("question", lambda: api("mike-interface", {"_class": "InvocationProgress", "id": asked["id"]}, token).get("input"), 20)
    bob = api("mike-interface", {"_class": "AnswerTerminalInput", "id": asked["id"], "version": question["version"], "yes": True}, ctx["bob_token"])
    check("answer:other_owner_refused", bob.get("_class") == "Rejected")
    replies: list[dict] = []
    threads = [threading.Thread(target=lambda: replies.append(api("mike-interface", {"_class": "AnswerTerminalInput", "id": asked["id"],
                                                                                     "version": question["version"], "yes": True}, token)))
               for _ in range(2)]
    for one in threads:
        one.start()
    for one in threads:
        one.join()
    classes = sorted(reply.get("_class") for reply in replies)
    check("answer:concurrent_yes_counts_once", classes == ["InputAnswered", "Rejected"], str(classes))
    result = outcome(token, asked["id"])
    log = subprocess.run(["git", "log", "--format=%s"], cwd=proj, capture_output=True, text=True).stdout.splitlines()
    check("answer:ran_once", result["state"] == "completed" and log.count("m160 concurrent") == 1)

    # A second grant asks for `git tag`; it is revoked before the answer.
    rc, text = service.ctl("grant", "add", "--root", proj, "--ask", "git tag", "--minutes", "30")
    tag_grant = text.split("\n")[1].split()[0]
    until("second grant", lambda: any(g["id"] == tag_grant for one in api("mike-interface", {"_class": "MyDevices"}, token)["items"]
                                      for g in one["grants"]), 20)
    tagged = step(token, ["git", "tag", "m160"], proj, device)
    question = until("tag question", lambda: api("mike-interface", {"_class": "InvocationProgress", "id": tagged["id"]}, token).get("input"), 20)
    service.ctl("grant", "revoke", tag_grant)
    until("grant gone", lambda: not any(g["id"] == tag_grant for one in api("mike-interface", {"_class": "MyDevices"}, token)["items"]
                                        for g in one["grants"]), 20)
    late = api("mike-interface", {"_class": "AnswerTerminalInput", "id": tagged["id"], "version": question["version"], "yes": True}, token)
    tags = subprocess.run(["git", "tag"], cwd=proj, capture_output=True, text=True).stdout
    check("answer:changed_grant_cannot_be_revived", late.get("_class") == "Rejected" and "m160" not in tags, late.get("reason", ""))


@scenario("bounds")
def bounds(ctx):
    token, device, proj, service = ctx["ada_token"], ctx["device"], str(ctx["proj"]), ctx["service"]
    noisy = outcome(token, step(token, ["python3", "tools/noisy.py"], proj, device)["id"])
    check("bounds:output_bounded", len(noisy["stdout"].encode()) <= 4096 and noisy["stdout"].endswith("[output truncated]"),
          f"state={noisy['state']} bytes={len(noisy['stdout'].encode())}")
    started = time.monotonic()
    slow = outcome(token, step(token, ["sleep", "40"], proj, device)["id"])
    check("bounds:grant_timeout_enforced", slow["state"] == "timed_out" and time.monotonic() - started < 35,
          f"{slow['state']} after {time.monotonic() - started:.0f}s (grant timeout 20 s)")
    stopped = step(token, ["sleep", "15"], proj, device)
    client = service.client()
    try:
        work = until("running", lambda: (client.state().get("work") or {}).get("stage") == "running" and client.state()["work"], 20)
        time.sleep(1)  # the command itself is running, not only the worker
        client.stop(work["id"])
    finally:
        client.detach()
    result = outcome(token, stopped["id"])
    check("bounds:local_stop", result["state"] == "cancelled", result["stderr"][:100])


@scenario("network_loss")
def network_loss(ctx):
    token, device, proj, service, relay = ctx["ada_token"], ctx["device"], str(ctx["proj"]), ctx["service"], ctx["relay"]
    accepted = step(token, ["python3", "tools/slow_write.py", "net", "6"], proj, device)
    until("running", lambda: (service.view().get("work") or {}).get("stage") == "running", 20)
    relay.cut()
    time.sleep(12)
    offline = service.view()
    pending = api("mike-interface", {"_class": "InvocationProgress", "id": accepted["id"]}, token)
    check("network:result_not_invented_while_cut", pending.get("state") == "pending" and offline["connection"] == "offline",
          f"progress={pending.get('state')} connection={offline['connection']}")
    relay.restore()
    result = outcome(token, accepted["id"], 60)
    check("network:delivered_after_reconnect", result["state"] == "completed" and result["exit_code"] == 0 and effects(ctx["proj"], "net") == 1,
          f"{result['state']} exit={result['exit_code']} effects={effects(ctx['proj'], 'net')}")


@scenario("crash_and_duplicate")
def crash_and_duplicate(ctx):
    token, device, proj, service = ctx["ada_token"], ctx["device"], str(ctx["proj"]), ctx["service"]
    accepted = step(token, ["python3", "tools/slow_write.py", "crash", "4"], proj, device)
    until("effect begun", lambda: effects(ctx["proj"], "crash") == 1, 20, 0.2)
    service.kill()
    time.sleep(6)
    service.start()
    result = outcome(token, accepted["id"], 60)
    check("crash:uncertain_reported_not_success", result["state"] == "failed" and "unknown" in result["stderr"], result["stderr"][:120])
    check("crash:no_replay_after_restart", effects(ctx["proj"], "crash") == 1)
    # The gateway delivers the same step again (as after restoring its store).
    mongo(f'const d=db.getSiblingDB("mike_interfaces_m160").state; const t=d.findOne({{_id:"task:{accepted["id"]}"}}); '
          f't.value.state="queued"; t.value.created_at=Math.floor(Date.now()/1000); d.replaceOne({{_id:t._id}}, t)')
    again = outcome(token, accepted["id"], 60)
    time.sleep(2)
    check("duplicate:redelivery_not_repeated", again["state"] == "rejected" and "delivered again" in again["stderr"] and effects(ctx["proj"], "crash") == 1,
          again["stderr"][:120])


@scenario("offline_target")
def offline_target(ctx):
    token, device, proj, service = ctx["ada_token"], ctx["device"], str(ctx["proj"]), ctx["service"]
    service.stop()
    until("offline", lambda: not next(one for one in api("mike-interface", {"_class": "MyDevices"}, token)["items"] if one["device_id"] == device)["online"], 70, 2)
    refused = step(token, ["ls"], proj, device)
    check("target:offline_reported_not_retargeted", refused.get("_class") == "InterfaceUnavailable" and "offline" in refused.get("reason", ""), refused.get("reason", ""))
    service.start()
    until("online", lambda: service.view().get("connection") == "connected", 30)
    check("target:headless_identity_survives_restart", service.view()["headless"] and service.view()["grants"])


@scenario("expiry")
def expiry(ctx):
    token, device, service = ctx["ada_token"], ctx["device"], ctx["service"]
    sub = str(ctx["proj"] / "sub")
    rc, text = service.ctl("grant", "add", "--root", sub, "--run", "pwd", "--minutes", "1")
    until("short grant advertised", lambda: step(token, ["pwd"], sub, device).get("_class") == "InvocationAccepted", 20)
    time.sleep(64)
    expired = step(token, ["pwd"], sub, device)
    left = [g["root"] for g in service.view()["grants"]]
    check("expiry:expired_grant_refused_and_pruned", expired.get("_class") == "Rejected" and sub not in left, expired.get("reason", ""))


@scenario("web_revocation")
def web_revocation(ctx):
    token, device, proj, service = ctx["ada_token"], ctx["device"], str(ctx["proj"]), ctx["service"]
    running = step(token, ["sleep", "18"], proj, device)
    until("running", lambda: (service.view().get("work") or {}).get("stage") == "running", 20)
    shown = browser("revoke", email=ctx["ada"], password=PASSWORD, grant=ctx["grant"])
    check("revoke:revoked_from_mike_web", shown.get("ok"), shown.get("error", ""))
    result = outcome(token, running["id"], 40)
    check("revoke:running_command_stopped", result["state"] == "cancelled", result["state"])
    after = step(token, ["ls"], proj, device)
    check("revoke:no_further_steps", after.get("_class") == "Rejected" and not service.view()["grants"], after.get("reason", ""))


@scenario("hygiene")
def hygiene(ctx):
    service = ctx["service"]
    texts = {"service.log": service.log_path.read_text(), "state": json.dumps(service.view()),
             "pushes": Path(STACK["pushes"]).read_text() if Path(STACK["pushes"]).exists() else "",
             "status": service.ctl("status")[1], "grant list": service.ctl("grant", "list")[1]}
    leaks = [name for name, text in texts.items() for secret in secrets_seen if secret and secret in text]
    check("hygiene:no_credential_or_token_in_output", not leaks, ", ".join(leaks) or f"{len(secrets_seen)} secrets checked in {len(texts)} outputs")


@scenario("unenroll")
def unenroll(ctx):
    service = ctx["service"]
    rc, text = service.ctl("unenroll")
    devices = api("mike-interface", {"_class": "MyDevices"}, ctx["ada_token"])["items"]
    gone = next(one for one in devices if one["device_id"] == ctx["device"])
    check("unenroll:device_revoked_everywhere", rc == 0 and not gone["active"] and not service.view()["paired"]
          and not (service.state / "credential").exists(), text.strip())


if __name__ == "__main__":
    sys.exit(main())
