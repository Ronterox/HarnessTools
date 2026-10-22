# aigource — gource for coding agents

Watch what Claude Code sessions do to a repo, live: which files they read,
edit, create and delete, how big each change is, and how the folder tree grows.
One native binary (C + raylib + tree-sitter), starts instantly, no browser.

```sh
make                       # fetches pinned raylib + yyjson into vendor/, builds build/aigource
cd your-repo && /path/to/aigource/build/aigource
```

It replays the history of the sessions active in the last 2 hours (main
sessions and their subagents, each one an actor), then follows them live.
Sessions launched in a subdirectory of the repo are included.

## What you see

| | |
|---|---|
| file | size = length in lines; color = what last happened to it (edit, create, delete, changed by a command; dim blue = only read), brighter the more often it changed |
| folder | the average size of what's in it (files and folders), its files around it; folders fan out from the repo root, each subtree in a slice of its own, so once settled no two folder lines cross; a chain of folders with no files of their own shows as one node (`examples/java/src`) until a file lands in the middle |
| beam actor → file | the action, colored: read, search, create, edit, delete, changed by a command; thickness = diff size |
| rings | diff pulse: green = lines added, red = lines removed; shape = how big next to the file (below) |
| name | each file touched shows its name and last change (+added −removed) for 4 seconds, at any replay speed (after a seek, the files touched just before) |
| bubble | the command it's running, or the pattern it's searching |
| HUD | total lines written / removed, file and command counts |
| panel | active actors with their lines and current turn's prompt |
| legend | right side; `H` toggles it |

Actors dim after 2 minutes idle and leave the stage after 10.

**How big a change is** is its lines (added and removed each get their ring)
over the file's length before it, or over the average file when that's bigger:
a new file or a small one is weighed against a usual file, not itself. The
average is over the files seen so far and not deleted, each counted up to 4000
lines, with five 200-line files mixed in so the first few don't set it alone.
A delete with no count (an `rm` from a command) removed the whole file. Under 0.15 is
**minor**: one thin dim ripple (10 lines of a 1000-line file). Up to a whole
file is **notable**: two rings in full color, one trailing. A file's worth and
more is **major**: a shockwave with a white-hot core, rays and a lingering glow
(a new 500-line file where files run 200).

**Click an actor** (in the graph or the panel) to open its current turn: the
prompt, then one line per step (read, edit, create, delete, search, `$`
command, any other tool or MCP call, with +/−). The files that turn touched get a soft area per folder in
the actor's color, everything else dims, and the camera frames them. Hover a
step to ring its file. It follows the actor: when its next turn starts (live,
replaying, or after you seek), the window moves to it. `✕`,
`Esc` or a click outside closes it.

**Click a step** to see it in full, beside the turn window: an edit's diff with
line numbers, a created file's contents, what a read returned, a command with
its output (stderr in red; for a file a command changed, that file's diff
first), a search's parameters and matches, any other tool's input and result.
It's read back from the transcript on click, so nothing extra stays in memory;
a step still running shows so and fills in when its result lands. Wheel or
`PgUp`/`PgDn` scroll, `Ctrl+C` copies it, `✕`/`Esc`/click outside close it
(one window at a time). **Click a line of code** in it (a read, a created
file, a diff's kept and added lines; a removed line goes to where it was on
the new side) to open the file at that line in your Neovim (below).

Code in it (reads, created files, diffs) is syntax highlighted with
tree-sitter, using the grammars and highlight queries Neovim has installed
(nvim-treesitter's, or Neovim's own); diffs keep their green/red rows and
`+`/`−` marks, each side highlighted as its own text. A grammar loads the first
time its language is shown, and a detail is parsed when its code is first
drawn: whole, or a 256 KB window around the visible rows for big files. A
fragment that starts inside a code fence or block comment is parsed as if it
were opened. Without a grammar, keyword lists stand in (C, Python, shell,
JS/TS, Go, Rust, Lua; Markdown headings). `AIG_DEBUG=1` says which grammar
loaded, or why none did.

Elsewhere what stands out is bold, in its own color: numbers (peach), dates
and times (sky), and in mauve each program of a command (flags and comments
dim, heredoc bodies plain), warnings, and any word listed in
`~/.config/aigource/keywords`; how a command's output went is red
(error/failed) or green (ok/passed).

**Comments.** What the agent wrote just before a step (its text since the last
tool result) is that step's comment. Opening a step shows them in a panel left
of it, one message box each, oldest first; parallel calls share the text that
led to them. Steps with comments carry a bubble in the turn window, and
hovering one previews its comments beside the row. Wheel scrolls the panel,
`Ctrl+C` over it copies them. Read back from the transcript like the detail.

**Failed commands.** Claude Code records what a command changed only when it
succeeds. For a failed one aigource guesses the files from the command
(`sed -i`, `perl -i`, `>`/`>>`, `tee`, `touch`, `truncate`, `rm`, `mv`, `cp`,
following `cd`) and lists them as "guessed". With nothing to guess, the command
is shown in red as failed. With `--watch`, a guessed change the watcher sees
becomes a real edit (and a guess it doesn't see is dropped); changes made while
a failed command ran go to its agent, not to you.

**Click a file** in the graph to see its last change the same way: the edit's
diff, the created file, or the command that changed it (its last read when it
was only read).

**Hover** a file for what last happened to it ("Edit · src/a.py", size, the
session's +/− on it, who touched it and when), a folder for its counts, an
actor for its turn, prompt and totals.

**Timeline** (bottom), like a video player: click or drag anywhere to jump,
hover to preview the time and the turn running then. On top, one bar per turn
that changed files, where it ran (prompt to last response): height = lines
changed, colours as the structure screen's turn strip (below); hover one for
its time, duration, prompt, +/− and tags, click it to jump to its start. Under
it the activity strip (amber = writes, blue = reads); on the bar, ticks are
prompts in the actor's color, dark notches are idle stretches squeezed to 2
minutes so multi-day histories stay usable. Jumping back rebuilds the scene from history and keeps
the layout where it was. "go live →" (or `L`) returns to now, playing (a pause ends).

## Structure view

`S` switches between the stage and the **structure screen**: the repo as it is
today (folders, files, and their functions and classes), coloured by what the
loaded history did to it. The history is what aigource loaded (`--since`,
`--all`, explicit files), so it agrees with the stage. The first `S` builds it
on a worker thread (a few seconds on a big history; the stage keeps running).
It opens on Floating nodes, heat by turns.

Files come from `git ls-files` (plus untracked files git doesn't ignore), at
most 5000 with the ones history touched first. Functions and classes are the
`@function.outer` / `@class.outer` captures of `nvim-treesitter-textobjects`'
queries, found next to the highlight queries; a language without them has file
numbers only. `AIG_NO_DEFS=1` turns functions off.

**Live:** while it's open, new events rebuild it about 3 s after the last one
(or after 15 s while they keep coming), one build at a time; the screen stays
as it is, with "updating…" in the bar, and keeps the folder, the inspected node
or turn, the selected turn, view, mode and scroll. While it's closed nothing is
built; opening it rebuilds if events arrived. `R` rebuilds now instead of
waiting, dropping a build already running (when events arrived since the last
build). Quitting stops a build midway. What each number means, and where in the
code, is in [docs/STRUCTURE.md](docs/STRUCTURE.md).

| | |
|---|---|
| views | **Icicle** (`1`): rows by depth below the folder in focus, width by lines. **Treemap** (`2`): nested rectangles, a header strip per group. **Floating nodes** (`3`): one level at a time, the folder (or file) in focus as a hub with its children floating around it (drag one to move it) |
| heat | turns, lines, risk (turns × complexity), rework (lines rewritten within 2 turns), fixes, context (reads and searches before a change), cost (output tokens), time (agent time), age (newest is hottest), owner (the session or subagent that wrote most of today's lines). Log scale next to the busiest node of the same level; folders are darker. Hover a mode for what it measures |
| dots | flags: coral corrected / interrupted, yellow untested / failing, purple changed without reading it first, cyan slow |
| arcs | hover or inspect a file or function: lines to what changed in the same turns (coral: another folder or file); dashed to the nearest thing on screen that holds a partner |
| inspector | right: hovering a node inspects it, and it stays when the mouse moves on (into the inspector: its code, partners and turns). Flags, the code with a strip per line in the colour of the turn that wrote it (hover a strip for that turn and the agent's comment; hover a legend row to single out its lines), size after each change, fixes, rework, context cost and what was read to change it, partners with `shared/turns (P% match)`, cost and time next to the average, who wrote today's lines and how old they are, the hottest children by the current mode, and the turns that changed it with the agent's comments. Rows and turn titles are clickable |
| turn strip | bottom: one bar per turn in time order, height = lines changed, width = time taken (with too many turns for the width, bins of neighbours). Fill: coral = corrected (your next prompt reads like a correction) or interrupted, else pink = a fix (its prompt reports a problem), else blue; a stripe at the base: red = the last build/test after a change failed, else yellow = a change with no build/test after it; a cyan top = slow (2× the average turn); amber, outlined = selected. The legend is under the turn's title; hover a bar for its time, duration, +/−, tags and prompt. Pick one (click, `[` `]`, ‹ ›) to dim what it didn't touch and inspect it: files and functions it changed, what it read, how much of it survives today, its cost, your next prompt when it was corrected, and what the agent said, in order |

Click a folder to focus it, a file to open its functions; the centre, a path
segment, `Esc` or `Backspace` go up. Keys: `1` `2` `3` views, `[` `]` turns,
`/` or `Ctrl+F` search (below), `n` `N` its next and previous result,
`Esc` closes the search box, then clears the search, then the turn, then goes
up, then back to the stage; `Backspace` up; `R` rebuild now, `S` stage, `Q` quit. The heat modes are picked in the bar
(click). Wheel scrolls the inspector, over the inspector or over the stage
(any view: read the hovered node's panel while still pointing at it); inside
the inspector's code, the code, and `Shift`+wheel sideways. Click a line of
the inspector's code to open it in your Neovim (below).

**Search.** `/` or `Ctrl+F` opens a box at the right of the path, its
results in one list under it; while it has focus every key is the box's (none
of the screen's, `S` and `Q` included). `Backspace` deletes a character,
`Ctrl+Backspace` or `Ctrl+W` a word; `Up`/`Down` (or `Ctrl+P`/`Ctrl+N`,
`PgUp`/`PgDn`) move the selection, the wheel scrolls the list, hovering lights
a row; `Enter` or a click goes to a result, `Esc` or a click elsewhere (which
does nothing else) closes the box. A query counts up to 64 characters. Two kinds:
- **Names**: the paths of folders, files, functions and classes,
  case-insensitive, ranked as a file finder does: the exact name first, then a
  name starting with the query, a name holding it, a path holding it, then the
  query's letters in order anywhere in the path (fzy's scoring: runs of
  consecutive letters and letters starting a word count more). Each with its
  kind and path, the matched letters in amber; the first 100 are listed. The
  paths are folded once per build (on its thread), a byte test goes first, the
  fuzzy ones are scored only when fewer than 100 matched better, and a query
  extending the last one looks only among its matches: well under a millisecond
  on this repo, a few ms per key on 60,000 paths.
- **In files**: every file of the tree holding the query as typed (literal),
  smart case (case-sensitive only when the query has a capital), from 2
  characters, 120 ms after the last key. One row per line, `path:line`, the
  function or class holding it, the line trimmed with the match marked; at most
  500 lines ("500+"). It runs on a worker thread, a newer query cancelling an
  older one, over the texts the build read (the tree's files, binaries and
  files over 8 MB already left out; section 0 of docs/STRUCTURE.md). On this
  repo (174 files, 2 MB) a search takes 1 to 12 ms. While a new query waits for
  its lines, the last query's are gone: nothing shown or gone to is another
  query's.

Going to a name focuses its folder (a function's file or class: its
functions), so it shows, and inspects it; going to a line inspects the function
holding it, else the file, and the inspector's code scrolls to the line, lit,
the query's matches in it marked (a line past the code's first 400 lines, or
a function's 80, moves the code there: "lines A–B of 1–N"). The box closes and
the query stays: `n` `N` step through the results (the first `n` goes to the
selected one; the box shows `–/N` until then), `/` opens the box on it again,
selected, so typing replaces it. While a query is active, what matched is outlined amber and what
neither matched nor holds a match is dimmed, as a selected turn dims what it
didn't touch (the outline fades with it); with both, a node shows only when
both would show it.

## Context view

`X` on an agent (the one under the mouse, else the turn window's; or the
**context** button in a turn window) opens the **context screen**: what that
agent (a session's main agent or a subagent) had in its context window at one
of its API calls, rebuilt exactly from the request records in its transcript
(`docs/CONTEXT.md` has the rules). The agent sits in the middle, its model and
how full its window is on a ring; every item of its context is a window around
it, newer ones nearer (distance follows age, no two overlap): the system
prompt, tool definitions, injected text (CLAUDE.md, memory, hook output,
system reminders), prompts, its own text, thinking and tool calls, and the
results, each with what is in the context: a Read's lines as code with their
numbers (click one: that file at that line in Neovim), a command's output, an
Edit as a diff, prompts and text as light markdown. Its title bar says the
kind and source and the tokens; under it, its share of the context, the call
it entered and its age, its cache state at this call (read, written, new),
what holding it has cost so far, and whether its file went stale (`stale ·
known` when the change is the agent's own, `⚠` when not). Older items are
title-bar chips (a click opens one); the last calls' items and the call's own
response (dashed) start open, ten at most. Drag a window by its title (it
stays there), resize it from its corner, scroll its body with the wheel; drag
the background to pan, wheel on it to zoom.

The strip at the bottom has a bar per call (height: context size; red: a
compaction; hollow: no response recorded): click or drag to scrub, `←` `→`
step, `space` plays (new items fly out of the agent, a compaction folds the
windows into its summary). The right panel shows what the context is made of
by kind, and the agent's actions (a filter box, `/`): pick one to go to the
call that made it, with dashed links from the agent to the items likely
behind it ("likely · read of a.c", "the prompt of this turn", …), listed
under **why it decided**. The bar's picker switches to another agent of the
session (at the call of the same moment, or its last when at the end).

It follows the stage's clock (the last call at or before it; live: the
newest), "following the stage" in the bar, until you scrub, step, play or pick
an action ("detached"); `L` or a click on it follows again. While it is open
the agent's transcript is watched: once it stops growing for 3 s (15 s at
most) it is read again in the background, and the windows, sizes, what you
opened or folded, scroll, the selected action, the filter and the view stay
as they were, new items flying in. `Esc` clears the filter's focus, then the
selection, then the filter, then goes back to the stage (as does `X`).

## Open in Neovim

Clicking a line of code (the stage's detail window, the structure
inspector's code, a Read window of the context screen) opens that file at that line in the Neovim whose working
directory is the repo root (`--root`; compared as real paths), and only
that one: `:edit` in its current window, or in the tab's first window with
a normal buffer when the current one is special (a terminal, a file tree,
quickfix, `winfixbuf`); never a new tab or split. The cursor goes on the
line, centred. Hovering such a line shows a pointing hand and the line lit
when there is one, with "click: line N in nvim" in the detail window's
header, "click: open in nvim" in a corner of the inspector's code. Neovim
0.9+ listens by default on `$XDG_RUNTIME_DIR/nvim.<pid>.0`; aigource looks
there for nvim processes whose `/proc/<pid>/cwd` is the root, newest first
(the next when one doesn't answer), re-checking every 2 s, and talks to
them over the socket with msgpack-RPC (`nvim_exec_lua`, the path passed as
data: no character in it can run a command). With none, nothing opens and
a hint says "no nvim open in <root>"; an error in Neovim (`E37: No write
since last change`, …) shows as a hint too, and no reply within 3 s as
"sent; nvim is busy". `AIG_NVIM_DIR` replaces the socket directory (the
tests use it).

## Options

```
-r, --root DIR     repo root (default: current directory)
-s, --since HOURS  sessions written to in the last HOURS (default 2; the
                   newest session when none qualify)
-a, --all          every session of the repo
-x, --speed N      replay speed multiplier (default 60)
-l, --live         skip the replay animation, start at now
-w, --watch        also show filesystem changes (yours, other tools), live
                   only; agent commands' changes go to that agent
-W, --size WxH     window size (default 1600x900)
--dump             print the normalized event stream as NDJSON and exit
--follow SECS      with --dump: keep printing new events for SECS seconds
--detail           with --dump: after each step, what the detail window shows
                   and the agent's comments on it
--structure-dump   print the structure analysis (tree, turns, changes) as JSON
                   and exit; counts go to stderr
--metrics FILE     with --structure-dump: also write what the structure view
                   derives from it (per node and turn) to FILE as JSON;
                   build times go to stderr
--rebuild N        with --structure-dump: build N more times on the same
                   analysis (its caches warm), as live rebuilds do
--context-dump AGENT
                   print what one agent had in its context window at each API
                   call as JSON (the context view's data, shaped like the
                   draft's data.json) and exit; AGENT is a transcript path, or
                   a session or subagent id (or a unique prefix) of the root's
                   sessions; counts, load time and memory go to stderr
--context-at CALLS with --context-dump: also each item's tokens, cache state and
                   carried cost at these calls (comma-separated, negative from
                   the end, or all)
--shot FILE        render, save a PNG after --frames frames, exit
--frames N         frames before --shot (default 600)
--seek F           start paused at fraction F (0..1) of the timeline
--mouse X,Y        fake mouse position for --shot (hover testing)
--turn ID          open the turn window of the actor whose id starts with ID
--step N           with --turn: open its step N in full (negative: from the end)
--file PATH        open the last change of this file, as a click on it does
--structure VIEW[:MODE[:PATH]]
                   open the structure screen: icicle, treemap or floating;
                   a heat mode (turns, lines, risk, rework, fixes, context,
                   cost, time, age, owner); a folder to focus, or a file or
                   function to inspect (path below the root)
--structure-turn N select its Nth turn in time order (negative: from the end)
--structure-search QUERY
                   open the structure screen's search box on QUERY
--structure-pick N with --structure-search: go to its Nth result, as Enter
--context AGENT[:CALL[:ACTION]]
                   open the context screen on an agent (AGENT as --context-dump
                   takes it) at its call CALL (negative: from the end; the
                   last by default), with the action item ACTION (as
                   --context-dump numbers items) selected; either way it
                   doesn't follow the stage's clock (L does)
```

`--structure`, `--structure-turn`, `--structure-search` and `--structure-pick`
are for `--shot` (a `--shot` waits for the build, and for the search's results):
`--structure floating:risk:src/model.c/model_new --structure-turn -1`,
`--structure icicle --structure-search an_build --structure-pick 15`.
`--context` too (a `--shot` waits for the load, or its error): `--context
ee491c71:-1`, `--context ~/.claude/projects/x/s.jsonl:12:340`.
`AIG_CTX_BENCH=N` scrubs the context screen through the last N calls, a call a
frame, then fits everything in view, and prints the layout and frame times to
stderr; `AIG_DEBUG=1` prints each live reload's time and what it kept.

Keys: `Esc` close the top window (step, then turn; quits when none is open), `space` pause (live, like a
DVR: unpausing replays from 2 s before the pause at x1, idle stretches squeezed, back to live and its speed once caught up), `←`/`→` seek 2%, `Home` start, `+`/`-` speed (down to x0.25), `L` live,
`H` legend, `C` auto camera (after mouse pan/zoom), `F` fullscreen, `S` structure screen, `X` the context screen of
the agent under the mouse (or the turn window's), `Q` quit.
In the structure screen: `1` `2` `3` views, `[` `]` turns, `/` or `Ctrl+F` search (`Enter` go, `n` `N` next, previous), `Esc`/`Backspace` up, `R` rebuild, wheel scrolls the inspector.
In the context screen: `←` `→` calls, `space` play, `L` follow the stage, `f` fit the open windows, `a` fit all, `/` or
`Ctrl+F` filter the actions (`Enter` leaves the box), `Esc` clear (filter box, selection, filter) then back, `X` back.

## Where the data comes from

Claude Code's transcripts in `~/.claude/projects/<repo-slug>/` (or
`$CLAUDE_CONFIG_DIR`): `Read`/`Grep`/`Glob`/`Edit`/`Write` calls with their
results, and `bashEditDiff`, where Claude Code records the files a Bash command
created, changed or deleted, generated files included. Files a command read (`cat`,
`head`, `tail`, `sed` without `-i`, `nl`, `less`, `<` redirects, following
`cd`) count as reads, marked "by the command" in the turn window, when the
command succeeded and the file is still there. A resumed session's file starts
with copies of earlier records; those are counted once. The transcript format
is internal to Claude Code; every assumption about it lives in
`src/transcript.c`.

## --watch

Watches the repo with inotify (skipping `.git`, gitignored paths and editor
temp files) and shows changes no agent made as a **you** actor: your saves,
formatters, a `git checkout`. Claude Code records every file an agent changes,
Bash commands included, so a change is yours when no transcript accounts for
it; while an agent call that may still report it is running, it waits. Live
only: nothing records past filesystem changes, so replays don't have them.

## Tests

`make test` checks the parser and session discovery against the hand-built
transcripts in `tests/fixtures` (regenerate with `tests/make_fixtures.py`), and
unit-tests the timeline's time mapping (`tests/test_timeline.c`), its per-turn stats (`tests/test_turns.c`), change ring
sizing (`tests/test_rings.c`), opening code in Neovim (`tests/test_nvim.c`:
discovery and msgpack-RPC against headless nvims in a throwaway socket
directory; skipped without nvim), the folder layout (`tests/test_layout.c`: no
crossing lines on random trees), the structure screen's geometry
(`tests/test_slayout.c`: treemap and partition against d3's numbers, nesting
on random trees, the floating nodes' simulation), its search
(`tests/test_ssearch.c`: ranking, highlights, the names index against them,
smart case, lines and columns with CRLF, UTF-8 and bytes that aren't, binary
texts skipped, the cap, cancelled searches; with `AIG_TEST_DISPLAY` set to a
private X display, a `--shot` of a search and its first result), the structure analysis
(`tests/test_analysis.c`: line alignment, hunks, relocation; `tests/test_change.c`:
a change read back from the transcript; `tests/test_metrics.c`; expected JSON
for a hand-built structure fixture, also with `AIG_NO_DEFS=1`) and syntax
highlighting against the installed grammars (`tests/test_syntax.c`, skipped for
a language with none). The Python and HTML draft in `draft/` is the oracle for
two parity checks: `tests/metrics_parity.js` (`--metrics` against the page's own
JavaScript; part of `make test`, needs node and the d3 that
`scripts/fetch-deps.sh` fetches, skipped without them, a failure with
`REQUIRE_PARITY=1`) and `tests/structure_parity.py` (`--structure-dump` against
`build_data.py` on this repo's transcripts; run by hand). The context engine
(`docs/CONTEXT.md`) has the same: expected JSON for a hand-built project
(`tests/fixtures/context`, from `tests/make_context_fixture.py`), plain and
leak-checked, that `draft/context/build_ctx.py` must agree on too, unit tests
for sha256, Python's `json.dumps` and the API (`tests/test_context.c`), and
`tests/context_parity.py` (`--context-dump` against `build_ctx.py` on every
agent of this repo's sessions; run by hand). The context screen's rules
(`tests/test_clay.c`, plain and leak-checked): which windows start open and the
cap, ranks and target distances, the calls strip's hit test, the header's words
on the fixture, a reload keeping the user's choices by item key (the fixture
transcript appended), and the window layout (settled apart, pinned windows
kept, newest nearest); with `AIG_TEST_DISPLAY`, a `--shot` of the fixture's main
agent with an action selected (plain and asan).
`make test-watch` (~25s) runs the watcher end to end in a throwaway repo.
`make asan` builds `build/asan/aigource` with AddressSanitizer and UBSan (its
own objects, the normal build is untouched); run it with the same flags, e.g.
`--shot` on a hand-made transcript. Exit-time leak reports from ingest/model
are long-lived allocations the app never frees, not errors.

## Roadmap

- M3: tree-sitter dependency edges (using Neovim's installed grammars),
  fading agent trails, same-turn grouping.
