"""Process-level checks of the shared Mike service's local transport.

Each test starts the real native service (real modules, scratch state and
runtime directories, an unreachable gateway) and talks to it as a client
would — and as a browser page or a stranger would.
"""

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CODE = os.environ.get("CODE_BIN", "/home/cdlvsm/git/codelovesme/code/target/release/code")
sys.path.insert(0, str(ROOT / "client"))
import mike_service  # noqa: E402


def start(area: Path, wait: bool = True) -> subprocess.Popen:
    env = dict(os.environ, MIKE_SERVICE_STATE_DIR=str(area / "state"), MIKE_SERVICE_RUNTIME_DIR=str(area / "run"),
               MIKE_SERVICE_HOST_URL="http://127.0.0.1:9")
    process = subprocess.Popen([CODE, "run", "main.code"], cwd=ROOT / "service", env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if wait:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and process.poll() is None:
            if mike_service.reachable(area / "run"):
                return process
            time.sleep(0.1)
        process.kill()
        raise AssertionError("service did not start: " + (process.communicate()[0] or "")[-500:])
    return process


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    if process.stdout:
        process.stdout.close()


def raw(port: int, path: str, headers: dict, body: bytes | None = None, method: str | None = None):
    request = Request(f"http://127.0.0.1:{port}{path}", data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"{}")
    except HTTPError as error:
        payload = error.read()
        try:
            return error.code, json.loads(payload or b"{}")
        except ValueError:
            return error.code, {}


class ServiceTransportTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mike-service-test-")
        self.area = Path(self.temporary.name)
        self.process = start(self.area)
        self.run_dir = self.area / "run"
        self.port, self.key = mike_service.read_endpoint(self.run_dir)

    def tearDown(self):
        stop(self.process)
        self.temporary.cleanup()

    def client(self, name="test", approvals=True):
        return mike_service.MikeService(name, approvals=approvals, directory=self.run_dir)

    def test_runtime_files_are_private(self):
        for path, mode in ((self.run_dir, 0o700), (self.area / "state", 0o700),
                           (self.run_dir / "endpoint", 0o600), (self.run_dir / "client.key", 0o600)):
            info = os.lstat(path)
            self.assertEqual(info.st_mode & 0o777, mode, path)
            self.assertEqual(info.st_uid, os.getuid(), path)
        self.assertEqual(mike_service.listener_owner(self.port), os.getuid())

    def test_browser_requests_and_strangers_are_refused(self):
        good = {"Authorization": "Bearer " + self.key, "X-Mike-Protocol": "1", "Content-Type": "application/json"}
        body = json.dumps({"client": "x", "approvals": True}).encode()
        cases = [
            ("origin", dict(good, Origin="https://apps.codeloves.me"), 403, "browser_refused"),
            ("null origin", dict(good, Origin="null"), 403, "browser_refused"),
            ("fetch metadata", dict(good, **{"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"}), 403, "browser_refused"),
            ("referer", dict(good, Referer="http://evil.test/"), 403, "browser_refused"),
            ("rebound host", dict(good, Host=f"mike.evil.test:{self.port}"), 403, "host_refused"),
            ("newer protocol", dict(good, **{"X-Mike-Protocol": "2"}), 426, "protocol_mismatch"),
            ("no protocol", {k: v for k, v in good.items() if k != "X-Mike-Protocol"}, 426, "protocol_mismatch"),
            ("no key", {k: v for k, v in good.items() if k != "Authorization"}, 401, "unauthorized"),
            ("wrong key", dict(good, Authorization="Bearer " + "0" * 64), 401, "unauthorized"),
        ]
        for label, headers, status, error in cases:
            with self.subTest(label):
                answered = raw(self.port, "/v1/attach", headers, body)
                self.assertEqual(answered[0], status, answered)
                self.assertEqual(answered[1].get("error"), error)
        preflight = raw(self.port, "/v1/attach", {"Origin": "https://evil.test", "Access-Control-Request-Method": "POST"},
                        method="OPTIONS")
        self.assertEqual(preflight[0], 403)
        self.assertEqual(raw(self.port, "/v1/state", dict(good))[0], 401, "the client key is not a session")

    def test_two_clients_have_separate_sessions(self):
        desktop, watcher = self.client("desktop"), self.client("watcher", approvals=False)
        first = desktop.attach()
        second = watcher.attach()
        self.assertNotEqual(desktop.session, watcher.session)
        self.assertEqual(second["clients"], 2)
        self.assertEqual(second["approval_clients"], 1)
        self.assertEqual(first["connection"], "signed_out")
        with self.assertRaises(mike_service.ServiceError) as refused:
            watcher.claim("whatever")
        self.assertEqual(refused.exception.error, "approvals_not_allowed")
        with self.assertRaises(mike_service.ServiceError) as unknown:
            desktop.claim("not-an-approval")
        self.assertEqual(unknown.exception.error, "unknown_approval")
        desktop.detach()
        self.assertEqual(watcher.state()["clients"], 1)
        watcher.sign_out()
        with self.assertRaises(mike_service.ServiceError) as ended:
            watcher.state()
        self.assertEqual(ended.exception.error, "session_ended")
        self.assertEqual(ended.exception.body["reason"], "signed_out")
        self.assertEqual(watcher.attach()["generation"], first["generation"] + 1)

    def test_connecting_without_gateway_reports_unavailable(self):
        desktop = self.client()
        desktop.attach()
        with self.assertRaises(mike_service.ServiceError) as refused:
            desktop.attach("some-token", "auto")
        self.assertEqual(refused.exception.error, "account_refused")
        with self.assertRaises(mike_service.ServiceError) as bad:
            desktop.attach(None, "auto")
        self.assertEqual(bad.exception.error, "token_required")

    def test_second_start_is_refused_and_first_keeps_serving(self):
        second = start(self.area, wait=False)
        try:
            output, _ = second.communicate(timeout=20)
        finally:
            stop(second)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("another Mike service is already running", output)
        self.assertEqual(mike_service.read_endpoint(self.run_dir), (self.port, self.key))
        self.assertTrue(mike_service.reachable(self.run_dir))

    def test_crash_frees_the_instance_and_rotates_the_key(self):
        desktop = self.client()
        desktop.attach()
        self.process.send_signal(signal.SIGKILL)
        stop(self.process)
        with self.assertRaises(mike_service.ServiceUnavailable):
            desktop.state()
        self.assertFalse(mike_service.reachable(self.run_dir))
        self.process = start(self.area)
        port, key = mike_service.read_endpoint(self.run_dir)
        self.assertNotEqual(key, self.key)
        old_session = raw(port, "/v1/state", {"Authorization": "Bearer " + desktop.session, "X-Mike-Protocol": "1"})
        self.assertEqual(old_session[0], 401)
        self.assertEqual(desktop.attach()["connection"], "signed_out")


class EndpointTrustTest(unittest.TestCase):
    def test_stale_endpoint_without_listener_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "run"
            directory.mkdir(mode=0o700)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                free = probe.getsockname()[1]
            (directory / "endpoint").write_text(json.dumps({"protocol": 1, "port": free}))
            (directory / "client.key").write_text("k" * 64)
            for name in ("endpoint", "client.key"):
                os.chmod(directory / name, 0o600)
            with self.assertRaises(mike_service.ServiceUnavailable):
                mike_service.read_endpoint(directory)
            os.chmod(directory / "client.key", 0o644)
            with self.assertRaises(mike_service.ServiceUnavailable):
                mike_service.read_endpoint(directory)

    def refused(self, area):
        process = start(area, wait=False)
        try:
            output, _ = process.communicate(timeout=20)
        finally:
            stop(process)
        self.assertNotEqual(process.returncode, 0)
        return output

    def test_symlinked_runtime_directory_is_refused_unchanged(self):
        with tempfile.TemporaryDirectory(prefix="mike-service-test-") as temporary:
            area = Path(temporary)
            target = area / "elsewhere"
            target.mkdir(mode=0o755)
            os.chmod(target, 0o755)
            (target / "keep.txt").write_text("unchanged")
            (area / "run").symlink_to(target)
            output = self.refused(area)
            self.assertIn("not a private directory", output)
            self.assertEqual(os.stat(target).st_mode & 0o777, 0o755, "the symlink target's mode was changed")
            self.assertEqual(sorted(p.name for p in target.iterdir()), ["keep.txt"])
            self.assertEqual((target / "keep.txt").read_text(), "unchanged")

    def test_existing_shared_directory_is_refused_not_repaired(self):
        with tempfile.TemporaryDirectory(prefix="mike-service-test-") as temporary:
            area = Path(temporary)
            (area / "state").mkdir()
            os.chmod(area / "state", 0o755)
            output = self.refused(area)
            self.assertIn("state directory exists but is not a private directory", output)
            self.assertEqual(os.stat(area / "state").st_mode & 0o777, 0o755)
            self.assertEqual(list((area / "state").iterdir()), [])


class ClientLogTest(unittest.TestCase):
    def test_log_directory_and_file_symlinks_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            area = Path(temporary)
            target = area / "target"
            target.mkdir(mode=0o755)
            os.chmod(target, 0o755)
            (area / "state").symlink_to(target)
            with self.assertRaises(mike_service.ServiceUnavailable):
                mike_service.ensure_running(area / "never-run", wait=0, env={"MIKE_SERVICE_STATE_DIR": str(area / "state")})
            self.assertEqual(os.stat(target).st_mode & 0o777, 0o755)
            self.assertEqual(list(target.iterdir()), [])
            private = area / "private"
            private.mkdir(mode=0o700)
            victim = area / "victim.txt"
            victim.write_text("original")
            (private / "service.log").symlink_to(victim)
            with self.assertRaises(OSError):
                mike_service.open_private_log(mike_service.private_directory(private))
            self.assertEqual(victim.read_text(), "original")

    def test_missing_log_directory_is_created_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            made = mike_service.private_directory(Path(temporary) / "a" / "state")
            self.assertEqual(os.stat(made).st_mode & 0o777, 0o700)
            descriptor = mike_service.open_private_log(made)
            os.close(descriptor)
            self.assertEqual(os.stat(made / "service.log").st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
