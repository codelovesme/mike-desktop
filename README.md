# Mike

Mike is a Linux desktop interface to your Euglena account. His conversation,
planning, and memory stay on the server. A focused WebKit window shows the
same Mike conversation as the website. The **Mike service** — a native
Euglena application in [`service/`](service) — pairs this computer, receives
its local tasks, runs them after your approval and reports the results. The
window is one client of that service; future terminal and IDE clients attach
to the same instance. The web page has no access to the service or the OS.

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

Upgrading from 0.4 keeps this computer's existing pairing: the service reuses
the one in `~/.local/state/mike-desktop` for the same account (and leaves those
files in place for rollback). The pairing survives restart in
`~/.local/state/mike-service`, with a private
directory and credential files; only the service reads them. The account
session token stays in the service's memory. Other paired computers have
their own task inboxes, and the website can choose the default target.

## The shared Mike service

One service runs per OS user and keeps this computer connected whether or
not a window is open. Closing the window only detaches it: presence and task
receipt continue, a running command finishes within its limits, and a new
command that needs your approval waits — it never runs because no window is
there — until you open Mike again (it is reported as not run if nobody
approves it within about four and a half minutes). **Sign out** signs this
computer out for every client; revoking the device on the website stops its
work and forgets the pairing.

```sh
mike-service install-unit    # a systemd user unit, enabled for your user manager
mike-service start | stop | status
```

Without the unit the window starts the service itself, and it keeps running
after the window closes until `mike-service stop`. Whether a user service
runs before login or after logout depends on lingering
(`loginctl enable-linger`); `mike-service status` shows it. The service needs
no display and never asks for root. Its private local protocol, the meaning
of detach, sign-out, account change and revocation, and the systemd details
are in [docs/local-service-protocol.md](docs/local-service-protocol.md).

### On a server without a display

The same service runs on a headless Linux server (no Desktop, WebKit,
display or inbound port; it only connects out). Enroll it with a code you
confirm in Mike on any signed-in device, then grant the work it may do
unattended:

```sh
mike-service install-unit && mike-service start
mike-service enroll --account you@example.com --name build-server
#   … enter the printed code in Mike → Your devices → Connect a server
mike-service grant add --root ~/src/app --run 'git status' --run 'python3 -m unittest' \
    --ask 'git commit' --hours 8
mike-service grant list | grant revoke ID | unenroll
```

Enrolling grants nothing; a server runs only steps a grant covers, checked
on the server itself. `--ask` commands wait for your yes in the Mike
conversation (a push to your own ntfy topic tells you, if you set one).
**A grant is not a sandbox**: granted commands run as this OS user with all
of its rights, and the directory is only where they start. See
[Headless servers](docs/local-service-protocol.md#headless-servers-enrollment-and-grants).

Google sign-in uses the same external browser connection route when the
identity provider is configured.

## Retired in 0.5

The undocumented text-mode window (`MIKE_DESKTOP_TEXT_UI=1`) was the old
companion process drawing its own terminal UI: email/password sign-in,
typed questions, `/apps`, `/history`, `/speak`/`/read` through
`arecord`/`aplay`, and Y/N keys for Mike's and the file task's approvals.
The window has been the published interface since 0.3 (whose README moved
`/speak` and friends to it), and that companion is now the shared service,
which has no UI of its own by design. So the text mode is gone, and the
launcher says so and exits (status 2) when the variable is set instead of
quietly opening something else. Nothing it offered is lost from the
published app: the window has chat, history, apps, Speak, voice chat and
read aloud (microphone and camera only after a native permission dialog),
Mike's action approval in the page, and the native search, folder and
exact-file dialogs. A terminal client of the service is separate work.

## Develop and verify

The service's modules are pinned to Code 2.12.1 releases (module ABI 1):
its launcher accepts only Code ≥ 2.12.1 and < 2.15.0 (`MIKE_SERVICE_CODE_BIN`
or CDLVSM's installed Code) and refuses anything else. Develop with 2.14.x.
`tools/package.py --service-only <version> <out>` stages the service as its
own package, with no Desktop UI; `tools/package.py <version> <out>` stages the
Desktop window, which declares the service as a CDLVSM dependency instead of
carrying it ([docs/service-package.md](docs/service-package.md)).

```sh
cd service
euglena install
sh tests/support/build.sh        # the fixtures' mock modules
euglena test                     # also generates service/main.code
euglena format --check
cd ..
/usr/bin/python3 -m unittest discover -s tests -p 'test_*.py'
MIKE_TEST_PORTAL=1 dbus-run-session -- /usr/bin/python3 -m unittest discover -s tests -p test_portal_ipc.py
```

`CODE_BIN` names the interpreter for the Python tests and tools. For local
development set `MIKE_DESKTOP_HOST_URL=http://127.0.0.1:8923` (the window)
and `MIKE_SERVICE_HOST_URL` (the service); each accepts that loopback
override and the public service only. `MIKE_SERVICE_STATE_DIR` and
`MIKE_SERVICE_RUNTIME_DIR` give a scratch service its own directories.

With disposable verified accounts, run the integration scenarios against a
real host — Mike's real terminal turn, the window being absent, two clients,
stop and decline, the guided file task, sign-out, revocation, an account
switch with a running command, and a crash/restart:

```sh
EUGLENA_HOST_URL=http://127.0.0.1:8923 E2E_EMAIL=... E2E_PASSWORD=... \
  E2E_SECOND_EMAIL=... E2E_SECOND_PASSWORD=... python3 tools/service-e2e.py
```

Headless servers (ticket 160) have their own run against a disposable
stack (`my-euglena-apps/tools/mike-scratch-stack.py up`, which builds its
own auth/Mike/gateway with their own keys and databases):
`python3 tools/headless-e2e.py` — enrollment, grants, a real Mike web turn
in Chromium, the question/notification/answer path with the page closed,
scope and owner refusals, bounds, network loss, crash, duplicate delivery,
offline targets, expiry, revocation and output hygiene.

Every boundary is real except the document portal's final call, which
`MIKE_OPEN_LOG` replaces with a recorder; every device the run pairs is
revoked. `tools/service-systemd-check.py <bundle>` installs a uniquely named,
unenabled user unit for a staged service package (`tools/package.py
--service-only`), checks
start/status, duplicate start, crash restart, stop and failure reporting,
and removes it. `tools/terminal-ui-smoke.py` checks the native GTK approval,
decline, Stop, sign-out, account switch, revocation and window close with a
disposable account under Xvfb; `tools/terminal-desktop-turn-smoke.py` starts
a turn inside the real Mike WebView and checks its visible final reply and
local activity. `tools/desktop-file-turn-smoke.py` asks for a file in the
window, confirms Mike's action in the page, answers the native search,
folder, filename and exact-file dialogs, checks the opened file and Mike's
reply, the Speak/voice chat/read-aloud controls, and that the page's
microphone request becomes a native permission dialog.

Linux x86_64 is the only packaged desktop platform today. Mobile apps are
future work.

## Terminal client

`bin/mike-cli` (or the independent `mike-cli` package) uses the same local
service as Desktop. See [docs/cli.md](docs/cli.md) for sign-in, project context,
resuming a central turn, explicit decisions, non-TTY and stop/detach behavior.
`mike` and `mike-desktop` keep their existing Desktop launch behavior.
Stage without publishing: `python3 tools/package.py --cli-only v0.5.0 /tmp/mike-packages`.
