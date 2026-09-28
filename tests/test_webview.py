import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("desktop_webview", Path(__file__).resolve().parents[1] / "ui/desktop_webview.py")
webview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(webview)


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


if __name__ == "__main__":
    unittest.main()
