import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location("desktop_webview", Path(__file__).resolve().parents[1] / "ui/desktop_webview.py")
webview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(webview)


class DesktopAppIdTest(unittest.TestCase):
    def test_matches_the_menu_entry_cdlvsm_writes(self):
        packages = Path("/prefix/share/cdlvsm/packages")
        self.assertEqual(webview.desktop_app_id(packages / "mike/v0.4.1/ui/desktop_webview.py"), "codelovesme-mike")
        self.assertEqual(webview.desktop_app_id(packages / "mike-desktop/v0.4.1/ui/desktop_webview.py"), "codelovesme-mike-desktop")
        self.assertEqual(webview.desktop_app_id(Path("/src/mike-desktop/ui/desktop_webview.py")), "codelovesme-mike")


class NavigationTest(unittest.TestCase):
    ORIGIN = "https://apps.codeloves.me"

    def test_bounded_terminal_output_labels_truncation(self):
        text = "é" * 3000
        bounded = webview.bounded_output(text)
        self.assertLessEqual(len(bounded.encode()), 4096)
        self.assertTrue(bounded.endswith("[output truncated]"))
        self.assertEqual(webview.bounded_output("short"), "short")

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

    def test_old_webview_session_cannot_replace_browser_handoff(self):
        window = webview.MikeWindow.__new__(webview.MikeWindow)
        window.origin = self.ORIGIN
        window.inserting_token = True
        window.handoff_loaded = False
        window.handoff_marker = "pending"
        window.current_token = "new-account-token"
        window.connecting_token = ""
        window.async_bridge = Mock()
        view = Mock()
        view.get_uri.return_value = self.ORIGIN + "/desktop/mike"
        view.run_javascript_finish.return_value.get_js_value.return_value.to_string.return_value = "old-account-token"
        window.got_session(view, None)
        self.assertTrue(window.inserting_token)
        window.async_bridge.assert_not_called()
        window.handoff_loaded = True
        window.session_generation = 1
        view.get_uri.return_value = self.ORIGIN + "/desktop/mike?handoff=pending"
        view.run_javascript_finish.return_value.get_js_value.return_value.to_string.return_value = "new-account-token"
        window.got_session(view, None)
        self.assertFalse(window.inserting_token)
        self.assertEqual(window.session_generation, 2)

    def test_pending_session_read_is_discarded_after_handoff(self):
        window = webview.MikeWindow.__new__(webview.MikeWindow)
        window.origin = self.ORIGIN
        window.session_generation = 0
        window.signing_out = False
        window.web = Mock()
        window.web.get_uri.return_value = self.ORIGIN + "/desktop/mike"
        window.got_session = Mock()
        window.read_session()
        callback = window.web.run_javascript.call_args.args[2]
        window.session_generation = 1
        callback(window.web, object())
        window.got_session.assert_not_called()

    def test_old_connection_callback_cannot_switch_new_account(self):
        window = webview.MikeWindow.__new__(webview.MikeWindow)
        window.origin = self.ORIGIN
        window.session_generation = 0
        window.inserting_token = False
        window.require_handoff = False
        window.current_token = ""
        window.connecting_token = ""
        window.closed = False
        window.async_bridge = Mock()
        window.set_status = Mock()
        window.stop_terminal = Mock()
        view = Mock()
        view.get_uri.return_value = self.ORIGIN + "/desktop/mike"
        view.run_javascript_finish.return_value.get_js_value.return_value.to_string.return_value = "old-account-token"
        window.got_session(view, None)
        old_callback = window.async_bridge.call_args.args[2]
        window.session_generation += 1
        window.connecting_token = ""
        old_callback(200, "connected")
        self.assertEqual(window.current_token, "")

    def test_saved_profile_cannot_switch_approved_native_owner(self):
        window = webview.MikeWindow.__new__(webview.MikeWindow)
        window.origin = self.ORIGIN
        window.session_generation = 2
        window.inserting_token = False
        window.require_handoff = False
        window.current_token = "approved-account-token"
        window.connecting_token = ""
        window.install_web_token = Mock()
        window.async_bridge = Mock()
        window.set_status = Mock()
        view = Mock()
        view.get_uri.return_value = self.ORIGIN + "/desktop/mike"
        view.run_javascript_finish.return_value.get_js_value.return_value.to_string.return_value = "stale-account-token"
        window.got_session(view, None)
        window.install_web_token.assert_called_once_with("approved-account-token")
        window.async_bridge.assert_not_called()

    def test_native_sign_out_stops_and_disconnects_before_page_reload(self):
        window = webview.MikeWindow.__new__(webview.MikeWindow)
        window.session_generation = 3
        window.current_token = "account-token"
        window.connecting_token = ""
        window.terminal_worker = None
        window.connect_state = "pending"
        window.inserting_token = False
        window.handoff_marker = "marker"
        window.stop_terminal = Mock()
        window.clear_activity = Mock()
        window.sign_out_button = Mock()
        window.stack = Mock()
        window.set_status = Mock()
        window.async_bridge = Mock()
        window.web = Mock()
        window.sign_out()
        window.stop_terminal.assert_called_once()
        self.assertEqual(window.current_token, "")
        self.assertEqual(window.session_generation, 4)
        self.assertTrue(window.signing_out)
        self.assertTrue(window.require_handoff)
        self.assertEqual(window.async_bridge.call_args.args[:2], ("/disconnect", {}))
        script = window.web.run_javascript.call_args.args[0]
        self.assertIn("localStorage.removeItem('id:token')", script)
        self.assertIn("location.replace('/desktop/mike')", script)

    def test_sign_out_waits_for_running_step_result(self):
        window = webview.MikeWindow.__new__(webview.MikeWindow)
        window.session_generation = 0
        window.current_token = "account-token"
        window.connecting_token = ""
        window.connect_state = ""
        window.inserting_token = False
        window.handoff_marker = ""
        window.terminal_worker = Mock()
        window.stop_terminal = Mock()
        window.clear_activity = Mock()
        window.sign_out_button = Mock()
        window.stack = Mock()
        window.set_status = Mock()
        window.async_bridge = Mock()
        window.web = Mock()
        window.sign_out()
        self.assertTrue(window.pending_disconnect)
        window.async_bridge.assert_not_called()

    def test_saved_token_cannot_reconnect_after_native_sign_out(self):
        window = webview.MikeWindow.__new__(webview.MikeWindow)
        window.origin = self.ORIGIN
        window.inserting_token = False
        window.require_handoff = True
        window.current_token = ""
        window.connecting_token = ""
        window.web = Mock()
        window.async_bridge = Mock()
        view = Mock()
        view.get_uri.return_value = self.ORIGIN + "/desktop/mike"
        view.run_javascript_finish.return_value.get_js_value.return_value.to_string.return_value = "stale-token"
        window.got_session(view, None)
        self.assertIn("removeItem('id:token')", window.web.run_javascript.call_args.args[0])
        window.async_bridge.assert_not_called()

    def test_web_sign_out_clears_local_activity(self):
        window = webview.MikeWindow.__new__(webview.MikeWindow)
        window.origin = self.ORIGIN
        window.session_generation = 0
        window.inserting_token = False
        window.require_handoff = False
        window.current_token = "account-token"
        window.connecting_token = ""
        window.terminal_worker = None
        window.stop_terminal = Mock()
        window.clear_activity = Mock()
        window.sign_out_button = Mock()
        window.async_bridge = Mock()
        window.stack = Mock()
        window.set_status = Mock()
        view = Mock()
        view.get_uri.return_value = self.ORIGIN + "/desktop/mike"
        view.run_javascript_finish.return_value.get_js_value.return_value.to_string.return_value = ""
        window.got_session(view, None)
        window.stop_terminal.assert_called_once()
        window.clear_activity.assert_called_once()
        self.assertEqual(window.current_token, "")
        self.assertTrue(window.require_handoff)

    def test_account_handoff_waits_for_running_step_result(self):
        window = webview.MikeWindow.__new__(webview.MikeWindow)
        window.closed = False
        window.port = 1234
        window.pending_disconnect = False
        window.current_token = "old-token"
        window.terminal_worker = Mock()
        window.stop_terminal = Mock()
        window.set_status = Mock()
        window.async_bridge = Mock()
        window.accept_handoff("new-token")
        self.assertEqual(window.deferred_handoff_token, "new-token")
        window.stop_terminal.assert_called_once()
        window.async_bridge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
