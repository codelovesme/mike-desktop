import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
CODE = os.environ.get("CODE_BIN", "/home/cdlvsm/git/codelovesme/code/target/release/code")


class BridgeRuntimeTest(unittest.TestCase):
    def test_loopback_secret_and_session_gate(self):
        with tempfile.TemporaryDirectory(prefix="mike-bridge-test-") as directory:
            secret = secrets.token_urlsafe(40)
            env = os.environ.copy()
            env.update(MIKE_DESKTOP_BRIDGE_SECRET=secret, MIKE_DESKTOP_BRIDGE_DIR=directory)
            process = subprocess.Popen([CODE, "run", "main.code"], cwd=ROOT, env=env,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
            try:
                port_file = Path(directory) / "port"
                for _ in range(80):
                    if port_file.exists() or process.poll() is not None:
                        break
                    time.sleep(0.1)
                self.assertIsNone(process.poll(), "companion exited before bridge started")
                self.assertTrue(port_file.exists(), "no loopback port was published")
                base = "http://127.0.0.1:" + port_file.read_text()

                def call(path, body=None, authorized=True):
                    headers = {"Content-Type": "application/json"}
                    if authorized:
                        headers["Authorization"] = "Bearer " + secret
                    payload = json.dumps(body).encode() if body is not None else None
                    request = Request(base + path, data=payload, headers=headers)
                    try:
                        with urlopen(request, timeout=3) as response:
                            return response.status, response.read().decode()
                    except HTTPError as error:
                        return error.code, error.read().decode()

                self.assertEqual(call("/state", authorized=False)[0], 403)
                status, body = call("/state")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["connection"], "sign in")
                self.assertEqual(call("/task/decide", {"yes": True})[0], 401)
                self.assertEqual(call("/session", {"token": ""})[0], 400)
            finally:
                process.terminate()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
