# Mike in a terminal

`bin/mike-cli` in the checkout; `mike-cli` in its independently staged
package (`cdlvsm install mike-cli`). It is Mike the way Claude Code and Codex
are themselves: a program for any terminal — the ide's terminal tab
included — that signs in and talks on its own. It attaches to this user's
shared Mike service and never starts a model or another paired executor.
`mike` and `mike-desktop` keep opening Desktop. Neither Desktop nor the ide
is needed.

## Signing in

```
mike-cli --sign-in          # the browser opens; sign in, press Connect
mike-cli --account          # who is signed in on this computer
mike-cli --switch-account   # sign in as someone else (an explicit choice)
mike-cli --sign-out         # sign this computer out of Mike
```

`--sign-in` starts the shared service first if it is not running, then
opens codelovesme's `/desktop/connect` page (the handoff Desktop uses: a
one-use state, the session token posted to a loopback callback straight
into the service — never saved by CLI, printed or put in argv). With no
browser to open it prints the address to open on this computer. A
different account than the service's is refused unless `--switch-account`.
In a terminal, plain `mike-cli` offers both when they are missing — "Start
it?", then "Sign in … in the browser?" — and does neither unasked; a
pipeline or `--ask` run never does.

Desktop, the CLI and every other client share the one sign-in of the
service. `mike-cli --connect` still takes a pasted account session token
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

## The screen

In a terminal `mike-cli` is a full-screen chat, like `claude` or `codex`.
The input box sits at the bottom between two rules, with a footer under it
(account, project, queue, and model / thinking / mode once the service
reports them). The conversation scrolls above it into the terminal's own
scrollback, as **You** and **Mike** blocks; Mike's steps and their output
are indented lines, replies show headings, **bold**, `code`, code blocks and
lists. While Mike works a spinner says what it is doing and for how long.

- **Enter** sends; **Alt+Enter**, **Ctrl+J** or `\` then Enter add a line
  (Shift+Enter too where the terminal reports it). Pasted text stays as
  pasted. ←/→, Home/End, Ctrl+A/E/U/K/W edit; ↑/↓ move between lines, then
  through earlier messages, kept between runs in
  `$XDG_STATE_HOME/mike-cli/history` (0600).
- **`/`** opens the command list, filtered as you type; ↑/↓ choose, Tab
  completes, Enter runs. **`?`** on an empty box lists the shortcuts.
- **Ctrl+V** pastes a clipboard image (wl-paste or xclip) as `[Image #1]`;
  a pasted image path does the same. Mike cannot take images yet
  (my-euglena-apps ticket 185), so sending one is refused and says so.
- **Hold Ctrl+Space**: record immediately; release after holding to transcribe
  into the draft. Enter sends only after the transcript is ready.
- **Tap Ctrl+Space** (under 350 ms): keep recording after release. Ctrl+Space
  again, bare Space or Enter stops and transcribes; that stopping key never
  sends the message. Both recording modes keep the one-minute capture bound.
- **Ctrl+Shift+Space**: spoken conversation on/off. Mike listens until you pause,
  sends that utterance, performs work through the existing approval flow, speaks
  the result, then listens again. **Ctrl+Space also turns conversation off.**
  Thirty seconds without speech ends listening automatically. The microphone is
  closed during transcription, model work and playback. Spoken yes/evet or
  no/hayır applies only to the exact displayed approval; other words never
  silently approve a command. Existing draft text/images are not auto-submitted.
  Finish an existing manual capture before starting conversation.

These shortcuts require the updated Console/IDE (open a new window) or a
terminal advertising Kitty key-release support. Conversation also needs the
updated Mike service and a local recorder/player (PipeWire, PulseAudio or ALSA).
Closing the window or leaving the CLI closes capture/playback; stopping
conversation never stops or approves a running tool command.

- A message sent while Mike is still on one waits its turn (ten at most,
  shown above the box) and goes when that turn ends; `/unqueue` takes them
  back unsent.
- **Esc** closes the menu, or stops the command Mike runs on this computer.
  **Ctrl+C** clears the box; twice on an empty box detaches. **Ctrl+D**
  detaches. Detaching never stops the service or Mike's accepted work.
- `/clear` clears the screen only (Mike keeps the conversation); `/status`
  says who, which computer, which project and turn.

A question from Mike, or a request to run something on this computer, shows
in a box above the input with the command in full and numbered choices.
Nothing is chosen until you pick one with ↑/↓ or its number, so Enter alone
never answers. `--ask`, `--resume` without a terminal, and piped input print
plain lines as before.

## Conversations

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

## CodeLovesMe IDE context

When launched in an IDE terminal with session context support, the CLI refreshes
that IDE's identity, project and originating terminal before each message.
Mike can use its existing local terminal tool to run `ide-control` for state,
terminal movement and resizing. The context supplies an absolute helper command
and a private descriptor path, so the shared service need not inherit the
terminal's environment. No credential is sent to the model, and context grants
no tool permission: the normal local command approval still applies.

A closed or unavailable IDE fails clearly before submitting a new message.
Outside an IDE, message submission is unchanged. Resuming an existing turn
continues that turn's original context; it does not retarget already accepted
work. A new message refreshes context for its current terminal.

## Models, thinking, modes and images (chat protocol 2)

`/model` lists configured chat models and their image/thinking capabilities;
`/model MODEL` selects one. `/think off|low|medium|high` selects the requested
thinking level on a compatible model. The footer shows the active turn's
choices; changes during work apply to the next message, including messages
queued while Mike works. `/mode ask|plan|auto`, or Shift+Tab, selects:

- ask: every local command needs confirmation, even when a run grant exists;
- plan: Mike proposes a plan and runs no tool or memory operation; saying yes
  explicitly continues in ask mode, and saying no preserves the plan;
- auto: only commands already covered by a run grant execute without asking.
  Outside those grants, ordinary approval remains necessary.

Ctrl+V attaches a clipboard PNG/JPEG; pasted file paths also attach supported
images. Up to three files, 2 MiB each and 4 MiB total. Sending to a model that
cannot see images fails clearly and keeps the draft. Image bytes are validated
again by the local service and fully decoded by Mike's API; they are not put in
conversation archives or memory extraction. Images and choices follow the turn
through explicit resume; they are never retargeted by another client's settings.

Replies still arrive when complete. Incremental response display is deferred.
The client still supports chat protocol 1 for ordinary text; new controls need
an updated service and API. A service restart retains pairing but its account
session is held in memory: reconnect through the normal browser sign-in flow.

The shared service persists a successful account sign-in privately and restores
it after restart. CLI and Desktop reuse that account; no separate login per
window. A reconnecting service reports an offline/restoring error rather than
asking for another login. Expired/refused credentials still require sign-in.
Only the service stores the account record; the CLI does not read its file.
