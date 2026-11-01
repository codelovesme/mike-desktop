"""Mike CLI's full-screen chat: the conversation scrolls into the terminal's
own scrollback, and a live area stays fixed at the bottom — what Mike is
doing, the input box (or a question to answer), and a footer that says who,
where and what is going on. Nothing happens out of sight."""
from __future__ import annotations

import base64
import codecs
import json
import os
from pathlib import Path
import queue
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty
import unicodedata

from voice_audio import Detector, Player, TAP_SECONDS, IDLE_SECONDS

# ── Colours ─────────────────────────────────────────────────────────────
RESET = '\x1b[0m'
BOLD = '\x1b[1m'
DIM = '\x1b[2m'
ITALIC = '\x1b[3m'
INVERSE = '\x1b[7m'
YOU = '\x1b[1;38;5;114m'
MIKE = '\x1b[1;38;5;141m'
ACCENT = '\x1b[38;5;141m'
CODE = '\x1b[38;5;81m'
RED = '\x1b[38;5;203m'
YELLOW = '\x1b[38;5;221m'
GRAY = '\x1b[38;5;245m'

SPINNER = '·✢✳✶✻✽✻✶✳✢'
MOST_INPUT_LINES = 12
IMAGE_SUFFIXES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                  '.gif': 'image/gif', '.webp': 'image/webp'}
IMAGE_MOST = 10 << 20
RECORD_MOST = 60

COMMANDS = [
    ('/model', 'MODEL', 'list or choose the model for the next message'),
    ('/think', 'LEVEL', 'off, low, medium, high for compatible models'),
    ('/mode', 'MODE', 'ask, plan or auto within project grants'),
    ('/help', '', 'what you can type and press'),
    ('/status', '', 'account, computer, project, turn'),
    ('/clear', '', 'clear the screen (Mike keeps the conversation)'),
    ('/conversations', '[all]', "this computer's conversations, or every one"),
    ('/open', 'N', 'continue a conversation from /conversations'),
    ('/new', '', 'start a new conversation'),
    ('/stop', '', "stop the command Mike is running on this computer"),
    ('/resume', 'TURN', 'follow an earlier turn by its id'),
    ('/unqueue', '', 'take back the messages waiting to be sent'),
    ('/yes', 'ID', "answer Mike's question: yes"),
    ('/no', 'ID', "answer Mike's question: no"),
    ('/approve', 'ID', 'let Mike run its request on this computer'),
    ('/decline', 'ID', "refuse Mike's request"),
    ('/find', 'ID QUERY', 'for a file request: search this project'),
    ('/quit', '', 'leave (Mike and the service keep going)'),
]

SHORTCUTS = [
    ('enter', 'send'),
    ('alt+enter · ctrl+j · \\ enter', 'new line'),
    ('/', 'commands'),
    ('↑ ↓', 'move between lines, then earlier messages'),
    ('ctrl+v', 'paste an image (or text) from the clipboard'),
    ('ctrl+space', 'hold: record once · tap: keep recording'),
    ('ctrl+shift+space', 'spoken conversation on/off'),
    ('esc', 'close a menu · stop the running command'),
    ('ctrl+c', 'clear the box · twice to leave'),
    ('ctrl+d', 'leave'),
]


def safe(value, limit=16384):
    """Text from Mike or a tool cannot send terminal control sequences."""
    value = str(value)
    return ''.join(c for c in value if c in '\n' or not unicodedata.category(c).startswith('C'))[:limit]


# ── Widths and wrapping ─────────────────────────────────────────────────
def char_width(c):
    if unicodedata.combining(c) or c in '​‍️':
        return 0
    return 2 if unicodedata.east_asian_width(c) in 'WF' else 1


def text_width(s):
    return sum(char_width(c) for c in s)


def wrap_spans(spans, width, indent='', first=None):
    """[(text, style)] → styled lines no wider than width. The first line
    starts with first (default indent), the lines after a wrap with indent."""
    first = indent if first is None else first
    width = max(width, text_width(indent) + 8)
    lines, line, used = [], first, text_width(first)
    lead = text_width(indent)
    fresh = True  # nothing but the prefix on this line yet
    current = ''  # the style in force on this line

    def add(piece, style):
        nonlocal line, current
        if style != current:
            line += (RESET if current else '') + style
            current = style
        line += piece

    def flush():
        nonlocal line, used, fresh, current
        lines.append(line + (RESET if current else ''))
        line, used, fresh, current = indent, lead, True, ''

    for text, style in spans:
        for word in re.findall(r'\S+|\s+', text):
            w = text_width(word)
            if word.isspace():
                if fresh and lines:
                    continue  # no leading space on a wrapped line
                if used + w > width:
                    flush()
                    continue
                add(word, style)
                used += w
                continue
            if used + w > width and not fresh:
                flush()
            while used + w > width:  # longer than a line: break it
                part, pw = '', 0
                for c in word:
                    if used + pw + char_width(c) > width:
                        break
                    part += c
                    pw += char_width(c)
                if not part:
                    break
                add(part, style)
                flush()
                word = word[len(part):]
                w = text_width(word)
            if word:
                add(word, style)
                used += w
                fresh = False
    lines.append(line + (RESET if current else ''))
    return lines


def clip(s, width):
    """A styled line cut to width columns."""
    out, used, i = '', 0, 0
    while i < len(s):
        if s[i] == '\x1b':
            m = re.match(r'\x1b\[[0-9;?]*[A-Za-z]', s[i:])
            if m:
                out += m.group()
                i += len(m.group())
                continue
        w = char_width(s[i])
        if used + w > width:
            return out + RESET
        out += s[i]
        used += w
        i += 1
    return out


def visible_width(s):
    return text_width(re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', s))


# ── Markdown, the little of it a terminal can show ──────────────────────
def inline(text, base=''):
    spans = []
    for part in re.split(r'(`[^`]+`|\*\*[^*]+\*\*)', text):
        if not part:
            continue
        if part.startswith('`') and part.endswith('`') and len(part) > 1:
            spans.append((part[1:-1], CODE))
        elif part.startswith('**') and part.endswith('**') and len(part) > 3:
            spans.append((part[2:-2], BOLD + base))
        else:
            spans.append((part, base))
    return spans


def markdown(text, width, indent='  '):
    lines, fence = [], False
    for raw in text.split('\n'):
        if raw.strip().startswith('```'):
            fence = not fence
            continue
        if fence:
            lines.append(indent + '  ' + CODE + clip(raw, width - len(indent) - 2) + RESET)
            continue
        heading = re.match(r'#{1,6}\s+(.*)', raw)
        bullet = re.match(r'(\s*)[-*+]\s+(.*)', raw)
        number = re.match(r'(\s*)(\d+[.)])\s+(.*)', raw)
        if not raw.strip():
            lines.append('')
        elif heading:
            lines += wrap_spans(inline(heading.group(1), BOLD), width, indent)
        elif bullet:
            pad = indent + bullet.group(1)
            lines += wrap_spans([('• ', ACCENT)] + inline(bullet.group(2)), width, pad + '  ', pad)
        elif number:
            pad = indent + number.group(1)
            mark = number.group(2) + ' '
            lines += wrap_spans([(mark, ACCENT)] + inline(number.group(3)), width, pad + ' ' * len(mark), pad)
        else:
            lines += wrap_spans(inline(raw), width, indent)
    while lines and not lines[-1]:
        lines.pop()
    return lines


# ── The text being typed ────────────────────────────────────────────────
class Editor:
    """Multiline text with a caret, history and images; no terminal."""

    def __init__(self, history=()):
        self.text, self.caret = '', 0
        self.history = list(history)
        self.back = None  # index into history while browsing it
        self.draft = ''
        self.images = {}  # n → (mime, bytes)
        self.next_image = 1

    def set(self, text):
        self.text, self.caret = text, len(text)

    def insert(self, s):
        self.text = self.text[:self.caret] + s + self.text[self.caret:]
        self.caret += len(s)
        self.back = None

    def backspace(self):
        if self.caret:
            image = re.search(r'\[Image #\d+\]$', self.text[:self.caret])
            n = len(image.group()) if image else 1
            self.text = self.text[:self.caret - n] + self.text[self.caret:]
            self.caret -= n

    def delete(self):
        self.text = self.text[:self.caret] + self.text[self.caret + 1:]

    def left(self): self.caret = max(0, self.caret - 1)
    def right(self): self.caret = min(len(self.text), self.caret + 1)

    def home(self):
        self.caret = self.text.rfind('\n', 0, self.caret) + 1

    def end(self):
        n = self.text.find('\n', self.caret)
        self.caret = len(self.text) if n < 0 else n

    def word_left(self):
        i = self.caret
        while i and self.text[i - 1].isspace(): i -= 1
        while i and not self.text[i - 1].isspace(): i -= 1
        self.caret = i

    def word_right(self):
        i, n = self.caret, len(self.text)
        while i < n and self.text[i].isspace(): i += 1
        while i < n and not self.text[i].isspace(): i += 1
        self.caret = i

    def kill_before(self):
        start = self.text.rfind('\n', 0, self.caret) + 1
        if start == self.caret and start:
            start -= 1
        self.text = self.text[:start] + self.text[self.caret:]
        self.caret = start

    def kill_after(self):
        n = self.text.find('\n', self.caret)
        n = len(self.text) if n < 0 else (n + 1 if n == self.caret else n)
        self.text = self.text[:self.caret] + self.text[n:]

    def kill_word(self):
        end = self.caret
        self.word_left()
        self.text = self.text[:self.caret] + self.text[end:]

    def clear(self):
        self.text, self.caret, self.back = '', 0, None
        self.images = {}

    def add_image(self, mime, data):
        n = self.next_image
        self.next_image += 1
        self.images[n] = (mime, data)
        self.insert(f'[Image #{n}]')
        return n

    def images_used(self):
        return sorted(int(n) for n in re.findall(r'\[Image #(\d+)\]', self.text) if int(n) in self.images)

    # Layout: rows of (start, end, last_of_its_line) for a given width.
    def rows(self, width):
        width = max(width, 4)
        rows, start = [], 0
        for line in self.text.split('\n'):
            end_of_line = start + len(line)
            i, used, row_start = start, 0, start
            while i < end_of_line:
                w = char_width(self.text[i])
                if used + w > width:
                    rows.append((row_start, i, False))
                    row_start, used = i, 0
                used += w
                i += 1
            rows.append((row_start, end_of_line, True))
            start = end_of_line + 1
        return rows

    def where(self, width):
        rows = self.rows(width)
        for r, (s, e, last) in enumerate(rows):
            if s <= self.caret < e or (self.caret == e and last):
                return rows, r, text_width(self.text[s:self.caret])
        return rows, len(rows) - 1, 0

    def vertical(self, width, step):
        """Move a row up (-1) or down (+1); False at the edge."""
        rows, r, col = self.where(width)
        t = r + step
        if not 0 <= t < len(rows):
            return False
        s, e, _ = rows[t]
        i, used = s, 0
        while i < e and used + char_width(self.text[i]) <= col:
            used += char_width(self.text[i])
            i += 1
        self.caret = i
        return True

    def older(self):
        if not self.history:
            return
        if self.back is None:
            self.draft, self.back = self.text, len(self.history)
        if self.back:
            self.back -= 1
            self.set(self.history[self.back])

    def newer(self):
        if self.back is None:
            return
        self.back += 1
        if self.back >= len(self.history):
            self.back = None
            self.set(self.draft)
        else:
            self.set(self.history[self.back])

    def remember(self, text):
        if text.strip() and (not self.history or self.history[-1] != text):
            self.history.append(text)
        del self.history[:-500]
        self.back = None


def read_history(path):
    try:
        raw = Path(path).read_text(errors='replace').splitlines()
    except OSError:
        return []
    out = []
    for line in raw:
        if line.startswith('"'):
            try:
                out.append(json.loads(line))
                continue
            except ValueError:
                pass
        if line and line != '_HiStOrY_V2_':
            out.append(line)
    return out[-500:]


def write_history(path, entries):
    if not path:
        return
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            for entry in entries[-500:]:
                f.write(json.dumps(entry) + '\n')
        os.chmod(path, 0o600)
    except OSError:
        pass


# ── Keys ────────────────────────────────────────────────────────────────
CONTROL = {'\r': 'enter', '\n': 'newline', '\t': 'tab', '\x7f': 'backspace', '\x08': 'backspace',
           '\x01': 'home', '\x05': 'end', '\x02': 'left', '\x06': 'right', '\x15': 'kill-before',
           '\x0b': 'kill-after', '\x17': 'kill-word', '\x03': 'ctrl-c', '\x04': 'ctrl-d',
           '\x00': 'ctrl-space', '\x16': 'ctrl-v', '\x0c': 'ctrl-l', '\x10': 'up', '\x0e': 'down'}
CSI_FINAL = {'A': 'up', 'B': 'down', 'C': 'right', 'D': 'left', 'H': 'home', 'F': 'end', 'Z': 'shift-tab'}
CSI_TILDE = {'1': 'home', '7': 'home', '4': 'end', '8': 'end', '3': 'delete', '5': 'page-up', '6': 'page-down'}


def parse_keys(s, final=False):
    """Terminal input → ([(kind, value)], rest). kind is 'key', 'text' or
    'paste'. A lone ESC stays in rest unless final (no more input came)."""
    events, i, n = [], 0, len(s)
    text = ''

    def push(kind, value):
        nonlocal text
        if text:
            events.append(('text', text))
            text = ''
        events.append((kind, value))

    while i < n:
        c = s[i]
        if s.startswith('\x1b[200~', i):
            end = s.find('\x1b[201~', i + 6)
            if end < 0:
                break
            push('paste', s[i + 6:end])
            i = end + 6
            continue
        if c == '\x1b':
            if i + 1 >= n:
                if final:
                    push('key', 'esc')
                    i += 1
                break
            nxt = s[i + 1]
            if nxt == '[':
                j = i + 2
                while j < n and not ('@' <= s[j] <= '~'):
                    j += 1
                if j >= n:
                    break
                params, fin = s[i + 2:j], s[j]
                i = j + 1
                if fin == 'u' and params.startswith('?'):
                    push('keyboard', params[1:])
                    continue
                mods = params.split(';')
                if fin != 'u' and len(mods) > 1 and mods[1].split(':')[1:] == ['3']:
                    continue
                if fin == 'u' or (fin == '~' and mods[0] == '27'):
                    code = mods[-1] if fin == '~' else mods[0].split(':')[0]
                    modifier = mods[1].split(':') if len(mods) > 1 else ['1']
                    try:
                        bits = int(modifier[0] or '1') - 1
                        event = int(modifier[1]) if len(modifier) > 1 else 1
                        number = int(code)
                    except ValueError:
                        continue
                    if event == 3:
                        if number == 32: push('key-up', 'space')
                        elif number in (57442, 57448): push('key-up', 'control')
                        elif number == 27: push('key-up', 'escape')
                        continue
                    kind = 'key-repeat' if event == 2 else 'key'
                    if number == 32 and bits & 4:
                        push(kind, 'voice-chat' if bits & 1 else 'ctrl-space')
                    elif number in (9, 13, 27, 127):
                        name = {9:'shift-tab' if bits & 1 else 'tab', 13:'newline' if bits else 'enter', 27:'esc', 127:'backspace'}[number]
                        push(kind, name)
                    elif bits & 2 and number in (98, 102, 127):
                        push(kind, {98:'word-left', 102:'word-right', 127:'kill-word'}[number])
                    elif bits & 4 and 97 <= number <= 122:
                        control = CONTROL.get(chr(number - 96))
                        if control: push(kind, control)
                    elif 32 <= number < 0xD800 or 0xDFFF < number < 0xE000 or 0xF900 <= number <= 0x10FFFF:
                        if not bits & (4 | 2):
                            char = chr(number)
                            push('text', char.upper() if bits & 1 else char)
                    continue
                if fin == '~':
                    if mods[0] in CSI_TILDE: push('key', CSI_TILDE[mods[0]])
                    continue
                name = CSI_FINAL.get(fin)
                if name:
                    if len(mods) > 1 and mods[1].split(':')[0] in ('3', '5') and name in ('left', 'right'):
                        name = 'word-' + name
                    push('key', name)
                continue
            if nxt == 'O':
                if i + 2 >= n:
                    break
                name = CSI_FINAL.get(s[i + 2])
                if name: push('key', name)
                i += 3
                continue
            i += 2
            if nxt in '\r\n': push('key', 'newline')
            elif nxt in '\x7f\x08': push('key', 'kill-word')
            elif nxt == 'b': push('key', 'word-left')
            elif nxt == 'f': push('key', 'word-right')
            elif nxt == '\x1b': push('key', 'esc'); i -= 1
            continue
        if c in CONTROL:
            push('key', CONTROL[c])
        elif ord(c) >= 32:
            text += c
        i += 1
    if text:
        events.append(('text', text))
    return events, s[i:]


# ── Clipboard and microphone ────────────────────────────────────────────
def run_quietly(argv, timeout=3):
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return b''


def clipboard():
    """('image', mime, bytes), ('text', str) or None."""
    wayland = os.environ.get('WAYLAND_DISPLAY') and shutil.which('wl-paste')
    xclip = os.environ.get('DISPLAY') and shutil.which('xclip')
    if wayland:
        types = run_quietly(['wl-paste', '--list-types']).decode(errors='replace').split()
    elif xclip:
        types = run_quietly(['xclip', '-selection', 'clipboard', '-t', 'TARGETS', '-o']).decode(errors='replace').split()
    else:
        return None
    for mime in ('image/png', 'image/jpeg', 'image/webp', 'image/gif'):
        if mime in types:
            data = run_quietly(['wl-paste', '-t', mime] if wayland else ['xclip', '-selection', 'clipboard', '-t', mime, '-o'], 10)
            if data:
                return ('image', mime, data)
    data = run_quietly(['wl-paste', '-n'] if wayland else ['xclip', '-selection', 'clipboard', '-o'])
    return ('text', data.decode(errors='replace')) if data else None


def image_file(text):
    """A pasted path (or file:// URL) of an image → (mime, bytes), else None."""
    path = text.strip().strip('\'"')
    if path.startswith('file://'):
        from urllib.parse import unquote
        path = unquote(path[7:])
    if '\n' in path or not path:
        return None
    p = Path(path).expanduser()
    mime = IMAGE_SUFFIXES.get(p.suffix.lower())
    try:
        if mime and p.is_file() and p.stat().st_size <= IMAGE_MOST:
            return mime, p.read_bytes()
    except OSError:
        pass
    return None


class Recorder:
    """A WAV recording of the microphone, 16 kHz mono, by whichever tool
    the computer has."""

    TOOLS = [
        ('pw-record', lambda f: ['pw-record', '--rate', '16000', '--channels', '1', '--format', 's16', f]),
        ('parecord', lambda f: ['parecord', '--rate=16000', '--channels=1', '--format=s16le', '--file-format=wav', f]),
        ('arecord', lambda f: ['arecord', '-q', '-f', 'S16_LE', '-r', '16000', '-c', '1', '-t', 'wav', f]),
    ]

    def __init__(self):
        self.process, self.path, self.started = None, None, 0.0

    @classmethod
    def available(cls):
        return any(shutil.which(name) for name, _ in cls.TOOLS)

    def start(self):
        for name, argv in self.TOOLS:
            if shutil.which(name):
                fd, self.path = tempfile.mkstemp(prefix='mike-voice-', suffix='.wav')
                os.close(fd)
                self.process = subprocess.Popen(argv(self.path), stdin=subprocess.DEVNULL,
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.started = time.monotonic()
                return True
        return False

    def seconds(self):
        return int(time.monotonic() - self.started) if self.process else 0

    def stop(self):
        """The recording's bytes (b'' if nothing usable)."""
        process, path = self.process, self.path
        self.process = self.path = None
        if not process:
            return b''
        process.send_signal(signal.SIGINT)
        try:
            process.wait(3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        try:
            data = Path(path).read_bytes()
        except OSError:
            data = b''
        finally:
            try: os.unlink(path)
            except OSError: pass
        return data if len(data) > 44 + 3200 else b''  # under 0.1 s is nothing


# ── The screen ──────────────────────────────────────────────────────────
class Screen:
    def __init__(self, terminal, history=None, fd_in=None, out=None):
        self.terminal = terminal
        self.fd_in = sys.stdin.fileno() if fd_in is None else fd_in
        self.out = out or sys.stdout
        self.history_file = history
        self.editor = Editor(read_history(history) if history else ())
        self.height = 0            # rows the live area holds now
        self.saved = None
        self.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self.pending_input = ''
        self.menu_at = 0
        self.choice = None          # selected option in the question box
        self.choice_key = None
        self.held = None            # a question set aside while words are typed for it
        self.ctrl_c = 0.0
        self.flash = ''             # one-line notice in the footer
        self.flash_until = 0.0
        self.recorder = Recorder()
        self.heard = queue.Queue()
        self.transcribing = False
        self.voice_held = False
        self.key_releases = False
        self.voice_pressed = 0.0
        self.record_mode = ''
        self.audio_epoch = 0
        self.conversation = False
        self.conversation_phase = ''
        self.conversation_last = None
        self.capture_question = None
        self.detector = None
        self.player = Player()
        self.speech_cancel = threading.Event()
        self.resized = False
        self.tick = 0
        self.leave = None

    # Terminal set-up
    def size(self):
        size = shutil.get_terminal_size((80, 24))
        return max(size.columns, 20), max(size.lines, 8)

    def __enter__(self):
        self.saved = termios.tcgetattr(self.fd_in)
        tty.setraw(self.fd_in)
        signal.signal(signal.SIGWINCH, lambda *_: setattr(self, 'resized', True))
        cols, rows = self.size()
        # Push what the shell showed into scrollback: the box starts at the bottom.
        self.write('\x1b[>11u\x1b[?u\x1b[?2004h' + '\r\n' * (rows - 1))
        self.draw()
        self.header()
        return self

    def __exit__(self, *exc):
        self.stop_conversation(quiet=True)
        if self.recorder.process:
            self.recorder.stop()
        self.audio_epoch += 1
        self.clear_live()
        self.write('\x1b[<u\x1b[?2004l\x1b[?25h' + RESET)
        if self.saved:
            termios.tcsetattr(self.fd_in, termios.TCSADRAIN, self.saved)
        signal.signal(signal.SIGWINCH, signal.SIG_DFL)
        write_history(self.history_file, self.editor.history)
        return False

    def write(self, s):
        self.out.write(s)
        self.out.flush()

    def header(self):
        t = self.terminal
        cols, _ = self.size()
        project = t.workspace.replace(str(Path.home()), '~', 1)
        lines = ['', f' {MIKE}✻ Mike{RESET} {DIM}in the terminal{RESET}',
                 f'   {GRAY}{t.owner} · this computer {t.device}{RESET}',
                 f'   {GRAY}{project}{RESET}',
                 f'   {GRAY}The project is context only; Mike asks before it runs anything here.{RESET}',
                 f'   {GRAY}/ for commands · ? for shortcuts{RESET}', '']
        self.print_lines([clip(l, cols) for l in lines])

    # The conversation, above the live area
    def above(self, text):
        self.block('note', text)

    def block(self, kind, text, data=None):
        cols, _ = self.size()
        width = cols - 1
        if kind == 'you':
            lines = ['', f'{YOU}You{RESET}'] + [l for part in text.split('\n')
                                                for l in wrap_spans([(part, '')], width, '  ')]
        elif kind == 'mike':
            lines = ['', f'{MIKE}Mike{RESET}'] + markdown(text, width)
        elif kind == 'step':
            lines = self.step_lines(data or {}, width)
        elif kind in ('ask', 'request'):
            title = 'Mike asks' if kind == 'ask' else 'Mike wants to use this computer'
            lines = ['', f'{YELLOW}?{RESET} {BOLD}{title}{RESET}'] + \
                    wrap_spans([(text, '' if kind == 'ask' else CODE)], width, '  ')
        elif kind == 'answer':
            lines = [f'  {GRAY}⎿ {text}{RESET}']
        elif kind == 'error':
            lines = wrap_spans([(text, RED)], width, '  ', f'{RED}✗{RESET} ')
        elif kind == 'done':
            lines = [f'{GRAY}✻ {text}{RESET}']
        else:
            lines = [l for part in text.split('\n') for l in wrap_spans([(part, GRAY)], width, '  ')]
        self.print_lines([clip(l, cols) for l in lines])

    def step_lines(self, step, width):
        head = safe(f"{step.get('app', '') or 'Mike'}: {step.get('say', '')}")
        lines = wrap_spans([(head, '')], width, '    ', f'  {ACCENT}●{RESET} ')
        result = step.get('result')
        if isinstance(result, dict):
            code = result.get('exit_code')
            colour = GRAY if code in (0, None) else RED
            lines.append(f'    {colour}⎿ {safe(result.get("state"))}; exit {safe(code)}{RESET}')
            for name in ('stdout', 'stderr'):
                body = safe(result.get(name) or '').rstrip('\n')
                if not body:
                    continue
                shown = body.split('\n')
                more = len(shown) - 40
                for l in shown[:40]:
                    lines.append('      ' + (RED if name == 'stderr' else GRAY) + clip(l, width - 6) + RESET)
                if more > 0:
                    lines.append(f'      {GRAY}… {more} more lines{RESET}')
        return lines

    def print_lines(self, lines):
        """Write lines into the conversation, just above the live area,
        scrolling older ones up into the terminal's scrollback."""
        if not lines:
            return
        cols, rows = self.size()
        top = rows - self.height + 1
        k = len(lines)
        scrolled = max(0, top + k - rows)
        self.write(f'\x1b[?2026h\x1b[{top};1H\x1b[J' + '\r\n'.join(lines) + '\r\n'
                   + f'\x1b[{rows};1H' + '\n' * (k - scrolled))
        self.draw()

    def clear_live(self):
        if self.height:
            _, rows = self.size()
            self.write(f'\x1b[{rows - self.height + 1};1H\x1b[J')
            self.height = 0

    # The live area
    def question(self):
        """The decision Mike waits for, as a box: title, body, options."""
        t = self.terminal
        view, work = t.view, t.local
        if work and work.get('approval_id') and self.held != 'file:' + work['approval_id'][:12]:
            key = work['approval_id'][:12]
            what = work.get('argv') or work.get('file') or work.get('goal')
            body = shlex.join(what) if isinstance(what, list) and all(isinstance(x, str) for x in what) else json.dumps(what)
            body = safe(body)
            where = safe(work.get('cwd') or t.workspace).replace(str(Path.home()), '~', 1)
            if work.get('kind') == 'file' and work.get('stage') == 'awaiting_approval':
                return ('file:' + key, 'Mike wants to look for a file', [body, 'in ' + where],
                        [('Search this project for…', '/find ' + key + ' '), ('No', '/decline ' + key)])
            return ('local:' + key, 'Mike wants to run this on your computer', ['$ ' + body, 'in ' + where],
                    [('Yes, run it', '/approve ' + key), ('No', '/decline ' + key)])
        nonce = view.get('confirmation_id')
        if view.get('stage') == 'confirm' and isinstance(nonce, str):
            return ('ask:' + nonce[:12], 'Mike asks', [safe(view.get('say', ''))],
                    [('Yes', '/yes ' + nonce[:12]), ('No', '/no ' + nonce[:12])])
        return None

    def menu(self):
        text = self.editor.text
        if not text.startswith('/') or ' ' in text or '\n' in text:
            return []
        exact = [c for c in COMMANDS if c[0].startswith(text)]
        loose = [c for c in COMMANDS if c not in exact and text[1:] in c[0]]
        return exact + loose

    def working(self):
        t = self.terminal
        return bool(t.turn) and not t.finished

    def live(self, cols):
        """(lines, caret (row, col) or None)."""
        t = self.terminal
        lines, caret = [], None
        for waiting in t.waiting[:3]:
            first = waiting.split('\n')[0]
            lines.append(clip(f'  {GRAY}↳ queued: {first}{RESET}', cols - 1))
        if len(t.waiting) > 3:
            lines.append(f'  {GRAY}↳ … {len(t.waiting) - 3} more queued{RESET}')
        q = self.question()
        if q:
            lines.append(f'{YELLOW}✻ Waiting for your answer{RESET}')
        elif self.working():
            spin = SPINNER[self.tick % len(SPINNER)]
            secs = int(time.monotonic() - t.started) if t.started else 0
            doing = safe(t.doing())
            hint = ' · esc to stop the command' if t.running() else ''
            lines.append(clip(f'{ACCENT}{spin} {doing}…{RESET} {GRAY}({secs}s{hint}){RESET}', cols - 1))
        rule = GRAY + '─' * (cols - 1) + RESET
        if q:
            key, title, body, options = q
            if key != self.choice_key:
                self.choice_key, self.choice = key, None
            inner = cols - 5
            lines.append(GRAY + '╭' + '─' * (cols - 3) + '╮' + RESET)
            box = [f'{YELLOW}{BOLD}{title}{RESET}', '']
            for b in body:
                box += wrap_spans([(b, CODE if b.startswith('$ ') else '')], inner)[:8]
            box.append('')
            for i, (label, _) in enumerate(options):
                mark = f'{ACCENT}❯{RESET}' if self.choice == i else ' '
                style = ACCENT + BOLD if self.choice == i else ''
                box.append(f'{mark} {style}{i + 1}. {label}{RESET}')
            box.append('')
            box.append(f'{GRAY}↑/↓ or 1-{len(options)} to choose, enter to confirm{RESET}')
            for b in box:
                pad = inner - visible_width(b)
                lines.append(GRAY + '│ ' + RESET + clip(b, inner) + ' ' * max(0, pad) + GRAY + ' │' + RESET)
            lines.append(GRAY + '╰' + '─' * (cols - 3) + '╯' + RESET)
        else:
            lines.append(rule)
            width = cols - 3
            rows, r, col = self.editor.where(width)
            first = max(0, min(r - MOST_INPUT_LINES + 1, len(rows) - MOST_INPUT_LINES))
            for i, (s, e, _) in enumerate(rows[first:first + MOST_INPUT_LINES]):
                lead = f'{ACCENT}❯{RESET} ' if first + i == 0 else '  '
                piece = self.editor.text[s:e]
                piece = re.sub(r'\[Image #\d+\]', lambda m: CODE + m.group() + RESET, piece)
                lines.append(lead + piece)
            if not self.editor.text and not self.recorder.process:
                lines[-1] = f'{ACCENT}❯{RESET} {GRAY}Ask Mike anything{RESET}'
            caret = (len(lines) - 1 - (min(len(rows), first + MOST_INPUT_LINES) - 1 - r), 2 + col)
            lines.append(rule)
        below = self.below(cols)
        lines += below
        return lines, caret

    def below(self, cols):
        t = self.terminal
        menu = self.menu() if not self.question() else []
        if menu:
            self.menu_at = min(self.menu_at, len(menu) - 1)
            start = max(0, self.menu_at - 7)
            out = []
            for i, (name, args, what) in enumerate(menu[start:start + 8], start):
                left = (name + (' ' + args if args else '')).ljust(18)
                if i == self.menu_at:
                    out.append(clip(f'  {ACCENT}{BOLD}{left}{RESET} {what}', cols - 1))
                else:
                    out.append(clip(f'  {left} {GRAY}{what}{RESET}', cols - 1))
            return out
        if self.editor.text == '?':
            return [clip(f'  {ACCENT}{k.ljust(28)}{RESET} {GRAY}{v}{RESET}', cols - 1) for k, v in SHORTCUTS]
        # Footer
        left = []
        if self.recorder.process:
            secs = self.recorder.seconds()
            hint = 'Space / Enter / ctrl+space to stop' if self.record_mode == 'latched' else 'release after holding · quick tap keeps recording'
            if self.conversation: hint = 'conversation · pause to send · ctrl+space to end'
            left.append(f'{RED}● REC {secs // 60}:{secs % 60:02d}{RESET} {GRAY}{hint}{RESET}')
        elif self.transcribing:
            left.append(f'{YELLOW}transcribing…{RESET}')
        if self.conversation:
            left.append(f'{ACCENT}voice chat: {self.conversation_phase} · {IDLE_SECONDS}s silence timeout{RESET}')
        if self.flash and time.monotonic() < self.flash_until:
            left.append(f'{YELLOW}{self.flash}{RESET}')
        status = t.status or {}
        if status.get('chat_version') == 2:
            status = getattr(t, 'options', None) or status
            if t.turn and not t.finished and t.view.get('model'):
                status = {key: t.view.get(key) for key in ('model','thinking','mode')}
        for name, label in (('model', 'model'), ('thinking', 'thinking'), ('mode', 'mode')):
            if status.get(name):
                value = status[name]
                if name == 'model':
                    value = next((m.get('label', m['id']) for m in getattr(t, 'settings', {}).get('models', []) if m['id'] == value), value)
                left.append(f'{ACCENT}{label}: {value}{RESET}')
        project = t.workspace.replace(str(Path.home()), '~', 1)
        left.append(f'{GRAY}{t.owner} · {project}{RESET}')
        if t.waiting:
            left.append(f'{GRAY}{len(t.waiting)} queued{RESET}')
        if self.editor.images:
            used = self.editor.images_used()
            if used:
                left.append(f'{CODE}{len(used)} image{"s" * (len(used) > 1)}{RESET}')
        right = f'{GRAY}? for shortcuts{RESET}'
        line = '  ' + f' {GRAY}·{RESET} '.join(left)
        gap = cols - 1 - visible_width(line) - visible_width(right)
        return [clip(line + ' ' * gap + right if gap > 1 else line, cols - 1)]

    def draw(self, force=False):
        cols, rows = self.size()
        lines, caret = self.live(cols)
        lines = lines[-(rows - 1):]
        h = len(lines)
        out = ['\x1b[?2026h\x1b[?25l']
        if h > self.height:
            out.append(f'\x1b[{rows};1H' + '\n' * (h - self.height))
        old_top = rows - max(self.height, h) + 1
        top = rows - h + 1
        out.append(f'\x1b[{old_top};1H\x1b[J')
        out.append(f'\x1b[{top};1H' + '\r\n'.join(lines))
        if caret:
            out.append(f'\x1b[{top + caret[0]};{caret[1] + 1}H\x1b[?25h')
        out.append('\x1b[?2026l')
        self.height = h
        self.write(''.join(out))

    def notice(self, text, seconds=4):
        self.flash, self.flash_until = text, time.monotonic() + seconds

    # Keys
    def handle(self, kind, value):
        if kind == 'keyboard':
            try: self.key_releases = int(value) & 2 != 0
            except ValueError: pass
            return
        if kind == 'key-up':
            if value == 'escape':
                self.stop_conversation(quiet=True)
                if self.recorder.process: self.finish_recording(discard=True)
                return
            if value in ('space', 'control') and self.voice_held:
                self.voice_held = False
                if self.recorder.process:
                    if time.monotonic()-self.voice_pressed < TAP_SECONDS:
                        self.record_mode = 'latched'
                    else:
                        self.finish_recording()
            return
        if kind == 'key-repeat':
            if value in ('ctrl-space', 'voice-chat'): return
            kind = 'key'
        if kind == 'key' and value == 'voice-chat':
            if self.conversation:self.stop_conversation()
            else:self.start_conversation()
            return
        if kind == 'key' and value == 'ctrl-space':
            if self.conversation:
                self.stop_conversation()
            elif self.recorder.process and self.record_mode == 'latched':
                self.finish_recording()
            elif not self.key_releases:
                self.notice('Recording shortcuts need key releases; open an updated Console/IDE window')
            elif not self.voice_held and not self.transcribing:
                self.voice_held = True
                self.voice_pressed = time.monotonic()
                self.start_recording('pending')
            return
        if self.recorder.process and self.record_mode == 'latched' and ((kind == 'key' and value == 'enter') or (kind == 'text' and value == ' ')):
            self.finish_recording()
            return
        if self.transcribing and kind == "key" and value == "enter":
            self.notice("Wait for transcription before sending")
            return
        e = self.editor
        cols, _ = self.size()
        width = cols - 3
        q = self.question()
        if q and kind == 'key' and value not in ('ctrl-c', 'ctrl-d', 'ctrl-l'):
            options = q[3]
            if value in ('up', 'shift-tab'):
                self.choice = (len(options) - 1) if self.choice is None else max(0, self.choice - 1)
            elif value in ('down', 'tab'):
                self.choice = 0 if self.choice is None else min(len(options) - 1, self.choice + 1)
            elif value == 'enter' and self.choice is not None:
                command = options[self.choice][1]
                self.choice = None
                if command.endswith(' '):  # needs words: into the box
                    self.held = q[0]
                    e.set(command)
                    self.notice('type what to look for, then enter')
                else:
                    self.run(command, echo=False)
            return
        if q and kind == 'text':
            options = q[3]
            for c in value:
                if c.isdigit() and 1 <= int(c) <= len(options):
                    self.choice = int(c) - 1
            return
        if q and kind == 'paste':
            return
        if kind == 'text':
            e.insert(value)
            self.menu_at = 0
        elif kind == 'paste':
            image = image_file(value)
            if image:
                n = e.add_image(*image)
                self.notice(f'Image #{n} attached')
            else:
                e.insert(safe(value.replace('\r\n', '\n').replace('\r', '\n').replace('\t', '    ')))
        elif value == 'enter':
            menu = self.menu()
            if menu and e.text != menu[min(self.menu_at, len(menu) - 1)][0]:
                name, args, _ = menu[min(self.menu_at, len(menu) - 1)]
                if args:
                    e.set(name + ' ')
                    return
                e.set(name)
            if e.text.endswith('\\') and e.caret == len(e.text):
                e.text = e.text[:-1]
                e.caret -= 1
                e.insert('\n')
                return
            self.submit()
        elif value == 'newline':
            e.insert('\n')
        elif value == 'shift-tab':
            modes = ('ask', 'plan', 'auto')
            options = getattr(self.terminal, 'options', {'mode':'ask'})
            mode = modes[(modes.index(options.get('mode','ask')) + 1) % len(modes)]
            self.run('/mode '+mode, echo=False)
        elif value == 'tab':
            menu = self.menu()
            if menu:
                name, args, _ = menu[min(self.menu_at, len(menu) - 1)]
                e.set(name + (' ' if args else ''))
        elif value == 'backspace': e.backspace()
        elif value == 'delete': e.delete()
        elif value == 'left': e.left()
        elif value == 'right': e.right()
        elif value == 'home': e.home()
        elif value == 'end': e.end()
        elif value == 'word-left': e.word_left()
        elif value == 'word-right': e.word_right()
        elif value == 'kill-before': e.kill_before()
        elif value == 'kill-after': e.kill_after()
        elif value == 'kill-word': e.kill_word()
        elif value in ('up', 'down'):
            menu = self.menu()
            if menu:
                self.menu_at = (self.menu_at + (1 if value == 'down' else -1)) % len(menu)
            elif not e.vertical(width, -1 if value == 'up' else 1):
                e.older() if value == 'up' else e.newer()
        elif value == 'esc':
            if self.menu():
                e.clear()
            elif self.terminal.running():
                self.run('/stop', echo=False)
        elif value == 'ctrl-c':
            if e.text:
                e.clear()
                self.ctrl_c = 0.0
            elif time.monotonic() - self.ctrl_c < 2:
                self.leave = 130
            else:
                self.ctrl_c = time.monotonic()
                self.notice('Press ctrl+c again to leave', 2)
        elif value == 'ctrl-d':
            if not e.text:
                self.leave = 0
            else:
                e.delete()
        elif value == 'ctrl-l':
            self.clear_screen()
        elif value == 'ctrl-v':
            got = clipboard()
            if not got:
                self.notice('The clipboard is empty (or no wl-paste/xclip here)')
            elif got[0] == 'image':
                n = e.add_image(got[1], got[2])
                self.notice(f'Image #{n} attached')
            else:
                image = image_file(got[1])
                if image:
                    n = e.add_image(*image)
                    self.notice(f'Image #{n} attached')
                else:
                    e.insert(safe(got[1].replace('\r\n', '\n').replace('\t', '    ')))

    def clear_screen(self):
        self.write('\x1b[2J\x1b[3J\x1b[H')
        _, rows = self.size()
        self.height = 0
        self.write('\r\n' * (rows - 1))
        self.header()

    def voice(self):
        # Kept for the existing recorder diagnostics and duration bound.
        if self.recorder.process:self.finish_recording()
        else:self.start_recording('pending')

    def start_recording(self, mode):
        if self.transcribing:
            self.notice('Still transcribing the last recording')
            return False
        if not Recorder.available():
            self.notice('No recorder here: install pipewire, pulseaudio-utils or alsa-utils')
            return False
        if not self.recorder.start():
            self.notice('Could not start recording')
            return False
        self.audio_epoch += 1
        self.record_mode = mode
        if mode == 'conversation':
            self.detector = Detector()
            q = self.question()
            self.capture_question = q[0] if q else None
            self.conversation_phase = 'listening'
        return True

    def finish_recording(self, discard=False):
        if not self.recorder.process:return
        mode = self.record_mode
        self.record_mode = ''
        data = self.recorder.stop()
        if discard:
            self.audio_epoch += 1
            self.voice_held = False
            return
        if not data:
            self.notice('Nothing was recorded')
            return
        self.transcribing = True
        if mode == 'conversation':self.conversation_phase = 'transcribing'
        threading.Thread(target=self.transcribe,args=(data,self.audio_epoch,mode=='conversation',self.capture_question),daemon=True).start()

    def transcribe(self, data, epoch=None, conversation=False, question=None):
        try:
            answer = self.terminal.service.call('/v1/chat/hear', {
                'audio_base64':base64.b64encode(data).decode(), 'format':'wav'},timeout=45)
            words = str(answer.get('text') or '').strip()
            if epoch is None:self.heard.put(('text',words))
            else:self.heard.put(('transcript',{'epoch':epoch,'conversation':conversation,'question':question,'text':words}))
        except Exception as error:
            if epoch is None:self.heard.put(('error','Could not hear it: '+str(error)))
            else:self.heard.put(('audio-error',{'epoch':epoch,'text':'Could not hear it: '+str(error)}))

    def start_conversation(self):
        if not self.terminal.status.get("voice_chat"):
            self.notice("Upgrade Mike service for spoken conversation")
            return
        if not self.key_releases:
            self.notice('Conversation needs key releases; open an updated Console/IDE window')
            return
        if self.recorder.process or self.transcribing:
            self.notice("Finish the current recording/transcript before starting conversation")
            return
        if not Recorder.available() or not Player.available():
            self.notice('Conversation needs a microphone recorder and a local audio player')
            return
        self.audio_epoch += 1
        self.transcribing = False
        self.conversation = True
        self.conversation_phase = 'idle'
        self.conversation_last = ('reply',self.terminal.turn) if self.terminal.finished else None
        self.speech_cancel = threading.Event()
        self.notice('Conversation on · ctrl+space or ctrl+shift+space to end')

    def stop_conversation(self, quiet=False):
        was_on = self.conversation
        self.conversation = False
        self.conversation_phase = ''
        self.speech_cancel.set()
        self.player.stop()
        if was_on:
            if self.recorder.process:self.finish_recording(discard=True)
            self.audio_epoch += 1
            self.transcribing = False
            self.voice_held = False
            if not quiet:self.notice('Conversation off')

    def request_speech(self, text, identity):
        self.conversation_last = identity
        self.conversation_phase = 'speaking'
        epoch,cancel = self.audio_epoch,self.speech_cancel
        def speak():
            try:
                started = self.terminal.service.call('/v1/chat/read',{'text':safe(text,16384)},timeout=15)
                end = time.monotonic()+90
                while not cancel.is_set() and time.monotonic()<end:
                    sound = self.terminal.service.call('/v1/chat/spoken',{'reading_id':started['reading_id']},timeout=15)
                    if sound.get('stage')=='ready':
                        self.heard.put(('speech',{'epoch':epoch,'sound':sound}))
                        return
                    if sound.get('stage')!='speaking':raise ValueError(sound.get('reason') or 'speech failed')
                    cancel.wait(.2)
                if not cancel.is_set():raise ValueError('speech timed out')
            except Exception as error:
                self.heard.put(('audio-error',{'epoch':epoch,'text':'Could not speak: '+str(error)}))
        threading.Thread(target=speak,daemon=True).start()

    def audio_event(self, kind, payload):
        if kind in ('text','error'):
            self.transcribing = False
            if kind=='text' and payload:self.editor.insert(safe(payload.replace('\n',' ')))
            elif kind=='error':self.block('error',payload)
            return
        if payload['epoch'] != self.audio_epoch:return
        if kind=='speech':
            if not self.conversation:return
            try:self.player.start(payload['sound']['audio_base64'],payload['sound']['format'])
            except Exception as error:
                self.stop_conversation();self.block('error',str(error))
            return
        self.transcribing = False
        if kind=='audio-error':
            self.stop_conversation();self.block('error',payload['text'])
            return
        words = safe(payload['text'].replace('\n',' '))
        if not payload['conversation']:
            if words:self.editor.insert(words)
            else:self.notice('Mike heard nothing')
            return
        if not self.conversation:return
        self.conversation_phase = 'idle'
        if not words:return
        q = self.question()
        if payload['question']:
            if not q or q[0]!=payload['question']:
                self.stop_conversation();self.notice('The approval question changed; answer it again')
                return
            word = words.casefold().strip(' .!?')
            choice = 0 if word in ('yes','evet') else -1 if word in ('no','hayır','hayir') else None
            if choice is None or q[3][choice][1].endswith(' '):
                self.notice('Say yes/evet or no/hayır for this exact question')
                return
            self.run(q[3][choice][1],echo=False)
        elif q:
            self.stop_conversation();self.notice('An approval arrived; answer the displayed question')
        else:
            try:self.terminal.ask(words)
            except Exception as error:
                self.stop_conversation();self.block('error',str(error))

    def audio_tick(self, now=None):
        now = time.monotonic() if now is None else now
        if self.recorder.process and self.recorder.seconds()>=RECORD_MOST:
            self.finish_recording()
        if not self.conversation:return
        if self.player.process:
            code = self.player.process.poll()
            if code is None:return
            self.player.stop()
            if code:
                self.stop_conversation();self.notice('Audio playback failed; conversation stopped')
                return
            self.conversation_phase = 'idle'
        if self.transcribing or self.conversation_phase=='speaking':return
        t = self.terminal
        q = self.question()
        identity = ('question',q[0]) if q else ('reply',t.turn)
        if self.recorder.process:
            if self.working() and not q:
                self.finish_recording(discard=True)
                self.conversation_phase = 'working'
                return
            try:activity = self.detector.inspect(self.recorder.path,now)
            except Exception as error:
                self.stop_conversation();self.block('error',str(error));return
            if activity=='idle':
                self.stop_conversation();self.notice('Conversation off after 30 seconds without speech')
            elif activity=='utterance':self.finish_recording()
            return
        if q and identity!=self.conversation_last:
            self.request_speech(q[1]+'. '+'. '.join(q[2])+'. Say yes or no.',identity)
            return
        if not q and self.working():
            self.conversation_phase = 'working'
            return
        if not q and t.finished and identity!=self.conversation_last:
            self.request_speech(t.view.get('reply') or t.view.get('say') or t.view.get('stage','Done'),identity)
            return
        if not self.start_recording('conversation'):
            self.stop_conversation()

    def submit(self):
        e = self.editor
        text = e.text
        if not text.strip() or text == '?':
            e.clear()
            return
        if e.images_used() and not text.startswith('/'):
            from images import attachment
            from mike_service import ServiceError
            try:
                attachments = [attachment(*e.images[n]) for n in e.images_used()]
                self.terminal.ask(text, images=attachments)
            except (ValueError, ServiceError) as error:
                self.block('error', str(error))
                return
            e.remember(text)
            e.clear()
            self.held = None
            return
        e.remember(text)
        e.clear()
        self.held = None
        self.run(text, echo=True)

    def run(self, line, echo=True):
        t = self.terminal
        if line.strip() == '/clear':
            self.clear_screen()
            return
        try:
            if t.command(line) == 'quit':
                self.leave = 0
        except ValueError as error:
            self.block('error', str(error))

    # The loop
    def loop(self, wait, once=False):
        t = self.terminal
        deadline = time.monotonic() + wait
        next_poll = 0.0
        while self.leave is None:
            now = time.monotonic()
            if now >= next_poll:
                next_poll = now + 1
                try:
                    result = t.poll()
                except t.Unavailable:
                    t.attach()
                    if t.turn: t.resume(t.turn)
                    result = None
                if result is not None:
                    return result
                if once and t.finished:
                    self.draw()
                    return 0 if t.view.get('stage') == 'done' else 1
                if t.turn and not t.finished and now > deadline:
                    t.show(f'Timed out waiting; turn {t.turn} may still be active. Use --resume; no request was replayed.')
                    return 4
            self.audio_tick(now)
            try:
                kind, payload = self.heard.get_nowait()
                self.audio_event(kind,payload)
            except queue.Empty:
                pass
            ready, _, _ = select.select([self.fd_in], [], [], 0.1)
            if ready:
                try:
                    data = os.read(self.fd_in, 65536)
                except OSError:
                    data = b''
                if not data:
                    return 0
                self.pending_input += self.decoder.decode(data)
                if self.pending_input == '\x1b' or self.pending_input.endswith('\x1b'):
                    more, _, _ = select.select([self.fd_in], [], [], 0.03)
                    if more:
                        continue
                events, self.pending_input = parse_keys(self.pending_input, final=True)
                for kind, value in events:
                    before = t.turn
                    self.handle(kind, value)
                    if t.turn != before:
                        deadline = time.monotonic() + wait
                    if self.leave is not None:
                        break
            if self.resized:
                self.resized = False
                cols, rows = self.size()
                self.write(f'\x1b[{max(1, rows - self.height - 2)};1H\x1b[J')
                self.height = 0
            self.tick += 1
            self.draw()
        return self.leave
