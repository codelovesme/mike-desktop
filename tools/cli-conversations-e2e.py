#!/usr/bin/python3
"""Ticket 193: `/rename`, `/archive`, `/unarchive`, `/delete` and a busy conversation
from the real CLI, service and model, against the isolated scratch stack
(my-euglena-apps tools/mike-scratch-stack.py up).

The account, device and conversation are disposable; nothing live is touched.
"""
import json
import os
from pathlib import Path
import pty
import runpy
import select
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
APPS = Path(os.environ.get('APPS_DIR', str(Path.home() / 'git/codelovesme/my-euglena-apps')))
STACK = Path(os.environ.get('STACK_DIR', '/var/tmp/m193/stack'))
state = json.loads((STACK / 'stack.json').read_text())
os.environ['STACK_DIR'] = str(STACK)
os.environ['EUGLENA_HOST_URL'] = state['web']
os.environ['CODE_BIN'] = os.environ.get('MIKE_SERVICE_CODE_BIN', str(Path.home() / '.local/share/cdlvsm/packages/code/v2.14.5/code'))
headless = runpy.run_path(str(ROOT / 'tools/headless-e2e.py'))
headless['mongo'].__globals__['APPS'] = APPS
helpers = runpy.run_path(str(ROOT / 'tools/service-e2e.py'))
checks = 0


shown = None


def check(condition, label):
    global checks
    if not condition and shown is not None:
        import re
        print(re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', shown.data)[-1500:])
    assert condition, label
    checks += 1
    print('PASS ' + label, flush=True)


class Pty:
    def __init__(self, args, env):
        self.master, slave = pty.openpty()
        self.process = subprocess.Popen([str(ROOT / 'bin/mike-cli'), *args], env=env, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        self.data = ''

    def until(self, text, timeout=240, times=1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.data.count(text) >= times:
                return True
            ready, _, _ = select.select([self.master], [], [], 0.2)
            if ready:
                try:
                    part = os.read(self.master, 65536)
                except OSError:
                    break
                if not part:
                    break
                self.data += part.decode(errors='replace')
            if self.process.poll() is not None:
                break
        return self.data.count(text) >= times

    def type(self, line):
        os.write(self.master, (line + '\r').encode())

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=5)
        os.close(self.master)


def listed(client, archived=False):
    body = {'device': 'all', 'limit': 10}
    if archived: body['archived'] = True
    return client.call('/v1/chat/conversations', body)['items']


with tempfile.TemporaryDirectory(prefix='m193-cli-') as tmp:
    area = Path(tmp)
    service = helpers['Service'](area)
    client = None
    cli = None
    try:
        email, token = headless['account']('cli193')
        service.start()
        client = service.client('cli193-control')
        view = client.attach(token, connect='auto')
        check(view.get('rename_archive') is True, 'the service advertises rename_archive')
        time.sleep(4)
        env = dict(service.env, COLUMNS='100', LINES='40', TERM='xterm-256color')
        cli = Pty([], env)
        shown = cli
        cli.until('\x00never', 6)
        cli.type('/rename Desk')
        check(cli.until('no conversation open', 30), 'with nothing open, /rename says so')
        cli.type('Reply only with HELLO193.')
        check(cli.until('HELLO193', times=cli.data.count('HELLO193') + 2), 'the first message is answered')
        items = listed(client)
        check(len(items) == 1, 'one conversation')
        conversation = items[0]['id']
        cli.type('/rename Shed planning 193')
        check(cli.until('Renamed: Shed planning 193', 30), 'CLI /rename reaches Mike')
        check(listed(client)[0]['title'] == 'Shed planning 193', 'the list shows the new title')
        cli.type('/rename sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA')
        check(cli.until('Not renamed:', 30), 'a credential-like title is refused and shown')
        check(listed(client)[0]['title'] == 'Shed planning 193', 'the refused title changed nothing')
        cli.type('/archive')
        check(cli.until('Archived: ', 30), 'CLI /archive puts the open conversation away')
        check(listed(client) == [], 'the archived conversation leaves the list')
        check([one['id'] for one in listed(client, True)] == [conversation], 'and is listed as archived')
        cli.type('/conversations archived')
        check(cli.until('Archived conversations:', 30) and cli.until('Shed planning 193', 30, cli.data.count('Shed planning 193') + 0), 'CLI lists archived conversations')
        cli.type('/unarchive 1')
        check(cli.until('Back in the list', 30), 'CLI /unarchive brings it back')
        check([one['id'] for one in listed(client)] == [conversation], 'it is in the list again')
        # Busy: while the CLI's turn runs, a second device-side ask in the same conversation is refused.
        cli.type('Count from 1 to 40 in words, one per line, then write DONE193.')
        time.sleep(1.5)
        try:
            client.call('/v1/chat/ask', {'text': 'second message', 'conversation_id': conversation})
            busy = None
        except Exception as error:
            busy = getattr(error, 'body', {}) or {}
            print('busy answer:', getattr(error, 'status', None), busy.get('error'))
        check(busy is not None and busy.get('error') == 'busy' and isinstance(busy.get('turn_id'), str), 'a second message while Mike works is answered busy, with the running turn')
        check(cli.until('DONE193', times=cli.data.count('DONE193') + 2), 'the running turn still finishes')
        cli.type('/delete')
        check(cli.until('its messages for good', 30) and len(listed(client)) == 1, '/delete asks first and deletes nothing yet')
        cli.type('/delete yes')
        check(cli.until('Deleted: ', 30), 'CLI /delete yes deletes it')
        check(listed(client) == [] and listed(client, True) == [], 'the conversation is gone from both lists')
        cli.type('/quit')
        cli.process.wait(timeout=10)
    finally:
        if cli:
            cli.close()
        if client:
            try:
                client.revoke()
            finally:
                client.detach()
        service.stop()
        service.log.close()
print(f'{checks}/19 CLI conversation checks passed')
