"""mike-root's checks: only a live step signed by Mike's server for this device, once."""

import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "service/root/mike_root.py"
spec = importlib.util.spec_from_file_location("mike_root", SOURCE)
mike_root = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mike_root)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Signer:
    """An Ed25519 key made and used with openssl, as the server's jwt module would."""

    def __init__(self, folder: str):
        self.key = os.path.join(folder, "key.pem")
        subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", self.key], check=True,
                       capture_output=True)
        der = subprocess.run(["openssl", "pkey", "-in", self.key, "-pubout", "-outform", "DER"], check=True,
                             capture_output=True).stdout
        self.public = der[-32:]
        self.folder = folder

    def sign(self, claims: dict, header=None) -> str:
        head = b64url(json.dumps(header or {"alg": "EdDSA", "typ": "JWT"}).encode())
        body = b64url(json.dumps(claims).encode())
        data = os.path.join(self.folder, "data")
        Path(data).write_bytes((head + "." + body).encode())
        signature = subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", self.key, "-rawin", "-in", data],
                                   check=True, capture_output=True).stdout
        return head + "." + body + "." + b64url(signature)


def step(device="desk", argv=("id",), cwd="/", step_id="s1"):
    return json.dumps({"device": device, "argv": list(argv), "cwd": cwd, "step": step_id})


class MikeRootTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.signer = Signer(self.folder.name)
        self.now = int(time.time())

    def tearDown(self):
        self.folder.cleanup()

    def claims(self, **change):
        claims = {"sub": step(), "role": "mike-root", "iat": self.now, "exp": self.now + 120}
        claims.update(change)
        return claims

    def check(self, token, device="desk", public=None):
        return mike_root.check_token(token, public or self.signer.public, device, self.now)

    def test_a_genuine_live_step_for_this_device_is_read(self):
        got = self.check(self.signer.sign(self.claims()))
        self.assertEqual(got["argv"], ["id"])
        self.assertEqual(got["cwd"], "/")
        self.assertEqual(got["step"], "s1")

    def test_everything_else_is_refused(self):
        other = Signer(tempfile.mkdtemp(dir=self.folder.name))
        genuine = self.signer.sign(self.claims())
        head, body, sig = genuine.split(".")
        altered = b64url(json.dumps(self.claims(sub=step(argv=("rm", "-rf", "/")))).encode())
        cases = {
            "forged by another key": other.sign(self.claims()),
            "altered command": head + "." + altered + "." + sig,
            "another device": self.signer.sign(self.claims(sub=step(device="laptop"))),
            "expired": self.signer.sign(self.claims(iat=self.now - 400, exp=self.now - 10)),
            "too long a life": self.signer.sign(self.claims(exp=self.now + 3600)),
            "not a root step": self.signer.sign(self.claims(role="user")),
            "HS256 header": self.signer.sign(self.claims(), header={"alg": "HS256"}),
            "no command": self.signer.sign(self.claims(sub=step(argv=()))),
            "garbage": "not.a.token",
        }
        for name, token in cases.items():
            with self.subTest(name):
                with self.assertRaises(mike_root.Refused):
                    self.check(token)

    def test_a_step_runs_once(self):
        used = os.path.join(self.folder.name, "state", "used")
        mike_root.claim_once("s1", self.now + 120, self.now, used)
        with self.assertRaises(mike_root.Refused):
            mike_root.claim_once("s1", self.now + 120, self.now, used)
        mike_root.claim_once("s2", self.now + 120, self.now, used)
        # Expired ids are forgotten; the token itself would be refused by then.
        mike_root.claim_once("s3", self.now + 1, self.now + 500, used)
        self.assertNotIn("s1", Path(used).read_text())

    def test_without_root_it_runs_nothing(self):
        if os.geteuid() == 0:
            self.skipTest("runs as root")
        done = subprocess.run(["/usr/bin/python3", str(SOURCE)], input="x.y.z", text=True, capture_output=True)
        self.assertEqual(done.returncode, 126)
        self.assertIn("did not run", done.stderr)
        extra = subprocess.run(["/usr/bin/python3", str(SOURCE), "id"], input="", text=True, capture_output=True)
        self.assertEqual(extra.returncode, 126)


if __name__ == "__main__":
    unittest.main()
