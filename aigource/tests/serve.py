#!/usr/bin/env python3
"""aigource --serve end to end, on the structure fixture (tests/fixtures/structure)
in a throwaway repo whose sessions are found the way a real repo's are
(CLAUDE_CONFIG_DIR/projects/<slug>). Each server gets its own socket prefix
(AIG_SOCK_NS) and a short idle grace where the case is about it (AIG_SERVE_IDLE).

- protocol: blame (ok, none), subscribe, saved, turn_lines, focus, quit; ids
  echoed, rev on every reply;
- blame runs equal --structure-dump's, text equals the file as the build read it;
- two servers started at once for one root: one serves, the other exits 0 at once;
- building: a reply before the first build (the history's read held up on a
  FIFO that find_projdirs opens while it vets a project dir: ingest.c);
- saved after editing a file: a rebuild, a `changed` push naming it, its new blame;
  saved and then a half-close (SHUT_WR): its reply, a rebuild;
- an id that isn't UTF-8 comes back as valid JSON;
- quit: every client gets the handover, the server exits 0; quit and close at once too;
- a root with no transcripts stays up and answers blame with every line unknown;
- idle: exits after the grace with no client, counted from launch and from the
  last disconnect.

  tests/serve.py [BINARY]   (default build/aigource)
"""
import hashlib, json, os, shutil, socket, subprocess, sys, tempfile, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
AIG = os.path.dirname(HERE)
BIN = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(AIG, "build", "aigource"))
FIX = os.path.join(HERE, "fixtures", "structure")
fails = 0
procs = []  # (process, its stderr's file)


NAME = "serve"  # what check() prints (tests/host.py's own)


def check(name, cond, detail=""):
    global fails
    if cond:
        print("ok   %s: %s" % (NAME, name))
    else:
        fails += 1
        print("FAIL %s: %s" % (NAME, name) + (": " + str(detail) if detail else ""))


def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_DATE="2026-10-14T09:00:00Z", GIT_COMMITTER_DATE="2026-10-14T09:00:00Z")
    subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                    "-c", "core.hooksPath=/dev/null", *args], check=True, env=env, capture_output=True)


def slug(root):
    return "".join(c if c.isalnum() else "-" for c in root)


def slot_of(key):
    """A turn's colour: FNV-1a 32 over its key's UTF-8 bytes, modulo the 8 colours (docs/STRUCTURE.md, 1)."""
    h = 2166136261
    for b in key.encode():
        h = (h ^ b) * 16777619 & 0xFFFFFFFF
    return h % 8


def sock_name(ns, root):
    return "\0" + ns + "/" + hashlib.sha1(root.encode()).hexdigest()[:16]


def spawn(root, env, ns, idle=None):
    e = dict(env, AIG_SOCK_NS=ns)
    if idle is not None:
        e["AIG_SERVE_IDLE"] = str(idle)
    err = os.path.join(env["AIG_TEST_TMP"], "serve-%d.err" % len(procs))
    with open(err, "w") as f:
        p = subprocess.Popen([BIN, "--serve", "--root", root], env=e, stdout=subprocess.DEVNULL, stderr=f)
    procs.append((p, err))
    return p


class Client:
    """One connection: requests by id, pushes collected on the side."""

    def __init__(self, ns, root, wait=5.0):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        until = time.time() + wait
        while True:
            try:
                self.s.connect(sock_name(ns, root))
                break
            except (ConnectionRefusedError, FileNotFoundError):
                if time.time() > until:
                    raise
                time.sleep(0.02)
        self.buf = b""
        self.next_id = 1
        self.pushes = []
        self.eof = False

    def _line(self, deadline):
        while b"\n" not in self.buf:
            left = deadline - time.time()
            if left <= 0:
                return None
            self.s.settimeout(left)
            try:
                b = self.s.recv(1 << 20)
            except socket.timeout:
                return None
            if not b:
                self.eof = True
                return None
            self.buf += b
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def req(self, cmd, timeout=10.0, **kw):
        rid = self.next_id
        self.next_id += 1
        self.s.sendall((json.dumps(dict(id=rid, cmd=cmd, **kw)) + "\n").encode())
        deadline = time.time() + timeout
        while True:
            m = self._line(deadline)
            if m is None:
                raise TimeoutError(cmd)
            if "event" in m:
                self.pushes.append(m)
            elif m.get("id") == rid:
                return m

    def push(self, event, timeout=10.0):
        deadline = time.time() + timeout
        while True:
            for i, m in enumerate(self.pushes):
                if m["event"] == event:
                    return self.pushes.pop(i)
            m = self._line(deadline)
            if m is None:
                return None
            if "event" in m:
                self.pushes.append(m)

    def close(self):
        self.s.close()


def wait_ok(c, file, timeout=15.0):
    """blame until the first build is in."""
    until = time.time() + timeout
    while True:
        r = c.req("blame", file=file)
        if r["status"] != "building" or time.time() > until:
            return r
        time.sleep(0.05)


def gone(p, timeout):
    try:
        p.wait(timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def main(run=None):
    tmp = tempfile.mkdtemp(prefix="aigs.")
    try:
        (run or globals()["run"])(tmp)
        # Every server exited by itself (quit, idle): under ASan its reports are in its stderr.
        for i, (p, err) in enumerate(procs):
            with open(err, errors="replace") as f:
                text = f.read()
            bad = [l for l in text.splitlines() if "Sanitizer" in l or "runtime error" in l]
            check("server %d: exited, no sanitizer report" % i, p.poll() is not None and not bad, bad[:5] or p.poll())
    finally:
        for p, _ in procs:
            if p.poll() is None:
                p.kill()
                p.wait()
        shutil.rmtree(tmp, ignore_errors=True)
    print("%s: %s" % (NAME, "all passed" if not fails else "%d failed" % fails))
    sys.exit(1 if fails else 0)


def fixture(tmp):
    """The structure fixture as a repo under tmp with its sessions where discovery finds them: the repo, the
    config dir, an environment for both, --structure-dump's snapshot, each file's blame, each turn's key."""
    cfg = os.path.join(tmp, "cfg")
    repo = os.path.realpath(os.path.join(tmp, "repo"))
    shutil.copytree(os.path.join(FIX, "repo", "before"), repo)
    with open(os.path.join(repo, "img.bin"), "wb") as f:
        f.write(b"bin\0ary")
    git(repo, "init", "-q")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    shutil.copytree(os.path.join(FIX, "repo", "after"), repo, dirs_exist_ok=True)
    proj = os.path.join(cfg, "projects", slug(repo))
    os.makedirs(os.path.join(proj, "st1", "subagents"))
    for f in ["st1.jsonl", "st2.jsonl", "st1/subagents/agent-sub1.jsonl"]:
        with open(os.path.join(FIX, f)) as src, open(os.path.join(proj, f), "w") as dst:
            dst.write(src.read().replace("/REPO", repo))
    shutil.copy(os.path.join(FIX, "st1", "subagents", "agent-sub1.meta.json"), os.path.join(proj, "st1", "subagents"))
    env = dict(os.environ, CLAUDE_CONFIG_DIR=cfg, AIG_TEST_TMP=tmp)
    env.pop("AIG_NO_DEFS", None)

    # What the build sees: --structure-dump over the same discovered sessions.
    dump = json.loads(subprocess.run([BIN, "--structure-dump", "--all", "--root", repo], env=env, check=True,
                                     capture_output=True).stdout)
    want = {}

    def walk(n, path):
        for k in n["kids"]:
            p = path + "/" + k["name"] if path else k["name"]
            if k["kind"] == "file":
                want[p] = k["blame"]
            elif k["kind"] == "dir":
                walk(k, p)

    walk(dump["tree"], "")
    keys = {}  # turn index -> its key, as analysis.c makes it
    for i, t in enumerate(dump["turns"]):
        import datetime
        ts = datetime.datetime.strptime(t["ts"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=datetime.timezone.utc)
        keys[i] = "%d\x1f%s" % (round(ts.timestamp() * 1e6), t["title"])
    check("fixture has blamed lines", any(o >= 0 for runs in want.values() for _, o in runs), want)
    return repo, cfg, env, dump, want, keys


def run(tmp):
    tag = "aigtest%d" % os.getpid()
    repo, cfg, env, dump, want, keys = fixture(tmp)

    # Two at once: one binds, the other leaves at once with 0.
    ns = tag + "a"
    a, b = spawn(repo, env, ns), spawn(repo, env, ns)
    time.sleep(1.0)
    alive = [p for p in (a, b) if p.poll() is None]
    loser = [p for p in (a, b) if p.poll() is not None]
    check("two spawns, one server", len(alive) == 1 and len(loser) == 1 and loser[0].returncode == 0,
          [p.returncode for p in (a, b)])
    srv = alive[0] if alive else a

    c = Client(ns, repo)
    r = wait_ok(c, "src/app.py")
    check("blame ok", r["status"] == "ok" and r["id"] == 1 and "rev" in r, r)
    rev = r["rev"]
    for path, runs in sorted(want.items()):
        r = c.req("blame", file=path)
        with open(os.path.join(repo, path), encoding="utf-8") as f:
            text = f.read()
        check("blame %s: runs as --structure-dump" % path, r["status"] == "ok" and r["runs"] == runs,
              (r.get("runs"), runs))
        check("blame %s: text as read" % path, r.get("text") == text)
        cids = {o for _, o in runs if o >= 0}
        check("blame %s: its changes" % path, set(map(int, r["changes"])) == cids, r["changes"])
        tk = {ch["turn_key"] for ch in r["changes"].values() if ch["turn_key"]}
        exp = {keys[dump["changes"][cid][0]] for cid in cids if dump["changes"][cid][0] >= 0}
        check("blame %s: turn keys" % path, tk == exp and set(r["turns"]) == tk, (tk, exp))
        for k, t in r["turns"].items():
            check("blame %s: turn %s" % (path, k.split("\x1f")[1]), t["prompt"] == k.split("\x1f", 1)[1] and
                  t["slot"] == slot_of(k) and abs(t["ts"] * 1e6 - int(k.split("\x1f")[0])) < 1000, t)
    r = c.req("blame", file=os.path.join(repo, "src", "lib.c"))
    check("blame by absolute path", r["status"] == "ok" and r["runs"] == want["src/lib.c"], r)
    r = c.req("blame", file="./src/../src/lib.c")
    check("blame by a path with . and ..", r["status"] == "ok" and r["runs"] == want["src/lib.c"], r)
    for f in ["img.bin", "nope.txt", "/etc/passwd", "../x", ".git/HEAD"]:
        r = c.req("blame", file=f)
        check("blame %s: none" % f, r["status"] == "none" and r["rev"] == rev, r)
    r = c.req("focus", turn_key="x", file="src/app.py", line=1)
    check("focus: headless", r.get("host") == "headless" and r["id"] == c.next_id - 1, r)
    r = c.req("nonsense")
    check("unknown cmd: an error", r["status"] == "error", r)
    c.s.sendall(b"not json\n")
    m = c._line(time.time() + 5)
    check("malformed line: an error, id null", m and m["status"] == "error" and m["id"] is None, m)
    # a request that never ends: read a slice per pass, dropped past 1 MB, the others answered meanwhile
    flood = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    flood.connect(sock_name(ns, repo))
    flood.settimeout(10)
    sent, dropped = 0, False
    try:
        while sent < 8 << 20:
            sent += flood.send(b"x" * 65536)
    except (BrokenPipeError, ConnectionResetError, socket.timeout):
        dropped = True
    if not dropped:
        try:
            dropped = flood.recv(16) == b""
        except (ConnectionResetError, socket.timeout):
            dropped = True
    flood.close()
    check("a line past 1 MB drops its client", dropped and sent < 8 << 20, sent)
    check("the others are answered meanwhile", c.req("blame", file="src/app.py")["status"] == "ok")

    # turn_lines: every file holding the turn's lines, other lines unknown.
    tkey = None
    for path, runs in want.items():
        for _, o in runs:
            if o >= 0 and dump["changes"][o][0] >= 0:
                tkey = keys[dump["changes"][o][0]]
    r = c.req("turn_lines", turn_key=tkey)
    exp = {}
    for path, runs in want.items():
        mine = [[n, o if o >= 0 and dump["changes"][o][0] >= 0 and keys[dump["changes"][o][0]] == tkey else -1]
                for n, o in runs]
        merged = []
        for n, o in mine:
            if merged and merged[-1][1] == o:
                merged[-1][0] += n
            else:
                merged.append([n, o])
        if any(o >= 0 for _, o in merged):
            exp[path] = merged
    got = {f["file"]: f["runs"] for f in r.get("files", [])}
    check("turn_lines", r["status"] == "ok" and got == exp and exp, (got, exp))
    r = c.req("turn_lines", turn_key="0\x1fno such turn")
    check("turn_lines of no turn: empty", r["status"] == "ok" and r["files"] == [], r)

    # detail: one line's change in full, at the blame's rev
    rev = drev = c.req("blame", file="src/app.py")["rev"]
    with open(os.path.join(cfg, "projects", slug(repo), "st1.jsonl")) as f:
        prompts = [json.loads(l)["message"]["content"] for l in f if '"origin"' in l]
    for path, runs in sorted(want.items()):
        at = 1
        for n, o in runs:
            if o >= 0:
                break
            at += n
        if o < 0:
            continue
        ch = dump["changes"][o]
        r = c.req("detail", file=path, line=at, rev=rev)
        t = dump["turns"][ch[0]] if ch[0] >= 0 else None
        note = ch[1] if ch[1] >= 0 else -1 - ch[1]
        check("detail %s:%d: its change" % (path, at), r["status"] == "ok" and r["cid"] == o and r["tool"] == ch[2] and
              r["ts"] == ch[7] and r["turn_key"] == (keys[ch[0]] if t else None), r)
        full = [p for p in prompts if isinstance(p, str) and p.startswith(t["title"])] if t else []
        check("detail %s:%d: the turn's whole prompt" % (path, at), t and r["prompt"] in full + [t["title"]] and
              (not full or r["prompt"] == full[0]), (r.get("prompt"), full))
        check("detail %s:%d: the comment" % (path, at), r["comment"] == (dump["comments"][note] or None) and
              r["prompt_truncated"] is False and r["comment_truncated"] is False, r["comment"])
        lines = [l for h in r["hunks"] for l in h["lines"]]
        check("detail %s:%d: its patch" % (path, at), r["hunks"] and r["truncated"] is False and
              all(l[:1] in "+- " for l in lines) and all({"old_start", "old_lines", "new_start", "new_lines"} <= set(h)
                                                         for h in r["hunks"]), r["hunks"])
    unknown = next((p, sum(n for n, _ in runs[:k]) + 1) for p, runs in sorted(want.items())
                   for k, (n, o) in enumerate(runs) if o < 0)
    r = c.req("detail", file=unknown[0], line=unknown[1], rev=rev)
    check("detail of an unknown line: none", r["status"] == "none", r)
    for f, l in (("img.bin", 1), ("nope.txt", 1), ("src/app.py", 0), ("src/app.py", 10 ** 6)):
        r = c.req("detail", file=f, line=l, rev=rev)
        check("detail %s:%d: none" % (f, l), r["status"] == "none", r)
    r = c.req("detail", file="src/app.py", line=1, rev=rev + 1)
    check("detail at another rev: stale", r["status"] == "stale" and r["rev"] == rev, r)
    r = c.req("detail", file="src/app.py", line=1)
    check("detail with no rev: stale", r["status"] == "stale", r)

    # saved: an edit on disk (an AI line gone), a rebuild, the push, the new blame.
    c2 = Client(ns, repo)
    check("subscribe", c2.req("subscribe")["status"] == "ok")
    path = next(p for p, runs in sorted(want.items()) if sum(n for n, o in runs if o >= 0) >= 2)
    full = os.path.join(repo, path)
    with open(full, encoding="utf-8") as f:
        lines = f.read().splitlines(True)
    first_ai = 0
    for n, o in want[path]:
        if o >= 0:
            break
        first_ai += n
    del lines[first_ai]
    with open(full, "w", encoding="utf-8") as f:
        f.write("".join(lines))
    t0 = time.time()
    check("saved", c.req("saved", file=path)["status"] == "ok")
    m = c2.push("changed", timeout=10)
    took = time.time() - t0
    check("changed push after saved names %s" % path, m and path in m["files"] and m["rev"] > rev, m)
    check("changed push within the cadence (%.1f s)" % took, m and took < 5)
    r = c.req("blame", file=path)
    check("blame after saved: new text and rev", r["status"] == "ok" and r["text"] == "".join(lines) and
          r["rev"] > rev, r)
    check("blame after saved: one AI line fewer",
          sum(n for n, o in r["runs"] if o >= 0) == sum(n for n, o in want[path] if o >= 0) - 1, r["runs"])
    check("no push for a client that didn't subscribe", not c.pushes, c.pushes)
    # saved with nothing that changes a line's turn: a rebuild, no push
    rev2 = r["rev"]
    c.req("saved", file="README.md")
    time.sleep(2.5)
    r = c.req("blame", file="README.md")
    check("saved, nothing changed: rebuilt, no push", r["rev"] > rev2 and c2.push("changed", timeout=0.3) is None, r)
    # detail at an older rev: answered while the file reads the same since, stale once it changed
    other = next(p for p, runs in sorted(want.items()) if p != path and any(o >= 0 for _, o in runs))
    oline = 1 + sum(n for n, o in want[other][:next(k for k, (n, o) in enumerate(want[other]) if o >= 0)])
    r = c.req("detail", file=other, line=oline, rev=drev)
    check("detail at an older rev of a file unchanged since: answered", r["status"] == "ok" and r["rev"] > drev, r)
    r = c.req("detail", file=path, line=1, rev=drev)
    check("detail at an older rev of a file changed since: stale", r["status"] == "stale", r)
    # saved, then the client shuts its side: the request still counts, its reply comes, then EOF
    rev3 = r["rev"]
    h = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    h.connect(sock_name(ns, repo))
    h.sendall(b'{"id":7,"cmd":"saved","file":"README.md"}\n')
    h.shutdown(socket.SHUT_WR)
    h.settimeout(5)
    got = b""
    while True:
        b = h.recv(4096)
        if not b:
            break
        got += b
    h.close()
    rr = json.loads(got.split(b"\n")[0]) if got else None
    check("saved, then SHUT_WR: its reply, then EOF", rr and rr["id"] == 7 and rr["status"] == "ok", got)
    time.sleep(2.5)
    r = c.req("blame", file="README.md")
    check("saved, then SHUT_WR: rebuilt", r["rev"] > rev3, r)
    # an id that isn't UTF-8 comes back as valid JSON, U+FFFD in its place
    c.s.sendall(b'{"id":"\xffa","cmd":"focus"}\n')
    try:
        m = c._line(time.time() + 5)
    except ValueError as e:
        m = e
    check("id with invalid UTF-8: valid JSON back", isinstance(m, dict) and m["id"] == "\ufffda", m)

    # quit: every client hears the handover, then the server is gone.
    c.s.sendall(b'{"id":99,"cmd":"quit"}\n')
    h1, h2 = c.push("handover", timeout=5), c2.push("handover", timeout=5)
    check("quit: handover to every client", h1 and h2, (h1, h2))
    check("quit: server exits 0", gone(srv, 5) and srv.returncode == 0, srv.returncode)
    c.close(), c2.close()

    # building: the history's read held up (the FIFO), blame says so, then the build lands.
    ns = tag + "b"
    fifo_dir = os.path.join(cfg, "projects", slug(repo) + "-zz")
    os.makedirs(fifo_dir)
    fifo = os.path.join(fifo_dir, "held.jsonl")
    os.mkfifo(fifo)
    p = spawn(repo, env, ns, idle=1)
    c = Client(ns, repo)
    r = c.req("blame", file="src/app.py")
    check("blame before the first build: building, rev 0", r["status"] == "building" and r["rev"] == 0, r)
    r = c.req("turn_lines", turn_key=tkey)
    check("turn_lines before the first build: building", r["status"] == "building", r)
    sub = Client(ns, repo)
    sub.req("subscribe")
    with open(fifo, "w"):
        pass  # the reader sees EOF: no cwd, the dir is rejected, the read goes on
    r = wait_ok(c, "src/app.py")
    check("blame after the first build: ok", r["status"] == "ok" and r["rev"] >= 1, r)
    m = sub.push("changed", timeout=5)
    check("first build: changed push names every file", m and set(m["files"]) == set(want), m)
    sub.close()
    check("one client gone, one left: up past the grace", not gone(p, 2.0))
    c.s.sendall(b'{"cmd":"quit"}\n')
    check("quit with no id: handover", c.push("handover", timeout=5) is not None)
    check("quit: exits 0", gone(p, 5) and p.returncode == 0, p.returncode)
    c.close()
    shutil.rmtree(fifo_dir)

    # detail's caps: a 10 KB prompt, a 9 KB comment, a 500-line file written whole.
    big = os.path.realpath(os.path.join(tmp, "big"))
    os.makedirs(big)
    body = "".join("line %d \u00e9\n" % i for i in range(500))
    with open(os.path.join(big, "big.py"), "w") as f:
        f.write(body)
    # pasted (its tag isn't the prompt's), 100 KB: past the 64 KB read, so from the record's start
    prompt = '<pasted_content index="1">Write big.py\n' + "\u00e9" * 50000
    said = "Writing it. " + "\u00e9" * 4600
    recs = [
        {"type": "user", "timestamp": "2026-10-14T12:00:00.000Z", "cwd": big, "origin": {"kind": "human"},
         "message": {"role": "user", "content": prompt}},
        {"type": "assistant", "timestamp": "2026-10-14T12:00:05.000Z", "cwd": big, "message": {
            "role": "assistant", "id": "b1", "content": [{"type": "text", "text": said}, {
                "type": "tool_use", "id": "wb", "name": "Write",
                "input": {"file_path": big + "/big.py", "content": body}}]}},
        {"type": "user", "timestamp": "2026-10-14T12:00:06.000Z", "cwd": big, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "wb", "is_error": False, "content": "ok"}]},
         "toolUseResult": {"type": "create", "filePath": big + "/big.py", "content": body, "structuredPatch": []}},
    ]
    bproj = os.path.join(cfg, "projects", slug(big))
    os.makedirs(bproj)
    with open(os.path.join(bproj, "b1.jsonl"), "w") as f:
        f.write("".join(json.dumps(x, separators=(",", ":")) + "\n" for x in recs))
    ns = tag + "e"
    p = spawn(big, env, ns, idle=30)
    c = Client(ns, big)
    b = wait_ok(c, "big.py")
    r = c.req("detail", file="big.py", line=1, rev=b["rev"])
    nlines = sum(len(h["lines"]) for h in r.get("hunks", []))
    whole = prompt.split(">", 1)[1]
    check("detail caps: the prompt to 8 KB at a character, the comment as the snapshot keeps it", r["status"] == "ok" and
          len(r["prompt"].encode()) <= 8192 and whole.startswith(r["prompt"]) and len(r["prompt"]) > 4000 and
          r["prompt_truncated"] is True and r["comment_truncated"] is False and
          len(r["comment"].encode()) <= 8192 and r["comment"].startswith("Writing it."),  # as the snapshot keeps it (900 characters)
          (len(r.get("prompt", "")), r.get("comment", "")[:40]))
    check("detail caps: 400 patch lines, truncated", nlines == 400 and r["truncated"] is True and
          r["hunks"][0]["new_lines"] == 500 and r["hunks"][0]["lines"][0] == "+line 0 \u00e9", (nlines, r.get("truncated")))
    # the transcript rewritten before the next build: another prompt where this one was, so its title instead
    recs[0]["message"]["content"] = "Another prompt entirely"
    with open(os.path.join(bproj, "b1.jsonl"), "w") as f:
        f.write("".join(json.dumps(x, separators=(",", ":")) + "\n" for x in recs))
    r = c.req("detail", file="big.py", line=1, rev=b["rev"])
    check("detail after a rewrite: the turn's title, not the prompt now there", r["status"] == "ok" and
          r["prompt"] == "Write big.py" and r["prompt_truncated"] is False, r.get("prompt"))
    c.s.sendall(b'{"cmd":"quit"}\n')
    check("that server quits", gone(p, 5) and p.returncode == 0, p.returncode)
    c.close()

    # said: a change's comment in a line (tests/fixtures/comments): the first readable line, code fences and
    # markdown marks skipped, cut at 200 bytes between characters; null with no comment.
    sys.path.insert(0, HERE)
    import comment_parity
    crepo, cenv = comment_parity.setup(os.path.join(tmp, "comments"))
    cenv["AIG_TEST_TMP"] = tmp
    ns = tag + "f"
    p = spawn(crepo, cenv, ns, idle=30)
    c = Client(ns, crepo)
    wait_ok(c, "src/a.py")
    want_said = {"a.py": "Writing all three files.", "b.py": "Writing all three files.", "c.py": "Writing all three files.",
                 "d.py": "Fixing the cut", "f.py": "-1 means unknown, `**kw` stays", "g.py": "#include <x.h> is needed", "h.py": "Heading here", "e.py": "a" + "\u00e9" * 99, "r1.py": "Round one edit.", "r2.py": None,
                 "r3.py": "Round two, with words."}
    for f, w in sorted(want_said.items()):
        got = [ch.get("said") for ch in c.req("blame", file="src/" + f)["changes"].values()]
        check("said for src/%s" % f, got == [w], got)
        if f == "e.py":  # 301 bytes of text: 200 would end inside an é
            check("said cut at 200 bytes, between characters", len(got[0].encode()) == 199, len(got[0].encode()))
    c.s.sendall(b'{"cmd":"quit"}\n')
    check("that server quits", gone(p, 5) and p.returncode == 0, p.returncode)
    c.close()

    # A root with no sessions: up, every line unknown.
    bare = os.path.realpath(os.path.join(tmp, "bare"))
    os.makedirs(bare)
    with open(os.path.join(bare, "a.txt"), "w") as f:
        f.write("one\ntwo\n")
    git(bare, "init", "-q")
    git(bare, "add", "-A")
    ns = tag + "c"
    p = spawn(bare, env, ns, idle=30)
    c = Client(ns, bare)
    r = wait_ok(c, "a.txt")
    check("no transcripts: blame ok, all unknown", r["status"] == "ok" and r["runs"] == [[2, -1]] and
          r["changes"] == {} and r["turns"] == {}, r)
    check("no transcripts: still up", p.poll() is None)
    c.s.sendall(b'{"cmd":"quit"}\n')
    c.close()  # at once: the quit read with the EOF still counts
    check("quit, then close: exits 0", gone(p, 5) and p.returncode == 0, p.returncode)

    # Idle: gone after the grace with nobody connected; a client holds it up.
    ns = tag + "d"
    p = spawn(bare, env, ns, idle=1)
    check("idle: exits 0 with no client", gone(p, 6) and p.returncode == 0, p.returncode)
    p = spawn(bare, env, ns, idle=1.5)
    c = Client(ns, bare)
    check("idle: a client keeps it up past the grace", not gone(p, 3.0))
    c.close()
    check("idle: counted from the disconnect, not launch", not gone(p, 0.7))
    check("idle: exits 0 after the grace from the last disconnect", gone(p, 5) and p.returncode == 0, p.returncode)


if __name__ == "__main__":
    main()
