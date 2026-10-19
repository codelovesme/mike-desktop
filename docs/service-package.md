# The Mike service package

The shared Mike service is installable on its own — on a desktop or a
headless server — without the Desktop window. `tools/package.py
--service-only <version> <out>` stages it from a checkout (after
`euglena test` in `service/` has generated `service/main.code`). Staging
publishes nothing; there is no release in this change.

## Contents

```
mike-service-<version>-x86_64-linux/
  mike-service              launcher: run | start | stop | restart | status |
                            install-unit | uninstall-unit | --version
  VERSION
  README.md
  service/                  the native Euglena application `mike-service`
    main.code, manifest.json, src/*.gene.code
    adapters/terminal_worker.py, adapters/secure_open.py
    env.so http_client.so http_server.so json.so process.so strings.so timer.so
                            (exactly the modules in service/.code/lock.json,
                             hash-checked when staged)
  client/mike_service.py    the protocol-1 client library (any local client)
  client/service_control.py lifecycle commands behind the launcher
  docs/local-service-protocol.md, docs/service-package.md
```

Nothing from `ui/`, no WebKit, GTK or display requirement. `mike-service
run` needs only Code and POSIX tools (`sh`, `flock`, `tail`, `stat`,
`install`, coreutils); the control commands and both adapters need Python 3
(the file opener additionally needs the session bus and a document portal,
and fails closed without them). No step needs root.

The Desktop bundle (`tools/package.py <version> <out>`) is an interim
artifact: it stages this same tree at its root beside `ui/` and the
`mike`/`mike-desktop` launchers, because no generic package dependency
exists yet.

## Launch contract

- `mike-service run` is the foreground service (what the user unit runs). It
  exits 1 with a `mike-service: not started: …` line when it refuses to
  start (another instance, a non-private directory, a bad setting, an
  incompatible Code), and otherwise runs until stopped.
- Code: the bundled modules are Code 2.12.1 releases (module ABI 1). The
  launcher uses `MIKE_SERVICE_CODE_BIN` if set, else CDLVSM's installed
  `../../code/current/code`, and in either case only if `--version` reports
  **≥ 2.12.1 and < 2.15.0**; otherwise it exits 1 naming the requirement.
  It never falls back to other interpreters. Verified: the staged package
  serves under Code 2.12.1, 2.13.0, 2.14.0, 2.14.1, 2.14.3, 2.14.4 and
  2.14.5; Code 2.15+ refuses the modules. (A source checkout resolves
  modules differently under 2.12.x; develop with 2.14.x.) Moving to Code
  2.15+ means repinning the service's modules and this range together.
- Settings, from the environment or the unit's environment file
  (`~/.config/mike-service/environment`): `MIKE_SERVICE_HOST_URL`
  (`https://apps.codeloves.me` or `http://127.0.0.1:<port>`),
  `MIKE_SERVICE_STATE_DIR`, `MIKE_SERVICE_RUNTIME_DIR`,
  `MIKE_SERVICE_LEGACY_STATE_DIR`, `MIKE_SERVICE_CODE_BIN`.
- State (`~/.local/state/mike-service`) belongs to the user, not the package
  version: upgrades and reinstalls keep the pairing. Uninstalling the package
  does not delete it.
- `install-unit` writes `~/.config/systemd/user/mike-service.service`
  pointing at this package's launcher; reinstalling to a new path must run it
  again. `status` exits 0 when the service answers, 3 when it does not.
- Clients find the running service by its runtime directory
  (`docs/local-service-protocol.md`), never by package path. To start it, a
  client uses the user unit if installed, otherwise a launcher named by
  `MIKE_SERVICE_LAUNCHER`, one beside it (the interim bundle), or
  `mike-service` on `PATH`.

## Handoff to ticket 163 (generic package dependencies)

What a generic resolver must provide for Desktop, CLI and IDE to depend on
this package instead of carrying it — no Mike-specific branches:

1. A package named for the service whose artifact is the tree above.
2. Desktop (and later CLI) declare a dependency on it; IDE's Mike support
   declares it where that feature is installed.
3. A runtime constraint the resolver can satisfy: Code ≥ 2.12.1, < 2.15.0
   for this package version, satisfied by installing a compatible Code next
   to the current one rather than replacing it (today CDLVSM's
   `code/current` is 2.16.0, which this package — and the installed Mike
   Desktop 0.4.1 — cannot use).
4. `mike-service` exposed on `PATH`, and a hook (or documented step) that
   runs `mike-service install-unit` for the user after install/upgrade and
   `uninstall-unit` on removal.
5. Reuse of an already-installed compatible service rather than a second
   copy, and upgrade ordering that keeps the protocol version a client
   needs (protocol 1 here; clients check `X-Mike-Protocol`).

Until then the interim Desktop bundle carries the service, and nothing in
this ticket changes CDLVSM.
