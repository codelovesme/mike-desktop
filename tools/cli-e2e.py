#!/usr/bin/python3
"""Ticket161: real CLI/service/model against the isolated ticket160 scratch stack.

Start tools/mike-scratch-stack.py up in my-euglena-apps first. All accounts,
devices and project data are disposable. No notifications or live deployment.
"""
import json
import os
from pathlib import Path
import pty
import re
import runpy
import select
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
STACK = Path(os.environ.get('STACK_DIR','/var/tmp/m161/stack'))
state = json.loads((STACK/'stack.json').read_text())
os.environ['STACK_DIR']=str(STACK)
os.environ['EUGLENA_HOST_URL']=state['web']
os.environ['CODE_BIN']=os.environ.get('MIKE_SERVICE_CODE_BIN',str(Path.home()/'.local/share/cdlvsm/packages/code/v2.14.5/code'))
headless = runpy.run_path(str(ROOT/'tools/headless-e2e.py'))
helpers = runpy.run_path(str(ROOT/'tools/service-e2e.py'))
checks = 0


def check(condition, label):
    global checks
    assert condition, label
    checks += 1
    print('PASS ' + label, flush=True)


def run_pty(args, env):
    master, slave = pty.openpty()
    process = subprocess.Popen([str(ROOT/'bin/mike-cli'),*args],env=env,stdin=slave,stdout=slave,stderr=slave,start_new_session=True)
    os.close(slave)
    data = ''; answered = False; deadline=time.monotonic()+180
    try:
        while time.monotonic()<deadline:
            ready,_,_=select.select([master],[],[],0.2)
            if ready:
                try: part=os.read(master,65536)
                except OSError: break
                if not part: break
                data+=part.decode(errors='replace')
                match=re.search(r'/yes ([a-f0-9]{12}) or /no',data)
                if match and not answered:
                    os.write(master,('/yes '+match.group(1)+'\n').encode());answered=True
            if process.poll() is not None: break
        process.wait(timeout=5)
        return process.returncode,data,answered
    finally:
        if process.poll() is None: process.terminate();process.wait(timeout=5)
        os.close(master)


with tempfile.TemporaryDirectory(prefix='m161-cli-') as tmp:
    area=Path(tmp); project=area/'project'; project.mkdir()
    (project/'test_sample.py').write_text("import unittest\nfrom pathlib import Path\nclass T(unittest.TestCase):\n def test_real(self):\n  Path('test-ran').write_text('ok')\n  self.assertEqual(2+2,4)\n")
    subprocess.run(['git','init','-q',str(project)],check=True)
    service=helpers['Service'](area)
    client=None
    try:
        email,token=headless['account']('cli161')
        service.start();client=service.client('cli-e2e-control');view=client.attach(token,connect='auto')
        check(view.get('chat_version')==1,'existing service advertises CLI contract')
        client.call('/v1/grants/add',{'root':str(project),'run':['python3 -m unittest'],'ask':['git status'],'minutes':30})
        time.sleep(4) # advertise to the scratch gateway
        env=dict(service.env)
        first=subprocess.run([str(ROOT/'bin/mike-cli'),'--project',str(project),'--ask','On this machine run python3 -m unittest -q in the project directory supplied in context. Do not ask me to run it myself. Report the real result and finish with CLI161DONE.'],env=env,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=180)
        check(first.returncode==0,'non-TTY real CLI/model/tool turn completed')
        check((project/'test-ran').read_text()=='ok','actual granted command changed the scratch project')
        check('CLI161DONE' in first.stdout and 'exit 0' in first.stdout,'CLI prints ordered tool result and final reply')
        turn=re.search(r'Turn (\S+)',first.stdout).group(1)
        resumed=subprocess.run([str(ROOT/'bin/mike-cli'),'--resume',turn],env=env,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=30)
        check(resumed.returncode==0 and 'CLI161DONE' in resumed.stdout,'reconnect resumes existing central turn without repeating Ask')
        rc,text,answered=run_pty(['--project',str(project),'--ask','On this machine run git status --short in the supplied project directory. Do not run any other command. Finish with CLI161ANSWER.'],env)
        check(rc==0 and answered and 'CLI161ANSWER' in text,'real pseudo-terminal answers the grant question through CLI')
        waiting=subprocess.run([str(ROOT/'bin/mike-cli'),'--project',str(project),'--ask','On this machine run git status --short in the supplied project directory. Finish with CLI161PIPE.'],env=env,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=180)
        check(waiting.returncode==3 and 'Input required' in waiting.stdout,'pipeline exits for input without approving')
        pending_id=re.search(r'Turn (\S+)',waiting.stdout).group(1)
        pending=client.call('/v1/chat/resume',{'turn_id':pending_id})
        check(pending['stage']=='confirm','CLI detach leaves question unanswered')
        client.call('/v1/chat/confirm',{'turn_id':pending_id,'yes':False,'confirmation_id':pending['confirmation_id']})
        check(service.process.poll() is None and client.state()['account']==email,'shared service and other client survive CLI exits')
    finally:
        if client:
            try: client.revoke()
            finally: client.detach()
        service.stop();service.log.close()
print(f'{checks}/9 real CLI integration checks passed')
