# 002 — Mike as a primary desktop interface

## Acceptance criteria

- [x] A new Linux user installs and starts the app with `cdlvsm install mike` and `cdlvsm mike`; the menu and window say **Mike**. Existing `mike-desktop` installs and upgrades still work.
- [x] Installation includes a secure direct connection to the public Euglena service. A user with an account can sign in and talk to Mike without a source checkout, URL editing, or SSH access.
- [x] After sign-in, conversation works before choosing a local file folder. The folder is requested only for a file task or when the user chooses to enable it.
- [x] The desktop shows recent server-held conversation, current thinking and approval, connected or offline state, and a clear place to enter a question. A new user can discover what Mike can do without returning to the website.
- [x] File search remains limited to a user-selected root and still shows the exact file and asks before opening it. A declined task opens nothing.
- [x] Native fixtures, the published bundle smoke test, and a live Mike turn through the installed client pass. The public Mike and CDLVSM releases are installed and verified from an isolated prefix.

## Verification

Native: `euglena build`, `euglena test` (4 passed), and `euglena format --check` passed. The v0.2.1 GitHub release built on a clean runner, passed the extracted window smoke test, and published the bundle. A disposable verified account completed Mike → public HTTPS → native installed client → two approvals → exact local file open; the runner checked the saved reply and revoked the device. CDLVSM v0.10.0 published successfully. Its network integration test installed from the public Mike release, upgraded Code 2.10 to 2.12.1, checked the Mike menu entry and CLI dispatch, upgraded, and uninstalled. The released CDLVSM binary also installed Mike from an isolated prefix; that installed bundle completed the same public HTTPS workflow. The isolated install and launcher were removed afterward.
