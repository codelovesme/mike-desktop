# 004 — Mike can investigate a selected desktop with terminal commands

Status: in progress. Design: [local agent plan](../../plans/004-mike-local-agent.md).

## First release acceptance

- [ ] A user can ask Mike from web or desktop to inspect a selected paired Linux computer. A device without terminal support does not advertise or accept terminal work. Capability filtering and live Mike `pwd` are proven; both UI entry paths remain to verify.
- [x] Mike proposes a command, receives its exit code and bounded output, then can choose another command in the same turn; `pwd`, `ls`, and `ps` work without a six-step cutoff (live public HTTPS, extracted bundle, one turn).
- [x] The desktop shows each command and working directory and runs nothing until the local user approves. Denial is visible to Mike. The WebView cannot directly call the runner (GTK approve/decline bundle smokes, gateway result, bridge route check).
- [x] Commands execute as the desktop user in a child process, with no implicit shell or app-provided elevation (worker process tests).
- [ ] Stop, sign-out, account switch, device revocation, timeout, and desktop exit prevent later steps and terminate running child processes. Duplicate or uncertain steps do not run twice. Stop, native Sign out, and a signal-ignoring grandchild now pass; the remaining transitions need full integration checks.
- [ ] Other owners/devices cannot read a job, claim a step, approve it, or submit its result. Offline devices are not silently replaced.
- [ ] Output has length and time limits; stdout, stderr, nonzero exit, timeout, missing command, cancellation, and uncertain completion remain distinct. Worker tests cover limits, missing command, timeout, cancellation; gateway expiry marks unknown. Truncated output is labeled for Mike. Final UI display remains.
- [x] Native fixtures, Mike/gateway fixtures, a browser case, and a real Mike-to-extracted-bundle end-to-end test pass. Existing file action and conversation tests remain green (public HTTPS bundle smoke, live Mike test, nine browser cases).
- [ ] A public tagged bundle installs through unpinned `cdlvsm install mike` on clean supported Ubuntu and Debian desktops; documented rollback works.

This ticket is complete only when all criteria are checked with recorded evidence. Browser, clipboard, screen, input, and media adapters follow in separate tickets after the terminal loop.

## Evidence so far

- Gateway fixture: one passed with terminal capability filtering, owner/device isolation, claim, completion, and replay.
- Mike fixtures: four passed; one proves two terminal commands can complete in one turn without consuming the ordinary six-action count.
- Desktop fixtures: four passed. Python worker tests and GTK terminal confirmation smoke passed; the latter ran a real local `pwd` and checked the gateway result.
- Disposable-account live test on public HTTPS: Mike proposed `pwd` for `/tmp`, the bridge required local approval, the worker ran it, and Mike's final reply used `/tmp`.
- Existing guided file action still passed against the running host. Focused Mike browser suite: nine passed, zero failed, including a new terminal-result case.
- The extracted beta bundle's real GTK approval and worker passed over public HTTPS. Decline returned `denied` without starting a worker. Three successive fresh accounts passed against one retained WebKit profile after the browser-handoff account race was fixed.
- The public HTTPS live Mike turn proposed and completed `pwd`, `ls`, and `ps` as three separate approved commands on the extracted bundle, then answered from their output.
- The extracted bundle's GTK Stop test approved `sleep 30`, stopped it, and Mike received `cancelled`. A worker process test proved Stop kills a grandchild even when its parent ignores `SIGTERM`.
- The browser callback smoke passed against the extracted bundle and public HTTPS after the session-handoff fix. Python suite: 17 tests passed, one normal portal skip; the private D-Bus portal test passed separately.
- `v0.4.0-beta.1` release CI passed and published a prerelease asset. With `CDLVSM_MIKE_DESKTOP_VERSION=v0.4.0-beta.1`, CDLVSM 0.10.2 installed it on clean Ubuntu 24.04 and Debian 13 containers; each installed its WebKit runtime and Code, reported the expected version, created the Mike desktop entry, and kept the window open under Xvfb.
- An isolated CDLVSM install of the published beta used Code 2.13.0 and passed the real GTK approval, local `pwd` worker, and gateway result against public HTTPS.
- The native Sign out button was exercised while `sleep 30` ran: the worker stopped, the bridge lost its session and task, and Mike received `cancelled` over public HTTPS.
- `v0.4.0-beta.2` passed release CI and a pinned CDLVSM upgrade from beta.1. Its installed bundle passed the same live Sign out cancellation check on retry. One first run stopped the worker and cleared the bridge but did not expose a completed gateway result within 35 seconds; investigate this report race before stable promotion.
