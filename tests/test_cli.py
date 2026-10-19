"""CLI decisions are tied to displayed turns/versions; transport owns effects."""
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'client'))
from cli import Terminal, plain
from mike_service import ServiceError, ServiceUnavailable


class FakeService:
    def __init__(self):
        self.calls = []
        self.owner = 'ada@example.invalid'
        self.work = None
        self.progress = {'turn_id':'t1','stage':'plan','steps':[]}
    def attach(self, token=None, connect='none'):
        self.calls.append(('attach',connect))
        return {'account':self.owner,'connection':'connected','device_id':'local','chat_version':1}
    def state(self): return {'account':self.owner,'work':self.work}
    def call(self, route, body):
        self.calls.append((route,body))
        if route.endswith('/ask'): return {'turn_id':'t1'}
        return self.progress
    def claim(self, id): self.calls.append(('claim',id))
    def answer(self, id, decision, **fields): self.calls.append(('answer',id,decision,fields))
    def stop(self, id): self.calls.append(('stop',id))


class CliTest(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()
        self.out = io.StringIO()
        self.cli = Terminal(self.service,'/project',True,self.out)
        self.cli.attach()
    def question(self):
        self.cli.turn = 't1'
        self.cli.render({'turn_id':'t1','stage':'confirm','confirmation_id':'q'*64,'say':'question','steps':[]})
    def test_workspace_is_context_not_another_executor(self):
        self.cli.ask('run tests')
        self.assertEqual(self.service.calls[-1],('/v1/chat/ask',{'text':'run tests','workspace':'/project'}))
        self.assertEqual(self.cli.turn,'t1')
    def test_active_turn_is_not_silently_replaced(self):
        self.cli.ask('first'); n=len(self.service.calls); self.cli.ask('second')
        self.assertEqual(len(self.service.calls),n)
    def test_blank_or_wrong_or_repeated_yes_never_approves(self):
        self.question(); n=len(self.service.calls)
        for command in ('','/yes','/yes wrongversion'):
            self.cli.command(command)
        self.assertEqual(len(self.service.calls),n)
        self.cli.command('/yes ' + 'q'*12)
        self.assertEqual(self.service.calls[-1][1]['confirmation_id'],'q'*64)
        n=len(self.service.calls); self.cli.command('/yes ' + 'q'*12)
        self.assertEqual(len(self.service.calls),n)
    def test_other_turn_response_is_refused(self):
        self.cli.turn='t1'
        with self.assertRaises(ValueError): self.cli.render({'turn_id':'t2'})
    def test_non_tty_question_has_actionable_exit(self):
        self.question(); self.cli.interactive=False; self.service.progress=self.cli.view
        self.assertEqual(self.cli.poll(),3)
        self.assertIn('--resume t1',self.out.getvalue())
        self.assertFalse(any(c[0].endswith('/confirm') for c in self.service.calls))
    def test_unrelated_local_approval_is_not_presented(self):
        self.cli.turn='t1'; self.service.progress={'turn_id':'t1','stage':'waiting_external','work_id':'mine'}
        self.service.work={'id':'someone-else','approval_id':'a'*64,'stage':'awaiting_approval'}
        self.cli.poll(); self.assertIsNone(self.cli.local)
        self.cli.command('/approve ' + 'a'*12)
        self.assertFalse(any(c[0]=='answer' for c in self.service.calls))
    def test_local_answer_uses_displayed_full_id_once(self):
        self.cli.local={'id':'work','approval_id':'a'*64,'kind':'terminal','stage':'awaiting_approval'}
        self.cli.command('/approve ' + 'a'*12)
        self.assertEqual(self.service.calls[-1],('answer','a'*64,'approve',{}))
        n=len(self.service.calls); self.cli.command('/approve ' + 'a'*12)
        self.assertEqual(len(self.service.calls),n)
    def test_stop_does_not_stop_another_clients_work(self):
        self.cli.view={'work_id':'mine'}; self.service.work={'id':'other','stage':'running'}
        self.cli.command('/stop'); self.assertFalse(any(c[0]=='stop' for c in self.service.calls))
        self.service.work={'id':'mine','stage':'running'}; self.cli.command('/stop')
        self.assertEqual(self.service.calls[-1],('stop','mine'))
    def test_leave_and_quit_do_not_stop_shared_service(self):
        self.cli.turn='t1'; self.cli.command('/leave')
        self.assertEqual(self.cli.turn,''); self.assertEqual(self.cli.command('/quit'),'quit')
        self.assertFalse(any(c[0]=='stop' for c in self.service.calls))
    def test_resume_only_fetches_known_central_turn(self):
        self.service.progress={'turn_id':'old','stage':'done','reply':'result'}
        self.cli.resume('old')
        self.assertEqual(self.service.calls[-1],('/v1/chat/resume',{'turn_id':'old'}))
        self.assertTrue(self.cli.finished)
    def test_account_switch_stops_old_transcript_polling(self):
        self.cli.turn='t1'; self.service.owner='bob@example.invalid'
        with self.assertRaises(ServiceError): self.cli.poll()
        self.assertFalse(any(c[0].endswith('/progress') for c in self.service.calls))
        with self.assertRaises(ServiceError): self.cli.attach()
    def test_tool_controls_are_removed_and_display_is_bounded(self):
        text=plain('\x1b[2Jprivate\x07' + 'x'*17000)
        self.assertNotIn('\x1b',text); self.assertNotIn('\x07',text)
        self.assertTrue(text.endswith('[display truncated]'))
    def test_missing_service_is_not_started_or_hidden(self):
        with tempfile.TemporaryDirectory() as area:
            env=dict(os.environ, MIKE_SERVICE_RUNTIME_DIR=area)
            result=subprocess.run([str(ROOT/'bin/mike-cli'),'--ask','hello'],env=env,capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,4)
            self.assertIn('mike-service start',result.stderr)
            self.assertEqual(list(Path(area).iterdir()),[])
    def test_pipeline_cannot_hide_token_prompt(self):
        result=subprocess.run([str(ROOT/'bin/mike-cli'),'--connect'],input='',capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,2)
        self.assertIn('needs a terminal',result.stderr)

    def test_old_chat_contract_is_rejected_without_downgrade(self):
        def old_attach(*args, **kwargs):
            return {'account':self.service.owner,'connection':'connected','chat_version':0}
        self.service.attach=old_attach
        with self.assertRaises(ServiceError) as caught: self.cli.attach()
        self.assertEqual(caught.exception.status,426)
    def test_uncertain_ask_is_not_replayed(self):
        def failed(route, body):
            self.service.calls.append((route,body))
            raise ServiceUnavailable('lost response')
        self.service.call=failed
        with self.assertRaises(ServiceUnavailable): self.cli.ask('change a file')
        self.assertEqual(len([c for c in self.service.calls if c[0].endswith('/ask')]),1)
        self.assertEqual(self.cli.turn,'')

    def test_staged_cli_is_independent_and_declares_shared_service(self):
        with tempfile.TemporaryDirectory() as area:
            made=subprocess.run([sys.executable,str(ROOT/'tools/package.py'),'--cli-only','v0.5.0',area],capture_output=True,text=True,timeout=30)
            self.assertEqual(made.returncode,0,made.stderr)
            stage=Path(area)/'mike-cli-v0.5.0-x86_64-linux'
            info=(stage/'cdlvsm-package.info').read_text()
            self.assertIn('requires=mike-service >=0.5.0 <0.6.0',info)
            self.assertNotIn('webkit',info)
            self.assertFalse((stage/'service').exists())
            self.assertFalse((stage/'ui').exists())
            version=subprocess.run([str(stage/'mike-cli'),'--version'],capture_output=True,text=True,timeout=5)
            self.assertEqual(version.returncode,0)
            self.assertIn('v0.5.0',version.stdout)


if __name__=='__main__': unittest.main()
