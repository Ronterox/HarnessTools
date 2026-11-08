"""Schema v5 (areas.py): cuts, merges, edges recounted over the new blobs, areas and their stability, captions,
the areas table's delta encoding, and the label keys of cut / merged blobs."""
import json, os
from collections import Counter

import pytest

import areas, label, replay, timeline
from conftest import run

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SESSION = os.path.expanduser('~/.claude/projects/-home-rontero-Documents-Projects-AIProjects-HarnessTools-aiblob-demo-app')
APP = os.path.join(REPO, 'demo', 'app')
FIX = os.path.join(HERE, 'fixtures')


def load(root):
    files = {}
    for dp, dirs, fn in os.walk(root):
        dirs[:] = [d for d in dirs if d != '__pycache__' and not d.startswith('.')]
        for n in fn:
            p = os.path.join(dp, n)
            with open(p, encoding='utf-8') as fh:
                files[os.path.relpath(p, root).replace(os.sep, '/')] = fh.read()
    return dict(sorted(files.items()))


def dump(x):
    return json.dumps(x, ensure_ascii=False, separators=(',', ':'))


def scratch_edges(blobs):
    """Code-blob edges counted from scratch over `blobs`, written apart from areas.py and analyze.py: call sites
    per (caller blob, callee blob, main / error), shared data symbols per unordered pair (`ctx:request` / `req`
    ignored, `ctx:x` linking `ctx:x.*`), and per (blob, file) the functions whose `refs` name the file."""
    of = {f['qual']: b['id'] for b in blobs for f in b['funcs']}
    out = {}
    for b in blobs:
        for f in b['funcs']:
            for c in f['calls']:
                t = of.get(c['to'])
                if t and t != b['id']:
                    k = (b['id'], t, 'error' if c['error'] else 'main')
                    out[k] = (out.get(k, (0,))[0] + 1,)
    syms = {b['id']: {d for d in b['data'] if not (d.startswith('ctx:') and d[4:].split('.')[0] in ('request', 'req'))}
            for b in blobs}
    ids = sorted(syms)
    for x, a in enumerate(ids):
        for b in ids[x + 1:]:
            via = syms[a] & syms[b]
            for p, q in ((a, b), (b, a)):
                via |= {d2 for d in syms[p] if d.startswith('ctx:') and '.' not in d for d2 in syms[q] if d2.startswith(d + '.')}
            if via:
                out[(a, b, 'data')] = (len(via), sorted(via))
    for b in blobs:
        n = Counter(p for f in b['funcs'] for p in f.get('refs', ()))
        for p, k in n.items():
            out[(b['id'], 'file:' + p, 'ref')] = (k,)
    return out


def as_scratch(edges, blobs):
    code = {b['id'] for b in blobs if b['funcs']}
    return {(e['a'], e['b'], e['kind']): (e['n'], e['via']) if e['kind'] == 'data' else (e['n'],)
            for e in edges if e['a'] in code}


def c_mini_steps():
    """c-mini written file by file, then edited: a call added, a function deleted, a new big helper."""
    files = load(os.path.join(FIX, 'c-mini'))
    order = ['util.h', 'util.c', 'store.h', 'store.c', 'handlers.c', 'main.c', 'tests/test_store.c', 'data/items.txt']
    snap, out = {}, []
    for rel in order:
        snap = dict(snap, **{rel: files[rel]})
        out.append((rel, snap))
    main = files['main.c']
    big = 'static int crunch(int n) {\n' + ''.join(f'    n = n * 3 + {k};\n' for k in range(30)) + '    return n;\n}\n'
    edits = [('util.c', files['util.c'] + '\n' + big + '\nint util_twice(int n) { return crunch(n) + crunch(n + 1); }\n')]
    edits.append(('main.c', main.replace('int main(', 'extern int util_twice(int n);\n\nint main(', 1)
                  .replace('{', '{\n    util_twice(2);', 1) if 'int main(' in main else main))
    for rel, text in edits:
        snap = dict(snap, **{rel: text})
        out.append((rel, snap))
    return out


def run_steps(snapshots, smart):
    pairs = [(replay.Step(i, '', 'Write', f, '', 0), snap) for i, (f, snap) in enumerate(snapshots)]
    return list(timeline.analyzed_steps(iter(pairs), timings=False, smart=smart))


def test_recounted_edges_equal_a_count_from_scratch_over_folded_and_cut_blobs(monkeypatch):
    # gates lowered so c-mini's entry gives up pieces and its small blobs fold, at most steps
    monkeypatch.setattr(areas, 'SPLIT_LINES', 10)
    monkeypatch.setattr(areas, 'PIECE_MIN', 3)
    monkeypatch.setattr(areas, 'PIECE_SHARE', 0.05)
    monkeypatch.setattr(areas, 'TINY', 6)
    snaps = c_mini_steps()
    plain = run_steps(snaps, smart=False)
    for st in plain:     # the fixture has nothing analyze() counts beyond calls, data and refs: the scratch count is exact
        assert as_scratch(st['edges'], st['blobs']) == scratch_edges(st['blobs']), st['idx']
    folds = cuts = 0
    for st in run_steps(snaps, smart=True):
        folds += sum(len(b.get('merged', ())) for b in st['blobs'])
        cuts += sum('cut' in b for b in st['blobs'])
        assert as_scratch(st['edges'], st['blobs']) == scratch_edges(st['blobs']), st['idx']
        ids = {b['id'] for b in st['blobs']}
        assert all(e['a'] in ids and e['b'] in ids for e in st['edges']), st['idx']
        keys = [(e['a'], e['b'], e['kind']) for e in st['edges']]
        assert len(keys) == len(set(keys)) and keys == sorted(keys, key=lambda k: (timeline.EDGE_ORDER[k[2]], k[0], k[1]))
    assert folds and cuts, (folds, cuts)


def flask_steps():
    """A tiny Flask app: `load` aborts (an error edge analyze() counts beyond calls) and is shared by two routes
    (a hub that folds); `page` names its template twice (a ref beyond one per function) and is show_a's callee
    (a piece once the gates are lowered)."""
    body = ''.join(f'    x = x * 2 + {k}\n' for k in range(10))
    app = ('from flask import Flask, abort, render_template\n\napp = Flask(__name__)\n\n\n@app.errorhandler(404)\n'
           'def missing(e):\n    return "gone", 404\n\n\ndef load(i):\n    if i < 0:\n        abort(404)\n    return i\n\n\n'
           '@app.route("/a/<int:i>")\ndef show_a(i):\n    return page(load(i))\n\n\n@app.route("/b/<int:i>")\ndef show_b(i):\n'
           '    load(i)\n    return "b"\n\n\ndef page(x):\n    tpl = "page.html"\n' + body +
           '    return render_template("page.html", x=x, tpl=tpl)\n')
    tpl = {'templates/page.html': '<p>{{ x }}</p>\n'}
    more = app.replace('@app.route("/b', '@app.route("/c/<int:i>")\ndef show_c(i):\n    return str(load(i))\n\n\n@app.route("/b')
    return [('app.py', {'app.py': app}), ('templates/page.html', {'app.py': app, **tpl}), ('app.py', {'app.py': more, **tpl})]


def test_recount_keeps_what_analyze_counted_beyond_calls_and_refs_with_the_blob_it_went_to(monkeypatch):
    monkeypatch.setattr(areas, 'SPLIT_LINES', 10)
    monkeypatch.setattr(areas, 'PIECE_MIN', 3)
    monkeypatch.setattr(areas, 'PIECE_SHARE', 0.05)
    snaps = flask_steps()
    plain, smart = run_steps(snaps, smart=False), run_steps(snaps, smart=True)
    seen = set()
    for p, z in zip(plain, smart):
        # residue: what analyze() counted beyond the scratch count (abort -> error handler, a second literal)
        got, base = as_scratch(p['edges'], p['blobs']), scratch_edges(p['blobs'])
        residue = {k: got[k][0] - base.get(k, (0,))[0] for k in got if got[k][0] != base.get(k, (0,))[0]}
        ids = {b['id'] for b in z['blobs']}
        home = {m['id']: b['id'] for b in z['blobs'] for m in b.get('merged', ())}
        home.update((i, i) for i in ids)
        want = {k: v[0] for k, v in scratch_edges(z['blobs']).items()}
        for (a, b, k), n in residue.items():
            a, b = home.get(a, a), home.get(b, b)
            if a != b:
                want[(a, b, k)] = want.get((a, b, k), 0) + n
        assert {k: v[0] for k, v in as_scratch(z['edges'], z['blobs']).items()} == want, z['idx']
        seen.update(('fold residue' for (a, b, k) in residue if k == 'error' and a not in ids),
                    ('ref residue' for (a, b, k) in residue if k == 'ref'),
                    ('piece ref' for b in z['blobs'] if 'cut' in b for f in b['funcs'] if f['refs']))
    assert seen == {'fold residue', 'ref residue', 'piece ref'}, seen


def test_a_cut_piece_rooted_at_a_method_takes_its_files_module(monkeypatch):
    monkeypatch.setattr(areas, 'SPLIT_LINES', 10)
    monkeypatch.setattr(areas, 'PIECE_MIN', 3)
    monkeypatch.setattr(areas, 'PIECE_SHARE', 0.05)
    monkeypatch.setattr(areas, 'TINY', 0)
    body = ''.join(f'        x = x * 2 + {k}\n' for k in range(12))
    files = {
        'svc.py': 'class Svc:\n    def run(self, x):\n' + body + '        return self.step(x)\n\n'
                  '    def step(self, x):\n' + body + '        return x\n',
        'app.py': 'from svc import Svc\n\n\ndef other():\n    return 1\n\n\ndef main():\n    s = Svc()\n    s.run(1)\n'
                  '    other()\n\n\nif __name__ == "__main__":\n    main()\n',
    }
    r, _, _ = run(files)
    z = areas.Zoner().apply(r)
    pieces = [b for b in z['blobs'] if 'cut' in b]
    assert [p['id'] for p in pieces] == ['app.<module>>svc.Svc.run'], [b['id'] for b in z['blobs']]
    assert pieces[0]['module'] == 'svc'                          # not the class: `svc.Svc`
    assert not any(b['module'] == 'svc.Svc' for b in z['blobs'])


def naive_group(stems, L, C, W, cap=areas.AREA_CAP):
    """group() as first written: every join scans every pair (cubic), and stops when the cap blocks all joins."""
    from collections import defaultdict
    G, of = {}, {}
    for i, s in enumerate(sorted(stems)):
        G[i] = {'stems': (s,), 'L': L[s], 'C': C[s], 'top': areas.top_of(s)}
        of[s] = i
    S = defaultdict(Counter)
    for (a, b), n in W.items():
        S[of[a]][of[b]] += n
        S[of[b]][of[a]] += n
    nxt = len(G)
    tops = len({g['top'] for g in G.values()})
    target = min(areas.MAX_AREAS, min(areas.CODE_AREAS, max(1, -(-sum(C.values()) // cap))) + tops - 1)
    for cross in (False, True):
        while len(G) > (areas.MAX_AREAS if cross else target):
            best = None
            for i in sorted(G):
                for j in sorted(G):
                    A, B = G[i], G[j]
                    if j <= i or (not cross and A['top'] != B['top']) or A['C'] + B['C'] > cap:
                        continue
                    k = (-S[i][j] / max(1, min(A['L'], B['L'])), A['C'] + B['C'], tuple(sorted(A['stems'] + B['stems'])))
                    if best is None or k < best[0]:
                        best = (k, i, j)
            if best is None:
                break
            _, i, j = best
            A, B = G.pop(i), G.pop(j)
            G[nxt] = {'stems': tuple(sorted(A['stems'] + B['stems'])), 'L': A['L'] + B['L'], 'C': A['C'] + B['C'],
                      'top': A['top'] if A['top'] == B['top'] else '*'}
            S[nxt] = Counter({k: S[i][k] + S[j][k] for k in G if k != nxt and S[i][k] + S[j][k]})
            for k, n in S[nxt].items():
                S[k][nxt] = n
            nxt += 1
    return sorted((g['stems'] for g in G.values()), key=lambda s: (-sum(L[x] for x in s), s))


def test_group_joins_the_best_pairs_like_the_plain_scan_and_never_leaves_more_than_max_areas():
    import random
    for seed in range(40):
        rnd = random.Random(seed)
        n, tops = rnd.choice([4, 10, 25, 40]), rnd.choice([1, 2, 3, 14])
        stems = [f'd{i % tops}.m{i}' for i in range(n)]
        L = {s: rnd.randint(0, 300) for s in stems}
        C = {s: rnd.randint(1, 4) for s in stems}
        W = Counter()
        for _ in range(n * rnd.choice([0, 1, 3])):
            a, b = sorted(rnd.sample(stems, 2))
            W[(a, b)] += rnd.randint(1, 3)
        got, ref = areas.group(stems, L, C, W), naive_group(stems, L, C, W)
        if len(ref) <= areas.MAX_AREAS:
            assert got == ref, seed
        else:                                   # the cap gives way: the best pairs keep joining down to MAX_AREAS
            assert len(got) == areas.MAX_AREAS, seed
        assert sorted(s for g in got for s in g) == sorted(stems)


def every_function_once(before, after):
    got = Counter(f['qual'] for b in after for f in b['funcs'])
    assert got == Counter(f['qual'] for b in before for f in b['funcs']) and max(got.values()) == 1
    for b in (b for b in after if b['funcs']):
        assert b['size'] == sum(f['lines'] for f in b['funcs']), b['id']
        assert b['data'] == sorted({d for f in b['funcs'] for d in f['data']}), b['id']


def remapped(edges, home, kinds):
    """Blob-level edges with ids mapped through `home`, n summed, via unioned, self edges dropped."""
    out = {}
    for e in edges:
        if e['kind'] not in kinds:
            continue
        a, b = home.get(e['a'], e['a']), home.get(e['b'], e['b'])
        if a == b:
            continue
        r = out.setdefault((a, b, e['kind']), [0, set()])
        r[0] += e['n']
        r[1].update(e.get('via', ()))
    return out


def test_flask_tiny_blobs_fold_into_their_module_but_entry_points_never():
    r, _, _ = run(load(APP))
    z = areas.Zoner().apply(r)
    every_function_once(r['blobs'], z['blobs'])
    ids = {b['id'] for b in z['blobs']}
    kept = {b['id'] for b in r['blobs'] if b['kind'] in areas.NO_FOLD}
    assert kept <= ids
    folded = {m['id']: b['id'] for b in z['blobs'] for m in b.get('merged', ())}
    assert folded == {'hub:auth': 'auth.login', 'hub:api': 'api.create_note', 'hub:notes': 'notes.edit'}
    for b in z['blobs']:
        for m in b.get('merged', ()):
            assert set(m['funcs']) <= {f['qual'] for f in b['funcs']} and m['lines'] > 0
    # merges only: calls and file refs are the analyzer's, summed per new pair
    home = {**{i: i for i in ids}, **folded}
    got = {(e['a'], e['b'], e['kind']): [e['n'], set(e.get('via', ()))] for e in z['edges'] if e['kind'] != 'data'}
    assert got == remapped(r['edges'], home, ('main', 'error', 'ref'))
    assert all(e['a'] in ids and e['b'] in ids for e in z['edges'])


def test_no_smart_keeps_the_analyzer_blobs_and_still_gives_areas():
    r, _, _ = run(load(APP))
    z = areas.Zoner(smart=False).apply(r)
    assert [dict(b, area=None) for b in z['blobs']] == [dict(b, area=None) for b in r['blobs']]
    assert z['edges'] is r['edges']
    assert {b['area'] for b in z['blobs']} == {a['id'] for a in z['areas']}


def test_big_blobs_give_up_callee_pieces(monkeypatch):
    # c-mini's entry with the size gates lowered: main's callees that own their own code become pieces
    monkeypatch.setattr(areas, 'SPLIT_LINES', 10)
    monkeypatch.setattr(areas, 'PIECE_MIN', 3)
    monkeypatch.setattr(areas, 'PIECE_SHARE', 0.05)
    monkeypatch.setattr(areas, 'TINY', 0)
    r, _, _ = run(load(os.path.join(FIX, 'c-mini')))
    z = areas.Zoner().apply(r)
    every_function_once(r['blobs'], z['blobs'])
    by = {b['id']: b for b in z['blobs']}
    pieces = [b for b in z['blobs'] if 'cut' in b]
    assert pieces, [b['id'] for b in z['blobs']]
    for p in pieces:
        parent = by[p['cut']]
        root = p['id'][len(parent['id']) + 1:]
        assert p['id'] == f"{parent['id']}>{root}" and p['name'] == root.rsplit('.', 1)[1]
        assert p['kind'] == parent['kind'] and p['emoji'] == parent['emoji'] and root in {f['qual'] for f in p['funcs']}
        assert z['blobs'].index(p) > z['blobs'].index(parent)
    # call sites are conserved: what left the parent is counted between parent and piece instead
    calls = lambda es: sum(e['n'] for e in es if e['kind'] in ('main', 'error'))
    inner = sum(1 for b in z['blobs'] for f in b['funcs'] for c in f['calls']
                if any(c['to'] == g['qual'] for g in b['funcs']))
    orig_inner = sum(1 for b in r['blobs'] for f in b['funcs'] for c in f['calls']
                     if any(c['to'] == g['qual'] for g in b['funcs']))
    assert calls(z['edges']) == calls(r['edges']) + orig_inner - inner


def test_pieces_of_reads_the_dominator_tree_like_split_entry():
    # r -> a -> a1, a2 (a owns them); r -> b; r -> c -> h; d -> h (h shared: owned by r)
    succ = {'m.r': ['m.a', 'm.b', 'm.c', 'm.d'], 'm.a': ['m.a1', 'm.a2'], 'm.a1': [], 'm.a2': [], 'm.b': [],
            'm.c': ['m.h'], 'm.d': ['m.h'], 'm.h': []}
    lines = {'m.r': 10, 'm.a': 30, 'm.a1': 40, 'm.a2': 40, 'm.b': 2, 'm.c': 25, 'm.d': 5, 'm.h': 60}
    got = areas.pieces_of('m.r', list(succ), lines, succ, frozenset())
    assert got == [('m.a', frozenset({'m.a', 'm.a1', 'm.a2'})), ('m.c', frozenset({'m.c'}))]


def test_captions_weigh_words_by_blobs_and_lines():
    assert areas.caption([('User login', 50), ('Login check', 4), ('Note editor', 30)]) == 'Login'   # user: 7.1 < 14.7 / 2
    assert areas.caption([('Search view renderer', 300), ('Search engine', 20), ('Layout view', 900)]) == 'View · Search'
    assert areas.caption([('Data handler', 10)]) == 'Misc'
    assert areas.caption([('UI layout engine', 40)]) == 'Layout · UI'
    table = areas.caption_table([
        {'id': 'x', 'area': 'a1', 'size': 40, 'name': 'x', 'emoji': '🔐', 'label': {'title': 'User login', 'emoji': '🔑'}},
        {'id': 'x>m.y', 'area': 'a1', 'size': 30, 'name': 'y', 'emoji': '🔐', 'cut': 'x'},       # unlabelled piece
        {'id': 't', 'area': 'tests', 'size': 9, 'name': 't', 'emoji': '🧪'}])
    assert table == [{'id': 'a1', 'caption': 'Login · User', 'faces': ['🔑']},
                     {'id': 'tests', 'caption': 'Tests', 'faces': ['🧪']}]


def code_blob(bid, module, lines, calls=()):
    f = {'qual': f'{module}.{bid}', 'name': bid, 'file': module.replace('.', '/') + '.c', 'start': 1, 'end': lines,
         'lines': lines, 'branches': 0, 'errors': 0, 'hash': bid, 'data': [], 'snippet': [], 'refs': [],
         'calls': [{'to': t, 'error': False} for t in calls]}
    return {'id': f['qual'], 'name': bid, 'kind': 'command', 'module': module, 'funcs': [f], 'size': lines,
            'complexity': 0, 'errors': 0, 'data': [], 'emoji': '•'}


def reassignments(states):
    """Blobs present in two consecutive states whose area changed."""
    n, prev = 0, {}
    for blobs in states:
        cur = {b['id']: b['area'] for b in blobs}
        n += sum(1 for i, a in cur.items() if i in prev and prev[i] != a)
        prev = cur
    return n


def test_area_ids_stay_put_while_modules_come_and_go():
    z = areas.Zoner(smart=False)
    mods = [f'src.m{i}' for i in range(12)]
    states, made = [], []
    for k in range(1, len(mods) + 1):      # one module more per step, each calling the previous one
        made.append(code_blob(f'f{k}', mods[k - 1], 40 + k, [f'{mods[k - 2]}.f{k - 1}'] if k > 1 else []))
        res = {'blobs': list(made), 'edges': [], 'modules': {m: {'file': m.replace('.', '/') + '.c'} for m in mods[:k]}}
        states.append(z.apply(res)['blobs'])
    assert reassignments(states) <= z.regroups * len(mods)
    last = states[-1]
    assert len({b['area'] for b in last}) > 1
    # nothing changes: no regroup, same ids
    before = z.regroups
    again = z.apply({'blobs': list(made), 'edges': [], 'modules': {m: {'file': m.replace('.', '/') + '.c'} for m in mods}})
    assert [b['area'] for b in again['blobs']] == [b['area'] for b in last] and z.regroups == before
    # one module removed: the others keep their area
    gone = z.apply({'blobs': made[:-1], 'edges': [], 'modules': {m: {'file': m.replace('.', '/') + '.c'} for m in mods[:-1]}})
    assert [b['area'] for b in gone['blobs']] == [b['area'] for b in last[:-1]]


def test_a_session_growing_module_by_module_never_shows_more_than_max_areas():
    # 150 modules, one more per step, each calling up to 4 others already there: the fresh grouping changes every
    # step (never held), so only the cap rule keeps new modules from opening area after area
    import random
    rnd = random.Random(3)
    n = 150
    mods = [f'src.m{i}' for i in range(n)]
    calls = {i: [j for j in rnd.sample(range(n), 4) if j != i] for i in range(n)}
    z = areas.Zoner(smart=False)
    states, most = [], 0
    for k in range(1, n + 1):
        made = [code_blob(f'f{i}', mods[i], 40, [f'{mods[j]}.f{j}' for j in calls[i] if j < k]) for i in range(k)]
        res = {'blobs': made, 'edges': [], 'modules': {m: {'file': m.replace('.', '/') + '.c'} for m in mods[:k]}}
        out = z.apply(res)
        most = max(most, len(out['areas']))
        states.append(out['blobs'])
    assert most == areas.MAX_AREAS and len({b['area'] for b in states[-1]}) == areas.MAX_AREAS
    assert z.regroups == 0 and reassignments(states) == 0      # held without reshuffling anything
    # already over the cap (say from before the rule): the fresh grouping is adopted at once
    z.assign = {m: f'x{i}' for i, m in enumerate(mods)}
    z.sig = None
    over = z.apply(res)
    assert len({b['area'] for b in over['blobs']}) <= areas.MAX_AREAS


@pytest.mark.skipif(not os.path.isdir(SESSION), reason='recorded demo session not on this machine')
def test_flask_session_area_reassignments():
    rs = replay.steps(replay.transcripts(SESSION), os.path.abspath(APP))
    states = [s['blobs'] for s in timeline.analyzed_steps(((st, st.content) for st in rs), timings=False)]
    n = reassignments(states)
    print(f'flask: {n} area reassignments over {len(states)} steps')
    # one regroup: the grouping that came out at step 7 held for half the session, so at step 13 auth's five blobs
    # leave the first area for one of their own and the error handler joins the first area
    assert n == 6
    groups = {}
    for b in states[-1]:
        groups.setdefault(b['area'], set()).add(b['module'])
    assert sorted(map(sorted, groups.values())) == [
        ['.', 'templates'], ['api', 'app', 'db', 'errors', 'models'], ['auth'], ['notes'],
        ['tests.test_app']]


def table_steps():
    def st(i, tab):
        return {'idx': i, 'blobs': [], 'edges': [], 'areas': tab, 'stats': {}}
    a1, a2, a3 = ({'id': f'a{k}', 'caption': f'C{k}', 'faces': ['x']} for k in (1, 2, 3))
    return [st(0, [a1, a2]), st(1, [a1, a2]), st(2, [a1, dict(a2, caption='New')]),
            st(3, [a1, dict(a2, caption='New'), a3]), st(4, [a1, a3]), st(5, [])]


@pytest.mark.parametrize('schema', [3, 4, 5])
def test_areas_table_travels_as_entry_patches(schema):
    steps = table_steps()
    out = timeline.compact(steps, schema)
    assert out[0]['areas'] == steps[0]['areas']
    assert 'areas' not in out[1] and 'areas_remove' not in out[1]
    assert out[2]['areas'] == [steps[2]['areas'][1]]
    assert out[3]['areas'] == [steps[3]['areas'][2]]
    assert 'areas' not in out[4] and out[4]['areas_remove'] == ['a2']
    assert out[5]['areas_remove'] == ['a1', 'a3']
    back = timeline.expand(json.loads(dump({'schema': schema, 'steps': out})))
    assert dump(back) == dump(steps)


def test_v4_steps_expand_without_areas():
    steps = [dict(s) for s in table_steps()]
    for s in steps:
        del s['areas']
    assert all('areas' not in s for s in timeline.expand({'schema': 4, 'steps': timeline.compact(steps, 4)}))


def test_label_keys_of_cut_parents_and_merge_targets_are_the_analyzer_blobs_keys():
    f = lambda q, h: {'qual': q, 'hash': h}
    parent = {'id': 'P', 'kind': 'hub', 'area': 'a1', 'size': 30, 'name': 'p', 'emoji': '•', 'funcs': [f('m.p', 'h1')]}
    piece = {'id': 'P>m.q', 'kind': 'hub', 'cut': 'P', 'area': 'a1', 'size': 20, 'name': 'q', 'emoji': '•',
             'funcs': [f('m.q', 'h2'), f('m.t', 'h4')],
             'merged': [{'id': 'T', 'name': 't', 'emoji': '•', 'kind': 'hub', 'lines': 2, 'funcs': ['m.t']}]}
    other = {'id': 'O', 'kind': 'route', 'area': 'a2', 'size': 5, 'name': 'o', 'emoji': '•', 'funcs': [f('m.o', 'h3')]}
    step = {'blobs': [parent, piece, other], 'areas': [{'id': 'a1', 'caption': 'P', 'faces': []},
                                                       {'id': 'a2', 'caption': 'O', 'faces': []}]}
    step['_keys'] = label.step_keys(step)
    assert step['_keys'] == {'P': 'h1,h2', 'P>m.q': 'h2', 'O': 'h3'}
    lab = lambda t, e: {'title': t, 'emoji': e, 'summary': '', 'risk': 'low'}
    cache = {'h1,h2': lab('Search view', '🔎'), 'h4': lab('Tiny thing', '🐜'), 'h3': lab('Note editor', '📝')}
    label.apply_labels([step], cache)
    assert parent['label']['title'] == 'Search view' and 'label' not in piece
    assert piece['merged'][0]['label']['title'] == 'Tiny thing'
    assert step['areas'] == [{'id': 'a1', 'caption': 'Search · View', 'faces': ['🔎']},
                             {'id': 'a2', 'caption': 'Editor · Note', 'faces': ['📝']}]


def test_shared_cut_parent_is_copied_when_its_label_changes_between_steps():
    f = lambda q, h: {'qual': q, 'hash': h}
    parent = {'id': 'P', 'kind': 'hub', 'funcs': [f('m.p', 'h1')]}
    s1 = {'blobs': [parent, {'id': 'P>m.q', 'kind': 'hub', 'cut': 'P', 'funcs': [f('m.q', 'h2')]}]}
    s2 = {'blobs': [parent, {'id': 'P>m.q', 'kind': 'hub', 'cut': 'P', 'funcs': [f('m.q', 'h9')]}]}
    for s in (s1, s2):
        s['_keys'] = label.step_keys(s)
    lab = lambda t: {'title': t, 'emoji': 'x', 'summary': '', 'risk': 'low'}
    label.apply_labels([s1, s2], {'h1,h2': lab('One'), 'h1,h9': lab('Two')})
    assert s1['blobs'][0]['label']['title'] == 'One' and s2['blobs'][0]['label']['title'] == 'Two'
    assert s1['blobs'][0] is parent and s2['blobs'][0] is not parent
