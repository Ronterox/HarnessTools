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
  sessions/<sid>/
    spec.md               human's pseudocode outline (editor writes; `:w` triggers compile)
    spec.submitted.md     snapshot of spec.md at last full submit
    trace.jsonl           normalized events (written ONLY by `aic watch`)
    human.jsonl           human code-save events (appended ONLY by the editor)
    seen.json             {"seen": ["<event id>", ...]}  (written by editor)
    touched.json          per-file touched line ranges (written by `aic watch`)
    diagnostics.json      last compile result (written by `aic compile`)
    chat.md               compiler REPL transcript (written by `aic ask`)
    hold                  exists ⇒ worker is to be held at its next tool call (one-shot; the gate deletes it)
    control.jsonl         submit/hold/release/held/hold-expired events (appended by backend + hook)
    compiling             exists ⇒ a compile is running (contains pid)
    state.json            compile/ask/submit bookkeeping (backend-private; updated under state.json.lock)
    usage.json            context/cost per session (see Usage; backend-written)
    watch.json            watcher resume point: offsets, seq, parser state (backend-private)
```

## workers/<sid>.json

```json
{"session_id": "...", "transcript_path": "/abs/...jsonl", "cwd": "/abs",
 "pane": "%12" | null, "pid": 1234, "tty": "/dev/pts/3",
 "state": "busy" | "idle" | "held" | "asking" | "ended",
 "last_event": "PreToolUse", "updated": 1760000000.12, "prompted": 1760000000.0}
```
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
| `tool` | `via: "Bash"` + `id "<bash id>:<file>"` when the change was found by diffing the tree after a worker Bash call; `tool` (name), `title` (one line, e.g. `Edit src/app.js:14-22`), `why` (assistant visible text preceding the call, may be `""`), `file` (rel path or null), `hunks` (see below, may be `[]`), `input` (short string summary of tool input), `error` (bool) |
| `text` | `text` — assistant visible text that was NOT followed by a tool call (end-of-turn message) |
| `human` | `file`, `hunks` — a code save by the human |
| `submit` | `text` — what aic delivered to the worker |
| `hold` / `release` | — |
| `held` | `tool` — the gate stopped the worker before this tool |
| `hold-expired` | the turn ended before any tool call; the hold was dropped |
| `compact` | the worker's context was compacted: `trigger` (manual/auto), `pre`, `post` tokens |

Hunk: `{"start": <new-file first line, 1-based>, "count": <new line count>, "lines": ["+added", "-removed", " context"]}`.

## touched.json

```json
{"src/app.js": [{"start": 14, "end": 22, "event": "e17", "actor": "worker"}, ...]}
```
Ranges are in **current** file coordinates (later edits shift earlier ranges).

## diagnostics.json

```json
{"compiled_at": 1760000000.0, "upto_seq": 42, "summary": "one paragraph",
 "items": [{"kind": "mismatch|incoherent|model|unspecified",
            "severity": "error|warning|info",
            "file": "spec" | "<rel path>", "line": 12,
            "spec_line": 12 | null, "message": "..."}]}
```
`file == "spec"` means the line refers to `spec.md`.
On compile failure: `{"error": "message", "compiled_at": ...}`.

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

Anchors work in the spec, in chat, and in ask questions. The grammar below is
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
| `aic compile` | spec + events since last compile → compiler Claude → diagnostics.json | summary line |
| `aic ask [--context-file F] [--whatif] <question...>` | ask the chat compiler; appends to chat.md | answer markdown |
| `aic submit [--from N --to M] [--now]` | type the request + paste the spec diff (or lines N..M) into the worker pane; queued if busy (absorbed at the next tool call); `--now` = Claude Code send-now (interrupts the running tool). Refuses while a dialog is open. | message |
| `aic hold` | set hold | message |
| `aic continue [message...]` | clear hold; type `message` (default "continue") into worker pane | message |
| `aic usage [--json]` | per-session context/cost + account quota (see Usage) | text / JSON |
| `aic compact compile\|ask\|worker [--reset]` | summarize-and-restart (or reset) a compiler session; `worker` types `/compact` into the worker pane (no reset) | message |
| `aic anchor [--json] TEXT` | resolve every anchor in TEXT (see Anchors) | blocks / JSON |
| `aic hook` / `aic-hook` | hook entry point (stdin = hook JSON); `init` installs the lighter `bin/aic-hook` | hook JSON |

Exit code non-zero ⇒ stderr holds a one-line human-readable error.

Context file for `ask` (written by editor, any subset):
```json
{"where": "spec|trace|code|diagnostic|chat", "file": "rel/path", "line": 12,
 "excerpt": "text around cursor", "event": { <trace event> }, "diagnostic": { ... }}
```
