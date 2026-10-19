# undograft

A Claude Code hook that puts the AI's file edits into Neovim's undo tree:
one undo step per `Write`/`Edit`/`MultiEdit`, next to your own history,
browsable in undotree and undone with `u`.

Without it, an AI edit reaches Neovim only through `autoread`. That reload is
one whole-buffer step that bundles every edit made since the last check. Above
`'undoreload'` lines (10000), the reload clears the buffer's undo history.
With `'undofile'` on, an edit to a file you don't have open also invalidates
its undofile, so the next time you open the file its history is gone.

## How

- **File loaded in a running Neovim** (found by its default server socket,
  `$XDG_RUNTIME_DIR/nvim.PID.0`): before the edit, the buffer is guarded so
  `autoread` can't reload it. After the edit, what changed on disk is applied
  as diff hunks in one undo step. The cursor, marks and extmarks stay put.
  Neovim then writes the now-identical buffer back (`noautocmd`, so format
  on save doesn't run), which has three effects:
  - the next `:w` doesn't warn that the file changed since reading;
  - undoing the AI step leaves the buffer modified;
  - the undofile is saved.

  Before writing, it checks that the file hasn't changed since it was read,
  and afterwards that the bytes on disk are still exactly what the AI wrote.
  If Neovim notices the change before the hook's second call (on focus, or
  the call never comes because the tool was denied), the guard syncs the
  buffer right away.
- **File not loaded, but it has an undofile**: the file is snapshotted before
  the edit. After the edit, a headless `nvim -u NONE` loads the undofile
  against the snapshot, adds the edit as a step, and writes the undofile back.
- **Left alone**: a buffer with your unsaved changes gets the usual W12
  prompt, even when you started typing after the guard went up. A change the
  hunks can't express (encoding, line endings, a final newline appearing or
  disappearing, a readonly or unmodifiable buffer) gets the usual reload,
  except that the undo history is kept at any file size. Bash edits
  (`sed -i`, scripts) aren't tracked per call; they arrive through `autoread`
  as before.

Cost: about 50 ms of hook time per tool call, so about 100 ms per edit, or up
to about 180 ms on a 50k-line file nobody has open. Neovim itself only applies
the hunks. A Neovim that is busy (a prompt, a long command) is given up on
after a second.

## Install

In `~/.claude/settings.json` (every project):

```json
"PreToolUse":         [{"matcher": "Write|Edit|MultiEdit", "hooks": [{"type": "command", "command": "/path/to/undograft/bin/undograft", "timeout": 15}]}],
"PostToolUse":        [{"matcher": "Write|Edit|MultiEdit", "hooks": [{"type": "command", "command": "/path/to/undograft/bin/undograft", "timeout": 15}]}],
"PostToolUseFailure": [{"matcher": "Write|Edit|MultiEdit", "hooks": [{"type": "command", "command": "/path/to/undograft/bin/undograft", "timeout": 15}]}]
```

Nothing to add to the Neovim config: the hook loads `nvim/lua/undograft.lua`
into each Neovim on first use. Changes to that file reach a running Neovim
only after it restarts.

The `'undodir'`/`'undolevels'` values are learned from a running Neovim and
cached in `~/.cache/undograft.json`. If no Neovim has run yet, they are read
once from a headless Neovim with your config.

## Test

```sh
python3 -m unittest discover -s undograft/tests
```

The tests start their own headless Neovims in a temporary `XDG_RUNTIME_DIR`,
so the Neovims you have open are never touched.
