"""Docs: the human's files, `.aic/docs/<name>.md`, shared by every worker session.

Per worker session (flat files, so the editor's session-dir watcher sees them):
  sent.<doc>.md           what this worker last received of the doc as work
  diagnostics.<doc>.json  last compile of the doc

A doc may start with a marker line, e.g. `<!-- aic: compile prompt=spec -->`:
`compile` = saving it compiles (`compile=NAME`: with compiler template NAME);
`prompt=` = the submit template offered first.
"""
import os
import re

from . import paths

DEFAULT = "spec"
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")
MARKER = re.compile(r"\s*<!--\s*aic:(.*?)-->\s*\Z")


class DocError(Exception):
    pass


def check_name(name):
    if not isinstance(name, str) or not NAME.match(name) or ".." in name:
        raise DocError("bad doc name %r: use letters, digits, _ . - (no /, no ..)" % (name,))
    return name


def docs_dir(root):
    return os.path.join(paths.aic_dir(root), "docs")


def path(root, name):
    return os.path.join(docs_dir(root), check_name(name) + ".md")


def sent_path(sdir, name):
    return os.path.join(sdir, "sent.%s.md" % check_name(name))


def diag_path(sdir, name):
    return os.path.join(sdir, "diagnostics.%s.json" % check_name(name))


def marker(text):
    """Settings from the doc's first line: {"compile": bool, "prompt": str|None}
    (+ "compile_template" for `compile=NAME`)."""
    first = (text or "").split("\n", 1)[0]
    m = MARKER.match(first)
    out = {"compile": False, "prompt": None}
    if m:
        for tok in m.group(1).split():
            k, _, v = tok.partition("=")
            if k == "compile":
                out["compile"] = True
                if v:
                    out["compile_template"] = v
            elif k == "prompt" and v:
                out["prompt"] = v
    return out


def strip_marker(text):
    """The doc without its marker line (what the worker is sent)."""
    first, _, rest = (text or "").partition("\n")
    return rest if MARKER.match(first) else (text or "")


def read(root, name):
    return paths.read_text(path(root, name))


def listing(root):
    """[{name, path, mtime, compile, prompt}], most recently modified first."""
    d = docs_dir(root)
    out = []
    for f in os.listdir(d) if os.path.isdir(d) else []:
        name, ext = os.path.splitext(f)
        if ext != ".md" or not NAME.match(name):
            continue
        p = os.path.join(d, f)
        m = marker(paths.read_text(p))
        out.append({"name": name, "path": os.path.relpath(p, root), "mtime": os.path.getmtime(p),
                    "compile": m["compile"], "compile_template": m.get("compile_template"),
                    "prompt": m["prompt"]})
    out.sort(key=lambda x: -x["mtime"])
    return out


def resolve(root, name):
    """Check `name`; -> the doc's absolute path. A doc that does not exist is
    an error (compile/ask/submit need its text)."""
    p = path(root, name)
    if not os.path.isfile(p):
        raise DocError("no doc %r (%s)" % (name, os.path.relpath(p, root)))
    return p
