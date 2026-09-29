"""Process-level checks for Mike's approved terminal worker."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest


WORKER = Path(__file__).resolve().parents[1] / "ui/terminal_worker.py"


def invoke(argv, cwd, timeout_seconds=5, env=None):
    request = {"argv": argv, "cwd": str(cwd), "timeout_seconds": timeout_seconds}
    completed = subprocess.run(
        ["/usr/bin/python3", str(WORKER)], input=json.dumps(request), text=True,
        capture_output=True, timeout=10, env=env,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


class TerminalWorkerTest(unittest.TestCase):
    def test_argv_is_data_and_cwd_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            result = invoke(["/usr/bin/printf", "%s", "$(touch injected)"], directory)
            self.assertEqual(result["state"], "completed")
            self.assertEqual(result["stdout"], "$(touch injected)")
            self.assertFalse((Path(directory) / "injected").exists())
            self.assertEqual(invoke(["pwd"], directory)["stdout"].strip(), directory)

    def test_missing_timeout_and_output_limit_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(invoke(["absent-mike-test-command"], directory)["reason"], "command not found")
            self.assertEqual(invoke(["/usr/bin/sleep", "3"], directory, 1)["state"], "timed_out")
            result = invoke(["/usr/bin/head", "-c", "40000", "/dev/zero"], directory)
            self.assertEqual(result["state"], "output_limit")
            self.assertTrue(result["truncated"])
            self.assertLessEqual(len(result["stdout"].encode()), 32768)

    def test_bridge_secret_is_not_inherited(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, MIKE_DESKTOP_BRIDGE_SECRET="private-test-secret")
            result = invoke(["/usr/bin/env"], directory, env=env)
            self.assertNotIn("MIKE_DESKTOP_BRIDGE_SECRET", result["stdout"])
            status = invoke(["/usr/bin/cat", "/proc/self/status"], directory)
            self.assertIn("NoNewPrivs:\t1", status["stdout"])

    def test_rejects_invalid_input_before_spawn(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(invoke(["/usr/bin/printf", "x"], "relative")["state"], "rejected")
            self.assertEqual(invoke(["/usr/bin/printf", "x"], directory, 0)["state"], "rejected")

    def test_stop_cancels_a_running_command(self):
        with tempfile.TemporaryDirectory() as directory:
            worker = subprocess.Popen(
                ["/usr/bin/python3", str(WORKER)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            worker.stdin.write(json.dumps({"argv": ["/usr/bin/sleep", "30"],
                                           "cwd": directory, "timeout_seconds": 60}))
            worker.stdin.close()
            time.sleep(0.2)
            worker.terminate()
            result = json.loads(worker.stdout.readline())
            worker.wait(timeout=3)
            worker.stdout.close()
            worker.stderr.close()
            self.assertEqual(result["state"], "cancelled")

    def test_stop_kills_child_that_ignores_term(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child.pid"
            command = (
                "import pathlib,signal,subprocess,time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "child=subprocess.Popen(['/usr/bin/sleep','30']); "
                f"pathlib.Path({str(marker)!r}).write_text(str(child.pid)); "
                "time.sleep(30)"
            )
            worker = subprocess.Popen(
                ["/usr/bin/python3", str(WORKER)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            worker.stdin.write(json.dumps({"argv": ["/usr/bin/python3", "-c", command],
                                           "cwd": directory, "timeout_seconds": 60}))
            worker.stdin.close()
            deadline = time.monotonic() + 4
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "command did not start")
            child_pid = int(marker.read_text())
            worker.terminate()
            result = json.loads(worker.stdout.readline())
            worker.wait(timeout=3)
            worker.stdout.close()
            worker.stderr.close()
            self.assertEqual(result["state"], "cancelled")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                status = Path(f"/proc/{child_pid}/status")
                if not status.exists() or "State:\tZ" in status.read_text():
                    break
                time.sleep(0.02)
            else:
                self.fail("grandchild survived Stop")


if __name__ == "__main__":
    unittest.main()
