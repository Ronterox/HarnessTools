#!/usr/bin/env python3
"""How specific is each blob's label? Measures on the final step of a labelled timeline (SCHEMA.md).

    python specificity.py TIMELINE --out DIR [--server http://127.0.0.1:8080] [--src ROOT]
                          [--no-model] [--split-lines N] [--split-funcs N] [--tiny-lines N]

Per code blob (functions > 0), written to DIR/blobs.csv and DIR/blobs.json:
1. discrimination: the label (title + summary) and 5 shuffled candidates, the blob plus its 4 hardest
   distractors (same module, edge-connected, same kind, nearest by function-name / file overlap), each
   described by kind, files, size and its biggest functions. 2 shuffles; score = fraction correct.
   Control: the label of a random other blob (not a candidate) on the first shuffle, chance = 0.2.
2. coverage: the blob's functions (name, lines, first line of the definition), chunked to --prompt-chars like
   label.py's prompt budget, each assigned by the model to one of five labels: the blob's own and its four
   distractors' (shuffled). coverage = share of functions given to the own label (chance 0.2), also by lines;
   the rest are "not described", with the blob they went to. Asked absolutely instead ("which functions does
   it NOT describe"), the model answered the same for the own and an unrelated label. Control: the own label
   replaced by a random non-candidate's.
3. numbers: lines, funcs, files, modules, internal call density, sub-roots (functions that alone own
   more than 10% / 20% of the blob's functions, ownership as CLang.split_entry measures it: what a
   callee reaches while no sibling callee and not the parent except through it does, i.e. its
   dominator subtree, descending past a callee that owns more than half like split_entry does).
Then a CPU-only split preview (DIR/split_preview.json / .txt): blobs over --split-lines or --split-funcs
cut at their dominant callees, tiny blobs (under --tiny-lines) folded into their strongest neighbour.

Answers are cached by prompt in DIR/answers.json (a rerun is free); before every uncached call it pauses
while the GPU reads >= --hot °C until below --cool (label.cool_down). Reasoning off, temperature 0.
"""
import argparse
import csv
import hashlib
import json
import os
import random
import re
import statistics
import sys
import time
import urllib.request
from collections import Counter, defaultdict

import label
import timeline

LETTERS = "ABCDE"
TOP_FUNCS = 8
SUB_SHARES = (0.10, 0.20)

DISC_SYSTEM = """You match a short description to the part of a software program it describes.
You get the description and five candidate parts, each with its kind, files, size and biggest functions.
Answer with JSON only, no prose: {"answer": "<letter>"}"""

# Coverage asks which of five descriptions each function belongs to (relative, like discrimination): asked
# absolutely ("which functions does it NOT describe", or a yes/no per function) Qwen3.5-4B without reasoning
# answered the same for the blob's own label and an unrelated one (see the run notes).
COV_SYSTEM = """You assign each function of a software program to the description of the part it belongs to.
You get five descriptions (A to E) of parts of the program and a numbered list of functions, each with its name,
size and first line. Answer with JSON only, no prose, every number once: {"1": "<letter>", "2": "<letter>", ...}"""


# --- data ---------------------------------------------------------------------------------------

def load_final(path):
    """-> (final step state, analyzed root) of a timeline file, any schema."""
    with open(path) as f:
        tl = json.load(f)
    return timeline.expand(tl)[-1], tl.get("root", ".")


def label_text(lab):
    return f'{lab["title"]}: {lab["summary"]}'


def module_of(qual):
    return qual.rsplit(".", 1)[0]


def callgraph(blobs):
    """-> (succ: qual -> [callee quals], blob_of: qual -> blob id), program calls only, deferred included."""
    blob_of = {f["qual"]: b["id"] for b in blobs for f in b["funcs"]}
    succ = {}
    for b in blobs:
        for f in b["funcs"]:
            succ[f["qual"]] = list(dict.fromkeys(c["to"] for c in f["calls"] if c["to"] in blob_of))
    return succ, blob_of


def dominators(nodes, succ, starts):
    """Immediate dominators over `nodes` (calls leaving `nodes` ignored) from a virtual root None linked to
    `starts`; a node no start reaches becomes a start too. -> {node: idom (None = the virtual root)}."""
    nodes = set(nodes)
    starts = [s for s in dict.fromkeys(starts) if s in nodes]
    order, seen = [], set()

    def dfs(s):  # iterative post-order
        stack = [(s, iter(succ.get(s, ())))]
        seen.add(s)
        while stack:
            n, it = stack[-1]
            for m in it:
                if m in nodes and m not in seen:
                    seen.add(m)
                    stack.append((m, iter(succ.get(m, ()))))
                    break
            else:
                stack.pop()
                order.append(n)

    for s in starts:
        if s not in seen:
            dfs(s)
    for n in sorted(nodes - seen):  # unreached (a cycle no start enters): its first node is a start
        if n not in seen:
            starts.append(n)
            dfs(n)
    rpo = [None] + order[::-1]
    index = {n: i for i, n in enumerate(rpo)}
    preds = defaultdict(list)
    for s in starts:
        preds[s].append(None)
    for n in nodes:
        for m in succ.get(n, ()):
            if m in nodes and m != n:
                preds[m].append(n)
    idom = {None: None}

    def meet(a, b):
        while a != b:
            while index[a] > index[b]:
                a = idom[a]
            while index[b] > index[a]:
                b = idom[b]
        return a

    changed = True
    while changed:
        changed = False
        for n in rpo[1:]:
            ps = [p for p in preds[n] if p in idom]
            new = ps[0]
            for p in ps[1:]:
                new = meet(p, new)
            if idom.get(n, "?") != new:
                idom[n] = new
                changed = True
    del idom[None]
    return idom


def blob_starts(blob, succ, blob_of):
    """A blob's starts: its root function when it has one, functions no blob function calls, and functions
    another blob calls."""
    quals = [f["qual"] for f in blob["funcs"]]
    mine = set(quals)
    inner = {m for q in quals for m in succ[q] if m in mine and m != q}
    outer = {m for q, ms in succ.items() if blob_of[q] != blob["id"] for m in ms if m in mine}
    root = [blob["id"]] if blob["id"] in mine else []
    return root + [q for q in quals if q not in inner or q in outer]


def ownership(blob, succ, blob_of):
    """split_entry-style ownership. -> dict: owned (qual -> set of quals it dominates), cands (the callees
    at the level ownership is read at), chain (the >50% owners descended through), stays (what the
    parent keeps: the chain plus everything no candidate owns)."""
    lines = {f["qual"]: f["lines"] for f in blob["funcs"]}
    nodes = set(lines)
    starts = blob_starts(blob, succ, blob_of)
    idom = dominators(nodes, succ, starts)
    kids = defaultdict(list)
    for n, d in idom.items():
        kids[d].append(n)
    owned = {}

    def subtree(n):
        out, stack = set(), [n]
        while stack:
            m = stack.pop()
            out.add(m)
            stack += kids[m]
        return out

    for n in nodes:
        owned[n] = subtree(n)
    callers = defaultdict(set)
    for q in nodes:
        for m in succ[q]:
            if m in nodes and m != q:
                callers[m].add(q)
    parent, chain, kept = None, [], []
    while True:
        direct = set(starts) if parent is None else set(succ[parent])
        # a qualifying callee: dominated by the parent (no other way in) and not a helper (2+ other callers)
        level = [c for c in kids[parent] if c in direct and len(callers[c] - {parent}) < 2]
        if not level:
            break
        big = max(level, key=lambda c: len(owned[c]))
        if len(owned[big]) > 0.5 * len(nodes) and len(chain) < 6:
            chain.append(big)     # as split_entry: descend into it, its siblings stay candidates
            kept += [c for c in level if c != big]
            parent = big
            continue
        break
    cands = kept + level
    held = set().union(*(owned[c] for c in cands)) if cands else set()
    stays = nodes - held
    return {"owned": owned, "cands": cands, "chain": chain, "stays": stays, "lines": lines}


def numbers(blob, succ, blob_of):
    quals = [f["qual"] for f in blob["funcs"]]
    mine, n = set(quals), len(quals)
    pairs = {(q, m) for q in quals for m in succ[q] if m in mine and m != q}
    out_calls = sum(1 for q in quals for m in succ[q] if m not in mine)
    own = ownership(blob, succ, blob_of)
    shares = sorted((len(own["owned"][c]) / n for c in own["cands"]), reverse=True)
    row = {"lines": blob["size"], "funcs": n, "files": len({f["file"] for f in blob["funcs"]}),
           "modules": len({module_of(q) for q in quals}),
           "call_density": round(len(pairs) / (n * (n - 1)), 4) if n > 1 else 0.0,
           "calls_inside": round(len(pairs) / (len(pairs) + out_calls), 3) if pairs or out_calls else 0.0,
           "split_depth": len(own["chain"]), "top_share": round(shares[0], 3) if shares else 0.0}
    for x in SUB_SHARES:
        row[f"sub_roots_{int(x * 100)}"] = sum(s > x for s in shares)
    return row


# --- prompts ------------------------------------------------------------------------------------

def name_tokens(blob):
    toks = set()
    for f in blob["funcs"]:
        for part in re.split(r"[^A-Za-z0-9]+|(?<=[a-z])(?=[A-Z])", f["name"]):
            if len(part) > 2:
                toks.add(part.lower())
    return toks


def jaccard(a, b):
    return len(a & b) / len(a | b) if a | b else 0.0


def distractors(blob, blobs, edges, k=4):
    """The k hardest other blobs: same module (or its first two parts), edge-connected, same kind, then
    nearest by function-name tokens and files."""
    linked = {e["b"] if e["a"] == blob["id"] else e["a"] for e in edges if blob["id"] in (e["a"], e["b"])}
    toks, files = name_tokens(blob), {f["file"] for f in blob["funcs"]}
    group = blob["module"].split(".")[:2]

    def score(o):
        return (2 * jaccard(toks, name_tokens(o)) + 2 * jaccard(files, {f["file"] for f in o["funcs"]})
                + (o["id"] in linked) + (o["module"] == blob["module"])
                + 0.5 * (o["module"].split(".")[:2] == group) + 0.5 * (o["kind"] == blob["kind"]))

    others = [o for o in blobs if o["id"] != blob["id"]]
    return sorted(others, key=lambda o: (-score(o), o["id"]))[:k]


def describe(blob):
    files = Counter()
    for f in blob["funcs"]:
        files[f["file"]] += f["lines"]
    fl = [p for p, _ in files.most_common(3)]
    more = f" +{len(files) - 3} more" if len(files) > 3 else ""
    top = [f["name"] for f in sorted(blob["funcs"], key=lambda f: -f["lines"])[:TOP_FUNCS]]
    return (f"kind {blob['kind']}; files {', '.join(fl)}{more}; {blob['size']} lines in {len(blob['funcs'])} "
            f"functions; biggest functions: {', '.join(top)}")


def disc_prompt(lab, cands):
    lines = [f'Description: "{label_text(lab)}"', "", "Which of these parts does the description describe?"]
    lines += [f"{LETTERS[i]}) {describe(c)}" for i, c in enumerate(cands)]
    lines += ["", "Answer with the letter of the one part it fits best."]
    return "\n".join(lines)


def parse_choice(text):
    try:
        d = json.loads(text[text.find("{"):text.rfind("}") + 1])
        a = str(d.get("answer", "")).strip().upper()[:1]
    except (ValueError, AttributeError):
        m = re.search(r"\b([A-E])\b", text)
        a = m.group(1) if m else ""
    return a if a and a in LETTERS else None


def first_line(src, f, cache):
    """The definition's first line (joined with the next when the name sits there), '' when unreadable."""
    if f["name"].startswith("<"):
        return ""
    if f["file"] not in cache:
        try:
            with open(os.path.join(src, f["file"]), encoding="utf-8", errors="replace") as fh:
                cache[f["file"]] = fh.read().splitlines()
        except OSError:
            cache[f["file"]] = []
    text = cache[f["file"]]
    i = f["start"] - 1
    if not 0 <= i < len(text):
        return ""
    line = text[i].strip()
    bare = f["name"].rsplit(".", 1)[-1]
    if bare not in line and i + 1 < len(text) and bare in text[i + 1]:
        line += " " + text[i + 1].strip()
    if bare not in line:
        return ""
    return line.rstrip("{ ").strip()[:110]


def func_items(blob, src, files):
    out = []
    for f in blob["funcs"]:
        sig = first_line(src, f, files)
        out.append(f"{f['name']} ({f['lines']} lines)" + (f": {sig}" if sig and sig != f["name"] else ""))
    return out


def cov_chunks(items, budget):
    """Split the function lines into chunks of at most `budget` characters each, numbered from 1 in each chunk
    (numbered on across chunks, the model restarted at 1 past ~90 and its answers were lost)."""
    chunks, cur, size = [], [], 0
    for it in items:
        line = f"{len(cur) + 1}. {it}"
        if cur and size + len(line) + 1 > budget:
            chunks.append(cur)
            cur, size = [], 0
            line = f"1. {it}"
        cur.append(line)
        size += len(line) + 1
    if cur:
        chunks.append(cur)
    return chunks


def cov_prompt(labels, chunk):
    lines = ["Descriptions:"] + [f'{LETTERS[i]}) "{label_text(lab)}"' for i, lab in enumerate(labels)]
    lines += ["", "Functions, as number. name (lines): first line:"] + chunk
    return "\n".join(lines + ["", "For each function, the letter of the description of the part it belongs to."])


def parse_assign(text, valid):
    """{number: letter} for the valid numbers the reply assigns to a letter A-E (others are left out)."""
    out = {}
    for n, v in re.findall(r'"?(\d+)"?\s*:\s*"\s*([A-Ea-e])\b', text):
        if int(n) in valid:
            out.setdefault(int(n), v.upper())
    return out


# --- model --------------------------------------------------------------------------------------

class Model:
    """One llama-server, answers cached by prompt (sha1 of system + user) in a JSON file."""

    def __init__(self, server, cache_path, hot=85, cool=80, offline=False):
        self.server, self.path, self.hot, self.cool, self.offline = server.rstrip("/"), cache_path, hot, cool, offline
        try:
            with open(cache_path) as f:
                self.cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self.cache = {}
        self.calls = self.hits = self.paused = 0
        self.secs = 0.0
        self.dirty = 0

    def ask(self, system, user, max_tokens):
        key = hashlib.sha1((system + "\0" + user).encode()).hexdigest()
        if key in self.cache:
            self.hits += 1
            return self.cache[key]
        if self.offline:
            return None
        paused, _ = label.cool_down(self.hot, self.cool)
        self.paused += paused
        t0 = time.monotonic()
        body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": 0, "max_tokens": max_tokens, "response_format": {"type": "json_object"},
                "cache_prompt": True, "chat_template_kwargs": {"enable_thinking": False}}
        for attempt in range(3):
            try:
                reply = label.post(self.server, "/v1/chat/completions", body, timeout=120)
                break
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(2 * 2 ** attempt)
        self.secs += time.monotonic() - t0
        self.calls += 1
        text = reply["choices"][0]["message"]["content"]
        self.cache[key] = text
        self.dirty += 1
        self.save()  # every answer, so a stopped run loses none
        return text

    def save(self):
        if self.dirty:
            label.save_cache(self.path, self.cache)
            self.dirty = 0


def discrimination(model, blob, lab, pool, edges, shuffles=2):
    """-> (fraction correct over the shuffles, [answers], [candidate ids per shuffle])."""
    cands = [blob] + distractors(blob, pool, edges)
    right, answers, orders = 0, [], []
    for k in range(shuffles):
        order = cands[:]
        random.Random(f"{blob['id']}:{k}").shuffle(order)
        text = model.ask(DISC_SYSTEM, disc_prompt(lab, order), 20)
        a = parse_choice(text) if text is not None else None
        pick = order[LETTERS.index(a)]["id"] if a else None
        answers.append({"letter": a, "pick": pick, "pos": order.index(blob)})
        orders.append([c["id"] for c in order])
        right += pick == blob["id"]
    return right / shuffles, answers, orders


def coverage(model, blob, lab, others, items, budget, seed):
    """Each chunk of the blob's functions is assigned to `lab` or one of the `others` (blobs, their labels),
    shuffled by `seed`. -> dict cov (share of functions given to `lab`), cov_lines (by lines), uncovered
    ([name, blob id it went to or None when unanswered]), unanswered (count); None offline."""
    slots = [None] + others
    random.Random(seed).shuffle(slots)
    labels = [lab if o is None else o["label"] for o in slots]
    funcs, got, base = blob["funcs"], {}, 0
    for chunk in cov_chunks(items, budget):
        text = model.ask(COV_SYSTEM, cov_prompt(labels, chunk), 12 * len(chunk) + 40)
        if text is None:
            return None
        got.update({base + i: v for i, v in parse_assign(text, set(range(1, len(chunk) + 1))).items()})
        base += len(chunk)
    own = LETTERS[slots.index(None)]
    mine = [i for i in range(1, len(funcs) + 1) if got.get(i) == own]
    lines = sum(f["lines"] for f in funcs) or 1
    return {"cov": len(mine) / len(funcs), "cov_lines": sum(funcs[i - 1]["lines"] for i in mine) / lines,
            "uncovered": [[funcs[i - 1]["name"], slots[LETTERS.index(got[i])]["id"] if i in got else None]
                          for i in range(1, len(funcs) + 1) if got.get(i) != own],
            "unanswered": len(funcs) - len(got),
            "same_letter": max(Counter(got.values()).values()) / len(got) if got else None}


# --- split preview ------------------------------------------------------------------------------

def split_preview(blobs, edges, succ, blob_of, split_lines, split_funcs, tiny_lines, min_share=0.0):
    """Big blobs cut at their qualifying callees (pieces of tiny_lines or more that own at least min_share of
    the blob's lines; 0: every one, as split_entry cuts), tiny ones folded into the neighbour with the most
    call (then data) weight. -> (records, sizes before, sizes after)."""
    by_id = {b["id"]: b for b in blobs}
    splits, folds = [], []
    after = {}
    for b in blobs:
        if b["size"] >= split_lines or len(b["funcs"]) >= split_funcs:
            own = ownership(b, succ, blob_of)
            ln = own["lines"]
            pieces, stays = [], set(own["stays"])
            for c in sorted(own["cands"], key=lambda c: -len(own["owned"][c])):
                o = own["owned"][c]
                size = sum(ln[q] for q in o)
                if size >= tiny_lines and ln[c] >= 3 and size >= min_share * b["size"]:
                    pieces.append({"root": c, "funcs": len(o), "lines": size,
                                   "top": [q.rsplit(".", 1)[-1] for q in sorted(o, key=lambda q: -ln[q])[:5]]})
                else:
                    stays |= o
            parent = {"funcs": len(stays), "lines": sum(ln[q] for q in stays)}
            splits.append({"id": b["id"], "label": b["label"]["title"], "lines": b["size"], "funcs": len(b["funcs"]),
                           "chain": own["chain"], "candidates": len(own["cands"]), "pieces": pieces,
                           "parent": parent})
            if pieces:
                after[b["id"]] = parent["lines"]
                for p in pieces:
                    after[f"{b['id']}>{p['root']}"] = p["lines"]
                continue
        after[b["id"]] = b["size"]
    weight = defaultdict(Counter)
    for e in edges:
        if e["a"] in by_id and e["b"] in by_id and e["a"] != e["b"]:
            w = e["n"] * (1000 if e["kind"] in ("main", "error") else 1)
            weight[e["a"]][e["b"]] += w
            weight[e["b"]][e["a"]] += w
    for b in sorted(blobs, key=lambda b: (b["size"], b["id"])):
        if b["size"] >= tiny_lines or b["id"] not in after:
            continue
        # never across tests / app code; a same-module neighbour (a .h with its .c) first, the biggest one, as
        # discrimination confuses a tiny blob with exactly that; else the strongest call (then data) link
        stem = lambda o: o["module"].removesuffix("_h")
        nb = [(stem(by_id[o]) == stem(b), w, by_id[o]["size"], o) for o in after if o in by_id and o != b["id"]
              and (by_id[o]["kind"] == "tests") == (b["kind"] == "tests")
              for w in [weight[b["id"]][o]] if w or stem(by_id[o]) == stem(b)]
        if not nb:
            folds.append({"id": b["id"], "label": b["label"]["title"], "lines": b["size"], "into": None})
            continue
        same, w, _, into = max(nb, key=lambda t: (t[0], t[2] if t[0] else t[1], -t[2] if not t[0] else 0, t[3]))
        folds.append({"id": b["id"], "label": b["label"]["title"], "lines": b["size"], "into": into,
                      "into_label": by_id[into]["label"]["title"],
                      "via": "module" if same else "call" if w >= 1000 else "data"})
        size = after.pop(b["id"])
        after[into] = after.get(into, by_id[into]["size"]) + size
        for o, ww in weight[b["id"]].items():  # the merged blob inherits the tiny one's links
            if o != into:
                weight[into][o] += ww
                weight[o][into] += ww
    return {"splits": splits, "folds": folds}, [b["size"] for b in blobs], list(after.values())


# --- report -------------------------------------------------------------------------------------

def dist(xs):
    xs = sorted(xs)
    if not xs:
        return "n=0"
    q = lambda p: xs[min(len(xs) - 1, int(p * (len(xs) - 1) + 0.5))]
    return (f"n={len(xs)} min={xs[0]:.3g} p10={q(.1):.3g} p25={q(.25):.3g} median={q(.5):.3g} "
            f"p75={q(.75):.3g} p90={q(.9):.3g} max={xs[-1]:.3g} mean={statistics.mean(xs):.3g}")


def spearman(xs, ys):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(v):
            j = i
            while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r
    if len(xs) < 3:
        return float("nan")
    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else float("nan")


LINE_BUCKETS = [1, 20, 50, 100, 200, 500, 1000]
FUNC_BUCKETS = [1, 2, 5, 10, 20, 50, 100]


def bucket_table(rows, key, edges, cols):
    out = [f"{key:>12} {'n':>3} " + " ".join(f"{c:>10}" for c in cols)]
    for lo, hi in zip(edges, edges[1:] + [None]):
        sel = [r for r in rows if r[key] >= lo and (hi is None or r[key] < hi)]
        name = f"{lo}-{hi - 1}" if hi else f"{lo}+"
        vals = [statistics.mean(r[c] for r in sel if r[c] is not None) if any(r[c] is not None for r in sel)
                else float("nan") for c in cols]
        out.append(f"{name:>12} {len(sel):>3} " + " ".join(f"{v:>10.3f}" for v in vals))
    return "\n".join(out)


def report(rows, model, previews, a):
    have = [r for r in rows if None not in (r["disc"], r["cov"])]
    cols = ["disc", "disc_ctl", "cov", "cov_lines", "cov_ctl"]
    out = ["== distributions"]
    for c in cols + ["lines", "funcs", "sub_roots_10", "sub_roots_20", "call_density"]:
        out.append(f"{c:>12}: {dist([r[c] for r in rows if r[c] is not None])}")
    if have:
        out.append("\n== by lines\n" + bucket_table(have, "lines", LINE_BUCKETS, cols))
        out.append("\n== by funcs\n" + bucket_table(have, "funcs", FUNC_BUCKETS, cols))
        out.append("\n== spearman vs log size")
        for c in ("disc", "cov", "cov_lines"):
            sel = [r for r in have if r[c] is not None]
            out.append(f"{c:>12}: lines {spearman([r['lines'] for r in sel], [r[c] for r in sel]):+.3f}  "
                       f"funcs {spearman([r['funcs'] for r in sel], [r[c] for r in sel]):+.3f}  "
                       f"sub_roots_10 {spearman([r['sub_roots_10'] for r in sel], [r[c] for r in sel]):+.3f}")
        worst = sorted(have, key=lambda r: (r["disc"] + (r["cov_lines"] or 0), -r["lines"]))[:10]
        out.append("\n== worst 10 (disc + cov_lines)")
        for r in worst:
            out.append(f"{r['id']:44} {r['lines']:5}L {r['funcs']:4}f disc {r['disc']:.2f} cov {r['cov']:.2f} "
                       f"covL {r['cov_lines']:.2f}  {r['title']} / {r['summary']}")
        letters = Counter(str(x["letter"]) for r in have for x in r["answers"])
        pos = Counter(x["pos"] for r in have for x in r["answers"])
        agree = sum(r["answers"][0]["pick"] == r["answers"][1]["pick"] for r in have) / len(have)
        both = Counter(sum(x["pick"] == r["id"] for x in r["answers"]) for r in have)
        size = {r["id"]: r["lines"] for r in rows}
        biggest = [max(o, key=lambda i: size.get(i, 0)) for r in have for o in r["orders"]]
        picks = [x["pick"] for r in have for x in r["answers"]]
        true_big = [r for r in have if max(r["orders"][0], key=lambda i: size.get(i, 0)) == r["id"]]
        rest = [r for r in have if r not in true_big]
        out += ["\n== reliability",
                f"answer letters {dict(sorted(letters.items(), key=str))}; true position {dict(sorted(pos.items()))}",
                f"same pick on both shuffles {agree:.3f}; blobs right on 0/1/2 shuffles {dict(sorted(both.items()))}",
                f"size bias: picks the largest candidate {sum(p == b for p, b in zip(picks, biggest)) / len(picks):.3f}"
                f" (chance 0.2); disc when the blob is the largest of its candidates "
                f"{statistics.mean(r['disc'] for r in true_big) if true_big else float('nan'):.3f} (n={len(true_big)}), "
                f"otherwise {statistics.mean(r['disc'] for r in rest) if rest else float('nan'):.3f} (n={len(rest)})",
                "control (random other label) disc mean "
                + (f"{statistics.mean(dc):.3f} (n={len(dc)}, chance 0.2)" if (dc := [r["disc_ctl"] for r in have
                                                                                   if r["disc_ctl"] is not None]) else "n/a")]
        cc = [r for r in have if r["cov_ctl"] is not None and r["cov"] is not None]
        if cc:
            out.append(f"coverage own label {statistics.mean(r['cov'] for r in cc):.3f} vs random label "
                       f"{statistics.mean(r['cov_ctl'] for r in cc):.3f} (n={len(cc)}); own > random on "
                       f"{sum(r['cov'] > r['cov_ctl'] for r in cc)}, equal {sum(r['cov'] == r['cov_ctl'] for r in cc)}")
    multi = [r for r in have if r["funcs"] >= 5 and r.get("same_letter") is not None]
    if multi:
        out.append(f"coverage, blobs of 5+ functions: share of answers on the most common letter "
                   f"{statistics.mean(r['same_letter'] for r in multi):.3f} (n={len(multi)}); "
                   f"unanswered functions {sum(r['unanswered'] or 0 for r in have)}")
    out.append(f"\nmodel: {model.calls} calls, {model.hits} cache hits, {model.secs:.0f}s asking, "
               f"{model.paused}s thermal pauses")
    out.append(f"\n== split preview (split >= {a.split_lines} lines or >= {a.split_funcs} funcs, tiny < {a.tiny_lines})")
    for name, (preview, before, after) in previews.items():
        out.append(f"[{name}] sizes before: {dist(before)}\n[{name}] sizes after:  {dist(after)}")
        out.append(f"[{name}] top-level code blobs before {len(before)}, after {len(after)}; "
                   f"{len(preview['splits'])} split candidates, {sum(bool(s['pieces']) for s in preview['splits'])} cut, "
                   f"{sum(len(s['pieces']) for s in preview['splits'])} pieces; "
                   f"{len(preview['folds'])} tiny, {sum(f['into'] is not None for f in preview['folds'])} folded")
    return "\n".join(out)


def preview_text(preview):
    out = []
    for s in preview["splits"]:
        out.append(f"{s['id']} ({s['label']}) {s['lines']}L/{s['funcs']}f chain {s['chain']} "
                   f"{s['candidates']} candidates -> parent keeps {s['parent']['lines']}L/{s['parent']['funcs']}f")
        for p in s["pieces"]:
            out.append(f"    {p['root']:48} {p['lines']:5}L {p['funcs']:4}f  {', '.join(p['top'])}")
    out.append("")
    for f in preview["folds"]:
        out.append(f"fold {f['id']} ({f['label']}, {f['lines']}L) -> " +
                   (f"{f['into']} ({f['into_label']}, {f['via']})" if f["into"] else "no neighbour"))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("timeline")
    ap.add_argument("--out", required=True, help="output directory (CSV, JSON, report, answer cache)")
    ap.add_argument("--src", help="source tree for first lines (default: the timeline's root)")
    ap.add_argument("--server", default="http://127.0.0.1:8080")
    ap.add_argument("--no-model", action="store_true", help="cached answers only, no server asked")
    ap.add_argument("--hot", type=int, default=85)
    ap.add_argument("--cool", type=int, default=80)
    ap.add_argument("--prompt-chars", type=int, default=4000, help="function-list budget per call (label.py's)")
    ap.add_argument("--split-lines", type=int, default=0, help="split preview: blobs this big (default p90 lines)")
    ap.add_argument("--split-funcs", type=int, default=0, help="split preview: or this many funcs (default p90)")
    ap.add_argument("--tiny-lines", type=int, default=0, help="split preview: fold blobs under this (default p10)")
    ap.add_argument("--measures", default="disc,cov", help="model measures to run: disc, cov or both")
    ap.add_argument("--disc-control-every", type=int, default=1,
                    help="discrimination control on every Nth blob by size only (default 1: all, 0: none)")
    ap.add_argument("--cov-control-every", type=int, default=1,
                    help="coverage control on every Nth blob by size only (default 1: all, 0: none)")
    ap.add_argument("--piece-share", type=float, default=0.10,
                    help="split preview 'dominant': a piece owns at least this share of the blob's lines")
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    step, root = load_final(a.timeline)
    src = a.src or root
    blobs = [b for b in step["blobs"] if b["funcs"]]
    edges = step["edges"]
    succ, blob_of = callgraph(blobs)
    model = Model(a.server, os.path.join(a.out, "answers.json"), a.hot, a.cool, a.no_model)
    if not a.no_model:
        try:
            with urllib.request.urlopen(model.server + "/health", timeout=5):
                pass
        except OSError as e:
            sys.exit(f"specificity.py: llama-server unreachable at {model.server} ({e}); use --no-model")
    labelled = [b for b in blobs if b.get("label")]
    files = {}
    rows = []
    by_size = sorted(labelled, key=lambda b: (b["size"], b["id"]))
    every = lambda n: {b["id"] for i, b in enumerate(by_size) if n and i % n == 0}
    disc_ctl_ids, cov_ctl_ids = every(a.disc_control_every), every(a.cov_control_every)
    t0 = time.monotonic()
    for n, b in enumerate(labelled, 1):
        lab = b["label"]
        row = {"id": b["id"], "kind": b["kind"], "module": b["module"], "title": lab["title"],
               "summary": lab["summary"]}
        row.update(numbers(b, succ, blob_of))
        if "disc" not in a.measures:
            model.offline, offline = True, model.offline
        row["disc"], row["answers"], row["orders"] = discrimination(model, b, lab, labelled, edges)
        if None in [x["letter"] for x in row["answers"]] and model.offline:
            row["disc"] = None
        # control: a random other blob's label that is none of the candidates, on shuffle 0's order
        rng = random.Random(f"{b['id']}:ctl")
        cand_ids = set(row["orders"][0])
        other = rng.choice([o for o in labelled if o["id"] not in cand_ids])
        order = [next(c for c in labelled if c["id"] == i) for i in row["orders"][0]]
        row["ctl_label_of"], row["disc_ctl"] = None, None
        if b["id"] in disc_ctl_ids:
            text = model.ask(DISC_SYSTEM, disc_prompt(other["label"], order), 20)
            ch = parse_choice(text) if text is not None else None
            row["ctl_label_of"] = other["id"]
            row["disc_ctl"] = None if text is None else float(bool(ch) and order[LETTERS.index(ch)]["id"] == b["id"])
        if "disc" not in a.measures:
            model.offline = offline
        if "cov" not in a.measures:
            model.offline, offline = True, model.offline
        items = func_items(b, src, files)
        rivals = [c for c in order if c["id"] != b["id"]]   # the discrimination distractors
        cov = coverage(model, b, lab, rivals, items, a.prompt_chars, f"{b['id']}:cov") or {}
        row["cov"], row["cov_lines"] = cov.get("cov"), cov.get("cov_lines")
        row["uncovered"], row["unanswered"] = cov.get("uncovered"), cov.get("unanswered")
        row["same_letter"] = cov.get("same_letter")
        row["cov_ctl"], row["cov_ctl_label_of"] = None, None
        if b["id"] in cov_ctl_ids:   # the own label replaced by a non-candidate's: share given to that one
            ctl = coverage(model, b, other["label"], rivals, items, a.prompt_chars, f"{b['id']}:cov") or {}
            row["cov_ctl"], row["cov_ctl_label_of"] = ctl.get("cov"), other["id"]
        if "cov" not in a.measures:
            model.offline = offline
        rows.append(row)
        if n % 10 == 0 or n == len(labelled):
            print(f"specificity.py: {n}/{len(labelled)} blobs, {model.calls} calls, {model.hits} hits, "
                  f"{time.monotonic() - t0:.0f}s, paused {model.paused}s", file=sys.stderr)
    model.save()

    sizes = sorted(b["size"] for b in labelled)
    nf = sorted(len(b["funcs"]) for b in labelled)
    pct = lambda xs, p: xs[int(p * (len(xs) - 1))]
    a.split_lines = a.split_lines or pct(sizes, 0.9)
    a.split_funcs = a.split_funcs or pct(nf, 0.9)
    a.tiny_lines = a.tiny_lines or pct(sizes, 0.1)
    previews = {name: split_preview(labelled, edges, succ, blob_of, a.split_lines, a.split_funcs, a.tiny_lines, share)
                for name, share in (("every", 0.0), (f"dominant>={a.piece_share:g}", a.piece_share))}
    with open(os.path.join(a.out, "blobs.json"), "w") as f:
        json.dump(rows, f, indent=1, ensure_ascii=False)
    flat = [k for k in rows[0] if k not in ("answers", "orders", "uncovered")] if rows else []
    with open(os.path.join(a.out, "blobs.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(flat + ["uncovered"])
        for r in rows:
            w.writerow([r[k] for k in flat] + ["; ".join(f"{n}->{t}" for n, t in r["uncovered"] or [])])
    with open(os.path.join(a.out, "split_preview.json"), "w") as f:
        json.dump({"split_lines": a.split_lines, "split_funcs": a.split_funcs, "tiny_lines": a.tiny_lines,
                   **{name: {"before": sorted(bf), "after": sorted(af), **pv}
                      for name, (pv, bf, af) in previews.items()}}, f, indent=1, ensure_ascii=False)
    with open(os.path.join(a.out, "split_preview.txt"), "w") as f:
        f.write("\n\n".join(f"### {name}\n{preview_text(pv)}" for name, (pv, _, _) in previews.items()) + "\n")
    text = report(rows, model, previews, a)
    with open(os.path.join(a.out, "report.txt"), "w") as f:
        f.write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
