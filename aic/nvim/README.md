# aic.nvim

Neovim client for **aic** (see `../DESIGN.md`, `../PROTOCOL.md`). Pure Lua, no
dependencies, Neovim 0.10+. It only reads files under `.aic/` and runs the
`aic` CLI; it never talks to Claude directly.

## Install

lazy.nvim (local dir):

```lua
{
  dir = '/path/to/aic/nvim',
  config = function()
    require('aic').setup({
      -- bin = '/path/to/aic/bin/aic', -- default: <aic>/bin/aic next to this dir
      -- prefix = '<leader>z',         -- default prefix
      -- keymaps = true,               -- install the default keys below
      -- winbar = true,               -- usage winbar on the aic views
    })
  end,
}
```

`plugin/aic.lua` only defines commands and `<Plug>` mappings. `setup()`
installs keymaps and the autocmds that place signs and record your code saves.
It also activates aic in the background (one `aic status --json`, no prompts) when
the cwd or an opened file is inside a project that has `.aic/`. Commands call
`setup({})` themselves if you haven't.

## Concepts

- **Project root**: the nearest ancestor that contains `.aic/`. The session dir
  comes from `aic status --json` (`dir`), and is re-resolved when
  `.aic/` or `.aic/workers/` changes, after `:AicAttach`, and on `R`.
- **One window, one tab.** aic's views are buffers shown in the current
  window: **history** (`aic://trace`), **questions** (`aic://questions`) and the
  **docs** (`.aic/docs/*.md`). aic never creates a tab or a split and never opens the
  quickfix window. Jumps to code (`<CR>`/`gf` in the history, `gd` on anchors)
  open in the same window; `<C-o>` / `<C-^>` bring you back. Window options a
  view needs (folds, wrap, winbar) are set buffer-locally, so code buffers
  shown in that window keep their own. Floats (answers, usage, blame) are the
  only extra windows; jumping from a float closes it and uses the window under it.
- **`:Aic`** shows the last aic view (default: history) in the current window.
  `<prefix>z` toggles between it and the last code buffer. If `.aic/` is
  missing it offers to run `aic init`. It starts `aic watch` for the project root unless
  `.aic/watch.pid` is a live process (one watcher per root, so opening `:Aic`
  in another project starts one there too). Every watcher it started is
  stopped on exit.
- **Watching**: `vim.uv` fs_event on the session dir (falls back to 500 ms
  polling), debounced 150 ms. Changes reload the trace, seen state, signs,
  statusline, questions, usage winbars, and (when a `diagnostics.<doc>.json` changes) that doc's diagnostics and the quickfix list.

## Keymaps (default prefix `<leader>z`)

| key | command | `<Plug>` |
|---|---|---|
| `<prefix>z` | `:AicToggle`: in an aic buffer, back to the last code buffer; elsewhere, the last aic view | `(aic-toggle)` |
| `<prefix>t` | `:AicHistory`: the history (trace) in this window | `(aic-history)` |
| `<prefix>q` | `:AicQuestions`: the questions (compiler chat) in this window | `(aic-questions)` |
| `<prefix>d` | `:AicDocs`: pick a doc (newest first) or `+ new doc`; shown in this window | `(aic-docs)` |
| `<prefix>D` | `:AicDocNew [name]`: new doc | `(aic-doc-new)` |
| `<prefix>s` (n, x) | `:AicSubmit`: submit the current doc through a prompt template, with a preview (see [Submit](#submit)); with a visual range (in a doc), just those lines | `(aic-submit)` |
| `<prefix>S` (n, x) | `:AicSubmitNow`: same, sent with `--now` (interrupts the running tool) | `(aic-submit-now)` |
| `<prefix>p` | `:AicPrompts`: edit a prompt template or a compiler template (`compiler/NAME`; a built-in is copied to the project or user dir first) or make one | `(aic-prompts)` |
| `<prefix>h` | `:AicHold` | `(aic-hold)` |
| `<prefix>c` | `:AicContinue [msg]` | `(aic-continue)` |
| `<prefix>?` | `:AicAsk`: ask about the thing under the cursor (any buffer) | `(aic-ask)` |
| `<prefix>A` | `:AicAskTemplate [name]`: pick the compiler template that questions and ask-at-cursor use (`ask` = the default; see [Compiler templates](#compiler-templates)) | `(aic-ask-template)` |
| `<prefix>w` | `:AicWhatIf` | `(aic-whatif)` |
| `<prefix>b` | `:AicBlame`: trace event for the touched range under the cursor | `(aic-blame)` |
| `<prefix>a` | `:AicAttach`: pick the worker session (or auto) | `(aic-attach)` |
| `<prefix>e` | `:AicDiagnostics`: load the last compile into the quickfix list (no window: `:copen`/`]q` are yours) | `(aic-diagnostics)` |
| `<prefix>f` (n, x) | `:AicFix`: ask the compiler for edits to the current doc that fix its diagnostics, then pick them in a float (see [Fixes](#fixes)); with a range, only the items about those lines. `:AicFix!` shows the last proposal again | `(aic-fix)` |
| `<prefix>u` | `:AicNextUnseen`: show the history and go to the next unseen node | `(aic-next-unseen)` |
| `<prefix>U` | `:AicUsage`: context/cost per session, account quota, compact/reset (see [Usage](#usage)) | `(aic-usage)` |
| `<prefix>]` / `<prefix>[` | `:AicNextTouched` / `:AicPrevTouched` | `(aic-next-touched)` / `(aic-prev-touched)` |
| `<prefix>g` | follow the anchor under the cursor, in any buffer (see [Anchors](#anchors)) | `(aic-follow)` |

Other commands: `:Aic` (show the last aic view here), `:AicDoc [name]`, `:AicCompile`,
`:AicChat <text>`, `:AicPrevUnseen`, `:AicRefresh`, `:AicCompact compile|ask|worker`,
`:AicReset compile|ask`. `<Plug>(aic-open)`,
`(aic-compile)` and `(aic-prev-unseen)` also exist.

`<leader>a` is never mapped.

### Buffer-local keys

**Trace** (`aic://trace`, filetype `aictrace`):

| key | action |
|---|---|
| `<CR>` | on a turn, or a node with details (why, diff hunks): show/hide them (showing marks it seen). On a hunk line or a node without details: jump to its file:line |
| `gf` | jump to file:line of the node or hunk line (marks the node seen) |
| `za` `zo` `zc` … | normal folds (turns open, details hidden); what you open or close survives new events |
| `]u` / `[u` | next / previous unseen node |
| `m` / `M` | toggle seen on the node / mark all seen |
| `a` / `W` | ask / what-if at cursor |
| `R` | re-resolve the session and reload everything |

Glyphs: `●` unseen, `○` seen, `✎` human save, `⇢` submit, `⏸`/`▶`
hold/release, `◆` end-of-turn text, `⇣` the worker's context was compacted
(`⇣ compacted 107k → 10k (manual)`, `AicCompact`). Only worker `tool` and
`text` nodes count as unseen. Seen state is saved to `seen.json` (tmp file + rename).

**Docs** (`<root>/.aic/docs/<name>.md`, real files, shared by every worker
session): write as many as you like; `:AicDoc [name]` shows one (default: the
current doc, else the last one entered, else `spec`; completes names),
`:AicDocNew [name]` makes one. The **current doc** is the doc in the current
buffer, else the last doc you entered: submit, ask, `:AicCompile`,
`<prefix>e` and the statusline act on it. Rename/delete docs as plain files.
- A doc compiles on `:w` only if its first line is `<!-- aic: compile -->`
  (`prompt=<template>` may follow, for the submit picker). New docs ask
  (config `new_doc_compile = 'ask' | true | false`; a doc made implicitly,
  e.g. the first `:AicDoc`, gets the marker unless it is `false`).
- If a compile is already running, the backend queues one more pass of that
  doc (`aic: compile queued`; the statusline shows `compiling+queued`). When
  it finishes, the quickfix list (title `aic compile <doc>`, never opened) and
  inline `vim.diagnostic` (namespace `aic`) are updated, and a one-line
  summary is shown. Each doc has its own diagnostics.
- Anchors are highlighted, `gd` follows them and `<C-x><C-o>` completes them
  (see [Anchors](#anchors)).
- The first time a project gets a docs dir, `aic docs` runs first so the old
  per-session `spec.md` is migrated to doc `spec`.

**Questions** (`aic://questions`): shows `chat.md` followed by a `## You` line. Type
below it and press `<CR>` in normal mode to send with `aic ask`. The plugin never
writes `chat.md`; the view reloads when the backend appends to it, and keeps
what you've typed. Anchors in your input are sent as-is (the backend resolves
them); `gd`, highlighting and `<C-x><C-o>` work as in docs. Questions are
about the current doc (`aic ask --doc`).

**Code buffers** with touched ranges: `]w` / `[w` go to the next / previous
touched range. Signs: `▎` in `AicWorker` (worker) or `AicHuman` (you). Signs
are drawn only within 200 lines of what each window shows (redrawn on
scroll), and a `touched.json` change only re-applies buffers whose ranges
changed, so big writes stay cheap.

**Ask / what-if float**: `q`/`<Esc>` close. Action keys are uppercase so
`y`, `p`, `f` and the other motions still work for copying from the float.
Every action closes the float first and acts in the window under it. `P`
appends the answer to the end of the current doc under `## Notes` (ask). `F`
adds it under `## What-if: <question>` (what-if). If the doc is open, the text
goes into the buffer and you save it yourself. In both floats:

- `S` appends the answer's first fenced code block (or the whole answer if it
  has none) under `## From compiler`, saves the doc, and opens the `selection`
  template's preview of those lines in the window under the float (`:w` sends).
  `S` appends once per answer (also after `:AicLastAnswer`); pressing it again
  only says the block is already in the doc (select it and `<prefix>s`).
- `Y` yanks the same block (linewise) into the unnamed register.
- `gd` follows anchors in the answer; they are highlighted.

The footer lists the keys.

## Compiler templates

What the compiler is told for compile, ask, what-if and fix is a template too:
`aic/prompts/compiler/{compile,ask,whatif,fix}.md`, overridden by a file of the
same name in `.aic/prompts/compiler/` (project) or
`~/.config/aic/prompts/compiler/` (user). Format and variables: PROTOCOL.md
"Compiler templates". `:AicPrompts` lists them as `compiler/NAME`. Its
`+ new compiler template` starts from a copy of the current `ask` or
`compile` template.

- **Ask styles**: any template with `task: ask` (for example `eli5`, or
  `critique` with `model: opus`). `:AicAskTemplate` (`<prefix>A`) picks one
  for the questions view and ask-at-cursor, and the questions footer shows it
  (`## You  template: eli5`). What-if always uses `whatif`.
- **Per-doc compiles**: `<!-- aic: compile=plan -->` compiles that doc with
  template `plan` (`task: compile`), for example a plan reviewed for missing
  steps instead of checked against the worker. The diagnostics format stays
  the same.
- A template places the data but never drops it: whatever it leaves out
  (doc, activity, …) is appended after it.
- Whenever a compile used a template that is not the built-in `compile`, the
  compile notice names it (`aic compile notes [plan, user]: …`), also when
  Neovim starts. A project one that hides your user template of the same name
  says `shadows user`. A project template you didn't write is worth a look.
  The hook refuses the worker's writes there, but a shell can always find a
  way.
- A project template's `model:` is ignored and its `@anchors` stay inside the
  project. Put a template in `~/.config/aic/prompts/compiler/` for those.
- `aic compile --print` (also `ask`, `fix`) shows the exact prompt without
  calling the compiler.

## Fixes

The compiler only suggests. `:AicFix` (or `<prefix>f`) is the one place where
you ask it to write the change, and even then it writes nothing itself. It
proposes edits to the current doc that resolve that doc's diagnostics, and you
pick which ones aic applies to your buffer. The compiler sees your unsaved text.

```
( ) 1. Name the exception that fetch retries on  doc:4
      [incoherent] 'errors' is vague: which errors are retried?
      why: src/retry.py:7 catches only IOError.
(•) 2. Describe exponential backoff instead of 1s  doc:5  overlaps 3
( ) 3. Drop the wait line  doc:5  overlaps 2
(✓) 4. Title it  doc:1  applied
( ) 5. Say what happens after the last try  doc:6  stale: its text is no longer in the doc

– not a doc fix: #6 the code raises instead of returning None — the worker is wrong here
```

| key | action |
|---|---|
| `<Space>` / `x` | pick / unpick the fix under the cursor. Fixes that change the same text work like radio buttons: picking one unpicks the others |
| `<Tab>` | show / hide its edits as a diff |
| `a` | pick every fix that can be picked (no two that overlap); again: pick none |
| `<CR>` | apply the picked fixes to the doc buffer and show it: all or none, in one undo step (`u` undoes them all). Nothing is saved |
| `q` / `<Esc>` | close (`:AicFix!` reopens; picks are kept) |

- Some fixes can't be picked, and the list says why:
  - stale: its text changed since the proposal (ask again with `:AicFix`)
  - its text is already in the doc (for example, applied before a restart)
  - its alternative N is applied
- `doc:N` and `@@ line N` show where the text is now, not where it was when
  the compiler proposed it.
- Applied fixes stay marked, also after you edit their text. If you undo
  their apply (`u`, even if you edit more afterwards), they can be picked
  again.
- The diagnostics a fix resolved are hidden (inline and in the quickfix list)
  until the next compile, which reports them again if they still hold.
  Undoing doesn't bring them back.
- Items the compiler would not fix in the doc (the worker is the one that is
  wrong, or it proposed nothing) are listed at the bottom with the reason.
- Proposals are made by the compile session (env `AIC_FIX_MODEL`, default the
  compile model). One `:AicFix` costs about one compile.

## Submit

`<prefix>s` (`:AicSubmit`, a range in a doc for just those lines):

1. Picker of prompt templates (`aic prompts --json`): `[source] name — description`
   (source first, `overrides built-in` when a project/user template shadows
   one; the description is capped, so it can't push the source out of view).
   First the doc's marker `prompt=`, then the template last sent for this doc,
   then the rest by name; broken templates last, with their error.
2. A template that asks for input (`{{input}}`) prompts for it (`vim.ui.input`).
3. `aic render --json` → the **preview** (`aic://preview`) opens in the current
   window: line 1 is the header that will be **typed** as you, the rest is the
   **pasted** body; a virtual line between them says `queued` or `send now`.
   Edit anything. Each digit, `@`, `\` or invisible character (zero-width,
   bidi, Unicode tags…) in the header is highlighted, non-ASCII ones get a
   `⟨U+…⟩` label, and `:w` refuses (the backend checks again). Editing the
   body doesn't change what is recorded as sent for the doc (a `marks_sent`
   template records the doc as rendered).
4. `:w` sends exactly what you see (`aic submit --rendered`) and returns to the
   buffer you were in; on an error the preview stays. A second `:w` while
   sending is ignored. Leaving the preview
   (`<C-^>`, `:b`, `<prefix>z`) closes it and sends nothing. No normal-mode
   keys are mapped there.

Templates: PROTOCOL.md "Prompt templates". `:AicPrompts` lists them (+ new
template from a skeleton); built-ins are read-only, so picking one copies it
to `.aic/prompts/` or `~/.config/aic/prompts/` (same name overrides it).

## Anchors

Anchors follow PROTOCOL.md "Anchors". An anchor starts with `@` at the start
of a line or after whitespace or one of `( [ { " ' ,` (so `a@b.com` is not
one). Trailing `.,;:)]}` is not part of it. Paths are relative to the project
root; a path anchor only counts if the path exists.

| anchor | `gd` / `<prefix>g` |
|---|---|
| `@path/file.ext` | open it (in the current window) |
| `@path/file.ext:42` | open at line 42 |
| `@path/file.ext:10-20` | open at 10, visually select 10-20 (`'<` `'>` set) |
| `@path/file.ext:symbol` | open at the first definition-ish match (`function symbol`, `def`, `class`, `const`, `let`, `symbol =`, `symbol(`), else any word match |
| `@path/dir/` | `:edit` the directory (netrw/oil) |
| `@/abs/path…` `@~/path…` | the same forms, outside the project |
| `@diff` / `@diff:path` | `git diff HEAD [-- path]` in a scratch `diff` buffer |
| `@staged` | `git diff --cached` in a scratch `diff` buffer |
| `@log` / `@log:path` | text from `aic anchor --json` in a scratch `git` buffer |
| `@show:REV` | same (`git show REV`) |
| `@turn:N` / `@turn:last` | open the trace on that turn's header |

- `gd` is buffer-local in docs, questions and the answer float; off an anchor it
  calls the `gd` mapping that was there before (e.g. an LSP one), else the
  normal `gd`. `<prefix>g` works in any buffer.
- Valid anchors are highlighted with `AicAnchor` (default link `Underlined`),
  refreshed on edits and scrolling (debounced): the whole buffer under 2000
  lines, else the visible lines. The questions buffer always highlights only the
  visible lines ±50.
- `omnifunc` (docs and questions) completes after `@` with `<C-x><C-o>`: project files and
  directories (`git ls-files -co --exclude-standard`, else a walk capped at
  5000 entries skipping `.git` `.aic` `node_modules`), `diff`, `diff:<path>`,
  `staged`, `log`, `log:<path>`, `show:HEAD`, `show:HEAD~1`, `turn:last` and
  `turn:N` for turns in the trace. Prefix matches come first, then substring matches.

## Usage

Reads `<session>/usage.json` (watched with the other session files) and the
account quota from `aic usage --json` (PROTOCOL.md "Usage"). The quota is
fetched asynchronously at most once a minute while an aic view is visible, plus
shortly after `usage.json` changes; a `null` quota is simply not shown.

**Winbars** (set buffer-locally when a view is shown, so they go away with it;
cached strings, no IO while drawing; `winbar = false` turns them off). Each
view shows the session behind it:

| view | example |
|---|---|
| doc | `compile · sonnet  ctx:[####------] 82k/200k (41%)  12 runs  $1.20` |
| trace | `worker · sonnet  ctx:[##--------] 36k/200k (18%)  turn 21` (window unknown: `worker · sonnet  ctx 107k (?%)  turn 21`) |
| questions | `ask · opus  ctx:[######----] 126k/200k (63%)  9 turns  $2.85   5h:57% wk:15%` |

- Model names drop `claude-` and the version (`claude-opus-5-5` → `opus`).
- The bar is 10 cells, followed by tokens used / window size and the percent;
  all of it uses `AicUsageOk` (< 70%), `AicUsageWarn` (≥ 70%) or
  `AicUsageHot` (≥ 85%). Until the window size is known (learned on the
  session's next call) only the tokens show, with `(?%)`.
- The questions winbar appends the account quota: `5h`, `wk`, then scoped buckets
  (`fable:3%`) only when above 0% or active. A quota older than 15 minutes is
  prefixed with `~` and dimmed.
- Before a session has made a call: `compile · (no session yet)`.

**`:AicUsage`** (`<prefix>U`) opens a float with one block per session
(model, ctx line such as ``- ctx `[####------]` 82k / 200k (41%)`` or, window
unknown, `- ctx 107k / ? (?%): the window size is learned on its next call`,
runs/turns, cost, `compacted from`), the
account quota with reset times (`resets in 3h30m`, `6d7h`) and its age, and a
"last calls" table (newest first, at most 15 across compile + ask: time,
session, note, in / cache_read / cache_write / out tokens, cost). It
re-renders when `usage.json` changes. Keys:

| key | action |
|---|---|
| `c` / `C` | compact the compile / ask session (`aic compact compile\|ask`) |
| `w` | compact the worker (`aic compact worker`: types `/compact` into its tmux pane as you) |
| `r` / `R` | reset the compile / ask session (`aic compact … --reset`) |
| `q` / `<Esc>` | close |

Each action asks yes/no first, runs in the background (the footer shows
`compacting…`), notifies the one-line result and re-renders; the float stays
open. `:AicCompact compile|ask|worker` and `:AicReset compile|ask` do the same
without the confirmation (reset only applies to the compiler sessions).
Compacting asks the compiler session for a handoff summary and continues in a
fresh session seeded with it; resetting starts fresh with no summary. Compacting
the worker runs its own `/compact`; the backend refuses (shown as an error) when
a dialog is open or the pane is not the worker's.

**80% hint**: when the compile or ask session's context crosses 80%, aic
notifies once per session (again only after it drops below and crosses again),
e.g. `aic: ask context 82% — <leader>zU then C to compact`. It never compacts
on its own.

## Statusline

`require('aic').status()` returns a cached string, e.g. `aic 3● 2E held`
(unseen count, compile errors, `held`/`compiling`/`no-worker`). It returns `""`
when aic is inactive and never does IO.

lualine:

```lua
sections = { lualine_x = { function() return require('aic').status() end, 'filetype' } }
```

## Highlight groups

All are defined with `default = true` links, so you can override them:
`AicUnseen` `AicSeen` `AicTurn` `AicPrompt` `AicTime` `AicTitle` `AicWhy` `AicHunk`
`AicWorker` `AicHuman` `AicSubmit` `AicHold` `AicCompact` `AicText` `AicError` `AicAnchor`
`AicUsageOk` (Comment) `AicUsageWarn` (WarningMsg) `AicUsageHot` (ErrorMsg).

## nvim-ufo

The trace buffer uses its own `foldexpr`. If `ufo` is installed, it is detached
from the trace buffer.

## Human edits

On `BufReadPost`, files inside the project (excluding `.aic/` and `.git/`) are
snapshotted. On `BufWritePost` (only while a session dir is active), the save is
diffed against the snapshot and one line is appended to `human.jsonl`:
`{"ts","file","hunks":[{"start","count","lines"}]}`.

## Tests

```sh
cd aic/nvim
nvim --headless -u NONE -l tests/smoke.lua
```

This builds a temporary project with a fake `aic` executable and checks the
single-window views (a sweep runs every command and `<prefix>` key and
asserts one tab, one window, no quickfix window, no pinned window; `<C-^>`
back from each view, `<C-o>` back to the trace line; views never take over the
quickfix window; only aic's own floats are closed; view options never leak
into a fresh buffer), trace rendering and folds (including `compact` events), `]u`, seen
persistence, the watcher (one per root, all stopped on exit, no leaked
timers), quickfix and diagnostics, signs and `]w` (bounded to the view, only
changed buffers re-applied), human-edit hunks, the
statusline, hold/continue, submit (picker order, broken templates, input,
preview header flag, `:w` sends the edited text with its receipt, leaving
cancels, render errors, `:AicPrompts` copy/new), docs (new doc with/without the compile
marker, per-doc diagnostics and statusline, picker, `--doc` on submit/ask,
migration before the first docs dir), compile on `:w` (and queued compiles),
ask/what-if floats (`P` `F` `S` `Y` `gd`), chat, and usage: the three
winbars (plus null ctx window, null quota, gone with the view, back with it), the 80% hint, the `:AicUsage` float and its `c`/`R`/`w` actions
(including a refused worker compact and `:AicReset worker` being rejected). The project is a git repo
with one commit, for anchors: parsing of every form (and non-anchors, plus
the shared fixture `../tests/anchor_cases.json`), `gd` on
each kind, highlighting (visible lines only in a long chat), the omnifunc and `<prefix>g`.
