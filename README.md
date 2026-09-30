# Mike

Mike is a Linux desktop interface to your Euglena account. His conversation,
planning, and memory stay on the server. A focused WebKit window shows the
same Mike conversation as the website. A native Euglena companion pairs this
computer and handles local tasks. The web page has no direct OS bridge.

The source project and release asset are named `mike-desktop`; the installed
application and window are named **Mike**.

## Install

On Linux x86_64, install [CDLVSM](https://github.com/codelovesme/cdlvsm), then:

```sh
cdlvsm install mike
cdlvsm mike
```

The supervised terminal workflow ships in `v0.4.0`. To return to the earlier
guided-file release, run
`CDLVSM_MIKE_DESKTOP_VERSION=v0.3.0 cdlvsm upgrade mike`.

The install includes the needed Code runtime and a **Mike** entry in the
applications menu. The desktop also needs the distribution packages
`python3-gi`, `gir1.2-webkit2-4.1`, `xdg-desktop-portal`, and
`xdg-desktop-portal-gtk`. Sign in with your existing Euglena account in the
system browser when the window asks you to
connect. Mike transfers that approved session through a one-time local
callback, then pairs the native companion. Its pairing credential stays in
private local storage.
Mike connects to `https://apps.codeloves.me` over TLS; you do not need a
checkout, server address, or SSH tunnel. `cdlvsm upgrade mike` and
`cdlvsm uninstall mike` manage the installation. `mike-desktop` remains a
legacy package name for existing installations.

## Use

Type a question in the conversation and press Send. The same account's chat,
history, memory, and Mike approval are available in the window. Connection
state appears above the conversation. You can also begin a conversation on
the website and choose this computer as the target for a local task.

Mike can request a terminal command on this paired computer. The desktop
shows its executable, arguments, and working directory and asks you locally
before running it as your OS user. You can stop a running command. Mike sees
the bounded output and may propose another step. Terminal work currently
requires approval for each command; it does not request root access. The
desktop window's **Sign out** button ends local pairing and stops a running
command.
The **Local activity** panel shows the reviewed command, its working
directory, exit status, and bounded output. It clears when you sign out,
switch accounts, or revoke the device.

The guided file action remains available. Local file access is off until a
task arrives. After Mike's action approval, the desktop asks you to choose a
folder with the OS picker and enter filename
words. It searches only that folder (up to six levels), shows the exact match,
and waits for a native Yes/No before opening it. The desktop then opens the
verified file descriptor through the document portal; a swapped symlink or
missing portal causes the task to fail closed. Mike's model cannot supply a
path for this guided file action. Terminal commands use a separate local
review and bounded worker.

The pairing survives restart in `~/.local/state/mike-desktop`, with a private
directory and credential files. The native sign-in token stays in memory. Other
paired desktops have their own task inboxes, and the website can choose the
default target. The desktop currently needs the app open to receive tasks.

Google sign-in uses the same external browser connection route when the
identity provider is configured.

## Develop and verify

```sh
euglena install
euglena build
euglena test
euglena format --check
/usr/bin/python3 -m unittest discover -s tests -p 'test_*.py'
MIKE_TEST_PORTAL=1 dbus-run-session -- /usr/bin/python3 -m unittest discover -s tests -p test_portal_ipc.py
```

The app uses pinned native modules and direct HTTPS requests to the public
host-web API. For local development, set
`MIKE_DESKTOP_HOST_URL=http://127.0.0.1:8923` to use a local host-web server.
The launcher accepts that loopback override and the public service only.

With a disposable verified account, run the full workflow against the public
service:

```sh
EUGLENA_HOST_URL=https://apps.codeloves.me \
  E2E_EMAIL=... E2E_PASSWORD=... python3 tools/live-e2e.py
```

The runner drives a real Mike turn, approves it in a headless native window,
chooses a temporary search folder, approves the exact match, and checks the
file opener and saved reply. It revokes its temporary pairing. Set
`E2E_DESKTOP_DIR` to an extracted bundle and `CODE_BIN` to Code to run the
same check against a release.

`tools/bridge-e2e.py` drives the corresponding session and local task bridge
against a real host. It uses a disposable account and replaces only the OS
file opener with a recorder.

`tools/terminal-e2e.py` exercises Mike's real terminal proposal, the paired
gateway, a simulated local approval, the installed worker, and Mike's final
reply. Set `MIKE_E2E_MULTI=1` to require `pwd`, `ls`, and `ps` in one turn.
`tools/terminal-ui-smoke.py` checks the native GTK approval, decline, and Stop
flows, sign-out, account switch, revocation, and close with a disposable
account under Xvfb. `tools/terminal-desktop-turn-smoke.py` starts a turn inside
the real Mike WebView and checks its visible final reply and local activity.

Linux x86_64 is the only packaged desktop platform today. Mobile apps are
future work.
