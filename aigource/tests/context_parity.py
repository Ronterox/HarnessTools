#!/usr/bin/env python3
"""Parity: aigource --context-dump against draft/context/build_ctx.py (the
oracle) on every agent of this repo's sessions, frozen first (live sessions
keep writing).

build_ctx.py is imported and run agent by agent, as its build() does (the
project's changes once, then per session its main agent and subagents), so
each of its agents is known by its transcript; aigource runs on the same
transcript. Compared, per agent:
- unresolved hashes (missing messages): 0 on both sides;
- calls: base, keep, tail, shape, usage, compaction, dropped thinking, model,
  time (to its 0.1 s rounding);
- message instances, and items: kind, source, text, length, weight (both
  round it to 0.1), the call that made it, tool, path, error, enter, leave,
  action and result, tool definitions;
- per call, each item's tokens, cache state and carried cost, from the
  page's prepare() (ported below) on build_ctx's data against aigource's
  --context-at (tokens and cost to float rounding: the page keeps carried
  cost in a Float32Array, aigource in a float);
- stale reads (time, author, own item, label) and likely links.
Every difference is listed; "explained" ones only where the cause is known
and documented in docs/CONTEXT.md (none so far).

With --project DIR --root ROOT it runs on that project dir as it is (no
snapshot): tests/run.sh checks the fixture this way. AIGOURCE names the
binary (default build/aigource).

  tests/context_parity.py [--keep [DIR]] [--only SESSION_PREFIX]
  tests/context_parity.py --project DIR --root ROOT
"""
import glob, json, os, re, shutil, subprocess, sys, tempfile, time
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
AIG = os.path.dirname(HERE)
REPO = os.path.dirname(AIG)
args = sys.argv[1:]
only = args[args.index("--only") + 1] if "--only" in args else None
BIN = os.environ.get("AIGOURCE", os.path.join(AIG, "build", "aigource"))
project = args[args.index("--project") + 1] if "--project" in args else None
if project:
    REPO = args[args.index("--root") + 1]
keep = None
if "--keep" in args:
    i = args.index("--keep")
    keep = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("--") else \
        os.path.join(tempfile.gettempdir(), "aig-ctx-parity-snapshot")

if project:
    tmp, proj = None, os.path.abspath(project)
else:
    cfg_src = os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude"))
    slug = re.sub(r"[/.]", "-", REPO)
    tmp = keep or tempfile.mkdtemp(prefix="aig-ctx-parity-")
    proj = os.path.join(tmp, "projects", slug)
    if not keep or not os.path.isdir(proj):
        shutil.rmtree(proj, ignore_errors=True)
        shutil.copytree(os.path.join(cfg_src, "projects", slug), proj)
    os.environ["CLAUDE_CONFIG_DIR"] = tmp  # before the import: build_ctx reads it as it loads
sys.path.insert(0, os.path.join(AIG, "draft", "context"))
import build_ctx as B  # noqa: E402

B.ROOT = os.path.abspath(REPO)
mains = sorted(glob.glob(os.path.join(proj, "*.jsonl")), key=os.path.getmtime)
allfiles = mains + sorted(glob.glob(os.path.join(proj, "*", "subagents", "*.jsonl")))
t_py = time.time()
changes = B.project_changes(allfiles)
t_py = time.time() - t_py


def prepare(A, prices):
    """The page's prepare() (context.tpl.html): per call, each position's tokens, cache state, carried cost."""
    items, calls = A["items"], A["calls"]
    n = len(calls)
    insts, f, total, noresp, ws, out = [None] * n, [0.0] * n, [0.0] * n, [0] * n, [0.0] * n, []
    orders = []
    for c, C in enumerate(calls):
        prev = insts[C["b"]] if C["b"] is not None else []
        cur = prev[:C["k"]] + C["tl"]
        insts[c] = cur
        order = list(C["sh"]) + [it for ii in cur for it in A["inst"][ii]]
        orders.append(order)
        drop = set(C.get("drop") or [])
        w = 0.0
        for it in order:
            if it not in drop:
                w += items[it]["w"]
        ws[c] = w
        if C["u"]:
            total[c] = C["u"][0] + C["u"][1] + C["u"][2] + C["u"][3]
            f[c] = total[c] / w if w else 0
        else:
            noresp[c] = 1
    for c in range(n):
        if noresp[c]:
            g, d = 0, 1
            while d < n and not g:
                if c - d >= 0 and not noresp[c - d]:
                    g = f[c - d]
                elif c + d < n and not noresp[c + d]:
                    g = f[c + d]
                d += 1
            f[c] = g or 1.3
            total[c] = ws[c] * f[c]
    acc = [0.0] * len(items)
    for c, C in enumerate(calls):
        p = prices.get(C["m"])
        drop = set(C.get("drop") or [])
        R = C["u"][1] if C["u"] else 0
        W = C["u"][2] + C["u"][3] if C["u"] else 0
        wrate = (C["u"][2] * p["w5"] + C["u"][3] * p["w1h"]) / W if p and W else 0
        x, row = 0.0, []
        for it in orders[c]:
            t = 0 if it in drop else items[it]["w"] * f[c]
            mid = x + t / 2
            x += t
            st = 3 if not C["u"] else 0 if mid <= R else 1 if mid <= R + W else 2
            if p and C["u"]:
                acc[it] += t * (p["read"] if st == 0 else wrate if st == 1 else p["in"]) / 1e6
            row.append((t, st, acc[it]))
        out.append(row)
    return out


def close(a, b, rel=1e-9, ab=1e-9):
    return abs(a - b) <= max(ab, rel * max(abs(a), abs(b)))


fails, explained, stats = [], Counter(), Counter()
timings = []


def fail(agent, msg):
    fails.append(f"{agent}: {msg}")


ITEM_FIELDS = ("k", "s", "m", "tool", "p", "err", "e", "l", "a", "r", "n", "names")

for f in mains:
    sid = os.path.basename(f)[:-6]
    if only and not sid.startswith(only):
        continue
    if not any(r.get("type") == "assistant" for r in B.records(f)):
        continue
    subs = sorted(glob.glob(os.path.join(proj, sid, "subagents", "*.jsonl")))
    agents = [B.Agent(f, {"kind": "main", "name": "main"}, sid)] + [B.Agent(g, {}, sid) for g in subs]
    for a in agents:
        t = time.time()
        a.build(allfiles)
        if not a.calls:
            continue
        B.stale_events(a, changes, [x.f for x in agents])
        B.links_of(a)
        py = B.agent_json(a, 0)
        t_py += time.time() - t
        name = f"{sid[:8]} {os.path.basename(a.f)[:-6][:23]}"
        stats["agents"] += 1
        r = subprocess.run([BIN, "--root", REPO, "--context-dump", a.f,
                            "--context-at", "all"], capture_output=True, text=True)
        if r.returncode:
            fail(name, f"aigource failed: {r.stderr.strip()[-300:]}")
            continue
        m = re.search(r"load (\d+) ms, peak RSS (\d+) MB", r.stderr)
        if m:
            timings.append((int(m.group(1)), int(m.group(2)), len(a.calls), name))
        D = json.loads(r.stdout)
        C = D["sessions"][0]["agents"][0]
        CS, t0 = D["S"], D["meta"]["t0"]
        # unresolved hashes
        stats["missing py"] += a.missing
        stats["missing c"] += C["missing"]
        if a.missing or C["missing"]:
            fail(name, f"unresolved hashes: build_ctx {a.missing}, aigource {C['missing']}")
        if C["est"] != a.estimated:
            fail(name, f"estimated: build_ctx {a.estimated}, aigource {C['est']}")
        # calls
        pc, cc = py["calls"], C["calls"]
        stats["calls"] += len(pc)
        if len(pc) != len(cc):
            fail(name, f"calls: {len(pc)} vs {len(cc)}")
        for i, (x, y) in enumerate(zip(pc, cc)):
            for k in ("b", "k", "tl", "sh", "u", "m"):
                if x.get(k) != y.get(k):
                    fail(name, f"call {i} {k}: {str(x.get(k))[:80]} vs {str(y.get(k))[:80]}")
            if bool(x.get("cmp")) != bool(y.get("cmp")) or (x.get("drop") or []) != (y.get("drop") or []):
                fail(name, f"call {i} cmp/drop: {x.get('cmp')} {x.get('drop')} vs {y.get('cmp')} {y.get('drop')}")
            if (x["t"] is None) != (y["t"] is None) or (x["t"] is not None and abs(x["t"] - (y["t"] + t0)) > 0.11):
                fail(name, f"call {i} t: {x['t']} vs {y['t']} + {t0}")
        if py["inst"] != C["inst"]:
            bad = next((i for i, (x, y) in enumerate(zip(py["inst"], C["inst"])) if x != y), min(len(py["inst"]), len(C["inst"])))
            fail(name, f"instances differ from {bad} ({len(py['inst'])} vs {len(C['inst'])})")
        # items
        pi, ci = py["items"], C["items"]
        stats["items"] += len(pi)
        if len(pi) != len(ci):
            fail(name, f"items: {len(pi)} vs {len(ci)}")
        nbad = 0
        for i, (x, y) in enumerate(zip(pi, ci)):
            diffs = [k for k in ITEM_FIELDS if x.get(k) != y.get(k)]
            if abs(x.get("w", 0) - y["w"]) > 1e-9:
                diffs.append("w")
            if B.S.list[x["x"]] != CS[y["x"]]:
                diffs.append("text")
            if [B.S.list[t] for t in x.get("tx", [])] != [CS[t] for t in y.get("tx", [])]:
                diffs.append("tx")
            if diffs:
                nbad += 1
                if nbad <= 5:
                    fail(name, f"item {i} ({x['k']} {x['s'][:40]!r}): " + "; ".join(
                        f"{k} {str(x.get(k) if k not in ('text',) else B.S.list[x['x']])[:60]!r} vs "
                        f"{str(y.get(k) if k not in ('text',) else CS[y['x']])[:60]!r}" for k in diffs))
            # stale reads
            xs = [[e[0], e[1], e[2], e[3]] for e in x.get("st", [])]
            ys = [[e[0] + t0, e[1], e[2], e[3]] for e in y.get("st", [])]
            if xs or ys:
                stats["stale reads"] += 1
                if len(xs) != len(ys) or any(abs(p[0] - q[0]) > 0.11 or p[1:] != q[1:] for p, q in zip(xs, ys)):
                    fail(name, f"item {i} stale: {xs[:3]} vs {ys[:3]}")
            # likely links
            if x.get("lk") is not None or y.get("lk") is not None:
                stats["actions"] += 1
                stats["links"] += len(x.get("lk") or [])
                if x.get("lk") != y.get("lk"):
                    fail(name, f"item {i} links: {x.get('lk')} vs {y.get('lk')}")
        if nbad > 5:
            fail(name, f"... {nbad - 5} more items differ")
        # tokens, cache state and carried cost per item per call
        if len(pc) == len(cc) and py["inst"] == C["inst"] and not nbad:
            rows = prepare(py, B.PRICES)
            for c, row in enumerate(rows):
                got = C["at"].get(str(c))
                if got is None or len(got) != len(row):
                    fail(name, f"call {c}: no derived values" if got is None else f"call {c}: {len(row)} vs {len(got)} positions")
                    continue
                stats["item-calls"] += len(row)
                for pos, ((t, st, cum), (t2, st2, cum2)) in enumerate(zip(row, got)):
                    if not close(t, t2) or st != st2 or not close(cum, cum2, rel=1e-6, ab=1e-12):
                        fail(name, f"call {c} pos {pos}: tokens/state/carried {t} {st} {cum} vs {t2} {st2} {cum2}")
                        break

print(f"agents {stats['agents']}, calls {stats['calls']}, items {stats['items']}, item-calls {stats['item-calls']}, "
      f"actions {stats['actions']} with {stats['links']} links, stale reads {stats['stale reads']}")
print(f"unresolved hashes: build_ctx {stats['missing py']}, aigource {stats['missing c']}")
if timings:
    timings.sort()
    big = max(timings, key=lambda x: x[2])
    print(f"aigource load: median {timings[len(timings) // 2][0]} ms, slowest {timings[-1][0]} ms ({timings[-1][3]}); "
          f"largest agent {big[3]} ({big[2]} calls): {big[0]} ms, peak RSS {big[1]} MB; build_ctx {t_py:.1f} s in all")
for k, n in explained.items():
    print(f"explained: {n} x {k}")
if tmp and not keep:
    shutil.rmtree(tmp)
for m in fails[:60]:
    print("MISMATCH " + m)
if len(fails) > 60:
    print(f"... {len(fails) - 60} more")
print("FAIL" if fails else "ok   context parity", f"({len(fails)} mismatches)" if fails else "")
sys.exit(1 if fails else 0)
