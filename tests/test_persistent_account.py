"""Real service processes, private disk state and deterministic backend failures."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from test_legacy_migration import FakeHost, ROOT, CODE, OWNER, mike_service


class SessionHost(FakeHost):
    def answer(self, path, envelope):
        particle = envelope.get('particle', {})
        if path == '/api/auth' and particle.get('_class') == 'RefreshToken':
            self.calls.append('RefreshToken')
            owner = self.tokens.get(envelope.get('token'))
            if owner:
                token = 'renewed-' + owner
                self.tokens[token] = owner
                return {'_class':'SignedIn', 'email':owner, 'token':token, 'verified':True}
            return {'_class':'Denied'}
        return super().answer(path, envelope)


class PersistentAccountTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mike-persistent-test-')
        self.area = Path(self.temp.name); self.host = SessionHost(); self.process = None

    def tearDown(self):
        self.stop(); self.host.close(); self.temp.cleanup()

    def start(self):
        env = dict(os.environ, MIKE_SERVICE_STATE_DIR=str(self.area/'state'), MIKE_SERVICE_RUNTIME_DIR=str(self.area/'run'),
                   MIKE_SERVICE_HOST_URL=self.host.url, MIKE_SERVICE_LEGACY_STATE_DIR=str(self.area/'no-legacy'))
        self.process = subprocess.Popen([CODE,'run','main.code'],cwd=ROOT/'service',env=env,
                                        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.wait(lambda: mike_service.reachable(self.area/'run'))

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate(); self.process.wait(timeout=10)

    def wait(self, probe, timeout=15):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            result=probe()
            if result:return result
            time.sleep(.1)
        self.fail('timed out waiting for account/device state')

    def client(self, name='cli', approvals=True):
        return mike_service.MikeService(name,approvals=approvals,directory=self.area/'run')

    @property
    def saved(self):return self.area/'state/account-session.json'

    def login(self):
        self.start(); c=self.client(); state=c.attach('tok-ada','auto'); c.detach()
        self.assertTrue(self.saved.exists()); return state

    def test_restart_heartbeats_without_clients_and_both_clients_reuse_account(self):
        first=self.login(); self.stop(); self.host.calls.clear(); self.start()
        # Observe backend traffic before attaching any UI client.
        self.wait(lambda: 'ClaimTask' in self.host.calls)
        self.assertNotIn('PairDevice',self.host.calls)
        cli=self.client(); state=cli.attach()
        self.assertEqual(state['account'],OWNER); self.assertTrue(state['chat_authenticated'])
        self.assertEqual(state['device_id'],first['device_id'])
        self.assertNotIn('token',state); self.assertNotIn('tok-ada',json.dumps(state))
        desktop=self.client('desktop'); desktop.attach()
        handoff=desktop.call('/v1/account/desktop-session',{'host':self.host.url})
        self.assertEqual(handoff['account'],OWNER)
        self.assertEqual(self.host.tokens[handoff['token']],OWNER)
        for client, host in ((cli,self.host.url),(desktop,'https://wrong.example')):
            with self.assertRaises(mike_service.ServiceError) as refused:
                client.call('/v1/account/desktop-session',{'host':host})
            self.assertEqual(refused.exception.status,403)

    def test_signout_and_account_switch_update_the_shared_persisted_owner(self):
        self.login(); cli=self.client(); cli.attach()
        with self.assertRaises(mike_service.ServiceError): self.client().attach('tok-bob','auto')
        self.assertEqual(json.loads(self.saved.read_text())['owner'],OWNER)
        other=self.client(); other.attach('tok-bob','switch')
        self.assertEqual(json.loads(self.saved.read_text())['owner'],'bob@example.test')
        with self.assertRaises(mike_service.ServiceError):cli.state()
        self.stop(); self.start()
        self.wait(lambda:self.client().attach().get('chat_authenticated'))
        c=self.client(); self.assertEqual(c.attach()['account'],'bob@example.test')
        c.sign_out(); self.assertFalse(self.saved.exists())
        self.stop(); self.host.calls.clear(); self.start(); time.sleep(.7)
        self.assertNotIn('Heartbeat',self.host.calls)
        self.assertFalse(self.client().attach()['chat_authenticated'])

    def test_backend_outage_preserves_saved_login_until_recovery(self):
        self.login(); self.stop(); self.host.outage=True; self.start()
        self.assertTrue(self.saved.exists())
        c=self.client(); self.assertEqual(c.attach()['connection'],'restoring')
        self.host.outage=False
        self.wait(lambda:c.state().get('chat_authenticated'),timeout=40)
        self.wait(lambda:c.state()['connection']=='connected')

    def test_invalid_account_and_revoked_device_cannot_return_after_restart(self):
        state=self.login(); self.stop(); self.host.tokens.pop('tok-ada'); self.start()
        self.wait(lambda:not self.saved.exists())
        self.assertFalse(self.client().attach()['chat_authenticated'])
        self.stop(); self.host.tokens['tok-ada']=OWNER; self.start()
        c=self.client(); c.attach('tok-ada','auto'); self.stop()
        self.host.devices[state['device_id']]['active']=False
        self.start(); self.wait(lambda:not self.saved.exists() and (self.area/'state/revoked').exists())
        self.assertTrue((self.area/'state/revoked').exists())
        self.stop(); self.host.calls.clear(); self.start(); time.sleep(.7)
        self.assertNotIn('Heartbeat',self.host.calls)

    def test_pending_restore_cannot_be_silently_replaced_by_another_login(self):
        self.login(); self.stop(); self.start()
        with self.assertRaises(mike_service.ServiceError) as mismatch:
            self.client().attach('tok-bob','auto')
        self.assertEqual(mismatch.exception.error,'account_mismatch')
        self.assertEqual(json.loads(self.saved.read_text())['owner'],OWNER)
        self.client().attach('tok-bob','switch')
        self.assertEqual(json.loads(self.saved.read_text())['owner'],'bob@example.test')

    def test_wrong_host_or_device_binding_is_not_restored(self):
        self.login(); self.stop(); value=json.loads(self.saved.read_text())
        value['host']='https://wrong.example'; self.saved.write_text(json.dumps(value))
        self.host.calls.clear(); self.start(); time.sleep(.7)
        self.assertNotIn('Me',self.host.calls)
        self.assertFalse(self.client().attach()['chat_authenticated'])
