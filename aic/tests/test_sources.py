import io
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aic import cli, compiler, paths, sources  # noqa: E402

SRC = """def total(xs):
    s = 0
    for i in range(1, len(xs)):
        s += xs[i]
    return s


def mean(xs):
    return total(xs) / len(xs)
"""

ITEMS = [
    {"severity": "error", "code": "off-by-one", "message": "loop skips the first element", "line": 3,
     "snippet": "range(1, len(xs))", "label": "starts at 1",
     "secondary": [{"line": 4, "snippet": "xs[i]", "label": "indexed here"}],
     "help": "start at 0", "edits": [{"line": 3, "old": "range(1, len(xs))", "new": "range(len(xs))"}]},
    {"severity": "warning", "code": "zero-division", "message": "empty list divides by zero", "line": 9,
     "snippet": "/ len(xs)", "label": "len(xs) may be 0", "note": "callers pass user input",
     "help": "return 0.0 for an empty list",
     "edits": [{"line": 9, "old": "    return total(xs) / len(xs)\n",
                "new": "    if not xs:\n        return 0.0\n    return total(xs) / len(xs)\n"}]},
]
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class ResolveTest(unittest.TestCase):
    def test_fragment_becomes_whole_lines(self):
        e = sources._resolve(SRC, ITEMS[0]["edits"][0])
        self.assertEqual(e, {"line": 3, "old": "    for i in range(1, len(xs)):\n",
                             "new": "    for i in range(len(xs)):\n"})

    def test_insertion_trimmed_to_added_lines(self):
        e = sources._resolve(SRC, ITEMS[1]["edits"][0])
        self.assertEqual(e, {"line": 9, "old": "", "new": "    if not xs:\n        return 0.0\n"})

    def test_nearest_occurrence_and_errors(self):
        text = "x = 1\ny = 2\nx = 1\n"
        self.assertEqual(sources._resolve(text, {"line": 3, "old": "x = 1", "new": "x = 3"})["line"], 3)
        self.assertEqual(sources._resolve(text, {"line": 1, "old": "x = 1", "new": "x = 3"})["line"], 1)
        self.assertIn("not in the file", sources._resolve(text, {"line": 1, "old": "z", "new": "w"}))
        self.assertIn("changes nothing", sources._resolve(text, {"line": 1, "old": "x", "new": "x"}))
        self.assertIn("U+202E", sources._resolve(text, {"line": 1, "old": "x", "new": "x‮"}))
        # deleting a whole line, copied with its line-number prefix
        self.assertEqual(sources._resolve(text, {"line": 2, "old": "   2| y = 2\n", "new": ""}),
                         {"line": 2, "old": "y = 2\n", "new": ""})

    def test_check_drops_overlapping_suggestion(self):
        items = sources.check(SRC, ITEMS + [dict(ITEMS[0], code="dup")])
        self.assertTrue(items[0].get("fix") and items[1].get("fix"))
        self.assertIn("overlaps", items[2]["fix_error"])


class PlaceTest(unittest.TestCase):
    def setUp(self):
        self.items = sources.check(SRC, ITEMS)

    def test_moves_past_your_edits(self):
        cur = "# mine\n" + SRC.replace("def mean(xs):", "def mean(xs):  # mine too")
        target, report = sources.place(SRC, cur, self.items)
        self.assertEqual([s for _, s, _ in report], ["ok", "changed"])  # line 8 changed: no fix next to it
        self.assertIn("# mine\n", target)
        self.assertIn("for i in range(len(xs)):", target)
        self.assertNotIn("return 0.0", target)

    def test_replacement_next_to_your_edit_still_applies(self):
        cur = SRC.replace("    s = 0", "    s = 0  # mine")  # line 2, right above the line-3 fix
        target, report = sources.place(SRC, cur, self.items)
        self.assertEqual([s for _, s, _ in report], ["ok", "ok"])
        self.assertIn("    s = 0  # mine\n    for i in range(len(xs)):", target)

    def test_already_applied_is_not_offered_again(self):
        once, _ = sources.place(SRC, SRC, self.items)
        again, report = sources.place(SRC, once, self.items)
        self.assertEqual(again, once)
        self.assertEqual([s for _, s, _ in report], ["applied", "applied"])


    def test_edges_are_neighbours_so_insertions_apply_once(self):
        for base, edit in (("import sys\nprint(1)\n", {"line": 1, "old": "import sys\n", "new": "import os\nimport sys\n"}),
                           ("a\nb\n", {"line": 2, "old": "b\n", "new": "b\nc\n"})):
            items = sources.check(base, [{"severity": "error", "message": "m", "line": 1, "edits": [edit]}])
            once, _ = sources.place(base, base, items)
            again, report = sources.place(base, once, items)
            self.assertEqual(again, once)
            self.assertEqual(report[0][1], "applied")

    def test_never_lands_on_a_look_alike_line_of_yours(self):
        base = "def f(x):\n    return None\n\ndef g():\n    pass\n"
        items = sources.check(base, [{"severity": "error", "message": "m", "line": 2,
                                      "edits": [{"line": 2, "old": "return None", "new": "return x"}]}])
        cur = "def g():\n    return None\n"
        target, report = sources.place(base, cur, items)
        self.assertEqual((target, report[0][1]), (cur, "changed"))

    def test_insertion_elsewhere_is_not_applied(self):
        base = "def a():\n    g(x)\n    h()\n\ndef b():\n    break\n"
        items = sources.check(base, [{"severity": "error", "message": "m", "line": 2,
                                      "edits": [{"line": 2, "old": "    g(x)\n", "new": "    g(x)\n    break\n"}]}])
        _, report = sources.place(base, base.replace("g(x)", "g(y)"), items)
        self.assertEqual(report[0][1], "changed")


class InputTest(unittest.TestCase):
    def test_crlf_files_keep_crlf(self):
        text = "def f(xs):\r\n    return 1 / len(xs)\r\n"
        e = sources._resolve(text, {"line": 1, "old": "def f(xs):\n    return 1 / len(xs)\n",
                                    "new": "def f(xs):\n    if not xs:\n        return 0\n    return 1 / len(xs)\n"})
        self.assertEqual(e, {"line": 2, "old": "", "new": "    if not xs:\r\n        return 0\r\n"})

    def test_line_numbers_count_only_newlines(self):
        self.assertIn("   3| x = 1", sources._numbered("a\n\f\nx = 1\n"))

    def test_model_cannot_set_fix_itself(self):
        items = sources.check(SRC, [{"severity": "error", "message": "m", "line": 1,
                                     "fix": [{"line": 1, "old": SRC, "new": "PWNED"}], "fix_error": "x"}])
        self.assertNotIn("fix", items[0])
        self.assertNotIn("fix_error", items[0])


class StreamTest(unittest.TestCase):
    """aic compile -v: claude's stream-json events reach the progress lines; the result is the same."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bin)
        self.f = os.path.join(self.tmp.name, "m.py")
        with open(self.f, "w") as f:
            f.write(SRC)

    def tearDown(self):
        self.tmp.cleanup()

    def fake_claude(self, body):
        p = os.path.join(self.bin, "claude")
        with open(p, "w") as f:
            f.write("#!/bin/sh\ncat >/dev/null\n" + body)
        os.chmod(p, 0o755)

    def test_events_become_steps(self):
        import json
        evs = [{"type": "system", "subtype": "init", "model": "sonnet"},
               {"type": "assistant", "message": {"content": [{"type": "text", "text": "checking callers"},
                {"type": "tool_use", "name": "Grep", "input": {"pattern": "total(", "path": self.tmp.name}},
                {"type": "tool_use", "name": "Read", "input": {"file_path": self.f, "offset": 3, "limit": 5}}]}},
               {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "no such file"}]}},
               {"type": "result", "subtype": "success", "session_id": "S", "total_cost_usd": 0.02, "num_turns": 3,
                "structured_output": {"summary": "ok", "items": ITEMS}}]
        events = os.path.join(self.tmp.name, "events.jsonl")
        with open(events, "w") as f:
            f.write("".join(json.dumps(e) + "\n" for e in evs))
        self.fake_claude("cat '%s'\n" % events)
        out = io.StringIO()
        env = {"PATH": self.bin + os.pathsep + os.environ["PATH"], "XDG_STATE_HOME": os.path.join(self.tmp.name, "st")}
        with mock.patch.dict(os.environ, env):
            d = sources.compile_file(self.f, progress=lambda ctx: sources.Progress(ctx, sources.Style(False), out))
        self.assertEqual(len(d["items"]), 2)
        lines = ANSI.sub("", out.getvalue())
        for want in ("    Checking m.py with sonnet", "             checking callers",
                     '   Searching "total(" in .', "     Reading m.py:3-7", "      Failed no such file",
                     "    Finished m.py in"):
            self.assertIn(want, lines)

    def test_timeout_kills_it(self):
        self.fake_claude("sleep 30\n")
        with mock.patch.dict(os.environ, {"PATH": self.bin + os.pathsep + os.environ["PATH"]}):
            with self.assertRaisesRegex(compiler.CompilerError, "timed out"):
                compiler.run_claude(self.tmp.name, os.path.join(self.tmp.name, "s"), "compile", "m", "p",
                                    timeout=1, progress=lambda ev: None)


class RenderTest(unittest.TestCase):
    def test_rustc_layout(self):
        diag = {"file": "m.py", "base": SRC, "summary": "", "items": sources.check(SRC, ITEMS)}
        out = sources.render(diag, "m.py", sources.Style(False))
        self.assertIn("error[off-by-one]: loop skips the first element\n --> m.py:3:14\n  |\n"
                      "3 |     for i in range(1, len(xs)):\n"
                      "  |              ^^^^^^^^^^^^^^^^^ starts at 1\n"
                      "4 |         s += xs[i]\n"
                      "  |              ----- indexed here\n  |\n"
                      "help: start at 0\n  |\n"
                      "3 -     for i in range(1, len(xs)):\n"
                      "3 +     for i in range(len(xs)):\n  |\n", out)
        self.assertIn("   = note: callers pass user input\nhelp: return 0.0 for an empty list\n", out)
        self.assertIn("error: could not compile `m.py` due to 1 previous error; 1 warning emitted\n"
                      "help: run `aic fix m.py` to apply 2 suggestions\n", out)
        colored = sources.render(diag, "m.py", sources.Style(True))
        self.assertIn("\x1b[1;38;5;9merror[off-by-one]\x1b[0m", colored)
        self.assertEqual(ANSI.sub("", colored), out)

    def test_labels_on_one_line(self):
        it = {"severity": "error", "message": "m", "line": 1, "snippet": "c", "label": "C",
              "secondary": [{"line": 1, "snippet": "a", "label": "A"}, {"line": 1, "snippet": "b", "label": "B"}]}
        out = sources.render_item(it, ["a b c\n"], "f", sources.Style(False))
        self.assertIn("1 | a b c\n  | - - ^ C\n  | | |\n  | | B\n  | A\n", out)


class FileFlowTest(unittest.TestCase):
    """compile -> edit by hand -> fix through git's hunk picker, with a fake compiler."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        os.makedirs(os.path.join(self.dir, ".aic"))
        self.f = os.path.join(self.dir, "m.py")
        with open(self.f, "w") as f:
            f.write(SRC)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake(self, root, sdir, key, model, prompt, **kw):
        self.calls.append((root, sdir, prompt))
        return {"structured_output": {"summary": "two bugs", "items": ITEMS}, "session_id": "S"}

    def run_cli(self, *argv, stdin=None):
        err = io.StringIO()
        with mock.patch.object(compiler, "run_claude", self.fake), mock.patch.object(sys, "stderr", err):
            code = cli.main(list(argv))
        return code, err.getvalue()

    def test_compile_keeps_context_per_file(self):
        code, out = self.run_cli("compile", self.f)
        self.assertEqual(code, 1)  # an error, like rustc
        self.assertIn("error[off-by-one]", out)
        ctx = sources.Ctx(self.f)
        self.assertEqual(ctx.sdir, os.path.join(self.dir, ".aic", "files", "m.py"))
        self.assertEqual(paths.read_json(ctx.diag_path)["base"], SRC)
        with open(self.f, "a") as f:
            f.write("# more\n")
        self.run_cli("compile", self.f)
        prompt = self.calls[1][2]
        self.assertIn("YOUR PREVIOUS DIAGNOSTICS FOR THIS FILE", prompt)
        self.assertIn("error[off-by-one] line 3", prompt)
        self.assertIn("+# more", prompt)

    def test_outside_a_project_nothing_is_written_there(self):
        os.rmdir(os.path.join(self.dir, ".aic"))
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": os.path.join(self.dir, "state")}):
            ctx = sources.Ctx(self.f)
            self.assertEqual(ctx.sdir, os.path.join(self.dir, "state", "aic", "files", self.f.lstrip("/")))
            self.run_cli("compile", self.f)
        self.assertFalse(os.path.exists(os.path.join(self.dir, ".aic")))

    def test_fix_picks_hunks_and_skips_yours(self):
        self.run_cli("compile", self.f)
        mine = "# my own change\n" + SRC
        with open(self.f, "w") as f:
            f.write(mine)
        # git's picker: the two suggestions are close, so split, then yes to the first, no to the second;
        # my line is context at most, never offered
        p = subprocess.run([os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "aic"), "fix", self.f], input="s\ny\nn\n", capture_output=True,
                           text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           env=dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1"))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("Split into 2 hunks", p.stdout)
        self.assertNotIn("+# my own change", p.stdout)
        self.assertNotIn("-# my own change", p.stdout)
        with open(self.f) as f:
            got = f.read()
        self.assertTrue(got.startswith("# my own change\n"))
        self.assertIn("for i in range(len(xs)):", got)
        self.assertNotIn("return 0.0", got)
        code, out = self.run_cli("fix", self.f, "--all")
        with open(self.f) as f:
            self.assertIn("return 0.0", f.read())
        code, out = self.run_cli("fix", self.f)
        self.assertIn("nothing to apply (2 already applied)", out)

    def git_fix(self, path, answers, **env):
        return subprocess.run([os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "aic"),
                               "fix", path], input=answers, capture_output=True, text=True,
                              env=dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", **env))

    def test_fix_through_a_symlink_fixes_the_target(self):
        link = os.path.join(self.dir, "link.py")
        os.symlink(self.f, link)
        self.run_cli("compile", link)
        p = self.git_fix(link, "y\n")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(os.path.islink(link))
        with open(self.f) as f:
            self.assertIn("return 0.0", f.read())

    def test_inherited_git_env_never_reaches_your_repo(self):
        repo = os.path.join(self.dir, "repo.git")
        subprocess.run(["git", "init", "-q", "--bare", repo], check=True)
        self.run_cli("compile", self.f)
        p = self.git_fix(self.f, "y\n", GIT_OBJECT_DIRECTORY=os.path.join(repo, "objects"), GIT_WORK_TREE="/")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(subprocess.run(["git", "--git-dir", repo, "count-objects"], capture_output=True,
                                        text=True).stdout.split()[0], "0")

    def test_colon_names_are_literal(self):
        f = os.path.join(self.dir, ":odd.py")
        os.rename(self.f, f)
        self.run_cli("compile", f)
        p = self.git_fix(f, "y\n")
        with open(f) as fh:
            self.assertIn("return 0.0", fh.read(), p.stdout + p.stderr)

    def test_file_saved_meanwhile_is_left_alone(self):
        self.run_cli("compile", self.f)
        reads = iter([SRC, SRC + "# saved meanwhile\n"])
        with mock.patch.object(sources.Ctx, "read", lambda self: next(reads)):
            code, out = self.run_cli("fix", self.f, "--all")
        self.assertEqual(code, 1)
        self.assertIn("changed while aic read it", out)

    def test_flags_of_the_other_mode_are_errors(self):
        for argv in (["fix", self.f, "--json"], ["fix", "--all"], ["compile", "--reset"], ["compile", self.f, "--doc", "x"]):
            with mock.patch.object(sys, "stderr", io.StringIO()), self.assertRaises(SystemExit):
                cli.main(argv)

    def test_fix_warnings_asks_once_for_the_bare_ones(self):
        bare = [dict(ITEMS[0]), {"severity": "warning", "code": "zero-division", "message": "empty list divides by zero",
                                 "line": 9, "help": "guard empty input"},
                {"severity": "warning", "code": "naming", "message": "unclear name", "line": 1}]
        compiled = {"structured_output": {"summary": "s", "items": bare}, "session_id": "S"}
        answer = {"structured_output": {"fixes": [{"item": 2, "help": "return 0.0 for an empty list", "edits": [
            {"line": 9, "old": "    return total(xs) / len(xs)\n",
             "new": "    if not xs:\n        return 0.0\n    return total(xs) / len(xs)\n"}]}],
            "skipped": [{"item": 3, "reason": "needs the callers' intent"}]}, "session_id": "S"}
        prompts_seen = []

        def fake(root, sdir, key, model, prompt, **kw):
            prompts_seen.append(prompt)
            return answer if kw.get("schema") is sources.WARNINGS_SCHEMA else compiled

        err = io.StringIO()
        with mock.patch.object(compiler, "run_claude", fake), mock.patch.object(sys, "stderr", err):
            cli.main(["compile", self.f])
            self.assertIn("`aic fix %s --warnings` asks for edits to 2 warnings without one" % self.f, err.getvalue())
            cli.main(["fix", self.f, "--warnings", "--all"])
            self.assertEqual(len(prompts_seen), 2)
            self.assertIn("#2 line 9 [zero-division] empty list divides by zero | help: guard empty input", prompts_seen[1])
            self.assertIn("#3 line 1 [naming]", prompts_seen[1])
            self.assertNotIn("#1 ", prompts_seen[1])  # the error has its edit already
            cli.main(["fix", self.f, "--warnings"])  # nothing left to ask
        self.assertEqual(len(prompts_seen), 2)
        out = ANSI.sub("", err.getvalue())
        self.assertIn("help: return 0.0 for an empty list\n   |\n 9 +     if not xs:", out)
        self.assertIn("no edit for m.py:1 (naming): needs the callers' intent", out)
        with open(self.f) as f:
            got = f.read()
        self.assertIn("return 0.0", got)
        self.assertIn("range(len(xs))", got)
        items = paths.read_json(sources.Ctx(self.f).diag_path)["items"]
        self.assertEqual([bool(it.get("fix")) for it in items], [True, True, False])

    def test_fix_before_compile(self):
        code, out = self.run_cli("fix", self.f)
        self.assertEqual(code, 1)
        self.assertIn("run `aic compile", out)


if __name__ == "__main__":
    unittest.main()
