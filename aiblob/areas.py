"""Merges, cuts and areas over one step's analysis (SCHEMA.md "v5"), and the clusters of big blobs ("v6").

    zoner = areas.Zoner()                      # one per session: area ids, cluster ids and the cut memo live across steps
    result = zoner.apply(analyze(snapshot))    # blobs cut / folded, edges recomputed, `areas` table added, `clusters`

The blob map a step shows is the analyzer's blobs with two changes (both off with Zoner(smart=False)):
- cuts: a blob of SPLIT_LINES lines or SPLIT_FUNCS functions (never a tests blob) gives up callee-rooted pieces.
  Ownership is the dominator tree over the blob's own calls, read like CLang.split_entry: from the blob's starts
  (its root function, functions nothing in the blob calls, functions another blob calls), the candidates are the
  start-level children that at most one other function calls; past one that owns more than half of the blob, one
  more level down (descending into the blob's own root function is free, as `main` is). A candidate whose owned
  functions hold at least PIECE_SHARE of the blob's lines and PIECE_MIN lines, with a root of ROOT_MIN lines or
  more, becomes a piece: blob id `<parent>><root qual>`, name the root function's name, the parent's kind and
  emoji, module the root function's module, `cut` the parent id. The parent keeps the rest.
- merges: a blob under TINY lines (never a route, entry, error handler or hook: those are the entry points a
  small app's map is made of; never a cut parent) folds into the biggest blob of its own module (a C header with
  its .c), else into the blob it has the most call sites with, then the most shared data symbols; tests only into
  tests. Smallest first, so a blob can take a fold before it folds itself. The receiving blob lists it in
  `merged` ({id, name, emoji, kind, lines, funcs}), flattened.
Edges are then recounted over the new blobs exactly as analyze() counts them (call sites, shared data symbols,
file literals); what analyze() counted beyond the functions' own calls and refs (an abort's edge to an error
handler, module-level code) stays with the blob that holds the original blob's id, or the one it folded into.
A ref edge of a cut parent is split by the functions whose `refs` name the file, one literal each, the rest
staying with the parent; every part keeps the parent's `via`.

Areas group the code blobs by module (a header with its .c): modules of one top directory are joined, the two
with the most call sites between them per line of the smaller first, while the join holds at most AREA_CAP
blobs, down to min(CODE_AREAS, ceil(code blobs / AREA_CAP)) areas plus one per further top directory; past
MAX_AREAS, joins cross top directories, and when the cap blocks every join the cap gives way, so the top view never
holds more than MAX_AREAS code areas. Tests (tests blobs, modules under a test directory) are area `tests`,
non-code files of a data format (`fmt` in analyze.DATA_FMTS: json, csv, toml ...) `data`, markdown and html nothing
references `docs`, the other non-code files (referenced html: templates) `files`. Stability (Zoner): a module keeps its area id from step to step. The grouping is
recomputed only when the set of modules or the call sites between them changed, and each fresh group is matched
to the area it overlaps most (by lines + 1 per module, greedy). New modules move in at once, to their group's
area (or a new one); existing modules move only when the fresh grouping is adopted wholesale (matched groups
keep their area's id, others get new ids), which needs both: the same fresh grouping has come out again
on each of the HOLD steps after the one it first came out on (HOLD + 1 steps in all; or half the session so far,
for short ones), so it is adopted at the earliest on step since + HOLD (an edit that flips the grouping and a later one
that flips it back move nothing), and it would change some area's existing modules by more than REGROUP of
their lines (symmetric difference over union). The cap holds between regroups too: once MAX_AREAS code areas
are in use, a new module whose group no area matches joins the area it shares the most call sites with (then the
smallest) instead of opening one, and an assignment still over MAX_AREAS adopts the fresh grouping at once.
Captions are word frequency over the member blobs' titles (label title, else name; an unlabelled piece is
skipped), count x sqrt(lines), generic words skipped, a second word when it scores at least half as much;
faces are the top 3 member emojis by lines. caption_table() recomputes them, e.g. after label.py sets labels.
Last, a blob of 12+ functions in 3+ call groups gets `clusters`, the call communities of its functions (clusters.py; off with
Zoner(smart=False), like merges and cuts).
"""
import heapq
import math
import re
from collections import Counter, defaultdict

import clusters
from analyze import ASSET_FMTS, DATA_FMTS, DOC_FMTS, shared

SPLIT_LINES, SPLIT_FUNCS = 500, 50
PIECE_SHARE, PIECE_MIN, ROOT_MIN = 0.10, 20, 3
TINY = 20
NO_FOLD = frozenset({'route', 'entry', 'error-handler', 'hook'})
CODE_AREAS, AREA_CAP, MAX_AREAS = 7, 9, 12
REGROUP, HOLD = 0.5, 40
TEST_TOPS = frozenset({'tests', 'test', 'testdata', '__tests__', 'spec'})
EDGE_ORDER = {'main': 0, 'error': 1, 'data': 2, 'ref': 3}


def stem(mod):
    return mod[:-2] if mod.endswith('_h') else mod


def top_of(mod):
    return mod.split('.', 1)[0] if '.' in mod else '.'


# --- ownership: dominators over a blob's own calls ---------------------------------------------------------------

def dominators(nodes, succ, starts):
    """Immediate dominators over `nodes` (calls leaving `nodes` ignored) from a virtual root None linked to
    `starts`; a node no start reaches becomes a start too. -> {node: idom (None = the virtual root)}."""
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
            if idom.get(n, '?') != new:
                idom[n] = new
                changed = True
    del idom[None]
    return idom


def pieces_of(bid, quals, lines, succ, outer):
    """The pieces a big blob gives up: [(root qual, frozenset of owned quals)], biggest first (see module doc).
    quals: the blob's functions in order; succ: qual -> callee quals inside the blob; outer: functions of the
    blob another blob calls."""
    nodes = set(quals)
    inner = {m for q in quals for m in succ[q] if m != q}
    starts = ([bid] if bid in nodes else []) + [q for q in quals if q not in inner or q in outer]
    idom = dominators(nodes, succ, starts)
    kids = defaultdict(list)
    for n, d in idom.items():
        kids[d].append(n)
    for k in kids:
        kids[k].sort(key=lambda q: (q is None, q or ''))
    owned = {}
    for n in nodes:
        out, stack = set(), [n]
        while stack:
            m = stack.pop()
            out.add(m)
            stack += kids[m]
        owned[n] = out
    callers = defaultdict(set)
    for q in quals:
        for m in succ[q]:
            if m != q:
                callers[m].add(q)
    root = bid if bid in nodes else None
    parent, chain, kept, extra = None, [], [], 0
    while True:
        direct = set(starts) if parent is None else set(succ[parent])
        level = [c for c in kids[parent] if c in direct and len(callers[c] - {parent}) < 2]
        if not level:
            break
        big = min(level, key=lambda c: (-len(owned[c]), c))
        if len(owned[big]) > 0.5 * len(nodes):
            free = big == root and not chain
            if not free and extra >= 1:
                break
            extra += not free
            chain.append(big)
            kept += [c for c in level if c != big]
            parent = big
            continue
        break
    size = sum(lines[q] for q in quals)
    out = []
    for c in sorted(set(kept + level)):
        o = owned[c]
        n = sum(lines[q] for q in o)
        if n >= PIECE_MIN and lines[c] >= ROOT_MIN and n >= PIECE_SHARE * size:
            out.append((c, frozenset(o), n))
    out.sort(key=lambda p: (-p[2], p[0]))
    return [(c, o) for c, o, _ in out]


# --- edges over the new blobs, counted as analyze() counts them --------------------------------------------------

def data_edges(blobs, only=None):
    """analyze()'s data edges over `blobs` (shared data symbols per unordered pair); with `only`, just the pairs
    with at least one blob of that set."""
    index = defaultdict(set)
    for b in blobs:
        for d in b['data']:
            if shared(d):
                index[d].add(b['id'])
    pairs = defaultdict(set)

    def link(xs, ys, via):
        for x in xs:
            for y in ys:
                if x != y and (only is None or x in only or y in only):
                    pairs[tuple(sorted((x, y)))].add(via)
    index = {d: sorted(bs) for d, bs in sorted(index.items())}
    for d, bs in index.items():
        link(bs, bs, d)
        if d.startswith('ctx:') and '.' not in d:
            for d2, bs2 in index.items():
                if d2.startswith(d + '.'):
                    link(bs, bs2, d2)
    return [{'a': a, 'b': b, 'kind': 'data', 'n': len(ds), 'via': sorted(ds)} for (a, b), ds in pairs.items()]


def recount(old_blobs, old_edges, new_blobs):
    """The edges of new_blobs (old_blobs with functions moved between them) as analyze() would count them. Only
    the edges of blobs that lost or gained functions (`touched`) are recounted; the others are kept as they are."""
    old_of = {f['qual']: b['id'] for b in old_blobs for f in b['funcs']}
    new_of = {f['qual']: b['id'] for b in new_blobs for f in b['funcs']}
    ids = {b['id'] for b in new_blobs}
    home = {b['id']: b['id'] if b['id'] in ids or not b['funcs'] else new_of[b['funcs'][0]['qual']] for b in old_blobs}
    touched = set()
    for q, a in new_of.items():
        if old_of[q] != a:
            touched.update((a, old_of[q]))
    calls_old, calls_new = Counter(), Counter()
    for b in old_blobs:
        mine = b['id'] in touched
        for f in b['funcs']:
            q = f['qual']
            for c in f['calls']:
                t = c['to']
                ot = old_of.get(t)
                if ot is None or not (mine or ot in touched):
                    continue
                k = 'error' if c['error'] else 'main'
                if old_of[q] != ot:
                    calls_old[(old_of[q], ot, k)] += 1
                if new_of[q] != new_of[t]:
                    calls_new[(new_of[q], new_of[t], k)] += 1
    calls = Counter(calls_new)
    refs = {}
    by_id = {b['id']: b for b in old_blobs}
    kept = []
    for e in old_edges:
        if e['a'] not in touched and e['b'] not in touched:
            kept.append(e)
        elif e['kind'] in ('main', 'error'):
            rest = e['n'] - calls_old[(e['a'], e['b'], e['kind'])]
            a, b = home[e['a']], home[e['b']]
            if rest and a != b:
                calls[(a, b, e['kind'])] += rest
        elif e['kind'] == 'ref' and not e['b'].startswith('file:'):     # a page's <script src>: to its module's blob
            r = refs.setdefault((home.get(e['a'], e['a']), home.get(e['b'], e['b'])), [0, set()])
            r[0] += e['n']
            r[1].update(e.get('via', ()))
        elif e['kind'] == 'ref':
            src = by_id.get(e['a'])
            path = e['b'][len('file:'):]
            per = Counter(new_of[f['qual']] for f in (src['funcs'] if src else ()) if path in f.get('refs', ()))
            rest = e['n'] - sum(per.values())
            if rest:
                per[home.get(e['a'], e['a'])] += rest
            for a, n in per.items():
                if n > 0:
                    r = refs.setdefault((a, e['b']), [0, set()])
                    r[0] += n
                    r[1].update(e.get('via', ()))
    out = kept + [{'a': a, 'b': b, 'kind': k, 'n': n} for (a, b, k), n in calls.items() if n > 0]
    out += data_edges(new_blobs, touched)
    out += [{'a': a, 'b': b, 'kind': 'ref', 'n': n, 'via': sorted(via)} for (a, b), (n, via) in refs.items()]
    out.sort(key=lambda e: (EDGE_ORDER[e['kind']], e['a'], e['b']))
    return out


def summed(blob, funcs, **extra):
    """blob with `funcs` in place of its functions and the sums analyze() derives from them recomputed."""
    out = dict(blob, funcs=funcs)
    out.update(size=sum(f['lines'] for f in funcs), complexity=sum(f['branches'] for f in funcs),
               errors=sum(f['errors'] for f in funcs), data=sorted({d for f in funcs for d in f['data']}), **extra)
    return out


# --- captions -----------------------------------------------------------------------------------------------------

GENERIC = frozenset('''engine manager handler data function functions utility helper new code system controller
startup cleanup creation test tests runner renderer the and for of to with from into hub orphan module main post
get put patch int str'''.split())


def caption(members):
    """members: [(title, lines)] -> 1-2 words: the title word with the best (members using it) x sqrt(their
    lines), generic words skipped; a second word when it scores at least half the first."""
    count, lines = Counter(), Counter()
    for title, n in members:
        for w in set(re.findall(r'[a-z]+', title.lower())):
            if w not in GENERIC and (len(w) > 2 or w == 'ui'):
                count[w] += 1
                lines[w] += n
    score = sorted(((count[w] * math.sqrt(lines[w]), w) for w in count), key=lambda t: (-t[0], t[1]))
    if not score:
        return 'Misc'
    words = [score[0][1]] + [w for sc, w in score[1:2] if sc >= score[0][0] / 2]
    return ' · '.join('UI' if w == 'ui' else w.capitalize() for w in words)


FIXED = {'tests': 'Tests', 'files': 'Files', 'data': 'Data', 'docs': 'Docs', 'assets': 'Assets'}


def file_area(b, used_html=frozenset()):
    """The area of a file / text blob: `data` by a data `fmt`; `assets` for images and fonts; `docs` for markdown, and
    for html the app does not use (used_html, app_html(): the html code references, a template, and what those pages link in turn,
    e.g. the base template they extend, are part of the app: `files`); else `files`."""
    fmt = b.get('fmt')
    if fmt in DATA_FMTS:
        return 'data'
    if fmt in ASSET_FMTS:
        return 'assets'
    return 'docs' if fmt in DOC_FMTS and not (fmt == 'html' and b['id'] in used_html) else 'files'


def app_html(blobs, edges):
    """The html file blobs code uses: those a code blob's `ref` edge names, and the html those link, transitively.
    An html page only other files link (a README's link to a demo page, pages linking each other) is not."""
    html = {b['id'] for b in blobs if b.get('fmt') == 'html'}
    links = defaultdict(list)
    stack = []
    for e in edges:
        if e['kind'] == 'ref' and e['b'] in html:
            if e['a'].startswith('file:'):
                links[e['a']].append(e['b'])
            else:
                stack.append(e['b'])
    seen = set(stack)
    while stack:
        for t in links.get(stack.pop(), ()):
            if t not in seen:
                seen.add(t)
                stack.append(t)
    return seen


def caption_table(blobs, ids=None):
    """[{id, caption, faces}] sorted by id for the areas of `blobs` (each with an `area`), from their labels when
    they have them. ids: the areas to list (default: those the blobs name)."""
    by_id = {b['id']: b for b in blobs}
    members = defaultdict(list)
    for b in blobs:
        if 'area' in b:
            members[b['area']].append(b)
    out = []
    for aid in sorted(ids if ids is not None else members):
        words, faces = [], Counter()
        for b in members.get(aid, ()):
            lab = b.get('label') or {}
            parent = by_id.get(b.get('cut')) or {}
            emoji = lab.get('emoji') or (parent.get('label') or {}).get('emoji') or b.get('emoji')
            if emoji:
                faces[emoji] += b['size']
            title = lab.get('title') or (None if 'cut' in b else b['name'])
            if title:
                words.append((title, b['size']))
        out.append({'id': aid, 'caption': FIXED.get(aid) or caption(words),
                    'faces': [e for e, _ in sorted(faces.items(), key=lambda t: (-t[1], t[0]))[:3]]})
    return out


# --- the per-session state ----------------------------------------------------------------------------------------

def group(stems, L, C, W, cap=AREA_CAP):
    """Agglomerate module stems into areas (see module doc). L: stem -> lines, C: stem -> blobs, W: (a, b) ->
    call sites. -> [sorted tuple of stems], deterministic. Each join takes the pair with the smallest
    (-call sites / lines of the smaller, blobs of both, their stems), from a heap of pair scores (a join only adds
    the new group's pairs; pairs of a group that is gone are skipped as they come up): O(n^2 log n) in modules.
    Three passes: within a top directory under the blob cap down to the target, across top directories under the
    cap down to MAX_AREAS, then across without the cap down to MAX_AREAS (the top view never holds more)."""
    G, of = {}, {}
    for i, s in enumerate(sorted(stems)):
        G[i] = {'stems': (s,), 'L': L[s], 'C': C[s], 'top': top_of(s)}
        of[s] = i
    S = defaultdict(Counter)
    for (a, b), n in W.items():
        if a in of and b in of:
            S[of[a]][of[b]] += n
            S[of[b]][of[a]] += n
    nxt = len(G)
    units = sum(C[s] for s in stems)
    tops = len({g['top'] for g in G.values()})
    target = min(MAX_AREAS, min(CODE_AREAS, max(1, math.ceil(units / cap))) + tops - 1)
    for cross, capped, limit in ((False, True, target), (True, True, MAX_AREAS), (True, False, MAX_AREAS)):
        if len(G) <= limit:
            continue

        def score(i, j):          # None: the pass does not allow this join
            A, B = G[i], G[j]
            if (not cross and A['top'] != B['top']) or (capped and A['C'] + B['C'] > cap):
                return None
            return (-S[i].get(j, 0) / max(1, min(A['L'], B['L'])), A['C'] + B['C'],
                    tuple(sorted(A['stems'] + B['stems'])), i, j)
        ids = sorted(G)
        heap = [e for x, i in enumerate(ids) for j in ids[x + 1:] for e in (score(i, j),) if e]
        heapq.heapify(heap)
        while len(G) > limit:
            while heap and (heap[0][3] not in G or heap[0][4] not in G):
                heapq.heappop(heap)
            if not heap:
                break
            *_, i, j = heapq.heappop(heap)
            A, B = G.pop(i), G.pop(j)
            G[nxt] = {'stems': tuple(sorted(A['stems'] + B['stems'])), 'L': A['L'] + B['L'], 'C': A['C'] + B['C'],
                      'top': A['top'] if A['top'] == B['top'] else '*'}
            row = S.pop(i, Counter()) + S.pop(j, Counter())
            row.pop(i, None)
            row.pop(j, None)
            S[nxt] = row
            for k, n in row.items():
                S[k].pop(i, None)
                S[k].pop(j, None)
                S[k][nxt] = n
            for k in list(G)[:-1]:
                e = score(k, nxt)
                if e:
                    heapq.heappush(heap, e)
            nxt += 1
    return sorted((g['stems'] for g in G.values()), key=lambda s: (-sum(L[x] for x in s), s))


class Zoner:
    """One per session (timeline._steps): applies merges, cuts and areas to each step's analysis in order."""

    def __init__(self, smart=True):
        self.smart = smart
        self.assign, self.sig, self.next_id = {}, None, 1
        self.fresh, self.since, self.step = None, 0, 0   # the latest fresh grouping, the step it first came out
        self.ripe = False                # that grouping has held long enough and was weighed against the areas
        self.cut_memo = {}               # blob id -> (signature, pieces)
        self.succ_memo = {}              # qual -> (its `calls` list, the quals it names): analyze() reuses the list
        self.regroups = 0                # times the fresh grouping was adopted (reassigning existing modules)
        self.cl_state = {}               # clusters.apply()'s state: replaced each step, never written to (fork())

    def apply(self, result):
        blobs = result['blobs']
        mod_of_file = {v['file']: m for m, v in result.get('modules', {}).items()}
        new = self.reshape(blobs, mod_of_file) if self.smart else blobs
        edges = recount(blobs, result['edges'], new) if new is not blobs else result['edges']
        new = self.areas(new, mod_of_file, app_html(new, edges))
        if self.smart:
            new, self.cl_state = clusters.apply(new, self.cl_state)
        return dict(result, blobs=new, edges=edges, areas=caption_table(new))

    # merges and cuts
    def reshape(self, blobs, mod_of_file):
        fn = {f['qual']: f for b in blobs for f in b['funcs']}
        blob_of = {q: b['id'] for b in blobs for f in b['funcs'] for q in (f['qual'],)}
        succ, old = {}, self.succ_memo        # callee quals; a callee no longer in the app is never looked up
        for q, f in fn.items():
            hit = old.get(q)
            if hit is None or hit[0] is not f['calls']:
                hit = (f['calls'], tuple(dict.fromkeys(c['to'] for c in f['calls'])))
            succ[q] = hit
        self.succ_memo = succ
        succ = {q: hit[1] for q, hit in succ.items()}
        called_by = defaultdict(set)
        for q, ms in succ.items():
            for m in ms:
                called_by[m].add(q)
        units, cut_parents, changed = [], set(), False
        for b in blobs:
            if not b['funcs']:
                units.append(b)
                continue
            quals = [f['qual'] for f in b['funcs']]
            pieces = []
            if b['kind'] != 'tests' and (b['size'] >= SPLIT_LINES or len(quals) >= SPLIT_FUNCS):
                pieces = self.cut(b, quals, fn, succ, called_by, blob_of)
            if not pieces:
                units.append(b)
                continue
            changed = True
            taken = set().union(*(o for _, o in pieces))
            units.append(summed(b, [f for f in b['funcs'] if f['qual'] not in taken]))
            cut_parents.add(b['id'])
            for c, o in pieces:
                root = fn[c]
                units.append(summed({'id': f"{b['id']}>{c}", 'name': root['name'], 'kind': b['kind'],
                                     'module': mod_of_file.get(root['file'], c.rsplit('.', 1)[0]), 'cut': b['id'],
                                     'emoji': b['emoji']},
                                    [f for f in b['funcs'] if f['qual'] in o]))
        folded = self.fold(units, cut_parents, fn, succ, called_by)
        return folded if changed or folded is not units else blobs

    def cut(self, b, quals, fn, succ, called_by, blob_of):
        mine = set(quals)
        inner = {q: tuple(m for m in succ[q] if m in mine) for q in quals}
        outer = frozenset(q for q in quals if any(blob_of[p] != b['id'] for p in called_by[q]))
        sig = (tuple((q, fn[q]['lines'], inner[q]) for q in quals), outer)
        hit = self.cut_memo.get(b['id'])
        if hit is None or hit[0] != sig:
            hit = (sig, pieces_of(b['id'], quals, {q: fn[q]['lines'] for q in quals}, inner, outer))
            self.cut_memo[b['id']] = hit
        return hit[1]

    def fold(self, units, cut_parents, fn, succ, called_by):
        code = [u for u in units if u['funcs']]
        size = {u['id']: u['size'] for u in code}
        tests = {u['id']: u['kind'] == 'tests' for u in code}
        where = {f['qual']: u['id'] for u in code for f in u['funcs']}
        alive = {u['id']: u for u in code}
        sym_units = defaultdict(set)
        syms = {}
        for u in code:
            syms[u['id']] = {d for d in u['data'] if shared(d)}
            for d in syms[u['id']]:
                sym_units[d].add(u['id'])
        merged = defaultdict(list)
        funcs = {u['id']: list(u['funcs']) for u in code}
        gone = set()
        for u in sorted(code, key=lambda u: (u['size'], u['id'])):
            uid = u['id']
            if size[uid] >= TINY or u['kind'] in NO_FOLD or uid in cut_parents:
                continue
            live = [v for v in alive.values() if v['id'] != uid and tests[v['id']] == tests[uid]]
            same = [v for v in live if stem(v['module']) == stem(u['module'])]
            if same:
                into = min(same, key=lambda v: (-size[v['id']], v['id']))['id']
            else:
                w = Counter()
                for f in funcs[uid]:
                    for m in succ[f['qual']]:
                        if m in where:
                            w[where[m]] += 1000
                    for p in called_by[f['qual']]:
                        w[where[p]] += 1000
                for d in syms[uid]:
                    for v in sym_units[d]:
                        w[v] += 1
                cands = [v for v in w if v != uid and v in alive and tests[v] == tests[uid]]
                if not cands:
                    continue
                into = min(cands, key=lambda v: (-w[v], -size[v], v))
            own = [f for f in funcs[uid] if all(f['qual'] not in m['funcs'] for m in merged[uid])]
            merged[into].append({'id': uid, 'name': u['name'], 'emoji': u['emoji'], 'kind': u['kind'],
                                 'lines': sum(f['lines'] for f in own), 'funcs': [f['qual'] for f in own]})
            merged[into] += merged.pop(uid, [])
            funcs[into] += funcs[uid]
            size[into] += size[uid]
            for f in funcs[uid]:
                where[f['qual']] = into
            for d in syms[uid]:
                sym_units[d].discard(uid)
                sym_units[d].add(into)
            syms[into] |= syms[uid]
            del alive[uid]
            gone.add(uid)
        if not gone:
            return units
        out = []
        for u in units:
            if u['id'] in gone:
                continue
            out.append(summed(u, funcs[u['id']], merged=merged[u['id']]) if merged.get(u['id']) else u)
        return out

    # areas
    def areas(self, blobs, mod_of_file, used_html=frozenset()):
        L, C, mod_of = Counter(), Counter(), {}
        test_area = lambda b: b['kind'] == 'tests' or top_of(stem(b['module'])) in TEST_TOPS
        stems = set()
        for b in blobs:
            if b['funcs'] and not test_area(b):
                s = stem(b['module'])
                stems.add(s)
                C[s] += 1
        for b in blobs:
            for f in b['funcs']:
                m = mod_of[f['qual']] = stem(mod_of_file.get(f['file'], b['module']))
                L[m] += f['lines']
        W = Counter()
        for b in blobs:
            for f in b['funcs']:
                a = mod_of[f['qual']]
                if a not in stems:
                    continue
                for c in f['calls']:
                    t = mod_of.get(c['to'])
                    if t in stems and t != a:
                        W[(a, t) if a < t else (t, a)] += 1
        sig = (frozenset(stems), frozenset(W.items()))
        if sig != self.sig:
            self.sig = sig
            fresh = group(stems, L, C, W)
            if fresh != self.fresh:
                self.fresh, self.since, self.ripe = fresh, self.step, False
            self.regroup(stems, L, W)
        elif not self.ripe and self.held():       # a fresh grouping that held: adopt it if it still differs enough
            self.regroup(stems, L, W)
        self.step += 1
        out = []
        for b in blobs:
            aid = (file_area(b, used_html) if not b['funcs'] else 'tests' if test_area(b) else self.assign[stem(b['module'])])
            out.append(dict(b, area=aid))
        return out

    def held(self):
        """The current fresh grouping first came out HOLD or more steps ago (or half the session so far, if shorter)."""
        return self.step - self.since >= min(HOLD, self.step // 2)

    def regroup(self, stems, L, W):
        fresh = self.fresh
        wt = lambda ss: sum(L[s] + 1 for s in ss)
        old = {s: a for s, a in self.assign.items() if s in stems}
        members = defaultdict(set)
        for s, a in old.items():
            members[a].add(s)
        pairs = sorted(((wt(set(g) & members[a]), a, gi) for gi, g in enumerate(fresh) for a in {old[s] for s in g if s in old}),
                       key=lambda t: (-t[0], t[1], t[2]))
        match, used = {}, set()
        for _, a, gi in pairs:
            if gi not in match and a not in used:
                match[gi] = a
                used.add(a)
        group_of = {s: gi for gi, g in enumerate(fresh) for s in g}
        moved = False
        if self.held():
            self.ripe = True
            for a, ms in members.items():
                gi = next((g for g, x in match.items() if x == a), None)
                now = {s for s in fresh[gi] if s in old} if gi is not None else set()
                if wt(ms ^ now) > REGROUP * wt(ms | now):
                    moved = True
                    break

        def area(gi):                    # a group no area matched gets a new id the first time a module needs one
            if gi not in match:
                match[gi] = f'a{self.next_id}'
                self.next_id += 1
            return match[gi]
        assign = {}
        if not moved:
            in_use = set(old.values())
            for s in sorted(stems):
                gi = group_of[s]
                if s in old:
                    assign[s] = old[s]
                elif gi in match or len(in_use) < MAX_AREAS:
                    assign[s] = area(gi)
                    in_use.add(assign[s])
                else:                    # at the cap: the area it shares the most call sites with, else the smallest
                    tie, size = Counter(), Counter()
                    for t, a in {**old, **assign}.items():
                        tie[a] += W.get((s, t) if s < t else (t, s), 0)
                        size[a] += L[t] + 1
                    assign[s] = min(in_use, key=lambda a: (-tie[a], size[a], a))
            moved = len(set(assign.values())) > MAX_AREAS     # still over the cap: adopt the fresh grouping now
        if moved:
            self.regroups += 1
            assign = {s: area(group_of[s]) for s in sorted(stems)}
        self.assign = assign
