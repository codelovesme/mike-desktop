#!/usr/bin/env python3
"""Check the shared service's systemd user-unit lifecycle from a staged package
(`tools/package.py --service-only`).

Installs a uniquely named, *not enabled* user unit pointing at BUNDLE with a
scratch environment file (state, runtime dir, host, Code), then checks:
start/status, no graphical libraries in the service, duplicate start refused
while the unit runs, crash -> systemd restart with the whole cgroup (including
a running command) stopped and the step reported as unknown, stop, and a
failing start reported through status/journal. The unit is removed at the end.

  python3 tools/service-systemd-check.py BUNDLE
  (E2E_EMAIL/E2E_PASSWORD enable the crash-with-running-command check)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

UNIT = "mike-service-check-" + secrets.token_hex(3) + ".service"
os.environ["MIKE_SERVICE_UNIT"] = UNIT
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "client"))
import mike_service  # noqa: E402

e2e_path = ROOT / "tools/service-e2e.py"
CODE = os.environ.get("CODE_BIN", "/home/cdlvsm/git/codelovesme/code/target/release/code")
HOST = os.environ.get("EUGLENA_HOST_URL", "http://127.0.0.1:8923").rstrip("/")


def sh(*args, check=False, env=None, timeout=60):
    done = subprocess.run(list(args), capture_output=True, text=True, env=env, timeout=timeout)
    if check and done.returncode != 0:
        raise AssertionError(f"{args}: {done.returncode} {done.stdout} {done.stderr}")
    return done


def show(prop):
    return sh("systemctl", "--user", "show", UNIT, "-p", prop, "--value").stdout.strip()


def until(label, probe, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("timed out: " + label)


def main():
    bundle = Path(sys.argv[1]).resolve()
    control = bundle / "mike-service"
    area = Path(tempfile.mkdtemp(prefix="mike-systemd-"))
    run_dir = area / "run"
    os.environ["MIKE_SERVICE_RUNTIME_DIR"] = str(run_dir)
    environment = area / "environment"
    environment.write_text(
        f"MIKE_SERVICE_STATE_DIR={area / 'state'}\nMIKE_SERVICE_RUNTIME_DIR={run_dir}\n"
        f"MIKE_SERVICE_HOST_URL={HOST}\nMIKE_SERVICE_CODE_BIN={CODE}\n"
    )
    results = []

    def record(name, detail):
        results.append(name)
        print("PASS", name + ":", detail, flush=True)

    try:
        installed = sh(str(control), "install-unit", "--environment-file", str(environment), "--no-enable", check=True)
        unit_file = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd/user" / UNIT
        text = unit_file.read_text()
        assert f'ExecStart="{bundle}/mike-service" run' in text and "Restart=on-failure" in text, text
        assert "NoNewPrivileges=yes" in text and "UMask=0077" in text and "RestartPreventExitStatus=1" in text, text
        assert show("UnitFileState") in ("disabled", "linked", ""), show("UnitFileState")
        record("install", installed.stdout.strip().splitlines()[0])

        sh(str(control), "start", check=True)
        assert show("ActiveState") == "active", show("ActiveState")
        status = sh(str(control), "status")
        assert status.returncode == 0 and "service: running" in status.stdout and "ActiveState=active" in status.stdout, status.stdout
        assert "linger:" in status.stdout, status.stdout
        record("start/status", " | ".join(line for line in status.stdout.splitlines() if line.startswith(("unit:", "linger:"))))

        pid = int(show("MainPID"))
        maps = Path(f"/proc/{pid}/maps").read_text()
        assert not any(name in maps for name in ("libgtk", "libwebkit", "libgdk", "libX11", "libwayland")), "graphical library in service"
        minimal = area / "minimal"
        minimal.mkdir()
        bare = {"HOME": str(area), "PATH": "/usr/bin:/bin", "MIKE_SERVICE_STATE_DIR": str(minimal / "state"),
                "MIKE_SERVICE_RUNTIME_DIR": str(minimal / "run"), "MIKE_SERVICE_HOST_URL": HOST, "MIKE_SERVICE_CODE_BIN": CODE}
        headless = subprocess.Popen([str(control), "run"], env=bare, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            until("headless service", lambda: mike_service.reachable(minimal / "run"), 20)
        finally:
            headless.terminate()
            headless.wait(timeout=10)
            headless.stdout.close()
        record("no graphical dependency", f"unit process maps no GTK/WebKit/X11/Wayland libraries; env -i (HOME, PATH only) instance served protocol 1")

        duplicate = sh(str(control), "run", env=dict(os.environ, **{line.split("=", 1)[0]: line.split("=", 1)[1]
                                                                 for line in environment.read_text().splitlines()}))
        assert duplicate.returncode != 0 and "already running" in duplicate.stdout + duplicate.stderr, duplicate
        assert show("ActiveState") == "active"
        record("duplicate start", "a manual second start exited %d: already running; the unit kept serving" % duplicate.returncode)

        detail = "no account given: crash restart only"
        token = step = ""
        if os.environ.get("E2E_EMAIL"):
            spec = __import__("importlib.util").util.spec_from_file_location("service_e2e", e2e_path)
            e2e = __import__("importlib.util").util.module_from_spec(spec)
            spec.loader.exec_module(e2e)
            token = e2e.Run.sign_in(os.environ["E2E_EMAIL"], os.environ["E2E_PASSWORD"])
            known = e2e.Run.devices(token)
            client = mike_service.MikeService("check", directory=run_dir)
            client.attach(token, "auto")
            device = [one for one in e2e.api("mike-interface", {"_class": "MyDevices"}, token)["items"]
                      if one["device_id"] not in known][0]["device_id"]
            started = e2e.api("mike-interface", {"_class": "RunTerminalStep", "argv": ["sleep", "25.159"], "cwd": "/tmp", "device_id": device}, token)
            step = started["id"]
            work = until("claimed step", lambda: (client.state().get("work") or {}).get("approval_id"))
            client.answer(work, "approve")
            until("command", lambda: e2e.alive("^sleep 25.159$"), 10)
        restarts = int(show("NRestarts") or 0)
        sh("kill", "-9", str(pid), check=True)
        until("systemd restart", lambda: int(show("NRestarts") or 0) > restarts and mike_service.reachable(run_dir), 40)
        assert int(show("MainPID")) != pid
        if token:
            assert not e2e.alive("^sleep 25.159$"), "the command outlived the crashed unit"
            client = mike_service.MikeService("check", directory=run_dir)
            client.attach(token, "auto")
            result = until("recovery report", lambda: (lambda p: p if p.get("state") == "completed" else None)(
                e2e.api("mike-interface", {"_class": "InvocationProgress", "id": step}, token)), 30)
            assert result["result"]["state"] == "failed" and "unknown" in result["result"]["stderr"], result
            for one in e2e.Run.devices(token) - known:
                e2e.api("mike-interface", {"_class": "RevokeDevice", "device_id": one}, token)
            detail = "the cgroup's running command was stopped with the unit; after restart Mike got failed/unknown, no replay"
        record("crash restart", f"kill -9 -> NRestarts {restarts} -> {show('NRestarts')}; " + detail)

        sh(str(control), "stop", check=True)
        assert show("ActiveState") in ("inactive", "failed") and not mike_service.reachable(run_dir)
        stopped = sh(str(control), "status")
        assert stopped.returncode == 3 and "not running" in stopped.stdout, stopped.stdout
        record("stop", "unit " + show("ActiveState") + "; status exit 3, not running")

        environment.write_text(environment.read_text().replace(f"MIKE_SERVICE_HOST_URL={HOST}", "MIKE_SERVICE_HOST_URL=https://evil.test"))
        sh("systemctl", "--user", "reset-failed", UNIT)
        sh(str(control), "start")
        until("failed start", lambda: show("ActiveState") == "failed", 30)
        assert show("Result") == "exit-code" and show("NRestarts") == "0", (show("Result"), show("NRestarts"))
        report = sh(str(control), "status").stdout
        assert "not started: MIKE_SERVICE_HOST_URL" in report, report
        record("failure reporting", next(line for line in report.splitlines() if "not started" in line).strip())
    finally:
        sh("systemctl", "--user", "stop", UNIT)
        sh(str(control), "uninstall-unit")
        sh("systemctl", "--user", "reset-failed", UNIT)
        subprocess.run(["rm", "-rf", "--", str(area)])
    print(f"{len(results)} checks passed; unit {UNIT} removed")


if __name__ == "__main__":
    main()
