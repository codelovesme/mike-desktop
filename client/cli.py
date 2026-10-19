#!/usr/bin/python3
"""Terminal presentation for the shared Mike service; no agent or tool loop."""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import select
import sys
import time
import unicodedata

from mike_service import MikeService, ServiceError, ServiceUnavailable


def plain(value, limit=16384):
    """Model/tool text cannot send terminal control sequences."""
    value = str(value)
    safe = ''.join(c for c in value if c in '\n\t' or not unicodedata.category(c).startswith('C'))
    return safe[:limit] + ('\n[display truncated]' if len(safe) > limit else '')


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

    def show(self, text):
        print(plain(text), file=self.output, flush=True)

    def attach(self, token=None):
        state = self.service.attach(token, connect='auto' if token else 'none')
        if self.owner and state.get('account') != self.owner:
            raise ServiceError(409, {'error':'account_changed', 'message':'account changed; start a new CLI session'})
        if state.get('connection') != 'connected' or not state.get('account'):
            raise ServiceError(409, {'error':'signed_out', 'message':'sign in through Desktop, or mike-cli --connect; headless enrollment alone is not a chat login'})
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
        if self.turn and not self.finished:
            self.show('A turn is still active. /leave detaches from it; /stop stops only its current local command.')
            return
        if not text.strip():
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
            self.show('/yes ID, /no ID: answer the displayed question; /approve ID, /decline ID: local decision\n/find ID QUERY: file search in this project; /stop: current local command only\n/resume TURN, /leave, /new, /quit. Enter alone never approves. Ctrl-C detaches; service stays alive.')
        else:
            self.show('Unknown command. /help lists commands.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ask', help='one message, then exit when done')
    parser.add_argument('--resume', metavar='TURN', help='explicitly attach to an existing central turn')
    parser.add_argument('--project', default=os.getcwd())
    parser.add_argument('--connect', action='store_true', help='read an account session token with a hidden terminal prompt')
    parser.add_argument('--token-stdin', action='store_true', help='read token from one stdin line (requires --ask or --resume)')
    parser.add_argument('--wait', type=int, default=300, help='maximum seconds waiting for a turn (default 300)')
    version_file = Path(__file__).resolve().parent.parent / 'VERSION'
    version = version_file.read_text().strip() if version_file.is_file() else 'dev'
    parser.add_argument('--version', action='version', version=f'mike-cli {version} (local protocol 1, chat 1)')
    args = parser.parse_args(argv)
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
    try:
        token = getpass.getpass('Account session token (not saved): ') if args.connect else None
        if args.token_stdin: token = sys.stdin.readline(65537).strip()
        if (args.connect or args.token_stdin) and (not token or len(token) > 65536):
            raise ValueError('provide a nonempty account token of at most 64 KiB')
        terminal.attach(token)
        token = None
        once = args.ask is not None or bool(args.resume) or not interactive
        if args.resume: terminal.resume(args.resume)
        elif args.ask is not None: terminal.ask(args.ask)
        elif not interactive:
            text = sys.stdin.read(16385)
            if not text.strip(): return 0
            terminal.ask(text)
        deadline = time.monotonic() + args.wait
        prompted = False
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
            if not prompted:
                print('mike> ', end='', flush=True)
                prompted = True
            ready, _, _ = select.select([sys.stdin], [], [], 1)
            if ready:
                line = sys.stdin.readline()
                if not line: return 0
                try:
                    if terminal.command(line.rstrip('\n')) == 'quit': return 0
                except ValueError as error:
                    terminal.show(str(error))
                deadline, prompted = time.monotonic() + args.wait, False
    except KeyboardInterrupt:
        terminal.show(f'Detached; service and accepted work continue. Turn: {terminal.turn or "none"}')
        return 130
    except ServiceError as error:
        print('mike-cli: ' + plain(error), file=sys.stderr)
        return 5 if error.error in ('session_ended', 'account_changed', 'signed_out', 'account_refused') else 4
    except (ServiceUnavailable, OSError, ValueError) as error:
        print('mike-cli: ' + plain(error) + '; start the shared service with mike-service start. Requests are not retried.', file=sys.stderr)
        return 4
    finally:
        service.detach()


if __name__ == '__main__':
    sys.exit(main())
