# notify

Desktop notification and sound when Claude Code finishes a turn or needs you.

- `notify.sh`: the Stop, StopFailure and Notification hook. It shows a
  `notify-send` popup with the session's badge icon, plays a sound from
  `~/.claude/sounds/<finished|error|attention>/`, and queues the pane for
  `tmux-notifications.sh`.
- `bg-pending TRANSCRIPT.jsonl`: prints how many background tasks the session
  launched and hasn't seen finish (agents, background shells, monitors,
  backgrounded MCP calls). `--list` prints their ids instead. If the transcript
  is missing it exits 2, and if it can't be parsed it exits 3. The header
  comment lists the transcript shapes it reads and its staleness rules.

A Stop, or an `idle_prompt` notification, stays silent while `bg-pending` is
above 0. Claude runs again when that work finishes, and the Stop after that is
the real one. Permission prompts and errors always notify. Any helper failure
notifies as before.

## Install

Both files are symlinked from `~/.claude/hooks/`, next to `session-badge.sh`
and `tmux-notifications.sh`, which `notify.sh` finds through its own path:

```sh
ln -s "$PWD/notify/notify.sh" "$PWD/notify/bg-pending" ~/.claude/hooks/
```

`BG_PENDING_MAX_AGE` (seconds, default 10800) is the age after which a launch
no longer counts.
