# How the context view gets its data

The spec for aigource's context view: what one agent (a session's main agent
or one of its subagents) had in its context window at every API call, and why
it acted. Everything comes from Claude Code's transcripts:
`$CLAUDE_CONFIG_DIR/projects/<slug>/` (`~/.claude` by default), the agent's
own file and, for stale reads and the rare message only another file has, the
project's other files.

`src/context.c` does the work (`ctx_load`), on a worker thread if wanted, into
an immutable `CtxAgent`; a `CtxProject` caches the project's changes between
loads (section 4), and `ctx_reload` loads an agent again after its transcript
grew. It was ported from a Python and HTML draft
(`draft/context/build_ctx.py` + `context.tpl.html`, plan in
`draft/context/PLAN.md`); the draft is now the test oracle:
`tests/context_parity.py` compares `--context-dump` with `build_ctx.py` on
every agent of this repo's sessions, and `make test` checks a hand-built
project against both expected JSON and `build_ctx.py`. Each rule below names
the C function that implements it; a table at the end maps the draft's names.

`aigource --context-dump AGENT` prints one agent as JSON shaped like the
draft's `data.json` (meta, one session with one agent, the `S` table), so the
draft page can show it. `--context-at CALLS` adds what the page derives at
those calls (`at`: per position of the call's order, tokens, cache state,
carried cost). Counts, load time and peak RSS go to stderr; `AIG_DEBUG=1`
also prints the load's phases.

## 0. Agents and transcripts

The stage names an actor by its transcript: a main session is
`<slug>/<session id>.jsonl` (actor id = the session id), a subagent
`<slug>/<session id>/subagents/agent-<id>.jsonl` (actor id = `<id>`) with
`agent-<id>.meta.json` beside it (`agentType`, `description`, `toolUseId`,
`model`). Events carry their transcript (`Event.src`), so the screen can load
an actor's context from it directly; `ctx_find` turns an id (a session id, a
subagent id with or without `agent-`, or a unique prefix of either) into the
path, looking in the root's project dir (every non-alphanumeric character of
the root becomes `-`, as `ingest.c` names it). `ctx_session_agents` lists a
session's agents (main first, subagents by path) for an agent switcher.

The project's files, as `build_ctx.py` lists them (`project_files`): main
sessions by modification time (then by name, so the order is the same every
time; nothing the view shows depends on it: it only orders the search for a
message another file has), then every subagent by path.
The session's own files are its main transcript and its `subagents/`.
`CtxRef.tu` is a subagent's `toolUseId`, the parent's Agent call that started
it.

**What the screen gets.** Per item: kind, source, exact text, the call that
made it (`made`), tool, path, language, a Bash command and description, an
Edit's old and new strings, a Write's content (`CtxItem.cmd`, `desc`,
`old_str`, `new_str`, `content`: parsed once at load), enter and leave, the
action and result it pairs with, a Read's line range, stale changes and
likely links. Per call: its order, the items its response made
(`CtxCall.made`, the page's `madeAt`), and the derived values (section 3).
`ctx_positions` maps a call's items to their positions once per call shown.
An item's `key` stays the same across reloads (its message's hash and its
place in it, or its shape and place; a message sent again as a new instance
takes the next key in creation order), so what the screen keeps per item
(window positions, open state) survives one; `ctx_item_by_key` finds it.

## 1. Rebuilding the calls

**Records** (`read_records`): every line is parsed with yyjson (`PYJ_READ`,
in the "C" numeric locale, section 5); records of
other types than `user`, `assistant`, `attachment`, `system`, `api-request`,
`api-request-blob`, `api-request-shape`, `ai-title` are dropped as read, and
so are lines that aren't JSON objects. The parsed records live only during
the load.

**Requests** (`build_exact`, when the file has any `api-request`). Each
`api-request` is one call: its messages are its base call's (`base`) first
`keep` messages + `tail`, hashes of messages. A request without `base` (after
a compaction, or a full resend) starts over and is compared with the call
before it: a tail hash equal to a dropped message's keeps that message's
instance, the first such one not taken yet (per hash a queue of the dropped
instances in order; the draft's "not already taken" needs nothing more, as a
call never holds one instance twice). Items of the other dropped
messages are offered, by content key, to rewritten messages: a block whose
message changed keeps its item when the block itself is the same (`Free`,
`emit`). System and tools come from `api-request-shape` by `shapeHash`: the
same shape as the call it is compared with keeps its items; a new one keeps
the items of unchanged system blocks (by text) and makes new ones for the
rest, the tools first as one item (`CK_TOOLS`, each definition in `tx`). A
missing shape is one `CK_MISSING` item.

**Blobs** (`store_build`, `resolve`). A hash is sha256 of the message as
`json.dumps(message, sort_keys=True, separators=(',', ':'),
ensure_ascii=False)` (`pyjson.c`, section 5). A file supplies, by hash: its
`api-request-blob` records (by their recorded `hash`; a later one replaces an
earlier entry), each user record's message, every run of consecutive user
records sent as one message (`{"role": "user", "content": [their contents,
a string content as one text block]}`; a run is broken only by an assistant
record; every sub-run `run[a:b]` is hashed, incrementally per start,
`flush_run`), and each assistant response (all records of one `message.id`,
content concatenated, `find_responses`); the first entry for a hash wins
except for blobs. A hash in none of the own file's is looked up in the
project's other files in order, each read and stored when first needed, and
then counts as found elsewhere (`found_elsewhere`); one found nowhere is a
`CK_MISSING` item and counts in `missing`.

**Request -> response.** Assistant records carry `requestRef`, the `id` of
the `api-request` they answer; a retried request keeps its last response. A
call's usage is that response's last record's `usage`: `[input, cache read,
cache write 5 min, cache write 1 h, output]`, the 5-minute part being
`cache_creation_input_tokens` minus `cache_creation.ephemeral_1h_input_tokens`
(`usage_of`); an empty usage counts as none. The response's model, when it
has one, replaces the request's. An assistant message in a tail is matched to
the response that made it by its hash, else (a response sent back changed)
by any block it still has (its `id`, else its hash): the call that made it is
`made`.

**Compactions.** A call with `keep` 0 after a `compact_boundary` system record
(since the call before) is marked `comp`. A request with `keep` but no base
in the file counts in `broken`.

**Dropped thinking.** A `thinking_drop` attachment names a request id and the
first dropped block (message index, block index): the thinking items of that
message at the call whose response has that request id weigh 0 there
(`drop`). The indices are trusted as the draft takes them: negative counts
from the end, a block index past the last thinking block means the last;
beyond the start (where the draft fails) it is skipped.

**Estimated sessions** (`build_estimated`, no request records): one call per
assistant response (by message id, in order of first record); its messages
are the conversation records since the last compaction: user records, and
attachments with text (`content` if a string, else `text`; not
`hook_success`, not blank) as `role: system` messages. A call keeps the
leading messages it shares (by hash) with the call before; the response
joins the messages after its call, so its items appear from the next call. A
compaction marks the next call. System and tools are one item whose weight
is what the first call's usage leaves after its messages.

**Order, enter, leave** (`post`). A call's order: its shape items, then each
message's items. An item enters at the first call it is in that the call it
is compared with (its base, else the call before) hasn't, and leaves at the
first call that drops it. A result is linked with the action that has its
`tool_use` id (the last item made for each id): it takes the action's label,
tool and path.

## 2. Items

`blocks_of`: one item per content block of a message, keyed by content
(`role:H(block)`, plus a segment index) for the rewritten-message rule.

| Block | Item |
|---|---|
| a `role: system` message with no content | `injected`, `(effort: X)` from `output_config.effort`, else `(empty system message)`; weight 4 |
| system text | `injected`, source by `inj_source` |
| system `tool_addition` | `injected`, `tool added: NAME`, the definition as JSON (indent 1) |
| assistant text | `text` |
| assistant thinking, redacted thinking | `thinking`; "not returned" when the text is empty |
| assistant `tool_use`, `server_tool_use` | `action`: the input as JSON (indent 1), labelled by `action_label` (`Read src/a.c 3-4`, `Bash make test` without a leading `cd X &&`, `Grep pat in dir`, `Agent type: description`, ...), `path` from `file_path`/`notebook_path` |
| user text | split by `split_text` (below); with tool results in the same message, a typed-looking segment is `injected`, "with tool results" |
| user image | `prompt`, `image png WxH`; tokens below |
| `tool_result` | `result`: its texts (injected segments split out as their own items after it), images as `[image ...]`, other blocks as JSON, joined by newlines; error flag from `is_error` |
| anything else | as JSON, source = its type |

**Injected text** (`split_text`, `inj_source`). `<system-reminder>...</system-reminder>`
segments are split out of the text they ride in (the shortest match from each
opening tag); the rest is a prompt unless it starts (after whitespace) with a
harness tag (`<local-command`, `<command-name>`, `<command-message>`,
`<total_tokens>`, `<task-notification>`, `<bash-input>`, `<bash-stdout>`,
`<user-prompt-submit-hook>`: injected), with "This session is being continued
from a previous conversation" (`summary`) or with `[Request interrupted`
(injected, "interrupt"). An injected segment's source, from its first 600
characters: CLAUDE.md, memory, `hook EVENT[:MATCHER]` (a hook's additional
context, blocking error or feedback), hook output, token count, slash command,
task notification, else system reminder.

**Weights** (`w`, before scaling): characters / 4 (code points, as Python's
`len`); a result adds 10. An assistant response's blocks share its output
tokens: thinking blocks get its `thinking_tokens` by size (signature or data +
text + 1), the rest the remaining output tokens by the length of their JSON +
1 (a usage without thinking tokens: thinking text / 4; without output tokens:
that length / 4); a block whose response isn't known weighs characters / 4
(an action + 20). An image weighs w×h/750 after
the API's resize to 1568 px on the long edge (`img_info`: PNG and JPEG
headers from the first 4000 base64 characters), 1000 when unreadable. Weights
are rounded to 0.1 at the end, as the draft writes them, and everything
derived uses the rounded ones.

**Texts are shared**: an item's text is one string of `CtxAgent.str` (a pool
keyed by content, `Pool`), and `CK_TOOLS` keeps each definition there too. A
Read result's code view (`ctx_code_lines`): its lines numbered `N→` or `N\t`
up to the first that isn't, when they are at least half the lines (the page's
`/^\s*(\d+)[\t→](.*)$/`: a line holding `\r`, U+2028 or U+2029 after the
number isn't numbered, as JavaScript's `.` doesn't match them); `line0`
and `line1` hold the range, `lang` the language of its path (`lang_of`), and
a Bash action and its result share `cmd`.

## 3. Tokens, cache state, carrying cost (`derive`, the page's `prepare`)

Each call scales its items so they add up to its real usage, input + cache
read + cache write: `f = total / sum(w)` over the call's order, dropped
thinking excluded; an item's tokens are `w × f` (`ctx_tokens`). A call
without a response (interrupted, failed: `noresp`) takes the scale of the
nearest call with one (before first, at equal distance), 1.3 when none, and
its total is its weight × that.

Cache state: walking the order (tools, system, messages), an item whose
middle falls within the call's cache-read tokens is `read`, within the next
cache-write tokens `written`, else `new`; `CS_NONE` without usage. Carrying
cost: the sum, over the calls so far, of its tokens times the rate of its
state at each call (cache read; cache write at the call's mix of 5-minute
and 1-hour writes; input), per million, kept as a float as the page keeps it
in a `Float32Array`. Prices: `ctx_prices`, the same table and source as
`PRICES` in `build_ctx.py` (the claude-api skill bundled with Claude Code
2.1.306, model table cached 2026-10-06; cache writes 1.25× input for 5
minutes, 2× for 1 hour); other models get tokens only. `ctx_window` holds
the context windows the page shows.

## 4. Stale reads and likely links

**Changes** (`project_changes`, `scan_file`, `fc_record`, `use_edits`): in
every file of the project, Edit, Write, MultiEdit and NotebookEdit calls (by
`file_path`/`notebook_path` as written) and the files Bash commands write
(`cmd_write_files`, only for a command matching `write_cmd`: redirect
targets, `sed`/`perl -i`, `tee`, `rm`, `truncate`, `patch`, both sides of
`mv`, the `cp` target, `git checkout`/`restore`/... naming files, the quoted
names a python heredoc assigns or opens when it writes), at their results'
times, for calls whose result isn't an error. Files Claude Code noticed
changing (`edited_text_file` attachments) are kept apart. Each file is mapped
and only lines that can hold a change are parsed (`relevant_line`: a tool
call naming one of those tools, a result for one, an `edited_text_file`).
The scan's state (the calls seen, their results, each call's computed
changes) lives in a `CtxProject`, per file with its device, inode, size and
modification time: a load rescans only what changed, from the last scanned
offset when a file only grew (whole lines; a last line without its newline
is taken when it parses, else left for later), from the start when it was
replaced or rewritten; a cancelled scan resumes where it stopped. One
project serves one root (labels and command paths depend on it; a load with
another root clears it), and its scan is locked, so loads on several threads
may share it. A load copies what it uses out of it. Commands are split as the draft
does (`segments`: `&&`, `||`, `;`, newlines and `|` outside quotes, words by
`shlex_split` (POSIX shlex) falling back to whitespace, following `cd`).

**Stale** (`stale_events`): for a Read result (its `file_path`) or a
read-only command's output (`cmd_read_files`: `cat`, `sed` without `-i`,
`head`, `tail`, `nl`, `grep`, `rg`, `less`, `awk`, `wc`, `bat`, `diff` naming
files; none when the command writes through python), the changes to its files
after the read (its result's time) up to the call it left (else the last
call). Author: its own Edit/Write (`CST_OWN_EDIT`) or command
(`CST_OWN_CMD`), another agent of the session (`CST_SESSION`), another session
(`CST_PROJECT`), or noticed by Claude Code (`CST_NOTICED`) when no change of
the file falls between 5 s before the read and the notice and its own command
didn't write it. Sorted by time, author, item, label. The item of an own
change is the action with that tool_use id, the last one made for it, as the
draft keys them (a response sent back changed makes a new one). At a call
(`ctx_stale_at`) a read is stale by the changes up to the call's time (both
rounded to 0.1 s relative to the agent's first call, `CtxAgent.t0`, as the
page compares them), and *known* when all of them are the agent's own and
still in its context.

**Why it decided** (`links_of`): for an action, in the context of the call
that made it: reads of the files it acts on (Read results by path; Bash and
Grep results by the file-like words of their command and their `path`,
`cmd_paths`; the latest two per file, four at most, latest first), the prompt
of the turn (the last prompt or compaction summary in that context), its text
and thinking just before in the same response, and up to three results that
share a rare name with its input (words `[A-Za-z_][A-Za-z0-9_]{3,}` with an
optional `.ext`, from the first 200000 characters of each result; stop words:
`STOP_WORDS`, names in more than max(3, 15%) of the results, pieces of the
root, home and config paths; a hit needs a name in at most max(2, 3%) of
them; score = sum of 1/frequency over the shared names, at least 0.34,
highest first; the label names the three rarest).

## 5. Python's semantics, byte for byte

The draft's numbers depend on Python, so the port reproduces it:

- **Canonical JSON** (`pyj_dumps` with `PYJ_CANON`): keys sorted by code point,
  which is UTF-8 byte order (`memcmp`, shorter first); separators `,` and `:`;
  strings escape `"`, `\`, `\n`, `\r`, `\t`, `\b`, `\f` and other control
  characters as `\u00xx` (lowercase), nothing else (not `/`, not DEL, not
  non-ASCII); `true`, `false`, `null`. Numbers keep Python's types: yyjson's
  integer/real distinction is Python's (`1` int, `1.0`, `1e2` float); a
  float is written as `float.__repr__` (`pyj_float`: the shortest digits that
  read back, fixed notation for exponents -4..15 with `.0` added, else
  `1e+16`/`1e-05`); `-0` is the int 0, `-0.0` stays; integers beyond 64 bits
  stay exact (read raw, `YYJSON_READ_BIGNUM_AS_RAW`), floats beyond a double
  become `Infinity`; `NaN` and `Infinity` are read and written as Python does.
- **Other dumps**: default separators `, ` and `: ` (sizes of assistant
  blocks, unknown blocks as text), and `indent=1` (actions, tool definitions)
  with `,` and newlines, keys in their order.
- **Lengths** are code points, not bytes (`nchars`); `s[:k]` cuts at the k-th
  code point (`char_off`).
- **Whitespace** (`str.split`, `strip`, `\s`) is Unicode's (`py_space`).
- **`\w`** is exact for ASCII; beyond it letters and digits are approximated
  by blocks (`py_word`: punctuation, symbols, arrows, box drawing, emoji,
  combining marks and private use are not word characters). It matters only
  for file-like words with non-ASCII characters.
- **Times**: `datetime.fromisoformat(ts).timestamp()` exactly (whole
  microseconds / 10^6, `epoch`) for timestamps with a timezone (`Z` or an
  offset), as Claude Code writes them; one without is taken as UTC where
  Python would take local time. `round(x, 1)` correctly rounded, ties to even
  (`round1`).
- **Locale**: printf and strtod follow the locale's decimal point, and under
  a comma-decimal one every hash would miss without an error. The entry
  points (`ctx_load`, `ctx_json`, `ctx_stale_at`) switch their thread to the
  "C" numeric locale (`uselocale`) and back, whatever the app set; a test
  loads the fixture under a comma-decimal locale when one is installed.
- **Depth**: dumps recurse; past `PYJ_MAX_DEPTH` (1000) levels a value is
  unhashable (Python's own limit is about the same: the draft fails there),
  so its message is never found by hash, and its text (or a label's `str()`
  of it, `py_str`) reads "[nested too deeply to show]". yyjson itself reads
  any depth.
- **Big numbers**: counts (tokens, `keep`, indices; `geti`) are clamped to
  ±10^15, so their sums and differences can't overflow; a Read's offset and
  limit saturate at 64 bits (`num_ll`). Python's ints have no limit, so such
  values give other numbers and labels (`Read a.c 9223372036854775807-...`);
  no real transcript comes near.
- **Paths**: `os.path.normpath`, `expanduser` (`~` and `~user`), `splitext`
  (leading dots are not an extension) as Python does them.
- **sha256** is our own (`sha256_*`, FIPS 180-4).

Known gaps, none seen in this repo's transcripts: Python's `json.loads` keeps
the last of duplicate keys (at the first one's place) where yyjson keeps
both; it accepts lone surrogates (`\ud800`), which make the draft fail to hash
the message, where yyjson rejects the line; Python reads lines split on `\r`
too (universal newlines); values nested deeper than 1000 levels (above); and
`thinking_drop` indices are trusted (section 1).

## 6. Memory and time

A load reads the agent's file into records, builds its hash store, then keeps
only what the view needs: items (texts pooled once), message instances, each
call's tail, shape, order and derived values; the records and the stores are
freed. On this repo, the largest agent (a 77 MB main transcript, 659 calls,
2675 items, 2.8 MB of distinct text) loads in about 1.1 s with a peak RSS of
121 MB (the own file's records during the build; the project's transcripts
are scanned before they are read, so their mapped pages aren't resident
beside them) and keeps 7.2 MB once loaded, plus about 5.5 MB a reload resumes
from (section 6a). `AIG_DEBUG=1` splits a first
load: changes 0.47 s (the project's 370 MB of transcripts, scanned once into
the project cache), read 0.1 s, store 0.24 s, calls 0.17 s, stale 0.01 s,
links 0.09 s. With the cache warm the scan
takes 2 ms: switching to another session's main agent (421 calls) takes 0.40
s, back to the largest 0.64 s. A reload of the 80 MB transcript (688 calls)
takes 46 ms after one record was appended, 46 ms after 50, 59 ms after 1 MB
(a full load with the project cache warm: 0.72-0.80 s); with nothing new, 50
ms: copying the previous agent (13 ms), stale reads (10 ms), links (11-14 ms)
and the derived values are what remains. The draft takes 35 s and 1.1 GB for
all 77 agents.

## 6a. Reloads (`ctx_reload`)

A live agent's transcript only grows, so a reload reads only the bytes
appended since the agent was loaded. A full load is itself a reload from an
empty state: one path builds both, so every load exercises it.

**What an agent keeps** (`Loader`, in the agent's private part; never the
file's text but a window of it):
- the file as far as taken: device, inode, offset (whole lines), the 256
  bytes before it (`chk`);
- the window: the raw lines from just after the last response before the
  last request to the end taken (`win`, usually a few KB to a few hundred).
  The next requests' messages are found there (the response being written, the
  tool results after it); the window is parsed again at each reload;
- per instance its message's hash (`ih`) and blocks' content keys (`keys`);
  per call its shapeHash, request model and kept count (`csh`, `cpm`,
  `ckept`, `cextra`): with the agent's public calls and instances this is the
  build the next requests extend;
- the request shapes (`shape_raw`), the responses before the window as
  metadata (`old`: their request ids, last usage, model, block keys), the
  thinking drops, the stale-read maps (`rt`, `cwd`), the title state, each
  result's words for `links_of` (`wix`, `rw`);
- what decides whether appended data can be applied (below): digest sets
  `old_only` (store entries from before the window), `tail_seen` (hashes calls
  looked up, with how each message was written: `rep`, the hash of its
  objects' key order), `unresolved` and `found_else` (and the files those were
  found in, `srcs`), the block keys and request shapes (`shape:` + its hash)
  a lookup missed (`block_missed`), the responses items took weights from
  (`used`), the window's responses with their record counts (`settled`: one
  with the same count is the one the calls were built with, as the window's
  bytes are checked unchanged).

**A reload** (`restore`, `read_new`, then the same build as a load):
`restore` copies the previous agent's items, texts, instances and calls into a
new build (every string taken into the new agent's pools; the previous agent
is not touched, and may still be shown or reloaded again) and its loader;
`read_new` reads the window back from the file (it must equal the kept
one) and the appended bytes (`restore` reset each result item's link label:
`post` links it again); the store is built over
them, with the earlier responses' metadata first; `build_calls` takes the
requests after the last one taken; every call's usage, model and dropped
thinking, then enter and leave, stale reads, links and the derived values are
computed again for all calls (cheap: no JSON is parsed for them but the
actions' small inputs); `save_loader` moves the window to after the last
response before the new last request, store entries and responses left
behind into `old_only` and `old`.

**A full load instead** when the appended data can't be applied to what was
built, as the draft would then build it otherwise (`b->fail`, printed by
`AIG_DEBUG=1`): the file replaced (inode), shorter, or rewritten before the
end taken (`chk`), or inside the window; another root, home or config; a last line without its
newline that parses; the agent had messages found nowhere (another file may
hold them now), or a file a message was found in shrank or was replaced; a
request needs a message only records before the window hold (`old_only`); a
request id seen before; a request shape defined again with other content, or
written after a call that needed it; a message written again in or after the
window, written differently from the earlier one (`rep`), when an earlier
load had it before the window; a blob written again for a hash a call looked up, written differently (key
order: `rep`), or for a store entry before the window; an appended response
that grows one items took their weights from, or whose hash or block keys a
call looked up and missed before; an appended message a call found nowhere or
in another file; a record of a response from before the window. Estimated
agents (no request records) always load in full.

**Equivalence**: after any sequence of appends a reload equals a fresh load:
its `ctx_json` (with every call's derived values), every field of every item,
call, instance and of the agent the UI reads, and the item of every key
(`tests/ctx_reload.c`, driven by `tests/context_reload.py`). In `make test`,
plain and under ASan: at every line, inside every line and at seeded random
cuts of each fixture transcript (a response's records arriving across
reloads), none falling back but for a last line without its newline; then
fixed cases, files edited between reloads, each pinned to the fallbacks it
must take in order (none for a case that must stay incremental), one or more
per guard. Two guards have no case: the file failing to open after `load`
found it, and the window's records short of the kept count, which only a
cancel reaches now that the window's bytes are compared. By hand, `tests/context_reload.py --transcript PATH` at random cuts of a
copy of a real transcript.

## 7. The screen

`src/cview.c` (the screen, input, loads), `src/cwin.c` (the floating windows)
and `src/clay.c` (the rules that draw nothing, unit-tested headless by
`tests/test_clay.c`), with the window layout in `src/slayout.c` (`lay_wins`).
The draft page (`context.tpl.html`) is the reference; where the port differs,
it says so below.

**Opening.** `X` on the agent under the mouse (else the turn window's), or a
turn window's **context** button: the actor's transcript is its events'
`Event.src` (else `ctx_find` by its id), opened at the stage's moment
(`cv_open`). One `CtxProject` lives as long as the screen, and one job runs on
the worker thread at a time (`worker`): a load, or a reload of the agent shown;
a finished job is swapped in at the start of a frame (`swap_in`). An open on
the agent already loaded loads nothing (it goes to the call or action asked
for: `go_to`), one already loading is kept, one for another agent drops the
job in flight (`cl_open_job`). Closing cancels nothing: a load or reload that
finishes meanwhile is taken in at the next open. What a finished job does is
`cl_job_done`'s: shown (a new agent), kept (a reload, or the agent shown
loaded again: below), dropped (another agent was asked for), dropped and
retried (a cancelled reload: the transcript still counts as changed), or a
failure. While another agent loads the screen says so (no bar of the old
one); a failed load, or a transcript with no API calls yet, shows "No context
to show: …" (and a `--shot` takes that); a failed reload keeps the agent.
`--context AGENT[:CALL[:ACTION]]` opens it for shots (the numbers taken from
the end; without CALL the last call), not following the stage.

**What a call shows** (`show`, the page's `show()`): its order, then what its
response made that isn't in it (`cl_shown`; drawn dashed, "this call's
response"), each item once. `ctx_positions` maps the call once. Open or chip
(`cl_open_default`, `cl_open`): the user's choice when there is one (a click on a
chip, the fold button), else open when it is the selected action or one of its
likely causes, or the call's response, or (nothing selected) it entered at this
call or the one before; never the system prompt or tools; at most
`CL_OPEN_MAX` (10) of the default-open ones that aren't linked, this call's
response then the newest kept. Sizes as the draft's `sizeOf` (`cl_size`); a
resized window keeps its size.

**Layout** (`simulate`, `lay_wins`; the draft's `simulate`, `targets`, `seed`,
`rectCollide`): ranks (`cl_targets`) put this call's response first, then the
linked, then the newest entered, then the call's order; each window's target
distance is the radius of a ring holding the area of everything ranked before
it (`sqrt(92² + (area + a/2) / (0.4π)) + h/4`). A new window starts at a free
spot near its target distance (rings of 24 px from it, alternating sides of
an angle from its item index, a grid of 256 px cells), then d3-force's radial
force (strength 0.12) and the rect collide (10 px apart; a pinned window
doesn't move) run for 120 ticks on a fresh call (24 when windows are kept, 6
plus 12 settling passes while scrubbing fast, the full pass 0.18 s after the
scrub rests), alpha 0.35 to 0.001, velocity decay 0.5; then settling passes
move the later ranked of two overlapping windows outward on the axis that
needs less, until none overlap. A dragged window is pinned where it was put
(and while in hand). Windows move to their new places over 0.45 s; new ones
fly out of the agent; leaving ones fade, fold shrinking into a compaction's
summary, cleared thinking greys out over twice as long. Calls shown less than
0.15 s apart don't animate.

**A window** (`cw_draw`, `body_build`): the title bar (kind, title with
"error · " before a failed result's, ⚠ when stale by a change not its own,
tokens; "why" on an action, fold), the facts in the draft's words
(`cl_facts`, `cl_meta`: tokens and share, or "out · this call's response";
"in #N HH:MM"; "age N calls · M"; "cache read/written/new"; "$x carried" for
priced models; "stale · known" or "stale ⚠", the changes in its title; "dropped by
the API at this call"), the content: a Read's lines in a `CodeView` with their
own numbers (a click: `nv_open` on the file, `item_file`), a Write's content
numbered from 1, an Edit as - and + lines, a command as `$ cmd` and its
description, results and other text as wrapped mono, prompts, text, summaries
and thinking as the draft's light markdown (a paragraph per line, headings,
lists, fences, inline code and bold), tool definitions as expandable names.
Content is laid out once per width as rows (a resize re-wraps text and keeps
the code view) and only the rows in view are drawn; only windows in view are
drawn, and get their facts, once per call. Below 4 px on screen text isn't
drawn, and below 1.5 px corners aren't rounded.

**The rest** of the draft: the bar (agent picker, estimated badge, recent and
all fits, the stats: context and window with a meter, cache hit at the call
and so far, items, call N/M, no-response badge), the right panel (the
composition by kind, `cl_by_kind`; why it decided; the actions, `cl_actions`,
filtered by `cl_match`), the strip (`cl_strip`, `cl_strip_hit`: d3's
`scaleBand`, padding 0.15 up to 300 calls; "#N HH:MM · tokens · +in −out",
`cl_inout`), links (dashed quadratics from the agent, labelled "likely · why",
the action's own "action"), pan and zoom (0.05 to 2.5, the wheel zooming about
the mouse unless over a window's content, which it scrolls). Switching agent
(`switch_agent`) keeps following or not as it was: following, the other shows
the stage's call; not, the call of the same moment, or its last when at the
end; the selection is cleared.

**Clock and live** (C3). The screen follows the stage's clock (`cv_clock`,
`cl_follow`: the last call at or before it, `cl_call_at`, by the latest time up
to each call, so a call recorded out of time order doesn't stop it early)
until a scrub, a step, play or an action detaches it: play is the view's own
run through its calls. `L` or the bar's "detached" follows again; a selected
action stays selected, its panel saying "selected action is from call N" when
the call shown is another. Before the agent's first call the screen says "No
call yet at this moment" (and when the first is). While open, the agent's
transcript is looked at every 0.5 s (size and modification time, `live`);
once it has stopped growing for 3 s, or has waited 15 s, or was last written
more than 3 s ago (a reopen after it grew: at once), the worker reloads it (`ctx_reload`, `start_reload`) and `install` swaps it in keeping, by
item key (`cl_key_map`, `cl_carry`), what the user did (opened or folded, where
windows were dragged, their sizes), the windows on screen (`carry_windows`:
place, size, scroll, content laid out), the selected action, the filter, the
view and a pan, scrub or drag in progress; the call is
the stage's when following, else the same. A failed reload keeps the agent
("update failed" in the bar) and is retried when it grows again. `AIG_DEBUG=1`
prints each reload's time and what it kept; `AIG_CTX_BENCH=N` scrubs the last N
calls and prints the layout and frame times.

On this repo's largest agents: the 738291ad session (1,040 items in its
last call) lays a call out in about 10 ms while scrubbing and draws a frame in
15–20 ms on software GL (llvmpipe), 8 ms with all 1,040 windows in view; a live
reload of the 80 MB main transcript (688 calls, 2,774 instance keys) takes
about 50 ms on the worker (`ctx_reload` reads only what was appended; it
took about 2 s when it read the whole file) and 9 ms to swap in at a frame's
start.

## Where each rule lives

| Draft (`build_ctx.py` / page) | C |
|---|---|
| `H` | `pyj_dumps(PYJ_CANON)` + `sha256_*` (pyjson.c), `blk_hash`, `hash_rebuilt` |
| `records`, `responses`, `response_message` | `read_records`, `find_responses`, `resp_blocks` |
| `store`, `flush` | `store_build`, `flush_run` |
| `Agent.resolve` | `resolve` |
| `Agent.build_exact` | `build_exact` |
| `Agent.build_estimated` | `build_estimated` |
| `Agent.blocks_of`, `finish_item` | `blocks_of`, `emit`, `finish_item` |
| `free_items` | `Free`, `free_add`, `free_pop`, `free_remove` |
| `Agent.post` | `post` |
| `usage_of` | `usage_of` |
| `split_text`, `inj_source`, `INJ_RE` | `split_text`, `inj_source` |
| `action_label`, `one_line`, `rel`, `lang_of`, `img_info` | same names |
| `project_changes` | `project_changes`, `scan_file`, `fc_record`, `use_edits` (cached in `CtxProject`) |
| `WRITE_CMD`, `PY_WRITE`, `QUOTED_FILE` | `write_cmd`, `py_write`, `quoted_files` |
| `cmd_write_files`, `cmd_read_files`, `cmd_paths`, `file_like`, `segments`, `split_unquoted`, `absolute` | same names; `shlex.split` is `shlex_split` |
| `stale_events` | `stale_events`; at a call: `ctx_stale_at` |
| `links_of`, `WORD`, `STOP`, `same_file` | `links_of`, `find_words`, `STOP_WORDS`, `same_file` |
| `session_title` | `session_title`, `title_of_file` |
| `agent_json`, `build` (output) | `ctx_json` |
| `PRICES`, `PRICE_SOURCE`, `WINDOWS` | `ctx_prices`, `ctx_price_source`, `WINDOWS` / `ctx_window` |
| page: `prepare` | `derive`, `ctx_tokens` |
| page: `facts` (stale, known), `posMap` | `ctx_stale_at`, `ctx_positions`, `ctx_pos` |
| page: `madeAt` | `CtxCall.made` |
| page: `body` (a Read as code) | `ctx_code_lines`, `CtxItem.line0`/`line1`/`lang`; drawn by `body_build` (cwin.c) |
| page: `show`, `isOpenDefault`, the cap of 10 | `show` (cview.c), `cl_shown`, `cl_open_default`, `cl_open` |
| page: `sizeOf`, `header`, `facts`, `metaHtml` | `cl_size`, `cl_title`, `cl_facts`, `cl_meta`, `cl_stale_text` |
| page: `targets`, `seed`, `simulate`, `rectCollide` | `cl_targets`, `lay_wins` (slayout.c: `seed`, `collide_rects`) |
| page: `drawStrip`, `scrubAt`, `drawStripCursor` | `draw_strip`, `cl_strip`, `cl_strip_hit`, `cl_inout` |
| page: `drawSide`, `fillActions`, `drawLinks`, `drawAgent` | `draw_side`, `cl_by_kind`, `cl_actions`, `cl_match`, `draw_links`, `draw_agent` |
| page: `fit`, `centerOn`, the zoom | `fit`, `center_on`, `cam_to`, `wheel` |

## Known limits

- The draft fixed for the port: its links depended on Python's set order
  (hash-randomized per run) where a command names several files, and for the
  order of equally rare names; both now sort (paths by name; names by rarity,
  then name), in `build_ctx.py` too.
- Change detection for commands is the draft's pattern heuristic: a command
  can write files in ways no pattern sees, so some stale reads are missed.
- Stale reads need the changes of the whole project: the first load scans
  every transcript of the project (later ones only what changed).
- A reload reads only what was appended, but recomputes for every call what
  depends on all of them (enter and leave, stale reads, links, the derived
  values: about 35 ms on the largest agent), and copies the previous agent
  (it may still be shown). Some appends can't be applied and load in full
  (section 6a): on this repo's largest transcript, cut before each of its
  last 120 requests and inside each response, 214 of 215 reloads were
  incremental (median 53 ms) and one loaded in full: a full resend (no base)
  whose tail has a message (a `<total_tokens>` reminder) only records before
  the window held. Appends of several MB at once fall back more often (the
  same reason), still equal to a fresh load.
- Messages found nowhere make every reload of that agent a full load
  (another file may hold them since).
- A reload trusts that the transcript only grows: a file rewritten with the
  same inode, as long, and the same last 256 bytes before the end taken is
  taken as grown.
- Transcripts are assumed compact JSON for the change scan's line filter
  (`"name":"Bash"`, `"tool_use_id":"..."`), as Claude Code writes them.
- Tokens are estimates scaled to each call's real total: the split between
  items is by size, not by the tokenizer.
- The screen against the draft: no session picker (one session, opened from
  the stage); no "show all N lines" (every line is there, drawn as it scrolls
  into view); fenced code in markdown wraps; no italic; the code's colours are
  the app's; windows fly and fade without growing from small; a dragged or
  resized window goes to the front and stays there; positions live as long as
  the agent is loaded (the draft kept them in the browser), across reloads by key.
- An item whose message was rewritten (a response cut off mid-way when it was
  read, completed by the next read) has a new key, so its window starts over.
