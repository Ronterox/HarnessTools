"""Golden submits: the exact header/body the worker received before prompt
templates existed. The built-in templates must keep reproducing them.

Regenerate (only when the change is intended):
    AIC_REGEN_GOLDEN=1 python3 -m unittest discover -s aic/tests -p 'test_submit_golden.py'
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aic import docs, paths, worker  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "submit_golden.json")

APP = "def load(path):\n    return open(path).read()\n\n\ndef save(path, text):\n    open(path, 'w').write(text)\n"
SPEC_V1 = "# Spec\n\n- load reads @app.py:1-2\n- save lives in @app.py\n"
SPEC_V2 = "# Spec\n\n- load reads @app.py:1-2\n- save lives in @app.py\n- save is atomic, see @app.py:save\n"
SPEC_TURN = "# Spec\n\n- redo what @turn:1 did, but atomic\n"
PROMPT = {"id": "e1", "seq": 1, "ts": 0, "kind": "prompt", "turn": 1, "text": "go"}
TOOL = {"id": "e2", "seq": 2, "ts": 0, "kind": "tool", "turn": 1, "tool": "Edit", "title": "Edit app.py:5-6",
        "why": "add save", "file": "app.py", "input": "app.py", "error": False,
        "hunks": [{"start": 5, "count": 2, "lines": ["+def save(path, text):", "+    open(path, 'w').write(text)"]}]}
HUMAN = {"id": "e3", "seq": 3, "ts": 0, "kind": "human", "turn": 1, "file": "app.py",
         "hunks": [{"start": 5, "count": 2, "lines": ["+def save(path, text):", "+    pass"]}]}
HUMAN_LATER = {"id": "e4", "seq": 4, "ts": 0, "kind": "human", "turn": 1, "file": "app.py",
               "hunks": [{"start": 1, "count": 1, "lines": ["-def load(p):", "+def load(path):"]}]}

# name -> spec.md, spec.submitted.md (None: never submitted), range, trace events,
# state.json submit_seq (hand edits up to it were already sent)
CASES = {
    "first": dict(spec=SPEC_V1),
    "first_hand_edits": dict(spec=SPEC_V1, events=[HUMAN]),
    "diff": dict(spec=SPEC_V2, sent=SPEC_V1),
    "diff_hand_edits": dict(spec=SPEC_V2, sent=SPEC_V1, events=[HUMAN]),
    "diff_after_submit": dict(spec=SPEC_V2, sent=SPEC_V1, events=[HUMAN, HUMAN_LATER], submit_seq=3),
    "range": dict(spec=SPEC_V2, sent=SPEC_V1, rng=(3, 4)),
    "range_hand_edits": dict(spec=SPEC_V2, rng=(5, 5), events=[HUMAN]),
    "turn_anchor": dict(spec=SPEC_TURN, events=[TOOL]),
}


def build(name):
    c = CASES[name]
    with tempfile.TemporaryDirectory() as root:
        worker.init(root, "/x/bin/aic")
        paths.write_text(os.path.join(root, "app.py"), APP)
        sdir = paths.session_dir(root, "S1")
        os.makedirs(sdir)
        os.makedirs(docs.docs_dir(root))
        paths.write_text(docs.path(root, "spec"), c["spec"])
        if c.get("sent") is not None:
            paths.write_text(docs.sent_path(sdir, "spec"), c["sent"])
        if c.get("submit_seq"):
            paths.write_json(os.path.join(sdir, "state.json"), {"submit_seq": c["submit_seq"]})
        events = [PROMPT] + c.get("events", [])
        paths.write_text(os.path.join(sdir, "trace.jsonl"), "".join(json.dumps(e) + "\n" for e in events))
        m = worker.build_submit(root, "S1", *(c.get("rng") or (None, None)))
        return {"header": m["header"], "body": m["body"], "spec": m["spec"], "seq": m["seq"]}


class SubmitGoldenTest(unittest.TestCase):
    def test_golden(self):
        got = {name: build(name) for name in CASES}
        if os.environ.get("AIC_REGEN_GOLDEN"):
            paths.write_text(FIXTURE, json.dumps(got, indent=1, ensure_ascii=False) + "\n")
        want = paths.read_json(FIXTURE)
        for name in CASES:
            self.assertEqual(got[name], want[name], name)


if __name__ == "__main__":
    unittest.main()
