#!/usr/bin/python3
"""Mike in a terminal: talk with Mike, sign in and out, on the shared Mike
service; no agent or tool loop of its own."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import unicodedata

from mike_service import MikeService, ServiceError, ServiceUnavailable
import sign_in

PROMPT = 'mike> '
# How many messages may wait while Mike works on one.
QUEUE_MOST = 10


def plain(value, limit=16384):
    """Model/tool text cannot send terminal control sequences."""
    value = str(value)
    safe = ''.join(c for c in value if c in '\n\t' or not unicodedata.category(c).startswith('C'))
    return safe[:limit] + ('\n[display truncated]' if len(safe) > limit else '')


def history_path():
    base = os.environ.get('XDG_STATE_HOME') or str(Path.home() / '.local/state')
    return Path(base) / 'mike-cli' / 'history'


class Prompt:
    """The line being typed, read by readline in a thread of its own: a caret
    that moves, line editing, ↑/↓ through earlier messages (kept between
    runs). What Mike says is printed above it, and the line comes back as it
    was."""

    def __init__(self, output=sys.stdout, history=None):
        import readline
        self.readline, self.output, self.history = readline, output, history
        self.lines = queue.Queue()
        if history:
            try:
                readline.read_history_file(str(history))
            except OSError:
                pass
        readline.set_history_length(500)
        self.thread = threading.Thread(target=self.read, daemon=True)

    def start(self):
        self.thread.start()

    def read(self):
        while True:
            try:
                line = input(PROMPT)
            except (EOFError, OSError, ValueError):
                self.lines.put(None)
                return
            self.lines.put(line)

    def above(self, text):
        typed = self.readline.get_line_buffer()
        self.output.write('\r\033[K' + text + '\n' + PROMPT + typed)
        self.output.flush()

    def get(self, timeout):
        """The next line typed; None at end of input; '' when none came in time."""
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            return ''
        return None if line is None else line + '\n'

    def save(self):
        if not self.history:
            return
        try:
            self.history.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.readline.write_history_file(str(self.history))
            os.chmod(self.history, 0o600)
        except OSError:
            pass


class Terminal:
    def __init__(self, service, workspace, interactive, output=sys.stdout):
        self.service, self.workspace, self.interactive, self.output = service, workspace, interactive, output
        self.owner = ''
        self.turn = ''
        self.view = {}
        self.local = None
        self.shown = set()
        self.steps = 0
        self.finished = False
        self.prompt = None
        self.waiting = []

    def show(self, text):
        if self.prompt:
            self.prompt.above(plain(text))
        else:
            print(plain(text), file=self.output, flush=True)

    def attach(self, token=None):
        state = self.service.attach(token, connect='auto' if token else 'none')
        if self.owner and state.get('account') != self.owner:
            raise ServiceError(409, {'error':'account_changed', 'message':'account changed; start a new CLI session'})
        if state.get('connection') != 'connected' or not state.get('account'):
            raise ServiceError(409, {'error':'signed_out', 'message':'not signed in; mike-cli --sign-in opens your browser to sign in (headless enrollment alone is not a chat login)'})
        if state.get('chat_version') != 1:
            raise ServiceError(426, {'error':'chat_incompatible', 'message':'upgrade mike-service to one supporting CLI chat version 1'})
        self.owner = state['account']
        self.show(f"Mike: {self.owner}; local device {state.get('device_id', '?')}; project {self.workspace}")
        self.show('Project context grants no tool permission. /help lists commands.')
        return state

    def resume(self, turn):
        view = self.service.call('/v1/chat/resume', {'turn_id':turn})
        self.turn, self.view, self.local = turn, {}, None
        self.steps, self.finished, self.shown = 0, False, set()
        self.render(view)

    def ask(self, text):
        if not text.strip():
            return
        if self.turn and not self.finished:
            if self.interactive and len(self.waiting) < QUEUE_MOST:
                self.waiting.append(text)
                self.show(f'Queued ({len(self.waiting)} waiting): it goes when this turn ends. /unqueue takes them back.')
                return
            self.show('A turn is still active. /leave detaches from it; /stop stops only its current local command.')
            return
        if len(text.encode()) > 16384:
            raise ValueError('message exceeds 16 KiB')
        # Never retry this POST: a lost reply may already have started a turn.
        self.turn = self.service.call('/v1/chat/ask', {'text':text, 'workspace':self.workspace})['turn_id']
        self.steps, self.finished, self.shown, self.view = 0, False, set(), {}
        self.show(f'Turn {self.turn} (resume with --resume {self.turn})')

    def render(self, view):
        if view.get('turn_id') != self.turn:
            raise ValueError('service returned another turn')
        self.view = view
        for step in view.get('steps', [])[self.steps:]:
            self.show(f"{step.get('app', '')}: {step.get('say', '')} [{step.get('class', '')}]")
            result = step.get('result')
            if isinstance(result, dict):
                self.show(f"Result: {result.get('state')}; exit {result.get('exit_code')}")
                for name in ('stdout', 'stderr'):
                    if result.get(name): self.show(f"{name}:\n{result[name]}")
        self.steps = len(view.get('steps', []))
        nonce = view.get('confirmation_id')
        if view.get('stage') == 'confirm' and nonce and nonce not in self.shown:
            self.shown.add(nonce)
            self.show(f"Question: {view.get('say', '')}\n/yes {nonce[:12]} or /no {nonce[:12]}")
        if view.get('stage') in ('done', 'failed') and not self.finished:
            self.show(view.get('reply', '') or view['stage'])
            self.finished = True

    def poll(self):
        state = self.service.state()  # heartbeat this client even while typing
        if state.get('account') != self.owner:
            raise ServiceError(409, {'error':'account_changed', 'message':'account changed; start a new CLI session'})
        if self.turn and not self.finished:
            self.render(self.service.call('/v1/chat/progress', {'turn_id':self.turn}))
        work = state.get('work')
        self.local = None
        if work and work.get('id') == self.view.get('work_id') and work.get('approval_id'):
            self.local = work
            key = work['approval_id']
            if key not in self.shown:
                self.shown.add(key)
                self.show(f"Local request {work['id']} in {work.get('cwd') or self.workspace}:\n{json.dumps(work.get('argv') or work.get('file') or work.get('goal'))}")
                if work['kind'] == 'file' and work['stage'] == 'awaiting_approval':
                    self.show(f'/find {key[:12]} QUERY searches only the current project; /decline {key[:12]} refuses.')
                else:
                    self.show(f'/approve {key[:12]} or /decline {key[:12]}')
        if not self.interactive and (self.local or self.view.get('stage') == 'confirm'):
            self.show(f'Input required in an interactive CLI; resume with --resume {self.turn}. Nothing was approved.')
            return 3
        if self.finished and self.waiting:
            self.finished, self.turn = False, ''
            self.ask(self.waiting.pop(0))
        return None

    @staticmethod
    def matches(given, nonce):
        return isinstance(nonce, str) and len(given) >= 12 and (given == nonce or given == nonce[:12])

    def command(self, line):
        if not line.startswith('/'):
            self.ask(line)
            return
        verb, _, arg = line.partition(' ')
        arg = arg.strip()
        if verb in ('/quit', '/exit'):
            return 'quit'
        if verb in ('/new', '/leave'):
            self.show(f'Left turn {self.turn or "(none)"}; accepted work is not cancelled.')
            self.turn, self.view, self.local, self.finished = '', {}, None, False
        elif verb == '/unqueue':
            if not self.waiting:
                self.show('Nothing is waiting.')
                return
            self.show('Taken back, not sent:\n' + '\n'.join(self.waiting))
            self.waiting = []
        elif verb == '/resume':
            if not arg: raise ValueError('/resume needs a turn id')
            self.resume(arg)
        elif verb in ('/yes', '/no'):
            nonce = self.view.get('confirmation_id')
            if self.view.get('stage') != 'confirm' or not self.matches(arg, nonce):
                self.show('No matching displayed question; read its current id. Nothing was approved.')
                return
            self.view = {}  # consume before sending, including on a lost reply
            self.service.call('/v1/chat/confirm', {'turn_id':self.turn, 'yes':verb == '/yes', 'confirmation_id':nonce})
        elif verb in ('/approve', '/decline', '/find'):
            given, _, query = arg.partition(' ')
            work = self.local
            if not work or not self.matches(given, work.get('approval_id')):
                self.show('No matching displayed local request. Nothing was approved.')
                return
            self.local = None
            if verb == '/find' and (work['kind'] != 'file' or not query.strip()):
                raise ValueError('/find needs the displayed file request id and a query')
            if verb == '/approve' and work['kind'] == 'file' and work['stage'] == 'awaiting_approval':
                raise ValueError('use /find ID QUERY to choose the project search, or /decline ID')
            self.service.claim(work['approval_id'])
            fields = {'folder':self.workspace, 'query':query} if verb == '/find' else {}
            self.service.answer(work['approval_id'], 'decline' if verb == '/decline' else 'approve', **fields)
        elif verb == '/stop':
            work = self.service.state().get('work')
            if not work or work.get('id') != self.view.get('work_id') or work.get('stage') != 'running':
                self.show('This turn has no running local command. Other clients were not stopped.')
                return
            self.service.stop(work['id'])
            self.show('Stop requested for this command; waiting for its actual result. This does not cancel future model steps.')
        elif verb == '/help':
            self.show('/yes ID, /no ID: answer the displayed question; /approve ID, /decline ID: local decision\n/find ID QUERY: file search in this project; /stop: current local command only\n/resume TURN, /leave, /new, /unqueue, /quit. Enter alone never approves; a message typed while Mike works waits its turn.\n↑/↓ earlier messages. Ctrl-C or Ctrl-D detaches; the service stays alive.\nAccount: mike-cli --account, --sign-in, --switch-account, --sign-out.')
        else:
            self.show('Unknown command. /help lists commands.')


def account_command(args, out=sys.stdout):
    """--account, --sign-in, --switch-account, --sign-out: one thing, then exit."""
    if args.sign_in or args.switch_account:
        account, note = sign_in.sign_in(out, switch=args.switch_account)
        print(('' if account else 'mike-cli: ') + note, file=out if account else sys.stderr)
        return 0 if account else 5
    try:
        service = MikeService('cli-account', approvals=False)
        state = service.attach(None, connect='none')
    except ServiceUnavailable:
        print('mike-cli: the Mike service is not running; mike-cli --sign-in starts it', file=sys.stderr)
        return 4
    try:
        if args.sign_out:
            if not state.get('account'):
                print('Not signed in.', file=out)
                return 0
            service.sign_out()
            print('Signed out of ' + state['account'] + ' on this computer (Desktop and the IDE use the same sign-in).', file=out)
            return 0
        if state.get('account') and state.get('connection') == 'connected':
            print('Signed in as ' + state['account'] + '; this computer is ' + str(state.get('device_id', '?')), file=out)
            return 0
        connection = str(state.get('connection') or 'signed out')
        print('Not signed in (' + connection + '); mike-cli --sign-in opens your browser to sign in', file=out)
        return 5
    finally:
        service.detach()


def offer(question):
    """A y/n question on the terminal; Enter alone is yes."""
    try:
        answer = input(question + ' [Y/n] ').strip().lower()
    except EOFError:
        return False
    return answer in ('', 'y', 'yes')


def main(argv=None):
    parser = argparse.ArgumentParser(prog='mike-cli', description=__doc__)
    parser.add_argument('--ask', help='one message, then exit when done')
    parser.add_argument('--resume', metavar='TURN', help='explicitly attach to an existing central turn')
    parser.add_argument('--project', default=os.getcwd())
    parser.add_argument('--sign-in', action='store_true', help='sign in with your codelovesme account, in the browser')
    parser.add_argument('--switch-account', action='store_true', help='sign in, moving this computer to another account')
    parser.add_argument('--sign-out', action='store_true', help='sign this computer out of Mike')
    parser.add_argument('--account', action='store_true', help='who is signed in')
    parser.add_argument('--connect', action='store_true', help='read an account session token with a hidden terminal prompt')
    parser.add_argument('--token-stdin', action='store_true', help='read token from one stdin line (requires --ask or --resume)')
    parser.add_argument('--wait', type=int, default=300, help='maximum seconds waiting for a turn (default 300)')
    version_file = Path(__file__).resolve().parent.parent / 'VERSION'
    version = version_file.read_text().strip() if version_file.is_file() else 'dev'
    parser.add_argument('--version', action='version', version=f'mike-cli {version} (local protocol 1, chat 1)')
    args = parser.parse_args(argv)
    accounts = [args.sign_in, args.switch_account, args.sign_out, args.account]
    if sum(accounts) > 1: parser.error('choose one of --sign-in, --switch-account, --sign-out, --account')
    if any(accounts):
        if args.ask is not None or args.resume or args.connect or args.token_stdin:
            parser.error('an account option runs alone')
        try:
            return account_command(args)
        except KeyboardInterrupt:
            print('\nCancelled; nothing changed.', file=sys.stderr)
            return 130
        except (ServiceError, ServiceUnavailable, OSError) as error:
            print('mike-cli: ' + plain(error), file=sys.stderr)
            return 4
    if not 1 <= args.wait <= 3600: parser.error('--wait must be 1..3600 seconds')
    if args.ask is not None and args.resume: parser.error('choose --ask or --resume')
    if args.connect and args.token_stdin: parser.error('choose --connect or --token-stdin')
    if args.token_stdin and args.ask is None and not args.resume: parser.error('--token-stdin requires --ask or --resume')
    workspace = Path(args.project).resolve()
    if not workspace.is_dir(): parser.error('--project must be an existing directory')
    interactive = sys.stdin.isatty()
    if args.connect and not interactive: parser.error('--connect needs a terminal; use --token-stdin with protected input')
    service = MikeService('cli', approvals=interactive)
    terminal = Terminal(service, str(workspace), interactive)
    prompt = None
    try:
        token = getpass.getpass('Account session token (not saved): ') if args.connect else None
        if args.token_stdin: token = sys.stdin.readline(65537).strip()
        if (args.connect or args.token_stdin) and (not token or len(token) > 65536):
            raise ValueError('provide a nonempty account token of at most 64 KiB')
        # In a terminal, what is missing is offered, never done unasked: the
        # service started, then the browser sign-in.
        if interactive and not token and not sign_in.mike_service.reachable():
            if not offer('The Mike service is not running. Start it?'):
                raise ServiceUnavailable('the Mike service is not running')
            if not sign_in.start_service(sys.stderr):
                raise ServiceUnavailable('the Mike service did not start')
        try:
            terminal.attach(token)
        except ServiceError as error:
            if not (interactive and not token and error.error == 'signed_out'):
                raise
            terminal.show('Not signed in to Mike on this computer.')
            if not offer('Sign in with your codelovesme account in the browser?'):
                raise
            account, note = sign_in.sign_in(sys.stdout)
            terminal.show(note)
            if not account:
                return 5
            terminal.attach()
        token = None
        once = args.ask is not None or bool(args.resume) or not interactive
        if args.resume: terminal.resume(args.resume)
        elif args.ask is not None: terminal.ask(args.ask)
        elif not interactive:
            text = sys.stdin.read(16385)
            if not text.strip(): return 0
            terminal.ask(text)
        if interactive:
            prompt = Prompt(sys.stdout, history_path())
            terminal.prompt = prompt
            prompt.start()
        deadline = time.monotonic() + args.wait
        while True:
            try:
                result = terminal.poll()
            except ServiceUnavailable:
                # Only resume a known id, never retry Ask or any decision.
                terminal.attach()
                if terminal.turn: terminal.resume(terminal.turn)
                result = None
            if result is not None: return result
            if once and terminal.finished: return 0 if terminal.view.get('stage') == 'done' else 1
            if terminal.turn and not terminal.finished and time.monotonic() > deadline:
                terminal.show(f'Timed out waiting; turn {terminal.turn} may still be active. Use --resume; no request was replayed.')
                return 4
            if not interactive:
                time.sleep(1)
                continue
            line = prompt.get(1)
            if line is None:
                terminal.prompt = None
                print('', flush=True)
                return 0
            if line:
                try:
                    if terminal.command(line.rstrip('\n')) == 'quit': return 0
                except ValueError as error:
                    terminal.show(str(error))
                deadline = time.monotonic() + args.wait
    except KeyboardInterrupt:
        terminal.prompt = None
        print('', flush=True)
        terminal.show(f'Detached; service and accepted work continue. Turn: {terminal.turn or "none"}')
        return 130
    except ServiceError as error:
        terminal.prompt = None
        print('mike-cli: ' + plain(error), file=sys.stderr)
        return 5 if error.error in ('session_ended', 'account_changed', 'signed_out', 'account_refused') else 4
    except (ServiceUnavailable, OSError, ValueError) as error:
        terminal.prompt = None
        print('mike-cli: ' + plain(error) + '; start the shared service with mike-service start (or mike-cli --sign-in). Requests are not retried.', file=sys.stderr)
        return 4
    finally:
        if prompt: prompt.save()
        service.detach()


if __name__ == '__main__':
    sys.exit(main())
