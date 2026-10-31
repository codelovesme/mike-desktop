"""Mike CLI on its own: the browser sign-in, the account options, and
messages typed while Mike works (ticket 005)."""
import io
from pathlib import Path
import sys
import threading
import types
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'client'))
import cli
import sign_in
from mike_service import ServiceError

ORIGIN = 'https://apps.codeloves.me'


def post(url, state, token, origin=ORIGIN):
    """What the connect page does: the state and token to the callback."""
    query = parse_qs(urlsplit(url).query)
    port = query['port'][0]
    body = urlencode({'state': state if state is not None else query['state'][0], 'token': token}).encode()
    request = Request(f'http://127.0.0.1:{port}/callback', data=body, method='POST', headers={'Origin': origin})
    try:
        with urlopen(request, timeout=5) as answer:
            return answer.status
    except HTTPError as error:
        return error.code


class FakeBrowser:
    """Opens the address, and later posts what a signed-in page would."""
    def __init__(self, token='session-token', state=None, origin=ORIGIN, opens=True):
        self.token, self.state, self.origin, self.opens = token, state, origin, opens
        self.url, self.statuses = None, []

    def __call__(self, url):
        self.url = url
        def later():
            self.statuses.append(post(url, self.state, self.token, self.origin))
            if self.state is not None:  # a forged one first, then the real page
                self.statuses.append(post(url, None, self.token))
        threading.Thread(target=later, daemon=True).start()
        return self.opens


class FakeService:
    """The shared service as sign-in sees it."""
    attached = []

    def __init__(self, client, approvals=True):
        self.client = client

    def attach(self, token=None, connect='none'):
        FakeService.attached.append((self.client, bool(token), connect))
        if token and FakeService.refuse:
            raise ServiceError(409, {'error': FakeService.refuse})
        account = 'ada@example.invalid' if token else FakeService.before
        return {'account': account, 'connection': 'connected' if account else 'signed_out', 'host': ORIGIN}

    def detach(self):
        pass


class SignInTest(unittest.TestCase):
    def setUp(self):
        FakeService.attached, FakeService.refuse, FakeService.before = [], None, ''
        self.started = sign_in.start_service
        sign_in.start_service = lambda out: True

    def tearDown(self):
        sign_in.start_service = self.started

    def test_the_browser_hands_the_token_straight_to_the_service(self):
        out, browser = io.StringIO(), FakeBrowser()
        account, note = sign_in.sign_in(out, opener=browser, timeout=10, service_class=FakeService)
        self.assertEqual(account, 'ada@example.invalid')
        self.assertEqual(note, 'Signed in as ada@example.invalid')
        query = parse_qs(urlsplit(browser.url).query)
        self.assertTrue(browser.url.startswith(ORIGIN + '/desktop/connect?'))
        self.assertEqual(query['app'], ['Mike CLI'])
        self.assertEqual(FakeService.attached[-1], ('cli-sign-in', True, 'auto'))
        self.assertNotIn('session-token', out.getvalue())

    def test_a_forged_state_or_origin_is_refused(self):
        browser = FakeBrowser(state='not-the-one')
        account, _ = sign_in.sign_in(io.StringIO(), opener=browser, timeout=10, service_class=FakeService)
        self.assertEqual(browser.statuses[0], 403)
        self.assertEqual(account, 'ada@example.invalid')  # only the real page's post counted
        token = sign_in.browser_handoff(ORIGIN, 1.5, io.StringIO(), FakeBrowser(origin='https://evil.example'))
        self.assertIsNone(token)

    def test_no_browser_prints_the_address_and_still_waits(self):
        out = io.StringIO()
        browser = FakeBrowser(opens=False)
        token = sign_in.browser_handoff(ORIGIN, 10, out, browser)
        self.assertEqual(token, 'session-token')
        self.assertIn('Open this address in a browser', out.getvalue())
        self.assertIn(browser.url, out.getvalue())

    def test_cancelled_changes_nothing(self):
        account, note = sign_in.sign_in(io.StringIO(), opener=lambda url: True, timeout=0.5, service_class=FakeService)
        self.assertEqual(account, '')
        self.assertIn('cancelled', note)
        self.assertEqual([a for a in FakeService.attached if a[1]], [])

    def test_another_account_is_not_switched_silently(self):
        FakeService.refuse, FakeService.before = 'account_mismatch', 'grace@example.invalid'
        account, note = sign_in.sign_in(io.StringIO(), opener=FakeBrowser(), timeout=10, service_class=FakeService)
        self.assertEqual(account, '')
        self.assertIn('grace@example.invalid', note)
        self.assertIn('--switch-account', note)
        FakeService.refuse = None
        sign_in.sign_in(io.StringIO(), switch=True, opener=FakeBrowser(), timeout=10, service_class=FakeService)
        self.assertEqual(FakeService.attached[-1], ('cli-sign-in', True, 'switch'))

    def test_only_codelovesme_or_loopback_hosts(self):
        self.assertTrue(sign_in.trusted_host(ORIGIN))
        self.assertTrue(sign_in.trusted_host('http://127.0.0.1:8090'))
        self.assertFalse(sign_in.trusted_host('http://example.com:80'))
        self.assertFalse(sign_in.trusted_host('https://apps.codeloves.me.evil.example'))


class AccountTest(unittest.TestCase):
    def run_account(self, state, **flags):
        calls = []
        class Service:
            def __init__(self, *_a, **_k): pass
            def attach(self, token=None, connect='none'): return state
            def sign_out(self): calls.append('sign_out')
            def detach(self): pass
        saved = cli.MikeService
        cli.MikeService = Service
        try:
            args = types.SimpleNamespace(**{'sign_in': False, 'switch_account': False, 'sign_out': False, 'account': False, **flags})
            out = io.StringIO()
            code = cli.account_command(args, out)
        finally:
            cli.MikeService = saved
        return code, out.getvalue(), calls

    def test_account_says_who(self):
        code, text, _ = self.run_account({'account': 'ada@example.invalid', 'connection': 'connected', 'device_id': 'd1'}, account=True)
        self.assertEqual(code, 0)
        self.assertIn('Signed in as ada@example.invalid', text)
        code, text, _ = self.run_account({'account': '', 'connection': 'signed_out'}, account=True)
        self.assertEqual(code, 5)
        self.assertIn('--sign-in', text)

    def test_sign_out(self):
        code, text, calls = self.run_account({'account': 'ada@example.invalid', 'connection': 'connected'}, sign_out=True)
        self.assertEqual((code, calls), (0, ['sign_out']))
        self.assertIn('Signed out of ada@example.invalid', text)

    def test_account_options_run_alone(self):
        with self.assertRaises(SystemExit) as stopped:
            cli.main(['--account', '--ask', 'hi'])
        self.assertEqual(stopped.exception.code, 2)
        with self.assertRaises(SystemExit):
            cli.main(['--sign-in', '--sign-out'])


class QueueTest(unittest.TestCase):
    def setUp(self):
        from test_cli import FakeService as Central
        self.service = Central()
        self.out = io.StringIO()
        self.cli = cli.Terminal(self.service, '/project', True, self.out)
        self.cli.attach()

    def asks(self):
        return [body['text'] for route, body in self.service.calls if isinstance(route, str) and route.endswith('/ask')]

    def test_messages_typed_while_mike_works_wait_their_turn(self):
        self.cli.command('first')
        self.cli.command('second')
        self.cli.command('/unqueue')
        self.assertIn('second', self.out.getvalue().split('Taken back, not sent:')[1])
        self.cli.command('third')
        self.assertEqual(self.asks(), ['first'])
        self.service.progress = {'turn_id': 't1', 'stage': 'done', 'reply': 'one', 'steps': []}
        self.cli.poll()
        self.assertEqual(self.asks(), ['first', 'third'])
        self.assertFalse(self.cli.finished)

    def test_at_most_ten_wait(self):
        self.cli.command('first')
        for n in range(cli.QUEUE_MOST + 1):
            self.cli.command(f'waiting {n}')
        self.assertEqual(len(self.cli.waiting), cli.QUEUE_MOST)
        self.assertIn('A turn is still active', self.out.getvalue())

    def test_a_non_interactive_run_never_queues(self):
        once = cli.Terminal(self.service, '/project', False, io.StringIO())
        once.attach()
        once.command('first')
        once.command('second')
        self.assertEqual(once.waiting, [])


sys.path.insert(0, str(ROOT / 'tests'))

if __name__ == '__main__':
    unittest.main()
