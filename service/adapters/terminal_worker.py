#!/usr/bin/python3
"""Run one approved terminal step for Mike and return bounded JSON evidence.

This process is launched by the trusted desktop controller, never by WebKit.
It receives a single JSON request on stdin. It cannot decide whether a step
was approved; the controller must check the job and grant before launching it.
"""

from __future__ import annotations

import ctypes
import json
import os
import selectors
import signal
import subprocess
import sys
import time


MAX_OUTPUT = 32 * 1024
MAX_SECONDS = 60
MAX_ARGV = 64
MAX_TEXT = 4096
PR_SET_NO_NEW_PRIVS = 38
_child: subprocess.Popen | None = None


def _stop_child(_signum, _frame) -> None:
    if _child is not None and _child.poll() is None:
        _kill_group(_child.pid)
    raise KeyboardInterrupt


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _no_new_privileges() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "could not disable privilege gain")


def _environment() -> dict[str, str]:
    kept = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "TERM",
            "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE",
            "XDG_CURRENT_DESKTOP", "DBUS_SESSION_BUS_ADDRESS")
    return {key: os.environ[key] for key in kept if key in os.environ}


def validate(request: object) -> tuple[list[str], str, int]:
    if not isinstance(request, dict) or set(request) != {"argv", "cwd", "timeout_seconds"}:
        raise ValueError("expected argv, cwd, and timeout_seconds")
    argv, cwd, timeout = request["argv"], request["cwd"], request["timeout_seconds"]
    if not isinstance(argv, list) or not 1 <= len(argv) <= MAX_ARGV:
        raise ValueError("argv must contain 1 to 64 strings")
    if any(not isinstance(arg, str) or not arg or len(arg) > MAX_TEXT or "\x00" in arg for arg in argv):
        raise ValueError("each argument must be a nonempty short string without NUL")
    if not isinstance(cwd, str) or not cwd.startswith("/") or len(cwd) > MAX_TEXT or "\x00" in cwd:
        raise ValueError("cwd must be an absolute directory")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= MAX_SECONDS:
        raise ValueError("timeout_seconds must be between 1 and 60")
    return argv, cwd, timeout


def run(request: object) -> dict:
    global _child
    argv, cwd, timeout = validate(request)
    if not os.path.isdir(cwd):
        return {"state": "failed", "reason": "working directory does not exist"}
    _no_new_privileges()
    begun = time.monotonic()
    output = bytearray()
    error = bytearray()
    truncated = False
    state = "completed"
    try:
        _child = subprocess.Popen(
            argv, cwd=cwd, env=_environment(), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
    except FileNotFoundError:
        return {"state": "failed", "reason": "command not found"}
    except OSError as exc:
        return {"state": "failed", "reason": f"could not start command: {exc.strerror}"}

    with selectors.DefaultSelector() as selector:
        for pipe, target in ((_child.stdout, output), (_child.stderr, error)):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, target)
        while selector.get_map():
            remaining = timeout - (time.monotonic() - begun)
            if remaining <= 0:
                state = "timed_out"
                _kill_group(_child.pid)
                break
            for key, _mask in selector.select(min(remaining, 0.2)):
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target = key.data
                room = MAX_OUTPUT - len(output) - len(error)
                target.extend(chunk[:max(0, room)])
                if len(chunk) > room:
                    truncated = True
                    state = "output_limit"
                    _kill_group(_child.pid)
                    break
            if state == "output_limit":
                break
        _child.wait()

    return {
        "state": state,
        "exit_code": _child.returncode if state == "completed" else None,
        "stdout": output.decode("utf-8", "replace"),
        "stderr": error.decode("utf-8", "replace"),
        "truncated": truncated,
        "duration_ms": int((time.monotonic() - begun) * 1000),
    }


def main() -> None:
    signal.signal(signal.SIGTERM, _stop_child)
    try:
        raw = sys.stdin.buffer.read(16 * 1024 + 1)
        if len(raw) > 16 * 1024:
            raise ValueError("request is too large")
        result = run(json.loads(raw))
    except (ValueError, json.JSONDecodeError) as exc:
        result = {"state": "rejected", "reason": str(exc)}
    except KeyboardInterrupt:
        result = {"state": "cancelled"}
    print(json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
