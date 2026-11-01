# 006 — Mike CLI: a full-screen chat like Claude Code

The owner (2026-11-01), looking at `mike>` in the ide's terminal: Mike CLI
should look and work like `claude` or `codex`. The chat history goes on top,
an input box is fixed at the bottom, and messages are marked **You** and
**Mike** the way the old ide AI pane marked them. Nothing should happen out
of sight: the person sees what Mike is doing, what it runs and what it needs.
Claude Code's features may be copied: image paste, multiline input, a `/`
menu that opens at once, and so on. Voice input comes back, and the person
sees that it is recording.

This ticket covers everything the CLI can do with today's service and Mike.
Model choice, thinking level, modes, images reaching Mike and streamed
answers need Mike itself: see my-euglena-apps ticket 185.

## Outcome

- **Layout.** The input box sits at the bottom of the terminal from the
  start, between two rules, with a footer line under it. The conversation
  scrolls above it into the terminal's own scrollback, so wheel scrolling,
  selecting and copying stay native. Resizing redraws the box at the new
  bottom.
- **Messages.** `You` and `Mike` blocks; Mike's steps (a command, a file
  search, its result) are short indented lines; replies show basic markdown
  (headings, **bold**, `code`, code blocks, lists).
- **Working.** While a turn runs, a spinner line above the box shows how
  long it has taken and what Mike is doing now ("Mike: running `ls`…
  12s · esc to leave").
- **Input.** Enter sends. Alt+Enter, Ctrl+J or `\` then Enter add a line
  (Shift+Enter too where the terminal reports it). Pasted text is kept as
  pasted, newlines included. ←/→/Home/End, Ctrl+A/E/U/K/W, ↑/↓ move between
  lines and then through the history (kept as today). Ctrl+C clears the
  box; Ctrl+C on an empty box, twice, or Ctrl+D leaves.
- **`/` menu.** Typing `/` opens the command list with a line each under
  the box, filtered as you type; ↑/↓ choose, Tab completes, Enter runs.
- **Images.** Ctrl+V pastes a clipboard image (Wayland and X11) as
  `[Image #1]`; a pasted path of an image file does the same. Until Mike
  takes images (185), sending one says so plainly and sends nothing.
- **Approvals.** A question or a local request shows a box above the input
  with the command or the request in full, and choices picked with ↑/↓ +
  Enter or 1/2 (`Yes` / `No`; for a file request: search this project for…
  / decline). Enter alone never approves without a visible selection.
  Typed `/yes ID` … keep working.
- **Voice (updated by workspace ticket 188).** Hold Ctrl+Space to record
  (PipeWire, PulseAudio or ALSA). Releasing Space or Control stops; the words
  land in the draft after transcription. Repeats do not toggle recording.
  The microphone closes on focus loss/exit and after at most one minute.
  Needs a terminal with release events; no recorder → a clear message.
- **Footer.** Account, project folder, queue size, and `? for shortcuts`;
  model, thinking level and mode appear here as soon as the service
  reports them (185).
- **Commands.** `/help`, `/clear`, `/status`, `/new`, `/resume`, `/stop`,
  `/unqueue`, `/quit`, and today's `/yes`, `/no`, `/approve`, `/decline`,
  `/find`.
- `--ask`, `--resume` and piped input stay plain text, as today.

## Check

- tests/test_cli.py green, plus tests for the editor (multiline, paste,
  `/` menu, approval box, image refused, voice text inserted).
- tools/cli-pty-smoke.py in a real pty: box at the bottom, You/Mike blocks,
  queue, `/` menu, approval by selection, Ctrl-D.
- Tried by hand in the ide's terminal tab and in GNOME Terminal.

Done (2026-11-01): client/screen.py (the live area, editor, keys, `/`
menu, question box, clipboard images, recorder) under client/cli.py, which
keeps the plain output for `--ask`/pipes. tests/test_cli_screen.py (17) +
test_cli.py + test_cli_sign_in.py green; tools/cli-pty-smoke.py 23/23 in a
real pty read back through a small emulator; opened against the real
service on this machine (`/status`). Not yet tried by hand in GNOME
Terminal or the ide's terminal tab, and voice not tried on a real
microphone. Model, thinking level, mode and images wait for 185.
