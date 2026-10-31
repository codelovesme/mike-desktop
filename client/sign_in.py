"""Mike CLI's own sign-in: the codelovesme browser handoff Desktop and the
IDE used (/desktop/connect, a one-use state, the session token posted to a
loopback callback). The token goes straight to the shared service and is
never saved, printed or put on a command line."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import threading
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

import mike_service

PUBLIC_ORIGIN = 'https://apps.codeloves.me'
APP_NAME = 'Mike CLI'


def trusted_host(host):
    """The public origin, or a loopback one (a developer's own stack)."""
    if host == PUBLIC_ORIGIN:
        return True
    try:
        parts = urlsplit(host)
    except ValueError:
        return False
    return (parts.scheme == 'http' and parts.hostname in ('127.0.0.1', 'localhost')
            and parts.port is not None and parts.path in ('', '/'))


def service_launcher():
    """mike-service: beside this checkout or package, else on PATH."""
    here = Path(__file__).resolve().parent.parent
    for candidate in (here / 'bin/mike-service', here / 'mike-service',
                      here / '.cdlvsm-deps/mike-service/mike-service'):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    for name in ('mike-service', 'cdlvsm-mike-service'):
        found = shutil.which(name)
        if found:
            return found
    return None


def start_service(out):
    """Starts the shared service, as `mike-service start` does. True when it answers."""
    if mike_service.reachable():
        return True
    launcher = service_launcher()
    if not launcher:
        print('mike-cli: the Mike service is not installed (cdlvsm install mike-cli brings it)', file=out)
        return False
    print('Starting the Mike service…', file=out, flush=True)
    started = subprocess.run([launcher, 'start'], capture_output=True, text=True, timeout=60)
    if started.returncode != 0:
        print('mike-cli: ' + (started.stderr.strip() or started.stdout.strip() or 'mike-service start failed'), file=out)
        return False
    return mike_service.reachable()


def browser_handoff(origin, timeout, out, opener=webbrowser.open):
    """Opens the browser on /desktop/connect and waits for its callback.
    Returns the session token, or None when cancelled or timed out. With no
    browser to open, the address is printed for the person to open."""
    expected = secrets.token_urlsafe(32)
    got = {}
    arrived = threading.Event()

    class Callback(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != '/callback' or self.headers.get('Origin') not in (origin, 'null') or arrived.is_set():
                self.send_error(403)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                length = 0
            if not 0 < length <= 4096:
                self.send_error(400)
                return
            try:
                fields = parse_qs(self.rfile.read(length).decode('utf-8'), strict_parsing=True)
            except (UnicodeDecodeError, ValueError):
                self.send_error(400)
                return
            state = fields.get('state', [''])[0]
            token = fields.get('token', [''])[0]
            if not secrets.compare_digest(state, expected) or not token:
                self.send_error(403)
                return
            got['token'] = token
            arrived.set()
            page = b'<!doctype html><title>Mike CLI connected</title><p>You are signed in. You can return to your terminal.</p>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Security-Policy', "default-src 'none'")
            self.send_header('Content-Length', str(len(page)))
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *_args):
            pass

    server = HTTPServer(('127.0.0.1', 0), Callback)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = origin + '/desktop/connect?' + urlencode({'port': server.server_port, 'state': expected, 'app': APP_NAME})
        opened = False
        try:
            opened = opener(url)
        except Exception:
            opened = False
        if opened:
            print('Finish signing in to codelovesme in your browser, then press Connect.', file=out, flush=True)
        else:
            print('Open this address in a browser on this computer to sign in:\n  ' + url, file=out, flush=True)
        print('(Ctrl-C cancels.)', file=out, flush=True)
        arrived.wait(timeout)
        return got.get('token')
    finally:
        server.shutdown()
        server.server_close()


REFUSED = {
    'account_refused': 'codelovesme refused the sign-in (expired or invalid); try mike-cli --sign-in again',
    'account_mismatch': None,
    'connect_required': "this computer's Mike access was revoked; mike-cli --switch-account connects it again",
    'gateway_unavailable': 'the Mike gateway is unreachable; still signed out; try again when online',
}


def sign_in(out, switch=False, timeout=300, opener=webbrowser.open, service_class=mike_service.MikeService):
    """Signs the shared service in through the browser. `switch` moves it to
    another account, an explicit choice; without it a different account is
    refused. Returns (account or '', message)."""
    if not start_service(out):
        return '', 'the Mike service is not running; start it with mike-service start'
    observer = service_class('cli-sign-in', approvals=False)
    try:
        before = observer.attach(None, connect='none')
    finally:
        observer.detach()
    origin = str(before.get('host') or PUBLIC_ORIGIN).rstrip('/')
    if not trusted_host(origin):
        return '', 'the Mike service host is not a codelovesme origin: ' + origin[:80]
    token = browser_handoff(origin, timeout, out, opener)
    if not token:
        return '', 'sign-in was cancelled or timed out; nothing changed'
    connector = service_class('cli-sign-in', approvals=False)
    try:
        state = connector.attach(token, connect='switch' if switch else 'auto')
    except mike_service.ServiceError as error:
        if error.error == 'account_mismatch':
            return '', ('not switched: the Mike service is signed in as ' + str(before.get('account') or 'another account') +
                        '; mike-cli --switch-account moves it')
        return '', REFUSED.get(error.error) or 'sign-in failed: ' + str(error.error)
    finally:
        token = None
        connector.detach()
    account = state.get('account') or ''
    if not account:
        return '', 'the service did not report an account'
    return account, 'Signed in as ' + account
