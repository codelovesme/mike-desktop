#!/usr/bin/python3
"""Mike in a terminal: talk with Mike, sign in and out, on the shared Mike
service; no agent or tool loop of its own."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import time
import unicodedata

from mike_service import MikeService, ServiceError, ServiceUnavailable, shown_command
import sign_in
import ide_context
import images as image_input

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


class Terminal:
    Unavailable = ServiceUnavailable

    def __init__(self, service, workspace, interactive, output=sys.stdout):
        self.service, self.workspace, self.interactive, self.output = service, workspace, interactive, output
        self.owner = ''
        self.turn = ''
        self.conversation = ''  # '' = the next message starts a new conversation
        self.listed = []        # the conversations /conversations last showed
        self.view = {}
        self.local = None
        self.shown = set()
        self.steps = 0
        self.finished = False
        self.prompt = None     # the full-screen chat, when there is one
        self.waiting = []
        self.status = {}       # the service's last state
        self.device = '?'
        self.started = 0.0     # when this turn began
        self.settings = {}
        self.options = {"model": "", "thinking": "off", "mode": "ask"}
        self.defer = False     # the screen shows the header itself

    def emit(self, kind, legacy, pretty=None, data=None):
        """legacy: the plain line printed without the screen; the screen
        shows kind (you, mike, step, ask, request, answer, error, done,
        note) with pretty (default legacy)."""
        if self.prompt:
            self.prompt.block(kind, plain(legacy if pretty is None else pretty), data)
        elif legacy is not None:
            print(plain(legacy), file=self.output, flush=True)

    def show(self, text):
        self.emit('note', text)

    def attach(self, token=None):
        state = self.service.attach(token, connect='auto' if token else 'none')
        if self.owner and state.get('account') != self.owner:
            raise ServiceError(409, {'error':'account_changed', 'message':'account changed; start a new CLI session'})
        if state.get('connection') == 'restoring':
            raise ServiceError(503, {'error':'restoring', 'message':'the service is restoring its saved account; retry when the backend reconnects'})
        if state.get('connection') != 'connected' and state.get('chat_authenticated'):
            raise ServiceError(503, {'error':'offline', 'message':'the signed-in service is reconnecting; no new login is needed'})
        if state.get('connection') != 'connected' or not state.get('account'):
            raise ServiceError(409, {'error':'signed_out', 'message':'not signed in; mike-cli --sign-in opens your browser to sign in (headless enrollment alone is not a chat login)'})
        if state.get('chat_version') not in (1, 2):
            raise ServiceError(426, {'error':'chat_incompatible', 'message':'upgrade mike-service to one supporting CLI chat version 1'})
        self.owner = state['account']
        self.device, self.status = str(state.get('device_id', '?')), state
        if state.get('chat_version') == 2:
            self.settings = self.service.call('/v1/chat/settings', {})
            configured = {m['id'] for m in self.settings.get('models', [])}
            if self.options['model'] not in configured:
                self.options['model'] = self.settings['default_model']
        if self.prompt:
            self.show('Reconnected to the Mike service.')
            return state
        if self.defer:
            return state
        self.show(f"Mike: {self.owner}; local device {state.get('device_id', '?')}; project {self.workspace}")
        self.show('Project context grants no tool permission. /help lists commands.')
        return state

    def resume(self, turn):
        view = self.service.call('/v1/chat/resume', {'turn_id':turn})
        self.turn, self.view, self.local = turn, {}, None
        self.steps, self.finished, self.shown = 0, False, set()
        self.started = time.monotonic()
        self.render(view)

    def ask(self, text, images=None, options=None):
        attachments = image_input.validate(images or [])
        selected = dict(options or self.options)
        if not text.strip():
            return
        if attachments:
            chosen = next((m for m in self.settings.get('models', []) if m['id'] == selected['model']), {})
            if not chosen.get('vision'):
                raise ValueError('this model cannot see images; choose a vision model or remove the attachment')
        if self.turn and not self.finished:
            if self.interactive and len(self.waiting) < QUEUE_MOST:
                if self.status.get('chat_version') == 2:
                    self.waiting.append({'text': text, 'images': attachments, 'options': selected})
                else:
                    self.waiting.append(text)
                if self.prompt: return
                self.show(f'Queued ({len(self.waiting)} waiting): it goes when this turn ends. /unqueue takes them back.')
                return
            self.show('A turn is still active. /leave detaches from it; /stop stops only its current local command.')
            return
        if len(text.encode()) > 16384:
            raise ValueError('message exceeds 16 KiB')
        enclosing_ide = ide_context.current()
        submitted = text
        if enclosing_ide:
            submitted = '[Local IDE context for this turn]\n' + enclosing_ide + '\n[User message]\n' + text
            if len(submitted.encode()) > 16384:
                raise ValueError('message plus IDE context exceeds 16 KiB; shorten the message')
        if self.prompt: self.emit('you', None, text)
        # Never retry this POST: a lost reply may already have started a turn.
        body = {'text':submitted, 'workspace':self.workspace}
        if self.status.get('conversations'):
            body['conversation_id'] = self.conversation
        if self.status.get('chat_version') == 2:
            body.update(selected)
            body['images'] = attachments
        elif attachments:
            raise ValueError('upgrade Mike service to send images')
        started = self.service.call('/v1/chat/ask', body)
        self.turn = started['turn_id']
        if isinstance(started.get('conversation_id'), str):
            self.conversation = started['conversation_id']
        self.steps, self.finished, self.shown, self.view = 0, False, set(), {}
        self.started = time.monotonic()
        if not self.prompt: self.show(f'Turn {self.turn} (resume with --resume {self.turn})')

    def render(self, view):
        if view.get('turn_id') != self.turn:
            raise ValueError('service returned another turn')
        self.view = view
        for step in view.get('steps', [])[self.steps:]:
            if self.prompt:
                self.emit('step', None, '', data=step)
                continue
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
            self.emit('ask', f"Question: {view.get('say', '')}\n/yes {nonce[:12]} or /no {nonce[:12]}", view.get('say', ''))
        if view.get('stage') in ('done', 'failed') and not self.finished:
            reply = view.get('reply', '') or view['stage']
            self.emit('mike' if view['stage'] == 'done' else 'error', reply)
            self.finished = True
            if self.prompt and self.started:
                self.emit('done', None, f'Worked for {int(time.monotonic() - self.started)}s')

    def poll(self):
        state = self.service.state()  # heartbeat this client even while typing
        self.status = state
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
                what = work.get('argv') or work.get('file') or work.get('goal')
                if self.prompt:
                    shown = shown_command(what) if isinstance(what, list) else json.dumps(what)
                    self.emit('request', None, f"{shown}  (in {work.get('cwd') or self.workspace})")
                    return None
                self.show(f"Local request {work['id']} in {work.get('cwd') or self.workspace}:\n{json.dumps(what)}")
                if work['kind'] == 'file' and work['stage'] == 'awaiting_approval':
                    self.show(f'/find {key[:12]} QUERY searches only the current project; /decline {key[:12]} refuses.')
                else:
                    self.show(f'/approve {key[:12]} or /decline {key[:12]}')
        if not self.interactive and (self.local or self.view.get('stage') == 'confirm'):
            self.show(f'Input required in an interactive CLI; resume with --resume {self.turn}. Nothing was approved.')
            return 3
        if self.finished and self.waiting:
            self.finished, self.turn = False, ''
            queued = self.waiting.pop(0)
            if isinstance(queued, dict): self.ask(**queued)
            else: self.ask(queued)
        return None

    def running(self):
        """This turn's local command is running (so it can be stopped)."""
        work = self.status.get('work') or {}
        return bool(self.turn) and work.get('id') == self.view.get('work_id') and work.get('stage') == 'running'

    def doing(self):
        """What Mike is doing now, in a few words."""
        work = self.status.get('work') or {}
        if self.running():
            what = work.get('argv')
            if isinstance(what, list):
                return 'Running ' + ' '.join(str(x) for x in what)[:60]
            return 'Working on this computer'
        steps = self.view.get('steps') or []
        if self.view.get('say') and self.view.get('stage') not in ('confirm', 'done', 'failed'):
            return 'Mike: ' + str(self.view['say'])[:60]
        if steps:
            return 'Mike: ' + str(steps[-1].get('say', ''))[:60]
        return 'Mike is thinking'

    def supports_conversations(self):
        if not self.status.get('conversations'):
            raise ValueError('upgrade mike-service to list conversations')

    def conversations(self, arg='', limit=20, start=False):
        """The person's conversations with Mike, the same list Mike's pages
        show: this computer's by default, `all` for every one. At start the
        few latest are shown, and nothing when there are none."""
        self.supports_conversations()
        which = 'all' if arg == 'all' else 'this'
        found = self.service.call('/v1/chat/conversations', {'device': which, 'limit': limit})
        names = {d['device_id']: d.get('name', '') for d in found.get('devices', [])}
        self.listed = found.get('items', [])
        if not self.listed:
            if start:
                return
            self.show('No conversations on this computer yet; /conversations all lists every one.' if which == 'this'
                      else 'No conversations yet.')
            return
        lines = ['This computer\'s conversations:' if which == 'this' else 'Your conversations:']
        for number, one in enumerate(self.listed, 1):
            when = time.strftime('%Y-%m-%d %H:%M', time.localtime(one.get('updated_at') or 0))
            where = ', '.join(names[d] for d in one.get('devices', []) if names.get(d))
            mark = ' (open)' if one.get('id') == self.conversation else ''
            lines.append(f"{number:>2}. {one.get('title', '')}{mark}\n    {when}{' · ' + where if where else ''}")
        if start:
            lines.append('/open N continues one; just type to start a new one. /conversations lists more.')
        else:
            lines.append('/open N continues one; /new starts a new one.' + ('' if which == 'all' else ' /conversations all lists every computer\'s.'))
        self.show('\n'.join(lines))

    def open(self, arg):
        """Continue a conversation: its history first, then the prompt."""
        self.supports_conversations()
        if not arg:
            raise ValueError('/open needs a number from /conversations')
        chosen = arg
        if arg.isdigit():
            if not 1 <= int(arg) <= len(self.listed):
                raise ValueError('no such number; /conversations lists them')
            chosen = self.listed[int(arg) - 1]['id']
        if self.turn and not self.finished:
            raise ValueError('a turn is still active; wait for it or /leave first')
        history = self.service.call('/v1/chat/history', {'conversation_id': chosen})
        self.conversation = chosen
        self.turn, self.view, self.local, self.finished = '', {}, None, False
        title = next((one.get('title', '') for one in self.listed if one.get('id') == chosen), '')
        self.show('Continuing: ' + (title or chosen) + ('\n(earlier messages not shown)' if history.get('has_more') else ''))
        for one in history.get('turns', []):
            self.emit('you', 'You: ' + str(one.get('text', '')), str(one.get('text', '')))
            self.emit('mike', 'Mike: ' + str(one.get('reply', '')), str(one.get('reply', '')))

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
        if verb in ('/model', '/think', '/mode'):
            if self.status.get('chat_version') != 2:
                raise ValueError('upgrade Mike service for model, thinking and mode controls')
            if verb == '/model':
                models = self.settings.get('models', [])
                if not arg:
                    self.show('\n'.join(f"{m['id']} — {m.get('label', m['id'])}; images: {bool(m.get('vision'))}; thinking: {bool(m.get('thinking'))}" for m in models))
                    return
                model = next((m for m in models if m['id'] == arg), None)
                if model is None: raise ValueError('choose a configured model from /model')
                self.options['model'] = arg
                if not model.get('thinking'): self.options['thinking'] = 'off'
            elif verb == '/think':
                if not arg: self.show('Thinking: '+self.options['thinking']); return
                if arg not in ('off','low','medium','high'): raise ValueError('thinking is off, low, medium or high')
                model = next((m for m in self.settings.get('models', []) if m['id'] == self.options['model']), {})
                if arg != 'off' and not model.get('thinking'): raise ValueError('this model does not offer thinking controls')
                self.options['thinking'] = arg
            else:
                if not arg: self.show('Mode: '+self.options['mode']); return
                if arg not in ('ask','plan','auto'): raise ValueError('mode is ask, plan or auto')
                self.options['mode'] = arg
            self.show('Next message: ' + ', '.join(f'{k}={v}' for k,v in self.options.items()))
            return
        if verb in ('/conversations', '/c'):
            self.conversations(arg)
            return
        if verb == '/open':
            self.open(arg)
            return
        if verb in ('/new', '/leave'):
            self.show(f'Left turn {self.turn or "(none)"}; accepted work is not cancelled.')
            self.turn, self.view, self.local, self.finished = '', {}, None, False
            if verb == '/new':
                self.conversation = ''
                self.show('The next message starts a new conversation.')
        elif verb == '/unqueue':
            if not self.waiting:
                self.show('Nothing is waiting.')
                return
            self.show('Taken back, not sent:\n' + '\n'.join(item['text'] if isinstance(item, dict) else item for item in self.waiting))
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
            if self.prompt: self.emit('answer', None, 'You said ' + verb[1:])
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
            if self.prompt:
                self.emit('answer', None, {'/approve':'You allowed it', '/decline':'You refused it'}.get(verb, 'Searching this project for ' + query.strip()))
        elif verb == '/stop':
            work = self.service.state().get('work')
            if not work or work.get('id') != self.view.get('work_id') or work.get('stage') != 'running':
                self.show('This turn has no running local command. Other clients were not stopped.')
                return
            self.service.stop(work['id'])
            self.show('Stop requested for this command; waiting for its actual result. This does not cancel future model steps.')
        elif verb == '/status':
            self.show(f"Signed in as {self.owner}; this computer {self.device}\n"
                      f"Project {self.workspace} (context only, no permission)\n"
                      f"Service: {self.status.get('connection', '?')}; turn {self.turn or 'none'}"
                      f"{' (finished)' if self.turn and self.finished else ''}; {len(self.waiting)} queued")
        elif verb == '/help' and self.prompt:
            from screen import COMMANDS, SHORTCUTS
            self.show('\n'.join(f"{name} {args}".strip().ljust(18) + what for name, args, what in COMMANDS)
                      + '\n\n' + '\n'.join(k.ljust(30) + v for k, v in SHORTCUTS))
        elif verb == '/help':
            self.show('/yes ID, /no ID: answer the displayed question; /approve ID, /decline ID: local decision\n/find ID QUERY: file search in this project; /stop: current local command only\n/conversations [all], /open N: continue an earlier conversation; /new: a new one\n/resume TURN, /leave, /unqueue, /quit. Enter alone never approves; a message typed while Mike works waits its turn.\n↑/↓ earlier messages. Ctrl-C or Ctrl-D detaches; the service stays alive.\nAccount: mike-cli --account, --sign-in, --switch-account, --sign-out.')
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
    parser.add_argument('--version', action='version', version=f'mike-cli {version} (local protocol 1, chat 2)')
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
    terminal.defer = interactive
    try:
        token = getpass.getpass('Account session token (saved privately by the service): ') if args.connect else None
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
        if interactive:
            from screen import Screen
            screen = Screen(terminal, history_path())
            with screen:
                terminal.prompt = screen
                if args.resume: terminal.resume(args.resume)
                elif args.ask is not None: terminal.ask(args.ask)
                elif terminal.status.get('conversations'):
                    # Like Mike's pages, start on this computer's conversations.
                    try: terminal.conversations(limit=5, start=True)
                    except Exception: pass
                result = screen.loop(args.wait, once)
                terminal.prompt = None
            if result == 130:
                print(f'Detached; service and accepted work continue. Turn: {terminal.turn or "none"}', flush=True)
            return result
        if args.resume: terminal.resume(args.resume)
        elif args.ask is not None: terminal.ask(args.ask)
        else:
            text = sys.stdin.read(16385)
            if not text.strip(): return 0
            terminal.ask(text)
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
            if terminal.finished: return 0 if terminal.view.get('stage') == 'done' else 1
            if terminal.turn and not terminal.finished and time.monotonic() > deadline:
                terminal.show(f'Timed out waiting; turn {terminal.turn} may still be active. Use --resume; no request was replayed.')
                return 4
            time.sleep(1)
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
        terminal.prompt = None
        service.detach()


if __name__ == '__main__':
    sys.exit(main())
