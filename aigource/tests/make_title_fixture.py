#!/usr/bin/env python3
"""tests/fixtures/titles/t1.jsonl: one session whose prompts make titles the app and
draft/build_data.py must cut alike (first_line in util.c, prompt_line in transcript.c),
each followed by a Write of its own file (src/tN.py) so it is a turn. Paths under
/REPO (tests/title_parity.py puts the repo there). Bytes that aren't UTF-8 are written
raw, as a transcript could hold them; compact JSON, as Claude Code writes it (build_data.py
picks records by their compact text).

  python3 tests/make_title_fixture.py
"""
import json, os

PROMPTS = [  # each a JSON string's source text (escapes kept), or bytes written raw inside the quotes
    " hello",
    "hello　",                 # trailing ideographic space: kept
    "\u000bfoo",                   # vertical tab: not skipped
    "\u000cfoo",                   # form feed: not skipped
    "\u001ftitle",                 # unit separator, the key's own separator byte
    "foo\rbar",                    # a CR ends the line
    "  \n \nreal",                 # leading spaces and newlines skipped
    " \nreal",                # a no-break space line is a line
    "abc\u0000def",                # the string ends at a NUL
    b"bad \xe2\x9c tail",          # a cut sequence: two U+FFFD
    b"\xff\xfe lead",              # bytes that never start one
    "\ud800 lone",                 # a lone surrogate escape
    "café " + "é" * 150,  # cut at 140 characters, not bytes
    "\t\ttabs\t",
]
here = os.path.dirname(os.path.abspath(__file__))
out = os.path.join(here, "fixtures", "titles", "t1.jsonl")
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "wb") as f:
    for i, p in enumerate(PROMPTS):
        m, s = divmod(i * 20, 60)
        ts = lambda k: "2026-10-14T11:%02d:%02d.%03dZ" % (m, s + k, 100 + i)
        text = b'"' + p + b'"' if isinstance(p, bytes) else json.dumps(p).encode()
        f.write(b'{"type":"user","timestamp":"' + ts(0).encode() + b'","cwd":"/REPO","message":{"role":"user",'
                b'"content":' + text + b'},"origin":{"kind":"human"}}\n')
        path, body = "/REPO/src/t%d.py" % i, "x = %d\n" % i
        use = {"type": "assistant", "timestamp": ts(5), "cwd": "/REPO", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "w%d" % i, "name": "Write", "input": {"file_path": path, "content": body}}],
            "id": "m%d" % i, "usage": {"output_tokens": 10, "input_tokens": 1}}}
        res = {"type": "user", "timestamp": ts(6), "cwd": "/REPO", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "w%d" % i, "is_error": False, "content": "ok"}]},
            "toolUseResult": {"type": "create", "filePath": path, "content": body, "structuredPatch": []}}
        f.write(json.dumps(use, separators=(",", ":")).encode() + b"\n" + json.dumps(res, separators=(",", ":")).encode() + b"\n")
print("wrote", out)
