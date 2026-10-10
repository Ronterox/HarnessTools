"""Backend tests that need no Claude: `python3 -m unittest discover -s aic/tests`."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aic import anchors, compiler, hook, paths, usage, watch, worker  # noqa: E402
from aic.transcript import Parser  # noqa: E402


def rec_user(text, uid, parent=None):
    return {"type": "user", "uuid": uid, "parentUuid": parent, "timestamp": "2026-10-10T10:00:00Z",
            "message": {"role": "user", "content": text}}


def rec_asst(blocks, uid, stop=None):
    return {"type": "assistant", "uuid": uid, "timestamp": "2026-10-10T10:00:01Z",
            "message": {"role": "assistant", "content": blocks, "stop_reason": stop}}


def rec_result(tid, result, uid, err=False):
    return {"type": "user", "uuid": uid, "timestamp": "2026-10-10T10:00:02Z", "toolUseResult": result,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                                     "content": "ok", "is_error": err}]}}


class ParserTest(unittest.TestCase):
    def test_turns_why_and_hunks(self):
        root = "/proj"
        p = Parser(root)
        evs = []
        evs += p.feed(rec_user("add a route", "u1", "r0"))
        evs += p.feed(rec_asst([{"type": "thinking", "thinking": ""}], "a0"))
        evs += p.feed(rec_asst([{"type": "text", "text": "I'll add the route in app.js."}], "a1", "tool_use"))
        evs += p.feed(rec_asst([{"type": "tool_use", "id": "T1", "name": "Edit",
                                 "input": {"file_path": "/proj/app.js", "old_string": "a", "new_string": "b"}}],
                               "a2", "tool_use"))
        evs += p.feed(rec_result("T1", {"filePath": "/proj/app.js", "structuredPatch": [
            {"oldStart": 3, "oldLines": 1, "newStart": 3, "newLines": 2, "lines": [" x", "+y"]}]}, "u2"))
        evs += p.feed(rec_asst([{"type": "text", "text": "Done."}], "a3", "end_turn"))
        kinds = [e["kind"] for e in evs]
        self.assertEqual(kinds, ["prompt", "tool", "text"])
        tool = evs[1]
        self.assertEqual(tool["title"], "Edit app.js:3-4")
        self.assertEqual(tool["why"], "I'll add the route in app.js.")
        self.assertEqual(tool["file"], "app.js")
        self.assertEqual(tool["hunks"][0]["lines"], [" x", "+y"])
        self.assertEqual(evs[2]["text"], "Done.")

    def test_resent_prompt_same_turn_and_commands(self):
        p = Parser("/proj")
        a = p.feed(rec_user("hello", "u1", "root"))
        b = p.feed(rec_user("hello again", "u2", "root"))
        c = p.feed(rec_user("<command-message>x</command-message>\n<command-name>/team</command-name>"
                            "\n<command-args>go</command-args>", "u3", "u2"))
        d = p.feed(rec_user("<command-name>/model</command-name>", "u4", "u3"))
        self.assertEqual(a[0]["turn"], 1)
        self.assertEqual(b[0]["turn"], 1)
        self.assertEqual(c[0]["text"], "/team go")
        self.assertEqual(c[0]["turn"], 2)
        self.assertEqual(d, [])

    def test_compact_boundary(self):
        p = Parser("/proj")
        p.feed(rec_user("x", "u1"))
        evs = p.feed({"type": "system", "subtype": "compact_boundary", "uuid": "b1", "timestamp": "2026-10-10T10:00:00Z",
                      "compactMetadata": {"trigger": "manual", "preTokens": 107395, "postTokens": 9799}})
        self.assertEqual((evs[0]["kind"], evs[0]["pre"], evs[0]["post"]), ("compact", 107395, 9799))
        self.assertEqual(p.last_usage, {"input_tokens": 9799})

    def test_write_create(self):
        p = Parser("/proj")
        p.feed(rec_user("x", "u1"))
        p.feed(rec_asst([{"type": "tool_use", "id": "W", "name": "Write",
                          "input": {"file_path": "/proj/new.txt", "content": "a\nb"}}], "a1"))
        ev = p.feed(rec_result("W", {"type": "create", "filePath": "/proj/new.txt", "content": "a\nb",
                                     "structuredPatch": []}, "u2"))[0]
        self.assertEqual(ev["hunks"], [{"start": 1, "count": 2, "lines": ["+a", "+b"]}])


class TouchedTest(unittest.TestCase):
    def test_context_lines_do_not_stretch_ranges(self):
        rs = [{"start": 13, "end": 13, "event": "a", "actor": "worker"},
              {"start": 16, "end": 16, "event": "b", "actor": "worker"}]
        hunk = {"start": 12, "count": 9, "lines": [" c1", " c2", " c3", "+n1", "+n2", "+n3", " c4", " c5", " c6"]}
        watch.apply_hunks(rs, [hunk], "e", "worker")
        got = [(r["start"], r["end"], r["event"]) for r in rs]
        self.assertIn((13, 13, "a"), got)   # context line stays put
        self.assertIn((19, 19, "b"), got)   # line after the insertion moves down 3
        self.assertIn((15, 17, "e"), got)

    def test_deleted_lines_shrink_range(self):
        rs = [{"start": 5, "end": 8, "event": "a", "actor": "worker"}]
        watch.apply_hunks(rs, [{"start": 5, "count": 1, "lines": [" x", "-y", "-z"]}], "e", "worker")
        self.assertEqual([(r["start"], r["end"]) for r in rs if r["event"] == "a"], [(5, 6)])

    def test_shift_and_add(self):
        rs = []
        watch.apply_hunks(rs, [{"start": 10, "count": 2, "lines": ["+a", "+b"]}], "e1", "worker")
        self.assertEqual([(r["start"], r["end"]) for r in rs], [(10, 11)])
        # insert 3 lines above: the old range moves down
        watch.apply_hunks(rs, [{"start": 2, "count": 3, "lines": ["+x", "+y", "+z"]}], "e2", "human")
        self.assertEqual([(r["start"], r["end"], r["event"]) for r in rs],
                         [(2, 4, "e2"), (13, 14, "e1")])
        # delete 2 lines at 5 (pure deletion): later range moves up, marker at 5
        watch.apply_hunks(rs, [{"start": 5, "count": 0, "lines": ["-p", "-q"]}], "e3", "worker")
        self.assertIn((11, 12, "e1"), [(r["start"], r["end"], r["event"]) for r in rs])


class ProjectTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        worker.init(self.root, "/x/bin/aic")
        self.sid = "S1"
        os.makedirs(paths.session_dir(self.root, self.sid))

    def run_hook(self, event, **extra):
        data = {"hook_event_name": event, "session_id": self.sid, "cwd": self.root,
                "transcript_path": "/t.jsonl"}
        data.update(extra)
        with mock.patch.dict(os.environ, {"TMUX_PANE": "%9"}), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(data))), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            hook.main()
        return out.getvalue()

    def test_init_idempotent(self):
        worker.init(self.root, "/x/bin/aic")
        s = paths.read_json(os.path.join(self.root, ".claude", "settings.local.json"))
        self.assertEqual(len(s["hooks"]["PreToolUse"]), 1)
        self.assertEqual(s["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "/x/bin/aic-hook")

    def test_hook_registers_and_holds_once(self):
        self.assertEqual(self.run_hook("UserPromptSubmit"), "")
        w = paths.worker(self.root, self.sid)
        self.assertEqual((w["state"], w["pane"]), ("busy", "%9"))
        worker.hold(self.root, self.sid)
        out = json.loads(self.run_hook("PreToolUse", tool_name="Edit"))
        self.assertFalse(out["continue"])
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(paths.worker(self.root, self.sid)["state"], "held")
        self.assertEqual(self.run_hook("PreToolUse", tool_name="Edit"), "")  # one-shot

    def test_compiler_role_is_ignored(self):
        with mock.patch.dict(os.environ, {"AIC_ROLE": "compiler"}):
            self.run_hook("UserPromptSubmit")
        self.assertIsNone(paths.worker(self.root, self.sid))

    def test_submit_diff_and_selection(self):
        sdir = paths.session_dir(self.root, self.sid)
        paths.write_text(os.path.join(sdir, "spec.md"), "# Spec\n- a\n- b\n")
        m = worker.build_submit(self.root, self.sid)
        self.assertIn("Here is my spec", m["header"])
        self.assertIn("- b", m["body"])
        self.assertEqual(m["spec"], "# Spec\n- a\n- b\n")
        paths.write_text(os.path.join(sdir, "spec.submitted.md"), "# Spec\n- a\n- b\n")
        with self.assertRaises(worker.WorkerError):
            worker.build_submit(self.root, self.sid)
        paths.write_text(os.path.join(sdir, "spec.md"), "# Spec\n- a\n- c\n")
        m = worker.build_submit(self.root, self.sid)
        self.assertIn("\n-- b", m["body"])
        self.assertIn("\n+- c", m["body"])
        m = worker.build_submit(self.root, self.sid, 2, 2)
        self.assertIsNone(m["spec"])
        self.assertTrue(m["body"].endswith("- a"))
        for msg in (m, worker.build_submit(self.root, self.sid)):
            self.assertFalse(any(ch.isdigit() for ch in msg["header"]), msg["header"])

    def test_init_refuses_broken_settings(self):
        sp = os.path.join(self.root, ".claude", "settings.local.json")
        with open(sp, "w") as f:
            f.write('{"permissions": {"allow": ["Bash"]},}')
        with self.assertRaises(worker.WorkerError):
            worker.init(self.root, "/x/bin/aic")
        self.assertIn("permissions", paths.read_text(sp))

    def test_deliver_refuses_dead_or_foreign_pane(self):
        w = {"session_id": self.sid, "pane": "%1", "pid": 999999999, "transcript_path": None}
        paths.write_json(os.path.join(paths.aic_dir(self.root), "workers", self.sid + ".json"), w)
        typed = []

        def fake_tmux(*args, input=None):
            typed.append(args)
            return "/dev/pts/77\n" if args[0] == "display-message" else ""
        with mock.patch.object(worker, "_tmux", fake_tmux):
            with self.assertRaisesRegex(worker.WorkerError, "not found"):
                worker.deliver(self.root, self.sid, "hello", "body")
            w["pid"] = os.getpid()  # alive, but its stdin is not that pane's tty
            paths.write_json(os.path.join(paths.aic_dir(self.root), "workers", self.sid + ".json"), w)
            with self.assertRaisesRegex(worker.WorkerError, "does not own"):
                worker.deliver(self.root, self.sid, "hello", "body")
        self.assertFalse(any(a[0] in ("send-keys", "paste-buffer") for a in typed))

    def test_deliver_never_types_into_dialog(self):
        typed = []

        def fake_tmux(*args, input=None):
            typed.append(args)
            if args[0] == "capture-pane":
                return " Do you want to proceed?\n ❯ 1. Yes\n   2. No\n"
            return ""
        with mock.patch.object(worker, "verify_pane", lambda w: "%1"), \
                mock.patch.object(worker, "_tmux", fake_tmux):
            with self.assertRaisesRegex(worker.WorkerError, "dialog"):
                worker.deliver(self.root, self.sid, "hello", "body")
        self.assertFalse(any(a[0] in ("send-keys", "paste-buffer") for a in typed))

    def test_deliver_sees_text_after_nbsp_prompt(self):
        typed = []

        def fake_tmux(*args, input=None):
            typed.append(args)
            return "out\n❯\u00a0[aic] hello there\n" if args[0] == "capture-pane" else ""
        with mock.patch.object(worker, "verify_pane", lambda w: "%1"), \
                mock.patch.object(worker, "_tmux", fake_tmux), mock.patch("time.sleep"):
            worker.deliver(self.root, self.sid, "[aic] hello there", "body")
        self.assertIn(("send-keys", "-t", "%1", "Enter"), typed)

    def test_deliver_stops_before_enter_if_text_not_in_input(self):
        typed = []

        def fake_tmux(*args, input=None):
            typed.append(args)
            return "some output\n❯ \n" if args[0] == "capture-pane" else ""
        with mock.patch.object(worker, "verify_pane", lambda w: "%1"), \
                mock.patch.object(worker, "_tmux", fake_tmux), mock.patch("time.sleep"):
            with self.assertRaisesRegex(worker.WorkerError, "not pressing Enter"):
                worker.deliver(self.root, self.sid, "[aic] hello", "body")
        self.assertNotIn(("send-keys", "-t", "%1", "Enter"), typed)

    def test_watcher_merges_side_logs(self):
        paths.write_text(os.path.join(paths.aic_dir(self.root), "current"), self.sid)
        sdir = paths.session_dir(self.root, self.sid)
        paths.append_jsonl(os.path.join(sdir, "human.jsonl"),
                           {"ts": 5.0, "file": "a.py", "hunks": [{"start": 1, "count": 1, "lines": ["+z"]}]})
        worker.control(self.root, self.sid, "hold")
        w = watch.Watcher(self.root)
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            w.tick()
        tr = paths.read_jsonl(os.path.join(sdir, "trace.jsonl"))
        self.assertEqual([e["kind"] for e in tr], ["human", "hold"])
        self.assertEqual(paths.read_json(os.path.join(sdir, "touched.json"))["a.py"][0]["actor"], "human")
        paths.append_jsonl(os.path.join(sdir, "human.jsonl"),
                           {"ts": 9.0, "file": "a.py", "hunks": [{"start": 1, "count": 1, "lines": ["+q"]}]})
        w.tick()
        tr = paths.read_jsonl(os.path.join(sdir, "trace.jsonl"))
        self.assertEqual([e["seq"] for e in tr], [1, 2, 3])


class WatcherResumeTest(unittest.TestCase):
    def test_restart_resumes_without_renumbering(self):
        root = tempfile.mkdtemp()
        worker.init(root, "/x/bin/aic")
        paths.write_text(os.path.join(paths.aic_dir(root), "current"), "S3")
        sdir = paths.session_dir(root, "S3")
        paths.append_jsonl(os.path.join(sdir, "human.jsonl"),
                           {"ts": 5.0, "file": "a.py", "hunks": [{"start": 1, "count": 1, "lines": ["+z"]}]})
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            w = watch.Watcher(root)
            w.tick()
            w.emit([{"id": "x", "ts": 6.0, "kind": "text", "turn": 0, "text": "live-only event"}])
            ino = os.stat(os.path.join(sdir, "trace.jsonl")).st_ino
            w2 = watch.Watcher(root)  # a restart
            w2.tick()
        tr = paths.read_jsonl(os.path.join(sdir, "trace.jsonl"))
        self.assertEqual([(e["seq"], e["id"]) for e in tr][-1], (2, "x"))  # kept, not rebuilt
        self.assertEqual(os.stat(os.path.join(sdir, "trace.jsonl")).st_ino, ino)

    def test_bad_records_are_skipped(self):
        root = tempfile.mkdtemp()
        worker.init(root, "/x/bin/aic")
        paths.write_text(os.path.join(paths.aic_dir(root), "current"), "S4")
        sdir = paths.session_dir(root, "S4")
        os.makedirs(sdir)
        with open(os.path.join(sdir, "human.jsonl"), "w") as f:
            f.write('[1,2]\n"x"\n{"file": 3}\n{"ts": 1, "file": "a", "hunks": [{"start": 1, "lines": ["+a"]}]}\n')
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            watch.Watcher(root).tick()
        self.assertEqual(len(paths.read_jsonl(os.path.join(sdir, "trace.jsonl"))), 1)
        self.assertEqual(Parser("/p").feed({"type": "user", "message": {"content": ["str", 5]}}), [])


class BashAttributionTest(unittest.TestCase):
    def test_bash_edit_becomes_event(self):
        root = tempfile.mkdtemp()
        worker.init(root, "/x/bin/aic")
        paths.write_text(os.path.join(paths.aic_dir(root), "current"), "S2")
        with open(os.path.join(root, "app.py"), "w") as f:
            f.write("a\nb\nc\n")
        w = watch.Watcher(root)
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            w.tick()  # rebuild + snapshot
        with open(os.path.join(root, "app.py"), "w") as f:
            f.write("a\nB\nc\nd\n")
        bash = {"id": "t-1", "ts": 1.0, "kind": "tool", "turn": 1, "tool": "Bash", "title": "Bash: sed",
                "why": "fix b", "file": None, "hunks": [], "input": "{}", "error": False}
        out = w.attribute_bash([bash])
        self.assertEqual([e["id"] for e in out], ["t-1", "t-1:app.py"])
        self.assertEqual(out[1]["hunks"], [{"start": 2, "count": 1, "lines": ["-b", "+B"]},
                                           {"start": 4, "count": 1, "lines": ["+d"]}])
        self.assertEqual(out[1]["why"], "fix b")
        self.assertEqual(w.attribute_bash([dict(bash, id="t-2")])[1:], [])  # nothing new

    def test_first_file_in_empty_tree_is_seen(self):
        root = tempfile.mkdtemp()
        snap = watch.Snapshot(root)
        with open(os.path.join(root, "a.txt"), "w") as f:
            f.write("x\n")
        self.assertEqual(snap.rescan(), {"a.txt": ([], ["x"])})

    def test_big_replace_keeps_every_line(self):
        hunks = watch.diff_hunks(["o%d" % i for i in range(500)], ["n%d" % i for i in range(10)])
        rs = [{"start": 600, "end": 600, "event": "a", "actor": "worker"}]
        watch.apply_hunks(rs, hunks, "e", "worker")
        self.assertIn((110, 110), [(r["start"], r["end"]) for r in rs if r["event"] == "a"])

    def test_bad_hunk_does_not_break_emit(self):
        root = tempfile.mkdtemp()
        worker.init(root, "/x/bin/aic")
        paths.write_text(os.path.join(paths.aic_dir(root), "current"), "S5")
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            w = watch.Watcher(root)
            w.tick()
            w.emit([{"id": "h", "ts": 1.0, "kind": "human", "turn": 0, "file": "a",
                     "hunks": [{"start": "x", "lines": ["+a"]}]}])
        self.assertEqual(paths.read_json(os.path.join(w.dir, "watch.json"))["seq"], 1)


FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "anchor_cases.json")


class AnchorTest(unittest.TestCase):
    def setUp(self):
        fx = paths.read_json(FIXTURE)
        self.fx = fx
        self.root = tempfile.mkdtemp()
        for d in fx["dirs"]:
            os.makedirs(os.path.join(self.root, d), exist_ok=True)
        for f in fx["files"]:
            with open(os.path.join(self.root, f), "w") as fh:
                fh.write(fx["app_js"] if f == "src/app.js" else "x\n")

    def kinds(self, text):
        return [[a["kind"], a["anchor"]] for a in anchors.parse(text, self.root)]

    def test_shared_fixture(self):
        for case in self.fx["cases"]:
            self.assertEqual(self.kinds(case["text"]), case["anchors"], case["text"])

    def test_symbol_prefers_definition(self):
        a = anchors.resolve(anchors.parse("@src/app.js:render", self.root)[0], self.root)
        self.assertEqual(a["line"], 3)
        self.assertIn("    3| function render(a) {", a["text"])

    def test_worker_blocks_skip_plain_project_files(self):
        b = anchors.blocks("@src/app.js and @src/app.js:3-4", self.root, for_worker=True)
        self.assertNotIn("### @src/app.js\n", b)
        self.assertIn("### @src/app.js:3-4", b)

    def test_past_eof_and_binary(self):
        a = anchors.resolve(anchors.parse("@src/app.js:90-99", self.root)[0], self.root)
        self.assertEqual(a["text"], "(file has 5 lines)")
        with open(os.path.join(self.root, "blob.bin"), "wb") as f:
            f.write(b"\x00\x01" * 100)
        a = anchors.resolve(anchors.parse("@blob.bin", self.root)[0], self.root)
        self.assertIn("binary file", a["text"])

    def test_outside_content_only_from_human_text(self):
        other = tempfile.mkdtemp()
        secret = os.path.join(other, "notes.txt")
        with open(secret, "w") as f:
            f.write("TOPSECRET\n")
        text = "see @%s" % secret
        self.assertIn("TOPSECRET", anchors.blocks(text, self.root))
        self.assertNotIn("TOPSECRET", anchors.blocks(text, self.root, allow_outside=False))
        self.assertNotIn("TOPSECRET", anchors.blocks(text, self.root, for_worker=True))
        os.symlink(secret, os.path.join(self.root, "link.txt"))  # in-project symlink pointing out
        self.assertNotIn("TOPSECRET", anchors.blocks("@link.txt:1", self.root, allow_outside=False))

    def test_credentials_never_read(self):
        os.makedirs(os.path.join(self.root, "k"))
        with open(os.path.join(self.root, "k", "server.pem"), "w") as f:
            f.write("PRIVATE\n")
        self.assertNotIn("PRIVATE", anchors.blocks("@k/server.pem", self.root))

    def test_add_dirs_limits(self):
        other = tempfile.mkdtemp()
        home = os.path.expanduser("~")
        parent = os.path.dirname(self.root)
        got = anchors.add_dirs(["@/ @~/ @%s/ @%s/ @diff:/etc @log:../.. @%s/" % (home, parent, other)], self.root)
        self.assertEqual(got, [os.path.realpath(other)])

    def test_git_anchors_resolve_in_repo(self):
        def git(*a):
            subprocess.run(["git", "-C", self.root] + list(a), capture_output=True, check=True)
        git("init", "-q")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "first")
        a = anchors.resolve(anchors.parse("@log", self.root)[0], self.root)
        self.assertIn("first", a["text"])
        a = anchors.resolve(anchors.parse("@show:HEAD", self.root)[0], self.root)
        self.assertIn("first", a["text"])


class UsageTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.home = tempfile.mkdtemp()
        self.sdir = paths.session_dir(self.root, "W")
        os.makedirs(self.sdir)
        self.tdir = os.path.join(self.home, "proj")
        os.makedirs(self.tdir)
        self.p = mock.patch.object(paths, "claude_project_dir", lambda root: self.tdir)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def transcript(self, sid, *usages):
        with open(os.path.join(self.tdir, sid + ".jsonl"), "w") as f:
            for u in usages:
                f.write(json.dumps({"type": "assistant", "message": {"model": "m-1", "usage": u}}) + "\n")

    def test_context_is_last_call_and_cost_accumulates(self):
        self.transcript("C1", {"input_tokens": 1, "cache_read_input_tokens": 900},
                        {"input_tokens": 2, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 500})
        out = {"session_id": "C1", "total_cost_usd": 0.5, "modelUsage": {"m-1": {"contextWindow": 10000}},
               "usage": {"input_tokens": 3, "cache_read_input_tokens": 1900, "output_tokens": 7}}
        usage.record_call(self.root, self.sdir, "compile", out, "compile")
        d = usage.record_call(self.root, self.sdir, "compile", out, "compile")
        c = d["compile"]
        self.assertEqual((c["ctx_tokens"], c["ctx_window"], c["turns"], c["cost_usd"]), (1502, 10000, 2, 1.0))
        self.assertEqual(c["calls"][-1]["cache_read"], 1900)
        usage.restart(self.sdir, "compile", "C1")
        c = paths.read_json(os.path.join(self.sdir, "usage.json"))["compile"]
        self.assertEqual((c["session_id"], c["ctx_tokens"], c["compacted_from"]), (None, 0, "C1"))

    def test_report_backfills_and_quota_is_optional(self):
        self.transcript("W", {"input_tokens": 5, "cache_read_input_tokens": 5})
        with mock.patch.object(usage, "OMC_CACHE", "/nonexistent"):
            rep = usage.report(self.root, "W")
        self.assertEqual(rep["sessions"]["worker"]["ctx_tokens"], 10)
        self.assertTrue(rep["sessions"]["worker"]["backfilled"])
        self.assertIsNone(rep["quota"])
        cache = os.path.join(self.home, "q.json")
        paths.write_json(cache, {"lastSuccessAt": paths.now() * 1000, "data": {
            "fiveHourPercent": 57, "weeklyPercent": 15,
            "scopedWeeklyBuckets": [{"id": "fable", "label": "Fable", "percent": 0, "isActive": False}]}})
        with mock.patch.object(usage, "OMC_CACHE", cache):
            q = usage.quota()
        self.assertEqual((q["five_hour"], q["weekly"], q["buckets"][0]["label"]), (57, 15, "Fable"))

    def test_compact_seeds_next_session(self):
        paths.write_json(os.path.join(self.sdir, "state.json"), {"ask": "OLD", "ask_upto_seq": 9})
        prompts = []

        def fake(root, sdir, key, model, prompt, *a):
            prompts.append(prompt)
            return {"result": "HANDOFF TEXT", "session_id": "OLD"}
        with mock.patch.object(compiler, "_run_claude", fake):
            msg = compiler.compact(self.root, "W", "ask")
        st = paths.read_json(os.path.join(self.sdir, "state.json"))
        self.assertIn("compacted", msg)
        self.assertEqual((st["ask"], st["ask_seed"], st["ask_upto_seq"]), (None, "HANDOFF TEXT", 0))
        self.assertIn("handoff", prompts[0].lower())
        with mock.patch.object(compiler, "_run_claude", fake):
            self.assertIn("reset", compiler.compact(self.root, "W", "compile", reset=True)
                          .replace("no session yet", "reset"))


class CompileQueueTest(unittest.TestCase):
    def test_busy_request_is_queued_and_run_by_holder(self):
        root = tempfile.mkdtemp()
        sdir = paths.session_dir(root, "S")
        os.makedirs(sdir)
        runs = []

        def fake_once(r, s):
            runs.append(1)
            if len(runs) == 1:  # while we compile, someone saves again
                self.assertIsNone(compiler.compile_spec(root, "S"))
            return {"items": [], "n": len(runs)}
        with mock.patch.object(compiler, "_compile_once", fake_once):
            d = compiler.compile_spec(root, "S")
        self.assertEqual(len(runs), 2)  # the queued request ran, once
        self.assertEqual(d["n"], 2)
        self.assertFalse(compiler.compiling(sdir))
        self.assertFalse(os.path.exists(os.path.join(sdir, "compile.again")))


if __name__ == "__main__":
    unittest.main()
