# aigource — gource for coding agents

Watch what Claude Code sessions do to a repo, live: which files they read,
edit, create and delete, how big each change is, and how the folder tree grows.
One native binary (C + raylib, ~1 MB), starts instantly, no browser.

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
| beam actor → file | the action, colored: read, search, create, edit, delete, changed by a command; thickness = diff size |
| rings | diff pulse: green = lines added, red = lines removed, size = how many |
| bubble | the command it's running, or the pattern it's searching |
| HUD | total lines written / removed, file and command counts |
| panel | active actors with their lines and current turn's prompt |
| legend | right side; `H` toggles it |

Actors dim after 2 minutes idle and leave the stage after 10.

**Click an actor** (in the graph or the panel) to open its current turn: the
prompt, then one line per step (read, edit, create, delete, search, `$`
command, any other tool or MCP call, with +/−). The files that turn touched get a soft area per folder in
the actor's color, everything else dims, and the camera frames them. Hover a
step to ring its file. Steps of that same turn keep appending live. `✕`,
`Esc` or a click outside closes it.

**Click a step** to see it in full, beside the turn window: an edit's diff with
line numbers, a created file's contents, what a read returned, a command with
its output (stderr in red; for a file a command changed, that file's diff
first), a search's parameters and matches, any other tool's input and result.
It's read back from the transcript on click, so nothing extra stays in memory;
a step still running shows so and fills in when its result lands. Wheel or
`PgUp`/`PgDn` scroll, `Ctrl+C` copies it, `✕`/`Esc`/click outside close it
(one window at a time).

**Hover** a file for what last happened to it ("Edit · src/a.py", size, the
session's +/− on it, who touched it and when), a folder for its counts, an
actor for its turn, prompt and totals.

**Timeline** (bottom), like a video player: click or drag anywhere to jump,
hover to preview the time and the turn running then. The strip above the bar
is activity (amber = writes, blue = reads), ticks are turns in the actor's
color, dark notches are idle stretches squeezed to 2 minutes so multi-day
histories stay usable. Jumping back rebuilds the scene from history and keeps
the layout where it was. "go live →" (or `L`) returns to now.

## Options

```
-r, --root DIR     repo root (default: current directory)
-s, --since HOURS  sessions written to in the last HOURS (default 2)
-a, --all          every session of the repo
-x, --speed N      replay speed multiplier (default 60)
-l, --live         skip the replay, start at now
-w, --watch        also show filesystem changes not made by an agent (live only)
--dump             print the normalized event stream as NDJSON and exit
--follow SECS      with --dump: keep printing new events for SECS seconds
--detail           with --dump: after each step, what its detail window shows
--shot FILE        render, save a PNG after --frames frames, exit
--seek F           start paused at fraction F (0..1) of the timeline
--mouse X,Y        fake mouse position for --shot (hover testing)
--turn ID          open the turn window of the actor whose id starts with ID
--step N           with --turn: open its step N in full (negative: from the end)
```

Keys: `Esc` close the top window (step, then turn; quits when none is open), `space` pause, `←`/`→` seek 2%, `Home` start, `+`/`-` speed, `L` live,
`H` legend, `C` auto camera (after mouse pan/zoom), `F` fullscreen, `Q` quit.

## Where the data comes from

Claude Code's transcripts in `~/.claude/projects/<repo-slug>/` (or
`$CLAUDE_CONFIG_DIR`): `Read`/`Grep`/`Glob`/`Edit`/`Write` calls with their
results, and `bashEditDiff`, where Claude Code records the files a Bash command
created, changed or deleted, generated files included. The transcript format
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
unit-tests the timeline's time mapping (`tests/test_timeline.c`).
`make test-watch` (~25s) runs the watcher end to end in a throwaway repo.

## Roadmap

- M3: tree-sitter dependency edges (using Neovim's installed grammars),
  fading agent trails, same-turn grouping, click to open in Neovim.
