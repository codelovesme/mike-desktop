"""Mike Desktop 0.4's pairing carried into the shared service.

The real service runs against a small fake host (auth `Me` and the Mike
Interface device particles) so each case is deterministic: matching owner,
another owner, an identity already in the new store, unsafe or malformed
legacy files, a refused legacy credential, a gateway outage, and revocation
followed by a restart. The legacy files are never changed.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
CODE = os.environ.get("CODE_BIN", "/home/cdlvsm/git/codelovesme/code/target/release/code")
sys.path.insert(0, str(ROOT / "client"))
import mike_service  # noqa: E402

OWNER = "ada@example.test"


class FakeHost:
    """Just enough of auth and mike-interface for pairing decisions."""

    def __init__(self):
        self.tokens = {"tok-ada": OWNER, "tok-bob": "bob@example.test"}
        self.devices = {"legacy-1": {"owner": OWNER, "credential": "legacy-secret", "active": True}}
        self.calls = []
        self.outage = False
        host = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if host.outage:
                    self.send_response(503)
                    self.end_headers()
                    return
                answer = host.answer(self.path, body)
                data = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def answer(self, path, envelope):
        owner = self.tokens.get(envelope.get("token"))
        particle = envelope.get("particle", {})
        kind = particle.get("_class")
        self.calls.append(kind)
        if path == "/api/auth" and kind == "Me" and owner:
            return {"_class": "Account", "email": owner, "active": True, "verified": True}
        if path != "/api/mike-interface" or not owner:
            return {"_class": "Denied", "reason": "token"}
        device = self.devices.get(particle.get("device_id"))
        if kind == "PairDevice":
            name = f"new-{len(self.devices)}"
            self.devices[name] = {"owner": owner, "credential": "fresh-" + name, "active": True}
            return {"_class": "DevicePaired", "device_id": name, "credential": "fresh-" + name}
        if kind == "SetDefaultDevice":
            return {"_class": "DefaultDeviceSet", "device_id": particle.get("device_id")}
        if kind == "RevokeDevice" and device and device["owner"] == owner:
            device["active"] = False
            return {"_class": "DeviceRevoked", "device_id": particle["device_id"], "replayed": False}
        if kind in ("Heartbeat", "ClaimTask"):
            if not device or device["owner"] != owner or not device["active"] or device["credential"] != particle.get("credential"):
                return {"_class": "Forbidden", "reason": "device credentials were refused"}
            return {"_class": "DeviceOnline", "device_id": particle["device_id"], "at": 1} if kind == "Heartbeat" else {"_class": "NoTask"}
        return {"_class": "Unknown", "class": kind}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class LegacyMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mike-legacy-test-")
        self.area = Path(self.temporary.name)
        self.host = FakeHost()
        self.legacy = self.area / "mike-desktop"
        self.legacy.mkdir(mode=0o700)
        os.chmod(self.legacy, 0o700)
        for name, value in (("owner", OWNER), ("device_id", "legacy-1"), ("credential", "legacy-secret")):
            path = self.legacy / name
            path.write_text(value)
            os.chmod(path, 0o600)
        self.process = None

    def tearDown(self):
        self.stop()
        self.host.close()
        self.temporary.cleanup()

    def snapshot(self):
        return {p.name: (p.read_text() if p.is_file() and not p.is_symlink() else os.readlink(p) if p.is_symlink() else None,
                         os.lstat(p).st_mode)
                for p in sorted(self.legacy.iterdir())} | {".": os.lstat(self.legacy).st_mode}

    def start(self):
        env = dict(os.environ, MIKE_SERVICE_STATE_DIR=str(self.area / "state"), MIKE_SERVICE_RUNTIME_DIR=str(self.area / "run"),
                   MIKE_SERVICE_HOST_URL=self.host.url, MIKE_SERVICE_LEGACY_STATE_DIR=str(self.legacy))
        self.process = subprocess.Popen([CODE, "run", "main.code"], cwd=ROOT / "service", env=env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not mike_service.reachable(self.area / "run"):
            time.sleep(0.1)
        self.assertTrue(mike_service.reachable(self.area / "run"), "service did not start")

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)

    def connect(self, token="tok-ada", connect="auto"):
        client = mike_service.MikeService("test", directory=self.area / "run")
        return client, client.attach(token, connect)

    def stored(self, name):
        path = self.area / "state" / name
        return path.read_text() if path.exists() else None

    def test_matching_owner_reuses_the_pairing_and_keeps_the_old_files(self):
        before = self.snapshot()
        self.start()
        _client, state = self.connect()
        self.assertEqual(state["connection"], "connected")
        self.assertNotIn("PairDevice", self.host.calls)
        self.assertEqual(self.stored("device_id"), "legacy-1")
        self.assertEqual(self.stored("credential"), "legacy-secret")
        self.assertEqual(os.stat(self.area / "state/credential").st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.stored("legacy-migrated"))["outcome"], "adopted")
        self.assertEqual(self.snapshot(), before, "the legacy store was changed")
        self.stop()
        self.host.calls.clear()
        self.start()
        self.connect()
        self.assertNotIn("PairDevice", self.host.calls, "a restart paired again")

    def test_another_owner_pairs_anew_and_leaves_the_legacy_for_its_owner(self):
        self.start()
        _client, state = self.connect("tok-bob")
        self.assertEqual(state["connection"], "connected")
        self.assertIn("PairDevice", self.host.calls)
        self.assertNotEqual(self.stored("credential"), "legacy-secret")
        self.assertIsNone(self.stored("legacy-migrated"))

    def test_an_existing_service_identity_is_never_overwritten(self):
        state = self.area / "state"
        state.mkdir(mode=0o700)
        for name, value in (("owner", "bob@example.test"), ("device_id", "bob-device"), ("credential", "bob-secret")):
            (state / name).write_text(value)
            os.chmod(state / name, 0o600)
        self.host.devices["bob-device"] = {"owner": "bob@example.test", "credential": "bob-secret", "active": True}
        self.start()
        self.connect("tok-bob")
        self.assertEqual(self.stored("device_id"), "bob-device")
        self.host.calls.clear()
        switched = mike_service.MikeService("t", directory=self.area / "run").attach("tok-ada", "switch")
        self.assertEqual(switched["account"], OWNER)
        self.assertIn("PairDevice", self.host.calls, "with an identity present the legacy one must not be copied over it")
        self.assertIsNone(self.stored("legacy-migrated"))

    def test_unsafe_or_malformed_legacy_files_are_ignored_untouched(self):
        cases = {
            "shared directory": lambda: os.chmod(self.legacy, 0o755),
            "readable credential": lambda: os.chmod(self.legacy / "credential", 0o644),
            "symlinked credential": lambda: (self.area.joinpath("elsewhere").write_text("legacy-secret"),
                                             (self.legacy / "credential").unlink(),
                                             (self.legacy / "credential").symlink_to(self.area / "elsewhere")),
            "empty device id": lambda: (self.legacy / "device_id").write_text(""),
            "two-line credential": lambda: (self.legacy / "credential").write_text("legacy-secret\nmore"),
        }
        for label, spoil in cases.items():
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                spoil()
                before = self.snapshot()
                self.start()
                self.connect()
                self.assertIn("PairDevice", self.host.calls, label)
                self.assertNotEqual(self.stored("device_id"), "legacy-1", label)
                self.assertEqual(self.snapshot(), before, label)

    def test_a_refused_legacy_credential_is_not_retried(self):
        self.host.devices["legacy-1"]["active"] = False
        self.start()
        self.connect()
        self.assertIn("PairDevice", self.host.calls)
        self.assertEqual(json.loads(self.stored("legacy-migrated"))["outcome"], "refused")

    def test_gateway_outage_adopts_offline_without_pairing(self):
        self.start()
        client = mike_service.MikeService("test", directory=self.area / "run")
        # auth must answer for the attach; the device gateway is what fails.
        original = self.host.answer
        self.host.answer = lambda path, body: original(path, body) if path == "/api/auth" else {"_class": "Unavailable"}
        state = client.attach("tok-ada", "auto")
        self.host.answer = original
        self.assertEqual(state["connection"], "offline")
        self.assertNotIn("PairDevice", self.host.calls)
        self.assertEqual(self.stored("device_id"), "legacy-1")

    def test_revocation_survives_restart_and_never_resurrects_the_legacy_pairing(self):
        before = self.snapshot()
        self.start()
        client, _ = self.connect()
        revoked = client.revoke()
        self.assertTrue(revoked["revoked"] and revoked["confirmed_by_gateway"])
        self.assertIsNone(self.stored("credential"))
        self.stop()
        self.start()
        observer = mike_service.MikeService("t", directory=self.area / "run")
        self.assertEqual(observer.attach()["connection"], "revoked")
        with self.assertRaises(mike_service.ServiceError) as latched:
            observer.attach("tok-ada", "auto")
        self.assertEqual(latched.exception.error, "connect_required")
        self.host.calls.clear()
        self.host.devices["legacy-1"]["active"] = True  # even a still-valid old credential stays retired
        state = observer.attach("tok-ada", "switch")
        self.assertEqual(state["connection"], "connected")
        self.assertIn("PairDevice", self.host.calls)
        self.assertNotEqual(self.stored("device_id"), "legacy-1")
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
