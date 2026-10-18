"""Prompt templates: what a submit sends to the worker.

A template is markdown with a small front matter:

    ---
    description: one line for the picker
    header: "Typed as your own words: asks the worker to act on the body"
    requires: [changed]      # refuse to render while these are empty
    marks_sent: true         # the doc counts as sent (diff base for the next submit)
    input: Message           # {{input}} is asked for with this prompt
    ---
    body: pasted under the header

The header is typed into the worker as the human, the body pasted (Claude Code
treats pasted text as data unless typed words ask for it). Syntax, in header
and body: {{var}}, {{#var}}..{{/var}} (kept if non-empty), {{^var}}..{{/var}}
(kept if empty), \\{{ for a literal {{. Whitespace-only counts as empty. A line
holding only a section tag disappears with its newline. Values are inserted
once and never re-scanned.

Lookup, first match wins: project .aic/prompts/, user
$XDG_CONFIG_HOME/aic/prompts/, built-in aic/prompts/.
"""
import difflib
import hashlib
import os
import re
import unicodedata

from . import anchors, docs, paths

BUILTIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")
KEYS = {"description", "header", "requires", "marks_sent", "input"}
VARS = ["doc", "doc.name", "selection", "selection.lines", "content", "previous", "changed", "diff",
        "anchors", "hand_edits", "diagnostics", "input"]
TAG = re.compile(r"\\\{\{|\{\{([#^/]?)\s*([\w.]+)\s*\}\}")
STANDALONE = re.compile(r"[ \t]*\{\{[#^/]\s*[\w.]+\s*\}\}[ \t]*\n?\Z")
# Typed as the human: no digits (a dialog could take them as menu choices),
# no @ (opens Claude Code's file picker, Enter accepts), no \, and nothing
# invisible: control/format/private/unassigned characters (zero-width, bidi,
# Unicode tags) would type words the human never saw in the preview.
HEADER_ASCII_BAD = set("@\\")
INVISIBLE = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
MAX_HEADER = 300
# in the pasted body: hidden text (Unicode tags) and bidi overrides
BODY_BAD = re.compile("[\U000E0000-\U000E007F\u202A-\u202E\u2066-\u2069]")
PREFIX = "[aic] "
ANCHOR_INTRO = "\n\nReferenced context (resolved from my @anchors):\n\n"
EDITS_INTRO = "\n\nMy hand edits since my last message: "


class PromptError(Exception):
    pass


# -- loading ------------------------------------------------------------------
def dirs(root):
    """[(source, dir)] in lookup order."""
    cfg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return [("project", os.path.join(paths.aic_dir(root), "prompts")),
            ("user", os.path.join(cfg, "aic", "prompts")),
            ("built-in", BUILTIN_DIR)]


QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"\s*(#.*)?\Z|\'([^\']*)\'\s*(#.*)?\Z')


def _value(raw):
    """A front-matter value: "quoted" (\\" inside) or 'quoted', then an
    optional # comment; unquoted: a ` #` starts a comment (quote values with #)."""
    raw = raw.strip()
    m = QUOTED.match(raw)
    if m:
        return m.group(1).replace('\\"', '"') if m.group(1) is not None else m.group(3)
    if raw[:1] in ('"', "'"):
        raise PromptError("unclosed quote or text after the closing quote: %s" % raw)
    raw = re.sub(r"\s+#.*$", "", raw)  # trailing comment
    if raw.startswith("[") and raw.endswith("]"):
        return [x.strip().strip("\"'") for x in raw[1:-1].split(",") if x.strip()]
    if raw in ("true", "false"):
        return raw == "true"
    return raw


def parse(text, name="?"):
    """-> (meta dict, body). Front matter: `key: value` lines between `---` lines."""
    text = text.replace("\r\n", "\n")
    meta, body = {}, text
    if text.startswith("---\n"):
        end = text.find("\n---", 3)
        if end < 0:
            raise PromptError("template %s: front matter is not closed with ---" % name)
        for i, line in enumerate(text[4:end].split("\n")):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            k, sep, v = line.partition(":")
            k = k.strip()
            if not sep or k not in KEYS:
                raise PromptError("template %s: front matter line %d: unknown key %r (known: %s)"
                                  % (name, i + 2, k, ", ".join(sorted(KEYS))))
            try:
                meta[k] = _value(v)
            except PromptError as e:
                raise PromptError("template %s: front matter line %d: %s" % (name, i + 2, e))
        body = text[end + 4:]
        body = body[1:] if body.startswith("\n") else body
    if not isinstance(meta.get("header"), str) or not meta["header"].strip():
        raise PromptError("template %s: needs a `header:` (your typed request to the worker)" % name)
    req = meta.get("requires") or []
    meta["requires"] = req if isinstance(req, list) else [req]
    for r in meta["requires"]:
        if r not in VARS:
            raise PromptError("template %s: requires unknown %r (known: %s)" % (name, r, ", ".join(VARS)))
    meta["marks_sent"] = meta.get("marks_sent") is True
    _tree(meta["header"], name)  # syntax errors surface at load time
    _tree(body, name)
    return meta, body


def _check_name(name):
    try:
        return docs.check_name(name)
    except docs.DocError:
        raise PromptError("bad template name %r: use letters, digits, _ . - (no /, no ..)" % (name,))


def find(root, name, builtin_only=False):
    """-> dict(name, source, path, meta, body) for template `name`."""
    _check_name(name)
    for source, d in dirs(root):
        if builtin_only and source != "built-in":
            continue
        p = os.path.join(d, name + ".md")
        if os.path.isfile(p):
            meta, body = parse(paths.read_text(p), name)
            return {"name": name, "source": source, "path": p, "meta": meta, "body": body}
    raise PromptError("no prompt template %r (looked in %s)" % (
        name, ", ".join(d for _, d in dirs(root))))


def listing(root):
    """Every template name once (the one that wins; `overrides` = the sources it
    shadows), sorted; broken ones carry `error`."""
    seen, out = {}, []
    for source, d in dirs(root):
        try:
            files = sorted(os.listdir(d)) if os.path.isdir(d) else []
        except OSError:
            continue
        for f in files:
            name, ext = os.path.splitext(f)
            if ext != ".md" or not docs.NAME.match(name) or ".." in name:
                continue
            if name in seen:
                seen[name]["overrides"].append(source)
                continue
            p = os.path.join(d, f)
            item = {"name": name, "source": source, "path": p, "overrides": []}
            seen[name] = item
            try:
                meta, body = parse(paths.read_text(p), name)
                item.update(description=meta.get("description") or "", header=meta["header"],
                            requires=meta["requires"], input=meta.get("input"),
                            marks_sent=meta["marks_sent"], uses_input=_uses(meta["header"] + body, "input"))
            except PromptError as e:
                item["error"] = str(e)
            out.append(item)
    out.sort(key=lambda x: x["name"])
    return out


# -- the template language ----------------------------------------------------
def _standalone(text):
    """Drop the line around a tag that is alone on its line (keep the tag)."""
    out = []
    for line in text.splitlines(True):
        out.append(line.strip() if STANDALONE.match(line) else line)
    return "".join(out)


def _tree(text, name="?"):
    """-> nested list of str | ("var", name) | ("sec", name, inverted, children)."""
    text = _standalone(text)
    root, stack, pos = [], [], 0
    cur = root
    for m in TAG.finditer(text):
        if m.start() > pos:
            cur.append(text[pos:m.start()])
        pos = m.end()
        if m.group(0) == "\\{{":
            cur.append("{{")
            continue
        kind, var = m.group(1), m.group(2)
        if var not in VARS:
            raise PromptError("template %s: unknown {{%s}} (known: %s)" % (name, var, ", ".join(VARS)))
        if kind in ("#", "^"):
            if any(s[0] == var for s in stack):
                raise PromptError("template %s: {{%s%s}} nested inside itself" % (name, kind, var))
            node = ("sec", var, kind == "^", [])
            cur.append(node)
            stack.append((var, cur))
            cur = node[3]
        elif kind == "/":
            if not stack or stack[-1][0] != var:
                raise PromptError("template %s: {{/%s}} does not close the open section%s" % (
                    name, var, (" {{#%s}}" % stack[-1][0]) if stack else ""))
            cur = stack.pop()[1]
        else:
            cur.append(("var", var))
    if stack:
        raise PromptError("template %s: {{#%s}} is never closed" % (name, stack[-1][0]))
    if pos < len(text):
        cur.append(text[pos:])
    return root


def _uses(text, var):
    try:
        return any(_walk(_tree(text), lambda n: n[1] == var))
    except PromptError:
        return False


def _walk(nodes, pred):
    for n in nodes:
        if isinstance(n, tuple):
            if pred(n):
                yield n
            if n[0] == "sec":
                yield from _walk(n[3], pred)


def _empty(v):
    return not (v or "").strip()


def _render(nodes, values, emitted):
    """Render; `emitted` collects ("text"|var, str) in output order (for anchors)."""
    out = []
    for n in nodes:
        if isinstance(n, str):
            out.append(n)
            emitted.append(("text", n))
        elif n[0] == "var":
            v = values.get(n[1]) or ""
            out.append(v)
            emitted.append((n[1], v))
        elif _empty(values.get(n[1])) == n[2]:  # section kept
            out.append(_render(n[3], values, emitted))
    return "".join(out)


def header_problems(h):
    """Characters of `h` that must never be typed as the human, as readable names."""
    bad = []
    for c in h:
        cat = unicodedata.category(c)
        if c in HEADER_ASCII_BAD or cat == "Nd" or cat in INVISIBLE:
            name = repr(c) if c.isprintable() else "U+%04X" % ord(c)
            if name not in bad:
                bad.append(name)
    return bad


def check_header(header):
    """The header as it will be typed: one line, `[aic] ` first, nothing a
    terminal UI could take as a command, nothing invisible. Raises PromptError."""
    h = " ".join((header or "").split())
    if not h:
        raise PromptError("the header is empty: it is your typed request to the worker")
    if not h.startswith("[aic]"):
        h = PREFIX + h
    bad = header_problems(h)
    if bad:
        raise PromptError("the header can't contain %s (it is typed into the worker as you: digits could pick "
                          "dialog options, @ opens the file picker, invisible characters type words nobody "
                          "saw); move that into the body" % " ".join(bad))
    if len(h) > MAX_HEADER:
        raise PromptError("the header is %d characters; keep it under %d (details go in the body)" % (len(h), MAX_HEADER))
    return h


def check_body(body):
    """Hidden text (Unicode tags) or bidi overrides in the pasted body would
    show the human something other than what the worker reads."""
    bad = sorted({"U+%04X" % ord(c) for c in BODY_BAD.findall(body or "")})
    if bad:
        raise PromptError("the body contains invisible or direction-changing characters (%s): remove them"
                          % " ".join(bad))


# -- rendering ----------------------------------------------------------------
def _diagnostics_text(sdir, doc):
    d = paths.read_json(docs.diag_path(sdir, doc), {}) or {}
    lines = []
    for it in d.get("items") or []:
        where = "doc" if it.get("file") in ("doc", "spec") else it.get("file")
        lines.append("- [%s] %s:%s %s" % (it.get("kind"), where, it.get("line"), it.get("message")))
    return "\n".join(lines)


def _hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def render(root, sid, prompt, doc=docs.DEFAULT, line_from=None, line_to=None, input_text=None,
           builtin_only=False):
    """-> {"header", "body", "receipt"}. Raises PromptError (or DocError)."""
    from . import worker  # (worker imports this module)
    t = find(root, prompt, builtin_only)
    meta = t["meta"]
    sdir = paths.session_dir(root, sid)
    raw = docs.read(root, doc)
    text = docs.strip_marker(raw)
    previous = paths.read_text(docs.sent_path(sdir, doc))
    st = paths.read_json(os.path.join(sdir, "state.json"), {}) or {}
    selection, sel_lines = "", ""
    if line_from:
        lines = raw.splitlines()
        line_to = line_to or line_from
        first = line_from - 1
        if first == 0 and lines and docs.MARKER.match(lines[0]):
            first = 1  # the marker line is the doc's settings, not content
        selection = "\n".join(lines[first:line_to])
        sel_lines = "%d-%d" % (line_from, line_to)
    diff = ""
    if previous.strip() and previous != text:
        diff = "".join(difflib.unified_diff(previous.splitlines(True), text.splitlines(True),
                                            "spec (last submit)", "spec (now)"))
    edits = worker._human_edits_since(sdir, st.get("submit_seq", 0))
    values = {
        "doc": text.strip(), "doc.name": doc, "selection": selection, "selection.lines": sel_lines,
        "content": selection if line_from else text.strip(), "previous": previous,
        "changed": "yes" if text != previous else "", "diff": diff, "anchors": "",
        "hand_edits": ", ".join(edits), "diagnostics": _diagnostics_text(sdir, doc),
        "input": input_text or "",
    }
    if meta.get("input") is not None or _uses(meta["header"] + t["body"], "input"):
        if _empty(input_text):
            raise PromptError("template %s asks for input (%s)" % (prompt, meta.get("input") or "Message"))
    for r in meta["requires"]:
        if _empty(values.get(r)):
            why = {"changed": "the doc is unchanged since it was last sent",
                   "selection": "select lines in the doc first", "diff": "nothing changed since the last send"}
            raise PromptError("%s: nothing to send: {{%s}} is empty%s" % (
                prompt, r, (" (%s)" % why[r]) if r in why else ""))

    body_tree = _tree(t["body"], prompt)
    emitted = []
    # first pass: what the anchors come from (with `anchors` non-empty, so
    # {{#anchors}}/{{^anchors}} sections take the branch the final body takes
    # whenever there are anchors to show)
    _render(body_tree, dict(values, anchors="\0"), emitted)
    src = []
    for kind, v in emitted:
        if kind in ("text", "input", "selection") or (kind == "content" and line_from):
            src.append(v)
        elif kind == "diff":
            src.append("\n".join(l[1:] for l in v.splitlines() if l.startswith("+") and not l.startswith("+++")))
        elif kind in ("doc", "content") and not ((diff and any(k == "diff" for k, _ in emitted))):
            src.append(v)
    ref = anchors.blocks("\n".join(src), root, sid, for_worker=True)
    values["anchors"] = ref
    final = []
    body = _render(body_tree, values, final).strip("\n")
    placed = {k for k, _ in final}  # what the body really emitted, not just mentions
    if ref and "anchors" not in placed:
        body += ANCHOR_INTRO + ref
    if edits and "hand_edits" not in placed:
        body += EDITS_INTRO + values["hand_edits"]
    header = check_header(_render(_tree(meta["header"], prompt), values, []))
    check_body(body)
    tr = paths.read_jsonl(os.path.join(sdir, "trace.jsonl"))
    receipt = {"prompt": prompt, "source": t["source"], "doc": doc, "marks_sent": meta["marks_sent"],
               "sent": text if meta["marks_sent"] else None, "previous_hash": _hash(previous),
               # a diff/previous in the body goes stale like a marks_sent send does
               "uses_previous": bool(placed & {"diff", "previous", "changed"}),
               "edits_sent": bool(edits), "seq": tr[-1].get("seq", 0) if tr else 0}
    return {"header": header, "body": body, "receipt": receipt}


def check_receipt(root, sid, receipt):
    """Before sending a rendered prompt: a well-formed receipt (it comes from a
    file), and the doc not sent as work since it was rendered (its diff would
    be stale). Raises PromptError (DocError for a bad doc name)."""
    doc = docs.check_name(receipt.get("doc"))
    sdir = paths.session_dir(root, sid)
    tr = paths.read_jsonl(os.path.join(sdir, "trace.jsonl"))
    last = tr[-1].get("seq", 0) if tr else 0
    seq = receipt.get("seq")
    if not (isinstance(seq, int) and not isinstance(seq, bool) and 0 <= seq <= last):
        raise PromptError("bad receipt: seq %r" % (seq,))
    if not isinstance(receipt.get("marks_sent"), bool) or not isinstance(receipt.get("prompt"), str) \
            or not isinstance(receipt.get("sent"), (str, type(None))) \
            or not isinstance(receipt.get("edits_sent", False), bool):
        raise PromptError("bad receipt: render the prompt again")
    if receipt["marks_sent"] or receipt.get("uses_previous"):
        cur = paths.read_text(docs.sent_path(sdir, doc))
        if _hash(cur) != receipt.get("previous_hash"):
            raise PromptError("%s was sent to the worker after this prompt was rendered; render it again" % doc)
