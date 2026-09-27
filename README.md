# Mike Desktop

Mike Desktop is a native Linux Euglena window. Mike's conversation, planning,
and memory stay in the server's mike-api. This app signs in as the person,
pairs with mike-interface-api, sends typed questions to Mike, follows turns
started in the browser, and collects local tasks addressed to this device.

## Install from the public release

On Linux x86_64, with [CDLVSM](https://github.com/codelovesme/cdlvsm) installed:

    cdlvsm install mike-desktop
    cdlvsm mike-desktop

The install also brings in the Code interpreter and adds **Mike Desktop** to
the desktop applications menu. `cdlvsm upgrade mike-desktop` and
`cdlvsm uninstall mike-desktop` manage later releases. The bundle has its
own pinned Euglena modules and needs no source checkout or `euglena install`.
It contains no account credential; sign in inside the window.

The launcher connects to `http://127.0.0.1:8899` by default. For a host on
another machine, start an authenticated SSH tunnel to the host's loopback
port, then launch with:

    ssh -N -L 127.0.0.1:18899:127.0.0.1:8899 user@euglena-server

In another terminal:

    MIKE_DESKTOP_HOST_URL=http://127.0.0.1:18899 cdlvsm mike-desktop

The launcher rejects a non-loopback URL.

The first local task is finding and opening a file. On each launch, the person
chooses an absolute search folder. No folder is enabled by default. For a
task, the person enters filename words; the app searches that folder (up to
six levels), displays the exact first match, and waits for Y before asking
xdg-open to open it. N declines. Mike also asks permission before he queues
the task. The model's goal is shown to the person but is never used as a
command, filename query, root, or path to open.

## Run

On the Linux desktop:

    cd mike-desktop
    cp .env.example .env
    # Point the three URLs at the host's reachable address.
    euglena install
    euglena build
    euglena run

The default .env.example uses the local Euglena host on port 8899. The
native net_client in Code 2.11 speaks HTTP only. On another computer, use
an authenticated encrypted tunnel with local forwarding to the host and
keep the URLs at 127.0.0.1; direct plain HTTP across a network is not a
supported deployment. Desktop sign-in and device credential both travel
through these connections.

For a remote Linux desktop, start tools/remote-tunnel.sh with the SSH login
for the server. When running from source, set AUTH_URL, MIKE_URL, and
MIKE_INTERFACE_URL in .env to http://127.0.0.1:18899/auth, /mike, and
/mike-interface respectively. For an installed release, set
MIKE_DESKTOP_HOST_URL=http://127.0.0.1:18899 instead.
The SSH session must stay open while the desktop app is connected. The
client's offline status appears when it stops.

Enter the email, password, and search folder in the window. Enter a question
to Mike in the same input line. Type /folder to change the search folder.
Type /speak to record eight seconds from the system microphone and send the
transcribed words to Mike. Type /read to play Mike's latest reply. These use
arecord, base64, openssl, and aplay directly; an unavailable microphone or
speaker is shown as a status in the window.
The title line shows connected or offline state. While the app is open it
heartbeats and checks its own task inbox every 2.5 seconds. Another connected
desktop has its own inbox. The browser at /mike lists paired desktops and
lets the person choose the default target.

The pairing survives restart in ~/.local/state/mike-desktop, with a 0700
directory and 0600 identity files. A sign-in token is held in memory only.
If the saved pairing was revoked, the app pairs anew. If secure local storage
cannot be prepared, it still works for that run and says that the pairing
will not survive restart.

## Verify

    euglena test
    euglena build
    euglena format --check

The native fixtures drive a headless window through the declined and
approved paths, and inspect each process call. The file fixture checks that
shell-looking text remains a single filename argument. The gateway's fixture
checks owner and device isolation, duplicate claims, completion, and
revocation. With a running host and a disposable signed-in account, run the
full live workflow:

    E2E_EMAIL=... E2E_PASSWORD=... python3 tools/live-e2e.py

It starts a real Mike turn, approves Mike's request in a headless native
window, claims the desktop task, enters a filename, approves the exact match,
and verifies the single file opener call and Mike's saved reply. The file
opener is a private test executable; the host, gateway, and desktop code are
real. The runner revokes its temporary pairing after the test.

The Linux interface supports typed and spoken input and reading replies.
iOS, Android, and a packaged encrypted remote connection remain follow-on
work. The browser already has microphone and voice chat support.

The server's two new held apps include root-level mongodb.so symlinks into
their lockfile-pinned 2.11 modules. Without them the Code compiler selects
an older globally installed flat module, and Mike's conversation indexes
silently fail at runtime. Run euglena install before rebuilding those apps.
