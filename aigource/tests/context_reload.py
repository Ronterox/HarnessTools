#!/usr/bin/env python3
"""ctx_reload against fresh loads at split points (tests/ctx_reload.c).

  tests/context_reload.py --fixture BIN
      every fixture transcript: a reload after each line (each response's
      records arriving across reloads), a cut inside each line, and seeded
      random multi-cut sequences; run by tests/run.sh, plain and under ASan.
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
import os, random, re, shutil, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
AIG = os.path.dirname(HERE)
args = sys.argv[1:]


def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default


def run(binary, root, src, dst, cuts, env):
    r = subprocess.run([binary, root, src, dst] + [str(c) for c in cuts], env=env, capture_output=True, text=True)
    reloads = [line for line in r.stderr.splitlines() if line.startswith("ctx_reload")]
    full = [line.split("full load: ", 1)[1] for line in reloads if "full load: " in line]
    return r.returncode, r.stdout.strip(), reloads, full


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
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    why = "; ".join(f"{n} full: {w}" for w, n in sorted(fulls.items()))
    print(f"ok   context reload ({plans} split plans, {nreloads} reloads equal to fresh loads{'; ' + why if why else ''})")
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
