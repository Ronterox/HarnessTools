"""Project/session discovery and small file helpers shared by every command."""
import contextlib
import fcntl
import glob
import json
import os
import re
import tempfile
import time

AIC = ".aic"


def find_root(start=None):
    """Nearest ancestor of `start` that contains .aic/, or None."""
    d = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.isdir(os.path.join(d, AIC)):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def init_root(start=None):
    """Where `aic init` creates .aic/: an existing root above, else the cwd.
    (Not the git root: the worker's cwd is what Claude Code keys sessions by.)"""
    return find_root(start) or os.path.abspath(start or os.getcwd())


def aic_dir(root):
    return os.path.join(root, AIC)


def session_dir(root, sid):
    return os.path.join(root, AIC, "sessions", sid)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    write_text(path, json.dumps(data, ensure_ascii=False))


@contextlib.contextmanager
def locked(path):
    """Exclusive advisory lock on `path`.lock for read-modify-write cycles."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def update_json(path, **changes):
    """Re-read under a lock and set only `changes`, so concurrent compile/ask/
    submit processes never revert each other's keys."""
    with locked(path):
        data = read_json(path, {}) or {}
        data.update(changes)
        write_json(path, data)
        return data


def write_text(path, text):
    """Atomic write (tmp + rename) so editors never see half a file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def read_text(path, default=""):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return default


def append_jsonl(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    except OSError:
        pass
    return out


def last_seq(sdir):
    """`seq` of the session's newest trace event (0: none yet)."""
    tr = read_jsonl(os.path.join(sdir, "trace.jsonl"))
    return tr[-1].get("seq", 0) if tr else 0


def control(root, sid, kind, **extra):
    """Record a hold/submit/... in control.jsonl (the watcher puts it in the trace)."""
    ev = {"ts": now(), "kind": kind}
    ev.update(extra)
    append_jsonl(os.path.join(session_dir(root, sid), "control.jsonl"), ev)


def claude_project_dir(root):
    """~/.claude/projects/<encoded root> — where Claude Code keeps transcripts."""
    enc = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(root))
    return os.path.join(os.path.expanduser("~/.claude/projects"), enc)


def compiler_ids(root):
    return set(read_json(os.path.join(aic_dir(root), "compilers.json"), []) or [])


def add_compiler_id(root, sid):
    p = os.path.join(aic_dir(root), "compilers.json")
    with locked(p):
        write_json(p, sorted(set(read_json(p, []) or []) | {sid}))


def workers(root):
    """Hook-registered workers, most recently updated first."""
    out = []
    for p in glob.glob(os.path.join(aic_dir(root), "workers", "*.json")):
        w = read_json(p)
        if isinstance(w, dict) and w.get("session_id"):
            out.append(w)
    out.sort(key=lambda w: w.get("updated", 0), reverse=True)
    return out


def _auto_key(w):
    # Sticky: rank by the last time the human *started* something there
    # (prompt/session start), not by tool calls, so two busy workers don't
    # make aic flip between them on every tool call.
    return (w.get("state") != "ended", w.get("prompted", 0), w.get("updated", 0))


def worker(root, sid):
    return read_json(os.path.join(aic_dir(root), "workers", sid + ".json"))


def transcript_sessions(root):
    """Sessions found on disk (covers workers started before `aic init`)."""
    skip = compiler_ids(root)
    out = []
    for p in glob.glob(os.path.join(claude_project_dir(root), "*.jsonl")):
        sid = os.path.basename(p)[:-6]
        if sid in skip:
            continue
        out.append({"session_id": sid, "transcript_path": p,
                    "updated": os.path.getmtime(p)})
    out.sort(key=lambda s: s["updated"], reverse=True)
    return out


def pinned(root):
    sid = read_text(os.path.join(aic_dir(root), "current")).strip()
    return sid or None


def attached(root):
    """The worker session aic follows: pinned, else most recent hook-registered,
    else most recent transcript on disk."""
    sid = pinned(root)
    if sid:
        return sid
    ws = sorted(workers(root), key=_auto_key, reverse=True)
    if ws:
        return ws[0]["session_id"]
    ts = transcript_sessions(root)
    return ts[0]["session_id"] if ts else None


def transcript_path(root, sid):
    w = worker(root, sid)
    if w and w.get("transcript_path"):
        return w["transcript_path"]
    p = os.path.join(claude_project_dir(root), sid + ".jsonl")
    return p if os.path.exists(p) else None


def proc_stat(pid):
    """The fields of /proc/<pid>/stat after the command name (state, ppid,
    pgrp, session, tty_nr, tpgid, ...), or None (gone, or not Linux)."""
    try:
        with open("/proc/%d/stat" % int(pid)) as f:
            return f.read().rsplit(")", 1)[1].split()
    except (OSError, ValueError, IndexError, TypeError):
        return None


def tty(pid):
    """The terminal on `pid`'s stdin, or None."""
    try:
        t = os.readlink("/proc/%d/fd/0" % int(pid))
    except (OSError, ValueError, TypeError):
        return None
    return t if t.startswith("/dev/pts/") or t.startswith("/dev/tty") else None


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def now():
    return time.time()
