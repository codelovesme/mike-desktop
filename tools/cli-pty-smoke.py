#!/usr/bin/python3
"""mike-cli in a real pseudo-terminal, against a stand-in service (nothing
leaves this computer), read back through a small terminal emulator: the
input box at the bottom, You/Mike blocks, a message queued while Mike works,
the `/` menu, a question answered in its box (Enter alone does nothing),
an image refused, multiline input, ↑ history, Ctrl-D, the history kept.

    python3 tools/cli-pty-smoke.py
"""
import fcntl
import os
from pathlib import Path
import pty
import re
import select
import struct
import sys
import tempfile
import termios
import time
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
COLS, ROWS = 90, 30
STAND_IN = r'''
import json, sys, time
sys.path.insert(0, sys.argv[1])
import cli, sign_in
LOG = sys.argv[3]

class Service:
    asked = []
    confirmed = None
    def __init__(self, *a, **k): self.since = 0
    def attach(self, token=None, connect='none'):
        return {'account': 'ada@example.invalid', 'connection': 'connected', 'device_id': 'd1', 'chat_version': 1}
    def state(self): return {'account': 'ada@example.invalid', 'connection': 'connected', 'work': None}
    def call(self, route, body=None, timeout=20):
        with open(LOG, 'a') as f: f.write(json.dumps([route, body]) + '\n')
        if route.endswith('/ask'):
            Service.asked.append(body['text'])
            self.since = time.monotonic()
            return {'turn_id': 't%d' % len(Service.asked)}
        if route.endswith('/confirm'):
            Service.confirmed = body['yes']
            return {'sent': True}
        turn = 't%d' % len(Service.asked)
        last = Service.asked[-1]
        if 'confirm' in last and Service.confirmed is None:
            return {'turn_id': turn, 'stage': 'confirm', 'say': 'Add the task "milk"?', 'confirmation_id': 'c' * 64, 'steps': []}
        if time.monotonic() - self.since < 2 and Service.confirmed is None:
            return {'turn_id': turn, 'stage': 'plan', 'say': 'Looking at it', 'steps': []}
        return {'turn_id': turn, 'stage': 'done', 'reply': 'Answer to **' + last.replace('\n', ' / ') + '**', 'steps': []}
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


class Emulator:
    """Just what mike-cli sends: CUP, ED, EL, CR, LF with scrolling, SGR and
    private modes ignored."""

    def __init__(self):
        self.grid = [[' '] * COLS for _ in range(ROWS)]
        self.r = self.c = 0
        self.rest = ''
        self.scrollback = []

    def scroll(self):
        self.scrollback.append(''.join(self.grid.pop(0)).rstrip())
        self.grid.append([' '] * COLS)

    def feed(self, text):
        s = self.rest + text
        self.rest = ''
        i = 0
        while i < len(s):
            ch = s[i]
            if ch == '\x1b':
                m = re.match(r'\x1b\[([0-9;?]*)([A-Za-z~])', s[i:])
                if not m:
                    if len(s) - i < 16:
                        self.rest = s[i:]
                        return
                    i += 1
                    continue
                params, final = m.group(1), m.group(2)
                i += len(m.group())
                if params.startswith('?'):
                    continue
                nums = [int(x) if x else 0 for x in params.split(';')] if params else []
                if final == 'H':
                    self.r = max(1, nums[0] if nums else 1) - 1
                    self.c = max(1, nums[1] if len(nums) > 1 else 1) - 1
                elif final == 'J':
                    mode = nums[0] if nums else 0
                    if mode == 0:
                        self.grid[self.r][self.c:] = [' '] * (COLS - self.c)
                        for r in range(self.r + 1, ROWS): self.grid[r] = [' '] * COLS
                    elif mode == 2:
                        self.grid = [[' '] * COLS for _ in range(ROWS)]
                elif final == 'K':
                    self.grid[self.r][self.c:] = [' '] * (COLS - self.c)
                continue
            i += 1
            if ch == '\r': self.c = 0
            elif ch == '\n':
                if self.r == ROWS - 1: self.scroll()
                else: self.r += 1
            elif ord(ch) >= 32:
                if self.c >= COLS:
                    self.c = 0
                    if self.r == ROWS - 1: self.scroll()
                    else: self.r += 1
                self.grid[self.r][self.c] = ch
                self.c += 2 if unicodedata.east_asian_width(ch) in 'WF' else 1

    def lines(self):
        return [''.join(row).rstrip() for row in self.grid]

    def everything(self):
        return '\n'.join(self.scrollback + self.lines())


def main():
    state = tempfile.mkdtemp(prefix='mike-cli-smoke-')
    script = Path(state) / 'stand_in.py'
    log = Path(state) / 'calls.jsonl'
    script.write_text(STAND_IN)
    image = Path(state) / 'shot.png'
    image.write_bytes(b'\x89PNG\r\n\x1a\n' + b'0' * 64)
    pid, fd = pty.fork()
    if pid == 0:
        env = dict(os.environ, XDG_STATE_HOME=state, TERM='xterm-256color', COLUMNS=str(COLS), LINES=str(ROWS))
        env.pop('WAYLAND_DISPLAY', None); env.pop('DISPLAY', None)
        os.execve('/usr/bin/python3', ['/usr/bin/python3', str(script), str(ROOT / 'client'), state, str(log)], env)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', ROWS, COLS, 0, 0))
    screen = Emulator()
    decoder = __import__('codecs').getincrementaldecoder('utf-8')(errors='replace')

    def pump(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            ready, _, _ = select.select([fd], [], [], 0.05)
            if ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    return
                if not data:
                    return
                screen.feed(decoder.decode(data))

    def wait_for(text, seconds=8):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            pump(0.1)
            if text in screen.everything():
                return True
        return False

    def bottom():
        lines = screen.lines()
        return lines

    def box_at_bottom():
        lines = bottom()
        rules = [i for i, l in enumerate(lines) if l.startswith('─' * 20)]
        return len(rules) >= 2 and rules[-1] == ROWS - 2 and lines[rules[-2] + 1].startswith('❯')

    check(wait_for('Ask Mike anything'), 'starts signed in, with the input box')
    check(box_at_bottom(), 'the box sits at the bottom of the terminal from the start')
    check('ada@example.invalid' in screen.everything(), 'names the account')
    os.write(fd, b'first question\r')
    check(wait_for('You\n  first question'), 'Enter sends; the message shows under You')
    os.write(fd, b'second question\r')
    check(wait_for('↳ queued: second question'), 'a message typed while Mike works waits, shown')
    check(wait_for('Answer to first question'), "Mike's answer is shown under Mike")
    check('Mike\n  Answer to first question' in screen.everything(), 'as a Mike block, markdown rendered')
    check(wait_for('Answer to second question'), 'then the waiting one goes and is answered')
    check(box_at_bottom(), 'the box is still at the bottom')
    os.write(fd, b'/')
    check(wait_for('/status'), '/ opens the command list at once')
    os.write(fd, b'sta')
    pump(0.5)
    check('/help' not in '\n'.join(bottom()), 'and filters it as you type')
    os.write(fd, b'\x15')  # Ctrl-U
    os.write(fd, b'line one\x1b\rline two\r')
    check(wait_for('Answer to line one / line two'), 'Alt+Enter adds a line; both lines are sent')
    os.write(fd, b'please confirm\r')
    check(wait_for('1. Yes'), "Mike's question shows in a box with choices")
    os.write(fd, b'\r')
    pump(1.0)
    check('/v1/chat/confirm' not in log.read_text(), 'Enter alone does not answer it')
    os.write(fd, b'1\r')
    check(wait_for('You said yes'), 'choosing 1 then Enter answers yes')
    check(wait_for('Answer to please confirm'), 'and the turn goes on')
    os.write(fd, b'\x1b[200~' + str(image).encode() + b'\x1b[201~')
    check(wait_for('[Image #1]'), 'a pasted image path becomes [Image #1]')
    os.write(fd, b' what is this\r')
    check(wait_for('cannot take images yet'), 'sending an image says plainly that Mike cannot take it yet')
    os.write(fd, b'\x15\x15')
    pump(0.3)
    mark = len(screen.everything())
    os.write(fd, b'\x1b[A')
    pump(0.6)
    check('please confirm' in '\n'.join(bottom()), '↑ brings the last message back to the box')
    os.write(fd, b'\x15\x04')  # Ctrl-U clears it, Ctrl-D leaves
    _, status = os.waitpid(pid, 0)
    check(os.WEXITSTATUS(status) == 0, 'Ctrl-D leaves cleanly')
    history = Path(state) / 'mike-cli' / 'history'
    check(history.is_file() and 'first question' in history.read_text(), 'the history is kept for the next run')
    check(history.is_file() and '"line one\\nline two"' in history.read_text(), 'multiline messages too')
    check(history.is_file() and (history.stat().st_mode & 0o777) == 0o600, 'and only this user can read it')
    if failures:
        print('\n'.join(screen.lines()))
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
