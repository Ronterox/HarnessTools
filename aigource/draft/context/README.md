# Context view draft

The design draft of aigource's context view (spec: `PLAN.md`), as one HTML page
built from real Claude Code transcripts: one agent in the middle, every item of
its context window as a floating window around it, at every API call of the
session.

```sh
draft/context/build.sh                                  # out/context.html: this repo's most recent session
draft/context/build.sh /tmp/x.html ~/code/app SESSION_ID  # one session of another repo
CTX_MAX_MB=0 draft/context/build.sh out/all.html "" --all  # every session, no size limit (or --recent N)
python3 -I draft/context/build_ctx.py --check draft/context/out/data.json
```

`build_ctx.py` writes `out/data.json`; `context.tpl.html` is the page (d3 and
highlight.js from cdnjs), with `__DATA__` replaced by the data. Output goes to
`out/` (gitignored): it embeds prompts, source and command output. The build
keeps the data under 15 MB (`CTX_MAX_MB`, 0 for none): newest sessions first, each main agent
before its subagents; what does not fit is left out and listed on stderr.

## How the data is rebuilt

- **Requests.** Each `api-request` record is one call: its messages are its base
  call's (`base`) first `keep` messages + `tail`, hashes of messages. A request
  without `base` (after a compaction, or a full resend) starts over; it is
  compared with the call before it, so messages sent again unchanged keep their
  items. System and tools come from `api-request-shape` by `shapeHash`.
- **Blobs.** A hash is sha256 of the message as compact JSON with sorted keys
  (`ensure_ascii` off). Messages the transcript already holds are not written as
  `api-request-blob`, and are rebuilt and hashed the same way: an assistant
  response (all records of one `message.id`, content concatenated), a user record,
  and runs of consecutive user records sent as one message (content as a list).
  A hash found in none of these is looked up in the project's other files.
- **Request → response.** Assistant records carry `requestRef`, the `id` of the
  `api-request` they answer. A call's real usage is that response's
  `input_tokens + cache_read_input_tokens + cache_creation_input_tokens`. Requests
  without a response (interrupted, failed) are drawn hollow; their tokens use the
  nearest call's scale.
- **Items.** One per content block (a tool call, a result, a text, a thinking
  block), with injected text split out of the user message it rides in:
  `<system-reminder>` segments (CLAUDE.md, memory, hook output, reminders),
  slash-command and token-count blocks, `role: system` messages (hook context,
  added tools), and text sent with tool results. An item lives from the call it
  entered to the call it left (`e`, `l`); a message sent again unchanged keeps its
  items, and a block whose message was rewritten keeps its item when the block
  itself is the same. System and tools are items per shape; unchanged system
  blocks keep theirs.
- **Tokens.** Each block's size: characters / 4; an assistant response's blocks
  from its own output tokens (thinking blocks get its thinking tokens, the rest
  share the remainder by size); images w×h/750 after the 1568 px resize. Each call
  scales its blocks so they add up to its real usage. Thinking blocks the API
  dropped (`thinking_drop` attachments) count 0 at that call.
- **Cache state and carrying cost.** Where the block falls in the prompt (tools,
  system, messages) against the call's cache-read and cache-write counts: read,
  written, new. Carried: what holding it has cost up to the call shown, at the
  rate of its state at each call (cache read, cache write 5 min or 1 h in the
  call's mix, or input). Prices: `PRICES` in `build_ctx.py`, from the claude-api
  skill (Claude Code 2.1.306, model table cached 2026-10-06); other models get
  tokens only.
- **Stale.** For a Read, or a read-only command naming files (`cat`, `sed -n`,
  `head`, `tail`, `grep`…): changes to the file after the read, while it stayed in
  context. Changes are Edit/Write calls of every agent of the project, and files
  commands write as far as patterns see (redirect targets, `sed -i`, `tee`, `rm`,
  `mv`, `cp` target, `git checkout`… naming them, python heredocs that assign or
  open a file and write), paths resolved from the record's `cwd` and any `cd`.
  Also files Claude Code noticed changing (`edited_text_file`) when no command
  wrote them just before. *Known* when the change is the agent's own Edit, Write or
  command and that call is still in its context; ⚠ otherwise.
- **Why it decided.** For an action (a tool call), in the context of the call
  that made it: reads of the file it acts on (the latest two per file), the
  prompt of the turn, its text and thinking just before in the same response,
  and up to three results sharing a rare name with its input.
- **Estimated sessions** (no request records): one call per assistant response;
  its messages are the conversation records since the last compaction
  (attachments with text as injected items); system and tools are one item, what
  the first call's usage leaves after its messages.

## The page

The agent is in the middle; distance follows age (newest closest: each ring
holds the area of everything newer), angle is free. A new window starts at a free
spot near its radius, then d3-force (a radial force, a rect collide) and a
settle pass where the older of two overlapping windows moves outward. A dragged
or resized window stays where it is put (kept in the browser's localStorage).
The last two calls' items, the call's own response and an action's likely
causes start open; the rest are title-bar chips. The strip at the bottom has one
bar per call (height: context size, red: a compaction); drag to scrub, `space`
plays. Only windows in view are displayed and updated.

Hash parameters, for links and headless shots: `s` (session id prefix), `a`
(agent index), `c` (call), `sel` (action item), `z` / `dx` / `dy` (a fixed zoom
on the agent), `next=MS` (one animated step after MS), `bench=N` (scrub the last
N calls and write the timings into the page).

## Checks

`build_ctx.py --check DATA.json` rebuilds every call the way the page does and
reports: per-call token sums against real usage (rounding), items entering or
leaving against `keep`, missing blobs, the spread of the scale (real tokens per
estimated token).
