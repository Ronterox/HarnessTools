"""Worker-facing commands: init, sessions, attach, submit, hold, continue.

The human's words reach the worker as real user input, typed into the
worker's tmux pane (recorded by the hook from $TMUX_PANE). Claude Code queues
input typed while it is busy and absorbs it at the next tool boundary, so a
plain submit already redirects mid-turn. `--now` additionally presses Claude
Code's "send now" (ctrl+x ctrl+s), which interrupts the running tool.
"""
import difflib
import json
import os
import re
import subprocess
import time

from . import anchors, docs, paths

HOOK_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
               "Stop", "Notification", "SessionEnd"]

# Claude Code dialogs that would swallow typed keys (and Enter would answer them).
DIALOG = re.compile(r"(Do|Would) you (want|like) to|Esc to cancel|Tab to amend|❯ \d+\. ")


class WorkerError(Exception):
    pass


# -- init ---------------------------------------------------------------------
def init(root, bin_path):
    d = paths.aic_dir(root)
    for sub in ("sessions", "workers"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    paths.write_text(os.path.join(d, ".gitignore"), "*\n")
    settings_path = os.path.join(root, ".claude", "settings.local.json")
    settings = {}
    if os.path.exists(settings_path):
        try:
            with open(settings_path, encoding="utf-8") as f:
                settings = json.load(f)
        except ValueError as e:
            raise WorkerError("%s is not valid JSON (%s); fix it, then rerun init" % (settings_path, e))
        if not isinstance(settings, dict):
            raise WorkerError("%s is not a JSON object" % settings_path)
        paths.write_text(settings_path + ".aic-backup", paths.read_text(settings_path))
    hooks = settings.setdefault("hooks", {})
    cmd = "%s-hook" % bin_path

    def ours(h):
        c = h.get("command") or ""
        return c.endswith("/aic hook") or c.endswith("/aic-hook")

    for ev in HOOK_EVENTS:
        groups = [g for g in hooks.get(ev, []) if not any(ours(h) for h in g.get("hooks", []))]
        entry = {"hooks": [{"type": "command", "command": cmd, "timeout": 10}]}
        if ev in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
            entry["matcher"] = "*"
        groups.append(entry)
        hooks[ev] = groups
    paths.write_text(settings_path, json.dumps(settings, indent=2) + "\n")
    return settings_path


# -- sessions -----------------------------------------------------------------
def _title(transcript):
    """Last ai-title (or first prompt) from the tail/head of a transcript."""
    if not transcript or not os.path.exists(transcript):
        return ""
    title = ""
    try:
        with open(transcript, "rb") as f:
            f.seek(max(0, os.path.getsize(transcript) - 200000))
            for raw in f:
                if b'"ai-title"' in raw:
                    try:
                        title = json.loads(raw).get("aiTitle") or title
                    except ValueError:
                        pass
    except OSError:
        pass
    return title


def sessions(root):
    att = paths.attached(root)
    pin = paths.pinned(root)
    seen, out = set(), []
    for w in paths.workers(root):
        seen.add(w["session_id"])
        out.append({"session_id": w["session_id"], "updated": w.get("updated", 0),
                    "state": w.get("state"), "pane": w.get("pane"),
                    "title": _title(w.get("transcript_path"))})
    for t in paths.transcript_sessions(root):
        if t["session_id"] not in seen:
            out.append({"session_id": t["session_id"], "updated": t["updated"], "state": None,
                        "pane": None, "title": _title(t["transcript_path"])})
    out.sort(key=lambda s: s["updated"], reverse=True)
    for s in out:
        s["attached"] = s["session_id"] == att
        s["pinned"] = s["session_id"] == pin
    return out


def attach(root, sid):
    p = os.path.join(paths.aic_dir(root), "current")
    if sid in (None, "auto", "--auto"):
        if os.path.exists(p):
            os.unlink(p)
        return paths.attached(root)
    paths.write_text(p, sid + "\n")
    return sid


def status(root):
    sid = paths.attached(root)
    sdir = paths.session_dir(root, sid) if sid else None
    w = paths.worker(root, sid) if sid else None
    from .compiler import compiling
    return {
        "root": root, "session_id": sid, "dir": sdir, "worker": w,
        "pinned": paths.pinned(root) is not None,
        "hold": bool(sdir and os.path.exists(os.path.join(sdir, "hold"))),
        "compiling": bool(sdir and os.path.isdir(sdir) and compiling(sdir)),
        "watching": paths.pid_alive(paths.read_text(os.path.join(paths.aic_dir(root), "watch.pid")).strip()),
    }


# -- delivery -----------------------------------------------------------------
def _tmux(*args, input=None):
    p = subprocess.run(["tmux"] + list(args), input=input, capture_output=True, text=True, timeout=10)
    if p.returncode != 0:
        raise WorkerError("tmux %s failed: %s" % (args[0], p.stderr.strip()))
    return p.stdout


def _stat(pid):
    """(pgrp, tpgid) from /proc/<pid>/stat, or None."""
    try:
        with open("/proc/%d/stat" % int(pid)) as f:
            fields = f.read().rsplit(")", 1)[1].split()
        return int(fields[2]), int(fields[5])
    except (OSError, ValueError, IndexError, TypeError):
        return None


def verify_pane(w):
    """The pane must still be the live Claude Code TUI of this session: its
    process alive, owning the pane's terminal, and in the foreground. Anything
    else (Claude exited back to a shell, a `claude -p` inside an editor's
    terminal, a reused pane) and typing there could run text as commands."""
    pane = w.get("pane")
    if not pane:
        raise WorkerError("worker pane unknown (or the session ended): run the worker inside tmux "
                          "in a project with `aic init` hooks")
    try:
        pane_tty = _tmux("display-message", "-p", "-t", pane, "#{pane_tty}").strip()
    except WorkerError:
        raise WorkerError("worker pane %s is gone" % pane)
    pid = w.get("pid")
    st = _stat(pid) if pid else None
    if not st:
        raise WorkerError("worker process not found (it exited?); refusing to type into %s" % pane)
    try:
        tty = os.readlink("/proc/%d/fd/0" % int(pid))
    except OSError:
        tty = None
    if tty != pane_tty:
        raise WorkerError("worker (pid %s) does not own pane %s's terminal; refusing to type" % (pid, pane))
    if st[0] != st[1]:
        raise WorkerError("worker (pid %s) is not in the foreground of %s; refusing to type" % (pid, pane))
    return pane


def _screen(pane):
    # Claude Code draws its prompt as "❯" + NO-BREAK SPACE; normalize it.
    text = _tmux("capture-pane", "-p", "-t", pane).replace("\u00a0", " ")
    return [l for l in text.splitlines() if l.strip()]


def _check_screen(pane, header=None, wait=1.5):
    """No dialog at the bottom of the screen and, when `header` is given, our
    text sits in Claude Code's input box. Positive evidence before every
    keystroke batch; polls briefly so a slow redraw isn't mistaken for failure.
    Errors are fail-safe: nothing more is typed."""
    end = time.time() + wait
    while True:
        lines = _screen(pane)
        if DIALOG.search("\n".join(lines[-12:])):
            raise WorkerError("the worker is showing a dialog in %s (permission prompt?). Answer it there; "
                              "aic never types into dialogs" % pane)
        if not header or any(l.lstrip().startswith("❯ " + header[:24]) for l in lines):
            return
        if time.time() >= end:
            raise WorkerError("could not see the typed request in the worker's input box in %s; "
                              "not pressing Enter (clear the input there if needed)" % pane)
        time.sleep(0.2)


def deliver(root, sid, header, body=None, now=False):
    """Type `header` into the worker's prompt as the human, paste `body` under it,
    and press Enter. The header is typed (not pasted) on purpose: Claude Code
    marks pasted text as untrusted data, so the human's own typed words are what
    ask the worker to act on the pasted spec/diff. Headers carry no digits:
    if a dialog appeared mid-delivery, digits could pick its options."""
    w = paths.worker(root, sid) or {}
    pane = verify_pane(w)
    header = " ".join(header.split())
    _check_screen(pane)
    size = os.path.getsize(w["transcript_path"]) if w.get("transcript_path") and os.path.exists(w["transcript_path"]) else 0
    _tmux("send-keys", "-t", pane, "-l", header)
    time.sleep(0.2)
    _check_screen(pane, header)
    if body:
        _tmux("load-buffer", "-b", "aic", "-", input="\n" + body)
        _tmux("paste-buffer", "-p", "-d", "-b", "aic", "-t", pane)
    time.sleep(0.3)  # let the TUI absorb the input before submitting it
    _check_screen(pane, header)
    _tmux("send-keys", "-t", pane, "Enter")
    if now:
        time.sleep(0.3)
        _check_screen(pane)
        _tmux("send-keys", "-t", pane, "C-x", "C-s")
    if not _confirm(w.get("transcript_path"), size, header):
        raise WorkerError("typed into %s but the worker did not record it; check that pane" % pane)
    return pane


def _confirm(transcript, offset, header, timeout=5.0):
    """True once the worker's transcript shows our message (sent or queued)."""
    if not transcript:
        return True  # nothing to check against
    needle = json.dumps(header[:60], ensure_ascii=False)[1:-1]
    end = time.time() + timeout
    while time.time() < end:
        try:
            with open(transcript, encoding="utf-8") as f:
                f.seek(offset)
                if needle in f.read():
                    return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


def control(root, sid, kind, **extra):
    ev = {"ts": paths.now(), "kind": kind}
    ev.update(extra)
    paths.append_jsonl(os.path.join(paths.session_dir(root, sid), "control.jsonl"), ev)


# -- submit / hold / continue -------------------------------------------------
def _human_edits_since(sdir, seq):
    """Where the human's edits are *now* (touched.json follows later shifts),
    falling back to the line numbers at the time of the edit."""
    touched = paths.read_json(os.path.join(sdir, "touched.json"), {}) or {}
    out = []
    for e in paths.read_jsonl(os.path.join(sdir, "trace.jsonl")):
        if e.get("kind") != "human" or e.get("seq", 0) <= seq:
            continue
        now = [r for r in touched.get(e.get("file"), []) if isinstance(r, dict) and r.get("event") == e.get("id")]
        if now:
            out += ["%s:%d-%d" % (e["file"], r["start"], r["end"]) for r in now]
            continue
        for h in e.get("hunks") or []:
            if isinstance(h, dict) and isinstance(h.get("start"), int):
                end = h["start"] + max(h.get("count") or 1, 1) - 1
                out.append("%s:%d-%d" % (e.get("file"), h["start"], end))
    return out


def _last_seq(sdir):
    tr = paths.read_jsonl(os.path.join(sdir, "trace.jsonl"))
    return tr[-1].get("seq", 0) if tr else 0


def build_submit(root, sid, line_from=None, line_to=None, doc=docs.DEFAULT):
    """-> dict(header=typed line, body=pasted text, spec=exact doc text sent or None,
    seq=trace position covered). Headers are digit-free; details go in the body.
    Line numbers are file lines; the doc's marker line is never sent."""
    sdir = paths.session_dir(root, sid)
    raw = docs.read(root, doc)
    spec = docs.strip_marker(raw)
    st = paths.read_json(os.path.join(sdir, "state.json"), {}) or {}
    seq = _last_seq(sdir)
    sent_spec = None
    if line_from:
        lines = raw.splitlines()
        line_to = line_to or line_from
        first = line_from - 1
        if first == 0 and lines and docs.MARKER.match(lines[0]):
            first = 1  # the marker line is the doc's settings, not content
        part = "\n".join(lines[first: line_to])
        if not part.strip():
            raise WorkerError("selected spec lines are empty")
        header = "[aic] Please implement this part of my spec:"
        body = "(spec lines %d-%d)\n\n%s" % (line_from, line_to, part)
    else:
        old = paths.read_text(docs.sent_path(sdir, doc))
        if old == spec:
            raise WorkerError("spec unchanged since last submit")
        if not old.strip():
            header = "[aic] Here is my spec, please implement it:"
            body = spec.strip()
        else:
            diff = "".join(difflib.unified_diff(old.splitlines(True), spec.splitlines(True),
                                                "spec (last submit)", "spec (now)"))
            header = "[aic] I changed my spec, please apply these changes (diff, then the full spec):"
            body = "```diff\n%s```\n\nFull spec now:\n\n%s" % (diff, spec.strip())
        sent_spec = spec
    # anchors in what is being sent: inline what the worker can't just open
    # (line ranges, symbols, diffs, turns, files outside the project)
    if line_from:
        sent_text = part
    elif not old.strip():
        sent_text = spec
    else:
        sent_text = "\n".join(l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    ref = anchors.blocks(sent_text, root, sid, for_worker=True)
    if ref:
        body += "\n\nReferenced context (resolved from my @anchors):\n\n" + ref
    edits = _human_edits_since(sdir, st.get("submit_seq", 0))
    if edits:
        header += " I also edited code by hand, keep my changes (listed at the end)."
        body += "\n\nMy hand edits since my last message: " + ", ".join(edits)
    return {"header": header, "body": body, "spec": sent_spec, "seq": seq}


def submit(root, sid, line_from=None, line_to=None, now=False, doc=docs.DEFAULT):
    sdir = paths.session_dir(root, sid)
    msg = build_submit(root, sid, line_from, line_to, doc)
    busy = (paths.worker(root, sid) or {}).get("state") == "busy"
    pane = deliver(root, sid, msg["header"], msg["body"], now=now and busy)
    if msg["spec"] is not None:  # exactly what was sent, not what is on disk now
        paths.write_text(docs.sent_path(sdir, doc), msg["spec"])
    paths.update_json(os.path.join(sdir, "state.json"), submit_seq=msg["seq"])
    control(root, sid, "submit", text=msg["header"] + "\n" + msg["body"], now=bool(now))
    if not busy:
        state = "sent"
    elif now:
        state = "sent now (interrupted the running tool)"
    else:
        state = "queued: the worker reads it at its next tool call"
    return "submitted to %s: %s" % (pane, state)


def hold(root, sid):
    sdir = paths.session_dir(root, sid)
    paths.write_text(os.path.join(sdir, "hold"), str(paths.now()))
    control(root, sid, "hold")
    w = paths.worker(root, sid) or {}
    if w.get("state") != "busy":
        return "hold armed (worker is %s; it will stop at its next tool call)" % (w.get("state") or "unknown")
    return "hold armed: worker stops at its next tool call"


def cont(root, sid, message=None):
    sdir = paths.session_dir(root, sid)
    if message:  # free text may hold digits: paste it under a fixed typed line
        pane = deliver(root, sid, "[aic] Continue, and:", message)
    else:
        pane = deliver(root, sid, "Continue.")
    h = os.path.join(sdir, "hold")
    if os.path.exists(h):
        os.unlink(h)
    control(root, sid, "release")
    return "released; sent to %s" % pane
