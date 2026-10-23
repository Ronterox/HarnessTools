import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aic import cli, compiler, docs, fixes, paths, worker  # noqa: E402

DOC = "<!-- aic: compile -->\n# Spec\n- retry on error\n- log it\n- retry on error later\n"


def fix(*edits, title="t", item=1):
    return {"item": item, "title": title, "why": "w", "edits": [{"old": o, "new": n} for o, n in edits]}


class CheckTest(unittest.TestCase):
    def test_lines_and_conflicts(self):
        fs, rej = fixes.check(DOC, [
            fix(("- log it\n", "- log it at warn\n")),
            fix(("# Spec\n", "# Spec\n\nRetries: 3.\n"), ("- log it", "- log it once"), item=2),
            fix(("later", "after 1s"), item=3),
        ])
        self.assertEqual(rej, [])
        self.assertEqual([(e["line"], e["end_line"]) for e in fs[0]["edits"]], [(4, 4)])
        self.assertEqual([e["line"] for e in fs[1]["edits"]], [2, 4])
        self.assertEqual(fs[0]["conflicts"], [2])
        self.assertEqual(fs[1]["conflicts"], [1])
        self.assertEqual(fs[2]["conflicts"], [])
        self.assertNotIn("start", fs[0]["edits"][0])

    def test_rejections(self):
        fs, rej = fixes.check(DOC, [
            fix(("retry on error", "x")),  # twice in the doc
            fix(("nope", "x")),
            fix(("<!-- aic: compile -->", "<!-- aic: -->")),
            fix(("- log it", "- log it")),
            fix(("- log it", "- log ‮it")),
            fix(("# Spec\n- retry", "a"), ("- retry on error\n- log", "b")),
            {"title": "no edits", "edits": []},
        ])
        self.assertEqual(fs, [])
        reasons = [r["reason"] for r in rej]
        self.assertIn("occurs 2 times", reasons[0])
        self.assertIn("not in the doc", reasons[1])
        self.assertIn("marker", reasons[2])
        self.assertIn("changes nothing", reasons[3])
        self.assertIn("U+202E", reasons[4])
        self.assertIn("overlap", reasons[5])
        self.assertEqual(reasons[6], "no edits")

    def test_more_rejections(self):
        fs, rej = fixes.check("# t\n---\n---\n---\n- retry\n", [
            fix(("---\n---", "===")),  # overlapping occurrences: an editor finds 2
            fix(("- retry", "- re\u200btry")),
            fix(("- retry", "- retry\r\x1b[2J")),
            fix(("# t", "<!-- aic: prompt=x -->\n# t")),
            fix(("   5| - retry", "   5| - retry\n- once")),
            fix(("- retry", "- retry \ud800")),
            fix(("- retry", "- retry\u00a0now")),
            fix(("- retry", "- retry   ")),
        ])
        self.assertEqual(fs, [])
        reasons = [r["reason"] for r in rej]
        self.assertIn("occurs 2 times", reasons[0])
        self.assertIn("U+200B", reasons[1])
        self.assertIn("U+000D U+001B", reasons[2])
        self.assertIn("marker", reasons[3])
        self.assertIn("prefixes on some lines only", reasons[4])
        self.assertIn("U+D800", reasons[5])
        self.assertIn("U+00A0", reasons[6])
        self.assertIn("only trailing spaces", reasons[7])
        fs, rej = fixes.check("- a \ue0a0 \U0001fae9\n", [fix(("- a", "- b \ue0a0"))])
        self.assertEqual((len(fs), rej), (1, []))  # icon-font glyphs and new emoji are fine

    def test_numbered_old_is_unnumbered(self):
        fs, rej = fixes.check(DOC, [fix(("   4| - log it", "   4| - log it twice"))])
        self.assertEqual(rej, [])
        self.assertEqual((fs[0]["edits"][0]["old"], fs[0]["edits"][0]["new"]), ("- log it", "- log it twice"))

    def test_no_marker_first_line_is_editable(self):
        fs, rej = fixes.check("# Spec\n- a\n", [fix(("# Spec", "# Plan"))])
        self.assertEqual((len(fs), rej), (1, []))


class ProposeTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        worker.init(self.root, "/x/bin/aic")
        self.sdir = paths.session_dir(self.root, "S1")
        os.makedirs(self.sdir)
        paths.write_text(docs.path(self.root, "spec"), DOC)
        paths.write_json(docs.diag_path(self.sdir, "spec"), {"compiled_at": 7, "items": [
            {"kind": "incoherent", "severity": "warning", "file": "doc", "line": 3, "message": "how many retries?"},
            {"kind": "unspecified", "severity": "info", "file": "src/a.py", "line": 9, "message": "added a cache"},
            {"kind": "model", "severity": "error", "file": "src/b.py", "line": 2, "spec_line": 4, "message": "no logger"},
        ]})
        self.prompts = []

    def fake(self, result):
        def run(root, sdir, key, model, prompt, **kw):
            self.prompts.append((key, prompt))
            return {"structured_output": result, "total_cost_usd": 0.01}
        return mock.patch.object(compiler, "run_claude", run)

    def test_propose_writes_and_annotates(self):
        res = {"fixes": [fix(("- retry on error\n- log", "- retry on error (3 times)\n- log"), item=1),
                         fix(("nope", "x"), item=3)],
               "skipped": [{"item": 2, "reason": "the worker should drop the cache"}]}
        with self.fake(res):
            r = fixes.propose(self.root, "S1", "spec")
        key, prompt = self.prompts[0]
        self.assertEqual(key, "compile")  # the compile session: it knows why it reported them
        self.assertIn("#1 [incoherent/warning] doc:3 how many retries?", prompt)
        self.assertIn("#3 [model/error] src/b.py:2 no logger (doc line 4)", prompt)
        self.assertIn("   3| - retry on error", prompt)
        self.assertEqual([f["message"] for f in r["fixes"]], ["how many retries?"])
        self.assertEqual(r["rejected"][0]["reason"], "its text is not in the doc")
        self.assertEqual(r["skipped"][0]["message"], "added a cache")
        self.assertEqual(r["compiled_at"], 7)
        self.assertEqual(paths.read_json(fixes.fixes_path(self.sdir, "spec")), r)

    def test_buffer_text_and_range(self):
        buf = DOC.replace("- log it", "- log it (unsaved)")
        with self.fake({"fixes": [fix(("(unsaved)", "(saved)"))]}):
            r = fixes.propose(self.root, "S1", "spec", text=buf, line_from=4, line_to=4)
        prompt = self.prompts[0][1]
        self.assertIn("- log it (unsaved)", prompt)
        self.assertNotIn("#1 ", prompt)  # line 3: outside the range
        self.assertNotIn("#2 ", prompt)  # no doc line at all
        self.assertIn("#3 ", prompt)
        self.assertEqual(len(r["fixes"]), 1)

    def test_unanswered_items_are_listed(self):
        with self.fake({"fixes": [fix(("- log it", "- log it once"), item=3)]}):
            r = fixes.propose(self.root, "S1", "spec")
        self.assertEqual([(x["item"], x["reason"]) for x in r["skipped"]],
                         [(1, "no doc change proposed"), (2, "no doc change proposed")])

    def test_cli(self):
        content = os.path.join(self.root, "buf.md")
        paths.write_text(content, DOC.replace("- log it", "- log it (buffer)"))
        out = io.StringIO()
        with self.fake({"fixes": [fix(("(buffer)", "(fixed)"), item=3)]}), \
                mock.patch.object(paths, "find_root", lambda *a: self.root), \
                mock.patch.object(paths, "attached", lambda r: "S1"), mock.patch("sys.stdout", out):
            self.assertEqual(cli.main(["fix", "--json", "--from", "4", "--content-file", content]), 0)
        r = json.loads(out.getvalue())
        self.assertEqual((r["doc"], r["fixes"][0]["edits"][0]["line"]), ("spec", 4))
        err = io.StringIO()
        with mock.patch.object(paths, "find_root", lambda *a: self.root), \
                mock.patch.object(paths, "attached", lambda r: "S1"), mock.patch("sys.stderr", err):
            self.assertEqual(cli.main(["fix", "--doc", "nope"]), 1)
        self.assertIn("no doc 'nope'", err.getvalue())

    def test_messages_are_one_line_and_bad_output_is_an_error(self):
        paths.update_json(docs.diag_path(self.sdir, "spec"), items=[
            {"kind": "incoherent", "severity": "warning", "file": "doc", "line": 3, "message": "two\nlines"}])
        with self.fake({"fixes": [], "skipped": [{"item": 1, "reason": "a\nb"}]}):
            r = fixes.propose(self.root, "S1", "spec")
        self.assertEqual((r["skipped"][0]["message"], r["skipped"][0]["reason"]), ("two lines", "a b"))
        for bad in (["a list"], "text"):
            with mock.patch.object(compiler, "run_claude", lambda *a, **k: {"structured_output": None,
                                                                           "result": json.dumps(bad)}), \
                    self.assertRaises(compiler.CompilerError):
                fixes.propose(self.root, "S1", "spec")

    def test_nothing_to_fix(self):
        with self.fake({"fixes": []}), self.assertRaises(compiler.CompilerError):
            fixes.propose(self.root, "S1", "spec", line_from=1, line_to=2)
        self.assertEqual(self.prompts, [])


if __name__ == "__main__":
    unittest.main()
