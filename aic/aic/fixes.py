"""`aic fix`: the compiler proposes edits to a doc that resolve its diagnostics.

Opt-in, and still read-only: the compiler's edits are data (replace `old`
with `new`). The editor shows them, the human picks which ones to apply, and
the editor applies them to its own buffer. Proposals are kept per session in
fixes.<doc>.json (the latest only).
"""
import hashlib
import json
import os
import re
import unicodedata

from . import compiler, docs, paths

FIX_MODEL = os.environ.get("AIC_FIX_MODEL", compiler.COMPILE_MODEL)

FIX_TASK = """The human asked you to FIX doc `%s`: propose edits to that doc that resolve the diagnostic items below.
You still never edit files: your edits are data. aic shows them to the human, who picks which ones to apply.
Rules:
- Edit only this doc: never code, never other docs, never its first line when that is an `<!-- aic: ... -->` marker.
- Fix an item only where changing the doc is the right resolution: the doc is ambiguous, contradicts itself, has a
  hole, assumes something false about the code, or omits something the worker did that the human should decide on.
  When the worker is the one that is wrong and the doc is right, do not change the doc: list the item under skipped.
- One fix per item; a fix that also resolves related items names the main one. item = the item's number; null for a
  fix no item names (only when it is needed for the others to make sense).
- Each edit replaces `old` with `new`. Copy `old` verbatim from the doc text WITHOUT the "  12| " line-number prefix,
  long enough to occur exactly once in the doc; prefer whole lines. To insert, put the neighbouring line in `old` and
  repeat it in `new`. The edits of one fix never overlap; different fixes should not touch the same lines.
- Keep the human's voice and form (outline, pseudocode, box diagrams); make the smallest edit that resolves the item.
  Verify what you write about the code with Read/Grep/Glob first.
- title: imperative, under 70 characters. why: one sentence."""

FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "fixes": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "item": {"type": ["integer", "null"]},
                "title": {"type": "string"},
                "why": {"type": "string"},
                "edits": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"old": {"type": "string"}, "new": {"type": "string"}},
                    "required": ["old", "new"],
                }},
            },
            "required": ["title", "edits"],
        }},
        "skipped": {"type": "array", "items": {
            "type": "object",
            "properties": {"item": {"type": "integer"}, "reason": {"type": "string"}},
            "required": ["item", "reason"],
        }},
    },
    "required": ["fixes"],
}

NUMBERED = re.compile(r"^ *\d+\| ?")
# shown as nothing (or nothing distinct) in a diff, yet changes the doc
INVISIBLE = {"Cc", "Cf", "Cs", "Zl", "Zp", "Zs"}  # (Zs: every space but " ")
INVISIBLE_EXTRA = {"\u034f", "\u115f", "\u1160", "\u3164", "\uffa0"}


def fixes_path(sdir, name):
    return os.path.join(sdir, "fixes.%s.json" % docs.check_name(name))


def doc_line(it):
    """The doc line an item is about, or None."""
    on_doc = it.get("file") in ("doc", "spec")
    n = it.get("line") if on_doc else None
    n = n if isinstance(n, int) else it.get("spec_line")
    return n if isinstance(n, int) else None


def _count(text, s):
    """Occurrences of s in text, overlapping ones too (as an editor finds them)."""
    n, i = 0, text.find(s)
    while i >= 0:
        n += 1
        i = text.find(s, i + 1)
    return n


def _invisible(s):
    return sorted({"U+%04X" % ord(c) for c in s if c not in "\t\n " and (
        unicodedata.category(c) in INVISIBLE or c in INVISIBLE_EXTRA)})


def _unnumber(s):
    lines = s.split("\n")
    if not all(NUMBERED.match(l) for l in lines if l):
        return None
    return "\n".join(NUMBERED.sub("", l, count=1) for l in lines)


def _locate(text, old, new):
    """-> (old, new, start) with old occurring exactly once in text, or an error string.
    Old copied with its "  12| " prefixes is taken without them (from new too)."""
    if not old:
        return "empty `old`"
    n = _count(text, old)
    if n == 0:
        bare = _unnumber(old)
        if bare and _count(text, bare) == 1:
            if _unnumber(new) is None:
                return "its new text has line-number prefixes on some lines only"
            old, new, n = bare, _unnumber(new), 1
    if n == 0:
        return "its text is not in the doc"
    if n > 1:
        return "its text occurs %d times in the doc" % n
    if old == new:
        return "it changes nothing"
    if [l.rstrip() for l in old.split("\n")] == [l.rstrip() for l in new.split("\n")]:
        return "it changes only trailing spaces"
    return old, new, text.index(old)


def _apply(text, edits):
    for e in sorted(edits, key=lambda e: -e["start"]):
        text = text[:e["start"]] + e["new"] + text[e["end"]:]
    return text


def _oneline(s):
    return " ".join(str(s or "").encode("utf-8", "replace").decode("utf-8").split())


def check(text, proposed):
    """Validate the compiler's fixes against `text`. -> (fixes, rejected):
    fixes carry edits with their lines; each fix lists the fixes it overlaps."""
    first = text.split("\n", 1)[0]
    protect = len(first) if docs.MARKER.match(first) else -1
    fixes, rejected = [], []
    for f in proposed or []:
        if not isinstance(f, dict):
            continue
        title = _oneline(f.get("title"))[:120] or "(untitled fix)"
        edits, err = [], None
        for e in f.get("edits") or []:
            if not isinstance(e, dict) or not isinstance(e.get("old"), str) or not isinstance(e.get("new"), str):
                err = "malformed edit"
                break
            r = _locate(text, e["old"], e["new"])
            if isinstance(r, str):
                err = r
                break
            old, new, start = r
            if start <= protect:
                err = "it changes the aic marker line"
                break
            bad = _invisible(new)
            if bad:
                err = "its new text has invisible characters (%s)" % " ".join(bad)
                break
            line = text.count("\n", 0, start) + 1
            edits.append({"old": old, "new": new, "start": start, "end": start + len(old),
                          "line": line, "end_line": line + old.rstrip("\n").count("\n")})
        if not err and not edits:
            err = "no edits"
        edits.sort(key=lambda e: e["start"])
        if not err and any(a["end"] > b["start"] for a, b in zip(edits, edits[1:])):
            err = "its edits overlap"
        if not err and protect < 0 and docs.MARKER.match(_apply(text, edits).split("\n", 1)[0]):
            err = "it would add an aic marker line"
        if err:
            rejected.append({"title": title, "item": f.get("item"), "reason": err})
            continue
        fixes.append({"id": len(fixes) + 1, "item": f.get("item") if isinstance(f.get("item"), int) else None,
                      "title": title, "why": _oneline(f.get("why")), "edits": edits})
    for a in fixes:
        a["conflicts"] = [b["id"] for b in fixes if b is not a and any(
            x["start"] < y["end"] and y["start"] < x["end"] for x in a["edits"] for y in b["edits"])]
    for a in fixes:
        for e in a["edits"]:
            del e["start"], e["end"]  # code-point offsets: editors locate `old` themselves
    return fixes, rejected


def propose(root, sid, doc, text=None, line_from=None, line_to=None):
    """Ask the compiler for fixes to doc `doc` (its saved text, or `text`: the
    editor's buffer). Range: only the items about those doc lines."""
    sdir = paths.session_dir(root, sid)
    if text is None:
        text = docs.read(root, doc)
    text = text.replace("\r\n", "\n")
    diag = paths.read_json(docs.diag_path(sdir, doc), {}) or {}
    picked = []
    for i, it in enumerate(diag.get("items") or [], 1):
        if not isinstance(it, dict):
            continue
        n = doc_line(it)
        if line_from is not None and not (n is not None and line_from <= n <= (line_to or line_from)):
            continue
        picked.append((i, it))
    if not picked:
        raise compiler.CompilerError("no diagnostics to fix in %s%s (compile it first)" % (
            doc, " lines %s-%s" % (line_from, line_to or line_from) if line_from is not None else ""))
    listing = "\n".join("#%d [%s/%s] %s:%s %s" % (
        i, it.get("kind"), it.get("severity"), "doc" if it.get("file") in ("doc", "spec") else it.get("file"),
        it.get("line"), it.get("message")) + ("" if it.get("file") in ("doc", "spec") or not it.get("spec_line")
                                             else " (doc line %s)" % it["spec_line"]) for i, it in picked)
    parts = [FIX_TASK % doc,
             "== DOC `%s` (.aic/docs/%s.md as the human sees it now, with line numbers) ==\n%s" % (
                 doc, doc, compiler.numbered(text)),
             "== ITEMS TO FIX (from your last compile of this doc; lines may have moved since) ==\n" + listing]
    out = compiler.run_claude(root, sdir, "compile", FIX_MODEL, "\n\n".join(parts), schema=FIX_SCHEMA,
                              note="fix " + doc)
    res = out.get("structured_output")
    if not isinstance(res, dict):
        try:
            res = json.loads(out.get("result") or "")
        except ValueError:
            res = None
    if not isinstance(res, dict):
        raise compiler.CompilerError("compiler returned no fixes")
    fixes, rejected = check(text, res.get("fixes"))
    by_index = dict(picked)
    for f in fixes:
        it = by_index.get(f["item"])
        if it:
            f.update({"kind": it.get("kind"), "severity": it.get("severity"), "message": _oneline(it.get("message"))})
    skipped = [{"item": s.get("item"), "reason": _oneline(s.get("reason")),
                "message": _oneline((by_index.get(s.get("item")) or {}).get("message"))}
               for s in res.get("skipped") or [] if isinstance(s, dict)]
    named = {f["item"] for f in fixes} | {s["item"] for s in skipped} | {r["item"] for r in rejected}
    skipped += [{"item": i, "reason": "no doc change proposed", "message": _oneline(it.get("message"))}
                for i, it in picked if i not in named]
    prop = {"doc": doc, "created_at": paths.now(), "compiled_at": diag.get("compiled_at"),
           "base_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
           "fixes": fixes, "rejected": rejected, "skipped": skipped, "cost_usd": out.get("total_cost_usd")}
    paths.write_json(fixes_path(sdir, doc), prop)
    return prop


def describe(res):
    """Plain-text listing of a proposal (the CLI without --json)."""
    lines = []
    for f in res["fixes"]:
        lines.append("%d. %s%s" % (f["id"], f["title"], " (overlaps %s)" % ", ".join(map(str, f["conflicts"]))
                                   if f["conflicts"] else ""))
        for e in f["edits"]:
            lines.append("   @@ line %d" % e["line"])
            lines += ["   - " + l for l in e["old"].split("\n")] + ["   + " + l for l in e["new"].split("\n")]
    for r in res["rejected"]:
        lines.append("rejected: %s (%s)" % (r["title"], r["reason"]))
    for s in res["skipped"]:
        lines.append("skipped #%s: %s" % (s["item"], s["reason"]))
    return "\n".join(lines) or "no fixes proposed"
