# aic protocol (backend ⇄ editor contract)

Everything is plain files under `<project>/.aic/` plus one-shot CLI commands.
Editors never talk to Claude directly; they read files and run `aic <cmd>`.

All paths inside files are **relative to the project root** unless named `*_abs`.
All JSON files are UTF-8. `*.jsonl` = one JSON object per line, append-only.

## Layout

```
.aic/
  .gitignore              "*"
  current                 pinned worker session id (absent = auto: most recent worker)
  workers/<sid>.json      written by the hook on every worker event
  compilers.json          ["<sid>", ...]  claude sessions owned by aic (excluded from pickers)
  watch.pid               pid of the running `aic watch`
  docs/<name>.md          the human's docs, shared by all worker sessions (editor writes; see Docs)
  prompts/<name>.md       project prompt templates (see Prompt templates)
  prompts/compiler/<name>.md  project compiler templates (see Compiler templates)
  sessions/<sid>/
    sent.<doc>.md         what this worker last received of doc <doc> as work (marker stripped): set by a `marks_sent` send
    trace.jsonl           normalized events (written ONLY by `aic watch`)
    human.jsonl           human code-save events (appended ONLY by the editor)
    seen.json             {"seen": ["<event id>", ...]}  (written by editor)
    touched.json          per-file touched line ranges (written by `aic watch`)
    diagnostics.<doc>.json  last compile of doc <doc> (written by `aic compile --doc`)
    fixes.<doc>.json      the compiler's last proposed fixes to doc <doc> (written by `aic fix --doc`)
    chat.md               compiler REPL transcript (written by `aic ask`)
    hold                  exists ⇒ worker is to be held at its next tool call (one-shot; the gate deletes it)
    control.jsonl         submit/hold/release/held/hold-expired events (appended by backend + hook)
    compiling             exists ⇒ a compile is running (contains pid)
    compile.again         docs queued for one more compile (JSON list; backend-private)
    state.json            compile/ask/submit bookkeeping (backend-private; updated under state.json.lock)
    spec.md, spec.submitted.md, diagnostics.json   before docs existed: migrated once (see Docs), then unused
    usage.json            context/cost per session (see Usage; backend-written)
    watch.json            watcher resume point: offsets, seq, parser state (backend-private)
```

## workers/<sid>.json

```json
{"session_id": "...", "transcript_path": "/abs/...jsonl", "cwd": "/abs",
 "pane": "%12" | null, "tmux_socket": "/tmp/tmux-1000/default" | null, "pid": 1234, "tty": "/dev/pts/3",
 "state": "busy" | "idle" | "held" | "asking" | "ended",
 "last_event": "PreToolUse", "updated": 1760000000.12, "prompted": 1760000000.0}
```
`tmux_socket` = the server `pane` belongs to (from `$TMUX`); aic addresses
that server only (null: written before it was recorded, the default server).
`pid`/`tty` = the Claude Code TUI process; delivery types into `pane` only while
that process is alive, owns the pane's terminal and is in its foreground.
`pane` becomes null on `SessionEnd`. Auto-attach prefers live workers with the
latest `prompted` (prompt/session start), so tool calls don't flip it.

## trace.jsonl events

The file is append-only and `seq` is stable across watcher restarts (it
resumes from watch.json). Only when that state is missing is the trace rebuilt,
as a new file (new inode): editors must reload when the inode changes.

Common fields: `id` (string, stable, e.g. `"e17"`), `seq` (int), `ts` (unix float),
`kind`, `turn` (int, worker turn number; human events carry the current turn).

| kind | extra fields |
|---|---|
| `prompt` | `text` — the user prompt that started turn `turn` (truncated to 2000 chars). `midturn: true` = sent while busy, absorbed inside the turn; `system: true` = harness-generated (e.g. background task finished); `resent: true` = the human re-sent/edited the prompt |
| `tool` | `via: "Bash"` + `id "<bash id>:<file>"` when the change was found by diffing the tree after a worker Bash call; `tool` (name), `title` (one line, e.g. `Edit src/app.js:14-22`), `why` (assistant visible text preceding the call, may be `""`), `file` (rel path or null), `hunks` (see below, may be `[]`), `input` (short string summary of tool input), `detail` (the input as a person reads it, at most 4000 characters: a Bash command as `$ <command>` under its description, an Agent's description and prompt, else JSON; null for file tools; absent in traces written before it existed), `error` (bool) |
| `text` | `text` — assistant visible text that was NOT followed by a tool call (end-of-turn message) |
| `human` | `file`, `hunks` — a code save by the human |
| `submit` | `text` — what aic delivered to the worker |
| `hold` / `release` | — |
| `held` | `tool` — the gate stopped the worker before this tool |
| `hold-expired` | the turn ended before any tool call; the hold was dropped |
| `guarded` | `tool`, `file` — the hook refused a worker file tool (or a writing Bash command) aimed at `.aic/docs/` or `.aic/prompts/` |
| `compact` | the worker's context was compacted: `trigger` (manual/auto), `pre`, `post` tokens |

Hunk: `{"start": <new-file first line, 1-based>, "count": <new line count>, "lines": ["+added", "-removed", " context"]}`.

## touched.json

```json
{"src/app.js": [{"start": 14, "end": 22, "event": "e17", "actor": "worker"}, ...]}
```
Ranges are in **current** file coordinates (later edits shift earlier ranges).

## Docs

- `.aic/docs/<name>.md`; name `[A-Za-z_][A-Za-z0-9_.-]*`, no `/`, no `..`
  (checked on every `--doc`).
- Optional first-line marker: `<!-- aic: compile prompt=<template> -->`.
  `compile=<name>` instead of `compile`: compiled with compiler template
  `<name>` (a `task: compile` one, see Compiler templates). `aic docs --json`
  reports it as `compile_template`.
  `compile` = the editor runs `aic compile --doc <name>` on save. The marker
  is never sent to the worker; compile numbers the file as it is (marker
  line included), so line numbers are always file lines.
- **Migration**, once per project (when `aic docs` / `aic init` / any `--doc`
  command finds no `docs/` dir; locked): the old per-session `spec.md` becomes
  `docs/spec.md` with `<!-- aic: compile -->` (the attached session's if it
  has more than the old template, else the most recently changed one). Every
  session's `spec.submitted.md` becomes its `sent.spec.md` and its
  `diagnostics.json` `upto_seq` its `compile_upto_seq`. The old files are left in place.
- Submit: range lines (`--from/--to`) are file lines, but the marker line is
  never sent; diff hunks are computed on the doc without its marker.

## Prompt templates

What a submit sends. Lookup, first match wins: `.aic/prompts/<name>.md`
(project), `$XDG_CONFIG_HOME/aic/prompts/<name>.md` (user, default
`~/.config/aic/prompts`), `aic/prompts/<name>.md` (built-in: `spec`,
`selection`, `message`, `review`).

```markdown
---
description: one line for the picker
header: "Typed into the worker as your own words: ask it to act on the body"
requires: [changed]   # refuse to render while these are empty
marks_sent: true      # this send makes the doc the worker's diff base
input: Message        # {{input}} is asked for (the prompt text)
---
the body, pasted under the header: {{doc}}, {{diff}}, … anywhere
```

- **Header** = typed as the human (one line); **body** = pasted. Claude Code
  treats pasted text as data unless typed words ask for it, so the header must
  ask for something. The rendered header is checked at render and again at
  send: whitespace collapsed, `[aic] ` prepended when missing (so it can never
  be a `/command`, `!bash` or `#memory`), at most 300 characters, and refused
  if it holds digits of any script (a dialog could take them as menu choices),
  `@` (opens Claude Code's file picker), `\`, or anything invisible: Unicode
  categories Cc Cf Cs Co Cn Zl Zp (controls, zero-width, bidi, Unicode tags,
  private use), which would type words nobody saw in the preview. The body is
  refused if it holds Unicode tag characters or bidi overrides. A trailing `;`
  is escaped for tmux.
- **Front matter values**: `"double"` (with `\"`) or `'single'` quotes,
  optionally followed by `# comment`; unquoted, ` #` starts a comment, so
  quote a value that contains `#`. CRLF files are read as LF.
- **Syntax** (header and body): `{{var}}`; `{{#var}}…{{/var}}` kept when var
  is non-empty, `{{^var}}…{{/var}}` when empty (whitespace-only = empty);
  sections nest with different names; `\{{` = literal `{{`. A line holding
  only a section tag disappears with its newline. Values are inserted once and
  never re-scanned. Unknown names, unclosed or mismatched sections are errors.
  The body is trimmed of leading/trailing newlines.

| var | value |
|---|---|
| `doc` | the doc without its marker line, stripped |
| `doc.name` | the doc's name |
| `selection`, `selection.lines` | the selected file lines (marker line excluded) and `N-M`; empty without a range |
| `content` | `selection` with a range, else `doc` |
| `previous` | what this worker last got of the doc (`sent.<doc>.md`); empty = never |
| `changed` | `yes` when the doc differs from `previous` |
| `diff` | unified diff `previous` → doc (empty when never sent or unchanged) |
| `anchors` | referenced context: anchors in what is sent, resolved for the worker (see Anchors, submit) |
| `hand_edits` | the human's code saves since the last submit (`file:N-M, …`) |
| `diagnostics` | the doc's last compile items, one per line |
| `input` | text asked for at submit (`--input`) |

- **Anchors** are resolved from: the body's own text, `input`, the selection,
  and the doc, except that when `diff` is sent (and non-empty) only the
  diff's added lines count instead of the whole doc. A body that does not place
  `{{anchors}}` gets them appended (`Referenced context (resolved from my
  @anchors):`); one that does not place `{{hand_edits}}` gets
  `My hand edits since my last message: …` appended. No template drops them.
  "Placed" means actually rendered: a `{{hand_edits}}` inside a section that
  is left out does not count.
- **Receipt** (part of `aic render --json`): `{prompt, source, doc,
  marks_sent, sent, previous_hash, uses_previous, edits_sent, seq}`. It comes
  back from a file, so sending checks it first (types; `seq` an int within
  the trace; the doc name). Sending applies it: `sent.<doc>.md` := `sent`
  only when `marks_sent`; `submit_seq` := max(current, `seq`) only when
  `edits_sent`. A send whose template is `marks_sent` or placed `diff`,
  `previous` or `changed` is refused when `sent.<doc>.md` changed since
  rendering (a stale diff): render again. Sends to one worker are serialized
  (check, type, record under one lock). Editing the body in a preview does not
  change what is recorded as sent: a `marks_sent` send records the doc as
  rendered.

## Compiler templates

What compile, ask, what-if and fix tell the compiler (`aic/compiler_prompts.py`).
Same language as prompt templates, in a `compiler/` subdirectory of the same
three places: `.aic/prompts/compiler/` (project),
`$XDG_CONFIG_HOME/aic/prompts/compiler/` (user), `aic/prompts/compiler/`
(built-in: `compile`, `ask`, `whatif`, `fix`). The compiler's role (system
prompt: read-only, never edits files, never talks to the worker) is not a
template.

```markdown
---
description: one line for the pickers
task: ask        # compile | ask | whatif | fix (a built-in name implies it)
model: haiku     # optional, user/built-in templates only: overrides AIC_COMPILE_MODEL / AIC_ASK_MODEL / AIC_FIX_MODEL
---
instructions, with the data placed anywhere: {{doc}} … {{activity}}
```

- **Which template runs:**
  - compile: the doc marker's `compile=<name>`, else `compile`.
  - ask: `aic ask --template <name>` (a `task: ask` one), else `ask`.
  - what-if: `whatif`.
  - fix: `fix`.
  - A same-named project or user file overrides a built-in. A template whose
    `task` is not the one asked for is an error (for a compile, it is recorded
    in that doc's diagnostics).
- **Variables.** Each data variable is a whole block with its `== HEADING ==`
  line, or empty when there is nothing (for example `diagnostics` before any
  compile), so `{{#var}}` sections work. The exception is `question`, the
  human's raw text. All tasks also have `doc.name`.

| task | data variables, in append order |
|---|---|
| compile | `doc` (numbered), `previous_items`, `anchors`, `activity` |
| ask, whatif | `doc`, `diagnostics`, `activity`, `pointing` (context at the cursor, trace event), `question`, `anchors` |
| fix | `doc` (numbered, as the editor has it), `items` |

- **A template places the data; it can't drop it.** Every non-empty data
  variable the template doesn't emit is appended after it, in the order above
  (`question` as `== QUESTION ==\n<text>`). "Emitted" means actually rendered,
  as with prompt templates.
- **Trust rule.** A template is instructions, so whatever is in it can talk
  the compiler into ignoring data. aic can't prevent that; it limits who can
  write templates and makes the ones in use visible.
  - **Who can write them.** Project templates live under `.aic/prompts/`, in
    the worker's reach. The hook refuses the worker's file tools there, and
    Bash commands that write there.
    - The command is read the way a shell reads it: quoted text is one word,
      and paths resolve against the cwd, following `cd`.
    - Only this project's `.aic/docs` and `.aic/prompts` count, and `.aic`
      itself only for what replaces or removes it. They count only as a
      destination:
      - a `>`/`>>`/`&>` redirect
      - the target of `cp`/`rsync`/`install`/`ln`
      - the files of `mv`/`rm`/`touch`/`tee`/`mkdir`/…
      - `dd of=`
      - in-place `sed`/`perl`/`awk`
      - `find -delete`, or `find -exec` running any of these
      - a command's own output flag (`curl -o`, `wget -O`, `sort -o`,
        `unzip -d`, `tar -x -C`, `cp -t`)
      - a `git` subcommand that writes files (not `--cached`/`--staged`)
    - `$(…)` and backticks outside single quotes are read too, and so is
      `sh -c`/`bash -lc`. Wrappers and their options (`env -u X`, `sudo -u`,
      `nice -n`, `timeout -s`) are skipped.
    - An inline python/node/ruby/perl script counts when it names such a path
      and calls something that writes files.
    - Reading and mentioning go through: grep, cat, ls, copies out, commit
      messages, test strings, heredoc bodies.
    - Not followed: variables, globs, script files, scripts fed on stdin or
      through a here-string, `eval`, `pushd`, `find . -name … -delete`. This
      is a speed bump, not a sandbox.
  - **What a project template can't change.** It shapes instructions only: its
    `model:` is ignored, and its own `@anchors` resolve inside the project only
    (no outside files, no `--add-dir`). User and built-in templates can do both.
  - **Data stays in the prompt.** Nothing the task has is dropped (see above).
    After a template that is not built-in, aic adds a closing block the template
    doesn't control (`== FROM AIC, NOT FROM THE TEMPLATE ==`, always the last
    one): its instructions may change how the compiler works, never what it
    reviews. Compiler templates with invisible characters (control, format,
    zero-width, bidi, private use) are refused.
  - **The ones in use are named.**
    - The usage note and the `chat.md` heading carry ` [name, source]`.
    - A project template that hides a user one adds `, shadows user`.
    - The compile's `diagnostics.<doc>.json` records `template: {name, source, shadows?}`.
    - The editor names such a template on every compile and when diagnostics load.
  - **Out of scope.** A worker whose shell can write anywhere you can (the
    user template dir, aic itself) can get around all of this. The rule guards
    against an overeager worker and against text that merely looks like
    instructions, not against a hostile one with an unrestricted shell.
- `aic compile --print`, `aic ask --print …` and `aic fix --print [--json]`
  print the prompt and model without asking or recording anything. After a
  compact, the next real call also gets the handoff summary in front of it,
  which `--print` does not show.

## diagnostics.\<doc\>.json

```json
{"doc": "spec", "compiled_at": 1760000000.0, "upto_seq": 42, "summary": "one paragraph",
 "items": [{"kind": "mismatch|incoherent|model|unspecified",
            "severity": "error|warning|info",
            "file": "doc" | "<rel path>", "line": 12,
            "spec_line": 12 | null, "message": "..."}],
 "template": {"name": "compile", "source": "built-in"}}
```
`file == "doc"` means the line is a line of that doc (`"spec"` in files
written before docs existed). `spec_line` = a related doc line.
On compile failure: the previous content plus `{"error": "message", "compiled_at": ...}`.
One compiler session serves every doc: worker activity is sent once
(`compile_upto_seq` in state.json), and each compile replaces only that
doc's items.

## fixes.\<doc\>.json

`aic fix` asks the compile session for edits to a doc that resolve that doc's
current diagnostics. The compiler still writes nothing: the edits are data,
and the editor applies the ones the human picks to its own buffer.

```json
{"doc": "spec", "created_at": 1760000100.0, "compiled_at": 1760000000.0, "base_hash": "<sha256 of the text>",
 "fixes": [{"id": 1, "item": 3, "title": "Name the retried exception", "why": "one sentence",
            "kind": "incoherent", "severity": "warning", "message": "<the item's message>",
            "edits": [{"old": "exact text", "new": "replacement", "line": 12, "end_line": 12}],
            "conflicts": [2]}],
 "rejected": [{"title": "...", "item": 4, "reason": "its text occurs 2 times in the doc"}],
 "skipped": [{"item": 5, "message": "...", "reason": "the worker is wrong here, not the doc"}],
 "cost_usd": 0.01}
```
- `item` = 1-based index into that compile's `items` (`compiled_at` says which
  compile); `null` for a supporting fix that no item names.
- Every edit's `old` occurs exactly once in the text the proposal was made
  from. No edit touches an `<!-- aic: ... -->` marker line, and `new` has no
  invisible or bidi characters. A fix that fails these checks is in `rejected`.
- `conflicts`: the fixes whose edits overlap this one's; apply at most one of
  them.
- An editor locates each `old` again in its current text before applying, and
  refuses fixes that are stale (`old` gone or no longer unique).
- `skipped`: items the compiler answered without a doc change, plus every
  item it did not answer at all (`no doc change proposed`).

## Usage

`sessions/<sid>/usage.json` (written by the backend under `usage.json.lock`):

```json
{"compile": {"session_id": "…", "model": "claude-sonnet-5-5", "ctx_tokens": 41234,
             "ctx_window": 200000, "turns": 12, "cost_usd": 1.2, "updated": 1760000000.0,
             "compacted_from": "…" | null,
             "calls": [{"ts": 0, "in": 2, "cache_read": 30000, "cache_write": 4000,
                        "out": 900, "cost": 0.08, "note": "compile"}]},
 "ask":    { …same… },
 "worker": {"model": "claude-sonnet-5-5", "ctx_tokens": 106701, "ctx_window": 200000 | null,
            "turn": 21, "updated": 1760000000.0},
 "windows": {"claude-sonnet-5-5": 200000}}
```
- `ctx_tokens` = prompt size of the session's latest call (input + cache read +
  cache write) = how full its context is. `calls` keeps the last 30.
- `windows` = context window per model id, learned from compiler calls; the
  worker's `ctx_window` comes from it (null until known).
- `aic usage --json` prints `{"sessions": <usage.json>, "quota": … | null}`;
  sessions that predate usage.json are filled in from their transcripts with
  `"backfilled": true` (context and model only)
  where quota (read from the OMC HUD cache when present, never fetched) is
  `{"five_hour": 57, "five_hour_resets_at": "ISO", "weekly": 15,
  "weekly_resets_at": "ISO", "buckets": [{"id","label","percent","resets_at"}], "age_s": 30}`.
- `aic compact compile|ask` asks that compiler session for a handoff summary
  and continues in a fresh session seeded with it; `--reset` starts fresh with
  no summary. Prints one line. The next compile after a compile-reset re-reads
  the full trace.

## human.jsonl (editor appends)

```json
{"ts": 1760000000.0, "file": "src/app.js", "hunks": [ <hunk>, ... ]}
```

## Anchors

Anchors work in docs, in chat, and in ask questions. The grammar below is
canonical; Python and Lua must both pass `tests/anchor_cases.json`.

- An anchor starts with `@` at the start of a line or after whitespace or one
  of `( [ { " ' ,` (so `a@b.com` and `` `@x` `` are not anchors).
- Token: `@show:` takes `[\w./^~@{}-]+`; anything else runs until whitespace
  or one of `( ) [ ] { } " ' ` , ; < > |`.
- Trailing `. , ; : ) ]` are stripped, except a `..` path part (`@log:../..`);
  a trailing `}` is stripped unless it closes a `{` (`@show:HEAD@{1}`).
  `!` and `?` are not stripped (so `@a.py!` is not an anchor).
- Path and suffix split at the first `:`. Suffix `N` (N ≥ 1), `N-M`
  (1 ≤ N ≤ M), or a symbol `[A-Za-z_][\w.]*`; anything else is not an anchor.
- An existing directory, with or without trailing `/`, is a dir anchor (no
  suffix allowed); a regular file written with a trailing `/` is not an anchor.
- One entry per occurrence (resolution de-duplicates).

| anchor | means | `gd` in Neovim |
|---|---|---|
| `@path/file.ext` | a project file | open it |
| `@path/file.ext:42` | one line | open at line |
| `@path/file.ext:10-20` | a line range | open at 10, select 10-20 |
| `@path/file.ext:symbol` | first definition-ish match of `symbol` (`[A-Za-z_][\w.]*`) | open at the match |
| `@path/dir/` | a directory (trailing `/`) | open the directory listing |
| `@/abs/path…` `@~/path…` | the same forms, outside the project | same |
| `@diff` | `git diff HEAD` of the whole project (staged + unstaged) | diff in a scratch buffer |
| `@diff:path` | `git diff HEAD -- path` | same |
| `@staged` | `git diff --cached` | same |
| `@log` / `@log:path` | recent `git log` (whole repo / one path, with --stat) | log in a scratch buffer |
| `@show:REV` | `git show REV` (message, stat, patch); REV like `HEAD~1`, `abc123`, `main` | same, filetype git |
| `@turn:N` / `@turn:last` | everything the worker did in turn N (from the trace) | jump to that turn in the trace |

A path anchor is only an anchor if the path exists (or is `@diff…`/`@staged`/`@log…`/`@show:…`/`@turn:…`);
otherwise the text is left alone.

Resolution (`aic anchor <text>` / `aic.anchors`) turns each anchor into a
context block that the backend adds to what it sends:
- **compile / ask**: every anchor in the spec, the question and the context
  excerpt is resolved and inlined for the compiler (numbered lines, bounded
  size). Outside-project content is resolved only from text the human typed
  (spec, question), never from the excerpt (code or chat the worker/compiler
  wrote). Only explicit outside `@dir/` anchors in that text become `--add-dir`,
  and never `/`, the home directory, a parent of the project or a credential path.
- **submit**: line ranges, symbols, diffs, turns and outside-project files are
  (and git log/show) inlined in the pasted body under "Referenced context"; plain in-project files
  and directories are left as paths (the worker can read them itself), and
  outside-project files are named but never pasted.
- **Always**: "outside" is decided by real path (symlinks count where they
  point); credential-like paths (`~/.ssh`, `~/.aws`, `*.pem`, `.env`, …) are
  never read; non-regular and binary files are refused; git runs with
  `core.fsmonitor` off and without external diff/textconv drivers.

`aic anchor --json <text>` prints `[{"anchor","kind","path","abs","line","end","symbol","exists","text"}]`
where `kind` is `file|line|range|symbol|dir|diff|staged|log|show|turn` and `text` is the
resolved content (bounded). Editors use it for `gd` on `@diff…`/`@turn:…`.

## CLI (`aic/bin/aic`, run with cwd anywhere inside the project)

| command | effect | stdout |
|---|---|---|
| `aic init` | create `.aic/`, install hooks into `.claude/settings.local.json` | message |
| `aic sessions [--json]` | list worker sessions (hook-registered + transcripts on disk) | JSON list `[{session_id, updated, state, title, pane, attached}]` |
| `aic attach <sid>` / `aic attach --auto` | pin / unpin worker | message |
| `aic status --json` | `{"root","session_id","dir","worker":{...}\|null,"hold":bool,"compiling":bool,"watching":bool}` | JSON |
| `aic watch` | long-running: tails transcript + human.jsonl → trace.jsonl, touched.json. Follows `current`/auto attach. Exits when stdin closes or on SIGTERM. Writes watch.pid. | log lines |
| `aic docs [--json]` | migrate once (see Docs), list docs | JSON `[{name, path, mtime, compile, compile_template, prompt}]`, newest first |
| `aic compile [--doc D] [--print]` | doc D (default `spec`) + activity since the last compile → compiler Claude → diagnostics.D.json, through D's compile template (see Compiler templates). Busy: queues one more run of D and prints `compile queued: …`. `--print`: the prompt only | summary line |
| `aic ask [--doc D] [--context-file F] [--whatif] [--template T] [--print] <question...>` | ask the chat compiler about doc D (default `spec`; it may not exist yet), through compiler template T (default `ask`; `whatif` with `--whatif`); appends to chat.md | answer markdown |
| `aic fix [--doc D] [--from N --to M] [--content-file F] [--json] [--print]` | the compile session proposes edits to doc D that fix its diagnostics (with a range: only the items about those doc lines). F = the doc's text as the editor has it, unsaved edits included (default: the saved doc). Never edits anything. Errors if there are no diagnostics to fix | `fixes.D.json` content (`--json`), else a readable list |
| `aic prompts [--json]` | list templates (the winning one per name) | JSON `[{name, source, overrides, path, description, header, requires, input, uses_input, marks_sent}]` (`overrides`: the sources it shadows); a broken one has `error` instead |
| `aic prompts --compiler [--json]` | list compiler templates | JSON `[{name, source, overrides, path, builtin?, description, task, model}]` (`builtin`: the built-in of that name; `model`: the one that counts, null for project templates); a broken one has `error` |
| `aic render --prompt P [--doc D] [--from N --to M] [--input TEXT] [--json]` | render template P for doc D: what a submit would send | `{header, body, receipt}` |
| `aic submit [--doc D] [--prompt P] [--from N --to M] [--input TEXT] [--now]` | render **built-in** template P (default `selection` with a range, else `spec`; project/user templates are never sent unseen: render them and use `--rendered`) and send it: type the header + paste the body into the worker pane; queued if busy (absorbed at the next tool call); `--now` = Claude Code send-now (interrupts the running tool). Refuses while a dialog is open. | message |
| `aic submit --rendered FILE [--now]` | send a rendered prompt (`aic render --json`, header/body possibly edited); applies its receipt | message |
| `aic hold` | set hold | message |
| `aic continue [message...]` | clear hold; type `message` (default "continue") into worker pane | message |
| `aic usage [--json]` | per-session context/cost + account quota (see Usage) | text / JSON |
| `aic compact compile\|ask\|worker [--reset]` | summarize-and-restart (or reset) a compiler session; `worker` types `/compact` into the worker pane (no reset) | message |
| `aic anchor [--json] TEXT` | resolve every anchor in TEXT (see Anchors) | blocks / JSON |
| `aic hook` / `aic-hook` | hook entry point (stdin = hook JSON); `init` installs the lighter `bin/aic-hook`. Records the worker (pane, tmux socket, pid, state), enforces holds, and denies the worker's Write/Edit/MultiEdit/NotebookEdit into `.aic/docs/` and `.aic/prompts/` (symlinks resolved), plus Bash commands that write there (see Compiler templates, Trust rule); the turn goes on. Never runs for the compiler (`AIC_ROLE=compiler`, project hooks off) | hook JSON |

Exit code non-zero ⇒ stderr holds a one-line human-readable error.

Context file for `ask` (written by editor, any subset):
```json
{"where": "doc|trace|code|diagnostic|chat", "doc": "name (where=doc)", "file": "rel/path", "line": 12,
 "excerpt": "text around cursor", "event": { <trace event> }, "diagnostic": { ... }}
```
