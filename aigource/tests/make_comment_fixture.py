#!/usr/bin/env python3
"""tests/fixtures/comments: which comment a change gets (comment_of in analysis.c, notes in
draft/build_data.py), under /REPO (tests/comment_parity.py and tests/serve.py put the repo there).

c1.jsonl, the main session: one prompt, then
- one message whose text comes before its first of three Writes (src/a.py, b.py, c.py), each
  call recorded on its own line with its result before the next, as Claude Code writes them:
  all three share the text;
- src/d.py after a text that opens with code fences and markdown marks (serve's `said`: the
  first readable line, its marks dropped), src/e.py after 301 bytes of text (cut at 200 bytes
  between characters), src/f.py after `-1` and `**` in and out of a code span (a minus that
  marks no list kept, `**` dropped outside code only), src/g.py after a ``` block holding diff
  lines, then `#include` (no heading), src/h.py after a ~~~ block and a rule, then a heading.
c1/subagents/agent-r1.jsonl, a subagent in two rounds: src/r1.py with its own text, a final
report, then the coordinator's message resuming it (isMeta, origin coordinator: SendMessage),
src/r2.py with no text since (no comment: never the last round's report), src/r3.py after text.

  python3 tests/make_comment_fixture.py
"""
import json, os

here = os.path.dirname(os.path.abspath(__file__))
out = os.path.join(here, "fixtures", "comments")
os.makedirs(os.path.join(out, "c1", "subagents"), exist_ok=True)


def line(r):
    return json.dumps(r, separators=(",", ":")) + "\n"


def ts(m, s):
    return "2026-10-14T11:%02d:%02d.000Z" % (m, s)


def user(t, text, **kw):
    return line(dict({"type": "user", "timestamp": t, "cwd": "/REPO", "message": {"role": "user", "content": text}}, **kw))


def said(t, mid, text):
    return line({"type": "assistant", "timestamp": t, "cwd": "/REPO", "message": {
        "role": "assistant", "id": mid, "content": [{"type": "text", "text": text}]}})


def write(t, mid, tid, name):
    """A Write of src/<name> on its own line, then its result."""
    path, body = "/REPO/src/" + name, "x = %r\n" % name
    use = {"type": "assistant", "timestamp": t, "cwd": "/REPO", "message": {"role": "assistant", "id": mid, "content": [
        {"type": "tool_use", "id": tid, "name": "Write", "input": {"file_path": path, "content": body}}]}}
    res = {"type": "user", "timestamp": t[:-4] + "500Z", "cwd": "/REPO", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "is_error": False, "content": "ok"}]},
        "toolUseResult": {"type": "create", "filePath": path, "content": body, "structuredPatch": []}}
    return line(use) + line(res)


main = [
    line({"type": "ai-title", "aiTitle": "Comments", "sessionId": "c1"}),
    user(ts(0, 0), "Write the files", origin={"kind": "human"}),
    said(ts(0, 5), "m1", "Writing all three files."),
    write(ts(0, 6), "m1", "w1", "a.py"),
    write(ts(0, 7), "m1", "w2", "b.py"),
    write(ts(0, 8), "m1", "w3", "c.py"),
    said(ts(4, 0), "m4", "```lua\n```\n> - **Fixing** the cut\nmore"),
    write(ts(4, 1), "m4", "w4", "d.py"),
    said(ts(4, 10), "m5", "a" + "é" * 150),
    write(ts(4, 11), "m5", "w5", "e.py"),
    said(ts(4, 20), "m6", "-1 means **unknown**, `**kw` stays"),
    write(ts(4, 21), "m6", "w6", "f.py"),
    said(ts(4, 30), "m7", "```diff\n- old line\n+ new line\n```\n#include <x.h> is needed"),
    write(ts(4, 31), "m7", "w7", "g.py"),
    said(ts(4, 40), "m8", "~~~\ncode\n~~~\n* * *\n## Heading here"),
    write(ts(4, 41), "m8", "w8", "h.py"),
]
sub = [
    user(ts(2, 0), "Round one"),
    said(ts(2, 5), "s1", "Round one edit."),
    write(ts(2, 6), "s1", "r1", "r1.py"),
    said(ts(2, 10), "s2", "Final report of round one."),
    user(ts(3, 0), "The coordinator sent a message while you were working:\nround two", isMeta=True,
         origin={"kind": "coordinator"}),
    write(ts(3, 5), "s3", "r2", "r2.py"),
    said(ts(3, 10), "s4", "Round two, with words."),
    write(ts(3, 11), "s4", "r3", "r3.py"),
]
with open(os.path.join(out, "c1.jsonl"), "w") as f:
    f.write("".join(main))
with open(os.path.join(out, "c1", "subagents", "agent-r1.jsonl"), "w") as f:
    f.write("".join(sub))
with open(os.path.join(out, "c1", "subagents", "agent-r1.meta.json"), "w") as f:
    json.dump({"agentType": "oh-my-claudecode:executor", "description": "Two rounds"}, f)
print("wrote", out)
