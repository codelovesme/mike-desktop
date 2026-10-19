import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("desktop_webview", Path(__file__).resolve().parents[1] / "ui/desktop_webview.py")
webview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(webview)


class DesktopAppIdTest(unittest.TestCase):
    def test_matches_the_menu_entry_cdlvsm_writes(self):
        packages = Path("/prefix/share/cdlvsm/packages")
        self.assertEqual(webview.desktop_app_id(packages / "mike/v0.4.1/ui/desktop_webview.py"), "codelovesme-mike")
        self.assertEqual(webview.desktop_app_id(packages / "mike-desktop/v0.4.1/ui/desktop_webview.py"), "codelovesme-mike-desktop")
        self.assertEqual(webview.desktop_app_id(Path("/src/mike-desktop/ui/desktop_webview.py")), "codelovesme-mike")


class ServiceLauncherTest(unittest.TestCase):
    def test_the_service_cdlvsm_resolved_comes_first(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "mike/v0.5.0"
            service = Path(temporary) / "mike-service/current"
            for directory in (root / ".cdlvsm-deps", service):
                directory.mkdir(parents=True)
            (service / "mike-service").write_text("#!/bin/sh\n")
            (service / "mike-service").chmod(0o755)
            (root / ".cdlvsm-deps/mike-service").symlink_to(service)
            beside = root / "mike-service"
            beside.write_text("#!/bin/sh\n")
            beside.chmod(0o755)
            with patch.dict(os.environ, {"PATH": "/nonexistent"}):
                os.environ.pop("MIKE_SERVICE_LAUNCHER", None)
                self.assertEqual(webview.service_launcher(root), root / ".cdlvsm-deps/mike-service/mike-service")
                (root / ".cdlvsm-deps/mike-service").unlink()
                self.assertEqual(webview.service_launcher(root), beside)
                beside.unlink()
                self.assertIsNone(webview.service_launcher(root))


class NavigationTest(unittest.TestCase):
    ORIGIN = "https://apps.codeloves.me"

    def test_mike_and_account_are_the_only_embedded_pages(self):
        for path in ("/desktop/mike", "/id", "/id/account", "/id/oauth/google?code=short"):
            self.assertTrue(webview.allowed_navigation(self.ORIGIN + path, self.ORIGIN), path)
        for path in ("/", "/todo", "/host", "/mike"):
            self.assertFalse(webview.allowed_navigation(self.ORIGIN + path, self.ORIGIN), path)

    def test_other_origins_and_malformed_ports_cannot_stay_embedded(self):
        for target in (
            "https://apps.codeloves.me.evil.test/desktop/mike",
            "http://apps.codeloves.me/desktop/mike",
            "https://evil.test@apps.codeloves.me/desktop/mike",
            "https://apps.codeloves.me:bad/desktop/mike",
            "javascript:alert(1)",
        ):
            self.assertFalse(webview.allowed_navigation(target, self.ORIGIN), target)



def bare_window(origin=NavigationTest.ORIGIN):
    """A MikeWindow without GTK widgets or a service; collaborators are mocks."""
    window = webview.MikeWindow.__new__(webview.MikeWindow)
    window.origin = origin
    window.attached = True
    window.closed = False
    window.session_generation = 0
    window.inserting_token = False
    window.handoff_loaded = False
    window.handoff_marker = ""
    window.require_handoff = False
    window.signing_out = False
    window.current_token = ""
    window.connecting_token = ""
    window.connect_state = ""
    window.task_dialog_open = False
    window.presented_approval = ""
    window.running_work = ""
    window.activity_seen = []
    window.service_call = Mock()
    window.set_status = Mock()
    window.clear_activity = Mock()
    window.show_activity = Mock()
    window.forget_web_session = Mock()
    window.install_web_token = Mock()
    window.sign_out_button = Mock()
    window.stop_button = Mock()
    window.stack = Mock()
    window.web = Mock()
    return window


def page_token(token, uri=NavigationTest.ORIGIN + "/desktop/mike"):
    view = Mock()
    view.get_uri.return_value = uri
    view.run_javascript_finish.return_value.get_js_value.return_value.to_string.return_value = token
    return view


class ServiceClientWindowTest(unittest.TestCase):
    def test_old_webview_session_cannot_replace_browser_handoff(self):
        window = bare_window()
        window.inserting_token = True
        window.handoff_marker = "pending"
        window.current_token = "new-account-token"
        window.got_session(page_token("old-account-token"), None)
        self.assertTrue(window.inserting_token)
        window.service_call.assert_not_called()
        window.handoff_loaded = True
        window.session_generation = 1
        window.got_session(page_token("new-account-token", NavigationTest.ORIGIN + "/desktop/mike?handoff=pending"), None)
        self.assertFalse(window.inserting_token)
        self.assertEqual(window.session_generation, 2)

    def test_failed_handoff_detaches_this_window_without_signing_out_others(self):
        window = bare_window()
        window.inserting_token = True
        window.handoff_loaded = True
        window.handoff_marker = "pending"
        window.current_token = "approved-token"
        window.got_session(page_token("other-token", NavigationTest.ORIGIN + "/desktop/mike?handoff=pending"), None)
        self.assertEqual(window.current_token, "")
        window.service_call.assert_not_called()

    def test_pending_session_read_is_discarded_after_handoff(self):
        window = bare_window()
        window.web.get_uri.return_value = NavigationTest.ORIGIN + "/desktop/mike"
        window.got_session = Mock()
        window.read_session()
        callback = window.web.run_javascript.call_args.args[2]
        window.session_generation = 1
        callback(window.web, object())
        window.got_session.assert_not_called()

    def test_saved_page_session_connects_only_automatically(self):
        window = bare_window()
        window.got_session(page_token("page-token"), None)
        action, payload, _done = window.service_call.call_args.args
        self.assertEqual((action, payload), ("attach", {"token": "page-token", "connect": "auto"}))

    def test_old_connection_callback_cannot_switch_new_account(self):
        window = bare_window()
        window.got_session(page_token("old-account-token"), None)
        old_callback = window.service_call.call_args.args[2]
        window.session_generation += 1
        window.connecting_token = ""
        old_callback(200, {"connection": "connected"})
        self.assertEqual(window.current_token, "")

    def test_another_account_on_the_service_needs_an_explicit_connect(self):
        window = bare_window()
        window.got_session(page_token("page-token"), None)
        done = window.service_call.call_args.args[2]
        done(409, {"error": "account_mismatch"})
        self.assertEqual(window.current_token, "")
        self.assertTrue(window.require_handoff)
        window.forget_web_session.assert_called_once()
        self.assertIn("another account", window.set_status.call_args.args[0])

    def test_saved_profile_cannot_switch_approved_native_owner(self):
        window = bare_window()
        window.current_token = "approved-account-token"
        window.got_session(page_token("stale-account-token"), None)
        window.install_web_token.assert_called_once_with("approved-account-token")
        window.service_call.assert_not_called()

    def test_browser_handoff_is_the_explicit_account_switch(self):
        window = bare_window()
        window.current_token = "old-token"
        window.accept_handoff("new-token")
        action, payload, _done = window.service_call.call_args.args
        self.assertEqual((action, payload), ("attach", {"token": "new-token", "connect": "switch"}))
        window.clear_activity.assert_called_once()

    def test_native_sign_out_signs_out_the_service_before_page_reload(self):
        window = bare_window()
        window.current_token = "account-token"
        window.running_work = "step-1"
        window.sign_out()
        self.assertEqual(window.current_token, "")
        self.assertEqual(window.session_generation, 1)
        self.assertTrue(window.signing_out and window.require_handoff)
        self.assertEqual(window.service_call.call_args.args[:2], ("signout", {}))
        script = window.web.run_javascript.call_args.args[0]
        self.assertIn("localStorage.removeItem('id:token')", script)
        self.assertIn("location.replace('/desktop/mike')", script)

    def test_saved_token_cannot_reconnect_after_native_sign_out(self):
        window = bare_window()
        window.require_handoff = True
        window.got_session(page_token("stale-token"), None)
        window.forget_web_session.assert_called_once()
        window.service_call.assert_not_called()

    def test_web_sign_out_signs_out_the_service_and_clears_activity(self):
        window = bare_window()
        window.current_token = "account-token"
        window.got_session(page_token(""), None)
        window.clear_activity.assert_called_once()
        self.assertEqual(window.service_call.call_args.args[:2], ("signout", {}))
        self.assertEqual(window.current_token, "")
        self.assertTrue(window.require_handoff)

    def test_closing_the_window_only_detaches(self):
        window = bare_window()
        window.callback = Mock()
        window.service = Mock()
        window.calls = Mock()
        original = webview.Gtk.main_quit
        webview.Gtk.main_quit = Mock()
        try:
            window.close()
        finally:
            webview.Gtk.main_quit = original
        window.service.detach.assert_called_once()
        window.service.sign_out.assert_not_called()
        window.service_call.assert_not_called()

    def test_each_approval_is_presented_once_and_not_over_another_window(self):
        window = bare_window()
        window.present = Mock()
        work = {"id": "step-1", "kind": "terminal", "stage": "awaiting_approval", "claim": "none",
                "approval_id": "a1", "argv": ["pwd"], "cwd": "/tmp"}
        window.observe({"connection": "connected", "work": work, "activity": []})
        window.observe({"connection": "connected", "work": work, "activity": []})
        window.present.assert_called_once_with(work)
        window.observe({"connection": "connected", "work": dict(work, approval_id="a2", claim="other"), "activity": []})
        window.present.assert_called_once()
        self.assertIn("Another Mike window", window.set_status.call_args.args[0])

    def test_running_work_enables_stop_for_that_work(self):
        window = bare_window()
        window.observe({"connection": "connected", "activity": [],
                        "work": {"id": "step-9", "stage": "running", "argv": ["sleep", "5"], "claim": "you"}})
        self.assertEqual(window.running_work, "step-9")
        window.stop_button.set_sensitive.assert_called_with(True)
        window.stop_terminal()
        self.assertEqual(window.service_call.call_args.args[:2], ("stop", {"work_id": "step-9"}))

    def test_ended_generation_clears_the_previous_owner_and_reattaches(self):
        window = bare_window()
        window.current_token = "account-token"
        window.running_work = "step-1"
        window.session_ended("signed_out")
        window.clear_activity.assert_called_once()
        self.assertEqual(window.current_token, "")
        self.assertTrue(window.require_handoff)
        window.forget_web_session.assert_called_once()
        self.assertEqual(window.service_call.call_args.args[:2], ("attach", {}))

    def test_revocation_returns_to_the_welcome_screen(self):
        window = bare_window()
        window.current_token = "account-token"
        window.observe({"connection": "revoked", "work": None, "activity": []})
        self.assertEqual(window.current_token, "")
        self.assertTrue(window.require_handoff)
        window.stack.set_visible_child_name.assert_called_with("welcome")

    def test_a_service_for_another_host_is_reported(self):
        local = bare_window("http://127.0.0.1:8923")
        local.present = Mock()
        local.observe({"host": "https://apps.codeloves.me", "connection": "connected",
                       "work": {"approval_id": "a", "claim": "none"}, "activity": []})
        local.present.assert_not_called()
        self.assertIn("not this window", local.set_status.call_args.args[0])

    def test_activity_text_renders_service_entries(self):
        text = webview.activity_text([
            {"kind": "requested", "argv": ["ls", "a b"], "cwd": "/tmp"},
            {"kind": "running", "argv": ["ls", "a b"]},
            {"kind": "result", "argv": ["ls", "a b"], "state": "completed", "exit_code": 0, "stdout": "x\n", "stderr": ""},
            {"kind": "note", "text": "Mike was told."},
        ])
        self.assertIn("Requested: ls 'a b'\nDirectory: /tmp", text)
        self.assertIn("Result: completed (exit 0)\nstdout:\nx", text)
        self.assertIn("Mike was told.", text)


class MediaAndLegacyTest(unittest.TestCase):
    """Voice is the page's (Speak, voice chat, read aloud); the window only
    turns the page's microphone/camera request into a visible native choice."""

    def permission(self, answer):
        window = bare_window()
        window.window = Mock()
        dialog = Mock()
        dialog.run.return_value = answer
        request = Mock()
        original = webview.Gtk.MessageDialog
        webview.Gtk.MessageDialog = Mock(return_value=dialog)
        try:
            handled = window.permission_request(None, request)
            asked = webview.Gtk.MessageDialog.call_args.kwargs["text"]
        finally:
            webview.Gtk.MessageDialog = original
        self.assertTrue(handled)
        dialog.destroy.assert_called_once()
        return request, asked

    def test_microphone_permission_needs_a_native_yes(self):
        request, asked = self.permission(webview.Gtk.ResponseType.YES)
        request.allow.assert_called_once()
        request.deny.assert_not_called()
        self.assertIn("Allow Mike to use", asked)

    def test_closing_or_refusing_the_permission_denies_it(self):
        for answer in (webview.Gtk.ResponseType.NO, webview.Gtk.ResponseType.DELETE_EVENT):
            request, _ = self.permission(answer)
            request.deny.assert_called_once()
            request.allow.assert_not_called()

    def test_retired_text_mode_is_refused_explicitly(self):
        import os
        import subprocess
        launcher = Path(__file__).resolve().parents[1] / "bin/mike-desktop"
        done = subprocess.run([str(launcher)], env=dict(os.environ, MIKE_DESKTOP_TEXT_UI="1"),
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(done.returncode, 2)
        self.assertIn("text-mode window (MIKE_DESKTOP_TEXT_UI) was retired", done.stderr)
        self.assertEqual(done.stdout, "")


if __name__ == "__main__":
    unittest.main()
