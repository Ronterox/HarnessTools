"""Prompt templates: parsing, the template language, header safety, lookup
order, rendering and what a send records."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aic import docs, paths, prompts, worker  # noqa: E402

APP = "def load(path):\n    return open(path).read()\n"


def tpl(header, body, **meta):
    lines = ["---", 'header: "%s"' % header] + ["%s: %s" % kv for kv in meta.items()] + ["---"]
    return "\n".join(lines) + "\n" + body


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.cfg = tempfile.mkdtemp()
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.cfg})
        self.env.start()
        self.addCleanup(self.env.stop)
        worker.init(self.root, "/x/bin/aic")
        paths.write_text(os.path.join(self.root, "app.py"), APP)
        self.sdir = paths.session_dir(self.root, "S1")
        os.makedirs(self.sdir)
        os.makedirs(docs.docs_dir(self.root))
        paths.write_text(docs.path(self.root, "notes"), "<!-- aic: compile -->\n# Notes\n- a\n- b\n")

    def project(self, name, text):
        paths.write_text(os.path.join(self.root, ".aic", "prompts", name + ".md"), text)

    def render(self, prompt, **kw):
        return prompts.render(self.root, "S1", prompt, kw.pop("doc", "notes"), **kw)


class LanguageTest(Base):
    def run_body(self, body, **values):
        nodes = prompts._tree(body)
        v = dict.fromkeys(prompts.VARS, "")
        v.update(values)
        return prompts._render(nodes, v, [])

    def test_sections_and_values(self):
        self.assertEqual(self.run_body("a {{doc}} b", doc="X"), "a X b")
        self.assertEqual(self.run_body("{{#diff}}has{{/diff}}{{^diff}}none{{/diff}}", diff="d"), "has")
        self.assertEqual(self.run_body("{{#diff}}has{{/diff}}{{^diff}}none{{/diff}}", diff="  \n"), "none")
        self.assertEqual(self.run_body("{{#doc}}[{{#diff}}{{diff}}{{/diff}}]{{/doc}}", doc="x", diff="y"), "[y]")

    def test_standalone_tag_lines_vanish(self):
        self.assertEqual(self.run_body("top\n{{#doc}}\nmid\n{{/doc}}\nend\n", doc="x"), "top\nmid\nend\n")
        self.assertEqual(self.run_body("top\n  {{^doc}}  \nmid\n{{/doc}}\nend", doc="x"), "top\nend")

    def test_values_are_not_rescanned_and_escape(self):
        self.assertEqual(self.run_body("{{doc}}", doc="{{diff}} {{#x}}"), "{{diff}} {{#x}}")
        self.assertEqual(self.run_body(r"\{{doc}} {{doc}}", doc="v"), "{{doc}} v")

    def test_errors(self):
        for body, msg in (("{{nope}}", "unknown {{nope}}"), ("{{#doc}}x", "never closed"),
                          ("{{#doc}}{{/diff}}", "does not close"), ("{{/doc}}", "does not close"),
                          ("{{#doc}}{{^doc}}{{/doc}}{{/doc}}", "nested inside itself")):
            with self.assertRaises(prompts.PromptError) as cm:
                prompts._tree(body)
            self.assertIn(msg, str(cm.exception))

    def test_quotes_comments_crlf(self):
        meta, body = prompts.parse('---\r\nheader: "Do it" # why\r\ndescription: \'single\'  # c\r\n---\r\n{{#doc}}\r\nx\r\n{{/doc}}\r\n')
        self.assertEqual((meta["header"], meta["description"]), ("Do it", "single"))
        self.assertEqual(self.run_body(body, doc="d"), "x\n")
        with self.assertRaises(prompts.PromptError):
            prompts.parse('---\nheader: "Do it" and more\n---\n')

    def test_front_matter(self):
        meta, body = prompts.parse('---\n# comment\ndescription: two words  # trailing\nheader: "Say \\"hi\\":"\n'
                                   'requires: [changed, diff]\nmarks_sent: true\n---\n\nbody\n')
        self.assertEqual(meta["description"], "two words")
        self.assertEqual(meta["header"], 'Say "hi":')
        self.assertEqual(meta["requires"], ["changed", "diff"])
        self.assertTrue(meta["marks_sent"])
        self.assertEqual(body, "\nbody\n")
        for text, msg in (("---\nheadr: x\n---\n", "unknown key 'headr'"), ("---\ndescription: x\n---\n", "needs a `header:`"),
                          ('---\nheader: "x"\nrequires: [nope]\n---\n', "requires unknown 'nope'"),
                          ('---\nheader: "x"\n', "not closed"), ('---\nheader: "{{#doc}}"\n---\n', "never closed")):
            with self.assertRaises(prompts.PromptError) as cm:
                prompts.parse(text)
            self.assertIn(msg, str(cm.exception))


class HeaderTest(unittest.TestCase):
    def test_invisible_and_foreign_characters(self):
        for h in ("a\U000E0041b", "zero\u200bwidth", "bidi\u202eflip", "c\x9bone", "soft\xadhyphen",
                  "arabic \u0663", "x" * 400):
            with self.assertRaises(prompts.PromptError, msg=repr(h)):
                prompts.check_header(h)
        self.assertEqual(prompts.check_header("café, naïve: ok;"), "[aic] café, naïve: ok;")
        with self.assertRaises(prompts.PromptError):
            prompts.check_body("looks fine\U000E0049\U000E0047")
        prompts.check_body("emoji 👍 and accents é are fine")

    def test_prefix_and_one_line(self):
        self.assertEqual(prompts.check_header("  Do it\n now: "), "[aic] Do it now:")
        self.assertEqual(prompts.check_header("[aic] Already"), "[aic] Already")
        self.assertEqual(prompts.check_header("/clear everything"), "[aic] /clear everything")  # never a slash command

    def test_dangerous_characters(self):
        for h in ("fix line 12", "see @app.py", "a\\b", "bell\x07", ""):
            with self.assertRaises(prompts.PromptError, msg=repr(h)):
                prompts.check_header(h)


class LookupTest(Base):
    def test_project_beats_user_beats_builtin(self):
        self.assertEqual(prompts.find(self.root, "spec")["source"], "built-in")
        paths.write_text(os.path.join(self.cfg, "aic", "prompts", "spec.md"), tpl("User spec:", "{{doc}}"))
        self.assertEqual(prompts.find(self.root, "spec")["source"], "user")
        self.project("spec", tpl("Project spec:", "{{doc}}"))
        self.assertEqual(prompts.find(self.root, "spec")["source"], "project")
        names = [t["name"] for t in prompts.listing(self.root)]
        self.assertEqual(names.count("spec"), 1)
        self.assertTrue({"spec", "selection", "message", "review"} <= set(names))

    def test_overrides_are_reported(self):
        self.project("spec", tpl("Project spec:", "{{doc}}"))
        spec = [t for t in prompts.listing(self.root) if t["name"] == "spec"][0]
        self.assertEqual((spec["source"], spec["overrides"]), ("project", ["built-in"]))

    def test_broken_template_is_listed_with_its_error(self):
        self.project("bad", "---\nheader: x\nwat: 1\n---\n")
        bad = [t for t in prompts.listing(self.root) if t["name"] == "bad"][0]
        self.assertIn("unknown key 'wat'", bad["error"])
        with self.assertRaises(prompts.PromptError):
            prompts.find(self.root, "nope")


class RenderTest(Base):
    def test_builtin_message_needs_input(self):
        with self.assertRaises(prompts.PromptError):
            self.render("message")
        r = self.render("message", input_text="stop touching @app.py:1-2 please")
        self.assertEqual(r["header"], "[aic] Please read my message below and act on it:")
        self.assertTrue(r["body"].startswith("stop touching"))
        self.assertIn("### @app.py:1-2", r["body"])  # anchors in typed input are resolved
        self.assertFalse(r["receipt"]["marks_sent"])

    def test_positioning_and_explicit_anchors(self):
        self.project("mine", tpl("Check this:", "Context first:\n{{anchors}}\n\nThen my notes ({{doc.name}}):\n{{doc}}\n\n"
                                 "Compare with @app.py:load"))
        r = self.render("mine")
        self.assertTrue(r["body"].startswith("Context first:\n### @app.py:load"), r["body"])
        self.assertNotIn("Referenced context", r["body"])  # placed by the template, not appended
        self.assertIn("Then my notes (notes):\n# Notes\n- a\n- b\n\nCompare with @app.py:load", r["body"])
        self.assertNotIn("aic: compile", r["body"])

    def test_requires_and_selection(self):
        self.project("sel", tpl("Look:", "{{selection}}", requires="[selection]"))
        with self.assertRaises(prompts.PromptError) as cm:
            self.render("sel")
        self.assertIn("select lines", str(cm.exception))
        self.assertEqual(self.render("sel", line_from=3, line_to=3)["body"], "- a")

    def test_header_vars_are_checked(self):
        self.project("hd", tpl("About {{doc.name}}:", "{{doc}}"))
        paths.write_text(docs.path(self.root, "v2"), "x\n")
        self.assertEqual(self.render("hd")["header"], "[aic] About notes:")
        with self.assertRaises(prompts.PromptError):  # digits reach the header through a value
            self.render("hd", doc="v2")

    def test_hand_edits_always_reach_the_worker(self):
        ev = {"id": "e2", "seq": 2, "ts": 0, "kind": "human", "turn": 1, "file": "app.py",
              "hunks": [{"start": 1, "count": 1, "lines": ["+x"]}]}
        paths.write_text(os.path.join(self.sdir, "trace.jsonl"), json.dumps(ev) + "\n")
        self.project("plain", tpl("Note:", "{{doc}}"))
        self.assertTrue(self.render("plain")["body"].endswith("My hand edits since my last message: app.py:1-1"))
        self.project("placed", tpl("Note:", "edits: {{hand_edits}}\n{{doc}}"))
        b = self.render("placed")["body"]
        self.assertTrue(b.startswith("edits: app.py:1-1") and b.count("app.py:1-1") == 1, b)

    def test_conditional_placement_never_drops_edits_or_anchors(self):
        ev = {"id": "e2", "seq": 2, "ts": 0, "kind": "human", "turn": 1, "file": "app.py",
              "hunks": [{"start": 1, "count": 1, "lines": ["+x"]}]}
        paths.write_text(os.path.join(self.sdir, "trace.jsonl"), json.dumps(ev) + "\n")
        self.project("cond", tpl("Note:", "{{#diff}}{{hand_edits}} {{anchors}}{{/diff}}{{doc}} @app.py:1-1"))
        r = self.render("cond")  # never sent: no diff, so the section is out
        self.assertIn("My hand edits since my last message: app.py:1-1", r["body"])
        self.assertIn("Referenced context", r["body"])
        self.assertTrue(r["receipt"]["edits_sent"])

    def test_inverted_anchor_section_is_not_a_source(self):
        self.project("inv", tpl("Note:", "{{doc}}{{^anchors}} see @app.py:1-1{{/anchors}}"))
        self.assertNotIn("Referenced context", self.render("inv")["body"])

    def test_review_is_not_a_send_of_the_doc(self):
        r = self.render("review")
        self.assertIn("My notes (notes):\n\n# Notes", r["body"])
        self.assertFalse(r["receipt"]["marks_sent"])
        self.assertIsNone(r["receipt"]["sent"])


class SendTest(Base):
    def send(self, r, header=None, body=None):
        with mock.patch.object(worker, "deliver", lambda root, sid, h, b, now=False: self.sent.append((h, b)) or "%9"):
            return worker.send_rendered(self.root, "S1", header or r["header"], body if body is not None else r["body"],
                                        r["receipt"])

    def setUp(self):
        super().setUp()
        self.sent = []

    def test_only_marks_sent_moves_the_diff_base(self):
        self.send(self.render("review"))
        self.assertEqual(paths.read_text(docs.sent_path(self.sdir, "notes")), "")
        r = self.render("spec")
        self.assertIn("Here is my spec", r["header"])
        self.send(r)
        self.assertEqual(paths.read_text(docs.sent_path(self.sdir, "notes")), "# Notes\n- a\n- b\n")
        paths.write_text(docs.path(self.root, "notes"), "<!-- aic: compile -->\n# Notes\n- a\n- c\n")
        self.assertIn("I changed my spec", self.render("spec")["header"])

    def test_the_edited_preview_is_what_is_sent(self):
        r = self.render("spec")
        self.send(r, header="Implement only the first bullet:", body="# Notes\n- a")
        self.assertEqual(self.sent[-1], ("[aic] Implement only the first bullet:", "# Notes\n- a"))
        with self.assertRaises(worker.WorkerError):  # an edited header is checked again
            self.send(self.render("review"), header="fix bug 7")

    def test_stale_preview_is_refused(self):
        r1 = self.render("spec")
        r2 = self.render("spec")
        self.send(r1)
        with self.assertRaises(worker.WorkerError) as cm:
            self.send(r2)
        self.assertIn("render it again", str(cm.exception))
        self.send(self.render("review"))  # a template that does not mark the doc sent is never stale

    def test_bad_receipt(self):
        for k, v in (("doc", "../etc"), ("seq", "9"), ("seq", 99), ("sent", 5), ("marks_sent", "yes"), ("prompt", None)):
            r = self.render("review")
            r["receipt"][k] = v
            with self.assertRaises(worker.WorkerError, msg=(k, v)):
                self.send(r)
        self.assertEqual(self.sent, [])  # nothing typed for any of them
        with self.assertRaises(worker.WorkerError):
            worker.send_rendered(self.root, "S1", None, "b", self.render("review")["receipt"])

    def test_hand_edit_mark_moves_only_when_they_were_sent(self):
        st = os.path.join(self.sdir, "state.json")
        paths.write_text(os.path.join(self.sdir, "trace.jsonl"), json.dumps(
            {"id": "e1", "seq": 1, "ts": 0, "kind": "prompt", "turn": 1, "text": "x"}) + "\n")
        self.send(self.render("review"))  # no hand edits: the mark stays
        self.assertNotIn("submit_seq", paths.read_json(st, {}) or {})
        paths.write_text(os.path.join(self.sdir, "trace.jsonl"), "".join(json.dumps(e) + "\n" for e in (
            {"id": "e1", "seq": 1, "ts": 0, "kind": "prompt", "turn": 1, "text": "x"},
            {"id": "e2", "seq": 2, "ts": 0, "kind": "human", "turn": 1, "file": "app.py",
             "hunks": [{"start": 1, "count": 1, "lines": ["+x"]}]})))
        old = self.render("review")  # rendered before the newer one below, sent after it
        self.send(self.render("review"))
        self.assertEqual(paths.read_json(st)["submit_seq"], 2)
        old["receipt"]["seq"] = 1
        self.send(old)
        self.assertEqual(paths.read_json(st)["submit_seq"], 2)  # never moves back

    def test_a_diff_without_marks_sent_also_goes_stale(self):
        paths.write_text(docs.sent_path(self.sdir, "notes"), "# Notes\n- a\n")
        self.project("d", tpl("See the change:", "{{diff}}", requires="[diff]"))
        r = self.render("d")
        self.assertTrue(r["receipt"]["uses_previous"])
        self.send(self.render("spec"))
        with self.assertRaises(worker.WorkerError):
            self.send(r)

    def test_no_preview_send_uses_built_ins_only(self):
        self.project("spec", tpl("Planted by someone:", "{{doc}}"))
        self.assertIn("Planted", self.render("spec")["header"])  # the preview shows the project one
        with mock.patch.object(worker, "deliver", lambda root, sid, h, b, now=False: self.sent.append((h, b)) or "%9"):
            worker.submit(self.root, "S1", doc="notes")
        self.assertEqual(self.sent[-1][0], "[aic] Here is my spec, please implement it:")
        self.project("mine", tpl("Mine:", "{{doc}}"))
        with self.assertRaises(prompts.PromptError):
            worker.submit(self.root, "S1", doc="notes", prompt="mine")

    def test_every_body_is_pasted_as_data(self):
        # verified live (Claude Code 2.1.x): pastes under 3 newlines are inlined as typed text
        for body in ("ok", "a\nb", "x" * 500):
            self.assertGreaterEqual(worker.paste_text(body).count("\n"), 3, body)
        calls = []
        with mock.patch.object(worker, "verify_pane", lambda w: "%9"), \
                mock.patch.object(worker, "_check_screen", lambda *a, **k: None), \
                mock.patch.object(worker, "_tmux", lambda *a, **k: calls.append((a, k))):
            worker.deliver(self.root, "S1", "[aic] do this", "short")
        self.assertEqual([k.get("input") for a, k in calls if a[0] == "load-buffer"], ["\n\nshort\n"])

    def test_trailing_semicolon_survives_tmux(self):
        calls = []
        with mock.patch.object(worker, "verify_pane", lambda w: "%9"), \
                mock.patch.object(worker, "_check_screen", lambda *a, **k: None), \
                mock.patch.object(worker, "_tmux", lambda *a, **k: calls.append(a)):
            worker.deliver(self.root, "S1", "[aic] do this;")
        self.assertEqual(calls[0], ("send-keys", "-t", "%9", "-l", "[aic] do this\\;"))

    def test_tmux_goes_to_the_workers_own_server(self):
        paths.write_json(os.path.join(self.root, ".aic", "workers", "S1.json"),
                         {"session_id": "S1", "pane": "%9", "tmux_socket": "/tmp/tmux-1000/work"})
        runs = []
        with mock.patch.object(worker, "verify_pane", lambda w: "%9"), \
                mock.patch.object(worker, "_check_screen", lambda *a, **k: None), \
                mock.patch("subprocess.run", lambda cmd, **k: runs.append(cmd) or mock.Mock(returncode=0, stdout="")):
            worker.deliver(self.root, "S1", "[aic] go", "body")
        self.assertTrue(runs and all(c[:3] == ["tmux", "-S", "/tmp/tmux-1000/work"] for c in runs), runs)


if __name__ == "__main__":
    unittest.main()
