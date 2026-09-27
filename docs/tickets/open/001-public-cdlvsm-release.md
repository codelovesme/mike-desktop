# 001 — Public Mike Desktop release through CDLVSM

## Acceptance criteria

- [ ] This app has its own public `codelovesme/mike-desktop` repository containing the Euglena sources, native fixtures, release workflow, and no local credentials or installed binaries.
- [ ] A version tag publishes a Linux x86_64 bundle with the launcher, genes, and lockfile-pinned native modules; a clean extracted bundle starts without this source checkout.
- [ ] The launcher accepts only a loopback Mike host URL (direct local host or an SSH tunnel), shows version/help, and reads no secret from the release bundle.
- [ ] The published CDLVSM CLI accepts `cdlvsm install mike-desktop`, `cdlvsm mike-desktop`, `cdlvsm upgrade mike-desktop`, and `cdlvsm uninstall mike-desktop`; installation adds an application menu entry.
- [ ] The app's native fixtures pass, the bundle smoke check passes with a clean home, and a real CDLVSM install from the public release is verified.
- [ ] The installed app reaches the live Mike host and preserves the existing file action confirmation workflow.

## Verification

Pending.
