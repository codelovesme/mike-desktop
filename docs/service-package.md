# The Mike service package

The shared Mike service is installable on its own — on a desktop or a
headless server — without the Desktop window. `tools/package.py
--service-only <version> <out>` stages it from a checkout (after
`euglena test` in `service/` has generated `service/main.code`). Staging
publishes nothing. The v0.5.0 release workflow packages the service, CLI and
Desktop separately under one release.

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

The Desktop bundle (`tools/package.py <version> <out>`) does not carry this
tree: it is the window (`ui/`, the `mike`/`mike-desktop` launchers) and the
protocol client, and its `cdlvsm-package.info` declares the service as a
CDLVSM dependency (below), so Desktop, CLI and IDE on one computer share one
installed service.

## Launch contract

- `mike-service run` is the foreground service (what the user unit runs). It
  exits 1 with a `mike-service: not started: …` line when it refuses to
  start (another instance, a non-private directory, a bad setting, an
  incompatible Code), and otherwise runs until stopped.
- Code: the bundled modules are Code 2.12.1 releases (module ABI 1). The
  launcher uses `MIKE_SERVICE_CODE_BIN` if set, else the Code CDLVSM
  resolved for this package (`.cdlvsm-deps/code`), else CDLVSM's active
  `../../code/current/code`, and in each case only if `--version` reports
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
  again (CDLVSM does, after every install and upgrade). Its environment file
  is `--environment-file`, else `MIKE_SERVICE_ENVIRONMENT_FILE`, else
  `~/.config/mike-service/environment`. `status` exits 0 when the service answers, 3 when it does not.
- Clients find the running service by its runtime directory
  (`docs/local-service-protocol.md`), never by package path. To start it, a
  client uses the user unit if installed, otherwise a launcher named by
  `MIKE_SERVICE_LAUNCHER`, the service CDLVSM resolved for it
  (`.cdlvsm-deps/mike-service`), one beside it (a source checkout), or
  `mike-service` on `PATH`.

## CDLVSM package information

Both packages carry `cdlvsm-package.info` (format in CDLVSM's
`docs/package-dependencies.md`), written by `tools/package.py`:

```text
# mike-service
format=1
requires=code >=2.12.1 <2.15.0 pin=v2.14.5 sha256=<code-v2.14.5 archive> bind=exact
system=python3
service=mike-service

# mike (Desktop), version <v>
format=1
requires=mike-service >=<v> <<major>.<minor+1>.0 pin=<v>
system=webkitgtk document-portal
```

- The service's Code is an **exact** binding: with a newer Code active
  (2.16 today) CDLVSM installs 2.14.5 beside it, or reuses an installed
  2.12.1–2.14.x, and links it as `.cdlvsm-deps/code`. The active Code and
  what uses it are not touched. Moving to Code 2.15+ means repinning the
  modules, this requirement and the launcher's check together.
- `service=mike-service` is the generic service contract: CDLVSM runs
  `install-unit` after install/upgrade (then `restart` if it was running)
  and `stop`/`uninstall-unit` before removal. It does not start the service
  and installation pairs nothing.
- Desktop needs at least the service released with it, within its minor
  series (protocol 1 throughout). Desktop's WebKitGTK and portal are its own;
  the service and a headless install need neither.
- The terminal client (161) declares `requires=mike-service …` and no
  GUI capability; IDE (162) declares `feature.mike=mike-service …`, selected
  with `cdlvsm install ide --with mike`. Neither needs the other.

Mike v0.5.0 publishes all three package assets. Use CDLVSM 0.12.0 or newer
for dependency-aware installation. CDLVSM's `tests/mike_packages.rs` stages
packages from this checkout into a local release directory and installs them.
