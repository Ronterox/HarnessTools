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
  **spec** (`spec.md`). aic never creates a tab or a split and never opens the
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
  statusline, questions, usage winbars, and (when `diagnostics.json` changes) the quickfix list.

## Keymaps (default prefix `<leader>z`)

| key | command | `<Plug>` |
|---|---|---|
| `<prefix>z` | `:AicToggle`: in an aic buffer, back to the last code buffer; elsewhere, the last aic view | `(aic-toggle)` |
| `<prefix>t` | `:AicHistory`: the history (trace) in this window | `(aic-history)` |
| `<prefix>q` | `:AicQuestions`: the questions (compiler chat) in this window | `(aic-questions)` |
| `<prefix>d` | `:AicSpec`: the spec in this window | `(aic-spec)` |
| `<prefix>s` (n, x) | `:AicSubmit`: full spec diff; with a visual range, `--from/--to` | `(aic-submit)` |
| `<prefix>S` (n, x) | `:AicSubmitNow`: same with `--now` | `(aic-submit-now)` |
| `<prefix>h` | `:AicHold` | `(aic-hold)` |
| `<prefix>c` | `:AicContinue [msg]` | `(aic-continue)` |
| `<prefix>?` | `:AicAsk`: ask about the thing under the cursor (any buffer) | `(aic-ask)` |
| `<prefix>w` | `:AicWhatIf` | `(aic-whatif)` |
| `<prefix>b` | `:AicBlame`: trace event for the touched range under the cursor | `(aic-blame)` |
| `<prefix>a` | `:AicAttach`: pick the worker session (or auto) | `(aic-attach)` |
| `<prefix>e` | `:AicDiagnostics`: load the last compile into the quickfix list (no window: `:copen`/`]q` are yours) | `(aic-diagnostics)` |
| `<prefix>u` | `:AicNextUnseen`: show the history and go to the next unseen node | `(aic-next-unseen)` |
| `<prefix>U` | `:AicUsage`: context/cost per session, account quota, compact/reset (see [Usage](#usage)) | `(aic-usage)` |
| `<prefix>]` / `<prefix>[` | `:AicNextTouched` / `:AicPrevTouched` | `(aic-next-touched)` / `(aic-prev-touched)` |
| `<prefix>g` | follow the anchor under the cursor, in any buffer (see [Anchors](#anchors)) | `(aic-follow)` |

Other commands: `:Aic` (show the last aic view here), `:AicCompile`,
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

**Spec** (`<session>/spec.md`, a real file): `:w` runs `aic compile`. If a
compile is already running, the backend queues one more pass on your latest
spec (`aic: compile queued`; the statusline shows `compiling+queued`). When it
finishes, quickfix (title `aic compile`) and inline `vim.diagnostic`
(namespace `aic`) are updated, and a one-line summary is shown. Anchors are
highlighted, `gd` follows them and `<C-x><C-o>` completes them (see
[Anchors](#anchors)).

**Questions** (`aic://questions`): shows `chat.md` followed by a `## You` line. Type
below it and press `<CR>` in normal mode to send with `aic ask`. The plugin never
writes `chat.md`; the view reloads when the backend appends to it, and keeps
what you've typed. Anchors in your input are sent as-is (the backend resolves
them); `gd`, highlighting and `<C-x><C-o>` work as in the spec.

**Code buffers** with touched ranges: `]w` / `[w` go to the next / previous
touched range. Signs: `▎` in `AicWorker` (worker) or `AicHuman` (you). Signs
are drawn only within 200 lines of what each window shows (redrawn on
scroll), and a `touched.json` change only re-applies buffers whose ranges
changed, so big writes stay cheap.

**Ask / what-if float**: `q`/`<Esc>` close. Action keys are uppercase so
`y`, `p`, `f` and the other motions still work for copying from the float.
`P` appends the answer to the
end of spec.md under `## Notes` (ask). `F` adds it under
`## What-if: <question>` (what-if). If spec.md is open, the text goes into the buffer and you
save it yourself (saving compiles). In both floats:

- `S` appends the answer's first fenced code block (or the whole answer if it
  has none) under `## From compiler: <question>`, saves spec.md (which
  compiles), and runs `aic submit --from <heading line> --to <last line>`.
  `S` works once per answer (also after `:AicLastAnswer`); pressing it again
  only notifies `aic: already submitted`.
- `Y` yanks the same block (linewise) into the unnamed register.
- `gd` follows anchors in the answer; they are highlighted.

The footer lists the keys.

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

- `gd` is buffer-local in the spec, questions and answer float; off an anchor it
  calls the `gd` mapping that was there before (e.g. an LSP one), else the
  normal `gd`. `<prefix>g` works in any buffer.
- Valid anchors are highlighted with `AicAnchor` (default link `Underlined`),
  refreshed on edits and scrolling (debounced): the whole buffer under 2000
  lines, else the visible lines. The questions buffer always highlights only the
  visible lines ±50.
- `omnifunc` (spec and questions) completes after `@` with `<C-x><C-o>`: project files and
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
| spec | `compile · sonnet  ctx:[####------] 82k/200k (41%)  12 runs  $1.20` |
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
statusline, submit/hold/continue, compile on `:w` (and queued compiles),
ask/what-if floats (`P` `F` `S` `Y` `gd`), chat, and usage: the three
winbars (plus null ctx window, null quota, gone with the view, back with it), the 80% hint, the `:AicUsage` float and its `c`/`R`/`w` actions
(including a refused worker compact and `:AicReset worker` being rejected). The project is a git repo
with one commit, for anchors: parsing of every form (and non-anchors, plus
the shared fixture `../tests/anchor_cases.json`), `gd` on
each kind, highlighting (visible lines only in a long chat), the omnifunc and `<prefix>g`.
