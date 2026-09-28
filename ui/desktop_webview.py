#!/usr/bin/python3
"""Mike's focused WebKit window and trusted local dialogs.

The Euglena Code process owns the device identity, task queue, and file opener.
This process only presents the first-party web conversation and collects local
decisions. Web content has no script-message handler or direct bridge access.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
import webbrowser

try:
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    gi.require_version("WebKit2", "4.1")
    from gi.repository import Gdk, GLib, Gtk, WebKit2
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
    def __init__(self, root: Path, code_bin: str, origin: str):
        self.root = root
        self.origin = origin.rstrip("/")
        self.secret = secrets.token_urlsafe(48)
        self.runtime = tempfile.TemporaryDirectory(prefix="mike-", dir=os.environ.get("XDG_RUNTIME_DIR"))
        self.port: int | None = None
        self.current_token = ""
        self.connecting_token = ""
        self.active_task = ""
        self.task_dialog_open = False
        self.request_running = False
        self.opening_file = False
        self.closed = False
        self.connect_state = ""
        self.inserting_token = False

        child_env = os.environ.copy()
        child_env["MIKE_DESKTOP_BRIDGE_SECRET"] = self.secret
        child_env["MIKE_DESKTOP_BRIDGE_DIR"] = self.runtime.name
        self.child = subprocess.Popen(
            [code_bin, "run", "main.code"], cwd=root, env=child_env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        self.window = Gtk.Window(title="Mike")
        theme = Gtk.CssProvider()
        theme.load_from_data(b"""
            window { background: #0d1117; color: #edf2fa; }
            headerbar { background: #161b22; color: #edf2fa; border-bottom: 1px solid #303947; }
            label { color: #edf2fa; }
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
        self.window.set_titlebar(header)
        self.status = Gtk.Label(label="Connecting desktop…")
        self.status.set_xalign(0)
        self.status.set_margin_start(14)
        self.status.set_margin_top(6)
        self.status.set_margin_bottom(6)
        shell.pack_start(self.status, False, False, 0)

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
        GLib.timeout_add(200, self.wait_for_bridge)
        GLib.timeout_add_seconds(2, self.poll)

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
        if self.closed:
            return False
        if self.port is None:
            GLib.timeout_add(200, self.accept_handoff, token)
            return False
        self.set_status("Connecting your desktop…")

        def connected(status, _body):
            if status != 200:
                self.set_status("Connection failed. Try Connect Mike again.")
                return False
            self.current_token = token
            self.inserting_token = True
            self.web.run_javascript(
                "localStorage.setItem('id:token', " + json.dumps(token) + "); location.replace('/desktop/mike')",
                None, None,
            )
            self.stack.set_visible_child_name("conversation")
            self.set_status("Desktop connected")
            return False

        self.async_bridge("/session", {"token": token}, connected)
        return False

    def set_status(self, message: str) -> None:
        self.status.set_text(message)

    def wait_for_bridge(self) -> bool:
        if self.closed:
            return False
        if self.child.poll() is not None:
            self.set_status("The desktop companion stopped. Close and reopen Mike.")
            return False
        port_file = Path(self.runtime.name) / "port"
        if not port_file.exists():
            return True
        try:
            port = int(port_file.read_text())
            if not 0 < port < 65536:
                raise ValueError("invalid port")
            self.port = port
        except (OSError, ValueError):
            self.set_status("The desktop companion could not connect.")
            return False
        self.set_status("Sign in to connect this desktop")
        return False

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

    def bridge(self, path: str, payload: dict | None = None) -> tuple[int, str]:
        if self.port is None:
            raise ConnectionError("companion is starting")
        body = None if payload is None else json.dumps(payload).encode()
        request = Request(
            f"http://127.0.0.1:{self.port}{path}", data=body,
            headers={"Authorization": "Bearer " + self.secret, "Content-Type": "application/json"},
            method="GET" if body is None else "POST",
        )
        try:
            with urlopen(request, timeout=160 if path == "/task/decide" else 18) as response:
                return response.status, response.read(32768).decode()
        except Exception as error:
            if hasattr(error, "code"):
                return error.code, error.read(32768).decode()
            raise

    def async_bridge(self, path: str, payload: dict | None, done) -> None:
        def worker():
            try:
                result = self.bridge(path, payload)
            except Exception as error:
                result = (0, str(error))
            GLib.idle_add(done, *result)
        threading.Thread(target=worker, daemon=True).start()

    def read_session(self) -> None:
        if not trusted_origin(self.web.get_uri() or "", self.origin):
            return
        self.web.run_javascript("window.localStorage.getItem('id:token') || ''", None, self.got_session)

    def got_session(self, view, result) -> None:
        try:
            token = view.run_javascript_finish(result).get_js_value().to_string()
        except Exception:
            return
        if not trusted_origin(view.get_uri() or "", self.origin):
            return
        if token == self.current_token or token == self.connecting_token:
            if token and self.inserting_token:
                self.inserting_token = False
            return
        if not token:
            if self.inserting_token:
                return
            self.current_token = ""
            self.connecting_token = ""
            self.async_bridge("/disconnect", {}, lambda _status, _body: False)
            self.stack.set_visible_child_name("welcome")
            self.set_status("Sign in to connect this desktop")
            return
        self.connecting_token = token
        self.set_status("Connecting your desktop…")

        def connected(status, _body):
            self.connecting_token = ""
            if status == 200:
                self.current_token = token
                self.stack.set_visible_child_name("conversation")
                self.set_status("Desktop connected")
            else:
                self.web.run_javascript("localStorage.removeItem('id:token'); localStorage.removeItem('id:who'); localStorage.removeItem('id:role')", None, None)
                self.stack.set_visible_child_name("welcome")
                self.set_status("Desktop connection unavailable. Try Connect Mike again.")
            return False

        self.async_bridge("/session", {"token": token}, connected)

    def poll(self) -> bool:
        if self.closed:
            return False
        if self.port is None:
            return True
        self.read_session()
        if self.request_running:
            return True
        self.request_running = True

        def received(status, body):
            self.request_running = False
            if status != 200:
                self.set_status("Desktop companion offline. Mike's chat may still work.")
                return False
            try:
                state = json.loads(body)
            except ValueError:
                return False
            if self.opening_file:
                pass
            elif state.get("connection") == "connected":
                self.set_status("Desktop connected")
            elif state.get("connection") == "revoked":
                self.stack.set_visible_child_name("welcome")
                self.set_status("Desktop access revoked. Connect Mike again to resume local tasks.")
            elif self.current_token:
                self.set_status("Desktop offline. Local tasks are paused.")
            task = state.get("task_id") or ""
            if not task:
                self.active_task = ""
            elif not self.task_dialog_open:
                mode = state.get("mode")
                if mode in ("root_task", "query") and task != self.active_task:
                    self.active_task = task
                    self.review_search(state)
                elif mode == "file_confirm":
                    self.review_file(state)
            return False

        self.async_bridge("/state", None, received)
        return True

    def review_search(self, state: dict) -> None:
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
            self.decline_task()
            return
        chooser = Gtk.FileChooserDialog(
            title="Choose the folder Mike may search", transient_for=self.window,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        chooser.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Choose folder", Gtk.ResponseType.OK)
        folder = chooser.get_filename() if chooser.run() == Gtk.ResponseType.OK else None
        chooser.destroy()
        if not folder:
            self.decline_task()
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
            self.decline_task()
            return
        self.async_bridge("/task/search", {"folder": folder, "query": query}, self.searched)

    def searched(self, status, body) -> bool:
        if status == 200:
            self.active_task = ""
        else:
            self.set_status("File search: " + body[:160])
            self.decline_task()
        return False

    def review_file(self, state: dict) -> None:
        path = state.get("file") or ""
        if not path:
            self.decline_task()
            return
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
            self.opening_file = True
            self.set_status("Opening the reviewed file…")

        def decided(status, _body):
            self.opening_file = False
            if status != 200:
                self.set_status("The file was not opened. Check the desktop portal and try again.")
            elif approved:
                self.set_status("The reviewed file was handed to the desktop.")
            return False

        self.async_bridge("/task/decide", {"yes": approved}, decided)

    def decline_task(self) -> None:
        self.task_dialog_open = False
        self.async_bridge("/task/decline", {}, lambda _status, _body: False)

    def close(self, *_args) -> None:
        if self.closed:
            return
        self.closed = True
        self.callback.shutdown()
        self.callback.server_close()
        self.child.terminate()
        try:
            self.child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait()
        self.runtime.cleanup()
        Gtk.main_quit()


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    origin = os.environ.get("MIKE_DESKTOP_HOST_URL", PUBLIC_ORIGIN)
    if origin != PUBLIC_ORIGIN and not (
        origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:")
    ):
        raise SystemExit("Mike only connects to its public host or a local test host")
    code_bin = os.environ.get("MIKE_DESKTOP_CODE_BIN", "cdlvsm-code")
    MikeWindow(root, code_bin, origin)
    Gtk.main()


if __name__ == "__main__":
    main()
