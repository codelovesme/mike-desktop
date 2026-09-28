# Mike WebView desktop

## Acceptance criteria

- [x] The Mike window has no address bar or app hub and opens the hosted conversation (Xvfb screenshots and `Mike desktop route` browser case).
- [x] Browser email sign-in returns to Mike; sign-out clears the session (`Mike desktop route` browser case).
- [x] A system-browser connection sends its session by one-time loopback POST, with no token in the URL (`Mike browser connection` browser case and GTK smoke test).
- [x] The WebView restricts navigation and exposes no page-to-OS command bridge (`test_webview.py`, `test_bridge_runtime.py`).
- [x] The native Euglena companion pairs under the signed-in account and reports connection and revoked states (GTK smoke and live bridge test).
- [x] A real Mike file task waits for Mike approval, receives a user-chosen search and exact-file decision, then opens the file (live bridge test with recorder).
- [x] Malformed bridge requests and out-of-scope resolved files cannot reach the opener (`test_bridge_runtime.py`, native file fixture, live bridge test).
- [x] All Mike browser cases and native fixtures remain green (8 existing Mike cases, 2 new browser cases, 4 native fixtures, 3 Python tests).
- [x] The extracted bundle launches without a source checkout and connects to the public host (release-bundle GTK smoke and screenshot).
- [ ] Exercise the actual browser connection and native folder/confirmation dialogs together on a clean supported desktop image.
- [ ] Verify Google sign-in with a configured provider and record account-switch and offline screenshots.
- [ ] Close the file replacement race between scope recheck and `xdg-open` before general release.
- [ ] Publish the release, verify `cdlvsm install mike` on clean Debian and Ubuntu desktops, commit, and push.

See `docs/plans/003-mike-desktop-webview.md` for the full product and security design.
