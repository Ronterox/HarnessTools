"""Golden compiler prompts: exactly what compile, ask, what-if and fix sent the
compiler before compiler templates existed. The built-in compiler templates
must keep reproducing them.

Regenerate (only when the change is intended):
    AIC_REGEN_GOLDEN=1 python3 -m unittest discover -s aic/tests -p 'test_compiler_golden.py'
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aic import compiler, docs, fixes, paths, worker  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compiler_golden.json")

APP = "def load(path):\n    return open(path).read()\n\n\ndef save(path, text):\n    open(path, 'w').write(text)\n"
SPEC = "<!-- aic: compile -->\n# Spec\n\n- load reads @app.py:1-2\n- save is atomic\n"
PROMPT = {"id": "e1", "seq": 1, "ts": 0, "kind": "prompt", "turn": 1, "text": "go"}
TOOL = {"id": "e2", "seq": 2, "ts": 0, "kind": "tool", "turn": 1, "tool": "Edit", "title": "Edit app.py:5-6",
        "why": "add save", "file": "app.py", "input": "app.py", "error": False,
        "hunks": [{"start": 5, "count": 2, "lines": ["+def save(path, text):", "+    open(path, 'w').write(text)"]}]}
TEXT = {"id": "e3", "seq": 3, "ts": 0, "kind": "text", "turn": 1, "text": "Done."}
ITEMS = [{"kind": "incoherent", "severity": "warning", "file": "doc", "line": 5, "message": "atomic how?"},
         {"kind": "model", "severity": "error", "file": "app.py", "line": 5, "spec_line": 4, "message": "no tmp file"}]
CTX = {"where": "trace", "event": TOOL}


def scenarios(root, sdir, out):
    """name -> callable running one compiler call (out: a dir outside the project)."""
    return [
        ("compile_first", lambda: compiler.compile_spec(root, "S1", "spec")),
        ("compile_again", lambda: compiler.compile_spec(root, "S1", "spec")),  # previous items, new activity
        ("ask_no_doc", lambda: compiler.ask(root, "S1", "what is missing?", doc="nope")),
        ("ask", lambda: compiler.ask(root, "S1", "why @app.py:save?", ctx=CTX, doc="spec")),
        ("ask_code_ctx", lambda: compiler.ask(root, "S1", "and this?", doc="spec",
                                              ctx={"where": "code", "file": "app.py", "line": 2,
                                                   "excerpt": "see @app.py:1"})),
        ("whatif", lambda: compiler.ask(root, "S1", "write to a tmp file first", ctx=CTX, whatif=True, doc="spec")),
        ("fix", lambda: fixes.propose(root, "S1", "spec")),
        ("fix_range", lambda: fixes.propose(root, "S1", "spec", line_from=4, line_to=4)),
        ("compile_plain", lambda: compiler.compile_spec(root, "S1", "plain")),  # no anchors
        ("whatif_no_ctx", lambda: compiler.ask(root, "S1", "skip it", whatif=True, doc="plain")),
        # outside anchors: inlined and --add-dir only from the human's own text (doc, question), not the excerpt
        ("compile_outside", lambda: compiler.compile_spec(root, "S1", "outside")),
        ("ask_outside", lambda: compiler.ask(root, "S1", "compare with @%s/guide.md and @%s/" % (out, out),
                                             doc="outside", ctx={"where": "code", "file": "app.py", "line": 1,
                                                                 "excerpt": "see @%s/guide.md" % out})),
    ]


def run_all():
    got = {}
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as out:
        root, out = os.path.realpath(root), os.path.realpath(out)
        paths.write_text(os.path.join(out, "guide.md"), "OUTSIDE GUIDE\n")
        worker.init(root, "/x/bin/aic")
        paths.write_text(os.path.join(root, "app.py"), APP)
        sdir = paths.session_dir(root, "S1")
        os.makedirs(sdir)
        os.makedirs(docs.docs_dir(root))
        paths.write_text(docs.path(root, "spec"), SPEC)
        paths.write_text(docs.path(root, "plain"), "# Plain\n\n- no anchors here\n")
        paths.write_text(docs.path(root, "outside"), "- follow @%s/guide.md\n- may read @%s/\n" % (out, out))
        trace = os.path.join(sdir, "trace.jsonl")
        paths.write_text(trace, json.dumps(PROMPT) + "\n" + json.dumps(TOOL) + "\n")
        calls = []

        def fake(root_, sdir_, key, model, prompt, schema=None, timeout=600, add_dirs=(), note=None):
            calls.append({"key": key, "model": model, "note": note, "schema": bool(schema),
                          "add_dirs": [d.replace(root, "<ROOT>").replace(out, "<OUT>") for d in add_dirs],
                          "prompt": prompt.replace(root, "<ROOT>").replace(out, "<OUT>")})
            paths.update_json(os.path.join(sdir_, "state.json"), **{key: "C-" + key})
            if key == "compile" and schema is compiler.SCHEMA:
                return {"structured_output": {"summary": "s", "items": ITEMS}}
            if schema is not None:
                return {"structured_output": {"fixes": [], "skipped": []}}
            return {"result": "answer"}
        with mock.patch.object(compiler, "run_claude", fake):
            for name, fn in scenarios(root, sdir, out):
                if name == "compile_again":
                    paths.write_text(trace, paths.read_text(trace) + json.dumps(TEXT) + "\n")
                fn()
                got[name] = calls[-1]
    return got


class CompilerGoldenTest(unittest.TestCase):
    def test_golden(self):
        got = run_all()
        if os.environ.get("AIC_REGEN_GOLDEN"):
            paths.write_text(FIXTURE, json.dumps(got, indent=1, ensure_ascii=False) + "\n")
        want = paths.read_json(FIXTURE)
        self.assertEqual(sorted(got), sorted(want))
        for name in want:
            self.assertEqual(got[name], want[name], name)


if __name__ == "__main__":
    unittest.main()
