#!/usr/bin/python3
"""Lifecycle commands for the shared Mike service: mike-service <command>.

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
                  " paired:", state.get("paired"), " clients:", state.get("clients"),
                  " approval clients:", state.get("approval_clients"))
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
    elif not mike_service.ensure_running(launcher(bundle), env=dict(os.environ)):
        print("mike-service: did not start; see ~/.local/state/mike-service/service.log", file=sys.stderr)
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
    environment, enable = None, True
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
    print("mike-service: unknown command", command, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
