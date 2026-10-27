"""`aic compile FILE` / `aic fix FILE`: compile any file, answered like rustc.

The compiler reviews one file for what is wrong with it (bugs, not style) and
answers as rustc would: errors and warnings pointing at lines, notes, and help
with a suggested edit, in rustc's layout and colors.

Context is kept per file: its own resumed compiler session, its last
diagnostics and the text they were made against, in .aic/files/<path>/ of the
aic project around it, else $XDG_STATE_HOME/aic/files/<abs path>/ (nothing is
written into a project that has no .aic/).

`aic fix FILE` applies the suggestions to the file as it is now: each one is
moved past your edits since the compile, ones on lines you changed are
skipped, and git's own hunk picker (`git restore -p`, so your diffFilter and
y/n/s/e) shows only the compiler's hunks, never yours.
"""
import difflib
import os
import re
import subprocess
import sys
import time

from . import compiler, compiler_prompts, fixes, paths, prompts

EDITS = {"type": "array", "items": {
    "type": "object",
    "properties": {"line": {"type": "integer"}, "old": {"type": "string"}, "new": {"type": "string"}},
    "required": ["line", "old", "new"],
}}
LABEL = {"type": "object", "properties": {
    "line": {"type": "integer"}, "snippet": {"type": "string"}, "label": {"type": "string"}},
    "required": ["line"]}
SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "severity": {"type": "string", "enum": ["error", "warning", "note"]},
                "code": {"type": "string"},
                "message": {"type": "string"},
                "line": {"type": "integer"},
                "snippet": {"type": "string"},
                "label": {"type": "string"},
                "secondary": {"type": "array", "items": LABEL},
                "note": {"type": "string"},
                "help": {"type": "string"},
                "edits": EDITS,
            },
            "required": ["severity", "message", "line"],
        }},
    },
    "required": ["summary", "items"],
}
WARNINGS_SCHEMA = {  # aic fix --warnings
    "type": "object",
    "properties": {
        "fixes": {"type": "array", "items": {
            "type": "object",
            "properties": {"item": {"type": "integer"}, "help": {"type": "string"}, "edits": EDITS},
            "required": ["item", "edits"],
        }},
        "skipped": {"type": "array", "items": {
            "type": "object",
            "properties": {"item": {"type": "integer"}, "reason": {"type": "string"}},
            "required": ["item", "reason"],
        }},
    },
    "required": ["fixes"],
}


class Ctx:
    """Where one file's context lives: root (the compiler's cwd), sdir, rel."""

    def __init__(self, path):
        self.path = os.path.realpath(path)  # a symlink: its target (git would replace the link)
        d = os.path.dirname(self.path)
        aic_root = paths.find_root(d)
        self.root = aic_root or _git_top(d) or d
        self.rel = os.path.relpath(self.path, self.root)
        if aic_root:
            self.sdir = os.path.join(paths.aic_dir(aic_root), "files", self.rel)
        else:
            state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
            self.sdir = os.path.join(state, "aic", "files", self.path.lstrip("/"))
        self.diag_path = os.path.join(self.sdir, "diagnostics.json")

    def read(self):
        try:
            with open(self.path, encoding="utf-8", newline="") as f:
                return f.read()
        except FileNotFoundError:
            raise compiler.CompilerError("%s: no such file" % self.path)
        except (OSError, UnicodeError) as e:
            raise compiler.CompilerError("can't read %s: %s" % (self.path, e))


def _git_top(d):
    try:
        p = subprocess.run(["git", "-C", d, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True)
    except OSError:
        return None
    return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None


def _lines(s):
    """Lines with their newline; only "\n" ends one (as in the line numbers)."""
    return re.findall(r"[^\n]*\n|[^\n]+\Z", s)


def _numbered(text):
    """compiler.numbered, but counting lines as _lines does (str.splitlines also splits at \\f, U+2028...)."""
    return "\n".join("%4d| %s" % (i + 1, l.rstrip("\r\n")) for i, l in enumerate(_lines(text))) or "(empty)"


def _unified(a, b, name):
    return "".join(difflib.unified_diff(_lines(a), _lines(b), "a/" + name, "b/" + name))


# -- compile ------------------------------------------------------------------
def compile_prompt(ctx, text, prev):
    t = compiler._template(ctx.root, "file", "file")
    base = prev.get("base")
    values = {
        "file.name": ctx.rel,
        "file": "== FILE `%s` (with line numbers) ==\n%s" % (ctx.rel, _numbered(text)),
        "previous_items": "== YOUR PREVIOUS DIAGNOSTICS FOR THIS FILE (your new list replaces exactly these) ==\n%s" % (
            _brief(prev.get("items")) if prev.get("items") else "(none)"),
        "changes": ("== CHANGED SINCE YOUR LAST COMPILE OF IT ==\n" + _unified(base, text, ctx.rel))
                   if isinstance(base, str) and base != text else "",
    }
    return {"prompt": compiler_prompts.render(t, values), "model": compiler_prompts.model(t, compiler.COMPILE_MODEL),
            "template": t, "note": "compile " + ctx.rel + compiler_prompts.tag(t, "file")}


def _brief(items):
    return "\n".join("%s[%s] line %s: %s" % (it.get("severity"), it.get("code") or "-", it.get("line"),
                                             it.get("message")) for it in items)[:8000]


def compile_file(path, reset=False, dry=False, progress=None):
    """-> the diagnostics dict (dry: {prompt, model}; nothing is asked or written).
    progress(ctx) -> a callback for the compiler's stream-json events (aic compile -v)."""
    ctx = Ctx(path)
    text = ctx.read()
    prev = paths.read_json(ctx.diag_path, {}) or {}
    if reset:
        prev = {}
    c = compile_prompt(ctx, text, prev)
    if dry:
        return {"prompt": c["prompt"], "model": c["model"]}
    if reset:
        compiler._set_state(ctx.sdir, compile=None)
    out = compiler.run_claude(ctx.root, ctx.sdir, "compile", c["model"], c["prompt"], schema=SCHEMA, note=c["note"],
                              progress=progress and progress(ctx))
    res = compiler.structured(out)
    if res is None:
        raise compiler.CompilerError("compiler returned no diagnostics")
    diag = {"file": ctx.rel, "path": ctx.path, "compiled_at": paths.now(), "summary": fixes._oneline(res.get("summary")),
            "items": check(text, res.get("items")), "base": text, "cost_usd": out.get("total_cost_usd")}
    paths.write_json(ctx.diag_path, diag)
    return diag


def check(text, items):
    """Keep well-formed items; resolve their edits to whole-line replacements
    of `text` ({"line", "old", "new"}), or say why the suggestion was dropped."""
    out, taken = [], []
    for it in items or []:
        if not isinstance(it, dict) or not isinstance(it.get("line"), int) or not it.get("message"):
            continue
        it = dict(it, message=fixes._oneline(it["message"]), severity=it.get("severity")
                  if it.get("severity") in ("error", "warning", "note") else "warning")
        for k in ("fix", "fix_error", "no_fix"):  # only aic sets these
            it.pop(k, None)
        taken += _attach(it, text, it.pop("edits", None) or [], taken)
        out.append(it)
    return out


def _attach(it, text, edits, taken):
    """Resolve `edits` against `text` into it["fix"], or say in it["fix_error"]
    why not (also when they overlap `taken`). -> the edits now taken."""
    fix, err = [], None
    for e in edits:
        r = _resolve(text, e) if isinstance(e, dict) else "malformed edit"
        if isinstance(r, str):
            err = r
            break
        fix.append(r)
    if not err and any(_overlap(a, b) for i, a in enumerate(fix) for b in taken + fix[:i]):
        err = "it overlaps another suggestion"
    if err:
        it["fix_error"] = err
        return []
    if fix:
        it.pop("fix_error", None)
        it["fix"] = sorted(fix, key=lambda e: e["line"])
    return fix


def suggest_warnings(ctx, diag, progress=None):
    """aic fix --warnings: ask the file's compiler session for edits to the
    warnings that came without one. They join the diagnostics (so a later
    `aic fix` offers them too, until the next compile); a warning it declines
    keeps its reason in `no_fix` and is not asked again. -> the items given a fix."""
    items = diag.get("items") or []
    todo = [(i, it) for i, it in enumerate(items, 1)
            if it.get("severity") == "warning" and not it.get("fix") and not it.get("no_fix")]
    if not todo:
        return []
    t = compiler._template(ctx.root, "fixfile", "fixfile")
    values = {"file.name": ctx.rel,
              "items": "== WARNINGS TO FIX (numbered; from your last compile of `%s`) ==\n%s" % (ctx.rel, "\n".join(
                  "#%d line %s [%s] %s%s" % (i, it["line"], it.get("code") or "-", it["message"],
                                            "".join(" | %s: %s" % (k, it[k]) for k in ("label", "note", "help")
                                                    if it.get(k))) for i, it in todo))}
    out = compiler.run_claude(ctx.root, ctx.sdir, "compile", compiler_prompts.model(t, compiler.COMPILE_MODEL),
                              compiler_prompts.render(t, values), schema=WARNINGS_SCHEMA,
                              note="fix warnings " + ctx.rel + compiler_prompts.tag(t, "fixfile"),
                              progress=progress and progress(ctx))
    res = compiler.structured(out)
    if res is None:
        raise compiler.CompilerError("compiler returned no fixes")
    by = dict(todo)
    taken = [e for it in items for e in it.get("fix") or []]
    got, answered = [], set()
    for f in res.get("fixes") or []:
        it = by.get(f.get("item")) if isinstance(f, dict) else None
        if it is None or f["item"] in answered:
            continue
        answered.add(f["item"])
        new = _attach(it, diag["base"], f.get("edits") or [], taken)
        if new:
            taken += new
            if f.get("help"):
                it["help"] = fixes._oneline(f["help"])
            got.append(it)
    for sk in res.get("skipped") or []:
        it = by.get(sk.get("item")) if isinstance(sk, dict) else None
        if it is not None and sk["item"] not in answered:
            answered.add(sk["item"])
            it["no_fix"] = fixes._oneline(sk.get("reason")) or "declined"
    for i, it in todo:
        if i not in answered:
            it["no_fix"] = "no edit proposed"
    paths.write_json(ctx.diag_path, diag)
    return got


def _overlap(a, b):
    """Overlapping, or an insertion touching the other (it needs both neighbours as they were)."""
    a0, b0 = a["line"], b["line"]
    a1, b1 = a0 + len(_lines(a["old"])), b0 + len(_lines(b["old"]))
    if a0 == a1 or b0 == b1:
        return a0 <= b1 and b0 <= a1
    return a0 < b1 and b0 < a1


def _resolve(text, e):
    """Edit {line, old, new} -> {"line", "old", "new"}: whole lines of `text`
    from `line` (old) and what replaces them, common lines trimmed off; the
    occurrence of `old` nearest to `line` is the one meant. Or an error string."""
    old, new, hint = e.get("old"), e.get("new"), e.get("line")
    if not isinstance(old, str) or not isinstance(new, str) or not isinstance(hint, int):
        return "malformed edit"
    if not old:
        return "empty `old`"
    bad = prompts.hidden(new)
    if bad:
        return "its new text has invisible characters (%s)" % " ".join(bad)
    eol = "\r\n" if text.count("\r\n") * 2 > text.count("\n") else "\n"
    if eol == "\r\n":  # a CRLF file: the model saw it without \r
        old, new = (x.replace("\r\n", "\n").replace("\n", "\r\n") for x in (old, new))
    starts = _starts(text, old)
    if not starts:
        bare = fixes._unnumber(old)
        if bare and _starts(text, bare):
            old, new, starts = bare, fixes._unnumber(new) or new, _starts(text, bare)
    if not starts:
        return "its text is not in the file"
    s = min(starts, key=lambda i: abs(text.count("\n", 0, i) + 1 - hint))
    a = text.rfind("\n", 0, s) + 1
    b = text.find("\n", max(s + len(old) - 1, s))
    b = len(text) if b < 0 else b + 1
    seg_old, seg_new = text[a:b], text[a:s] + new + text[s + len(old):b]
    if seg_old.endswith("\n") and seg_new and not seg_new.endswith("\n"):
        seg_new += eol
    o, n = _lines(seg_old), _lines(seg_new)
    line = text.count("\n", 0, a) + 1
    while o and n and o[0] == n[0]:
        o, n, line = o[1:], n[1:], line + 1
    while o and n and o[-1] == n[-1]:
        o, n = o[:-1], n[:-1]
    if not o and not n:
        return "it changes nothing"
    return {"line": line, "old": "".join(o), "new": "".join(n)}


def _starts(text, s):
    out, i = [], text.find(s)
    while i >= 0:
        out.append(i)
        i = text.find(s, i + 1)
    return out


# -- fix ----------------------------------------------------------------------
EDGE = ("\0aic-start\n", "\0aic-end\n")  # sentinel lines: the file's start and end are neighbours too


def place(base, cur, diag_items):
    """Move the suggestions of `base` onto `cur`. -> (target text, report):
    report = [(item, status, line now)], status: "ok" | "applied" (already
    in cur, where it goes) | "changed" (you edited it or around it since the
    compile). A replacement needs its lines and a neighbour unchanged, an
    insertion both neighbours: so it never lands on a look-alike line of yours."""
    b, c = [EDGE[0]] + _lines(base) + [EDGE[1]], [EDGE[0]] + _lines(cur) + [EDGE[1]]
    blocks = difflib.SequenceMatcher(None, b, c, autojunk=False).get_matching_blocks()

    def at(lo, hi):  # where base lines lo..hi-1 are in cur, unchanged together, or None
        k = next((k for k in blocks if k.a <= lo and hi <= k.a + k.size), None)
        return None if k is None else k.b + lo - k.a

    todo, report = [], []
    for it in diag_items:
        if not it.get("fix"):
            continue
        moved, done, where = [], True, None
        for e in it["fix"]:
            la = e["line"]  # (index in b: the start sentinel is b[0])
            lb, new = la + len(_lines(e["old"])), _lines(e["new"])
            p, n = at(la - 1, la), at(lb, lb + 1)
            done = done and p is not None and n is not None and c[p + 1:n] == new
            where = where or (p + 1 if p is not None else e["line"])
            if lb > la:
                s = at(la - 1, lb)
                s = s + 1 if s is not None else at(la, lb + 1)
            else:
                s = p + 1 if at(la - 1, la + 1) is not None else None
            moved.append(None if s is None else (s, s + lb - la, new))
        if done:
            report.append((it, "applied", where))
        elif all(moved):
            todo += moved
            report.append((it, "ok", moved[0][0]))
        else:
            report.append((it, "changed", where))
    for s, t, new in sorted(todo, reverse=True):
        c[s:t] = new
    return "".join(c[1:-1]), report


def fix_file(path, apply_all=False, out=None, color=None, warnings=False, progress=None):
    """Apply the last compile's suggestions to FILE: hunk by hunk through
    `git restore -p`, or all of them (apply_all). warnings: first ask for
    edits to the warnings that have none (suggest_warnings). -> exit code."""
    out = out or sys.stderr
    ctx = Ctx(path)
    diag = paths.read_json(ctx.diag_path)
    if not isinstance(diag, dict) or not isinstance(diag.get("base"), str):
        raise compiler.CompilerError("no diagnostics for %s; run `aic compile %s` first" % (ctx.rel, path))
    st = Style(out if color is None else color)
    with paths.locked(os.path.join(ctx.sdir, "fix")):
        if warnings:
            asked = sum(1 for it in diag.get("items") or []
                        if it.get("severity") == "warning" and not it.get("fix") and not it.get("no_fix"))
            if asked and not progress:
                out.write("%s %d warning%s of %s (asking the compiler)\n" % (
                    st("note", "Fixing".rjust(12)), asked, "" if asked == 1 else "s", ctx.rel))
                out.flush()
            lines = _lines(diag["base"])
            for it in suggest_warnings(ctx, diag, progress):
                out.write(render_item(it, lines, ctx.rel, st) + "\n\n")
            for it in diag.get("items") or []:
                if it.get("severity") == "warning" and it.get("no_fix") and not it.get("fix"):
                    out.write("%s: no edit for %s:%d (%s): %s\n" % (
                        st("note", "note"), ctx.rel, it["line"], it.get("code") or it["message"], it["no_fix"]))
        return _fix(ctx, diag, apply_all, out, st)


def _fix(ctx, diag, apply_all, out, st):
    cur = ctx.read()
    target, report = place(diag["base"], cur, diag.get("items") or [])
    for it, status, line in report:
        if status == "changed":
            out.write("%s: suggestion for %s:%d skipped: you changed those lines since `aic compile` (%s)\n" % (
                st("warning", "warning"), ctx.rel, line, it.get("help") or it["message"]))
    if target == cur:
        n = sum(1 for _, s, _ in report if s == "applied")
        out.write("nothing to apply%s\n" % (" (%d already applied)" % n if n else ""))
        return 0
    if ctx.read() != cur:  # saved meanwhile: the picker would offer to revert it
        raise compiler.CompilerError("%s changed while aic read it; run `aic fix` again" % ctx.rel)
    if apply_all:
        with open(ctx.path, "w", encoding="utf-8", newline="") as f:
            f.write(target)
        out.write("applied %d suggestions to %s\n" % (sum(1 for _, s, _ in report if s == "ok"), ctx.rel))
        return 0
    return _git_pick(ctx, cur, target)


def _git_pick(ctx, cur, target):
    """git's hunk picker for cur -> target, from a private git dir: the index
    holds `cur`, the source tree `target`; your repo is never touched."""
    gdir = os.path.join(ctx.sdir, "git")
    name = os.path.basename(ctx.path)
    # nothing inherited may point git elsewhere (GIT_OBJECT_DIRECTORY...); your config still applies
    clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k.startswith("GIT_CONFIG")}
    env = dict(clean, GIT_DIR=gdir, GIT_WORK_TREE=os.path.dirname(ctx.path),
               GIT_INDEX_FILE=os.path.join(gdir, "aic-index"), GIT_LITERAL_PATHSPECS="1")

    def git(*args, data=None, env=env):
        p = subprocess.run(["git"] + list(args), input=data, env=env, capture_output=True, text=True,
                           encoding="utf-8")
        if p.returncode:
            raise compiler.CompilerError("git %s: %s" % (args[0], p.stderr.strip()[:300]))
        return p.stdout.strip()

    if not os.path.isdir(gdir):
        os.makedirs(ctx.sdir, exist_ok=True)
        git("init", "-q", "--bare", gdir, env=clean)
        # the file's .gitattributes must not convert or filter what the picker shows and writes
        paths.write_text(os.path.join(gdir, "info", "attributes"), "* -text -filter -ident !eol\n")
    mode = "100755" if os.stat(ctx.path).st_mode & 0o100 else "100644"
    git("read-tree", "--empty")
    git("update-index", "--add", "--cacheinfo", "%s,%s,%s" % (mode, git("hash-object", "-w", "--stdin", data=cur), name))
    tree = git("mktree", "-z", data="%s blob %s\t%s\0" % (mode, git("hash-object", "-w", "--stdin", data=target), name))
    return subprocess.run(["git", "restore", "-p", "--source=" + tree, "--worktree", "--", name],
                          env=env, cwd=os.path.dirname(ctx.path)).returncode


# -- rustc-style output -------------------------------------------------------
COLORS = {"error": "1;38;5;9", "warning": "1;38;5;11", "note": "1;38;5;10", "help": "1;38;5;14",
          "blue": "1;38;5;12", "bold": "1", "red": "38;5;9", "green": "38;5;10"}


class Style:
    def __init__(self, on):
        if not isinstance(on, bool):  # a stream: color when it is a terminal
            on = hasattr(on, "isatty") and on.isatty() and not os.environ.get("NO_COLOR")
        self.on = on

    def __call__(self, name, s):
        return "\x1b[%sm%s\x1b[0m" % (COLORS[name], s) if self.on and s else s


class Progress:
    """aic compile -v: what the compiler does while it compiles, as cargo
    prints its steps: a green verb, then what it touched."""

    def __init__(self, ctx, st, out=None):
        self.ctx, self.st, self.out, self.t0 = ctx, st, out or sys.stderr, time.time()

    def step(self, verb, what, name="note"):
        self.out.write("%s %s\n" % (self.st(name, verb.rjust(12)), what))
        self.out.flush()

    def _path(self, p):
        p = p or "."
        r = os.path.relpath(p, self.ctx.root) if os.path.isabs(p) else p
        return p if r.startswith("..") else r

    def __call__(self, ev):
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            self.step("Checking", "%s with %s" % (self.ctx.rel, ev.get("model") or "?"))
        elif t == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if c.get("type") == "text" and c.get("text", "").strip():
                    for l in c["text"].strip().splitlines():
                        self.out.write(" " * 13 + l + "\n")
                elif c.get("type") == "tool_use":
                    self._tool(c.get("name"), c.get("input") or {})
        elif t == "user":
            for c in (ev.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error"):
                    msg = c.get("content")
                    msg = msg if isinstance(msg, str) else " ".join(x.get("text", "") for x in msg or []
                                                                    if isinstance(x, dict))
                    self.step("Failed", fixes._oneline(msg)[:160], "warning")
        elif t == "result":
            self.step("Finished", "%s in %.1fs ($%.2f, %d turns)" % (
                self.ctx.rel, time.time() - self.t0, float(ev.get("total_cost_usd") or 0), ev.get("num_turns") or 0))
        self.out.flush()

    def _tool(self, name, i):
        if name == "Read":
            off, lim = i.get("offset"), i.get("limit")
            rng = ":%s-%s" % (off or 1, (off or 1) + lim - 1) if lim else ":%s-" % off if off else ""
            self.step("Reading", self._path(i.get("file_path")) + rng)
        elif name == "Grep":
            self.step("Searching", '"%s" in %s%s' % (i.get("pattern"), self._path(i.get("path")),
                                                     " (%s)" % i["glob"] if i.get("glob") else ""))
        elif name == "Glob":
            self.step("Listing", "%s in %s" % (i.get("pattern"), self._path(i.get("path"))))
        elif name == "StructuredOutput":
            k = "items" if "items" in i else "fixes"
            self.step("Reporting", "%d %s" % (len(i.get(k) or []), k))
        else:
            self.step("Running", name or "?")


def _row(segs, st):
    """Text with (col, text, style) segments at their columns."""
    out, at = "", 0
    for col, s, name in sorted(segs, key=lambda x: x[0]):
        out += " " * max(col - at, 0) + st(name, s)
        at = max(col, at) + len(s)
    return out


def _mark(src, lab):
    """-> (col, width) of a label's snippet in a source line (0-based)."""
    snip = (lab.get("snippet") or "").expandtabs(4)
    col = src.find(snip) if snip.strip() else -1
    if col < 0:
        col = len(src) - len(src.lstrip())
        return col, max(len(src.strip()), 1)
    return col, max(len(snip), 1)


def _hl(a, b, st):
    """Old/new line with only the changed characters colored, as rustc does."""
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    x = y = ""
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        x += a[i1:i2] if op == "equal" else st("red", a[i1:i2])
        y += b[j1:j2] if op == "equal" else st("green", b[j1:j2])
    return x, y


def render_item(it, lines, fname, st):
    level = it["severity"]
    rows = [st(level, level + ("[%s]" % it["code"] if it.get("code") else "")) + st("bold", ": " + it["message"])]
    labels = [dict(it, primary=True)] + [dict(x, primary=False) for x in it.get("secondary") or []
                                         if isinstance(x, dict) and isinstance(x.get("line"), int)]
    labels = [x for x in labels if 1 <= x["line"] <= len(lines)]
    fix = it.get("fix") or []
    nums = [x["line"] for x in labels] + [e["line"] + max(len(_lines(e["old"])), len(_lines(e["new"])), 1) - 1
                                          for e in fix]
    w = len(str(max(nums or [it["line"]])))
    pad, bar = " " * w, st("blue", "|")
    col = _mark(lines[it["line"] - 1].rstrip("\r\n").expandtabs(4), it)[0] if labels and labels[0]["primary"] else 0
    rows.append("%s%s%s:%d:%d" % (pad, st("blue", "--> "), fname, it["line"], col + 1))
    gutter = lambda n="": "%s %s" % (st("blue", str(n).rjust(w)) if n else pad, bar)  # noqa: E731
    if labels:
        rows.append(gutter())
        prev = None
        for n in sorted({x["line"] for x in labels}):
            if prev is not None and n - prev > 1:
                rows.append(st("blue", "..."))
            prev = n
            src = lines[n - 1].rstrip("\r\n").expandtabs(4)
            rows.append(gutter(n) + " " + src)
            marks = []
            for x in labels:
                if x["line"] == n:
                    c, wd = _mark(src, x)
                    name = level if x["primary"] else "blue"
                    marks.append((c, ("^" if x["primary"] else "-") * wd, name, (x.get("label") or "").strip()))
            marks.sort()
            last = marks[-1]
            rows.append(gutter() + " " + _row([(c, m, nm) for c, m, nm, _ in marks]
                                              + ([(last[0] + len(last[1]) + 1, last[3], last[2])] if last[3] else []), st))
            rest = [m for m in marks[:-1] if m[3]]
            if rest:
                rows.append(gutter() + " " + _row([(c, "|", "blue") for c, _, _, _ in rest], st))
                for i in range(len(rest) - 1, -1, -1):
                    c, _, nm, text = rest[i]
                    rows.append(gutter() + " " + _row([(x[0], "|", "blue") for x in rest[:i]] + [(c, text, nm)], st))
        rows.append(gutter())
    if it.get("note"):
        rows.append("%s %s %s: %s" % (pad, st("blue", "="), st("bold", "note"), it["note"]))
    if it.get("fix_error"):
        rows.append("%s %s %s: aic dropped the suggested edit: %s" % (pad, st("blue", "="), st("bold", "note"),
                                                                     it["fix_error"]))
    if it.get("help") and not fix:
        rows.append("%s %s %s: %s" % (pad, st("blue", "="), st("bold", "help"), it["help"]))
    if fix:
        rows.append(st("help", "help") + ": " + (it.get("help") or "apply this change"))
        rows.append(gutter())
        for e in fix:
            o, n = [l.rstrip("\r\n") for l in _lines(e["old"])], [l.rstrip("\r\n") for l in _lines(e["new"])]
            pairs = min(len(o), len(n))
            hl = [_hl(o[i], n[i], st) for i in range(pairs)]
            for i, l in enumerate(o):
                rows.append("%s %s%s" % (st("blue", str(e["line"] + i).rjust(w)), st("red", "- "),
                                         hl[i][0] if i < pairs else st("red", l)))
            for i, l in enumerate(n):
                rows.append("%s %s%s" % (st("blue", str(e["line"] + i).rjust(w)), st("green", "+ "),
                                         hl[i][1] if i < pairs else st("green", l)))
        rows.append(gutter())
    return "\n".join(rows)


def render(diag, path, st):
    lines = _lines(diag["base"])
    out = [render_item(it, lines, diag["file"], st) + "\n" for it in diag["items"]]
    count = lambda sev: sum(1 for it in diag["items"] if it["severity"] == sev)  # noqa: E731
    errs, warns = count("error"), count("warning")
    plural = lambda n, w: "%d %s%s" % (n, w, "" if n == 1 else "s")  # noqa: E731
    fixable = sum(1 for it in diag["items"] if it.get("fix"))
    if errs:
        out.append(st("error", "error") + st("bold", ": could not compile `%s` due to %s%s" % (
            diag["file"], plural(errs, "previous error"), "; " + plural(warns, "warning") + " emitted" if warns else "")))
    elif warns:
        out.append(st("warning", "warning") + st("bold", ": `%s` generated %s" % (diag["file"], plural(warns, "warning"))))
    if fixable:
        out.append(st("help", "help") + st("bold", ": run `aic fix %s` to apply %s" % (path, plural(fixable, "suggestion"))))
    bare = sum(1 for it in diag["items"] if it["severity"] == "warning" and not it.get("fix") and not it.get("no_fix"))
    if bare:
        out.append(st("help", "help") + st("bold", ": `aic fix %s --warnings` asks for edits to %s without one" % (
            path, plural(bare, "warning"))))
    if not diag["items"]:
        out.append("%s %s" % (st("note", "    Finished"), diag["summary"] or "no problems found"))
    return "\n".join(out) + "\n"
