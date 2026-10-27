#!/usr/bin/env python3
"""A GUI started with -a hosting the root's blame server (render.c, srv_take), on the
structure fixture as tests/serve.py sets it up. It opens windows, so only on a display
given for it: AIG_TEST_DISPLAY (a private Xvfb or Xephyr, never the inherited DISPLAY),
software GL, nvim sockets in a throwaway directory; the window is closed with xdotool.

- takeover: a headless server with a subscribed client; the GUI's window up with its first
  build held back (AIG_ENG_DELAY_FIRST): no handover meanwhile; once it has built, the
  headless exits 0, the client hears the handover, connects again and gets the same blame
  from the GUI;
- the GUI is the one server: a headless started then leaves at once;
- focus: the GUI shows the file, selects the turn and lights the line (its reply reads
  them back from the screen); an unknown turn or file is an error;
- a second -a GUI takes over; the first doesn't take the name back while it is held,
  and hosts again once the second quits;
- the GUI's exit frees the socket: its clients see EOF, a new headless binds;
- the gap: a server that comes back after its quit (as a headless an editor spawns in
  the gap would) is asked to quit again, and the GUI ends up serving;
- a holder that never quits is reported once, and the GUI binds once it lets go;
- a GUI without -a, or with -a -w, never touches the socket: the headless keeps serving.

  AIG_TEST_DISPLAY=:N tests/host.py [BINARY]   (exit 0 skipped without AIG_TEST_DISPLAY)
"""
import os, shutil, socket, subprocess, sys, threading, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serve  # noqa: E402  (its fixture, client and checks)
from serve import Client, check, gone, sock_name, spawn, wait_ok  # noqa: E402

DISPLAY = os.environ.get("AIG_TEST_DISPLAY")


def gui(root, env, ns, *args, delay=None):
    """A GUI on the test display; its stderr in the temp dir, checked like the servers'."""
    e = dict(env, AIG_SOCK_NS=ns, DISPLAY=DISPLAY, LIBGL_ALWAYS_SOFTWARE="1", GALLIUM_DRIVER="llvmpipe",
             AIG_NVIM_DIR=os.path.join(env["AIG_TEST_TMP"], "nv"), AIG_DEBUG="1",
             ASAN_OPTIONS="detect_leaks=0")  # the GL driver's own leaks
    if delay:
        e["AIG_ENG_DELAY_FIRST"] = str(delay)
    err = os.path.join(env["AIG_TEST_TMP"], "gui-%d.err" % len(serve.procs))
    with open(err, "w") as f:
        p = subprocess.Popen([serve.BIN, "--root", root, "-W", "900x600", *args], env=e, stdout=subprocess.DEVNULL,
                             stderr=f)
    serve.procs.append((p, err))
    return p, err


def close_window(p):
    """Q in the GUI's window, as a user quits it; True once it exited by itself."""
    env = dict(os.environ, DISPLAY=DISPLAY)
    for _ in range(50):
        w = subprocess.run(["xdotool", "search", "--pid", str(p.pid)], env=env, capture_output=True, text=True).stdout.split()
        if w:
            subprocess.run(["xdotool", "windowfocus", w[0]], env=env, capture_output=True)
            subprocess.run(["xdotool", "key", "--window", w[0], "q"], env=env, capture_output=True)
            if gone(p, 1.0):
                return True
        else:
            time.sleep(0.1)
    return False


def window_up(p, timeout=10):
    env = dict(os.environ, DISPLAY=DISPLAY)
    for _ in range(int(timeout * 10)):
        if subprocess.run(["xdotool", "search", "--pid", str(p.pid)], env=env, capture_output=True).stdout.strip():
            return True
        time.sleep(0.1)
    return False


def holder(ns, root):
    """The pid listening on the root's socket name (/proc/net/unix, then whose fd it is), None when free."""
    want = "@" + sock_name(ns, root)[1:]
    inode = None
    with open("/proc/net/unix") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 8 and parts[7] == want and parts[5] == "01":  # listening
                inode = parts[6]
    if not inode:
        return None
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            for fd in os.listdir("/proc/%s/fd" % pid):
                if os.readlink("/proc/%s/fd/%s" % (pid, fd)) == "socket:[%s]" % inode:
                    return int(pid)
        except OSError:
            pass
    return None


def until(cond, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return cond()


def built(err):
    with open(err, errors="replace") as f:
        return "an_build" in f.read()


def run(tmp):
    tag = "aighost%d" % os.getpid()
    repo, cfg, env, dump, want, keys = serve.fixture(tmp)
    os.makedirs(os.path.join(tmp, "nv"))
    ns = tag + "a"

    # Takeover, the GUI's build held up until we say.
    h1 = spawn(repo, env, ns, idle=60)
    c = Client(ns, repo)
    check("headless: subscribe", c.req("subscribe")["status"] == "ok")
    before = wait_ok(c, "src/app.py")
    g, gerr = gui(repo, env, ns, "-a", delay=5)
    check("the GUI's window is up", window_up(g))
    m = c.push("handover", timeout=3.0)
    check("its first build held back: no handover, the headless serves on", m is None and h1.poll() is None and
          not built(gerr) and c.req("blame", file="src/app.py")["status"] == "ok", m)
    h = c.push("handover", timeout=20)
    check("the GUI built, then took over: handover to the client", h is not None and built(gerr), h)
    check("the headless exits 0", gone(h1, 10) and h1.returncode == 0, h1.returncode)
    c.close()
    c = Client(ns, repo, wait=10)
    after = wait_ok(c, "src/app.py")
    check("the GUI answers the same blame", after["status"] == "ok" and
          all(after[k] == before[k] for k in ("text", "runs", "changes", "turns")), (after, before))
    check("the GUI hosts", c.req("focus", turn_key="x", file="x").get("status") == "error")
    h2 = spawn(repo, env, ns, idle=60)
    check("one server: a headless started now leaves at once", gone(h2, 5) and h2.returncode == 0, h2.returncode)

    # focus: the file and the turn shown, read back by the GUI.
    tkey = next(iter(after["turns"]))
    r = c.req("focus", turn_key=tkey, file="src/app.py", line=2)
    check("focus: the screen shows the file, the turn, the line", r["status"] == "ok" and r.get("host") == "gui" and
          r.get("shown") == {"file": "src/app.py", "turn_key": tkey, "line": 2}, r)
    other = [k for k in after["turns"] if k != tkey]
    r = c.req("focus", turn_key=other[0], file="src/lib.c", line=1)
    check("focus again: another file and turn shown", r.get("shown") == {"file": "src/lib.c", "turn_key": other[0],
          "line": 1}, r)
    r = c.req("focus", turn_key="0\x1fno such turn", file="src/app.py", line=1)
    check("focus on an unknown turn: an error", r["status"] == "error", r)
    r = c.req("focus", turn_key=tkey, file="nope.py", line=1)
    check("focus on an unknown file: an error", r["status"] == "error", r)

    # A second -a GUI: the newest wins; the first hosts again only once the name is free.
    check("the first GUI holds the name", holder(ns, repo) == g.pid, holder(ns, repo))
    g2, g2err = gui(repo, env, ns, "-a")
    h = c.push("handover", timeout=20)
    check("a second -a GUI takes over: handover to the first one's client", h is not None and
          until(lambda: holder(ns, repo) == g2.pid, 5), holder(ns, repo))
    c.close()
    time.sleep(7)  # past the first one's retake wait
    check("the first doesn't take the name back while it is held", holder(ns, repo) == g2.pid, holder(ns, repo))
    if shutil.which("xdotool"):
        check("the second GUI exits on Q", close_window(g2) and g2.returncode == 0, g2.returncode)
        check("the first hosts again once the name is free", until(lambda: holder(ns, repo) == g.pid, 12),
              holder(ns, repo))
    else:
        g2.kill()
    c = Client(ns, repo, wait=10)

    # The GUI quits: the socket is free, its clients see EOF (no handover), a headless binds.
    if not shutil.which("xdotool"):
        print("skip host: the GUI's exit (no xdotool to close its window)")
        g.kill()
    else:
        check("the GUI exits on Q", close_window(g) and g.returncode == 0, g.returncode)
        m = c.push("handover", timeout=2)
        check("its clients see EOF, no handover", m is None and c.eof, m)
        h3 = spawn(repo, env, ns, idle=60)
        c3 = Client(ns, repo, wait=5)
        check("after the GUI: a headless binds and serves", h3.poll() is None and wait_ok(c3, "src/app.py")["status"] == "ok")
        c3.s.sendall(b'{"cmd":"quit"}\n')
        check("that headless quits", gone(h3, 5) and h3.returncode == 0, h3.returncode)
        c3.close()
    c.close()

    # The gap: a server that answers quit by going and coming straight back.
    ns = tag + "b"
    quits = []

    def fake():
        name = sock_name(ns, repo)
        ls = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        ls.bind(name)
        ls.listen(4)
        for n in range(2):
            conn, _ = ls.accept()
            conn.settimeout(10)
            data = b""
            while b"\n" not in data:
                b = conn.recv(4096)
                if not b:
                    break
                data += b
            quits.append(data)
            ls.close()
            if n == 0:  # a fresh server in the gap: bound before the GUI hears the first one go
                ls = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                ls.bind(name)
                ls.listen(4)
            conn.close()

    t = threading.Thread(target=fake, daemon=True)
    t.start()
    time.sleep(0.2)
    g, gerr = gui(repo, env, ns, "-a")
    t.join(30)
    check("the gap: the server back in it was asked to quit again", len(quits) == 2 and
          all(b'"quit"' in q for q in quits), quits)
    c = Client(ns, repo, wait=10)
    r = c.req("focus", turn_key="x", file="x")
    check("the gap: the GUI ends up serving", r.get("status") == "error" and r.get("error") == "unknown turn_key", r)
    c.close()
    if shutil.which("xdotool"):
        check("that GUI exits on Q", close_window(g) and g.returncode == 0, g.returncode)
    else:
        g.kill()

    # A holder that never quits: reported once; the GUI binds once it lets go.
    ns = tag + "d"
    stop = threading.Event()
    asked = []

    def deaf():
        ls = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        ls.bind(sock_name(ns, repo))
        ls.listen(8)
        ls.settimeout(0.2)
        conns = []
        while not stop.is_set():
            try:
                conn, _ = ls.accept()
                conns.append(conn)
                asked.append(1)
            except socket.timeout:
                pass
        ls.close()
        for x in conns:
            x.close()

    t = threading.Thread(target=deaf, daemon=True)
    t.start()
    time.sleep(0.2)
    g, gerr = gui(repo, env, ns, "-a")

    def said():
        with open(gerr, errors="replace") as f:
            return f.read().count("doesn't quit")

    check("a holder that doesn't quit: said once", until(lambda: said() == 1, 15) and asked, (said(), len(asked)))
    time.sleep(6)
    check("still said once, asked again less often", said() == 1 and len(asked) <= 3, (said(), len(asked)))
    stop.set()
    t.join(5)
    check("it lets go: the GUI serves", until(lambda: holder(ns, repo) == g.pid, 70), holder(ns, repo))
    if shutil.which("xdotool"):
        check("that GUI exits on Q", close_window(g) and g.returncode == 0, g.returncode)
    else:
        g.kill()

    # Without -a: the socket is left alone.
    ns = tag + "c"
    h4 = spawn(repo, env, ns, idle=60)
    c = Client(ns, repo)
    check("subscribe", c.req("subscribe")["status"] == "ok")
    for args, what in (((), "without -a"), (("-a", "-w"), "with -a -w")):
        g, gerr = gui(repo, env, ns, *args)  # without -a: its default history (--since)
        window_up(g)
        time.sleep(3.0)
        check("a GUI %s: the headless serves on, no handover" % what, h4.poll() is None and
              c.push("handover", timeout=0.5) is None and wait_ok(c, "src/app.py")["status"] == "ok" and
              holder(ns, repo) == h4.pid)
        if shutil.which("xdotool"):
            check("that GUI exits on Q", close_window(g) and g.returncode == 0, g.returncode)
        else:
            g.kill()
    check("the headless still serves", wait_ok(c, "src/app.py")["status"] == "ok" and h4.poll() is None)
    c.s.sendall(b'{"cmd":"quit"}\n')
    check("the headless quits", gone(h4, 5) and h4.returncode == 0, h4.returncode)
    c.close()


if __name__ == "__main__":
    if not DISPLAY:
        print("skip host (set AIG_TEST_DISPLAY to a private X display)")
        sys.exit(0)
    serve.NAME = "host"
    serve.main(run)
