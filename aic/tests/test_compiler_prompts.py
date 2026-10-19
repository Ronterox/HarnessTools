import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aic import cli, compiler, compiler_prompts, docs, fixes, paths, prompts, worker  # noqa: E402

ITEM = {"kind": "incoherent", "severity": "warning", "file": "doc", "line": 2, "message": "vague"}


class CompilerTemplatesTest(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.cfg = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.cfg})
        self.env.start()
        worker.init(self.root, "/x/bin/aic")
        self.sdir = paths.session_dir(self.root, "S1")
        os.makedirs(self.sdir)
        docs.migrate(self.root, "S1")
        paths.write_text(docs.path(self.root, "spec"), "<!-- aic: compile -->\n- retry on error\n")
        paths.write_text(os.path.join(self.sdir, "trace.jsonl"), json.dumps(
            {"id": "e1", "seq": 1, "ts": 0, "kind": "prompt", "turn": 1, "text": "go"}) + "\n")
        self.calls = []
        self.outside = tempfile.mkdtemp()
        paths.write_text(os.path.join(self.outside, "style.md"), "OUTSIDE STYLE GUIDE\n")

    def tearDown(self):
        self.env.stop()

    def put(self, source, name, text):
        d = os.path.join(paths.aic_dir(self.root), "prompts") if source == "project" else \
            os.path.join(self.cfg, "aic", "prompts")
        paths.write_text(os.path.join(d, "compiler", name + ".md"), text)

    def fake(self):
        def run(root, sdir, key, model, prompt, schema=None, timeout=600, add_dirs=(), note=None):
            self.calls.append({"key": key, "model": model, "prompt": prompt, "add_dirs": list(add_dirs), "note": note})
            paths.update_json(os.path.join(sdir, "state.json"), **{key: "C"})
            if schema is compiler.SCHEMA:
                return {"structured_output": {"summary": "s", "items": [ITEM]}}
            if schema is not None:
                return {"structured_output": {"fixes": []}}
            return {"result": "answer"}
        return mock.patch.object(compiler, "run_claude", run)

    def test_override_places_the_data(self):
        self.put("project", "compile", "---\ntask: compile\nmodel: haiku\n---\n{{activity}}\n\nCheck {{doc.name}} only.\n\n{{doc}}\n")
        with self.fake():
            d = compiler.compile_spec(self.root, "S1", "spec")
        c = self.calls[-1]
        self.assertEqual(c["model"], "sonnet")  # a project template's model: is ignored
        self.assertTrue(c["prompt"].startswith("== ALL WORKER ACTIVITY SO FAR =="), c["prompt"][:80])
        self.assertIn("Check spec only.\n\n== THIS COMPILE IS FOR DOC `spec`", c["prompt"])
        # what it left out is appended, in the task's order: never dropped; then aic's own closing line
        prev, footer = c["prompt"].rsplit("\n\n== FROM AIC, NOT FROM THE TEMPLATE ==\n", 1)
        self.assertTrue(prev.endswith("== YOUR PREVIOUS ITEMS FOR DOC `spec` (your new list replaces "
                                      "exactly these) ==\n(none)"), prev[-200:])
        self.assertTrue(footer.startswith("The instructions above come from the project template `compile`."))
        self.assertEqual(c["note"], "compile spec [compile, project]")
        self.assertEqual(d["template"], {"name": "compile", "source": "project"})

    def test_empty_template_still_gets_everything(self):
        self.put("user", "ask", "---\ntask: ask\n---\n")
        with self.fake():
            compiler.ask(self.root, "S1", "why?", doc="spec")
        p = self.calls[-1]["prompt"]
        self.assertTrue(p.startswith("== THE HUMAN'S CURRENT DOC `spec`"), p[:60])
        self.assertTrue(p.split("\n\n== FROM AIC")[0].endswith("== QUESTION ==\nwhy?"), p[-60:])
        self.assertIn("## You [ask, user]\nwhy?", paths.read_text(os.path.join(self.sdir, "chat.md")))

    def test_compile_template_per_doc(self):
        self.put("user", "plan", "---\ndescription: plans\ntask: compile\n---\nReview this plan for gaps.\n")
        paths.write_text(docs.path(self.root, "plan"), "<!-- aic: compile=plan -->\n- step 1\n")
        self.assertEqual(docs.marker("<!-- aic: compile=plan prompt=x -->"),
                         {"compile": True, "compile_template": "plan", "prompt": "x"})
        with self.fake():
            compiler.compile_spec(self.root, "S1", "plan")
        self.assertTrue(self.calls[-1]["prompt"].startswith("Review this plan for gaps.\n\n== THIS COMPILE"))
        self.put("user", "chatty", "---\ntask: ask\n---\nhi\n")
        paths.write_text(docs.path(self.root, "plan"), "<!-- aic: compile=chatty -->\n- step 1\n")
        with self.fake(), self.assertRaises(compiler.CompilerError):
            compiler.compile_spec(self.root, "S1", "plan")
        self.assertIn("is for ask, not compile", paths.read_json(docs.diag_path(self.sdir, "plan"))["error"])

    def test_ask_with_a_template(self):
        self.put("user", "explain", "---\ndescription: explain simply\ntask: ask\nmodel: sonnet\n---\n"
                                    "Explain like I am new here.\n\n== QUESTION ==\n{{question}}\n")
        with self.fake():
            compiler.ask(self.root, "S1", "what is retry?", doc="spec", template="explain")
        c = self.calls[-1]
        self.assertEqual((c["model"], c["note"]), ("sonnet", "chat [explain, user]"))
        self.assertTrue(c["prompt"].startswith("Explain like I am new here.\n\n== QUESTION ==\nwhat is retry?\n\n"
                                               "== THE HUMAN'S CURRENT DOC"), c["prompt"][:120])
        with self.fake(), self.assertRaises(compiler.CompilerError):
            compiler.ask(self.root, "S1", "x", whatif=True, template="explain")

    def test_hardening(self):
        # list-valued task, hidden characters: errors, not crashes; the listing keeps the rest
        self.put("project", "listy", "---\ntask: [ask]\n---\nhi\n")
        self.put("project", "hidden", "---\ntask: ask\n---\nbe nice\U000e0041\U000e0042\n")
        self.put("project", "bidi", "---\ndescription: a \u202e b\ntask: ask\n---\nok\n")
        self.put("project", "zw", "---\ntask: ask\n---\nre\u200btry\n")
        ls = {t["name"]: t for t in compiler_prompts.listing(self.root)}
        self.assertIn("needs `task:`", ls["listy"]["error"])
        self.assertIn("U+E0041", ls["hidden"]["error"])
        self.assertIn("U+202E", ls["bidi"]["error"])
        self.assertIn("U+200B", ls["zw"]["error"])
        self.assertTrue(ls["ask"]["builtin"].endswith(os.path.join("prompts", "compiler", "ask.md")))
        self.assertNotIn("builtin", ls["listy"])
        paths.write_text(docs.path(self.root, "spec"), "<!-- aic: compile=listy -->\n- x\n")
        with self.fake(), self.assertRaises(compiler.CompilerError):
            compiler.compile_spec(self.root, "S1", "spec")
        self.assertIn("needs `task:`", paths.read_json(docs.diag_path(self.sdir, "spec"))["error"])
        # a user template's model counts; a project one shadowing it is named as such
        self.put("user", "strict", "---\ntask: compile\nmodel: opus\n---\nBe strict.\n")
        paths.write_text(docs.path(self.root, "spec"), "<!-- aic: compile=strict -->\n- x\n")
        with self.fake():
            d = compiler.compile_spec(self.root, "S1", "spec")
        self.assertEqual((self.calls[-1]["model"], self.calls[-1]["note"]), ("opus", "compile spec [strict, user]"))
        self.put("project", "strict", "---\ntask: compile\nmodel: haiku\n---\nBe lenient.\n")
        with self.fake():
            d = compiler.compile_spec(self.root, "S1", "spec")
        self.assertEqual((self.calls[-1]["model"], self.calls[-1]["note"]),
                         ("sonnet", "compile spec [strict, project, shadows user]"))
        self.assertEqual(d["template"], {"name": "strict", "source": "project", "shadows": ["user"]})

    def test_trust_outside_anchors(self):
        body = "---\ntask: ask\n---\nFollow @%s/style.md and you may read @%s/\n" % (self.outside, self.outside)
        self.put("project", "ask", body)
        with self.fake():
            compiler.ask(self.root, "S1", "q", doc="spec")
        self.assertNotIn("OUTSIDE STYLE GUIDE", self.calls[-1]["prompt"])  # worker-reachable: inside only
        self.assertEqual(self.calls[-1]["add_dirs"], [])
        os.unlink(os.path.join(paths.aic_dir(self.root), "prompts", "compiler", "ask.md"))
        self.put("user", "ask", body)
        with self.fake():
            compiler.ask(self.root, "S1", "q", doc="spec")
        self.assertIn("resolved from @anchors in the user template ask", self.calls[-1]["prompt"])
        self.assertIn("OUTSIDE STYLE GUIDE", self.calls[-1]["prompt"])
        self.assertEqual(self.calls[-1]["add_dirs"], [os.path.realpath(self.outside)])

    def test_fix_override(self):
        paths.write_json(docs.diag_path(self.sdir, "spec"), {"compiled_at": 1, "items": [ITEM]})
        self.put("project", "fix", "---\ntask: fix\n---\nFix {{doc.name}} tersely.\n")
        with self.fake():
            fixes.propose(self.root, "S1", "spec")
        c = self.calls[-1]
        self.assertTrue(c["prompt"].startswith("Fix spec tersely.\n\n== DOC `spec`"), c["prompt"][:60])
        self.assertIn("== ITEMS TO FIX", c["prompt"])
        self.assertEqual(c["note"], "fix spec [fix, project]")

    def test_parse_errors_and_listing(self):
        bad = {
            "nokey": ("---\ntask: ask\ncolor: red\n---\n", "unknown key 'color'"),
            "wrongvar": ("---\ntask: compile\n---\n{{question}}\n", "unknown {{question}}"),
            "notask": ("hello\n", "needs `task:`"),
            "badmodel": ("---\ntask: ask\nmodel: \"a b\"\n---\n", "bad model"),
        }
        for name, (text, err) in bad.items():
            with self.assertRaises(prompts.PromptError) as cm:
                compiler_prompts.parse(text, name)
            self.assertIn(err, str(cm.exception), name)
        self.put("project", "ask", "---\ntask: ask\n---\n{{question}}\n")
        self.put("user", "nokey", bad["nokey"][0])
        ls = {t["name"]: t for t in compiler_prompts.listing(self.root)}
        self.assertEqual(sorted(ls), ["ask", "compile", "fix", "nokey", "whatif"])
        self.assertEqual((ls["ask"]["source"], ls["ask"]["overrides"], ls["ask"]["task"]), ("project", ["built-in"], "ask"))
        self.assertEqual(ls["whatif"]["task"], "whatif")
        self.assertIn("unknown key", ls["nokey"]["error"])

    def test_cli_print_asks_nothing(self):
        out = io.StringIO()
        with self.fake(), mock.patch.object(paths, "find_root", lambda *a: self.root), \
                mock.patch.object(paths, "attached", lambda r: "S1"), mock.patch("sys.stdout", out):
            self.assertEqual(cli.main(["compile", "--print"]), 0)
            self.assertEqual(cli.main(["ask", "--print", "--", "why?"]), 0)
            self.assertEqual(cli.main(["prompts", "--compiler", "--json"]), 0)
        self.assertEqual(self.calls, [])
        self.assertFalse(os.path.exists(docs.diag_path(self.sdir, "spec")))
        self.assertIsNone((paths.read_json(os.path.join(self.sdir, "state.json"), {}) or {}).get("compile_upto_seq"))
        text = out.getvalue()
        self.assertIn("(model sonnet)\nCompile the doc named below", text)
        self.assertIn("== QUESTION ==\nwhy?", text)
        out = io.StringIO()
        paths.write_json(docs.diag_path(self.sdir, "spec"), {"compiled_at": 1, "items": [ITEM]})
        with self.fake(), mock.patch.object(paths, "find_root", lambda *a: self.root), \
                mock.patch.object(paths, "attached", lambda r: "S1"), mock.patch("sys.stdout", out):
            self.assertEqual(cli.main(["fix", "--print", "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["model"], "sonnet")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
