#!/usr/bin/env python3
"""ctx_reload against fresh loads at split points (tests/ctx_reload.c).

  tests/context_reload.py --fixture BIN
      every fixture transcript: a reload after each line (each response's
      records arriving across reloads), a cut inside each line, and seeded
      random multi-cut sequences; then the fixed cases (CASES below): files
      edited between reloads, each with the reasons its reloads must fall back
      to a full load for (none for a case that must stay incremental). Run by
      tests/run.sh, plain and under ASan.
  tests/context_reload.py --transcript PATH [--root ROOT] [--plans N] [--bin BIN]
      a real transcript, by hand: its project dir copied (live sessions keep
      writing), then N random sequences of cuts (some inside a line) in a
      copy of it, each reload checked against a fresh load. Prints each
      reload's time and whether it had to load in full.
  tests/context_reload.py --transcript PATH --requests N [...]
      the same, as a live agent grows: one sequence cut just before each of
      its last N requests and just after the first record of each one's
      response; prints how many reloads were incremental and their times.
Fails on the first difference.
"""
import json, os, random, re, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
AIG = os.path.dirname(HERE)
args = sys.argv[1:]
sys.path.insert(0, HERE)
import make_context_fixture as mk  # its record builders: the cases write small transcripts of their own


def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default


def run(binary, root, src, dst, cuts, env):
    r = subprocess.run([binary, root, src, dst] + [str(c) for c in cuts], env=env, capture_output=True, text=True)
    reloads = [line for line in r.stderr.splitlines() if line.startswith("ctx_reload")]
    full = [line.split("full load: ", 1)[1] for line in reloads if "full load: " in line]
    return r.returncode, r.stdout.strip(), reloads, full


# ---- the fixed cases: a process per case (ctx_reload --cmd), the files edited between its loads ----

# Why a reload falls back to a full load: the strings of src/context.c.
WHY = dict(
    replaced="the transcript was replaced", shrank="the transcript shrank", rewritten="the transcript was rewritten",
    window="the transcript was rewritten inside the window", nonl="a last line without its newline",
    srcs="a file a message was found in changed", root="another root, home or config",
    missing="messages found nowhere (another file may hold them now)",
    unresolved="a message a call missed appeared", reqid="a request id seen before",
    shapechg="a request shape defined again, changed", shape_late="a request shape a call looked up appeared",
    blobrep="a message written again as a blob, differently", rewrote="a message written again, differently",
    oldonly="a message only an earlier part of the transcript holds", oldresp="a response from before the window grew",
    usedgrew="a response items were weighed by grew", tailseen="a response a call looked up appeared",
    blockmiss="a block a call looked up appeared")
CASES = []


def case(*why):
    """A fixed case: the reasons its reloads fall back for, in order (none: all incremental)."""
    def deco(fn):
        CASES.append((fn.__name__, [WHY[w] for w in why], fn))
        return fn
    return deco


class Case:
    def __init__(self, binary, env, tmp):
        self.d = os.path.join(tmp, "case")
        shutil.rmtree(self.d, ignore_errors=True)
        shutil.copytree(os.path.join(AIG, "tests", "fixtures", "context", "proj"), self.d)
        self.path = os.path.join(self.d, "s1.jsonl")
        self.err = tempfile.TemporaryFile("w+")
        self.p = subprocess.Popen([binary, "--cmd", "/REPO", self.path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=self.err, env=env, text=True)

    def cmd(self, line):
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()
        reply = self.p.stdout.readline().strip()
        if reply != "ok":
            raise AssertionError(f"{line}: {reply + self.p.stdout.read() if reply else 'the process ended'}")

    load = lambda self: self.cmd("load")
    reload = lambda self: self.cmd("reload")

    def write(self, data, rel=None):  # in place: the same file, truncated
        with open(os.path.join(self.d, rel) if rel else self.path, "wb") as f:
            f.write(data)

    def append(self, data, rel=None):
        with open(os.path.join(self.d, rel) if rel else self.path, "ab") as f:
            f.write(data)

    def replace(self, data):  # another file under the name
        with open(self.path + ".new", "wb") as f:
            f.write(data)
        os.rename(self.path + ".new", self.path)

    def close(self):
        """The reasons the reloads fell back for."""
        self.p.stdin.close()
        self.p.wait()
        self.err.seek(0)
        return [ln.split("full load: ", 1)[1].strip() for ln in self.err.read().splitlines()
                if ln.startswith("ctx_reload") and "full load: " in ln]


def run_cases(binary, env, tmp):
    fulls = {}
    for name, want, fn in CASES:
        c = Case(binary, env, tmp)
        try:
            fn(c)
        except AssertionError as e:
            c.p.kill()
            print(f"FAIL context reload case {name}: {e}")
            sys.exit(1)
        got = c.close()
        if c.p.returncode or got != want:
            print(f"FAIL context reload case {name}: falls back for {got or 'nothing'}, wanted {want or 'nothing'}"
                  f" (exit {c.p.returncode})")
            sys.exit(1)
        for w in got:
            fulls[w] = fulls.get(w, 0) + 1
    if not fulls:
        print("FAIL context reload cases: no case fell back to a full load")
        sys.exit(1)
    return len(CASES), fulls


def jl(*recs):
    return b"".join((json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n").encode() for r in recs)


def fixture_lines(rel="s1.jsonl"):
    with open(os.path.join(AIG, "tests", "fixtures", "context", "proj", rel), "rb") as f:
        return f.read().splitlines(keepends=True)


S1 = fixture_lines()


def s1(a=0, b=None):  # lines a..b-1 of s1.jsonl
    return b"".join(S1[a:b])


def rev(v):  # every object's keys in reverse order: the same message written differently
    if isinstance(v, dict):
        return {k: rev(v[k]) for k in reversed(list(v))}
    return [rev(x) for x in v] if isinstance(v, list) else v


# The file's own guards: it is not the file, or not the bytes, the last load saw.
@case("replaced")
def replaced(c):  # another file under the name: same content, longer
    c.write(s1(0, 40)), c.load()
    c.replace(s1(0, 44)), c.reload()


@case("shrank")
def shrank(c):  # then regrown: the reload after the full load is incremental again
    c.write(s1(0, 30)), c.load()
    c.write(s1(0, 10)), c.reload()
    c.append(s1(10, 20)), c.reload()


@case("rewritten")
def rewritten_tail(c):  # the last 60 bytes changed in place
    c.write(s1(0, 20)), c.load()
    data = bytearray(s1(0, 20))
    data[-60:-57] = b"zzz"
    c.write(bytes(data)), c.append(s1(20, 24)), c.reload()


@case("rewritten")
def regrown_other(c):  # truncated and grown past its size with other content
    c.write(s1(0, 20)), c.load()
    c.write(b"".join(fixture_lines("s3.jsonl"))[:len(s1()) + 500]), c.reload()


@case("window")
def rewritten_window(c):  # the prompt a call sent, in the window and built into an item, changed in place; then a call
    u1, u2 = mk.user("hi"), mk.user("again")
    done = mk.asst("mA", "r1", "req1", [{"type": "text", "text": "Done."}], mk.usage(3, 0, 900, 100, 60))
    shape = {"type": "api-request-shape", "shapeHash": "sh1", "timestamp": mk.ts(), "shape": mk.SHAPE}
    data = jl(shape, u1, mk.req("r1", [mk.H(u1["message"])]), *done)
    c.write(data), c.load()
    c.write(data.replace(b'"hi"', b'"ho"'))
    c.append(jl(u2, mk.req("r2", [mk.H(mk.resp_msg(done)), mk.H(u2["message"])], base="r1", keep=1))), c.reload()


@case("nonl")
def no_newline(c):  # the last line completes without its newline: a full load counts it, a resume would not
    c.write(s1(0, 20)[:-5]), c.load()
    c.append(s1(0, 20)[-5:-1]), c.reload()
    c.append(s1(0, 24)[len(s1(0, 20)) - 1:]), c.reload()


@case("srcs")
def source_shrank(c):  # a message was found in s2.jsonl, which lost its end
    c.write(s1(0, 44)), c.load()
    c.write(b"".join(fixture_lines("s2.jsonl"))[:100], "s2.jsonl")
    c.append(s1(44, 46)), c.reload()


@case("srcs")
def source_removed(c):
    c.write(s1(0, 44)), c.load()
    os.remove(os.path.join(c.d, "s2.jsonl"))
    c.append(s1(44, 46)), c.reload()


@case("root", "root")
def root_changed(c):  # labels depend on the home and the config dir
    c.write(s1(0, 30)), c.load()
    c.cmd("env HOME /home/other"), c.append(s1(30, 34)), c.reload()
    c.cmd("env CLAUDE_CONFIG_DIR /home/other/.claude"), c.append(s1(34, 38)), c.reload()


@case("missing")
def missing(c):  # a prompt in no transcript (another file may hold it by now)
    c.write(s1(0, 1) + s1(2, 20)), c.load()
    c.append(s1(20, 24)), c.reload()


@case("unresolved")
def unresolved(c):  # s2's first line, a message s1 found in s2, is now in s1
    c.write(s1()), c.load()
    c.append(fixture_lines("s2.jsonl")[0]), c.reload()


# What appended records change in what was built.
@case("reqid")
def request_again(c):  # a request id taken before, appended
    c.write(s1()), c.load()
    c.append(S1[45]), c.reload()


@case("shapechg")
def shape_redefined(c):
    c.write(s1()), c.load()
    shape = json.loads(S1[0])
    shape["shape"]["system"][0]["text"] = "You are another Claude."
    c.append(jl(shape)), c.reload()


@case("shape_late")
def shape_late(c):  # a request shape written after the call that needed it: it took none
    c.write(S1[1] + S1[2] + S1[3]), c.load()
    c.append(b"".join(S1[4:11]) + S1[0] + s1(11, 13)), c.reload()


@case()
def request_early(c):  # a request before the messages it follows: its results are linked again
    order = list(range(48))
    order[11:17] = [16] + order[11:16]
    lines = [S1[i] for i in order]
    c.write(b"".join(lines[:18])), c.load()
    c.append(b"".join(lines[18:23])), c.reload()


@case("oldonly")
def swapped_blob(c):  # a request before the blob it needs: a message only the earlier part of the file held
    lines = S1[:8] + [S1[12]] + S1[9:12] + [S1[8]] + S1[13:]
    c.write(b"".join(lines[:13])), c.load()
    c.append(b"".join(lines[13:])), c.reload()


@case("oldresp")
def swapped_response(c):  # a response before its request: its records grow after it left the window
    lines = S1[:8] + [S1[9], S1[8]] + S1[10:]
    c.write(b"".join(lines[:10])), c.load()
    c.append(b"".join(lines[10:])), c.reload()


def blob_reordered():  # s1's Edit result blob, a block's keys the other way round
    blob = json.loads(S1[12])
    blob["message"]["content"][0] = rev(blob["message"]["content"][0])
    return jl(blob)


@case("blobrep")
def blob_again_looked_up(c):  # the blob is in the window: its hash was looked up
    c.write(s1(0, 14)), c.load()
    c.append(blob_reordered()), c.reload()


@case("blobrep")
def blob_again_old(c):  # the blob is before the window
    c.write(s1()), c.load()
    c.append(blob_reordered()), c.reload()


def first_answer(extra=()):  # (before, answer): two requests, the first one's response before the window, never looked up
    inp = {"file_path": "/REPO/src/a.c", "old_string": "x", "new_string": "y", "meta": {"a": 1, "b": [{"p": 1, "q": 2}]}}
    u1 = mk.user("Fix the bug in src/a.c")
    answer = mk.asst("mR", "r1", "req1", [{"type": "text", "text": "Editing."},
                                          {"type": "tool_use", "id": "tE", "name": "Edit", "input": inp}], mk.usage(3, 0, 900, 100, 60))
    other = mk.asst("mS", "r2", "req2", [{"type": "text", "text": "Other."}], mk.usage(3, 0, 900, 100, 60))
    shape = {"type": "api-request-shape", "shapeHash": "sh1", "timestamp": mk.ts(), "shape": mk.SHAPE}
    h1 = mk.H(u1["message"])
    return (jl(shape, u1, mk.req("r1", [h1]), *answer, mk.req("r2", [h1]), *other, mk.req("r3", [h1])), answer,
            mk.H(mk.resp_msg(answer)))


@case("rewrote")
def answer_written_again(c):  # a response before the window written again, its keys the other way round, then sent back
    before, answer, h = first_answer()
    again = json.loads(json.dumps(answer))
    for r in again:
        r["message"]["id"], r["message"]["content"] = "mRb", rev(r["message"]["content"])
    c.write(before), c.load()
    c.append(jl(*again, mk.req("r4", [h]))), c.reload()


@case()
def prompt_written_again(c):  # the same, a prompt typed twice and sent again: written the same, nothing differs
    c.write(s1()), c.load()
    c.append(jl(json.loads(S1[1]), mk.req("r9", [json.loads(S1[2])["tail"][0]]))), c.reload()


def result_again(c, reorder):  # a result a call sent, before the window, written again (its keys reversed or not), sent again
    q = mk.user([{"type": "tool_result", "tool_use_id": "tZ", "content": [{"type": "document", "z": 1, "a": {"y": 1, "b": 2}}]}])
    h = mk.H(q["message"])
    a1 = mk.asst("mQ1", "r8", "rq8", [{"type": "text", "text": "ok one"}], mk.usage(3, 0, 900, 100, 60))
    p1 = mk.user("pad1")
    a2 = mk.asst("mQ2", "r8b", "rq8b", [{"type": "text", "text": "ok two"}], mk.usage(3, 0, 900, 100, 60))
    u = mk.user("something else")
    c.write(s1() + jl(q, mk.req("r8", [h]), *a1, p1, mk.req("r8b", [mk.H(p1["message"])]), *a2)), c.load()
    q2 = json.loads(json.dumps(q))
    if reorder:
        q2["message"]["content"] = rev(q2["message"]["content"])
    c.append(jl(q2, u, mk.req("r9", [mk.H(u["message"])]), mk.req("r10", [h]))), c.reload()


@case("rewrote")
def result_written_again(c):
    result_again(c, True)


@case()
def result_written_again_same(c):
    result_again(c, False)


def two_turns():  # (before, u2, response): a call sent a response back that was found as a blob, the response not written yet
    u1 = mk.user("Fix the bug in src/a.c")
    resp = mk.asst("m1", "r1", "req1", [
        {"type": "thinking", "thinking": "", "signature": "c2lnbmF0dXJl" * 8}, {"type": "text", "text": "I'll read it."},
        {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/REPO/src/a.c"}}], mk.usage(3, 0, 900, 100, 60))
    u2 = mk.user([{"type": "tool_result", "tool_use_id": "t1", "content": "     1→int a;\n"}])
    shape = {"type": "api-request-shape", "shapeHash": "sh1", "timestamp": mk.ts(), "shape": mk.SHAPE}
    return [shape, u1, mk.req("r1", [mk.H(u1["message"])])], u2, resp


@case("tailseen")
def response_looked_up(c):  # the response arrives after a call found its message as a blob
    head, u2, resp = two_turns()
    msg = mk.resp_msg(resp)
    c.write(jl(*head, mk.blob(msg), u2, mk.req("r2", [mk.H(msg), mk.H(u2["message"])], base="r1", keep=1))), c.load()
    c.append(jl(*resp)), c.reload()


@case("blockmiss")
def block_looked_up(c):  # the response arrives after a call found a block of it in a message without its thinking
    head, u2, resp = two_turns()
    less = {"role": "assistant", "content": mk.resp_msg(resp)["content"][1:]}
    c.write(jl(*head, mk.blob(less), u2, mk.req("r2", [mk.H(less), mk.H(u2["message"])], base="r1", keep=1))), c.load()
    c.append(jl(*resp)), c.reload()


@case("usedgrew")
def response_grew(c):  # a response a call took its weights from gets another record
    head, u2, resp = two_turns()
    msg = mk.resp_msg(resp)
    c.write(jl(*head, mk.blob(msg), u2, mk.req("r2", [mk.H(msg), mk.H(u2["message"])], base="r1", keep=1), *resp[:2])), c.load()
    c.append(jl(*resp[2:])), c.reload()


@case("tailseen")
def twin_response(c):  # a response with the words of one a call took: that call takes this one now. Then it stays so
    u1, u2, u3, u4 = (mk.user(t) for t in ("hi", "again", "and again", "more"))
    done = [mk.asst(m, r, "req" + r, [{"type": "text", "text": "Done."}], mk.usage(3, 0, 900, 100, 60))
            for m, r in (("mA", "r1"), ("mB", "r2"))]
    msg = mk.resp_msg(done[0])
    shape = {"type": "api-request-shape", "shapeHash": "sh1", "timestamp": mk.ts(), "shape": mk.SHAPE}
    c.write(jl(shape, u1, mk.req("r1", [mk.H(u1["message"])]), *done[0], u2,
               mk.req("r2", [mk.H(msg), mk.H(u2["message"])], base="r1", keep=1))), c.load()
    c.append(jl(*done[1])), c.reload()
    c.append(jl(u3)), c.reload()
    c.append(jl(u4)), c.reload()


@case()
def twin_responses_before(c):  # the same, the twins there when loaded: no reload falls back
    u1, u2, u3, u4 = (mk.user(t) for t in ("hi", "again", "and again", "more"))
    done = [mk.asst(m, "r" + str(i), "req" + str(i), [{"type": "text", "text": "Done."}], mk.usage(3, 0, 900, 100, 60))
            for i, m in ((1, "mA"), (2, "mB"), (2, "mC"))]
    msg = mk.resp_msg(done[0])
    shape = {"type": "api-request-shape", "shapeHash": "sh1", "timestamp": mk.ts(), "shape": mk.SHAPE}
    c.write(jl(shape, u1, mk.req("r1", [mk.H(u1["message"])]), *done[0], u2,
               mk.req("r2", [mk.H(msg), mk.H(u2["message"])], base="r1", keep=1), *done[1], *done[2])), c.load()
    c.append(jl(u3)), c.reload()
    c.append(jl(u4)), c.reload()

if "--fixture" in args:
    binary = opt("--fixture")
    src = os.path.join(AIG, "tests", "fixtures", "context", "proj")
    env = dict(os.environ, HOME="/home/test", CLAUDE_CONFIG_DIR="/home/test/.claude", AIG_DEBUG="1")
    rnd = random.Random(7)
    plans = nreloads = 0
    fulls = {}
    tmp = tempfile.mkdtemp(prefix="aig-reload-")
    try:
        for rel in ["s1.jsonl", "s1/subagents/agent-sub1.jsonl", "s1/subagents/agent-sub2.jsonl", "s2.jsonl", "s3.jsonl",
                    "e1.jsonl"]:
            data = open(os.path.join(src, rel), "rb").read()
            ends = [i + 1 for i, c in enumerate(data) if c == 10]
            mids = [s + max(1, (e - s) // 2) for s, e in zip([0] + ends[:-1], ends)]
            todo = [ends] + [[m] for m in mids] + \
                   [sorted(rnd.sample(range(1, len(data)), k=min(6, len(data) - 1))) for _ in range(12)]
            for cuts in todo:
                d = os.path.join(tmp, "p")
                shutil.rmtree(d, ignore_errors=True)
                shutil.copytree(src, d)
                rc, out, reloads, full = run(binary, "/REPO", os.path.join(src, rel), os.path.join(d, rel), cuts, env)
                plans += 1
                nreloads += len(reloads)
                for f in full:
                    fulls[f] = fulls.get(f, 0) + 1
                if rc:
                    print(f"FAIL context reload: {rel} cuts {cuts[:10]}: {out}")
                    sys.exit(1)
                odd = [f for f in full if f != WHY["nonl"]]  # appends alone: only a cut just before a newline may
                if odd:
                    print(f"FAIL context reload: {rel} cuts {cuts[:10]}: appends fell back to a full load: {odd[0]}")
                    sys.exit(1)
        ncases, case_fulls = run_cases(binary, env, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    why = "; ".join(f"{n} full: {w}" for w, n in sorted(fulls.items()))
    print(f"ok   context reload ({plans} split plans, {nreloads} reloads equal to fresh loads{'; ' + why if why else ''}; "
          f"{ncases} fixed cases, {sum(case_fulls.values())} full loads: {len(case_fulls)} reasons)")
    sys.exit(0)

path = opt("--transcript")
if not path:
    print(__doc__)
    sys.exit(2)
root = opt("--root", os.path.dirname(AIG))
binary = opt("--bin")
nplans = int(opt("--plans", "6"))
if not binary:
    binary = os.path.join(tempfile.gettempdir(), "aig-ctx-reload")
    subprocess.run(f"cc -O2 -std=c11 -D_GNU_SOURCE -I{AIG}/src -I{AIG}/vendor/yyjson-0.10.0/src -o {binary} "
                   f"{HERE}/ctx_reload.c {AIG}/src/context.c {AIG}/src/pyjson.c {AIG}/src/util.c {AIG}/build/yyjson.o "
                   f"-lm -lpthread", shell=True, check=True)
path = os.path.abspath(path)
proj = os.path.dirname(path) if "/subagents/" not in path else path.split("/subagents/")[0].rsplit("/", 1)[0]
tmp = tempfile.mkdtemp(prefix="aig-reload-")
try:
    snap = os.path.join(tmp, "proj")
    shutil.copytree(proj, snap)
    src = os.path.join(snap, os.path.relpath(path, proj))
    frozen = src + ".all"
    shutil.copy(src, frozen)  # the cuts come from this copy: the transcript copy itself is cut and appended
    size = os.path.getsize(frozen)
    rnd = random.Random(int(opt("--seed", "1")))
    env = dict(os.environ, AIG_DEBUG="1", CLAUDE_CONFIG_DIR=os.environ.get("CLAUDE_CONFIG_DIR", ""))
    if not env["CLAUDE_CONFIG_DIR"]:
        del env["CLAUDE_CONFIG_DIR"]
    if "--requests" in args:
        n = int(opt("--requests"))
        offs, pos = [], 0
        with open(frozen, "rb") as fh:
            for line in fh:
                offs.append((pos, b'"type":"api-request"' in line[:40], b'"type":"assistant"' in line))
                pos += len(line)
        reqs = [i for i, o in enumerate(offs) if o[1]][-n:]
        cuts = []
        for i in reqs:
            cuts.append(offs[i][0])
            j = i + 1
            while j < len(offs) and not offs[j][2]:
                j += 1
            if j + 1 < len(offs):
                cuts.append(offs[j + 1][0])
        cuts = sorted(set(cuts))
        rc, out, reloads, full = run(binary, root, frozen, src, cuts, env)
        ms = sorted(int(re.match(r"ctx_reload (\d+) ms", r).group(1)) for r in reloads if "full load: " not in r)
        why = {}
        for f in full:
            why[f] = why.get(f, 0) + 1
        print(f"{len(reloads)} reloads: {out}; incremental {len(ms)}"
              + (f" (ms: median {ms[len(ms) // 2]}, max {ms[-1]})" if ms else "")
              + "".join(f"; full {n}: {w}" for w, n in sorted(why.items())))
        if rc:
            print("FAIL")
            sys.exit(1)
        nplans = 0
    for k in range(nplans):
        start = rnd.randrange(size // 2, size)
        cuts = sorted([start] + [rnd.randrange(start, size) for _ in range(4)])
        rc, out, reloads, full = run(binary, root, frozen, src, cuts, env)
        times = [re.match(r"ctx_reload (\d+) ms", r).group(1) + " ms" + (" (full: " + r.split("full load: ")[1] + ")"
                 if "full load: " in r else "") for r in reloads]
        print(f"plan {k}: cuts {[c - start for c in cuts]} after byte {start}: {out}; reloads {', '.join(times)}")
        if rc:
            print("FAIL")
            sys.exit(1)
    print("ok   context reload on", os.path.basename(path))
finally:
    shutil.rmtree(tmp, ignore_errors=True)
