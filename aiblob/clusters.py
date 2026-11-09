"""Clusters inside big blobs (SCHEMA.md "v6"): the call communities of a blob's functions, for the page's drill-down.

    blobs, state = clusters.apply(blobs, state)   # areas.Zoner.apply, after merges, cuts and areas; state: {} at first

A blob at the cut size (areas.SPLIT_LINES lines or areas.SPLIT_FUNCS functions, never a tests blob) gets `clusters`:
[{id, cap, emoji, funcs: [qual...]}] sorted by id, its functions in the blob's order. The page draws it as a few
sub-blobs instead of one crowd of functions.

The fresh grouping is the draft's (web/index.html clusters view, variant A), bit for bit: Louvain modularity over the
blob's own calls, undirected, weighted by call sites (resolution RESOLUTION, the page's rng() seeded SEED shuffling the
visit order, levels until nothing moves); functions with no call inside the blob are the loose ends; a single-function
community joins the one it has the most call sites with per sqrt(its lines); then the smallest by lines joins its best
linked one the same way (the loose ends when it has no link) until at most round(PER_ROOT x sqrt(n)) clusters are
left, clamped to MIN_CLUSTERS..MAX_CLUSTERS, loose ends included. Ordered loose ends last, then by lines.

Stability: a blob's clusters keep their ids and members from step to step, as areas.Zoner keeps its areas. The fresh
grouping is recomputed only when the blob's functions, their lines or the calls between them changed, and each fresh
group is matched to the cluster it overlaps most (by lines + 1 per function, greedy; the loose ends always to id 0).
A function new to the blob joins its fresh group's cluster (a group no cluster matched opens one; at the cap, it joins
the cluster it has the most call sites with, then the smallest); a function gone leaves; the others stay where they
are until the fresh grouping is adopted wholesale (matched groups keep their cluster's id, others get new ids), which
needs both: the same fresh grouping has come out on each of the areas.HOLD steps after the one it first came out on
(or half the blob's clustered steps so far, for short ones), and it would change some cluster's members by more than
areas.REGROUP of their lines (symmetric difference over union); or at once, when the clusters would exceed the cap.
A blob that drops under the cut size forgets its clusters. Everything is ordered by position, id or an explicit key,
never by hash, so a run is the same whatever PYTHONHASHSEED.

Captions (guess()): the first word of the members' names, abbreviations expanded (tl -> timeline), weighted by
sqrt(lines) + 1, when it covers half the cluster; else the best-connected member's first two name words; a caption
another cluster already took gets the hub's words. Emoji: the first keyword in EMO matching the caption, else the hub
and the biggest members' names; the loose ends are 'loose ends' 🧷. label.py may later add `label: {title, emoji}` to
a cluster (as it does to blobs): the page shows it over the guess.
"""
import math
import re
from collections import Counter, defaultdict

import areas             # SPLIT_LINES, SPLIT_FUNCS, HOLD, REGROUP, read at call time (tests lower them)

RESOLUTION, SEED, LEVELS, PASSES = 1.3, 7, 12, 30
MIN_CLUSTERS, MAX_CLUSTERS, PER_ROOT = 6, 14, 1.1
LOOSE = 0                # the loose ends' id

ABBR = {'tl': 'timeline', 'fmt': 'format', 'nv': 'nvim', 'ss': 'search', 'sv': 'structure', 'syn': 'syntax',
        'lay': 'layout', 'ts': 'turns', 'hist': 'histogram', 'st': 'blame', 'si': 'inspect', 'tm': 'treemap',
        'an': 'analysis', 'cmp': 'compare', 'ui': 'ui', 'is': 'checks', 'in': 'checks', 'get': 'json', 'put': 'json'}
EMO = [(re.compile(p), e) for p, e in (
    ('viz', '🎬'), ('draw|render|paint|pulse', '🖌️'), ('model|apply|adopt', '🧠'), ('turn', '🔁'),
    ('format|clock|ago|count', '🔤'), ('detail|inspect', '🔎'), ('timeline|time|seek|clock|advance', '⏱️'),
    ('histogram|bins|metric', '📊'), ('font|mono|sans|text|measure', '🔠'), ('actor', '🎭'), ('hover|hit', '👆'),
    ('camera|fit|zoom', '🎥'), ('input|keys|handle|key', '⌨️'), ('file|disk|dir|ensure', '📁'),
    ('search|find|fuzzy|match|names', '🔍'), ('nvim', '📝'), ('syntax|style|highlight|md|rich', '🌈'),
    ('layout|squarify|collide|grid|charge|treemap|sim', '📐'), ('structure', '🗺️'), ('update', '⚙️'),
    ('panel|legend|label|counter|note|balloon', '🏷️'), ('color|heat|kind|alpha', '🎨'), ('node|add|ring', '🔵'),
    ('ingest|event', '📥'), ('json|dec|value|reply|head', '📦'), ('worker|run|job|thread|build', '🧵'),
    ('blame|origin', '🧬'), ('free|cleanup', '🧹'), ('ui|code|row|cell', '🪟'))]
LOOSE_CAP, LOOSE_EMOJI, ANY_EMOJI = 'loose ends', '🧷', '🧩'


def due(b):
    """A blob that gets clusters: the cut size (areas.py), code, not tests."""
    fs = b.get('funcs') or ()
    return bool(fs) and b.get('kind') != 'tests' and (b['size'] >= areas.SPLIT_LINES or len(fs) >= areas.SPLIT_FUNCS)


def cap_of(n):
    """The most clusters (loose ends included) for n functions: round(PER_ROOT x sqrt(n)) in MIN..MAX."""
    return min(MAX_CLUSTERS, max(MIN_CLUSTERS, math.floor(math.sqrt(n) * PER_ROOT + 0.5)))


# --- the page's rng() and the draft's Louvain, bit for bit ----------------------------------------------------------

def _i32(x):
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x & 0x80000000 else x


def rng(seed):
    """web/index.html rng() (mulberry32): the same floats in [0, 1) for the same seed."""
    s = seed

    def nxt():
        nonlocal s
        s = _i32(s + 0x6D2B79F5)
        t = _i32((s ^ ((s & 0xFFFFFFFF) >> 15)) * (1 | s))
        t = _i32(t + _i32((t ^ ((t & 0xFFFFFFFF) >> 7)) * (61 | t))) ^ t
        return ((t ^ ((t & 0xFFFFFFFF) >> 14)) & 0xFFFFFFFF) / 4294967296
    return nxt


def louvain(adj, seed=SEED, gamma=RESOLUTION):
    """adj: per node {node: weight}, symmetric, no self loops -> the community of each node (0..k-1). One level of
    local moves in a seeded random order, then the communities become nodes, until a level moves nothing."""
    n, A, self_, member = len(adj), [dict(m) for m in adj], [0] * len(adj), list(range(len(adj)))
    R = rng(seed)
    for _ in range(LEVELS):
        k = []
        for i in range(n):
            s = 2 * self_[i]
            for w in A[i].values():
                s += w
            k.append(s)
        m2 = 0
        for x in k:
            m2 += x
        if not m2:
            break
        c, tot, order = list(range(n)), list(k), list(range(n))
        for i in range(n - 1, 0, -1):
            j = math.floor(R() * (i + 1))
            order[i], order[j] = order[j], order[i]
        improved = False
        for _ in range(PASSES):
            moved = 0
            for i in order:
                ci = c[i]
                tot[ci] -= k[i]
                w = {}
                for j, wt in A[i].items():
                    if j != i:
                        w[c[j]] = w.get(c[j], 0) + wt
                best, bg = ci, w.get(ci, 0) - gamma * tot[ci] * k[i] / m2
                for cc, wt in w.items():
                    g = wt - gamma * tot[cc] * k[i] / m2
                    if g > bg + 1e-9:
                        bg, best = g, cc
                c[i] = best
                tot[best] += k[i]
                moved += best != ci
            if not moved:
                break
            improved = True
        if not improved:
            break
        ren = {}
        for x in c:
            if x not in ren:
                ren[x] = len(ren)
        n2 = len(ren)
        A2, self2 = [{} for _ in range(n2)], [0] * n2
        for i in range(n):
            ci = ren[c[i]]
            self2[ci] += self_[i]
            for j, wt in A[i].items():
                cj = ren[c[j]]
                if ci == cj:
                    self2[ci] += wt / 2
                else:
                    A2[ci][cj] = A2[ci].get(cj, 0) + wt
        member = [ren[c[x]] for x in member]
        if n2 == n:
            break
        n, A, self_ = n2, A2, self2
    return member


def graph(quals, inner):
    """quals: the blob's functions in order; inner: per function the quals it calls inside the blob, one per call
    site -> (adj, deg): call sites between two functions both ways, as the page's prep() builds them (a function's
    callees in first-call order), and per function its distinct callees + distinct callers."""
    idx = {q: i for i, q in enumerate(quals)}
    adj, ncall = [{} for _ in quals], [0] * len(quals)
    out = []
    for i, ts in enumerate(inner):
        m = {}
        for t in ts:
            j = idx[t]
            if j != i:
                m[j] = m.get(j, 0) + 1
        out.append(m)
    for i, m in enumerate(out):
        for j, n in m.items():
            adj[i][j] = adj[i].get(j, 0) + n
            adj[j][i] = adj[j].get(i, 0) + n
            ncall[j] += 1
    return adj, [len(out[i]) + ncall[i] for i in range(len(quals))]


def fresh(lines, adj, deg):
    """The draft's clusters() over one blob -> [(loose, [node...] biggest first)], loose ends last, then by lines."""
    N, mem = len(lines), louvain(adj)
    G = {}
    for i in range(N):
        G.setdefault(f'c{mem[i]}' if deg[i] else 'loose', []).append(i)

    def size(ids):
        return sum(lines[i] for i in ids)

    def link(a, b):
        sb, w = set(b), 0
        for i in a:
            for j, wt in adj[i].items():
                if j in sb:
                    w += wt
        return w
    top = cap_of(N)
    for _ in range(400):
        keys = [k for k in G if k != 'loose']
        small = [k for k in keys if len(G[k]) < 2]
        g = small[0] if small else None
        if g is None and len(keys) + ('loose' in G) > top:
            keys.sort(key=lambda k: size(G[k]))
            g = keys[0]
        if g is None:
            break
        best, bw = None, 0
        for h in keys:
            if h != g:
                w = link(G[g], G[h]) / math.sqrt(max(1, size(G[h])))
                if w > bw:
                    bw, best = w, h
        G.setdefault(best or 'loose', []).extend(G.pop(g))
    out = [(k == 'loose', sorted(ids, key=lambda i: -lines[i])) for k, ids in G.items()]
    out.sort(key=lambda t: (t[0], -size(t[1])))
    return out


# --- captions -------------------------------------------------------------------------------------------------------

def words(name):
    """A function name's words, lower case: split at _ and other non-alphanumerics, and at camelCase humps."""
    return [w.lower() for p in re.split(r'[^A-Za-z0-9]+', name) if p for w in re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', p).split()]


def word(w):
    return ABBR.get(w, w)


def guess(groups, names, lines, adj):
    """groups: [(loose, [node...] biggest first)] in list order (loose ends last, then by lines) -> [(cap, emoji)]."""
    used, out = set(), []
    for loose, ids in groups:
        score = {}
        for i in ids:
            t = word((words(names[i]) or [''])[0])
            score[t] = score.get(t, 0) + math.sqrt(lines[i]) + 1
        top = sorted(score.items(), key=lambda kv: -kv[1])
        total = 0
        for _, v in top:
            total += v
        inside = set(ids)

        def deg(i):
            d = 0
            for j, w in adj[i].items():
                if j in inside:
                    d += w
            return d * math.sqrt(lines[i])
        hub = ids[0]
        for i in ids:
            if deg(i) > deg(hub):
                hub = i
        hw = [word(w) for w in words(names[hub])]
        cap = top[0][0] if top[0][1] >= total * 0.5 else ' '.join(hw[:2])
        if cap in used:
            cap = ' '.join(hw[:2]) if hw and top[0][0] == hw[0] else top[0][0] + ' · ' + (hw[0] if hw else '')
        if cap in used:
            cap = ' '.join(hw)
        if loose:
            cap = LOOSE_CAP
        used.add(cap)
        text = ' '.join([cap, names[hub].replace('_', ' ')] + [names[i].replace('_', ' ') for i in ids[:4]])
        emoji = LOOSE_EMOJI if loose else (next((e for r, e in EMO if r.search(cap)), None) or
                                          next((e for r, e in EMO if r.search(text)), ANY_EMOJI))
        out.append((cap, emoji))
    return out


# --- per blob, step to step -----------------------------------------------------------------------------------------

def held(s):
    """The blob's current fresh grouping came out HOLD or more of its clustered steps ago (or half of them)."""
    return s['k'] - s['since'] >= min(areas.HOLD, s['k'] // 2)


def regroup(s, quals, lines, inner):
    """s['assign'] (qual -> cluster id) for this step's quals from the last one and s['fresh'] (see module doc)."""
    fresh = s['fresh']
    wt = lambda qs: sum(lines[q] + 1 for q in qs)
    old = {q: c for q, c in (s['assign'] or {}).items() if q in lines}
    members = defaultdict(set)
    for q, c in old.items():
        members[c].add(q)
    match, used = {}, set()
    for gi, (loose, _) in enumerate(fresh):
        if loose:
            match[gi] = LOOSE
            used.add(LOOSE)
    pairs = sorted(((wt(set(g) & members[a]), a, gi) for gi, (loose, g) in enumerate(fresh) if not loose
                    for a in sorted({old[q] for q in g if q in old} - {LOOSE})), key=lambda t: (-t[0], t[1], t[2]))
    for _, a, gi in pairs:
        if gi not in match and a not in used:
            match[gi] = a
            used.add(a)
    group_of = {q: gi for gi, (_, g) in enumerate(fresh) for q in g}
    moved = False
    if held(s):
        s['ripe'] = True
        for a in sorted(members):
            gi = next((g for g, x in match.items() if x == a), None)
            now = {q for q in fresh[gi][1] if q in old} if gi is not None else set()
            if wt(members[a] ^ now) > areas.REGROUP * wt(members[a] | now):
                moved = True
                break

    def cid(gi):               # a group no cluster matched gets a new id the first time a function needs one
        if gi not in match:
            match[gi] = s['next']
            s['next'] += 1
        return match[gi]
    top = cap_of(len(quals))
    if not old:
        moved = True
    if not moved:
        assign, in_use = {}, set(old.values())
        for q in quals:
            gi = group_of[q]
            if q in old:
                assign[q] = old[q]
            elif gi in match or len(in_use) < top:
                assign[q] = cid(gi)
                in_use.add(assign[q])
            else:              # at the cap: the cluster it has the most call sites with, else the smallest
                here, tie, size = {**old, **assign}, Counter(), Counter()
                for p, c in here.items():
                    size[c] += lines[p] + 1
                    tie[c] += inner[p].count(q)
                for t in inner[q]:
                    if t in here:
                        tie[here[t]] += 1
                assign[q] = min(in_use, key=lambda c: (-tie[c], size[c], c))
        moved = len(set(assign.values())) > top
    if moved:
        for gi in range(len(fresh)):
            cid(gi)
        assign = {q: cid(group_of[q]) for q in quals}
    s['assign'] = assign


def apply(blobs, state):
    """-> (blobs with `clusters` where due, the new state). state: what the last call returned ({} at first); it is
    never written to (a forked Zoner shares it)."""
    out, new = [], {}
    for b in blobs:
        if not due(b):
            out.append(b)
            continue
        fs = b['funcs']
        quals = [f['qual'] for f in fs]
        lines = {f['qual']: f['lines'] for f in fs}
        mine = set(quals)
        inner = {f['qual']: tuple(c['to'] for c in f['calls'] if c['to'] in mine) for f in fs}
        sig = (tuple(quals), tuple(lines[q] for q in quals), tuple(inner[q] for q in quals))
        prev = state.get(b['id'])
        s = dict(prev) if prev else {'k': -1, 'next': 1, 'assign': None, 'fresh': None, 'since': 0, 'ripe': False,
                                     'sig': None, 'out': None}
        s['k'] += 1
        adj = deg = None
        if sig != s['sig']:
            s['sig'] = sig
            adj, deg = graph(quals, sig[2])
            groups = [(loose, tuple(quals[i] for i in ids)) for loose, ids in fresh(sig[1], adj, deg)]
            if groups != s['fresh']:
                s['fresh'], s['since'], s['ripe'] = groups, s['k'], False
            regroup(s, quals, lines, inner)
        elif not s['ripe'] and held(s):
            regroup(s, quals, lines, inner)
        if adj is None:
            adj, deg = graph(quals, sig[2])
        by = {}
        for i, q in enumerate(quals):
            by.setdefault(s['assign'][q], []).append(i)
        L = sig[1]
        order = sorted(by, key=lambda c: (c == LOOSE, -sum(L[i] for i in by[c]), c))
        groups = [(c == LOOSE, sorted(by[c], key=lambda i: -L[i])) for c in order]
        names = [f['name'] for f in fs]
        caps = dict(zip(order, guess(groups, names, L, adj)))
        table = [{'id': c, 'cap': caps[c][0], 'emoji': caps[c][1], 'funcs': [quals[i] for i in by[c]]} for c in sorted(by)]
        if table != s['out']:
            s['out'] = table
        new[b['id']] = s
        out.append(dict(b, clusters=s['out']))
    return out, new
