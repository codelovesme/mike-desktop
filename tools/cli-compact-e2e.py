#!/usr/bin/python3
"""Ticket 196: `/compact` from the real CLI, service and model, against the
isolated scratch stack (my-euglena-apps tools/mike-scratch-stack.py up).

The account, device and conversation are disposable; nothing live is touched.
Checks that the CLI's /compact reaches Mike's CompactConversation for the open
conversation, that Mike writes the summary, and that the next message in the
same conversation is answered from it.
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
STACK = Path(os.environ.get('STACK_DIR', '/var/tmp/m196/stack'))
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


def summaries(owner):
    script = ('const m=db.getSiblingDB("mike_m160");'
              f'const s=m.conversation_summaries.find({{owner:{json.dumps(owner)}}}).toArray();'
              'print(JSON.stringify(s.map(x=>({c:x._id,v:x.version,u:x.until_order,cu:x.compact_until,t:String(x.text||x.summary||"")}))))')
    out = headless['mongo'](script)
    return json.loads(out.splitlines()[-1]) if out else []


with tempfile.TemporaryDirectory(prefix='m196-cli-') as tmp:
    area = Path(tmp)
    service = helpers['Service'](area)
    client = None
    cli = None
    try:
        email, token = headless['account']('cli196')
        service.start()
        client = service.client('cli196-control')
        view = client.attach(token, connect='auto')
        check(view.get('compact') is True, 'the service advertises /compact')
        time.sleep(4)
        env = dict(service.env, COLUMNS='100', LINES='40', TERM='xterm-256color')
        cli = Pty([], env)
        shown = cli
        cli.until('\x00never', 6)  # let the full-screen chat come up
        cli.type('/compact')
        check(cli.until('/compact works after the first message', 30), 'with nothing said yet, /compact says so and asks nothing')
        cli.type('Please remember this for later: the shed key is under the blue flower pot. Reply only with NOTED196.')
        check(cli.until('NOTED196', times=cli.data.count('NOTED196') + 2), 'the first message is answered by the model')
        time.sleep(1)
        cli.type('/compact')
        check(cli.until('Summarizing this conversation', 30), 'CLI /compact reaches Mike and says what happens')
        written = headless['until']('the summary', lambda: [s for s in summaries(email) if s['cu']], 240, 2)
        conversation = written[0]['c']
        check('blue' in written[0]['t'].lower() or 'pot' in written[0]['t'].lower(), 'the summary keeps the fact')
        cli.type('Where is the shed key? Answer in one short sentence and end with WHERE196.')
        check(cli.until('WHERE196', times=cli.data.count('WHERE196') + 2), 'the next message in the same conversation is answered')
        tail = cli.data[cli.data.rfind('Where is the shed key'):]
        check('pot' in tail.lower(), 'the answer after /compact still knows the fact')
        listed = client.call('/v1/chat/conversations', {'device': 'all', 'limit': 5})
        check(len(listed['items']) == 1 and listed['items'][0]['id'] == conversation, 'one central conversation, the compacted one')
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
print(f'{checks}/8 CLI compact checks passed')
