# Porting the structure view into aigource

The draft (`structure.tpl.html` + `build_data.py`) was the reference; the spec
for every number is now `../docs/STRUCTURE.md` (`METHODS.md` is a pointer). The
app gets the same output, natively: no Python or browser at runtime, one binary
as today. **Status: P1 to P6 are done**; the draft stays as the test oracle.
What is left is under Follow-ups.

## Decisions

- **All C.** `build_data.py` becomes the test oracle, not a dependency.
- **Same window.** `S` switches between the stage (today's gource view) and the
  structure screen. Inside it: tabs Icicle / Treemap / Floating nodes (`1 2 3`),
  heat modes, breadcrumb, inspector, turn strip, as in the draft.
- **History = what aigource loaded** (`--since`, `--all`, explicit files), so the
  structure view agrees with the stage. `--all` gives the draft's view.
- **Tree = files on disk today**: `git ls-files` under the root (cap 5000), or the
  files history touched when not a git repo. Untouched files show cold.
- **Functions/classes from tree-sitter textobjects** (`@function.outer`,
  `@class.outer` from nvim-treesitter-textobjects, found like the highlight
  queries are). No query for a language → the file has no functions (file level
  only). Complexity = 1 + branch nodes inside the def (a node-type list:
  if/elif/else-if, for, while, do, case, catch/except, conditional expression,
  `&&`/`||`/`and`/`or`). This replaces the draft's regex/indentation detection
  (a known caveat), so function-level numbers won't match the draft exactly.
- **Analysis off the render thread.** It reads files and runs git; it builds an
  immutable snapshot on a worker thread and the view swaps it in.
- **Every transcript assumption stays in transcript.c.** Analysis consumes
  Events plus on-demand re-reads of a record (as `tr_detail` does), never JSONL.

## Phases

Each phase: an executor implements, `make && make test && make asan` pass, a
reviewer agent checks it against METHODS.md, fixes land before the next phase.

### P1. Events carry what analysis needs (transcript.c, event.h)
Done: `EV_USAGE`, `EV_COMPACT`, `EV_INTERRUPT`, `EV_TITLE`, `EV_BASH_END`,
`EV_TURN_COPY` and read ranges (`EV_READ` `range`, `VIA_CMD`); prompt kinds
(`PROMPT_*`: only human and pasted start turns); `tr_change` for a change's
patch on demand; resumed-copy dedupe in `ingest.c`; `--dump`, fixtures and
expected NDJSON extended.

New or extended events, each mapped from METHODS.md "The stream":
- `EV_USAGE`: tokens (output, new input, cached input) per response, the
  largest seen per message id (parts repeat it, output growing).
- `EV_COMPACT`: compaction (`compact_boundary` / `isCompactSummary`).
- `EV_INTERRUPT`: `[Request interrupted…`.
- `EV_TITLE`: session titles (`ai-title`).
- `EV_READ`: line range read (`file.startLine`/`numLines`; whole file = 0/0);
  command reads keep theirs (`VIA_CMD`).
- `EV_BASH` outcome: is_error and end time, for verification (a verify event
  = a command whose segment starts with VERIFY, METHODS.md section 0).
- A change's patch on demand: `tr_change(src, off, id, path)` → original text
  (if any), hunks, created content, or bashEditDiff hunks for that path.
- Subagent meta (agentType, description) for ownership names.
- Dedupe of resumed-session copies (by tool_use id + file; turns by prompt
  ts + text), as METHODS.md says.
`--dump` prints the new kinds; expected NDJSON updated; fixtures extended with
one record of each new kind.

### P2. Analysis engine (new `src/analysis.c/.h`)
Done: `analysis.c` (P2a: the replay, blame, turns, costs, flags;
`--structure-dump`) and `metrics.c` (P2b: per-node and per-turn values;
`--metrics`). The line diff is difflib-equivalent below 10^7 line pairs and
patience-anchored above, not Myers as planned (origins then agree with the
oracle line for line); git calls are literal-pathspec, prompt-free, 10 s
timeout. Tests: `test_analysis.c`, `test_metrics.c`, the structure fixture's
expected JSON (also with `AIG_NO_DEFS=1`), `metrics_parity.js` in `make test`,
`structure_parity.py` by hand.

Split in two: **P2a** ports `build_data.py` (the data, `--structure-dump`,
parity test); **P2b** ports what the page derives in JS (per-node aggregates,
heat values, coupling, rework window, slow, turn info), so P4 only draws.
Turn rule (settled in P1): human and pasted prompts start turns; slash
commands and harness records don't (`build_data.py` and METHODS.md updated).
A port of `build_data.py` per METHODS.md, sections 0–12 plus time/slow:
- turns, comments, actors; the tree (folder chains merged as on the stage);
- file replay with line origins: apply hunks; resync with a line diff (Myers on
  line hashes) when the tracked text drifts; git baseline at the change's time
  (`git -c core.fsmonitor= log -1 --before`, `git show`); lining up with a later
  known text; "unplaced"; `has_new` dedupe of concurrent bashEditDiff credit;
- defs + complexity (syntax.c gains `syn_defs()`), per-change def attribution;
- metrics per node: turns, +/−, risk, rework (2 turns), fixes, context cost,
  coupling (≥ 2 shared turns, `shared/turns (P% match)`), flags (corrected,
  interrupted, untested/failing, changed unread, slow), complexity trend,
  survival, cost (tokens, time with the 5-min idle cap, line share for defs),
  ownership, age;
- per turn: changes, files/defs touched, reads, comments, survival, time.
`aigource --structure-dump` prints the snapshot as JSON shaped like the
draft's `data.json` (without sources). Tests:
- `tests/run.sh`: a structure fixture (two sessions, a subagent, an edit
  without originalFile, a command edit, a read, a test run, a compaction, an
  interrupt) with expected JSON;
- `tests/structure_parity.py`: run the app (`--all`) and `build_data.py` on
  this repo; file-level numbers (turns, +/−, blame %, rework, fixes, verify,
  tokens, time) must match for every common file; differences listed. Function
  level is reported, not enforced (tree-sitter vs regex).
- unit tests for the line diff, hunk apply and relocation.

### P3. Shared drawing (render.c → `src/ui.c/.h`)
Done: fonts, text drawing and measuring, `fit`, colors and the styled code row
moved to `ui.c`; the stage's `--shot` PNGs unchanged.

Move fonts, text drawing/measuring, `fit`, colors, the styled code row
(syntax + word styles) out of render.c so the structure screen reuses them.
No visible change: same `--shot` PNGs before/after.

### P4. Structure screen (new `src/sview.c/.h`)
Done: `sview.c` (screen, input), `slayout.c` (icicle, treemap, floating
nodes), `sinsp.c` (inspector), `--structure` / `--structure-turn`; the ui.c
items below are in; `test_slayout.c` checks the layouts against d3's numbers.

First, the P3 review's small items in ui.c: `ui_code_free` on a zeroed
CodeView, clamp `first` ≥ 1 / reject NULL path, an alpha for `ui_code_row` /
`ui_draw_row` (dim styled text), no outcome words (error/ok) in CodeView,
cache `ui_code_cell()`, stale comments; consider moving `kind_color` into the
ui palette.
- Layouts in C: icicle (partition), squarified treemap, floating nodes (one
  level at a time, hub + ring forces, collision); click a folder = focus it,
  click a file = open its functions; `Esc`/`Backspace`/breadcrumb = up.
- Heat on a log scale per level; 10 modes (turns, lines, risk, rework, fixes,
  context, cost, time, age, owner); ramp and dark folders as the draft.
- Inspector (scrollable): header and stats, flags, code snippet with blame
  strips + blame card + by-turn legend (hover a function anywhere = its
  snippet), size trend sparkline, fixes, rework, context cost, partners with
  % match, cost and time vs average, ownership, age, turns with the agent's
  comments.
- Turn strip: height = lines, width = time, fix/slow marks; `[` `]`, click =
  dim untouched nodes and inspect the turn (survival, cost, files, functions,
  reads, comments, next prompt).
- Coupling arcs and flag dots on nodes.
- Test hooks: `--structure VIEW[:MODE[:PATH]]` opens it, for `--shot`.

### P5. Live
Done: the screen owns an `Analysis` fed every event the stage drains; builds
run on a worker thread, one at a time, rebuild 3 s after the last event (15 s
at most), `R` forces one, a finished build is swapped in at the start of a
frame, and focus, inspected node, turn, view, mode and scroll survive it;
quitting cancels a build.

New events → recompute (debounced, ~3 s, only while the screen is open or
when opened); keep focus, inspected node and turn across snapshot swaps.

### P6. Docs
Done: `docs/STRUCTURE.md` is the spec (draft names mapped to C functions, known
limits, a status for each caveat), the README's Structure view section and
Options match `main.c`'s usage, `draft/METHODS.md` and `draft/README.md` point
to it and name the draft as the oracle.

README section; METHODS.md moves to `aigource/docs/STRUCTURE.md` as the
app's spec, updated where the port differs (tree-sitter defs); the caveat
list in memory gets a status per item.

## Order and delegation
P1 → P2 → P3 → P4 → P5 → P6, one executor per phase (opus for P2 and P4),
reviewer after each. P3 can run beside P2 (disjoint files) if P2 runs long.
Constraints for every agent: stage nothing, no commits, no whole-repo
stash/checkout/reset (other sessions' work shares the checkout), never kill
tmux, `make asan` for sanitizer runs, single-window rule for any nvim tooling.

## Follow-ups

Not done, from reading the code and the parity tests:
- **Command-read ranges.** Done: `sed -n` line prints, `head` and `tail` carry
  a range through `GuessFn` to `EV_READ` (tail's last N resolved against the
  replayed text); `build_data.py` has the same rule. `structure_parity.py`
  allows 4 + 10% reads per change for how each side splits a command into words.
- **`structure_parity.py` is not in `make test`.** It needs this repo's
  transcripts, so it is run by hand; a make target that freezes them (as the
  script does) would put it on the same footing as `metrics_parity.js`.
- **Function-level parity is reported, not enforced** (tree-sitter against the
  draft's regexes); a language without textobjects queries has file numbers
  only.
- **CRLF files.** Texts are not newline-translated, so they differ from the
  oracle's (Python's universal newlines).
- **Pipes hide a failing build** (`make | tail`). Done: a piped `VERIFY`
  segment with no pipefail is judged by failure signals in its output
  (`failed_out` on `EV_BASH_END`; the draft mirrors it). The signals are a
  fixed list; `make || true` and `make; echo` still hide the status. An
  audit then anchored the signals, split segments with the shell lexer, made
  pipefail need a real `set`, and dropped runs in another tree.
- **`kind_color`** (the stage's palette, `model.c`) is still outside the ui
  palette; the P3 review suggested moving it.
- **Rebuilds redo everything.** Each live rebuild reruns git, rereads and
  reparses every file (~3 s on this repo). Per-file caches keyed by text hash
  (defs, cx, today's line map) would make a rebuild cost what changed.
- **Floating labels can collide** in crowded folders (the draft doesn't avoid
  it either; the app shows untouched files too, so folders are fuller).
- **Exit-time leaks of the stage** (`model.c`, `ingest.c`, `timeline.c`,
  `transcript.c` allocations; `viz_run` never frees the scene) make ASan GUI
  runs exit 1. Older than this port; nothing leaks from the structure code.
- **Subagent titles have ts 0** (ingest emits them on discovery). Harmless:
  names, not timed.
- **`metrics_parity.js` on an empty history** reports `ageSpan` (C floors it
  at 3600 s, the page gives NaN); skip that global when there are no changes.
- **Partners and per-line stats are computed for every node** on each build;
  fine at 55k nodes (0.45 s), lazy partners would be the next step if needed.
