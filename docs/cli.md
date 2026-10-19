# Mike in a terminal

`bin/mike-cli` in the checkout; `mike-cli` in its independently staged
package. It attaches to this user's existing shared Mike service, never starts
a model, another paired executor or its own service. `mike` and
`mike-desktop` keep opening Desktop. IDE and Desktop do not depend on CLI.

Start the shared service with `mike-service start`. Sign in via Desktop's
browser handoff, or use `mike-cli --connect` and paste an account session token
into its hidden terminal prompt. The token is passed to the existing service
for account verification, never saved by CLI, placed in argv or printed.
`--token-stdin` reads one protected stdin line for a `--ask`/`--resume` run.
There is no CLI account password store. Signing into a different account must
be done explicitly through Desktop; CLI refuses to switch it silently.
Headless enrollment alone authorizes tools, not access to an account's chat:
a chat token must still be supplied. Never paste tokens into shell commands.

```
mike-cli                         # interactive conversation
mike-cli --project /srv/project   # explicit project context
mike-cli --ask 'Run the tests'    # one turn, then exit
mike-cli --resume TURN_ID         # explicit central-turn continuation
printf 'What can you do?' | mike-cli
```

The project defaults to the starting directory. Its absolute path and this
service's device id go to central Mike as context, **not permission**. Central
Mike owns recent conversation and memory. Follow-up messages are normal new
central turns with that existing history; `--resume` attaches to an existing
turn without resending the original request. It is owner-checked centrally,
then bound to this local client session. No separate local chat database is
created; in-memory central turns disappear on central Mike restart.

Question/approval commands include the first 12 characters of the displayed
question/request id. Empty Enter, bare `yes`, another message, reconnecting
or closing the terminal never approve. Model/tool output is never parsed as
an input command; terminal control characters are removed and text bounded.

- `/yes ID`, `/no ID`: answer the displayed central question. A service-side
  one-use id and central comparison of the exact pending question reject a
  stale/repeated answer, even if another client advanced the same turn.
- `/approve ID`, `/decline ID`: explicitly claim/answer this turn's local
  request. Other clients' tasks are not offered for approval here.
- `/find ID QUERY`: for a file request, choose the project as the search
  folder. Opening an exact result requires its new approval id and the
  existing desktop portal; CLI does not supply a display on a headless host.
- `/stop`: requests stopping only this turn's currently running command on
  this local service. Actual outcome comes back through progress; it does
  not cancel the model or promise that later steps cannot be proposed.
- `/leave` or `/new`: stop following this turn; already accepted work may
  continue. Does not clear central history or stop other clients.
- `/resume TURN`, `/quit`, `/help` do what their names describe. EOF or
  Ctrl-C detaches the client, releases its local approval claim and leaves
  the service and accepted work alive. Ctrl-U cancels a partially typed line
  through the terminal's normal line editing, without stopping a job.

Non-TTY mode accepts a single stdin message or `--ask`/`--resume`, prints
progress and exits. If input/approval is needed it exits **3**, naming the
turn for interactive resumption, without answering. Waiting is bounded by
`--wait` (default 300 s, maximum 3600); this is not a job duration guarantee.
Exit 0 = done/normal detach, 1 = failed turn, 2 = invalid invocation,
3 = interactive decision required, 4 = unavailable/incompatible/uncertain
transport or timeout, 5 = sign-in/account change, 130 = Ctrl-C detach.

Transport and presentation are separate (`client/mike_service.py` versus
`client/cli.py`); output is human-readable, not a promised automation format.
On a read failure CLI reattaches and fetches only a known turn id. Ask and
answers are never automatically retried: if their reply is lost, the outcome
is uncertain. On account change CLI stops before displaying more old data;
text already printed in the user's terminal cannot be erased from scrollback.

Requires local protocol 1 plus `chat_version=1` (ticket161); older services
are rejected clearly. Staging, without installing or publishing:

```
python3 tools/package.py --cli-only v0.5.0 /tmp/mike-packages
```

The archive contains only Python client code, its launcher and metadata.
`requires=mike-service` uses CDLVSM's generic dependency resolver, with the
same minor-series compatibility rule as Desktop. `cdlvsm install mike-cli`
becomes usable when that release's assets have been published; a source
commit does not publish a release. No GUI or Code runtime is bundled in CLI.
