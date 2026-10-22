# Context view: plan

aigource's third screen: what one agent has in its context window at a moment,
shown as floating windows around it, so you can see what it remembers and why
it took a decision. Draft first (HTML page on real data, as the structure view
was), port to the app once it feels right.

## Decisions (with the user, 2026-10-21)

- **Answers two questions:** what is in the context now, and what the agent saw
  before an action (an edit, a command).
- **One session at a time**, opened from an agent on the stage.
- **Layout: floating windows.** The agent in the middle; every item of its
  context is a window around it, draggable and resizable, showing exactly what
  is in the context: the lines a Read returned, a command's output, a prompt, its
  own comments, injected text. Distance = age: the newest closest, older ones
  drift outward and shrink to title-bar chips; angle free (force layout, no
  overlaps). A dragged window stays where it is put.
- **Open vs chip:** the last few calls' items and whatever links to the selected
  action start open; the rest are chips, a click opens them.
- **Older sessions** (no request records): estimated from the conversation,
  marked as such.

## Data: the exact requests

Recent Claude Code versions write each API request into the transcript:
- `api-request`: `shapeHash`, `keep`, `tail`, `params` (model, max_tokens,
  thinking, `context_management` edits), `cacheControl`, `querySource`.
  Its messages = the previous request's first `keep` messages + `tail`
  (hashes);
- `api-request-blob`: a message by `hash` (content-addressed; a few hashes are
  only in other files of the project, look there too);
- `api-request-shape`: `system` and `tools` by `shapeHash`.
So the input of every call can be rebuilt as sent. Older sessions: the
conversation records (`user`/`assistant`, compaction boundaries) give the
messages; system and tools are estimated from the first call's usage.

## Items (one window each)

A context item is one content block of the input, with a stable identity across
calls (blob hash + block index; system/tools by shape) so a window lives from the
call it entered to the call it left (compaction, cleared thinking).
Kinds: system prompt, tool definitions, injected (CLAUDE.md, memory, hook
output, system reminders: split out of the user message they ride in), prompt
(typed / pasted), the agent's text, thinking (if the transcript has it), its tool
calls, tool results by tool (Read with path and range, Bash with the command,
Grep/Glob, Edit/Write results, subagent reports), compaction summary.

Header row of a window:
- kind and source (`Read src/a.c 1-400`, `Bash make test`);
- tokens (block characters scaled per call so the call's blocks add up to its
  real usage: input + cache read + cache write) and share of the context;
- entered at call N / time, age (calls, minutes);
- cache state at this call (read / written / new);
- carrying cost: what holding it has cost so far (its tokens × each later call's
  cache-read rate; prices from one table, named in the page);
- stale: the file changed after this read. "known" when the change is in the
  context (its own Edit), ⚠ when it isn't (another agent, a command, the user).

## Why it decided

Selecting an action (a tool call) moves to the call that produced it and draws
links from the agent to the items most likely behind it, labeled "likely":
reads of the file it acted on, the prompt that started the turn, its text and
thinking just before in the same response, results that mention names in the
action's input.

## Time

Follows a clock: a strip of the session's calls at the bottom (bar height =
context size, compactions marked), scrub or play; new items fly out of the
agent, a compaction folds the windows into its summary, cleared thinking fades.
In the app the clock is the stage's.

## Port to the app (approved 2026-10-21)

The draft (`build_ctx.py` + `context.tpl.html`) is the reference and becomes
the test oracle, as the structure draft did. Same output in C, one binary, no
Python or browser at runtime.

Draft fixes first (it is the oracle and the user's reference): text gets
selected while dragging windows; switching agent shows nothing until a step
is clicked (it should show the call at the current position, the last call by
default).

### C1. Data engine (`src/context.c/.h`)
A C port of `build_ctx.py` for one agent (a session's main agent or a
subagent): read its transcript on demand (not through the event stream: the
blobs are large and only the open agent needs them), on a worker thread;
rebuild calls (`base`/`keep`/`tail`, shapes, blobs by sha256 of canonical
JSON, rebuilt messages for hashes with no blob), request → response by
`requestRef`, items with enter/leave, injected text split out, tokens scaled
to real usage, cache state, carrying cost (prices in one table, same source),
stale reads, likely links, estimated sessions. `aigource --context-dump
AGENT` prints it as JSON shaped like the draft's `data.json` for one agent.
Tests: a fixture transcript with expected JSON in `make test`; unit tests for
sha256 and canonical JSON (Python `json.dumps(sort_keys=True,
separators=(',',':'), ensure_ascii=False)`, numbers included);
`tests/context_parity.py` (by hand) runs both on this repo's sessions:
0 unresolved hashes, same calls, items, enter/leave, tokens, stale, links.

### C2. Screen (`src/cview.c`, windows in `src/cwin.c`)
Opened from an agent on the stage (its turn window gets a "context" button,
and a key on the hovered agent); `Esc` back. As the draft: the agent in the
middle; windows (draggable by the title, resizable from a corner, wheel
scrolls the body) with the header row and exact content (code via
`CodeView` with the read's line numbers, output in mono, text via `ui_md`);
chips; force layout with distance = age and rect collision (reuse the
floating-nodes simulation in `slayout.c`); the calls strip (scrub, play,
compactions); the right panel (composition by kind, why it decided, actions
with a filter); dashed likely links; agent switcher for the session's
subagents. Click code in a window → nvim (`nv_open`). Only visible windows are
laid out and drawn. Test hooks `--context AGENT[:CALL[:ACTION]]` for `--shot`.

### C3. Live and clock
The view follows the stage clock (call = last call at or before it) until
scrubbed or played (play is the view's own run through its calls); L follows
again. The transcript is re-read as it grows (appended bytes
only), debounced like structure rebuilds, keeping window positions, open
state and selection.

Done (2026-10-22): `ctx_reload` reads only the bytes appended since the last
load. It resumes from a loader state kept with the agent (the calls'
and instances' keys, request shapes, metadata of earlier responses, a
small window of raw lines from just after the last response before the last
request) and checks that nothing appended changes what was built; when it
would, it loads in full. Every reload equals a fresh load (JSON and keys):
`tests/context_reload.py` at every line, inside every line and at random cuts
of the fixtures (`make test`), and at random cuts of a real 80 MB transcript
by hand. docs/CONTEXT.md, "Reloads".

Pending (review of the incremental reload, 2026-10-23; fixes started, not
committed: usage limits stopped the work). None of these showed in real
transcripts (52 reloads of the 81 MB one equal, median 43 ms):
- the fixture reloads never fall back to a full load, so all 17 fallback
  guards survive mutation: fixed cases per guard (replace, shrink and regrow,
  tail rewrite, a late blob, a redefined shape, a blob written again, ...),
  the runner failing when none falls back;
- a shape written after the request that needed it is no fallback (record
  the missed shape hash, fall back when it appears);
- `restore` keeps a result's link label after it stops being linked (reset
  it so `post()` links again);
- an edit inside the kept window goes unseen (read the window back from the
  file and compare);
- the equivalence check compares `ctx_json` only, not every field the UI
  reads;
- a message written again with other key order, after an earlier load had
  it (`old_only`), may differ from a fresh load.

### C4. Docs
`docs/CONTEXT.md` is the spec (the draft README's rules, mapped to C
functions, known limits); README section, keys, options.

Each phase: an executor implements, `make && make test && make asan` pass, a
reviewer checks it, fixes land before the next phase. Same constraints as the
structure port (no staging, no whole-repo git commands, sandbox display only,
`AIG_NVIM_DIR` in every run).

## Draft

`draft/context/`: `build_ctx.py` (transcripts → data.json, one session or all
sessions of a repo), `context.tpl.html` (the page), `build.sh` → `out/`
(gitignored: it embeds prompts, source and command output). Checks: block
tokens add up per call, items' enter/leave against `keep`, stale against the
replayed file history, shots of the page.
