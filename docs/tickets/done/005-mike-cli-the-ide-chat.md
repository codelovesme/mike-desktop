# 005 — Mike CLI: the ide's AI chat, as a terminal program of its own

The owner (2026-10-31): the AI chat leaves the ide completely (ide ticket
009) and becomes its own app, Mike CLI — a command-line tool like Claude
Code or Codex. It runs in any terminal, and so in the ide's terminal tab,
the same way they do. Sign-in and everything else about the account live
in the CLI; nothing of it stays in the ide.

Where it starts from: `mike-cli` already exists (docs/cli.md,
client/cli.py over client/mike_service.py; `cdlvsm install mike-cli`,
requires mike-service). Two things are missing for it to stand alone:

1. **Its own sign-in.** Today it needs Desktop's browser handoff or a
   pasted session token. The CLI must sign in itself.
2. **The ide chat's way of working.** Today it is a plain line prompt. The
   ide chat had an input box with a caret and line wrap, ↑/↓ question
   history, a queue while a turn runs, progress while Mike works, and
   answers wrapped to the window.

Acceptance criteria:

- [x] `cdlvsm install mike-cli`, then `mike-cli` in a fresh terminal with
      nothing signed in: it offers to sign in, opens the browser (the
      ordinary account page, `/desktop/connect` handoff to a one-time
      loopback callback, as the ide's Account sign-in did in
      `ide/tools/mike_bridge.py`), and after Connect the chat is ready. On
      a machine with no browser it prints the URL to open elsewhere.
- [x] `mike-cli --sign-out` and `mike-cli --account` (who is signed in);
      the credential is kept as the service keeps it today, never printed.
- [x] The chat screen: input box with caret, wrap, ↑/↓ history, Enter
      sends, a message typed during a turn waits in a queue; progress and
      approvals (`/approve`, `/yes` … as today) stay.
- [x] It works inside the ide's terminal tab exactly as in a plain terminal
      (keys, resize, colours); checked in a real pty.
- [x] The project is the folder it starts in, as today.
- [x] Desktop, the service and today's `--ask` / non-TTY modes keep working;
      existing tests green; a pty smoke for sign-in-less start, chat, quit.
- [x] Released (patch/minor) and installed; the ide's README points to it
      for AI ("run `mike-cli`, `claude` or `codex` in a terminal tab").

Decided (2026-10-31, the owner left it to me): the command stays
`mike-cli` (`mike` keeps opening Desktop, so nothing installed changes
meaning); it talks with Mike only — no local model, which would bring back
the dependency 009 removed; voice input is dropped, not moved.

Done (2026-10-31): client/sign_in.py (the browser handoff, the service
started first, a different account refused unless --switch-account);
`--sign-in`, `--account`, `--switch-account`, `--sign-out`; an interactive
start offers the service and the sign-in, never unasked. The prompt is
readline in its own thread (caret, ↑/↓ history in
$XDG_STATE_HOME/mike-cli/history, 0600), Mike's output printed above the
typed line, a queue of ten with /unqueue. tests/test_cli_sign_in.py (12) +
tests/test_cli.py (17) green; tools/cli-pty-smoke.py 11/11 in a real pty;
`mike-cli --account` against the real service on this machine. "Released"
means v0.5.2 (patch, so it stays in the 0.5 service series Desktop 0.5.1
pins).
