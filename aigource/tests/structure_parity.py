#!/usr/bin/env python3
"""Parity: aigource --structure-dump against draft/build_data.py (the oracle) on
the same transcripts of this repo, frozen first (live sessions keep writing).

Enforced, for every file in build_data's tree (it must be in aigource's too):
its turns, +/- per change, blame (the turn of each line), rework pairs,
verify states, tokens and seconds, changed-unread, the file's length after
each change (trend), searches; reads and top paths within the tolerances
below. Per turn: title, flags, tokens, seconds; every build_data turn must
exist in aigource. Every build_data change must match one of aigource's.
Functions and complexity are reported only (tree-sitter vs regexes).

Tolerances, from documented deviations (aigource/src/analysis.c, top):
- tokens/seconds: aigource also sees changes to files build_data doesn't map,
  and an agent's tokens are split at each change: the agent's running total
  in the turn must still agree at every change both see.
- reads: aigource counts one read per file per command (no sed/head ranges)
  and cmdguess's readers (tail, more, <, which build_data doesn't count): per
  change -2 - 10% <= build_data - aigource <= 2 + 70% of build_data's; over all
  changes within 15%.
- top paths: the 8 most read follow from reads: half or more in common for
  90% of changes.
- changed-unread: depends on the functions; a difference counts only in files
  whose functions both find alike.

  tests/structure_parity.py [--keep [DIR]] [--verbose]
"""
import glob, json, os, re, shutil, subprocess, sys, tempfile
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
AIG = os.path.dirname(HERE)
REPO = os.path.dirname(AIG)
SCOPE = ["aigource/src/", "aic/aic/"]
args = sys.argv[1:]
verbose = "--verbose" in args
keep = None
if "--keep" in args:
    i = args.index("--keep")
    keep = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("--") else \
        os.path.join(tempfile.gettempdir(), "aig-parity-snapshot")

cfg_src = os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude"))
slug = re.sub(r"[/.]", "-", REPO)
tmp = keep or tempfile.mkdtemp(prefix="aig-parity-")
proj = os.path.join(tmp, "projects", slug)
if not keep or not os.path.isdir(proj):
    shutil.rmtree(proj, ignore_errors=True)
    shutil.copytree(os.path.join(cfg_src, "projects", slug), proj)
env = dict(os.environ, CLAUDE_CONFIG_DIR=tmp)
env.pop("AIG_NO_DEFS", None)
files = sorted(glob.glob(proj + "/**/*.jsonl", recursive=True))  # build_data's set and order

py = json.loads(subprocess.run([sys.executable, "-I", os.path.join(AIG, "draft", "build_data.py"), REPO, *SCOPE],
                               env=env, capture_output=True, text=True, check=True).stdout)
r = subprocess.run([os.path.join(AIG, "build", "aigource"), "--structure-dump", "--root", REPO, *files],
                   env=env, capture_output=True, text=True, check=True)
c = json.loads(r.stdout)
if not keep:
    shutil.rmtree(tmp)


def file_nodes(data):
    out = {}
    def walk(n, path):
        for k in n["kids"]:
            p = path + k["name"]
            if k["kind"] == "dir":
                walk(k, p + "/")
            elif k["kind"] == "file":
                out[p] = k
    walk(data["tree"], "")
    return out


def defs_of(node, pre=""):
    out = []
    for k in node["kids"]:
        key = pre + k["name"]
        out.append(key)
        out += defs_of(k, key + ".")
    return out


def tid(data, t):  # a turn's identity across both: prompt time and title
    return (data["turns"][t]["ts"], data["turns"][t]["title"])


def comment(data, ch):
    x = ch[1]
    return data["comments"][x if x >= 0 else -1 - x]


def cumulative(data, cid):  # the agent's tokens in the turn up to this change
    ch, me = data["changes"], data["changes"][cid]
    return [sum(ch[k][9 + j] for k in range(cid + 1) if ch[k][0] == me[0] and ch[k][8] == me[8]) for j in range(3)]


pf, cf = file_nodes(py), file_nodes(c)
common = sorted(set(pf) & set(cf))
fails, notes = [], []
explained, stats = Counter(), Counter()
for f in sorted(set(pf) - set(cf)):
    fails.append(f"{f}: in build_data's tree, not in aigource's")

# Changes line up file by file (both replay in time order).
pmap, cmap = {}, {}  # cid -> (file, index in its ch)
for f in common:
    for i, e in enumerate(pf[f]["ch"]):
        pmap[e[0]] = (f, i)
for f in cf:
    for i, e in enumerate(cf[f]["ch"]):
        cmap[e[0]] = (f, i)

reads_p = reads_c = 0
for f in common:
    p, q = pf[f], cf[f]
    pch, cch = p["ch"], q["ch"]
    pt = [tid(py, py["changes"][e[0]][0]) for e in pch]
    ct = [tid(c, c["changes"][e[0]][0]) for e in cch]
    if set(pt) != set(ct):
        fails.append(f"{f}: turns differ: only build_data {sorted(set(pt) - set(ct))[:3]} "
                     f"only aigource {sorted(set(ct) - set(pt))[:3]}")
        continue
    if len(pch) != len(cch) or pt != ct:
        fails.append(f"{f}: changes differ: {len(pch)} vs {len(cch)}")
        continue
    pd, cd = set(defs_of(p)), set(defs_of(q))
    same_defs = pd == cd
    for i, (a, b) in enumerate(zip(pch, cch)):
        stats["changes"] += 1
        if a[1:3] != b[1:3]:
            fails.append(f"{f}: change {i} ({pt[i][1][:40]!r}) +/- {a[1:3]} vs {b[1:3]}")
        pc, cc = py["changes"][a[0]], c["changes"][b[0]]
        if pc[3] != cc[3]:
            fails.append(f"{f}: change {i} verify {pc[3]} vs {cc[3]}")
        if a[3] != b[3]:
            if same_defs:
                fails.append(f"{f}: change {i} changed-unread {a[3]} vs {b[3]}")
            else:
                explained["changed-unread: the file's functions differ (tree-sitter vs regexes)"] += 1
        if pc[9:13] != cc[9:13]:
            if cumulative(py, a[0]) == cumulative(c, b[0]):
                explained["tokens/secs: split at changes to files outside build_data's scope (running totals agree)"] += 1
            else:
                fails.append(f"{f}: change {i} tokens/secs {pc[9:13]} vs {cc[9:13]}")
        if pc[5] != cc[5]:
            fails.append(f"{f}: change {i} searches {pc[5]} vs {cc[5]}")
        reads_p += pc[4]
        reads_c += cc[4]
        stats["reads equal"] += pc[4] == cc[4]
        # aigource also counts tail, more and < reads (docs/STRUCTURE.md, "Command
        # reads"; build_data's pattern is cat|head|less|bat|batcat|nl|sed). The
        # data keeps no reader per read, so those can't be told apart: aigource
        # may count up to 2 + 10% more.
        if not -2 - 0.1 * pc[4] <= pc[4] - cc[4] <= 2 + 0.7 * pc[4]:
            fails.append(f"{f}: change {i} reads {pc[4]} vs {cc[4]}")
        elif pc[4] - cc[4] < -2:
            explained["reads: aigource's extra command readers (tail, more, <)"] += 1
        ptop = {py["paths"][x] for x, _ in pc[6]}
        ctop = {c["paths"][x] for x, _ in cc[6]}
        stats["top paths half shared"] += len(ptop & ctop) * 2 >= len(ptop | ctop)
        stats["top paths equal"] += [(py["paths"][x], n) for x, n in pc[6]] == [(c["paths"][x], n) for x, n in cc[6]]
    # blame: the turn of each of today's lines
    def lines(data, node):
        out = []
        for n, o in node["blame"]:
            out += [tid(data, data["changes"][o][0]) if o >= 0 else None] * n
        return out
    pl, cl = lines(py, p), lines(c, q)
    if pl != cl:
        diff = sum(1 for a, b in zip(pl, cl) if a != b) + abs(len(pl) - len(cl))
        fails.append(f"{f}: blame differs on {diff} of {len(pl)} lines")
    # rework pairs and trend, by position of the change in the file
    pidx = {e[0]: i for i, e in enumerate(pch)}
    cidx = {e[0]: i for i, e in enumerate(cch)}
    prw = Counter({(pidx.get(o), pidx.get(k)): n for o, k, n in p["rw"]})
    crw = Counter({(cidx.get(o), cidx.get(k)): n for o, k, n in q["rw"]})
    if prw != crw:
        fails.append(f"{f}: rework differs: {dict(prw - crw)} vs {dict(crw - prw)}")
    ptr = [(pidx.get(k), n) for k, n, _ in p["tr"]]
    ctr = [(cidx.get(k), n) for k, n, _ in q["tr"]]
    if ptr != ctr:
        fails.append(f"{f}: trend differs: {ptr[:4]} vs {ctr[:4]}")
    if p["lines"] != q["lines"]:
        fails.append(f"{f}: lines {p['lines']} vs {q['lines']}")
    # functions and complexity: reported only
    stats["file cx equal"] += p["cx"] == q["cx"]
    if not same_defs:
        notes.append(f"{f}: defs {len(pd)} vs {len(cd)}, {len(pd & cd)} shared"
                     + (f"; only build_data {sorted(pd - cd)[:4]}" if verbose else ""))
n = stats["changes"]
if reads_p and abs(reads_p - reads_c) > 0.15 * reads_p:
    fails.append(f"reads over all changes: {reads_p} vs {reads_c}")
if n and stats["top paths half shared"] < 0.9 * n:
    fails.append(f"top paths half shared in only {stats['top paths half shared']} of {n} changes")

# build_data changes in no compared file: the same change in aigource, in a file
# build_data's tree hasn't got (git doesn't track it yet).
cused = {k for k, (f, _) in cmap.items() if f in common}
by_key = {}
for k, ch in enumerate(c["changes"]):
    if k not in cused:
        by_key.setdefault((ch[7], tid(c, ch[0]), ch[2], comment(c, ch)), []).append(k)
for k, ch in enumerate(py["changes"]):
    if k in pmap:
        continue
    hits = by_key.get((ch[7], tid(py, ch[0]), ch[2], comment(py, ch)), [])
    where = next((cmap[h][0] for h in hits if h in cmap and cmap[h][0] not in pf), None)
    if where:
        explained[f"change in a file git doesn't track (build_data maps tracked files only)"] += 1
        hits.remove(next(h for h in hits if h in cmap and cmap[h][0] == where))
    else:
        fails.append(f"build_data change {k} ({ch[2]} at {ch[7]}) matches no aigource change")

pturns = {tid(py, i): t for i, t in enumerate(py["turns"])}
cturns = {tid(c, i): t for i, t in enumerate(c["turns"])}
for k in sorted(set(pturns) - set(cturns)):
    fails.append(f"turn {k[0]} {k[1][:40]!r}: only in build_data")
for k in sorted(set(pturns) & set(cturns)):
    a, b = pturns[k], cturns[k]
    for field in ("corr", "intr", "fix", "tok", "secs"):
        if a.get(field) != b.get(field):
            fails.append(f"turn {k[0]} {k[1][:40]!r}: {field} {a.get(field)} vs {b.get(field)}")

print(f"files: {len(pf)} build_data, {len(cf)} aigource, {len(common)} compared")
print(f"turns: {len(pturns)} build_data, {len(cturns)} aigource, {len(set(pturns) & set(cturns))} compared")
print(f"changes: {len(py['changes'])} build_data, {len(c['changes'])} aigource, {n} compared")
print(f"reads: {reads_p} build_data, {reads_c} aigource; equal in {stats['reads equal']} of {n} changes")
print(f"top paths: equal in {stats['top paths equal']}, half or more shared in {stats['top paths half shared']} of {n}")
print(f"complexity: file cx equal in {stats['file cx equal']} of {len(common)} files (reported, not enforced)")
for k, m in explained.items():
    print(f"explained: {m} x {k}")
print(f"functions differ in {len(notes)} of {len(common)} files (reported, not enforced)")
if verbose:
    for m in notes:
        print("  " + m)
print("aigource: " + r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "")
for m in fails[:60]:
    print("MISMATCH " + m)
if len(fails) > 60:
    print(f"... {len(fails) - 60} more")
print("FAIL" if fails else "ok   structure parity", f"({len(fails)} mismatches)" if fails else "")
sys.exit(1 if fails else 0)
