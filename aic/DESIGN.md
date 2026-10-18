# aic — AI Compiler (working name)

A bridge between a human expert and a coding AI, so both can work at full speed
at the same time. The AI keeps working; the human keeps understanding, in their
editor, in parallel — and can steer at any moment.

"Compiler" here is a **compiler of ideas**: you write high-level pseudocode, and
a second Claude tells you whether your idea is coherent, whether it matches what
the worker actually built, and whether your mental model of the code is right.
It never writes code.

## Roles

| Role | What it is | Can do |
|---|---|---|
| **Worker** | A normal Claude Code session, run however you like | Everything it normally does |
| **Compiler** | Persistent headless `claude -p --resume` session | Read/Grep/Glob only. Advises; **never** stops the worker |
| **Human** | You, in Neovim | Sole authority over the worker (submit / hold / redirect) |

The worker is auto-attached: most recently active session in the project dir,
with a picker to switch.

Models: Sonnet for `:w` compiles (frequent, cheap), Opus for chat / ask-at-cursor
/ what-if (deep). Configurable per action.

## Backend

Plain files + a tiny Python daemon. Editors stay thin; Emacs later is cheap;
everything is `cat`-able.

```
.aic/                       (gitignored)
  docs/<name>.md            your docs: specs, notes (project-wide)
  sessions/<worker-session-id>/
    sent.<doc>.md           what this worker last got of doc <doc> (for diffing)
    trace.jsonl             normalized worker + human events
    seen.json               inbox state (seen node ids)
    diagnostics.<doc>.json  last compile of doc <doc>
    chat.md                 compiler REPL transcript
    mailbox                 human → worker messages (hook gate reads this)
```

The daemon:
- tails the worker transcript `~/.claude/projects/<project>/<session>.jsonl`
- watches human code saves (reported by the editor client)
- normalizes both into `trace.jsonl`
- runs compiles on request and writes `diagnostics.<doc>.json`

## Core loop

1. **Write** the spec: a markdown outline in free-form pseudocode. Lines may
   carry anchors (`@path/file.ts:symbol`) so compiler and `gd` can jump
   spec ↔ code.
2. **`:w` = compile.** Only your save of the spec triggers it. The compiler sees
   the spec plus everything that happened since the last compile. Output goes to
   quickfix. Diagnostic kinds:
   - **mismatch** — spec says X, worker did Y (or hasn't yet)
   - **incoherent** — spec contradicts itself / is ambiguous / has holes
   - **model** — your spec assumes something about the code that isn't true
   - **unspecified** — worker changed things the spec doesn't mention
3. **Submit** (explicit): you pick a **prompt template** (yours or a built-in:
   `spec` sends the doc's diff since this worker last got it, `selection` the
   selected lines, `message`, `review`…); the rendered prompt opens for
   editing and `:w` sends exactly that, plus any human code edits since the
   last submit. The worker only ever sees what you submit — docs are your
   private scratch. Templates come from files the worker could write, so the
   typed header is checked (no digits, `@`, invisible characters) and every
   editor send goes through the preview.
4. **Hold**: one key. The worker stops at its next tool call and waits.
   Release with *continue* or with a submit.

### Control channel (verified live, Claude Code 2.1.x)

- **The human's words arrive as real user input.** `aic submit` types into the
  worker's tmux pane (the hook records `$TMUX_PANE`). A hook's deny-reason
  *cannot* carry instructions: the worker rightly treats hook text as
  untrusted and ignored a test redirect that contradicted the user.
- **Typed header + pasted body.** Claude Code wraps pasted text in
  `<pasted_content>` and treats it as data unless the user's own typed words
  ask for it. So the one-line request is typed and the spec/diff is pasted.
- **Submit is already a redirect.** Input sent while the worker is busy is
  queued and absorbed *at the next tool boundary* of the running turn.
  `--now` additionally presses Claude Code's "send now" (ctrl+x ctrl+s),
  which interrupts the running tool.
- **Hold** = a `PreToolUse` hook returning `deny` **and** `continue:false`
  (either alone is not enough: `continue:false` alone still runs the tool).
  It is one-shot: the gate consumes it; a turn that ends first expires it.
- **Never type into a dialog.** Delivery refuses while a permission prompt is
  on screen (pane check + `Notification` hook state `asking`); otherwise the
  Enter could approve a command on the human's behalf.
- Every delivery is confirmed against the worker's transcript; a lost message
  is an error, never silent.

## Seeing the worker

- **Trace buffer**: foldable chronological tree
  `turn → tool call → one-line "why" → diff hunk`. `<CR>` shows/hides a node's details, `gf` jumps to file:line.
  Built from the transcript, so it uses no AI. Messages you sent mid-turn show
  as `✉ you (mid-turn)`; holds, submits and your own saves are nodes too.
- **Bash edits are visible.** Workers often edit with `sed`/`python` through
  Bash. After each worker Bash call the watcher diffs the project tree against
  a snapshot and records the changes as `Bash edit file:lines` nodes (live
  only: a rebuilt trace can't recover them for the past).
- **Inbox**: trace nodes are unseen until you visit or mark them. `]u` / `[u`
  go to the next/previous unseen node; the statusline shows the unseen count.
- **Human nodes**: your own code saves show up in the trace too.
- **Per-file history**: signs and blame on lines the worker touched, in your
  normal code buffers.

## Talking to the compiler

- **REPL**: a persistent chat buffer (`chat.md`).
- **Ask-at-cursor**: works on any spec line, diagnostic, trace node or code
  line. Opens a float with that context preloaded; the answer can be appended
  to the spec.
- **What-if**: on demand, on a trace node: "what if it had done X instead of
  Y?" The compiler sketches an alternative tree, clearly marked as
  *reconstructed* (the real alternatives aren't recorded in transcripts). One
  key folds the sketch into the spec; from there it reaches the worker only via
  a normal submit.

## Neovim UI

- One window, one tab. The views (history = trace, questions = compiler chat,
  spec) are buffers shown in the current window; jumps to code open in the
  same window and `<C-o>`/`<C-^>` come back. aic never creates a tab or a
  split. Floats are used only for short-lived things (answers, usage, blame).
- Everything is exposed as commands and `<Plug>` mappings; default keys go
  under one leader prefix.

## Docs

You write in as many **docs** as you like (`.aic/docs/<name>.md`, project-wide,
gitignored scratch): specs, notes, plans. Not every doc is a spec, so a doc
compiles on `:w` only when its first line says so (`<!-- aic: compile -->`).
What each worker was last sent of a doc is remembered per worker, so a submit
still sends only the diff. One compiler session serves all docs. The old
per-session `spec.md` becomes doc `spec` once (migration).

## Milestones (each usable on its own)

- **M1 — Trace (no AI):** daemon, transcript parser, trace buffer, inbox,
  human nodes, per-file signs.
- **M2 — Compile:** spec buffer, anchors, `:w` → compiler → quickfix.
- **M3 — Gate:** hook install, submit (diff/selection), hold/continue.
- **M4 — Talk:** REPL, ask-at-cursor, what-if sketches.

## Verified findings (were risks)

- **Thinking is stored empty** in transcripts. The "why" is the visible text
  the worker wrote before a tool call; quiet workers (e.g. Sonnet) often have
  none, which is why ask-at-cursor exists.
- **Hold semantics**: see the control channel above.
- **Transcript format** is internal to Claude Code; all assumptions live in
  `aic/transcript.py` (queued messages are `attachment.queued_command`,
  harness turns carry `origin.kind != "human"`, re-sent prompts share a
  `parentUuid`).

## Open (not yet decided)

- Name
- Keymaps beyond the defaults (`<leader>z` prefix; `<leader>a` is harpoon)
- Trace node display format
- Anchor syntax details (line ranges? symbol paths?)
- Emacs client
