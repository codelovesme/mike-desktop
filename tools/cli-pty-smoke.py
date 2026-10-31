#!/usr/bin/python3
"""mike-cli in a real pseudo-terminal, against a stand-in service (nothing
leaves this computer): a question, a second one typed while Mike works
(queued, then sent), ↑ bringing it back, Ctrl-D to leave, the history kept.

    python3 tools/cli-pty-smoke.py
"""
import os
from pathlib import Path
import pty
import select
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
STAND_IN = r'''
import sys, time
sys.path.insert(0, sys.argv[1])
import cli, sign_in

class Service:
    asked = []
    def __init__(self, *a, **k): self.since = 0
    def attach(self, token=None, connect='none'):
        return {'account': 'ada@example.invalid', 'connection': 'connected', 'device_id': 'd1', 'chat_version': 1}
    def state(self): return {'account': 'ada@example.invalid', 'work': None}
    def call(self, route, body=None):
        if route.endswith('/ask'):
            Service.asked.append(body['text'])
            self.since = time.monotonic()
            return {'turn_id': 't%d' % len(Service.asked)}
        turn = 't%d' % len(Service.asked)
        if time.monotonic() - self.since < 2:
            return {'turn_id': turn, 'stage': 'plan', 'steps': []}
        return {'turn_id': turn, 'stage': 'done', 'reply': 'Answer to ' + Service.asked[-1], 'steps': []}
    def detach(self): pass

cli.MikeService = Service
sign_in.mike_service.reachable = lambda *a: True
sys.exit(cli.main(['--project', sys.argv[2]]))
'''

failures = []


def check(ok, what):
    print(('ok   ' if ok else 'FAIL ') + what)
    if not ok:
        failures.append(what)


def main():
    state = tempfile.mkdtemp(prefix='mike-cli-smoke-')
    script = Path(state) / 'stand_in.py'
    script.write_text(STAND_IN)
    pid, fd = pty.fork()
    if pid == 0:
        env = dict(os.environ, XDG_STATE_HOME=state, TERM='xterm-256color')
        os.execve('/usr/bin/python3', ['/usr/bin/python3', str(script), str(ROOT / 'client'), state], env)
    seen = b''

    def read(seconds):
        nonlocal seen
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    seen += os.read(fd, 65536)
                except OSError:
                    return

    def wait_for(text, seconds=10):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if text.encode() in seen:
                return True
            read(0.2)
        return text.encode() in seen

    check(wait_for('mike> '), 'starts signed in, with the prompt')
    check(b'ada@example.invalid' in seen, 'names the account')
    os.write(fd, b'first question\r')
    check(wait_for('Turn t1'), 'Enter sends the message')
    os.write(fd, b'second question\r')
    check(wait_for('Queued (1 waiting)'), 'a message typed while Mike works waits')
    check(wait_for('Answer to first question'), "Mike's answer is printed")
    check(wait_for('Turn t2'), 'then the waiting one goes')
    check(wait_for('Answer to second question'), 'and is answered')
    mark = len(seen)
    os.write(fd, b'\x1b[A')
    read(1.0)
    check(b'second question' in seen[mark:], '↑ brings the last message back to the line')
    os.write(fd, b'\x15\x04')  # Ctrl-U clears it, Ctrl-D leaves
    _, status = os.waitpid(pid, 0)
    check(os.WEXITSTATUS(status) == 0, 'Ctrl-D leaves cleanly')
    history = Path(state) / 'mike-cli' / 'history'
    check(history.is_file() and 'first question' in history.read_text(), 'the history is kept for the next run')
    check(history.is_file() and (history.stat().st_mode & 0o777) == 0o600, 'and only this user can read it')
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
