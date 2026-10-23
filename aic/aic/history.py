"""`aic history`: what the human asked in this project, for finding requests
worth a prompt template (the aic-prompts skill reads it).

Prompts typed to every worker session on disk (compiler sessions excluded),
newest session first, and the questions asked of the compiler (chat.md).
"""
import glob
import os
import re

from . import paths
from .transcript import Parser

QUESTION = re.compile(r"^## You([^\n]*)\n(.*?)\n\n## Compiler\n", re.M | re.S)
# not the human's words: harness messages (<task-notification>…) and slash commands
NOT_ASKED = re.compile(r"<[a-z-]+>|/[a-z]")


def prompts(root):
    out = []
    for s in paths.transcript_sessions(root):
        p = Parser(root)
        for rec in paths.read_jsonl(s["transcript_path"]):
            try:
                evs = p.feed(rec)
            except Exception:  # one odd record must not hide the rest
                continue
            out += [{"kind": "prompt", "session": s["session_id"], "ts": ev.get("ts"), "text": ev["text"]}
                    for ev in evs if ev["kind"] == "prompt" and not ev.get("system") and not ev.get("resent")
                    and not NOT_ASKED.match(ev["text"])]
    return out


def questions(root):
    out = []
    for f in sorted(glob.glob(os.path.join(paths.aic_dir(root), "sessions", "*", "chat.md"))):
        sid = os.path.basename(os.path.dirname(f))
        out += [{"kind": "whatif" if m.group(1).lstrip().startswith("(what-if)") else "question",
                 "session": sid, "how": m.group(1).strip(), "text": m.group(2).strip()}
                for m in QUESTION.finditer(paths.read_text(f))]
    return out


def text(items, width=300):
    """One line per item: kind, session, the start of what was asked."""
    lines = []
    for it in items:
        s = " ".join(it["text"].split())
        lines.append("%-8s %s  %s" % (it["kind"], it["session"][:8], s if len(s) <= width else s[:width - 1] + "…"))
    return "\n".join(lines) or "(nothing asked yet)"
