"""`aic hook`: the worker-side gate. Installed as a Claude Code hook.

Records which session/tmux pane/process the worker lives in and whether it is
busy, and enforces a one-shot hold: the next tool call is denied and the turn
ends. It also keeps the worker's file tools out of the human's docs and prompt
templates (.aic/docs, .aic/prompts): a template's header is typed as the human.
The compiler never reaches this hook (no project hooks, AIC_ROLE=compiler) and
never writes files anyway.

It never injects instructions into the worker. Claude (rightly) treats hook
text as untrusted, so the human's words always arrive as real user input
(typed into the worker's pane by `aic submit`).
"""
import json
import os
import sys

from . import paths

STATE = {
    "SessionStart": "idle",
    "UserPromptSubmit": "busy",
    "PreToolUse": "busy",
    "PostToolUse": "busy",
    "PostToolUseFailure": "busy",
    "Stop": "idle",
    "SessionEnd": "ended",
}

HOLD_REASON = ("Paused by the human user via aic (hold). Do not retry. "
               "End your turn and wait for their next message.")
GUARD_REASON = ("This file is the human's own (an aic doc or prompt template, under .aic/). "
                "Do not change it, by any means. If it should change, say so in your reply.")
FILE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
GUARDED = ("docs", "prompts")


def guarded(root, data):
    """The .aic/docs or .aic/prompts path a worker file tool would write, or None."""
    key = FILE_TOOLS.get(data.get("tool_name"))
    inp = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    target = inp.get(key) if key else None
    if not isinstance(target, str) or not target:
        return None
    target = os.path.realpath(os.path.join(data.get("cwd") or root, os.path.expanduser(target)))
    aic = os.path.realpath(paths.aic_dir(root))
    for d in GUARDED:
        base = os.path.join(aic, d)
        if target == base or target.startswith(base + os.sep):
            return os.path.relpath(target, os.path.realpath(root))
    return None


def control_event(root, sid, kind, **extra):
    ev = {"ts": paths.now(), "kind": kind}
    ev.update(extra)
    paths.append_jsonl(os.path.join(paths.session_dir(root, sid), "control.jsonl"), ev)


def _proc_ppid(pid):
    try:
        with open("/proc/%d/stat" % pid) as f:
            return int(f.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def _tty(pid):
    try:
        t = os.readlink("/proc/%d/fd/0" % pid)
    except OSError:
        return None
    return t if t.startswith("/dev/pts/") or t.startswith("/dev/tty") else None


def worker_process():
    """(pid, tty) of the nearest ancestor that owns a terminal: the Claude Code
    TUI (hooks themselves run with piped stdin). (None, None) off Linux."""
    pid = os.getppid()
    for _ in range(8):
        if not pid or pid <= 1:
            break
        tty = _tty(pid)
        if tty:
            return pid, tty
        pid = _proc_ppid(pid)
    return None, None


def handle(data):
    if os.environ.get("AIC_ROLE") == "compiler":
        return None
    event = data.get("hook_event_name", "")
    sid = data.get("session_id")
    root = paths.find_root(data.get("cwd"))
    if not root or not sid:
        return None

    wpath = os.path.join(paths.aic_dir(root), "workers", sid + ".json")
    w = paths.read_json(wpath, {}) or {}
    w.update({
        "session_id": sid,
        "transcript_path": data.get("transcript_path") or w.get("transcript_path"),
        "cwd": data.get("cwd") or w.get("cwd"),
        "last_event": event,
        "updated": paths.now(),
    })
    if event in ("SessionStart", "UserPromptSubmit") or not w.get("pid"):
        pid, tty = worker_process()
        # $TMUX = "<socket>,<pid>,<session>": the server the pane id belongs to
        sock = (os.environ.get("TMUX") or "").split(",")[0] or None
        w.update(pid=pid, tty=tty, pane=os.environ.get("TMUX_PANE"), tmux_socket=sock)
    if event in ("SessionStart", "UserPromptSubmit"):
        w["prompted"] = paths.now()
    if event in STATE:
        w["state"] = STATE[event]
    elif event == "Notification" and "permission" in (data.get("message") or "").lower():
        w["state"] = "asking"  # advisory: delivery trusts what the pane shows
    if event == "SessionEnd":
        w["pane"] = None  # never type into this pane again

    out = None
    hold = os.path.join(paths.session_dir(root, sid), "hold")
    if event == "PreToolUse" and os.path.exists(hold):
        try:
            os.unlink(hold)  # one-shot; a parallel call that loses the race just runs
        except FileNotFoundError:
            hold = None
        if hold:
            w["state"] = "held"
            control_event(root, sid, "held", tool=data.get("tool_name"))
            out = {
                "continue": False,
                "stopReason": "aic: held by the human",
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": HOLD_REASON,
                },
            }
    if event == "PreToolUse" and out is None:
        g = guarded(root, data)
        if g:
            control_event(root, sid, "guarded", tool=data.get("tool_name"), file=g)
            out = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": GUARD_REASON,
                },
            }
    elif event in ("Stop", "SessionEnd") and os.path.exists(hold):
        # The turn ended on its own before the next tool call: nothing left to hold.
        try:
            os.unlink(hold)
            control_event(root, sid, "hold-expired")
        except FileNotFoundError:
            pass

    paths.write_json(wpath, w)
    return out


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    try:
        out = handle(data) if isinstance(data, dict) else None
    except Exception as e:  # the gate must never break the worker
        sys.stderr.write("aic hook: %s\n" % e)
        return 0
    if out is not None:
        sys.stdout.write(json.dumps(out))
    return 0
