# Mike's shared local service and its client protocol (version 1)

`service/` is a native Euglena application (`mike-service`: manifest, genes,
pinned modules). One instance per OS user holds this computer's paired
device. It keeps the pairing credential, sends presence, claims the
device's tasks from Mike Interface, runs a local tool only after a client
relays the person's approval, and reports a bounded result. Central Mike
keeps the conversation, reasoning and memory; the service is not a memory
store, a model host, or a workflow engine.

Desktop (`ui/desktop_webview.py`) is a client. It keeps the first-party web
conversation in its WebView, shows the service's activity, and presents
approvals in native dialogs (including the OS folder picker). Future CLI
and IDE clients attach to the same instance through the same protocol.
`client/mike_service.py` is the reference Python client.

## Transport and discovery

The service listens on **127.0.0.1, an ephemeral port**. No public or LAN
listener exists. On every start it writes, in its runtime directory:

| File | Mode | Content |
|---|---|---|
| `endpoint` | 0600 | `{"protocol":1,"port":N,"pid":"…","service":"…"}` |
| `client.key` | 0600 | 64 hex characters, new on every start |
| `instance.lock` | — | held with `flock` while the instance lives |

The runtime directory is `$MIKE_SERVICE_RUNTIME_DIR`, else
`$XDG_RUNTIME_DIR/mike-service`, else `<state>/run`. State (the pairing and
an in-flight marker) is in `$MIKE_SERVICE_STATE_DIR`, else
`~/.local/state/mike-service`. Both must be real directories owned by this
user with mode 0700. An existing path is inspected before anything touches
it and is never repaired: a symlink, another user's directory or a
group/world-accessible one is refused unchanged. A missing directory is
created 0700. Clients apply the same rule to the state directory before
opening `service.log` (never through a symlink) when they start a detached
service.

A client must check the files are private and owned by it, and that the
listener on the published port belongs to its own uid (`/proc/net/tcp`)
before sending anything: after a stop the port is free, and another user's
process could take it.

## Every request

```
X-Mike-Protocol: 1
Authorization: Bearer <client key | session token>
Host: 127.0.0.1:<port>
Content-Type: application/json        (POST bodies are JSON objects)
```

Checked in this order, before any route:

1. `Origin`, `Referer`, `Sec-Fetch-Site` or `Sec-Fetch-Mode` present → **403
   `browser_refused`**. Browsers attach these to the requests a page can
   make to localhost (including no-cors and preflights); first-party clients
   never send them.
2. `Host` other than `127.0.0.1:<port>` → **403 `host_refused`** (DNS
   rebinding).
3. `X-Mike-Protocol` other than `1` → **426 `protocol_mismatch`** with
   `supported` and `service`. A client that gets this must not retry
   blindly; it reports the version mismatch.
4. Missing or unknown bearer → **401 `unauthorized`**.

Errors are `{"error": "<code>", "message": "…", …}`.

## Sessions and generations

`POST /v1/attach` with the client key creates a session and returns its
token. Every other route takes the session token, so clients never share
request/response correlation. A session that has not been seen for 45 s is
detached. At most 16 sessions are open.

A **generation** is one owner's sign-in on this computer. Sign-out, an
account change and device revocation each end it: current work is stopped
and (where the device may still speak) reported, activity and decisions
are dropped, and every session of the old generation answers **409
`session_ended`** with `reason` (`signed_out`, `account_changed`,
`device_revoked`) to its next request. The client clears what it showed and
attaches again. No client can see the previous owner's results.

The account token and the device credential are never returned to a client.

## Routes

| Route | Body | Answer |
|---|---|---|
| `POST /v1/attach` (client key) | `client` (≤40 chars), `approvals` (bool), `connect`: `none`\|`auto`\|`switch`, `token` (account session) | `session_id`, `session_token`, `generation`, `state` |
| `GET /v1/state` | — | the state below |
| `POST /v1/detach` | `{}` | `{detached:true}` |
| `POST /v1/approvals/claim` | `approval_id` | `{claimed, id, stage}` |
| `POST /v1/approvals/answer` | `approval_id`, `decision`: `approve`\|`decline`, file search: `folder`, `query` | `{stage}` |
| `POST /v1/work/stop` | `work_id` | `{stopping:true}` |
| `POST /v1/signout` | `{}` | `{signed_out:true}` |
| `POST /v1/device/revoke` | `{}` | `{revoked, confirmed_by_gateway}` |
| `POST /v1/chat/ask` | `text` | `{turn_id}` |
| `POST /v1/chat/progress` | `turn_id` | `{stage, reply, say}` |
| `POST /v1/chat/confirm` | `turn_id`, `yes` | `{sent:true}` |

`connect`:

- `none` — observe whatever this computer is signed in as (or not).
- `auto` — sign in with `token` if the service has no account; refresh the
  token if it is the same account. A different account → **409
  `account_mismatch`**; after a revocation → **409 `connect_required`**.
- `switch` — the person explicitly connected this computer (Desktop's
  browser handoff): change accounts, or pair again after revocation.

Signing in reuses this account's saved pairing; it pairs a new device only
when there is none or the gateway refuses it, never because the gateway is
merely unreachable.

State:

```
protocol, service, host, generation, session, approvals,
account, connection (signed_out | connected | offline | session_expired |
  gateway_unavailable | revoked), paired, capabilities,
clients, approval_clients,
work: null | { id, kind (terminal | file), stage, claim (none | you | other),
               approval_id, argv, cwd, goal, folder, file, seconds_left },
activity: [{ at, kind (requested | running | found | result | note), id,
             text, argv, cwd, state, exit_code, stdout, stderr }]
```

`approval_id` is shown only to sessions attached with `approvals: true`,
and only while the work awaits a decision. `/chat/*` are a thin pass-through
to central Mike with the account's session, for clients that do not embed the
web conversation; a turn is visible only to the session that started it.

## Work and approvals

One task at a time, exactly as the gateway delivered it:

```
terminal: awaiting_approval → running → reporting → (done)
file:     awaiting_approval → file_confirm → opening → reporting → (done)
```

- Only an approval-capable session can claim or answer. The first claim
  wins; others get **409 `claimed_elsewhere`** for claim and answer.
  Answering an unclaimed approval claims it.
- Each decision is recorded. Repeating an answer (any session) gets **409
  `already_decided`** with the recorded decision; nothing runs twice. A
  file task's exact-file confirmation is a new `approval_id`.
- Detaching (closing a window) or expiring releases the claim. The task
  neither runs nor is declined because a window closed; another client, or
  the same one reopened, can answer it.
- With no approval-capable client the task waits. 270 s after the claim
  (the gateway expires a task at 300 s) it is reported as not run
  (`cancelled`, or `declined` for a file task). Running the service never
  grants unattended execution.
- `stop` sends SIGTERM to the worker, which kills its command's process
  group; the result is what the worker observed (`cancelled` when stopped).
- Results are bounded as Mike Desktop always bounded them: at most 4096
  UTF-8 bytes per stream, the `\n[output truncated]` marker included, cut on
  a character boundary (the terminal worker's `report_bytes`). They are
  delivered once; a failed delivery is retried
  with the same result (the gateway answers `replayed`) until the step's
  expiry, never by repeating the effect.

The terminal worker (`service/adapters/terminal_worker.py`) bounds time
(60 s) and output (32 KiB), runs without privilege gain, and keeps only a
fixed environment. The file opener (`service/adapters/secure_open.py`)
re-checks the chosen folder and hands a verified descriptor to the document
portal. Both are OS adapters run by the service; neither decides approval.

## Upgrading from Mike Desktop 0.4

Mike Desktop up to 0.4 kept its pairing in `~/.local/state/mike-desktop`
(`owner`, `device_id`, `credential`). When the service holds no identity at
all and an owner connects, it may reuse that pairing — never pairing a
second device and inbox after an upgrade — only if:

- the legacy directory is this user's real 0700 directory and each file is
  this user's non-empty 0600 regular file (checked with `stat`, never
  followed or changed; `MIKE_SERVICE_LEGACY_STATE_DIR` overrides the path);
- `owner` is exactly the connecting, verified account;
- the device id and credential are single, bounded lines.

The gateway then decides: a refused credential is recorded and the computer
pairs anew; an accepted one, or an unreachable gateway (as with any saved
pairing), is copied into the service's store. The decision is recorded in
`legacy-migrated` and made once. The legacy files are left in place for
rollback to 0.4; nothing deletes them. A pairing in the service's store is
never overwritten by the legacy one, and another owner's legacy files are
never copied.

A revocation (website or `device/revoke`) is remembered in the state
directory (`revoked`) and retires the legacy pairing, so neither a restart,
an automatic sign-in, sign-out, nor the migration can bring the computer
back; only an explicit `connect=switch` pairs it again, as a new device.

## Lifecycle: what each event means

| Event | Running command | Pending approval | Pairing | Sessions |
|---|---|---|---|---|
| Window closed / `detach` | continues within its bounds; result reported | claim released; waits | kept | that session only |
| Client stops polling 45 s | as detach | as detach | kept | that session only |
| `signout` (any client; Desktop's Sign out or the web page signing out) | stopped, reported `cancelled` | reported not run | kept on disk, account token forgotten (a remembered revocation stays) | all end (`signed_out`) |
| `attach … connect=switch` to another account | stopped, reported to the old account | reported to the old account | other account's pairing used/created | all end (`account_changed`) |
| Revoked on the website (heartbeat refused) | stopped; gateway reports `unknown` | dropped; gateway reports revoked | deleted; revocation remembered across restarts | all end (`device_revoked`) |
| `device/revoke` from a client | stopped, reported, then revoked | reported, then revoked | revoked at the gateway, deleted; revocation remembered | all end (`device_revoked`) |
| Service stopped (`systemctl --user stop`) | killed with the unit's cgroup; reported `failed`/unknown after restart | reported not run after restart | kept | gone; account token gone |
| Service crash / kill | killed with the unit's cgroup (outside systemd the worker's own 60 s limit still applies); reported `failed`/unknown after restart | reported not run after restart | kept | gone |

The account token lives only in the service's memory. After a restart the
pairing is still on disk but the service is signed out until a client
attaches with `connect=auto` (Desktop does this from its web session). A
task that was claimed when the service stopped is then reported once:
`cancelled`/"did not run" if it had not been approved, `failed`/"whether
it finished is unknown" if its effect had begun. Nothing is replayed, and
no checkpoint or resumption exists.

## systemd user unit

```sh
mike-service install-unit      # ~/.config/systemd/user/mike-service.service, enabled
mike-service start | stop | restart | status
mike-service uninstall-unit
```

The unit runs `mike-service run` as the user (no root), with
`NoNewPrivileges=yes`, `UMask=0077`, an optional
`~/.config/mike-service/environment` (`MIKE_SERVICE_HOST_URL`,
`MIKE_SERVICE_CODE_BIN`, …), `Restart=on-failure` after a crash, and
`RestartPreventExitStatus=1` so a refused start (another instance, a
non-private directory, a bad setting) fails once and says why in the
journal. `status` reports the unit state, the service's own last notices,
the account/connection, and linger.

When the service runs depends on the user manager, not on the unit file:
without lingering it starts at login and stops when the user's last
session ends; with `loginctl enable-linger <user>` the user manager — and
an enabled unit — starts at boot and survives logout. Neither is implied by
installing the unit. The core needs no display; the file opener needs the
session bus and a running desktop portal, and fails closed without them.

Without the unit, Desktop starts the service detached (it outlives the
window, logging to `~/.local/state/mike-service/service.log`);
`mike-service stop` ends it.

## Compatibility

The version is the `X-Mike-Protocol` header and `endpoint.protocol`.
Within version 1, new routes and new fields may be added; clients ignore
unknown fields. Removing or changing the meaning of a route, field, stage
or error code requires version 2, and a service answers 426 to a version it
does not speak. The service's Code modules are pinned in
`service/.code/lock.json`; the Code interpreter must be able to load them
(see `MIKE_SERVICE_CODE_BIN`).

## Security notes

- The trust boundary is the OS user. Any process of this user that can
  read the runtime directory can attach — the same boundary as the user's
  own files, including the pairing in the state directory. An approved
  command runs as the user and could therefore read the client key; local
  approval of each command is what protects against that.
- Other OS users can connect to the loopback port but cannot read the key,
  cannot attach, and get no information beyond the error codes above.
- Web pages cannot use the service: their requests carry Origin or Fetch
  metadata and are refused before authentication; DNS rebinding fails the
  Host check; a preflight is refused, so a page cannot send custom headers.
- The key and session tokens are compared as strings, not in constant
  time. Exploiting that would need timing over loopback HTTP against 256-bit
  random values; it is noted, not mitigated.
