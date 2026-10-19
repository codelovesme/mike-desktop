#!/usr/bin/python3
"""Lifecycle and headless commands for the shared Mike service: mike-service <command>.

`run` is handled by the shell launcher (it is what the systemd user unit
starts). These commands manage that unit, or a detached instance when no
unit is installed, and report what is running. None of them needs root.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mike_service  # noqa: E402

UNIT = mike_service.UNIT


def systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)


def launcher(bundle: Path) -> Path:
    bundled = bundle / "mike-service"
    return bundled if bundled.is_file() else bundle / "bin/mike-service"


def unit_text(bundle: Path, environment_file: str | None) -> str:
    start = str(launcher(bundle))
    if any(ch in start for ch in "\"\\\n%"):
        raise SystemExit("mike-service: the bundle path cannot be written into a unit file")
    environment = environment_file or "%h/.config/mike-service/environment"
    return f"""[Unit]
Description=Mike shared local service (this computer's paired device)

[Service]
Type=simple
ExecStart="{start}" run
EnvironmentFile=-{environment}
# A crash (signal) restarts; a refused start (exit 1: another instance, a
# shared directory, a bad setting) is reported once instead of looping.
Restart=on-failure
RestartPreventExitStatus=1
RestartSec=5
UMask=0077
NoNewPrivileges=yes

[Install]
WantedBy=default.target
"""


def unit_path() -> Path:
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config / "systemd/user" / UNIT


def endpoint_pid() -> int | None:
    try:
        published = json.loads((mike_service.runtime_dir() / "endpoint").read_text())
        pid = int(published.get("pid"))
    except (OSError, ValueError, TypeError):
        return None
    try:
        if os.stat(f"/proc/{pid}").st_uid != os.getuid():
            return None
    except OSError:
        return None
    return pid


def status() -> int:
    installed = unit_path().exists() or mike_service.unit_installed()
    running = mike_service.reachable()
    print("service:", "running" if running else "not running", f"(runtime {mike_service.runtime_dir()})")
    if installed:
        shown = systemctl("show", UNIT, "-p", "ActiveState,SubState,Result,ExecMainStatus,NRestarts,UnitFileState")
        print("unit:", UNIT, " ".join(shown.stdout.split()))
        if "Result=success" not in shown.stdout:
            journal = subprocess.run(["journalctl", "--user", "-u", UNIT, "-n", "40", "--no-pager", "-o", "cat"],
                                     capture_output=True, text=True, timeout=10)
            lines = journal.stdout.splitlines()
            # The service's own reason, then systemd's verdict.
            for line in [one for one in lines if one.startswith("mike-service: ")][-3:] + lines[-2:]:
                print("  journal:", line)
    else:
        print("unit: not installed (mike-service install-unit)")
    linger = subprocess.run(["loginctl", "show-user", str(os.getuid()), "-p", "Linger", "--value"],
                            capture_output=True, text=True, timeout=10)
    if linger.returncode == 0:
        value = linger.stdout.strip()
        print("linger:", value, "- the user manager", "keeps running after logout and starts at boot"
              if value == "yes" else "stops at logout and starts at login (loginctl enable-linger to change)")
    if running:
        client = mike_service.MikeService("status", approvals=False)
        try:
            state = client.attach()
            work = state.get("work") or {}
            print("account:", state.get("account") or "(signed out)", " connection:", state.get("connection"),
                  " paired:", state.get("paired"), " headless:", state.get("headless"),
                  " clients:", state.get("clients"), " approval clients:", state.get("approval_clients"))
            print("grants:", len(state.get("grants") or []), "(mike-service grant list)")
            if work:
                print("work:", work.get("kind"), work.get("stage"), "claim:", work.get("claim"))
        except (mike_service.ServiceError, mike_service.ServiceUnavailable) as error:
            print("state: unavailable -", error)
        finally:
            client.detach()
    return 0 if running else 3


def start(bundle: Path) -> int:
    if mike_service.reachable():
        print("mike-service: already running")
        return 0
    if mike_service.unit_installed():
        started = systemctl("start", UNIT)
        if started.returncode != 0:
            print("mike-service: systemd could not start", UNIT, started.stderr.strip(), file=sys.stderr)
            return 1
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not mike_service.reachable():
            time.sleep(0.2)
    else:
        try:
            started = mike_service.ensure_running(launcher(bundle), env=dict(os.environ))
        except mike_service.ServiceUnavailable as error:
            print("mike-service: not started:", error, file=sys.stderr)
            return 1
        if not started:
            print("mike-service: did not start; see service.log in the state directory", file=sys.stderr)
            return 1
    if not mike_service.reachable():
        print("mike-service: started but not answering", file=sys.stderr)
        return 1
    print("mike-service: running")
    return 0


def stop() -> int:
    if mike_service.unit_installed() and systemctl("is-active", UNIT).stdout.strip() in ("active", "activating"):
        stopped = systemctl("stop", UNIT)
        print("mike-service: stopped" if stopped.returncode == 0 else stopped.stderr.strip())
        return stopped.returncode
    pid = endpoint_pid()
    if pid is None or not mike_service.reachable():
        print("mike-service: not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and Path(f"/proc/{pid}").exists():
        time.sleep(0.1)
    print("mike-service: stopped")
    return 0


def install_unit(bundle: Path, args: list[str]) -> int:
    # A package manager runs this without arguments; the variable lets an
    # isolated installation point its unit at a scratch environment file.
    environment, enable = os.environ.get("MIKE_SERVICE_ENVIRONMENT_FILE") or None, True
    rest = list(args)
    while rest:
        option = rest.pop(0)
        if option == "--environment-file" and rest:
            environment = rest.pop(0)
        elif option == "--no-enable":
            enable = False
        else:
            raise SystemExit("usage: mike-service install-unit [--environment-file PATH] [--no-enable]")
    path = unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text(bundle, environment))
    systemctl("daemon-reload")
    note = "(not enabled)"
    if enable:
        enabled = systemctl("enable", UNIT)
        note = "(enabled for the user manager's default target)" if enabled.returncode == 0 else enabled.stderr.strip()
    print("mike-service: installed", path, note)
    print("  start it now with: mike-service start")
    return 0


def uninstall_unit() -> int:
    systemctl("disable", "--now", UNIT)
    try:
        unit_path().unlink()
    except FileNotFoundError:
        pass
    systemctl("daemon-reload")
    print("mike-service: unit removed")
    return 0


def control_client() -> mike_service.MikeService:
    """The OS user at this terminal, attached with control capability."""
    if not mike_service.reachable():
        raise SystemExit("mike-service: not running (mike-service start)")
    client = mike_service.MikeService("mike-service control", approvals=True)
    client.attach()
    return client


def enroll(args: list[str]) -> int:
    """Pair a server without a browser: print a short code, wait for the
    person (signed in elsewhere as --account) to enter it in Mike."""
    account, name, wait = None, None, 600
    rest = list(args)
    while rest:
        option = rest.pop(0)
        if option == "--account" and rest:
            account = rest.pop(0)
        elif option == "--name" and rest:
            name = rest.pop(0)
        elif option == "--no-wait":
            wait = 0
        else:
            raise SystemExit("usage: mike-service enroll --account EMAIL [--name NAME] [--no-wait]")
    if not account:
        raise SystemExit("mike-service enroll: --account names the account that will confirm the code")
    client = control_client()
    try:
        started = client.call("/v1/enroll/start", {"account": account, **({"name": name} if name else {})})
        minutes = int(started.get("expires_in", 600)) // 60
        print(f"Enrolling this computer as \"{started['name']}\" for {started['account']}.")
        print(f"In Mike (signed in as {started['account']}), open Your devices -> Connect a server and enter:")
        print()
        print("    " + started["user_code"])
        print()
        print(f"The code expires in {minutes} minutes. A code confirmed by any other account is revoked at once.")
        print("Enrolling grants nothing: add a grant (mike-service grant add) before Mike can run anything here.")
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            time.sleep(2)
            enrollment = client.state().get("enrollment") or {}
            if enrollment.get("state") != "pending":
                print("mike-service:", enrollment.get("detail") or enrollment.get("state"))
                return 0 if enrollment.get("state") == "confirmed" else 1
        if wait:
            print("mike-service: still waiting; check `mike-service status`")
        return 0
    except mike_service.ServiceError as error:
        print("mike-service enroll:", error.body.get("message", error), file=sys.stderr)
        return 1
    finally:
        client.detach()


GRANT_USAGE = """usage:
  mike-service grant add --root DIR [--run 'CMD WORDS']... [--ask 'CMD WORDS']...
                         [--minutes N | --hours N] [--timeout SECONDS]
  mike-service grant list
  mike-service grant revoke GRANT_ID"""


def describe(grant: dict) -> str:
    runs = ", ".join(" ".join(words) for words in grant.get("run", [])) or "-"
    asks = ", ".join(" ".join(words) for words in grant.get("ask", [])) or "-"
    left = max(0, int(grant.get("seconds_left", 0)))
    text = f"{grant['id']}  {grant['root']}\n    runs: {runs}\n    asks first in Mike: {asks}\n" \
           f"    expires in {left // 3600} h {left % 3600 // 60} min, at most {grant.get('timeout_seconds')} s per command"
    if grant.get("missing"):
        text += "\n    not installed here: " + ", ".join(grant["missing"])
    return text


def grant(args: list[str]) -> int:
    if not args:
        raise SystemExit(GRANT_USAGE)
    action, rest = args[0], list(args[1:])
    client = control_client()
    try:
        if action == "list":
            grants = client.state().get("grants") or []
            for one in grants:
                print(describe(one))
            if not grants:
                print("No grants: Mike can run nothing on this computer without a local approval.")
            return 0
        if action == "revoke" and len(rest) == 1:
            client.call("/v1/grants/revoke", {"grant_id": rest[0]})
            print("mike-service: grant", rest[0], "revoked; a command running under it was stopped")
            return 0
        if action != "add":
            raise SystemExit(GRANT_USAGE)
        request: dict = {"run": [], "ask": []}
        while rest:
            option = rest.pop(0)
            if option in ("--root", "--run", "--ask", "--minutes", "--hours", "--timeout") and rest:
                value = rest.pop(0)
                if option == "--root":
                    request["root"] = os.path.abspath(value)
                elif option in ("--run", "--ask"):
                    request[option[2:]].append(value)
                elif option == "--minutes":
                    request["minutes"] = int(value)
                elif option == "--hours":
                    request["minutes"] = int(value) * 60
                else:
                    request["timeout_seconds"] = int(value)
            else:
                raise SystemExit(GRANT_USAGE)
        if "root" not in request:
            raise SystemExit(GRANT_USAGE)
        added = client.call("/v1/grants/add", request)["grant"]
        print("mike-service: granted")
        print(describe({**added, "seconds_left": added["expires_at"] - added["created_at"]}))
        print("Granted commands run unattended as this OS user, with all of its rights. The directory is where")
        print("they start, not a sandbox: a granted program can read or change anything this user can.")
        return 0
    except mike_service.ServiceError as error:
        print("mike-service grant:", error.body.get("message", error), file=sys.stderr)
        return 1
    finally:
        client.detach()


def unenroll() -> int:
    client = control_client()
    try:
        answer = client.call("/v1/device/revoke", {})
        print("mike-service: this computer's pairing was revoked"
              + ("" if answer.get("confirmed_by_gateway") else " here; the gateway could not be reached, revoke it in Mike too"))
        return 0
    except mike_service.ServiceError as error:
        print("mike-service unenroll:", error.body.get("message", error), file=sys.stderr)
        return 1
    finally:
        client.detach()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        raise SystemExit("usage: service_control.py <bundle> <command>")
    bundle, command, rest = Path(argv[0]), argv[1], argv[2:]
    if command == "status":
        return status()
    if command == "start":
        return start(bundle)
    if command == "stop":
        return stop()
    if command == "restart":
        stop()
        return start(bundle)
    if command == "install-unit":
        return install_unit(bundle, rest)
    if command == "uninstall-unit":
        return uninstall_unit()
    if command == "enroll":
        return enroll(rest)
    if command == "grant":
        return grant(rest)
    if command == "unenroll":
        return unenroll()
    print("mike-service: unknown command", command, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
