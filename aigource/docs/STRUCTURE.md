# How the structure view gets its numbers

The spec for aigource's structure screen (`S`). Everything comes from two
places: Claude Code's transcripts of the repo and the repo itself (today's
files, and git for baselines). The transcripts are
`$CLAUDE_CONFIG_DIR/projects/<repo path with / and . as ->/**/*.jsonl`
(`~/.claude` by default), subagents included. Which of them are loaded is the
stage's choice (`--since HOURS`, `--all`, explicit files), so the structure
view agrees with the stage; `--all` gives the whole history.

`src/analysis.c` collects facts change by change (`an_build`); `src/metrics.c`
turns them into the numbers the screen draws (`st_build`). Both run on a worker
thread and produce an immutable snapshot that the screen swaps in. This file
names the function that does the work next to each rule. The port was made
from a Python and HTML draft (`draft/build_data.py` + `draft/structure.tpl.html`);
the draft is now the test oracle (`tests/structure_parity.py` compares
`--structure-dump` with `build_data.py`, `tests/metrics_parity.js` runs the
page's own JavaScript against `--metrics`). A table at the end maps the
draft's names to the C functions.

`aigource --structure-dump` prints the snapshot as JSON (counts go to stderr);
with `--metrics FILE` it also writes what the screen derives from it.

## 0. Foundations

### The tree
The tree is the repo as it is today: `git ls-files -z --cached --others
--exclude-standard` (tracked files, and untracked ones git doesn't ignore),
binary files (a NUL in the first 8000 bytes) and files over 8 MB skipped
(`setup_files`, `list_tree`). It is capped at 5000 files: the files history
changed come first, then the others in path order; `stats.cut` counts the ones
left out. Files that history changed and that are gone from disk are kept as
nodes unless git ignores them (`git check-ignore`), since a temporary file in an
ignored folder isn't part of the repo. Outside a git repo the tree is the files
history touched that still exist, under the same cap. Untouched files are in the
tree and show cold.

A folder whose only child is a folder (and has no files) merges with it into
one node (`a/b/c`), as on the stage; the root never merges (`tmp_build`).

### Git calls
Baselines come from git (`git`, `git_text`): `--literal-pathspecs`,
`core.fsmonitor=` off, `GIT_TERMINAL_PROMPT=0`, `GIT_NO_LAZY_FETCH=1`, stdin
and stderr on `/dev/null`, no shell (paths are argv), and a 10 s timeout after
which the process is killed and the call counts as failed. A file's baseline at
time T is the first commit at or before T's second in `git log HEAD -- FILE`
(HEAD as `git rev-parse` gives it at the start of the build; when that fails,
no file has a baseline in that build), read with `git show COMMIT:./FILE`;
one `git log` per file, one `git show` per commit used, and none of either
when an earlier build already asked (Rebuilds and caching, below).

### The stream
`transcript.c` reads every transcript into events; `ingest.c` merges them in
time order; `an_add` hands them to the analysis. Every assumption about the
transcript format lives in `transcript.c`; analysis never reads JSONL.

| Event | Comes from |
|---|---|
| turn (`EV_TURN`) | a user message that isn't a tool result, meta or a compact summary: typed by the human (`origin` human or none) or pasted (`<pasted_content …>` is dropped); main sessions only. It doesn't start with `<local-command`, `<command-message>`, `[Request interrupted`, `Caveat:` and isn't empty. `PROMPT_*` tells the kind (`prompt_text`) |
| turn copy (`EV_TURN_COPY`) | a prompt a resumed session's file copied from an earlier file (`ingest.c`): the same as a turn for the resumed session |
| interrupt | a user message starting `[Request interrupted` |
| compact | a `system` record with subtype `compact_boundary`, or a record marked `isCompactSummary` |
| read | a Read tool result (`file.startLine`, `file.numLines`), any path; or a **successful** Bash command (command reads, below) |
| search | a Grep or Glob call; or a Bash command segment starting with `grep`, `rg`, `ag`, `fd`, `find` or `git grep` (followed by a space or the end), whether it succeeded or not |
| verify | a Bash command segment matching `VERIFY` (below) that ran in the repo (not after a `cd` out of it), of a call that ran at all (below); ok = the result isn't marked `is_error` and, when a pipe hid the run's status, its output shows no failure (`failed_out`, below) |
| usage | an assistant response's `usage`: output tokens, new input (`input_tokens` + `cache_creation_input_tokens`), cached input (`cache_read_input_tokens`) |
| change | an Edit/MultiEdit/Write result (`structuredPatch`, `originalFile`, or `type: create` with `content`), or one file of a Bash result's `bashEditDiff`; only what the transcript recorded (guessed changes of failed commands and watcher events are left out, `an_add`) |
| title | a session's `ai-title` record, or a subagent's `agentType` and `description` from its `.meta.json` |

**Turn kinds.** Only human and pasted prompts start turns. A slash command
(`/clear`; `PROMPT_COMMAND`) and records Claude Code writes itself (a task
notification; `PROMPT_HARNESS`) don't, so what follows them stays in the turn
before. A resumed session's copies of earlier prompts (`EV_TURN_COPY`) start
turns of that session too: its work before its first new prompt is in its
turn. Subagents' prompts start none; their work belongs to the parent
session's turn (below).

**Command segments** (`cmd_runs` in `cmdguess.c`, `cmd_runs` in
`build_data.py`): the simple commands of a command line as the shell lexer of
command reads sees them (`lex`): quotes, escapes, here-documents (their bodies
are not commands), `$(…)` and backquotes (not split), `(( ))` and comments are
understood, and redirects (`2>&1`, `> f`) are dropped. A segment is a simple
command's words joined by spaces; it is split off at `&&`, `||`, `;`, `&`, `|`,
newlines and `(`/`)`. Each loses a leading `env`, `VAR=value`, `sudo`, `time`
and `timeout [options] N` (any number of them). So `python3 -c "…; make | tail"`
holds no run, and a here-document's lines aren't commands.

```
VERIFY = (?:(?:ba)?sh\s+|python3?\s+|uv\s+run\s+)?
         (?:make|pytest|python3? -m (?:pytest|unittest)|\S*tests?/\S+|\S*/test_\w+\S*|ctest|cargo (?:test|build)|
            npm (?:test|run)|cc|gcc|clang|nvim --headless)(?:\s|$)       — matched at the start of a segment
```
So `(cd x && make)`, `sudo make`, `bash tests/run.sh`, `uv run pytest` count;
`git commit -m "make …"` and `echo make` don't. (The C side compiles the same
pattern as a POSIX extended expression, `CMD_VERIFY_ERE` in `cmdguess.h`.)

**Runs in another tree** (`CMD_AWAY`; `verify` in analysis.c). A run checks the
repo's changes only when it runs in the repo. The lexer follows `cd`, `pushd`
and `popd` (a subshell's `cd` ends with it) from where the command began (the
record's `cwd`, carried as `EV_BASH_END`'s path when it isn't the repo root): a
`VERIFY` segment after a `cd` to a directory outside the repo root (a git
worktree, a scratch copy, `/tmp/…`, `~`), or after one that can't be followed
(`cd $W`, `cd "$(mktemp -d)"`, `cd -`, a bare `cd`), is not a run of this repo:
such a command neither passes nor fails a change, and the turn's earlier run in
the repo decides. Other segments of the same command still count.

**Calls that never ran** (`never_ran` in `transcript.c`, `EV_BASH_END`'s
`not_run`, dumped as `"ran":false`; `never_ran` in `build_data.py`). A Bash
result marked `is_error` whose message (after an optional `Error: `) starts
with one of the texts Claude Code records for a call it didn't run: `The user
doesn't want to proceed with this tool use` / `… to take this action right now`
(rejected), `[Request interrupted by user for tool use]`, `<tool_use_error>`
(Claude Code blocked it), `PreToolUse:` or `Hook PreToolUse` (a hook blocked
it), `This session is isolated in the worktree` (refused); or a result marked
`interrupted` with no output at all. Such a call neither passes nor fails a
change: `verify` skips it. The stage shows it as before.

**Piped runs** (`failed_out` in `transcript.c`, carried on `EV_BASH_END`). A
line's exit status is its last command's: `make test | tail -20` is never
`is_error`, whatever make did. So when a Bash result isn't marked `is_error`
and a `VERIFY` segment of the repo is followed by a single `|` (or ends a
subshell that is: `(cd x && make) 2>&1 | tail`) while pipefail is off, the
status is not the run's own and the output decides. Pipefail is on after a
`set` segment with `-o pipefail` (or a cluster ending in `o`: `set -euo
pipefail`) and off again after `set +o pipefail`; the word elsewhere (a string,
a here-document) changes nothing. The output is scanned for strong failure
signals, and any match makes the event `failed_out`. The output is
`toolUseResult`'s `stdout` and `stderr` (for a record holding one result), else
the result block's text. `err` stays the recorded `is_error`; the analysis
fails a run on `err` or `failed_out` (`an_add`). The signals (`FAILED_OUT_ERE`,
POSIX ERE under `REG_NEWLINE`: `^`/`$` per line; `build_data.py`'s `FAILED_OUT`):
```
^make(\[[0-9]+\])?: \*\*\*                           make's "make: *** [...]", "make[2]: ***"
:[0-9]+:([0-9]+:)? error:                            a compiler error at file:line[:col]
(:[0-9]+:([0-9]+:)? |^[^ :]+: )fatal error:          ... a fatal one, also "cc1: fatal error:"
^collect2: error:                                    the linker
^(--- )?FAIL\b   \bFAILED\b                          test runners (go's "--- FAIL")
Traceback \(most recent call last\):\n  File "[^<]   a traceback whose first frame is a file, not
                                                     <stdin>/<string> (an inline script: usually an edit)
ERROR: (Address|Memory|Thread|UndefinedBehavior)Sanitizer   not LeakSanitizer's exit-time report
:[0-9]+:[0-9]+: runtime error:                       UBSan at file:line:col
(^|[0-9] )Segmentation fault   \(core dumped\)       the shell's crash reports
\b[1-9][0-9]* (failed|errors?)\b                     "2 failed", "1 error" in summaries
\b[1-9][0-9]* failures?\b                            "1 failure(s)"
\bError [0-9]+$                                      make's "Error 2" (not "Error 1 (ignored)")
^[Ee]xit (code|status) [1-9]
^E5(108|113):                                        a Neovim -l script that aborted
```
Weak words are left out on purpose: "0 failed", "no errors", "error" inside a
name, a signal quoted mid-line (source code, a grep of it). Only `VERIFY`
segments matter: in `make && grep FAIL log | head` the run isn't piped, so FAIL
in the output changes nothing. The whole line's output is scanned, so a failure
printed by a later command of the same line counts too.

**Command reads** (`cmd_guess` in `cmdguess.c`, `on_guess` and `emit_guesses`
in `transcript.c`): for a Bash call that succeeded, the shell words of the
command are walked, following `cd`, `pushd` and `popd`. A segment starting with
`cat`, `more`, `less`, `head`, `tail`, `bat`, `batcat`, `nl` or `sed` (not
`sed -i`), or a `<` redirect, reads each operand that is a regular file
existing today (a path needs no extension; `-` and `+cmd` aren't files; words
built at run time (`$VAR`, `$(…)`, globs) are skipped; directories aren't
read). A file outside the repo is read by its absolute path. Which lines:
- `sed` with `-n` (`--quiet`, `--silent`) and a script (the first operand, or
  every `-e`) made only of line prints, `A,Bp`, `Ap` or `A,$p` (`;` or
  newlines between): each print is a read of lines A..B (`5,2p` is line 5) of
  each file (`cmd_inplace`, `sed_ranges`). Anything else in the script, `-f`,
  or no `-n`: the whole file.
- `head`: lines 1..N; `tail`: the last N lines, or from line N with `+N`. N
  comes from `-n N`, `-nN`, `-N`, `--lines N` or `--lines=N` (the last one
  given), 10 without one (`cmd_headtail`). Bytes (`-c`), `head -n -N` (all but
  the last N) or a count that isn't a plain number: the whole file; N = 0
  reads nothing.
- the other readers: the whole file.

A command's reads are **once per file and range**: `sed -n '1,5p;9p' f` is two
reads, `cat f; cat f` one (`on_guess` keys them by file and range).

**Reads outside the repo.** A Read of a path outside the repo is an event with
an absolute path and no node; it counts as a read for the context cost and
shows as `~/…` or the absolute path (`shown_path`). A Grep or Glob outside the
repo, or one that failed, is a plain step that still counts as a search; other
tool calls count for nothing. A read
of something that isn't text (an image, a PDF: `type` other than `text`) has no
lines and is **not counted** at all.

**Read ranges.** A Read result carries `startLine` and `numLines`: those lines
were read; without `numLines` the read goes to the end. A command read has its
range from above (`EV_READ.range`: 0, 0 the whole file; A, 0 from A to the
end). `tail`'s last N lines are stored as 0, −N, since the file's length then
isn't known when the transcript is read: the analysis resolves them against
the text the read is matched with (`replay_read`: the tracked text, or the git
version), so they are that text's last N lines.

**Paths.** A path under the repo maps to its file (`rel_path` in
`transcript.c`); paths outside are kept for display only.

**Duplicates** (`ingest.c`). A resumed session copies earlier records into its
new file. Events are unique by tool-use id (and file for changes, path and line
range for reads), interrupts by timestamp, turns by (prompt time, prompt text);
a copied turn becomes `EV_TURN_COPY` instead of being dropped. The first copy
seen wins; it sets the event's session and agent, and the comments of a
resumed session's records come from that copy as well. Usage is unique per
response id; a response's parts each repeat it with output growing, so the
copy with the most output wins (its time is the first seen).

### Turns
A turn is one prompt, until the next prompt in that session (`turn_of`,
`prompt_at`). A subagent's events belong to the parent session's turn running
at that time. Its title is the prompt's first non-empty line, cut to 140
characters. Its **time** (`secs`) runs from the prompt to the start of the last
response in that turn (any agent of the session); waiting for the next prompt
is not in it. Gaps between responses (a background wait, a long command) count
at most 5 min each (`IDLE_CAP`, `active`), so the time is close to time spent
working. Seconds are rounded half to even.

**Only turns that changed a file exist.** Turns are created when their first
change is replayed; everything else (interrupts, tests, tokens, reads) only
attaches to turns that exist. So the turn strip, the rework gap (5) and turn
counts all count these turns only.

### Comments
The agent's comment on a call is the text it wrote since the last tool result
or typed or pasted prompt, before that `tool_use` (parallel calls share it).
Task notifications, slash commands and interrupts don't end it. When there is
none, the turn's latest text stands in, shown as "earlier in the turn"
(`comment_of`; stored as `-1 - index`). Trimmed to 900 characters. The text is
read back from the transcript (`tr_notes`) for the calls that need it.

### Functions and classes
Found in each version of a file by tree-sitter (`syn_defs` in `syntax.c`,
cached by language and text by `defs_of`):
- The grammar is the one Neovim has installed, as for highlighting. The
  definitions are the captures `@function.outer` and `@class.outer` of
  `nvim-treesitter-textobjects`' `textobjects.scm` for the language, looked up
  next to Neovim's queries (the plugin's own `queries/` first, then the places
  highlight queries come from). Only the patterns that capture those two are
  kept.
- A def's name is its `name` field, or the end of its declarator chain (C:
  `function_definition` → `function_declarator` → `identifier`); an unnamed
  struct takes its typedef's name. Defs without a name are dropped.
- A class (struct, enum, class, ...) is told from a function by its capture.
  Lines run from the first to the last line of the node. Defs nest by
  containment: `Class.method`, `outer.inner`, `Class.method.inner`.
- A language with no grammar or no textobjects query has **no functions**: its
  files are measured at file level only (no function nodes, no function
  numbers). `AIG_NO_DEFS=1` turns functions off everywhere (tests that mustn't
  depend on installed grammars).
- A text with syntax errors is parsed afresh rather than incrementally, since
  error recovery can come out differently.

A file's functions are matched across versions by their keys (names, with their
parents').

### Replaying the files (AI blame)
Each file is rebuilt at every change, and every line carries the id of the
change that wrote it (`replay_change`, `resync`):

1. **Edit/Write with `originalFile`**: line up the tracked version with
   `originalFile` (below), then apply the patch (`an_apply`): kept lines keep
   their origin, `+` lines get this change. Equal lines keep their origin;
   other lines get the last change that couldn't be placed, else "unknown".
2. **Create**: every line gets this change.
3. **Command (`bashEditDiff`), or an Edit without `originalFile`**, in order:
   - apply the hunks to the tracked version, if every context and removed line
     matches;
   - else to the file as of the last git commit before the change (`git_text`),
     after lining the tracked version up with it, so origins carry over;
   - else find each hunk's new side in the first later known text of the file
     that holds all of them (`later_text`, `an_relocate`): the before and after
     texts of later Edit/Write results, created files, and today's file; each
     hunk goes to the match nearest its `newStart`. This maps the change to
     functions but places no lines, and the file is **unplaced**: the next
     line-up (step 1, the git case, or the end) gives the lines it can't match
     to this change; a create just clears the mark. While unplaced, command
     changes skip the tracked text and reads use the git version;
   - else the change counts for the file only (`+/−`, no functions, no trend
     point), and the file is unplaced as well.
4. **Recorded twice**: Claude Code also credits an edit made while a command
   ran to that command. A command change whose hunks' new side already sits at
   their `newStart` lines in the tracked file is dropped (`an_has_new`).
5. At the end every file is lined up with today's file, so each of today's
   lines has an origin or "unknown" (written before these transcripts).

**Lining up** (`line_map`, `an_line_map`). Texts are split on `\n`. Up to
10^7 pairs of lines (`DIFFLIB_MAX`, lines of one side times lines of the
other) the alignment is what Python's `difflib.SequenceMatcher(None, a, b,
autojunk=False)` gives: the longest matching block first (earliest in `a`, then
in `b`), then recursively either side of it; origins therefore carry over as
the oracle's do. Above that, difflib's cost grows with repeated lines, so the
common prefix and suffix are matched first and the rest is anchored on lines
that occur exactly once on each side (patience: the longest increasing run of
them), the gaps in between recursively, and difflib again in gaps small enough.
A gap with no unique line and too big for difflib stays unmatched, so on such
files the lines it can't place become "unknown" or the unplaced change's.

The functions a change touched are those holding its `+` lines (in the new
version) or `-` lines (in the old one); for a create, all of them, and all its
lines count as added. A change with an empty patch adds nothing.

A file's length is its number of lines, one final newline ending the last line
rather than starting another (`nlines`); blame is trimmed to it.

Texts are not newline-translated (the oracle reads files with Python's
universal newlines), so a CRLF file differs from the oracle's.

The replay runs twice: first in time order for texts, origins, turns and costs
(nothing there needs a file's functions), then per file on worker threads for
everything that does (functions touched, changed unread, rework and trend per
function; `work_all`), each file's versions parsed incrementally.

`stats` in the snapshot counts what had to fall back: duplicate events dropped
(`dups`), changes with no text before them, placed from git, placed in a later
text, at file level only, and changed unread.

### Rebuilds and caching
Live rebuilds (3 s after new events, at most every 15 s) run the whole build
again on everything added so far: the replay is not incremental, so nothing it
derives can go stale. What a build reads and computes along the way goes into
caches on the `Analysis`, kept in memory only, each entry under a key naming
everything its value was computed from:

| Cache | Key | Value |
|---|---|---|
| functions (`defs_of`) | language, `hash64` of the text, its length | the defs of that text |
| `git show` (`git_show`) | commit, path | the file at that commit (failures aren't kept) |
| `git log` (`git_log`) | HEAD commit, path | the file's commits as of that HEAD |
| change records (`change_of`) | transcript, offset, tool id, path | `tr_change`'s record, once it has its result |
| comments (`notes_of`) | transcript, offset, count, tool id | `tr_notes`' text blocks, once all are written |
| commands (`runs`) | search or verify, where it started, the command | `cmd_runs`' bits |

None of these values can change under its key, so nothing is ever
invalidated: a commit's content is fixed; so is the history behind a HEAD
commit, which is why the log is asked as of the commit `git rev-parse HEAD`
gives (one call per build), and a new commit, checkout or reset simply makes
new keys; transcripts are only appended to, and a record or comment is kept
only once complete; the same text in the same language parses to the same
functions, whichever version of the file was parsed before it (an incremental
parse gives a fresh one's defs; `test_syntax` checks that over random edits of
real files); `cmd_runs` depends on its strings alone. Two things are assumed:
no 64-bit hash collision between two texts of the same length and language,
and that the history behind a commit doesn't change (a shallow clone deepened,
or replace refs, at the same HEAD, would not be noticed).

Today's files are read again and `git ls-files`, `git check-ignore` and the
replay itself run on every build: they cost little (about 15 ms and 70 ms on
this repo) and are where changes show up. Each entry remembers the last build
that used it; after a build the rest are dropped, so the caches hold about one
build's working set. The largest part is the change records, which hold each
change's text before it; a build uses those texts by reference, as it uses
`git show`'s. A cancelled build drops nothing and adds only whole entries; the
next finished build sweeps. The worker threads share the functions cache under
a lock; the others are used by the build's own thread, and a cached text is
only ever used for its own file, which one worker handles.

`AIG_DEBUG=1` prints each build's phases, git calls, texts parsed and cache
sizes to stderr; `--structure-dump --rebuild N` builds N more times in one
process (new events polled first, as live), which times a warm rebuild. On
this repo's whole history (18.9k events, 1394 changes) a first build takes
about 2.2 s and a rebuild with nothing new about 0.11 s; one new edit of
`analysis.c` about 0.13 s (one new text parsed; the file changing on disk too
parses a second).

### Heat
Every heat mode is a metric per node, on a log scale against the busiest node
of the same level (folders, files, functions/classes):
`log(1 + value) / log(1 + max)`, with max at least 1 (`st_build`, METRIC / MAX
/ heat). Colors run dark blue → amber; folders are drawn darker.

| Mode | Value |
|---|---|
| turns | turns that changed it |
| lines | lines added + lines removed |
| risk | turns × complexity (2) |
| rework | reworked lines (count, not the rate; 5) |
| fixes | fix turns (7) |
| context | average reads + searches per turn (8) |
| cost | output tokens (10) |
| time | agent time on its changes (10) |
| age | already 0–1, newest = 1 (12) |
| owner | not a scale: one color per author (11) |

## 1. AI blame
Each line of today's code → the change that wrote it → its turn, tool, agent,
time and comment. The code view colors a strip per line by turn (newest turn
first in the palette, 8 colors repeating); the legend counts lines per turn
(`st_blame_slot`, `st_line_origins`). Unknown lines are grey.

## 2. Hotspots × complexity
**Complexity**, on today's code (`measure` in `syntax.c`; `SynDef.cx` and
`.depth`): `1 +` the number of branch nodes inside the definition, nested
definitions included. The branch nodes, by tree-sitter node type across
grammars (`BRANCHES`): if, else-if and elif, unless, for and foreach loops,
while, until, do, repeat, case and switch labels, match arms, catch, except and
rescue clauses, conditional and ternary expressions, comprehension `for` and
`if` clauses. A binary or boolean expression counts one more for a `&&`, `||`,
`and` or `or` operator in it, so `a && b && c` adds two, as counting the
operators does.
- **Depth** = the deepest nesting of block nodes (`BLOCKS`: compound
  statements, blocks, class and declaration bodies, ...) below the
  definition's own body (the body itself is 0).
- A class or a function with nested functions is measured over its whole body:
  its branches and those of everything inside it.
- A file's complexity is the sum of its top-level definitions'; a folder's, the
  sum of its files'.
- A file with no functions (no query for its language, or `AIG_NO_DEFS=1`) has
  complexity 0, so its risk is 0.

**Risk** = turns × complexity. The badge turns amber from complexity 15.

## 3. Change coupling
For a file or function changed in at least 2 turns: every other node of the
same level (file↔file, function/class↔function/class, never its own ancestors
or descendants) that changed in **2 or more of the same turns**. Sorted by
shared turns, then by `shared / min(turns of both)`; top 8 (`ST_TOP_ROWS`;
partners in `st_build`). Each partner shows `shared/turns (P% match)`, where
turns are the inspected node's and P = shared / turns, rounded.
"Other folder" / "other file" when the partner sits in another folder (files)
or file (functions). Arcs go to the partner, or dashed to the nearest node on
screen that holds it.

## 4. Friction and verification flags
- **Changed unread** (per change and function; "Blind" comment): the function
  existed before the change and, in this agent's context (main session or one
  subagent) since its last compaction, no read covered any of its lines and
  nothing wrote it. A read finds its functions in the tracked text, or the git
  version when the file isn't tracked yet or is unplaced; a read with neither
  is ignored. A read covers the lines of its range (a command read: all of
  them). Creates, and changes placed in a later text (step 3), are never
  "changed unread": there's no text before them.
- **Untested / failing** (per change; "Verification" comment): verify events
  later in the same turn, by any agent of that session (subagents included)
  (`verify`). None → untested; the last one failed → failing. A run failed
  when its result is `is_error`, or when a pipe hid its status and its output
  shows a failure (`failed_out`, section 0, Piped runs). Runs in another tree
  and calls that never ran (rejected, blocked) are ignored (section 0). A subagent's changes take the parent turn's last
  run, which may be the parent's, after the subagent finished.
- **Corrected** (per turn): the next prompt in that session, on its first line
  (≤ 140 chars), case-insensitive, matches
  ```
  \b(?:bugs?|broken|wrong|doesn'?t work|does not work|not working|didn'?t work|revert|undo|regress\w*|crash\w*|
     fail(?:s|ed|ing)?|errors?|strange|weird|horrible|not what i|why did you)\b|^no\b
  ```
- **Interrupted** (per turn): an interrupt event during it (`finish_turns`).

- **Slow** (per file; `ST_SLOW_X`, 2): its agent time (10) per turn that changed
  it is at least 2× the average of that over all files with turns. A folder
  counts its slow files. Functions get no flag: their time is a share by lines
  (10), too rough to call one slow. A **slow turn** took at least 2× the average
  turn time (see Turns).

A node's dots: coral = corrected or interrupted turns, yellow = untested or
failing changes, violet = changed-unread changes, cyan = slow.

## 5. Rework rate
While replaying, every removed line reports the change that wrote it. Each
(writing change, removing change) pair is counted for the file and for the
function the line was in ("Rework" comment). A pair is **rework** when the
removing turn is 0–2 turns after the writing turn (`ST_REWORK_TURNS`), counting
turns (that exist, see Turns) in time order across all sessions. Rework rate =
reworked lines / lines added there (capped at 100%); the heat mode uses the
count. "Same turn" = gap 0.

## 6. Complexity trend
After every change that touched a function: its length and complexity in the
new version ("Trend" comment); for a file, its length. Creates add no function
points (only the file's length); points from a later text a change was lined
up with (step 3) are skipped, since that's not the file right after it. If
today's values differ from the last point, a "now" point is added.
**Growing**, first point vs last (including "now"): lines ≥ 1.5× and +20 or
more, or, for functions, complexity +8 or more.

## 7. Fix hotspots
A **fix turn** is a turn whose own prompt's first line (≤ 140 chars),
case-insensitive, matches
```
\b(?:bugs?|broken|doesn'?t work|does not work|not working|didn'?t work|crash\w*|regress\w*|fail(?:s|ed|ing)|errors?|
   wrong|can'?t|cannot|still|isn'?t|not (?:showing|working|catching)|horrible)\b
```
Fixes for a node = its fix turns (and of how many).

## 8. Context cost
Per change: everything the same agent read and searched **in this turn before
it** ("Context cost" comment): reads, searches, and the 8 most-read paths
(`reads_of`). A node's cost in a turn is that of its first change there, so it
measures the context the agent built before touching it. Shown: the average of
reads + searches over the node's turns; "read to change it" = each path's
highest read count over those first changes. A read is one Read result or one
file and line range of a command (once each per command, section 0); a read of
something that isn't text isn't counted.

## 9. Prompt-to-code traceability
For a turn (`st_build`, TINFO; `StTurn`): the changes in it; the files and
functions they touched with `+/−`; tests after them; what was read (as in 8);
the agent's comments in order; its time next to the average turn. **Survives
today** = today's lines whose origin is a change of this turn / lines the turn
added. Its cost: every response in the turn (any agent), and the share that led
up to its changes (10).

## 10. Cost per area
Per change ("Cost" comment): the usage of this agent's responses in this turn
since its previous change (or since the turn began), and the time since then
(or since its first such response), with each gap between those responses
capped at 5 min as for turns. When one call changes several files, the
first file gets it. A node's cost sums its distinct changes: output tokens (the
cost heat), new input, cached input, agent time (the time heat). A function gets
each change's share that its changed lines (added + removed) are of the file's
in that change (lineShare, `cid_lines` in `metrics.c`; 1 when the file has none
for it, which the analysis never produces), so one call writing 30 functions
doesn't count 30 times; nested functions count inside their class too.

**Turn strip:** bar height = lines changed (`5 + 17·sqrt(lines / lines of the
largest turn)`, `bar_h`, the draft's 22 px scale, drawn on 48 px: `BARS_H` in
`sview.c`); bar width = turn time, `0.4 + sqrt(secs / average turn)` (`bar_w`),
so one long turn can't crowd out the rest. The bars are in time order; `[` and
`]` step through them.

Widths (`bar_spans`): flex-grow by `bar_w` with a 4 px minimum and 2 px gaps;
when the turns don't fit, a 2 px, then 1 px minimum with 1 px gaps; past that
(more than about half the strip's width in turns) bins of consecutive turns,
2 px each, drawn as their tallest with all their tags, a click picking the turn
at the mouse's place in the bin. Positions are snapped to whole pixels so every
gap shows. Drawing and hit-testing read the same spans. (Before, a crowded
strip dropped the gap to 0 and gave every bar the same `width / turns`: one
solid block of touching same-coloured bars, the clicks still landing on turns.)

Colours (`ui_turn_bar` in `ui.c`, `UT_*` in `ui.h`), so one bar shows every
tag it has:
- **fill**, the most serious of: corrected or interrupted (coral, the flag's
  colour: the next prompt reads like a correction (7), or you interrupted it) >
  fix (pink: its prompt reports a problem; coral in the draft, moved so the two
  stay apart) > plain (`#3b5a7a`). The selected turn is amber, outlined.
- **base stripe** (2 px under a dark line): failing (red: the last build/test
  after one of its changes failed) > untested (yellow: a change with no
  build/test after it in the turn) (6).
- **top edge**: slow (cyan, at least 2× the average turn).
Hover brightens a bar and shows its time, duration, +/−, tags and prompt; the
legend sits under the selected turn's title.

**On the stage** the timeline (`render.c`) draws the same bars where each turn
ran on its own axis (gap-compressed, `tl_to_v`): from the prompt to its last
response, a pixel short so neighbours keep a gap, with a dark line at each
bar's left so a turn of another session overlapping it stays apart. The stage
doesn't build the analysis: `turns.c` keeps light per-turn stats from the
events it already gets, by the same rules (`an_fix_prompt`, `an_corr_prompt`,
`an_verify_cmd`, `an_active` / `AN_IDLE_CAP` in `analysis.c`, `ST_SLOW_X`): a
typed or pasted prompt of a main session until its next one, a subagent's work
in its session's turn, only turns that changed a file. Without the replay or
git, +/− are the events' own counts, and a change the analysis drops (a
duplicate, outside today's tree) still counts, so the two can differ slightly.
Clicking a bar seeks to its turn's start (dragging from it scrubs as anywhere
on the timeline).
Responses after a turn's last change (checking, summing up) belong to no change;
the turn view shows them in the turn's total.

## 11. Ownership
Each change belongs to an agent (`setup_actors`, `actor_out`), one per
transcript file (so each resumed session file is its own agent):
- a main session, named by its AI title (the last `ai-title` record), or
  "session <first 8 characters of its id>" without one;
- a subagent, named by the `agentType` (after its last `:`) and `description`
  in its `.meta.json`.

Each of today's lines belongs to the agent of the change that wrote it (1).
Per node: lines per agent; the **owner** is the known agent with most lines
(lines from before the transcripts don't count; a tie goes to the agent seen
first). Owner mode colors each node by its owner: 8 colors (`ST_ACTOR_COLORS`),
by lines in the whole repo, repeating after 8.

## 12. Code age
Each of today's lines is as old as the change that wrote it, counted back from
the last change in the data (not the clock, so a screen reads the same later).
Per node: the median age (the upper median) of its lines that have an origin,
and their spread over < 1 hour, < 1 day, < 1 week, older, and before these
transcripts. Age heat = `1 − log(1 + median minutes) / log(1 + span minutes)`,
span = first to last change, at least an hour; newest is hottest, and a node
with no known lines gets 0.

## Where each number lives

| Draft (`build_data.py` / page) | C |
|---|---|
| `defs_c`, `defs_py`, `defs` | `syn_defs` (syntax.c), `defs_of` (analysis.c) |
| `complexity` | `measure` (syntax.c); the file's sum in `build_tree` |
| `apply` | `an_apply`, `replay_change` |
| `has_new` | `an_has_new` |
| `relocate`, `later_text` | `an_relocate`, `later_text` |
| `git_text` | `git_text`, `git` |
| `resync`, difflib | `resync`, `line_map` / `an_line_map` |
| `nlines` | `nlines` |
| `prompt_of` | `prompt_text` (transcript.c), `setup_prompts` |
| `rel_of`, `shown` | `rel_path` (transcript.c), `shown_path` |
| `cmd_reads`, `sed_lines`, `sed_ranges`, `head_tail_lines` | `cmd_guess`, `cmd_inplace`, `sed_ranges`, `cmd_headtail` (cmdguess.c), `on_guess`, `emit_guesses` (transcript.c) |
| `lex_segments`, `read_word`, `skip_parens`, `cmd_runs`, `VERIFY`, `SEARCH` | `lex`, `read_word`, `skip_parens`, `cmd_runs`, `unwrap` (cmdguess.c), `CMD_VERIFY_ERE` (cmdguess.h), regexes in `an_new` |
| `FAILED_OUT`, `failed_out` (`cmd_runs` & `PIPED`) | `FAILED_OUT_ERE`, `failed_out` (transcript.c), `CMD_PIPED` |
| verify runs in another tree (`cmd_runs` & `RUN`) | `verify` (analysis.c), `CMD_RUN` / `CMD_AWAY` |
| `never_ran` | `never_ran` (transcript.c), `not_run` on `EV_BASH_END`, skipped in `verify` |
| `FIX`, `CORRECTION` | regexes in `an_new`; used in `turn_of` |
| `push` and its duplicate rules | `ingest.c`, `an_add` |
| `actor_of` | `setup_actors`, `actor_out` |
| `turn_of` | `turn_of`, `prompt_at` |
| `comment_of` | `comment_of`, `cut_comment` |
| `active`, `IDLE_CAP` | `an_active`, `AN_IDLE_CAP` (shared with `turns.c`) |
| verify flags | `verify` |
| turn time, tokens, interrupts | `finish_turns` |
| tree (`FILES`, folder merge) | `setup_files`, `list_tree`, `build_tree` |
| page: `prep` | `tmp_build`, `st_build` |
| page: per node loop, `METRIC`, `MAX`, `heat`, `SLOW_X` | `st_build` (metrics.c) |
| page: `partners` | `st_build`, `ST_TOP_ROWS` |
| page: `TINFO`, `inspectTurn` | `st_build`, `StTurn` |
| page: `turnBar` | `bar_h`, `bar_w` in `st_build`; `bar_spans`, `ui_turn_bar` |
| page: `readsOf` | `reads_of` |
| page: `blameOf` | `st_blame_slot`, `st_line_origins` |
| page: `lineShare` | `cid_lines` in `st_build` |

## Known limits

What the port fixed, from the draft's list:
- Resumed sessions' copies are counted once, and their prompts start turns of
  the resumed session (`EV_TURN_COPY`).
- Functions and classes come from tree-sitter, no longer from a regex for C and
  indentation for Python, so any language with a textobjects query has them.

What remains:
- A run piped into another command (`make | tail`, no pipefail) is judged by
  its output: only the signals listed (section 0, Piped runs) fail it, so a
  failure that prints none of them passes. What the output can't tell:
  - a check that breaks a file on purpose, runs the tests expecting FAIL and
    restores the file (a mutation check) reads as a failing run;
  - another command's output in the same call counts: `make | tail; head
    old-asan.log` fails on the old log's report;
  - `run > log; tail log`, `make; echo done` and `make || true` lose the status
    through `;`, newlines or `||` and are not checked (104 such runs on this
    repo's history, about 6 of them real failures): out of scope;
  - a sanitizer report that doesn't stop the run (`halt_on_error=0`, a report
    in a child process) is only caught by its `ERROR:` line.
- A subagent's changes take the parent turn's last run: a run the parent made
  after the subagent finished decides them too.
- A run after a `cd` that can't be followed (`$VAR`, `$(…)`) counts as one in
  another tree, even when the variable points into the repo.
- Test runs are recognized by how a command starts: wrappers other than those
  listed (e.g. `nix develop -c make`) and project scripts with other names are
  missed.
- Fixes and corrections are keyword matches on a prompt's first line: strict
  (few false hits), so polite corrections are missed.
- "2 turns later" counts turns of all sessions in time order; parallel
  sessions interleave.
- Command reads count only files that exist today. Only `sed -n` scripts of
  line prints, `head` and `tail` give line ranges; other sed scripts (`/re/p`,
  `1,10d`) and the other readers read the whole file. `sed -n 1,5p a b` takes
  the range in each file (sed counts lines across both unless `-s`). A
  `tail`'s lines are the last N of the text known when it is replayed, which
  may not be the file as it was then.
- Sessions no longer on disk leave lines "unknown" and their turns uncounted.
- Tokens per change only cover the agent's own responses since its previous
  change; work done before an earlier change in the turn counts there.
- Times are timestamps of response starts with gaps capped at 5 min: a long
  build counts as 5 min, and a pause under 5 min counts in full.
- Command edits need a baseline (tracked text, git, or a later text); when none
  fits the change counts for the file only (`stats.file_level_only`).
- `bashEditDiff` also credits edits made by other agents while the command ran;
  they are dropped only when the tracked file already has the change.
- Past 10^7 line pairs the alignment is patience-anchored, not difflib's, and
  can leave lines unmatched (see Lining up).
- Function numbers need Neovim's grammars and `nvim-treesitter-textobjects`
  queries; without them for a language the file has file-level numbers only,
  and function-level numbers differ from the draft's (a different complexity
  count and different definition boundaries), so the parity test reports them
  without enforcing them.
- The tree is capped at 5000 files; the rest is left out (`stats.cut`).
- A CRLF file isn't newline-translated.

Statuses of the caveats noted while building the draft:

| Caveat | Status |
|---|---|
| A build piped into `tail` hides the exit code, so verification overcounts passes | fixed: a `VERIFY` segment of the repo piped with pipefail off is judged by anchored failure signals in its output (`failed_out`); runs in another tree are ignored; segments come from the shell lexer. Still a limit: the signals are a fixed list, mutation checks and other commands' output in the same call read as failures, `;` and `||` aren't checked, a segment is matched by how it starts |
| Corrections and fixes are keywords on the first line of a prompt | still a limit: same patterns, deliberately strict |
| "2 turns later" interleaves parallel sessions | still a limit |
| Command (Bash) edits need a baseline; missing sessions leave lines unknown | still a limit: same fallbacks (tracked text, git, later text, file level); `stats` counts how often each was used |
| `bashEditDiff` credits concurrent edits to the running command | still a limit: dropped when the tracked file already has the change (`an_has_new`), as in the draft |
| Resumed sessions copy earlier records | fixed in the port: dedupe by tool-use id (and file or range) in `ingest.c`, turns by (time, text) kept as `EV_TURN_COPY` for the resumed session |
| Time uses response starts with each gap capped at 5 min; the slow flag is file-level | changed only in where it runs (`active`, `ST_SLOW_X`); the rule is the same and stays a limit |
| C defs by regex, Python by indentation; use tree-sitter textobjects | fixed in the port (`syn_defs`); new limit: a language without the grammar or query has no functions, and function numbers differ from the draft's |
| Command reads: `sed`/`head` ranges (draft) | ranges in both now, the same rule: `sed -n` line prints, `head`, `tail` (last N resolved against the replayed text); the draft gained `tail`. The port also reads `less`, `more`, `tac`, `<` and extension-less names, and lexes the shell (quotes, here-documents), where the draft's pattern splits inside quotes and counts `cat > f`: `structure_parity.py` allows 4 + 10% reads per change for that |
