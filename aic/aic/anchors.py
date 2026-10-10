"""Anchors: `@path`, `@path:42`, `@path:10-20`, `@path:symbol`, `@dir/`,
`@/abs` `@~/home`, `@diff`, `@diff:path`, `@staged`, `@log`, `@log:path`,
`@show:REV`, `@turn:N`, `@turn:last`.

Parsing follows PROTOCOL.md exactly and must agree with the Neovim client
(both run tests/anchor_cases.json). Resolving turns each anchor into a bounded
block of context that the backend inlines for the compiler (compile, ask) or
the worker (submit).

Safety: anchors can come from text the human did not write (code comments,
compiler answers), so content from outside the project is only resolved when
the caller says the text is the human's own (`allow_outside`), credential-like
paths are never read, and `--add-dir` is only granted for explicit outside
directories, never for `/`, the home directory or a parent of the project.
"""
import fnmatch
import os
import re
import stat
import subprocess

from . import paths

_START = set(" \t\r\n([{\"',")
_SHOW = re.compile(r"show:[\w./^~@{}-]+")
_TOKEN = re.compile(r"[^\s()\[\]{}\"'`,;<>|]+")
_TRAIL = ".,;:)]"
_SYMBOL = re.compile(r"^[A-Za-z_][\w.]*$")

MAX_FILE_LINES = 400
MAX_BLOCK_CHARS = 20000
MAX_DIR_ENTRIES = 200

# Never read these, whoever wrote the anchor.
SENSITIVE = ["~/.ssh/*", "~/.gnupg/*", "~/.aws/*", "~/.config/gh/*", "~/.netrc", "~/.docker/config.json",
             "~/.kube/*", "~/.claude/.credentials*", "*.pem", "*.key", "*/id_rsa*", "*/id_ed25519*",
             "*/id_ecdsa*", "*/.env", "*/.env.*", "*/.git-credentials"]


def _abs(root, p):
    if p.startswith("~/"):
        return os.path.normpath(os.path.expanduser(p))
    if p.startswith("/"):
        return os.path.normpath(p)
    return os.path.normpath(os.path.join(root, p))


def sensitive(path):
    real = os.path.realpath(path)
    home = os.path.expanduser("~")
    for pat in SENSITIVE:
        pat = pat.replace("~", home, 1) if pat.startswith("~") else pat
        if fnmatch.fnmatch(real, pat) or fnmatch.fnmatch(path, pat):
            return True
    return False


def outside(a, root):
    """By real path, so a symlink in the project pointing out counts as outside."""
    ap = a.get("abs")
    if not ap:
        return False
    real, rroot = os.path.realpath(ap), os.path.realpath(root)
    return not (real == rroot or real.startswith(rroot + os.sep))


def _strip(tok):
    """Trailing punctuation is not part of an anchor, except a `..` path part
    (`@log:../..`) and a `}` that closes a `{` (`@show:HEAD@{1}`)."""
    while tok:
        body = tok.split(":", 1)[1] if tok.startswith(("diff:", "log:")) else tok
        if tok.endswith("/..") or body == "..":
            return tok
        if tok[-1] in _TRAIL:
            tok = tok[:-1]
        elif tok[-1] == "}" and tok.count("}") > tok.count("{"):
            tok = tok[:-1]
        else:
            return tok
    return tok


def parse(text, root):
    """-> list of anchor dicts in order, one per occurrence (valid ones only)."""
    out = []
    text = text or ""
    i = 0
    while True:
        i = text.find("@", i)
        if i < 0:
            return out
        if i > 0 and text[i - 1] not in _START:
            i += 1
            continue
        m = _SHOW.match(text, i + 1) or _TOKEN.match(text, i + 1)
        i += 1
        if not m:
            continue
        tok = _strip(m.group(0))
        a = _parse_one(tok, root) if tok else None
        if a:
            out.append(a)
            i += len(tok)


def _parse_one(raw, root):
    if raw in ("diff", "staged", "log"):
        return {"anchor": "@" + raw, "kind": raw, "path": None}
    for k in ("diff", "log"):
        if raw.startswith(k + ":") and len(raw) > len(k) + 1:
            p = raw[len(k) + 1:]
            return {"anchor": "@" + raw, "kind": k, "path": p, "abs": _abs(root, p)}
    if raw.startswith("show:"):
        rev = raw[5:]
        if rev and not rev.startswith("-") and re.fullmatch(r"[\w./^~@{}-]+", rev):
            return {"anchor": "@" + raw, "kind": "show", "rev": rev}
        return None
    m = re.fullmatch(r"turn:(\d+|last)", raw)
    if m:
        return {"anchor": "@" + raw, "kind": "turn", "turn": m.group(1)}
    path, _, suffix = raw.partition(":")
    line = end = symbol = None
    if suffix:
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", suffix)
        if m:
            line = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else None
            if line < 1 or (end is not None and end < line):
                return None
        elif _SYMBOL.match(suffix):
            symbol = suffix
        else:
            return None
    ap = _abs(root, path)
    if os.path.isdir(ap):
        if suffix:
            return None
        return {"anchor": "@" + raw, "kind": "dir", "path": path, "abs": ap}
    if not os.path.isfile(ap) or path.endswith("/"):
        return None
    kind = "symbol" if symbol else ("range" if end is not None else ("line" if line else "file"))
    return {"anchor": "@" + raw, "kind": kind, "path": path, "abs": ap,
            "line": line, "end": end, "symbol": symbol}


# -- resolution ---------------------------------------------------------------
def _clip(s):
    return s if len(s) <= MAX_BLOCK_CHARS else s[:MAX_BLOCK_CHARS] + "\n…(truncated)"


def _git(root, *args):
    # No fsmonitor/external diff/textconv: a worker-edited .git/config can't
    # make an anchor run commands.
    cmd = ["git", "-c", "core.fsmonitor=", "-C", root] + list(args)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)
    if p.returncode != 0:
        return None, p.stderr.strip() or "git failed"
    return p.stdout, None


def find_symbol(lines, symbol):
    """1-based line of the best match: a definition-looking line, else any word match."""
    name = re.escape(symbol.split(".")[-1])
    defs = re.compile(r"(\b(function|def|class|const|let|var|fn|func|type|interface|struct|enum)\s+%s\b)"
                      r"|(\b%s\s*[:=]\s*(async\s+)?(function|\(|[A-Za-z_]+\s*=>))"
                      r"|(^\s*(async\s+)?%s\s*\()" % (name, name, name))
    word = re.compile(r"\b%s\b" % name)
    for i, l in enumerate(lines):
        if defs.search(l):
            return i + 1
    for i, l in enumerate(lines):
        if word.search(l):
            return i + 1
    return None


def _read_lines(path, limit):
    """At most `limit` lines (plus the total count); refuses non-regular and binary files."""
    st = os.stat(path)
    if not stat.S_ISREG(st.st_mode):
        raise ValueError("not a regular file")
    with open(path, "rb") as f:
        if b"\0" in f.read(8192):
            raise ValueError("binary file, %d bytes" % st.st_size)
    lines, total = [], 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for l in f:
            total += 1
            if total <= limit:
                lines.append(l.rstrip("\n"))
    return lines, total


def _turn_events(root, sid, which):
    from .compiler import fmt_events  # local: compiler imports this module
    trace = paths.read_jsonl(os.path.join(paths.session_dir(root, sid), "trace.jsonl")) if sid else []
    turns = [e.get("turn") for e in trace if e.get("kind") == "prompt" and not e.get("midturn")]
    turns = [t for t in turns if isinstance(t, int)]
    if not turns:
        return None
    n = max(turns) if which == "last" else int(which)
    evs = [e for e in trace if e.get("turn") == n]
    return n, (fmt_events(evs, budget=MAX_BLOCK_CHARS) if evs else None)


def resolve(a, root, sid=None):
    """Fill a["text"] (bounded content), and a["line"] for symbols; returns a."""
    k = a["kind"]
    if a.get("abs") and k not in ("diff", "log") and sensitive(a["abs"]):
        a["text"] = "(refused: credential-like path)"
        return a
    if k in ("diff", "staged"):
        args = ["diff", "--no-ext-diff", "--no-textconv"] + (["--cached"] if k == "staged" else ["HEAD"])
        if a.get("path"):
            args += ["--", a["abs"]]
        out, err = _git(root, *args)
        if out is None and k == "diff":  # no commit yet: plain working-tree diff
            out, err = _git(root, *([x for x in args if x != "HEAD"]))
        a["text"] = _clip(out) if out else ("(no changes)" if err is None else "(git: %s)" % err)
        return a
    if k == "log":
        args = ["log", "--oneline", "--decorate", "-n", "30"]
        if a.get("path"):
            args = ["log", "-n", "15", "--format=%h %ad %an  %s", "--date=short", "--stat", "--", a["abs"]]
        out, err = _git(root, *args)
        a["text"] = _clip(out) if out else "(git: %s)" % (err or "no history")
        return a
    if k == "show":
        out, err = _git(root, "show", "--no-ext-diff", "--no-textconv", "--stat", "--patch",
                        "--format=commit %H%nAuthor: %an%nDate: %ad%n%n    %s%n", a["rev"], "--")
        a["text"] = _clip(out) if out else "(git: %s)" % err
        return a
    if k == "turn":
        got = _turn_events(root, sid, a["turn"])
        if not got:
            a["text"] = "(no such turn)"
        else:
            a["turn"], txt = got
            a["text"] = _clip(txt or "(nothing recorded in turn %s)" % got[0])
        return a
    if k == "dir":
        try:
            names = sorted(os.listdir(a["abs"]))
        except OSError as e:
            a["text"] = "(%s)" % e
            return a
        names = [n + ("/" if os.path.isdir(os.path.join(a["abs"], n)) else "") for n in names
                 if n not in (".git", "node_modules", ".aic")]
        a["text"] = "\n".join(names[:MAX_DIR_ENTRIES]) + ("\n…" if len(names) > MAX_DIR_ENTRIES else "")
        return a
    want = MAX_FILE_LINES
    if k in ("line", "range"):
        want = max(want, (a.get("end") or a["line"]) + 5)
    if k == "symbol":
        want = 20000
    try:
        lines, total = _read_lines(a["abs"], want)
    except (OSError, ValueError) as e:
        a["text"] = "(%s)" % e
        return a
    if k == "symbol":
        a["line"] = find_symbol(lines, a["symbol"])
        if a["line"] is None:
            a["text"] = "(symbol %s not found)" % a["symbol"]
            return a
        first, last = max(1, a["line"] - 3), min(len(lines), a["line"] + 40)
    elif k in ("range", "line"):
        if a["line"] > total:
            a["text"] = "(file has %d lines)" % total
            return a
        if k == "range":
            first, last = a["line"], min(total, a["end"])
        else:
            first, last = max(1, a["line"] - 5), min(total, a["line"] + 5)
    else:
        first, last = 1, min(total, MAX_FILE_LINES)
    body = "\n".join("%5d| %s" % (n, lines[n - 1]) for n in range(first, last + 1) if n <= len(lines))
    if k == "file" and total > MAX_FILE_LINES:
        body += "\n…(%d more lines)" % (total - MAX_FILE_LINES)
    a["text"] = _clip(body)
    return a


def label(a):
    k = a["kind"]
    if k == "symbol" and a.get("line"):
        return "%s (%s line %s)" % (a["anchor"], a["path"], a["line"])
    if k == "turn":
        return "@turn:%s (worker activity)" % a.get("turn")
    if k in ("diff", "staged"):
        return a["anchor"] + " (git %s)" % ("diff --cached" if k == "staged" else "diff HEAD")
    if k == "log":
        return a["anchor"] + " (git log)"
    if k == "show":
        return a["anchor"] + " (git show)"
    return a["anchor"]


def blocks(text, root, sid=None, for_worker=False, allow_outside=True):
    """Resolved context blocks for every distinct anchor in `text`, as one string.
    for_worker: whole in-project files and dirs stay paths (the worker reads
    them), and outside-project content is never pasted to the worker.
    allow_outside=False: text not typed by the human; outside paths are skipped."""
    out, seen = [], set()
    for a in parse(text, root):
        if a["anchor"] in seen:
            continue
        seen.add(a["anchor"])
        is_out = a["kind"] not in ("diff", "log") and outside(a, root)
        if is_out and (for_worker or not allow_outside):
            if for_worker:
                out.append("### %s\n(outside the project: open it yourself if you need it)" % a["anchor"])
            continue
        if for_worker and a["kind"] in ("file", "dir"):
            continue
        resolve(a, root, sid)
        lang = "diff" if a["kind"] in ("diff", "staged", "show") else ""
        out.append("### %s\n```%s\n%s\n```" % (label(a), lang, a.get("text", "")))
    return "\n\n".join(out)


def add_dirs(texts, root):
    """Outside-project directories the compiler may Read: only explicit `@dir/`
    anchors (file anchors are inlined already), never `/`, the home directory,
    a parent of the project, or a credential-like path."""
    home = os.path.realpath(os.path.expanduser("~"))
    rroot = os.path.realpath(root)
    dirs = set()
    for t in texts:
        for a in parse(t, root):
            if a["kind"] != "dir" or not outside(a, root):
                continue
            real = os.path.realpath(a["abs"])
            if real in ("/", home) or rroot.startswith(real + os.sep) or sensitive(real) \
                    or sensitive(os.path.join(real, "x")):
                continue
            dirs.add(real)
    return sorted(dirs)
