#!/usr/bin/python3
"""Mike's focused WebKit window and trusted local dialogs.

The shared local Mike service (service/, a native Euglena application) owns
the device identity, the task inbox, command execution and the file opener.
This window is one of its clients: it presents the first-party web
conversation, shows the service's local activity, and collects the person's
decisions through native dialogs. Web content has no script-message handler
and no access to the service.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import queue
import secrets
import shlex
import sys
import threading
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client"))
import mike_service  # noqa: E402


def desktop_app_id(here: Path) -> str:
    """The name GNOME ties the window to its menu entry by: CDLVSM writes the
    entry as codelovesme-<package>.desktop, the package being `mike` (or the
    legacy `mike-desktop`) — …/packages/<package>/<tag>/ui/this file."""
    package = here.resolve().parents[2].name
    return "codelovesme-" + (package if package in ("mike", "mike-desktop") else "mike")


try:
    import gi
    from gi.repository import GLib

    # Before GTK starts, or it is too late: the window was named after this
    # script, so Alt+Tab showed "Unknown" with a generic icon (the Wayland
    # app_id and the X11 WM_CLASS must match the menu entry's file name).
    GLib.set_prgname(desktop_app_id(Path(__file__)))
    GLib.set_application_name("Mike")
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import Gdk

    Gdk.set_program_class(GLib.get_prgname())
    from gi.repository import Gtk, WebKit2
except (ImportError, ValueError) as error:
    raise SystemExit(
        "Mike needs GTK/WebKitGTK. Install python3-gi and gir1.2-webkit2-4.1 "
        f"on this desktop: {error}"
    )


PUBLIC_ORIGIN = "https://apps.codeloves.me"


def trusted_origin(target: str, origin: str) -> bool:
    """Only the exact configured origin may live in the Mike WebView."""
    try:
        candidate, allowed = urlsplit(target), urlsplit(origin)
        return (
            candidate.scheme == allowed.scheme
            and candidate.hostname == allowed.hostname
            and candidate.port == allowed.port
            and candidate.username is None
            and candidate.password is None
        )
    except ValueError:
        return False


def allowed_navigation(target: str, origin: str) -> bool:
    if not trusted_origin(target, origin):
        return False
    path = urlsplit(target).path.rstrip("/") or "/"
    return path == "/desktop/mike" or path == "/id" or path.startswith("/id/")


class MikeWindow:
    def __init__(self, root: Path, origin: str):
        self.root = root
        self.origin = origin.rstrip("/")
        self.service = mike_service.MikeService("desktop", approvals=True)
        self.calls: queue.Queue = queue.Queue()
        self.attached = False
        self.service_down = False
        self.current_token = ""
        self.connecting_token = ""
        self.presented_approval = ""
        self.task_dialog_open = False
        self.request_running = False
        self.running_work = ""
        self.closed = False
        self.connect_state = ""
        self.inserting_token = False
        self.signing_out = False
        self.require_handoff = False
        self.handoff_marker = ""
        self.handoff_loaded = False
        self.session_generation = 0
        self.activity_seen: list = []
        threading.Thread(target=self.serve_calls, daemon=True).start()

        self.window = Gtk.Window(title="Mike")
        theme = Gtk.CssProvider()
        theme.load_from_data(b"""
            window { background: #0d1117; color: #edf2fa; }
            headerbar { background: #161b22; color: #edf2fa; border-bottom: 1px solid #303947; }
            headerbar button { background: #212b38; color: #edf2fa; border: 1px solid #3b4655;
                               border-radius: 7px; padding: 5px 10px; }
            headerbar button:disabled { color: #718095; background: #19212c; }
            label { color: #edf2fa; }
            expander { color: #dce7f5; }
            textview, textview text { background: #111a26; color: #dce7f5; font-family: monospace; }
            scrolledwindow { background: #111a26; border: 1px solid #303947; }
            .mike-welcome button { background: #3385fa; color: #ffffff; border: 0; border-radius: 8px;
                     padding: 10px 16px; }
            .mike-welcome button:hover { background: #4a96ff; }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), theme, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self.window.set_default_size(1050, 760)
        self.window.set_size_request(540, 420)
        self.window.connect("destroy", self.close)
        shell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.window.add(shell)
        header = Gtk.HeaderBar(title="Mike")
        header.set_show_close_button(True)
        self.stop_button = Gtk.Button(label="Stop command")
        self.stop_button.set_sensitive(False)
        self.stop_button.connect("clicked", self.stop_terminal)
        header.pack_start(self.stop_button)
        self.sign_out_button = Gtk.Button(label="Sign out")
        self.sign_out_button.set_sensitive(False)
        self.sign_out_button.connect("clicked", self.sign_out)
        header.pack_end(self.sign_out_button)
        self.window.set_titlebar(header)
        self.status = Gtk.Label(label="Connecting to the Mike service…")
        self.status.set_xalign(0)
        self.status.set_margin_start(14)
        self.status.set_margin_top(6)
        self.status.set_margin_bottom(6)
        shell.pack_start(self.status, False, False, 0)
        self.activity_panel = Gtk.Expander(label="Local activity")
        self.activity_panel.set_margin_start(14)
        self.activity_panel.set_margin_end(14)
        self.activity_panel.set_margin_bottom(6)
        activity_scroll = Gtk.ScrolledWindow()
        activity_scroll.set_size_request(-1, 120)
        self.activity_view = Gtk.TextView()
        self.activity_view.set_editable(False)
        self.activity_view.set_cursor_visible(False)
        self.activity_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        activity_scroll.add(self.activity_view)
        self.activity_panel.add(activity_scroll)
        shell.pack_start(self.activity_panel, False, False, 0)

        self.stack = Gtk.Stack()
        shell.pack_start(self.stack, True, True, 0)
        welcome = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        welcome.get_style_context().add_class("mike-welcome")
        welcome.set_halign(Gtk.Align.CENTER)
        welcome.set_valign(Gtk.Align.CENTER)
        heading = Gtk.Label()
        heading.set_markup('<span size="xx-large" weight="bold">Mike on your desktop</span>')
        welcome.pack_start(heading, False, False, 0)
        detail = Gtk.Label(label="Your conversations stay with Mike. Connect this computer to use local abilities.")
        detail.set_line_wrap(True)
        detail.set_max_width_chars(45)
        welcome.pack_start(detail, False, False, 0)
        button = Gtk.Button(label="Connect Mike")
        button.connect("clicked", self.connect_browser)
        welcome.pack_start(button, False, False, 0)
        self.stack.add_named(welcome, "welcome")

        data = Path.home() / ".local/share/mike-desktop/webkit"
        cache = Path.home() / ".cache/mike-desktop/webkit"
        data.mkdir(parents=True, exist_ok=True)
        cache.mkdir(parents=True, exist_ok=True)
        manager = WebKit2.WebsiteDataManager(base_data_directory=str(data), base_cache_directory=str(cache))
        context = WebKit2.WebContext.new_with_website_data_manager(manager)
        self.web = WebKit2.WebView.new_with_context(context)
        self.web.connect("decide-policy", self.decide_policy)
        self.web.connect("load-changed", self.load_changed)
        self.web.connect("load-failed", self.load_failed)
        self.web.connect("permission-request", self.permission_request)
        self.stack.add_named(self.web, "conversation")
        self.stack.set_visible_child_name("welcome")
        self.start_callback()
        self.web.load_uri(self.origin + "/desktop/mike")
        self.window.show_all()
        self.activity_panel.hide()
        self.service_call("start", None, self.service_started)
        GLib.timeout_add_seconds(2, self.poll)

    # ---- The shared service: every call runs on one worker thread ----

    def serve_calls(self) -> None:
        while True:
            action, payload, done = self.calls.get()
            if action is None:
                return
            try:
                status, body = 200, self.perform(action, payload)
            except mike_service.ServiceError as error:
                status, body = error.status, error.body
            except mike_service.ServiceUnavailable as error:
                status, body = 0, {"error": "unavailable", "message": str(error)}
            except Exception as error:  # a client bug must not kill the worker
                status, body = 0, {"error": "client", "message": str(error)[:200]}
            GLib.idle_add(done, status, body)

    def perform(self, action: str, payload):
        service = self.service
        if action == "start":
            launcher = self.root / "mike-service"
            if not launcher.exists():
                launcher = self.root / "bin/mike-service"
            env = None
            if not mike_service.unit_installed():
                env = dict(os.environ, MIKE_SERVICE_HOST_URL=self.origin)
            if not mike_service.ensure_running(launcher, env=env):
                raise mike_service.ServiceUnavailable("the Mike service did not start")
            return service.attach()
        if action == "attach":
            return service.attach(payload.get("token"), payload.get("connect", "none"))
        if action == "state":
            return service.state()
        if action == "claim":
            return service.claim(payload["approval_id"])
        if action == "answer":
            fields = dict(payload)
            return service.answer(fields.pop("approval_id"), fields.pop("decision"), **fields)
        if action == "stop":
            return service.stop(payload["work_id"])
        if action == "signout":
            return service.sign_out()
        if action == "detach":
            service.detach()
            return {}
        raise ValueError("unknown action " + action)

    def service_call(self, action: str, payload, done) -> None:
        self.calls.put((action, payload, done))

    def service_started(self, status, body) -> bool:
        if self.closed:
            return False
        if status != 200:
            self.service_down = True
            self.set_status("The Mike service is not running. Start it with `mike-service start`, then reopen Mike.")
            return False
        self.attached = True
        self.service_down = False
        self.observe(body)
        self.read_session()
        return False

    # ---- Browser handoff: the only way the account on this computer changes ----

    def start_callback(self) -> None:
        owner = self

        class Callback(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path != "/callback" or self.headers.get("Origin") not in (owner.origin, "null"):
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if not 0 < length <= 4096:
                    self.send_error(400)
                    return
                try:
                    fields = parse_qs(self.rfile.read(length).decode("utf-8"), strict_parsing=True)
                except (UnicodeDecodeError, ValueError):
                    self.send_error(400)
                    return
                state = fields.get("state", [""])[0]
                token = fields.get("token", [""])[0]
                if not owner.connect_state or not secrets.compare_digest(state, owner.connect_state) or not token:
                    self.send_error(403)
                    return
                owner.connect_state = ""
                GLib.idle_add(owner.accept_handoff, token)
                page = b"<!doctype html><title>Mike connected</title><p>Connecting Mike. You can return to the desktop window.</p>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Security-Policy", "default-src 'none'")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)

            def log_message(self, *_args):
                pass

        self.callback = HTTPServer(("127.0.0.1", 0), Callback)
        threading.Thread(target=self.callback.serve_forever, daemon=True).start()

    def connect_browser(self, *_args) -> None:
        self.connect_state = secrets.token_urlsafe(32)
        query = urlencode({"port": self.callback.server_port, "state": self.connect_state})
        webbrowser.open(self.origin + "/desktop/connect?" + query)
        self.set_status("Finish signing in and approve this desktop in your browser")

    def accept_handoff(self, token: str) -> bool:
        """An explicit browser approval: the service may switch accounts or
        pair again after revocation. A running command of the previous
        account is stopped and reported by the service, not by this window."""
        if self.closed:
            return False
        if not self.attached:
            GLib.timeout_add(200, self.accept_handoff, token)
            return False
        if self.current_token and self.current_token != token:
            self.clear_activity()
        self.set_status("Connecting your desktop…")
        self.session_generation += 1
        self.signing_out = False
        self.require_handoff = False
        generation = self.session_generation
        self.connecting_token = ""
        self.inserting_token = True
        self.handoff_loaded = False

        def connected(status, body):
            if generation != self.session_generation or self.closed:
                return False
            if status != 200:
                self.inserting_token = False
                self.set_status("Connection failed. Try Connect Mike again.")
                return False
            self.current_token = token
            self.sign_out_button.set_sensitive(True)
            self.observe(body)
            self.install_web_token(token)
            self.stack.set_visible_child_name("conversation")
            self.set_status("Desktop connected")
            return False

        self.service_call("attach", {"token": token, "connect": "switch"}, connected)
        return False

    def install_web_token(self, token: str) -> None:
        self.session_generation += 1
        self.inserting_token = True
        self.handoff_loaded = False
        self.handoff_marker = secrets.token_urlsafe(12)
        self.web.run_javascript(
            "localStorage.setItem('id:token', " + json.dumps(token) + "); location.replace('/desktop/mike?handoff=" + self.handoff_marker + "')",
            None, None,
        )

    def forget_web_session(self) -> None:
        self.web.run_javascript(
            "localStorage.removeItem('id:token'); localStorage.removeItem('id:who'); "
            "localStorage.removeItem('id:role')", None, None,
        )

    def set_status(self, message: str) -> None:
        self.status.set_text(message)

    # ---- Activity comes from the service, scoped to this computer's owner ----

    def show_activity(self, entries: list) -> None:
        if entries == self.activity_seen:
            return
        self.activity_seen = list(entries)
        if not entries:
            self.clear_activity()
            return
        self.activity_view.get_buffer().set_text(activity_text(entries))
        self.activity_panel.show_all()
        self.activity_panel.set_expanded(True)

    def clear_activity(self) -> None:
        self.activity_seen = []
        self.activity_view.get_buffer().set_text("")
        self.activity_panel.hide()

    def decide_policy(self, _view, decision, decision_type) -> bool:
        if decision_type not in (
            WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
            WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION,
        ):
            return False
        request = decision.get_navigation_action().get_request()
        target = request.get_uri()
        if decision_type == WebKit2.PolicyDecisionType.NAVIGATION_ACTION and allowed_navigation(target, self.origin):
            return False
        decision.ignore()
        if target.startswith(("https://", "http://")):
            webbrowser.open(target)
        return True

    def load_changed(self, _view, event) -> None:
        if event == WebKit2.LoadEvent.FINISHED:
            uri = self.web.get_uri() or ""
            if trusted_origin(uri, self.origin):
                if self.signing_out:
                    self.signing_out = False
                if self.inserting_token and self.handoff_marker:
                    marker = parse_qs(urlsplit(uri).query).get("handoff", [""])[0]
                    if marker == self.handoff_marker:
                        self.handoff_loaded = True
                self.read_session()

    def load_failed(self, _view, _event, _uri, _error) -> bool:
        self.set_status("Mike is offline. Check your connection and reopen the window.")
        return False

    def permission_request(self, _view, request) -> bool:
        # The web page gets only a normal, visible platform permission prompt.
        kind = request.__class__.__name__
        question = Gtk.MessageDialog(
            transient_for=self.window, modal=True, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO, text=f"Allow Mike to use {kind}?",
        )
        approved = question.run() == Gtk.ResponseType.YES
        question.destroy()
        request.allow() if approved else request.deny()
        return True

    def read_session(self) -> None:
        if self.signing_out or not self.attached:
            return
        uri = self.web.get_uri() or ""
        if not trusted_origin(uri, self.origin):
            return
        generation = self.session_generation
        self.web.run_javascript(
            "window.localStorage.getItem('id:token') || ''", None,
            lambda view, result: self.got_session(view, result, uri) if generation == self.session_generation else None,
        )

    def got_session(self, view, result, read_uri: str | None = None) -> None:
        try:
            token = view.run_javascript_finish(result).get_js_value().to_string()
        except Exception:
            return
        if not trusted_origin(view.get_uri() or "", self.origin):
            return
        if self.inserting_token:
            if not self.handoff_loaded:
                return
            observed_uri = read_uri or view.get_uri() or ""
            marker = parse_qs(urlsplit(observed_uri).query).get("handoff", [""])[0]
            if marker != self.handoff_marker:
                return
            if token == self.current_token:
                self.inserting_token = False
                self.handoff_marker = ""
                self.session_generation += 1
            else:
                # The page did not take the approved session. Detach this
                # window only; the shared service keeps its account.
                self.inserting_token = False
                self.current_token = ""
                self.sign_out_button.set_sensitive(False)
                self.clear_activity()
                self.stack.set_visible_child_name("welcome")
                self.set_status("Desktop handoff did not reach the Mike window. Connect again.")
            return
        if token and self.require_handoff:
            self.forget_web_session()
            return
        if not token:
            if not self.current_token and not self.connecting_token:
                return
            # Signing out in the page signs this computer out for every client.
            self.session_generation += 1
            self.require_handoff = True
            self.clear_activity()
            self.current_token = ""
            self.sign_out_button.set_sensitive(False)
            self.connecting_token = ""
            self.service_call("signout", {}, lambda _status, _body: False)
            self.stack.set_visible_child_name("welcome")
            self.set_status("Sign in to connect this desktop")
            return
        if token == self.current_token or token == self.connecting_token:
            return
        if self.current_token:
            # The native owner changes only through a browser handoff. A stale
            # WebKit profile cannot silently switch the service's account.
            self.install_web_token(self.current_token)
            self.set_status("Keeping Mike connected to the approved desktop account")
            return
        self.connecting_token = token
        self.session_generation += 1
        generation = self.session_generation
        self.set_status("Connecting your desktop…")

        def connected(status, body):
            if generation != self.session_generation or token != self.connecting_token or self.closed:
                return False
            self.connecting_token = ""
            if status == 200:
                self.current_token = token
                self.sign_out_button.set_sensitive(True)
                self.stack.set_visible_child_name("conversation")
                self.set_status("Desktop connected")
                self.observe(body)
                return False
            self.forget_web_session()
            self.require_handoff = True
            self.stack.set_visible_child_name("welcome")
            error = body.get("error") if isinstance(body, dict) else ""
            if error == "account_mismatch":
                self.set_status("This computer's Mike service is connected to another account. Connect Mike to switch.")
            elif error == "connect_required":
                self.set_status("This computer's access was revoked. Connect Mike to pair it again.")
            else:
                self.set_status("Desktop connection unavailable. Try Connect Mike again.")
            return False

        self.service_call("attach", {"token": token, "connect": "auto"}, connected)

    # ---- Polling the shared service ----

    def poll(self) -> bool:
        if self.closed:
            return False
        if not self.attached:
            return True
        self.read_session()
        if self.request_running:
            return True
        self.request_running = True

        def received(status, body):
            self.request_running = False
            if status == 200:
                self.observe(body)
            elif status == 409 and body.get("error") == "session_ended":
                self.session_ended(body.get("reason", ""))
            elif status == 401:
                # The service restarted (new key) or forgot this session.
                self.reattach()
            else:
                self.set_status("The Mike service is unavailable. Local tasks are paused; Mike's chat may still work.")
            return False

        self.service_call("state", None, received)
        return True

    def reattach(self) -> None:
        def attached(status, body):
            if status != 200:
                self.set_status("The Mike service is unavailable. Local tasks are paused; Mike's chat may still work.")
                return False
            self.observe(body)
            if not body.get("account") and self.current_token and not self.require_handoff:
                # After a service restart the account session lives only here.
                self.service_call("attach", {"token": self.current_token, "connect": "auto"},
                                  lambda s, b: self.observe(b) if s == 200 else False)
            return False

        self.service_call("attach", {}, attached)

    def session_ended(self, reason: str) -> None:
        """Another client (or the service) ended this computer's session:
        drop everything this window showed for the previous owner."""
        self.clear_activity()
        self.presented_approval = ""
        self.running_work = ""
        self.stop_button.set_sensitive(False)
        if reason in ("signed_out", "device_revoked") or reason == "account_changed" and self.current_token:
            self.session_generation += 1
            self.current_token = ""
            self.connecting_token = ""
            self.require_handoff = True
            self.sign_out_button.set_sensitive(False)
            self.forget_web_session()
            self.stack.set_visible_child_name("welcome")
            self.set_status({
                "signed_out": "This computer was signed out of Mike.",
                "device_revoked": "This computer's access was revoked. Connect Mike to pair it again.",
            }.get(reason, "This computer's Mike service changed accounts. Connect Mike to continue."))
        self.reattach()

    def observe(self, state) -> bool:
        if not isinstance(state, dict) or self.closed:
            return False
        if state.get("host") and state["host"] != self.origin:
            self.set_status("The Mike service talks to " + str(state["host"]) + ", not this window's " + self.origin)
            return False
        self.show_activity(state.get("activity") or [])
        connection = state.get("connection")
        work = state.get("work")
        if connection == "revoked" and self.current_token:
            self.current_token = ""
            self.require_handoff = True
            self.sign_out_button.set_sensitive(False)
            self.forget_web_session()
            self.stack.set_visible_child_name("welcome")
            self.set_status("Desktop access revoked. Connect Mike again to resume local tasks.")
            return False
        running = isinstance(work, dict) and work.get("stage") == "running"
        self.running_work = work.get("id", "") if running else ""
        self.stop_button.set_sensitive(running)
        if running:
            self.set_status("Mike is running: " + shlex.join(work.get("argv") or [])[:120])
        elif isinstance(work, dict) and work.get("claim") == "other":
            self.set_status("Another Mike window on this computer is reviewing Mike's request.")
        elif connection == "connected":
            self.set_status("Desktop connected")
        elif self.current_token:
            self.set_status("Desktop offline. Local tasks are paused.")
        if isinstance(work, dict) and not self.task_dialog_open:
            approval = work.get("approval_id") or ""
            if approval and approval != self.presented_approval and work.get("claim") in ("none", "you"):
                self.presented_approval = approval
                self.present(work)
        return False

    # ---- Approvals: claimed by this window, answered once ----

    def present(self, work: dict) -> None:
        approval = work["approval_id"]

        def claimed(status, body):
            if status != 200:
                if body.get("error") == "claimed_elsewhere":
                    self.set_status("Another Mike window on this computer is reviewing Mike's request.")
                return False
            if work.get("kind") == "terminal":
                self.review_terminal(work)
            elif work.get("stage") == "file_confirm":
                self.review_file(work)
            else:
                self.review_search(work)
            return False

        self.service_call("claim", {"approval_id": approval}, claimed)

    def answer(self, work: dict, decision: str, done=None, **fields) -> None:
        def answered(status, body):
            if status == 409 and body.get("error") == "already_decided":
                self.set_status("That request was already answered.")
            elif status not in (200,) and done is None:
                self.set_status("Mike's request: " + str(body.get("message", "could not be answered"))[:160])
            if done is not None:
                done(status, body)
            return False

        self.service_call("answer", {"approval_id": work["approval_id"], "decision": decision, **fields}, answered)

    def review_search(self, work: dict) -> None:
        self.task_dialog_open = True
        prompt = Gtk.MessageDialog(
            transient_for=self.window, modal=True, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL, text="Mike requests a local file search",
        )
        prompt.format_secondary_text(
            "Choose a folder to search. Mike cannot choose the folder or open a file without your confirmation."
        )
        allowed = prompt.run() == Gtk.ResponseType.OK
        prompt.destroy()
        if not allowed:
            self.task_dialog_open = False
            self.answer(work, "decline")
            return
        chooser = Gtk.FileChooserDialog(
            title="Choose the folder Mike may search", transient_for=self.window,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        chooser.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Choose folder", Gtk.ResponseType.OK)
        folder = chooser.get_filename() if chooser.run() == Gtk.ResponseType.OK else None
        chooser.destroy()
        if not folder:
            self.task_dialog_open = False
            self.answer(work, "decline")
            return
        dialog = Gtk.Dialog(title="Find a file", transient_for=self.window, modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Search", Gtk.ResponseType.OK)
        entry = Gtk.Entry()
        entry.set_placeholder_text("Filename words")
        entry.set_activates_default(True)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.get_content_area().pack_start(entry, True, True, 12)
        dialog.show_all()
        query = entry.get_text().strip() if dialog.run() == Gtk.ResponseType.OK else ""
        dialog.destroy()
        self.task_dialog_open = False
        if len(query) < 2:
            self.answer(work, "decline")
            return

        def searched(status, body):
            if status != 200:
                self.set_status("File search: " + str(body.get("message", ""))[:160])
            return False

        self.answer(work, "approve", searched, folder=folder, query=query)

    def review_file(self, work: dict) -> None:
        path = work.get("file") or ""
        self.task_dialog_open = True
        dialog = Gtk.MessageDialog(
            transient_for=self.window, modal=True, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO, text="Open this exact file?",
        )
        dialog.format_secondary_text(path)
        approved = dialog.run() == Gtk.ResponseType.YES
        dialog.destroy()
        self.task_dialog_open = False
        if approved:
            self.set_status("Opening the reviewed file…")

        def decided(status, _body):
            if status != 200:
                self.set_status("The file was not opened. Check the desktop portal and try again.")
            elif approved:
                self.set_status("The reviewed file is being handed to the desktop.")
            return False

        self.answer(work, "approve" if approved else "decline", decided)

    def review_terminal(self, work: dict) -> None:
        argv, cwd = work.get("argv"), work.get("cwd")
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
            self.set_status("The terminal request was invalid.")
            self.answer(work, "decline")
            return
        self.task_dialog_open = True
        dialog = Gtk.MessageDialog(
            transient_for=self.window, modal=True, message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO, text="Allow Mike to run this command?",
        )
        dialog.format_secondary_text("On this computer, as your user:\n" + shlex.join(argv) + "\n\nWorking directory: " + str(cwd) + "\n\nBounded output will be sent to Mike.")
        approved = dialog.run() == Gtk.ResponseType.YES
        dialog.destroy()
        self.task_dialog_open = False

        def decided(status, body):
            if status != 200:
                self.set_status("The terminal step could not start: " + str(body.get("message", ""))[:120])
            return False

        self.answer(work, "approve" if approved else "decline", decided)

    def stop_terminal(self, *_args) -> None:
        if not self.running_work:
            return
        self.set_status("Stopping Mike's command…")
        self.service_call("stop", {"work_id": self.running_work}, lambda _status, _body: False)

    def sign_out(self, *_args) -> None:
        """Sign this computer out for every local client. The service stops
        and reports a running command before forgetting the account."""
        self.clear_activity()
        self.session_generation += 1
        self.signing_out = True
        self.require_handoff = True
        self.current_token = ""
        self.sign_out_button.set_sensitive(False)
        self.connecting_token = ""
        self.connect_state = ""
        self.inserting_token = False
        self.handoff_marker = ""
        self.stack.set_visible_child_name("welcome")
        self.set_status("Signed out. Connect Mike to use this desktop again.")
        self.service_call("signout", {}, lambda _status, _body: False)
        self.web.run_javascript(
            "localStorage.removeItem('id:token'); localStorage.removeItem('id:who'); "
            "localStorage.removeItem('id:role'); location.replace('/desktop/mike')",
            None, None,
        )

    def close(self, *_args) -> None:
        """Closing the window detaches this client only: the service keeps the
        pairing and its task inbox, and nothing pending is approved."""
        if self.closed:
            return
        self.closed = True
        self.callback.shutdown()
        self.callback.server_close()
        if self.attached:
            self.service.detach()
        self.calls.put((None, None, None))
        Gtk.main_quit()


def activity_text(entries: list) -> str:
    lines = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        argv = entry.get("argv") if isinstance(entry.get("argv"), list) else []
        if kind == "requested" and argv:
            lines.append("Requested: " + shlex.join(argv) + "\nDirectory: " + str(entry.get("cwd")))
        elif kind == "requested":
            lines.append("Requested: " + str(entry.get("text") or "a local file task"))
        elif kind == "running":
            lines.append("Running…")
        elif kind == "found":
            lines.append("Found: " + str(entry.get("text")))
        elif kind == "result":
            summary = "Result: " + str(entry.get("state"))
            if entry.get("exit_code") is not None:
                summary += " (exit " + str(entry["exit_code"]) + ")"
            if entry.get("stdout"):
                summary += "\nstdout:\n" + str(entry["stdout"])
            if entry.get("stderr"):
                summary += "\nstderr:\n" + str(entry["stderr"])
            if not argv and entry.get("text"):
                summary += "\n" + str(entry["text"])
            lines.append(summary)
        elif entry.get("text"):
            lines.append(str(entry["text"]))
    return ("\n".join(lines))[-16000:]


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    origin = os.environ.get("MIKE_DESKTOP_HOST_URL", PUBLIC_ORIGIN)
    if origin != PUBLIC_ORIGIN and not (
        origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:")
    ):
        raise SystemExit("Mike only connects to its public host or a local test host")
    MikeWindow(root, origin)
    Gtk.main()


if __name__ == "__main__":
    main()
