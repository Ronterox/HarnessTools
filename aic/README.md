# aic — AI compiler

Review a coding Claude **while it works**, from Neovim. A second Claude (the
compiler) checks your pseudocode spec against what the worker actually did and
answers questions about it; you steer the worker with submits and holds. Both
sides keep working.

- Why and how: [DESIGN.md](DESIGN.md). File/CLI contract: [PROTOCOL.md](PROTOCOL.md).
- Neovim client and keymaps: [nvim/README.md](nvim/README.md).
- Python 3.8+ stdlib only, no install. Needs `claude`, `tmux`, Neovim 0.10+.

## Quickstart

```sh
cd your-project
/path/to/aic/bin/aic init        # .aic/ + hooks in .claude/settings.local.json
```

1. **Worker**: in a tmux pane, start `claude` in the project as usual. (Already
   running? Restart it so it loads the hooks.)
2. **You**: Neovim with the plugin (lazy.nvim):
   ```lua
   { dir = '/path/to/aic/nvim', config = function() require('aic').setup({}) end }
   ```
   `:Aic` shows aic in the current window. Views are buffers you switch
   between, never tabs or splits: `<leader>zt` history (trace), `<leader>zq`
   questions (compiler chat), `<leader>zd` docs (pick one or make a new one,
   `<leader>zD` new); `<leader>zz` toggles back to your code.
3. Write in docs (`.aic/docs/*.md`): specs, notes, whatever you need
   (`@path:symbol` anchors, `gd` follows them). A doc whose first line is
   `<!-- aic: compile -->` compiles on `:w`: diagnostics land in the quickfix
   list and the doc's sign column. `.aic/` is gitignored: `git clean -fdx`
   deletes your docs.
4. `<leader>zs` submits the current doc's diff (visual: just the selected lines) to the
   worker; `<leader>zS` interrupts it to deliver now; `<leader>zh` holds it at its
   next tool call; `<leader>zc` continues.
5. Read along in the trace (`]u` next unseen, `<CR>` show/hide a node's diff, `gf` jump to code, `W` what-if),
   ask about anything with `<leader>z?`, see who touched a line with `<leader>zb`.

## Ways of working (all tested live)

- **Write in whatever form you think in.** Outline, raw pseudocode
  (`fn visible(todos, filter): match …`) or ASCII box diagrams. The compiler
  reads pseudocode as intent (no syntax nits) and checks every box exists and
  every arrow is a real call/data path; it flagged a wrong "load() on every
  request" arrow as a mental-model error.
- **Point at things with anchors** (`gd` follows them, `<C-x><C-o>` completes
  after `@`). Their content is resolved and sent along:

  | anchor | what |
  |---|---|
  | `@file` `@file:42` `@file:10-20` `@file:symbol` | a file, line, range, definition |
  | `@dir/` | a directory listing |
  | `@/abs/…` `@~/…` | the same forms in other projects: inlined for the compiler when you typed them; an outside `@dir/` lets it Read there (never `/` or `~`) |
  | `@diff` `@diff:path` `@staged` | uncommitted changes |
  | `@log` `@log:path` `@show:REV` | git history and commits |
  | `@turn:N` `@turn:last` | what the worker did in a turn |

- **Debug by asking.** Describe symptoms in chat ("toggled, restarted, came
  back undone; check @log and @show:HEAD"); the compiler finds the cause in
  code/history and answers with fix pseudocode.
- **Answer → worker in one key.** In an answer float, `S` appends its
  pseudocode block to the current doc and submits just that (once); `Y` yanks it;
  `<leader>zl` reopens the last answer if you closed it. (Uppercase, so
  `y`/`f` motions still work for copying from the float.)
- **Keep working in parallel.** Submit what you already know while the worker
  is busy; it reads it at its next tool call.
- **Mark finished sections `## [done] …`.** The compiler keeps checking they
  still hold but stops treating them as pending.
- **Review before committing:** "review @diff against the spec" gives a
  checklist and a commit message.
- The statusline says `stale` when code changed since the last compile.
- **Watch context and cost, compact when it grows.** Each aic view's winbar
  shows the session behind it (doc → compile, history → worker, questions → ask,
  plus your 5h/weekly quota from the OMC HUD cache). `<leader>zU` opens the
  breakdown with the last calls; `c`/`C` compact the compile/ask session
  (handoff summary → fresh session), `r`/`R` reset it. In testing, compacting
  a 483k-token compile session took it to 67k and the next compile from
  $4.28 to $0.26. aic only hints at 80%; it never compacts by itself.

## CLI

```
aic init | sessions | attach [sid|--auto] | status
aic watch                    # trace builder (the plugin starts it)
aic docs [--json]            # list docs (migrates the old spec.md once)
aic compile [--doc D]        # doc vs worker activity -> diagnostics.D.json (queues if busy)
aic anchor [--json] TEXT     # resolve @anchors (what the compiler/worker will see)
aic usage [--json]           # context/cost per session + account quota
aic compact compile|ask [--reset]
aic ask [--doc D] [--whatif] [--context-file F] QUESTION
aic submit [--doc D] [--from N --to M] [--now] | hold | continue [MESSAGE]
```

## Tests

```sh
python3 -m unittest discover -s aic/tests          # backend
cd aic/nvim && nvim --headless -u NONE -l tests/smoke.lua   # Neovim client
```

## Known limits

- The worker must run inside tmux (that's how your words reach it).
- The worker's thinking isn't stored, so "why" is the text it wrote before a
  tool call; some workers write none. Ask the compiler instead.
- Edits the worker makes through Bash are attributed live by diffing the tree;
  a trace rebuilt later (`aic watch` restart) can't recover them.
- Each compile/ask is a real Claude call (compile ≈ 10s, ~$0.10 on Sonnet in
  the test project).
