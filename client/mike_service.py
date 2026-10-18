"""Client for Mike's shared local service, protocol 1.

Desktop uses this to attach to the one service instance this OS user runs.
The service owns the pairing, the task inbox and tool execution; a client
presents approvals and shows activity. See docs/local-service-protocol.md.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROTOCOL = 1
UNIT = os.environ.get("MIKE_SERVICE_UNIT", "mike-service.service")


class ServiceUnavailable(Exception):
    """No verified service is listening for this user."""


class ServiceError(Exception):
    """The service answered with a protocol error."""

    def __init__(self, status: int, body: dict):
        self.status = status
        self.body = body
        self.error = str(body.get("error", "error"))
        super().__init__(f"{status} {self.error}: {body.get('message', '')}")


def runtime_dir() -> Path:
    """Mirror the service's choice: explicit, then XDG_RUNTIME_DIR, then state."""
    chosen = os.environ.get("MIKE_SERVICE_RUNTIME_DIR")
    if chosen:
        return Path(chosen)
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "mike-service"
    state = os.environ.get("MIKE_SERVICE_STATE_DIR") or str(Path.home() / ".local/state/mike-service")
    return Path(state) / "run"


def _private(path: Path, directory: bool) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    return kind and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) & 0o077 == 0


def listener_owner(port: int) -> int | None:
    """The uid owning a loopback TCP listener on `port`, from /proc/net/tcp.

    A stale endpoint file must not lead a client to hand its key or account
    token to whatever process (perhaps another OS user's) took the port.
    """
    try:
        lines = Path("/proc/net/tcp").read_text().splitlines()[1:]
    except OSError:
        return None
    wanted = "0100007F:%04X" % port
    for line in lines:
        fields = line.split()
        if len(fields) > 7 and fields[1] == wanted and fields[3] == "0A":
            return int(fields[7])
    return None


def read_endpoint(directory: Path | None = None) -> tuple[int, str]:
    directory = directory or runtime_dir()
    endpoint, key = directory / "endpoint", directory / "client.key"
    if not (_private(directory, True) and _private(endpoint, False) and _private(key, False)):
        raise ServiceUnavailable("the Mike service is not running for this user")
    try:
        published = json.loads(endpoint.read_text())
        secret = key.read_text().strip()
    except (OSError, ValueError) as error:
        raise ServiceUnavailable("the Mike service endpoint is unreadable") from error
    port = published.get("port")
    if published.get("protocol") != PROTOCOL:
        raise ServiceError(426, {"error": "protocol_mismatch",
                                 "message": f"service publishes protocol {published.get('protocol')}"})
    if not isinstance(port, int) or not 0 < port < 65536 or len(secret) != 64:
        raise ServiceUnavailable("the Mike service endpoint is invalid")
    if listener_owner(port) != os.getuid():
        raise ServiceUnavailable("no Mike service of this user is listening")
    return port, secret


class MikeService:
    """One attached session. Not thread-safe; use one per thread."""

    def __init__(self, client: str = "desktop", approvals: bool = True, directory: Path | None = None):
        self.client = client
        self.approvals = approvals
        self.directory = directory
        self.port: int | None = None
        self.key = ""
        self.session = ""
        self.session_id = ""

    def _call(self, method: str, path: str, payload: dict | None, bearer: str, timeout: float) -> dict:
        if self.port is None:
            self.port, self.key = read_endpoint(self.directory)
        body = None if payload is None else json.dumps(payload).encode()
        request = Request(
            f"http://127.0.0.1:{self.port}{path}", data=body, method=method,
            headers={"Authorization": "Bearer " + bearer, "Content-Type": "application/json",
                     "X-Mike-Protocol": str(PROTOCOL)},
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read(1 << 20) or b"{}")
        except HTTPError as error:
            try:
                answer = json.loads(error.read(1 << 16) or b"{}")
            except ValueError:
                answer = {"error": "http_" + str(error.code)}
            raise ServiceError(error.code, answer) from None
        except (URLError, OSError, ValueError) as error:
            self.port = None
            raise ServiceUnavailable(str(error)) from error

    def attach(self, token: str | None = None, connect: str = "none", timeout: float = 45) -> dict:
        # A restarted service publishes a new port and key; always reread them.
        self.port, self.key = read_endpoint(self.directory)
        payload = {"client": self.client, "approvals": self.approvals, "connect": connect}
        if token:
            payload["token"] = token
        attached = self._call("POST", "/v1/attach", payload, self.key, timeout)
        self.session = attached["session_token"]
        self.session_id = attached["session_id"]
        return attached["state"]

    def call(self, path: str, payload: dict | None = None, timeout: float = 20) -> dict:
        if not self.session:
            raise ServiceError(401, {"error": "unauthorized", "message": "not attached"})
        return self._call("GET" if payload is None else "POST", path, payload, self.session, timeout)

    def state(self) -> dict:
        return self.call("/v1/state")

    def claim(self, approval_id: str) -> dict:
        return self.call("/v1/approvals/claim", {"approval_id": approval_id})

    def answer(self, approval_id: str, decision: str, **fields) -> dict:
        return self.call("/v1/approvals/answer", {"approval_id": approval_id, "decision": decision, **fields}, timeout=45)

    def stop(self, work_id: str) -> dict:
        return self.call("/v1/work/stop", {"work_id": work_id})

    def detach(self) -> None:
        try:
            self.call("/v1/detach", {}, timeout=5)
        except (ServiceError, ServiceUnavailable):
            pass
        self.session = ""

    def sign_out(self) -> dict:
        return self.call("/v1/signout", {}, timeout=30)

    def revoke(self) -> dict:
        return self.call("/v1/device/revoke", {}, timeout=30)


def reachable(directory: Path | None = None) -> bool:
    """True when this user's service answers protocol 1 on its endpoint."""
    try:
        port, _key = read_endpoint(directory)
    except (ServiceUnavailable, ServiceError):
        return False
    probe = Request(f"http://127.0.0.1:{port}/v1/state", headers={"X-Mike-Protocol": str(PROTOCOL)})
    try:
        urlopen(probe, timeout=3)
    except HTTPError as error:
        return error.code == 401
    except (URLError, OSError):
        return False
    return False


def unit_installed() -> bool:
    if not shutil.which("systemctl"):
        return False
    shown = subprocess.run(["systemctl", "--user", "cat", UNIT], capture_output=True, text=True, timeout=10)
    return shown.returncode == 0


def ensure_running(launcher: Path, wait: float = 20, env: dict | None = None) -> bool:
    """Start the shared service if it is not running.

    With the user unit installed systemd starts it (and owns its lifecycle);
    otherwise the service is started detached so it outlives this window.
    A second start is refused by the service itself, so racing is harmless.
    """
    if reachable():
        return True
    if env is None and unit_installed():
        subprocess.run(["systemctl", "--user", "start", UNIT], capture_output=True, timeout=30)
    else:
        log_dir = Path(os.environ.get("MIKE_SERVICE_STATE_DIR") or Path.home() / ".local/state/mike-service")
        log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(log_dir / "service.log", "ab") as log:
            subprocess.Popen([str(launcher), "run"], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             start_new_session=True, env=env)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if reachable():
            return True
        time.sleep(0.2)
    return False
