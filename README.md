# Mike

Mike is a native Linux interface to your Euglena account. His conversation,
planning, and memory stay on the server. The desktop app signs you in, shows
recent conversation, follows turns started on other devices, and lets you
approve local actions. You can use Mike here without opening the website.

The source project and release asset are named `mike-desktop`; the installed
application and window are named **Mike**.

## Install

On Linux x86_64, install [CDLVSM](https://github.com/codelovesme/cdlvsm), then:

```sh
cdlvsm install mike
cdlvsm mike
```

The install includes the needed Code runtime and a **Mike** entry in the
applications menu. Sign in with your existing Euglena account in the window.
Mike connects to `https://apps.codeloves.me` over TLS; you do not need a
checkout, server address, or SSH tunnel. `cdlvsm upgrade mike` and
`cdlvsm uninstall mike` manage the installation. `mike-desktop` remains a
legacy package name for existing installations.

## Use

Type a question and press Enter. Mike can use the server applications your
account is allowed to use. Type `/help` for the desktop commands, `/apps`
to see the applications available to your account, `/history`
to refresh the most recent conversation, `/speak` to record a question, or
`/read` to play Mike's latest reply. Connection state appears at the top.

Local file access is off until you choose a folder. Use `/folder` to choose
one in advance, or wait until you approve a file task and Mike asks for it.
Enter filename words; Mike searches only that folder (up to six levels),
shows the exact match, and waits for your second confirmation before opening
it. Escape or N declines. Mike's model cannot supply a path or shell command
for the desktop to run.

The pairing survives restart in `~/.local/state/mike-desktop`, with a private
directory and credential files. The sign-in token stays in memory. Other
paired desktops have their own task inboxes, and the website can choose the
default target. The desktop currently needs the app open to receive tasks.

## Develop and verify

```sh
euglena install
euglena build
euglena test
euglena format --check
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

Linux x86_64 is the only packaged desktop platform today. Mobile apps,
account creation in the desktop app, and a richer visual conversation are
future work.
