# 001 — Public Mike Desktop release through CDLVSM

## Acceptance criteria

- [x] This app has its own public `codelovesme/mike-desktop` repository containing the Euglena sources, native fixtures, release workflow, and no local credentials or installed binaries.
- [x] A version tag publishes a Linux x86_64 bundle with the launcher, genes, and lockfile-pinned native modules; a clean extracted bundle starts without this source checkout.
- [x] The launcher accepts only a loopback Mike host URL (direct local host or an SSH tunnel), shows version/help, and reads no secret from the release bundle.
- [x] The published CDLVSM CLI accepts `cdlvsm install mike-desktop`, `cdlvsm mike-desktop`, `cdlvsm upgrade mike-desktop`, and `cdlvsm uninstall mike-desktop`; installation adds an application menu entry.
- [x] The app's native fixtures pass, the bundle smoke check passes with a clean home, and a real CDLVSM install from the public release is verified.
- [x] The installed app reaches the live Mike host and preserves the existing file action confirmation workflow.

## Verification

- Public repository: https://github.com/codelovesme/mike-desktop; release `v0.1.1` passed GitHub Actions, including `euglena test`, package integrity checks, and an extracted window running under Xvfb with a clean home. The first tag's smoke check caught a runner path error; `v0.1.1` fixed it and passed.
- CDLVSM `v0.9.1` was published. An isolated install fetched both public releases, installed Code, created `codelovesme-mike-desktop.desktop`, dispatched `--version`, recognized an already-current upgrade, and uninstalled cleanly. A second public CLI install started with Code 2.10 and upgraded it to 2.11 before installing Mike Desktop. The network integration test and CDLVSM main CI passed.
- Using the actual installed release's genes and native modules, the live end-to-end runner signed into the real host, approved Mike's task and the exact local file, opened it, checked Mike's saved reply, and revoked the test device. The disposable auth account was removed afterward.
