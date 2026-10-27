#!/usr/bin/env python3
"""aigource's structure fixture (aigource/tests/serve.py: fixture) as a repo
under TMP, for tests/blame_e2e.lua. Prints one JSON object:
{"repo", "cfg", "lines": {path: [[slot, prompt] or null per line]}}, the colour
slot and prompt of each line's turn as --structure-dump has them.

  blame_fixture.py TMP BINARY
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "..", "aigource", "tests"))
import serve  # noqa: E402

tmp, serve.BIN = sys.argv[1], sys.argv[2]
out, sys.stdout = sys.stdout, sys.stderr  # its checks print
repo, cfg, env, dump, want, keys = serve.fixture(tmp)
sys.stdout = out
lines = {}
for path, runs in want.items():
    per = []
    for n, cid in runs:
        t = dump["changes"][cid][0] if cid >= 0 else -1
        per += [[serve.slot_of(keys[t]), keys[t].split("\x1f", 1)[1]] if t >= 0 else None] * n
    lines[path] = per
print(json.dumps({"repo": repo, "cfg": cfg, "lines": lines}))
