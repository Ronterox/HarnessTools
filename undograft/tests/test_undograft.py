"""End-to-end tests of the undograft hook against real headless Neovims.

Each test gets its own XDG_RUNTIME_DIR, so the hook only ever finds the
Neovims started here (never the user's). Run: python3 -m unittest discover undograft/tests
"""
import glob
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(os.path.dirname(HERE), "bin", "undograft")


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="undograft-")
        self.run_dir = os.path.join(self.tmp, "run")
        self.undodir = os.path.join(self.tmp, "undo")
        os.makedirs(self.run_dir)
        os.makedirs(self.undodir)
        self.env = dict(os.environ, XDG_RUNTIME_DIR=self.run_dir, XDG_CACHE_HOME=os.path.join(self.tmp, "cache"))
        self.env.pop("NVIM", None)
        self.servers = []
        self.n = 0
        self.times = []

    def tearDown(self):
        if os.environ.get("UNDOGRAFT_TIMES"):
            print(" ".join("%s=%.0fms" % (e[:4], t * 1000) for e, t in self.times))
        for p, _ in self.servers:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def file(self, name, lines):
        path = os.path.join(self.tmp, name)
        self.write(path, lines)
        return path

    def write(self, path, lines, eol=True):
        with open(path, "w") as f:
            f.write("\n".join(lines) + ("\n" if eol else ""))

    def read(self, path):
        with open(path, "rb") as f:
            return f.read()

    def nvim(self, *files, opts="undofile autoread"):
        """A headless Neovim (a stand-in for the user's) with `files` loaded."""
        args = ["nvim", "--headless", "-u", "NONE", "-i", "NONE", "-n",
                "--cmd", "set %s undodir=%s" % (opts, self.undodir)]
        for f in files:
            args += ["-c", "edit " + f]
        p = subprocess.Popen(args, env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        sock = os.path.join(self.run_dir, "nvim.%d.0" % p.pid)
        for _ in range(200):
            if os.path.exists(sock) and self.lua_in(sock, "1") == 1:
                break
            time.sleep(0.02)
        self.servers.append((p, sock))
        return sock

    def lua_in(self, sock, code, *args):
        r = subprocess.run(["nvim", "--server", sock, "--remote-expr",
                            "json_encode(luaeval(%s, %s))" % (json.dumps(code), json.dumps(list(args)))],
                           env=self.env, capture_output=True, text=True, timeout=5)
        try:
            return json.loads(r.stdout)
        except ValueError:
            return None

    def lua(self, sock, code, *args):
        return self.lua_in(sock, "(function(...) %s end)(unpack(_A))" % code, *args)

    def lines(self, sock, path):
        return self.lua(sock, "return vim.api.nvim_buf_get_lines(vim.fn.bufnr(...), 0, -1, false)", path)

    def seq(self, sock, path):
        return self.lua(sock, "local b = vim.fn.bufnr(...) return vim.api.nvim_buf_call(b, function() "
                              "return vim.fn.undotree().seq_last end)", path)

    def modified(self, sock, path):
        return self.lua(sock, "return vim.bo[vim.fn.bufnr(...)].modified", path)

    def hook(self, event, path, tool="Edit", tool_use_id=None):
        self.n += 1
        data = {"hook_event_name": event, "tool_name": tool, "tool_input": {"file_path": path},
                "tool_use_id": tool_use_id or "toolu_%d" % self.n, "session_id": "s", "cwd": self.tmp}
        t = time.monotonic()
        r = subprocess.run([HOOK], input=json.dumps(data), env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertEqual(r.stderr, "", "hook wrote to stderr")
        t = time.monotonic() - t
        self.times.append((event, t))
        return t

    def ai_edit(self, path, lines, checktime_in=None, eol=True):
        """One AI tool call: Pre hook, the write (optionally with a Neovim
        noticing the change before Post), Post hook."""
        tid = "toolu_%d" % (self.n + 1)
        self.hook("PreToolUse", path, tool_use_id=tid)
        self.write(path, lines, eol=eol)
        st = os.stat(path)  # a distinct mtime per edit, without sleeping a second
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + (self.n + 1) * 1_000_000_000))
        written = self.read(path)
        if checktime_in:
            self.lua(checktime_in, "vim.cmd('checktime')")
        self.hook("PostToolUse", path, tool_use_id=tid)
        self.assertEqual(self.read(path), written, "the AI's bytes stay as written")

    def short_timeout(self, sock, path, ms):
        """Load the module into `sock` (a no-op tool call), then shorten its timeout."""
        self.hook("PreToolUse", path, tool_use_id="load")
        self.hook("PostToolUse", path, tool_use_id="load")
        self.lua(sock, "package.loaded.undograft.timeout = %d" % ms)

    def guards(self, sock):
        return self.lua(sock, "return #vim.api.nvim_get_autocmds({event = 'FileChangedShell'})")


class LoadedBuffer(Case):
    def test_edit_is_one_undo_step(self):
        path = self.file("a.txt", ["one", "two", "three", "four"])
        s = self.nvim(path)
        self.lua(s, "vim.api.nvim_win_set_cursor(0, {4, 0})")
        self.ai_edit(path, ["one", "TWO", "three", "four", "five"])
        self.assertEqual(self.lines(s, path), ["one", "TWO", "three", "four", "five"])
        self.assertEqual(self.seq(s, path), 1)
        self.assertFalse(self.modified(s, path))
        self.assertEqual(self.lua(s, "return vim.api.nvim_win_get_cursor(0)"), [4, 0], "cursor stays put")
        self.lua(s, "vim.cmd('undo')")
        self.assertEqual(self.lines(s, path), ["one", "two", "three", "four"])
        self.assertTrue(self.modified(s, path), "undone: differs from the file")

    def test_save_after_graft_does_not_warn(self):
        # Neovim must know the file as read after the edit, or :w asks
        # "the file has been changed since reading it".
        path = self.file("a.txt", ["a", "b"])
        s = self.nvim(path)
        self.ai_edit(path, ["a", "B"])
        self.lua(s, "vim.api.nvim_buf_set_lines(0, 0, 1, false, {'human'}) vim.api.nvim_input(':w<CR>')")
        self.assertEqual(self.lua(s, "return vim.api.nvim_get_mode().mode"), "n")
        with open(path) as f:
            self.assertEqual(f.read(), "human\nB\n")

    def test_steps_per_edit_and_no_reload_after(self):
        path = self.file("a.txt", ["a", "b", "c"])
        s = self.nvim(path)
        self.ai_edit(path, ["a", "B", "c"])
        self.ai_edit(path, ["a", "B", "c", "d"])
        self.ai_edit(path, ["A", "B", "c", "d"])
        self.assertEqual(self.seq(s, path), 3)
        self.lua(s, "vim.cmd('checktime')")  # nothing left to reload
        self.assertEqual(self.seq(s, path), 3)
        self.assertEqual(self.lua(s, "return vim.bo[vim.fn.bufnr(...)].autoread", path), None,
                         "back to the global 'autoread'")
        self.assertEqual(self.guards(s), 0)

    def test_own_local_autoread_is_kept(self):
        path = self.file("a.txt", ["a"])
        s = self.nvim(path)
        self.lua(s, "vim.bo.autoread = false")
        self.ai_edit(path, ["b"])
        self.assertIs(self.lua(s, "return vim.bo.autoread"), False)

    def test_neovim_noticing_first_does_not_reload(self):
        path = self.file("a.txt", ["a", "b", "c"])
        s = self.nvim(path)
        self.ai_edit(path, ["a", "x", "c"], checktime_in=s)
        self.assertEqual(self.lines(s, path), ["a", "x", "c"])
        self.assertEqual(self.seq(s, path), 1)
        self.lua(s, "vim.cmd('undo')")
        self.assertEqual(self.lines(s, path), ["a", "b", "c"])

    def test_big_file_keeps_history(self):
        # Above 'undoreload' (10000 lines) a plain reload clears the undo history.
        lines = ["line %d" % i for i in range(20000)]
        path = self.file("big.txt", lines)
        s = self.nvim(path)
        self.lua(s, "vim.api.nvim_buf_set_lines(0, 0, 1, false, {'human'}) vim.cmd('silent write')")
        lines[0] = "human"
        lines[15000] = "ai"
        self.ai_edit(path, lines)
        self.assertEqual(self.seq(s, path), 2)
        self.assertEqual(self.lines(s, path)[15000], "ai")

    def test_unsaved_human_edits_are_left_alone(self):
        path = self.file("a.txt", ["a", "b"])
        s = self.nvim(path)
        self.lua(s, "vim.api.nvim_buf_set_lines(0, 0, 1, false, {'mine'})")
        self.ai_edit(path, ["a", "b", "ai"])
        self.assertEqual(self.lines(s, path), ["mine", "b"])
        self.assertTrue(self.modified(s, path))

    def test_crlf_file(self):
        path = os.path.join(self.tmp, "dos.txt")
        with open(path, "wb") as f:
            f.write(b"a\r\nb\r\n")
        s = self.nvim(path)
        with open(path, "wb") as f:
            f.write(b"a\r\nB\r\nc\r\n")
        self.hook("PostToolUse", path)
        self.assertEqual(self.lines(s, path), ["a", "B", "c"])
        self.assertEqual(self.seq(s, path), 1)
        self.assertFalse(self.modified(s, path))

    def test_lost_final_newline_falls_back_to_reload(self):
        path = self.file("a.txt", ["a", "b"])
        s = self.nvim(path)
        self.ai_edit(path, ["a", "b", "c"], eol=False)
        self.assertEqual(self.lines(s, path), ["a", "b", "c"])
        self.assertFalse(self.lua(s, "return vim.bo[vim.fn.bufnr(...)].eol", path))
        self.assertFalse(self.modified(s, path))

    def test_guard_lifts_itself_without_post(self):
        path = self.file("a.txt", ["a"])
        s = self.nvim(path)
        self.lua(s, "vim.bo.autoread = true")
        self.short_timeout(s, path, 50)
        self.hook("PreToolUse", path)  # and no PostToolUse
        self.write(path, ["b"])
        time.sleep(0.3)
        self.lua(s, "vim.wait(10)")  # let the timer run
        self.assertEqual(self.lines(s, path), ["b"])
        self.assertEqual(self.seq(s, path), 1)
        self.assertEqual(self.guards(s), 0)
        self.assertIs(self.lua(s, "return vim.bo.autoread"), True)

    def test_change_noticed_without_post_is_synced(self):
        # The tool's PostToolUse never reaches this Neovim (denied, interrupted,
        # busy): the guard must not leave the buffer stale.
        path = self.file("a.txt", ["a", "b"])
        s = self.nvim(path)
        self.hook("PreToolUse", path)
        self.lua(s, "package.loaded.undograft.notice_delay = 10")
        self.write(path, ["a", "B"])
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        self.lua(s, "vim.cmd('checktime') vim.wait(100)")
        self.assertEqual(self.lines(s, path), ["a", "B"])
        self.assertEqual(self.seq(s, path), 1)
        self.assertFalse(self.modified(s, path))

    def test_lost_post_does_not_keep_the_guard(self):
        path = self.file("a.txt", ["a"])
        s = self.nvim(path)
        self.short_timeout(s, path, 300)
        self.hook("PreToolUse", path, tool_use_id="denied")  # and no PostToolUse
        self.ai_edit(path, ["b"])
        self.assertEqual(self.guards(s), 1, "the denied call still holds it")
        self.lua(s, "vim.wait(400)")
        self.assertEqual(self.guards(s), 0)
        self.assertEqual(self.lines(s, path), ["b"])

    def test_deleted_file_keeps_the_buffer(self):
        path = self.file("a.txt", ["keep", "me"])
        s = self.nvim(path)
        self.hook("PreToolUse", path, tool_use_id="t1")
        os.unlink(path)
        self.hook("PostToolUseFailure", path, tool_use_id="t1")
        self.assertEqual(self.lines(s, path), ["keep", "me"])
        self.assertEqual(self.guards(s), 0)
        self.assertFalse(os.path.exists(path))

    def test_same_text_rewrite_then_save_does_not_warn(self):
        path = self.file("a.txt", ["a", "b"])
        s = self.nvim(path)
        self.ai_edit(path, ["a", "b"])  # Write with the same text: only the mtime changes
        self.assertEqual(self.seq(s, path), 0)
        self.lua(s, "vim.api.nvim_buf_set_lines(0, 0, 1, false, {'human'}) vim.api.nvim_input(':w<CR>')")
        self.assertEqual(self.lua(s, "return vim.api.nvim_get_mode().mode"), "n")
        with open(path) as f:
            self.assertEqual(f.read(), "human\nb\n")

    def test_no_final_newline_is_kept(self):
        path = os.path.join(self.tmp, "noeol.txt")
        self.write(path, ["a", "b"], eol=False)
        s = self.nvim(path)
        self.ai_edit(path, ["a", "B"], eol=False)  # asserts the bytes on disk
        self.assertEqual(self.seq(s, path), 1)

    def test_unmodifiable_buffer_reloads(self):
        path = self.file("a.txt", ["a"])
        s = self.nvim(path)
        self.lua(s, "vim.bo.modifiable = false")
        self.ai_edit(path, ["b"])
        self.assertEqual(self.lines(s, path), ["b"])
        self.assertEqual(self.guards(s), 0)

    def test_two_edits_overlapping(self):
        # Parallel tool calls on one file: the first Post must not lift the
        # second call's guard.
        path = self.file("a.txt", ["a", "b"])
        s = self.nvim(path)
        self.hook("PreToolUse", path, tool_use_id="t1")
        self.hook("PreToolUse", path, tool_use_id="t2")
        self.write(path, ["A", "b"])
        self.hook("PostToolUse", path, tool_use_id="t1")
        self.assertEqual(self.guards(s), 1)
        self.write(path, ["A", "B"])
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        self.hook("PostToolUse", path, tool_use_id="t2")
        self.assertEqual(self.seq(s, path), 2)
        self.assertEqual(self.guards(s), 0)

    def test_undofile_written_alongside(self):
        path = self.file("a.txt", ["a", "b"])
        s = self.nvim(path)
        self.ai_edit(path, ["a", "c"])
        self.lua(s, "vim.cmd('bwipeout! ' .. vim.fn.bufnr(...))", path)
        s2 = self.nvim(path)
        self.assertEqual(self.seq(s2, path), 1)


class UnloadedFile(Case):
    def test_undofile_history_survives(self):
        open(os.path.join(self.run_dir, "nvim.1.0"), "w").close()  # left behind, pid alive: not a server
        path = self.file("a.txt", ["one", "two"])
        s = self.nvim(path)
        self.lua(s, "vim.api.nvim_buf_set_lines(0, 2, 2, false, {'three'}) vim.cmd('silent write')")
        self.lua(s, "vim.cmd('bwipeout')")  # still running: the hook learns 'undodir' from it
        self.ai_edit(path, ["one", "TWO", "three", "four"])
        s2 = self.nvim(path)
        self.assertEqual(self.seq(s2, path), 2)
        self.lua(s2, "vim.cmd('undo')")
        self.assertEqual(self.lines(s2, path), ["one", "two", "three"])
        self.lua(s2, "vim.cmd('undo')")
        self.assertEqual(self.lines(s2, path), ["one", "two"])
        self.assertEqual(os.listdir(os.path.join(self.run_dir, "undograft")), [], "snapshots cleaned up")

    def test_busy_neovim_is_not_taken_for_absent(self):
        path = self.file("a.txt", ["one"])
        s = self.nvim(path)
        self.lua(s, "vim.api.nvim_buf_set_lines(0, 1, 1, false, {'two'}) vim.cmd('silent write')")
        self.hook("PreToolUse", path, tool_use_id="warm")  # learns 'undodir'
        self.hook("PostToolUse", path, tool_use_id="warm")
        busy = subprocess.Popen(["nvim", "--server", s, "--remote-expr", "execute('sleep 2')"], env=self.env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.2)
        self.hook("PreToolUse", path, tool_use_id="t1")
        busy.wait()
        self.assertFalse(glob.glob(os.path.join(self.run_dir, "undograft", "*.snap*")))

    def test_no_undofile_no_snapshot(self):
        path = self.file("a.txt", ["x"])
        self.nvim()
        self.ai_edit(path, ["y"])
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, "undograft")))
        self.assertEqual(os.listdir(self.undodir), [])

    def test_no_neovim_running_is_quick_and_quiet(self):
        path = self.file("a.txt", ["x"])
        t = self.hook("PreToolUse", path, tool="Bash")
        self.assertLess(t, 0.5)
        os.makedirs(os.path.join(self.tmp, "cache"))
        with open(os.path.join(self.tmp, "cache", "undograft.json"), "w") as f:
            json.dump({"undodir": self.undodir, "undolevels": 1000}, f)
        self.assertLess(self.hook("PreToolUse", path), 0.5)


if __name__ == "__main__":
    unittest.main()
