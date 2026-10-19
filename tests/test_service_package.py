"""The standalone service package: contents, Code requirement, lifecycle.

Stages `tools/package.py --service-only` into a temporary directory and runs
it from there, as an installed package would be run: no Desktop UI, no
source checkout on the path, no systemd unit (a unique unit name keeps any
real one out of the picture).
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
CODE = os.environ.get("CODE_BIN", "/home/cdlvsm/git/codelovesme/code/target/release/code")


class ServicePackageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="mike-service-package-")
        out = Path(cls.temporary.name) / "out"
        staged = subprocess.run(["/usr/bin/python3", str(ROOT / "tools/package.py"), "--service-only", "v0.0.0-test", str(out)],
                                capture_output=True, text=True, timeout=60)
        assert staged.returncode == 0, staged.stderr
        cls.package = out / "mike-service-v0.0.0-test-x86_64-linux"

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def env(self, area, **extra):
        env = {key: value for key, value in os.environ.items() if not key.startswith("MIKE_")}
        env.update(HOME=str(area), MIKE_SERVICE_STATE_DIR=str(area / "state"), MIKE_SERVICE_RUNTIME_DIR=str(area / "run"),
                   MIKE_SERVICE_HOST_URL="http://127.0.0.1:9", MIKE_SERVICE_UNIT="mike-service-package-test-none.service", **extra)
        return env

    def launch(self, package, area, *args, **extra):
        return subprocess.run([str(package / "mike-service"), *args], env=self.env(area, **extra), cwd=area,
                              capture_output=True, text=True, timeout=60)

    def test_contents_are_the_service_alone(self):
        names = {str(path.relative_to(self.package)) for path in self.package.rglob("*")}
        for required in ("mike-service", "VERSION", "service/main.code", "service/manifest.json",
                         "service/src/companion.gene.code", "service/src/files.gene.code", "service/src/nucleus.gene.code",
                         "service/adapters/terminal_worker.py", "service/adapters/secure_open.py",
                         "client/mike_service.py", "client/service_control.py",
                         "docs/local-service-protocol.md", "docs/service-package.md"):
            self.assertIn(required, names)
        locked = json.loads((ROOT / "service/.code/lock.json").read_text())["modules"]
        self.assertEqual({name for name in names if name.endswith(".so")}, {f"service/{m}.so" for m in locked})
        for absent in ("ui", "mike", "mike-desktop", "app.info"):
            self.assertNotIn(absent, names)
        self.assertFalse(any("pycache" in name or "desktop_webview" in name for name in names))

    def test_code_requirement_is_checked_not_guessed(self):
        with tempfile.TemporaryDirectory() as temporary:
            area = Path(temporary)
            version = self.launch(self.package, area, "--version")
            self.assertIn("Code >= 2.12.1 < 2.15.0", version.stdout)
            fake = area / "code"
            for reported in ("Code v2.16.0", "Code v2.12.0", "Code v3.0.0", "not code"):
                fake.write_text(f"#!/bin/sh\necho '{reported}'\n")
                fake.chmod(0o755)
                refused = self.launch(self.package, area, "run", MIKE_SERVICE_CODE_BIN=str(fake))
                self.assertEqual(refused.returncode, 1, reported)
                self.assertIn("is not Code >= 2.12.1 and < 2.15.0", refused.stderr, reported)
            missing = self.launch(self.package, area, "run")
            self.assertEqual(missing.returncode, 1)
            self.assertIn("needs Code >= 2.12.1 and < 2.15.0", missing.stderr)

    def test_cdlvsm_layout_uses_installed_code_only_when_compatible(self):
        with tempfile.TemporaryDirectory() as temporary:
            area = Path(temporary)
            installed = area / "packages/mike-service/v0.0.0-test"
            shutil.copytree(self.package, installed, symlinks=True)
            (area / "packages/code").mkdir(parents=True)
            current = area / "packages/code/current"
            current.mkdir()
            (current / "code").write_text("#!/bin/sh\necho 'Code v2.16.0'\n")
            (current / "code").chmod(0o755)
            refused = self.launch(installed, area, "run")
            self.assertEqual(refused.returncode, 1)
            (current / "code").unlink()
            (current / "code").symlink_to(CODE)
            # Reap with wait(), not communicate(): an unreaped service still
            # looks alive to its lock helper, which holds inherited pipes.
            log = open(area / "run.log", "w")
            process = subprocess.Popen([str(installed / "mike-service"), "run"], env=self.env(area), cwd=area,
                                       stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and not (area / "run/endpoint").exists():
                    time.sleep(0.1)
                self.assertTrue((area / "run/endpoint").exists(), "the installed-layout service did not start")
            finally:
                process.terminate()
                process.wait(timeout=10)
                log.close()
            self.assertIn("mike-service: ready for local clients", (area / "run.log").read_text())

    def test_lifecycle_without_desktop(self):
        with tempfile.TemporaryDirectory() as temporary:
            area = Path(temporary)
            started = self.launch(self.package, area, "start", MIKE_SERVICE_CODE_BIN=CODE)
            try:
                self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
                status = self.launch(self.package, area, "status")
                self.assertEqual(status.returncode, 0, status.stdout)
                self.assertIn("service: running", status.stdout)
                self.assertIn("connection: signed_out", status.stdout)
                self.assertTrue((area / "state/service.log").exists())
                self.assertEqual(os.stat(area / "state/service.log").st_mode & 0o777, 0o600)
            finally:
                stopped = self.launch(self.package, area, "stop")
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            after = self.launch(self.package, area, "status")
            self.assertEqual(after.returncode, 3)


if __name__ == "__main__":
    unittest.main()
