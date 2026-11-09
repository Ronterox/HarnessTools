"""Schema v6 (clusters.py): the call communities of a big blob, deterministic whatever the hash seed, the page's rng bit
for bit, ids and members held from step to step, the fresh grouping adopted only once it held, the cap; a forked
Zoner shares no state with its original."""
import copy, json, os, re, shutil, subprocess, sys

import pytest

import areas, clusters, timeline

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def fn(mod, name, lines, calls=()):
    return {'qual': f'{mod}.{name}', 'name': name, 'file': mod + '.c', 'lines': lines, 'branches': 1, 'errors': 0,
            'data': [], 'calls': [{'to': f'{mod}.{c}', 'error': False} for c in calls]}


def blob(funcs, bid='src.big', kind='command'):
    return {'id': bid, 'name': 'big', 'kind': kind, 'module': 'src', 'emoji': '🎬', 'data': [], 'complexity': 0, 'errors': 0,
            'size': sum(f['lines'] for f in funcs), 'funcs': funcs}


def groups(n=3, k=6, lines=30, extra=None):
    """n dense communities of k functions (each calls the next two of its own), one call from each to the next
    community; extra: {name: calls} more functions."""
    fs = []
    for g in range(n):
        for i in range(k):
            calls = [f'g{g}_{(i + 1) % k}', f'g{g}_{(i + 2) % k}'] + ([f'g{(g + 1) % n}_0'] if i == 0 else [])
            fs.append(fn('src', f'g{g}_{i}', lines, calls))
    for name, calls in (extra or {}).items():
        fs.append(fn('src', name, lines, calls))
    return fs


def table(b):
    return {c['id']: set(c['funcs']) for c in b['clusters']}


def run(steps, state=None):
    """[[blob, ...] per step] -> ([[blob with clusters, ...] per step], state)"""
    state, out = state or {}, []
    for bs in steps:
        got, state = clusters.apply(bs, state)
        out.append(got)
    return out, state


def test_rng_is_the_pages_rng_bit_for_bit():
    if not shutil.which('node'):
        pytest.skip('needs node')
    html = open(os.path.join(REPO, 'web', 'index.html'), encoding='utf-8').read()
    src = re.search(r'^function rng\(seed\) \{.*\}$', html, re.M).group(0)
    seeds = [0, 7, 11, 3, 123456789, -5, 2 ** 31 - 1]
    js = subprocess.run(['node', '-e', src + f'; console.log(JSON.stringify({seeds}.map(s => {{ const r = rng(s); '
                                                 f'return Array.from({{length: 40}}, r); }})))'],
                        capture_output=True, text=True, check=True).stdout
    py = [[r() for _ in range(40)] for r in map(clusters.rng, seeds)]
    assert json.loads(js) == py


def test_louvain_finds_planted_communities_and_the_post_steps_hold():
    fs = groups(4, 6, extra={'lone': [], 'pair_a': ['pair_b'], 'pair_b': []})
    quals = [f['qual'] for f in fs]
    mine = set(quals)
    adj, deg = clusters.graph(quals, [[c['to'] for c in f['calls'] if c['to'] in mine] for f in fs])
    got = clusters.fresh([f['lines'] for f in fs], adj, deg)
    names = [{fs[i]['name'] for i in ids} for _, ids in got]
    for g in range(4):
        assert {f'g{g}_{i}' for i in range(6)} in names
    assert {'pair_a', 'pair_b'} in names and got[-1][0] and {fs[i]['name'] for i in got[-1][1]} == {'lone'}   # no inner call: loose
    assert len(got) <= clusters.cap_of(len(fs)) and all(len(ids) > 1 for loose, ids in got if not loose)
    assert [loose for loose, _ in got] == [False] * (len(got) - 1) + [True]


def test_clusters_only_for_code_blobs_at_the_cut_size():
    big = groups()                                                    # 18 x 30 = 540 lines
    small = [fn('src', 'tiny', 5)]
    many = [fn('lib', f'f{i}', 2) for i in range(areas.SPLIT_FUNCS)]  # 50 functions, 100 lines
    got, _ = clusters.apply([blob(big), blob(small, 'src.small'), blob(many, 'lib.many'), blob(big, 'tests.t', 'tests'),
                             {'id': 'file:a.txt', 'kind': 'text', 'size': 900, 'funcs': []}], {})
    assert ['clusters' in b for b in got] == [True, False, True, False, False]
    for b in got[::2][:2]:
        assert sorted(q for c in b['clusters'] for q in c['funcs']) == sorted(f['qual'] for f in b['funcs'])
        assert [c['id'] for c in b['clusters']] == sorted({c['id'] for c in b['clusters']})
        assert all(set(c) == {'id', 'cap', 'emoji', 'funcs'} for c in b['clusters'])


def test_captions_from_names_and_the_loose_ends():
    fs = [fn('src', f'draw_{w}', 80, [f'draw_{v}' for v in ('a', 'b', 'c') if v != w]) for w in ('a', 'b', 'c')]
    fs += [fn('src', f'tl_{w}', 80, [f'tl_{v}' for v in ('a', 'b', 'c') if v != w]) for w in ('a', 'b', 'c')]
    fs += [fn('src', 'solo', 80)]
    got, _ = clusters.apply([blob(fs)], {})
    caps = {c['cap']: c['emoji'] for c in got[0]['clusters']}
    assert caps == {'draw': '🖌️', 'timeline': '⏱️', 'loose ends': '🧷'}
    assert next(c for c in got[0]['clusters'] if c['cap'] == 'loose ends')['id'] == clusters.LOOSE
    assert clusters.words('drawSceneHUD_now2') == ['draw', 'scene', 'hud', 'now2']


SCRIPT = '''
import json, sys
sys.path.insert(0, {repo!r}); sys.path.insert(0, {tests!r})
import test_clusters as t, clusters
steps = []
fs = t.groups(5, 7, extra={{'x1': ['g0_1', 'g3_2'], 'x2': ['g1_1'], 'x3': []}})
for k in range(30):
    cur = fs[:20 + k] if k < 18 else fs
    if k > 22:
        cur = [dict(f, calls=[{{'to': 'src.g4_%d' % ((i + k) % 7), 'error': False}}]) if i % 3 == 0 else f for i, f in enumerate(cur)]
    steps.append([t.blob(cur)])
out, _ = t.run(steps)
print(json.dumps([b[0].get('clusters') for b in out]))
'''


def test_the_same_clusters_whatever_the_hash_seed():
    code = SCRIPT.format(repo=REPO, tests=HERE)
    outs = {subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True,
                           env=dict(os.environ, PYTHONHASHSEED=str(seed))).stdout for seed in (0, 1, 2, 3)}
    assert len(outs) == 1 and outs.pop().count('"funcs"') > 30


def test_ids_and_members_hold_while_functions_come_and_go():
    base = groups()
    s0 = [blob(base)]
    s1 = [blob(base + [fn('src', 'g1_new', 30, ['g1_0', 'g1_1', 'g1_2'])])]          # joins g1's cluster
    s2 = [blob([f for f in s1[0]['funcs'] if f['name'] != 'g2_3'])]                   # leaves
    s3 = [blob(s2[0]['funcs'])]                                                       # nothing changed
    (b0,), (b1,), (b2,), (b3,) = run([s0, s1, s2, s3])[0]
    t0, t1, t2 = table(b0), table(b1), table(b2)
    of = lambda t, name: next(c for c, qs in t.items() if f'src.{name}' in qs)
    assert len(t0) == 3 and set(t1) == set(t0) == set(t2)
    assert of(t1, 'g1_new') == of(t0, 'g1_0') and t1[of(t0, 'g1_0')] == t0[of(t0, 'g1_0')] | {'src.g1_new'}
    assert 'src.g2_3' not in set().union(*t2.values()) and t2[of(t0, 'g2_0')] == t0[of(t0, 'g2_0')] - {'src.g2_3'}
    assert b3['clusters'] == b2['clusters']


def rewired(fs):
    """The same functions, calls rewired so the communities become halves of two old ones: {g0_0..2, g1_0..2} ..."""
    out = []
    for f in fs:
        g, i = map(int, f['name'][1:].split('_'))
        mates = [f'g{(g // 2) * 2 + h}_{j}' for h in (0, 1) for j in (range(3) if i < 3 else range(3, 6))]
        out.append(dict(f, calls=[{'to': f'src.{m}', 'error': False} for m in mates if m != f['name']]))
    return out


def test_a_new_grouping_is_adopted_only_after_it_held(monkeypatch):
    base, n = groups(4, 6), 100
    flip = rewired(base)
    steps = [[blob(base)]] * n + [[blob(flip)]] * (areas.HOLD + 5)
    out, _ = run(steps)
    tabs = [table(b[0]) for b in out]
    assert all(t == tabs[0] for t in tabs[:n + areas.HOLD])           # held: nothing moves for HOLD steps
    after = tabs[n + areas.HOLD]
    assert after != tabs[0] and all(t == after for t in tabs[n + areas.HOLD:])
    halves = {frozenset(f'src.g{g}_{j}' for g in pair for j in js) for pair in ((0, 1), (2, 3)) for js in (range(3), range(3, 6))}
    assert set(map(frozenset, after.values())) == halves
    assert len(set(after) & set(tabs[0])) >= 2                         # matched groups kept their ids
    # flipped and flipped back before it held: nothing moves
    steps = [[blob(base)]] * n + [[blob(flip)]] * 20 + [[blob(base)]] * (areas.HOLD + 5)
    assert all(table(b[0]) == tabs[0] for b in run(steps)[0])


def test_new_functions_never_open_clusters_past_the_cap():
    # one function per step, each calling only functions not there yet: every new one is loose, then the calls
    # come in and the fresh grouping changes every step (never held): the cap rule keeps the count down
    names = [f'f{i}' for i in range(80)]
    fs = [fn('src', n, 10, [names[(i * 7 + 3) % 80], names[(i * 11 + 5) % 80]]) for i, n in enumerate(names)]
    most = 0
    state = {}
    for k in range(50, 81):
        (b,), state = clusters.apply([blob(fs[:k])], state)
        most = max(most, len(b['clusters']))
        assert len(b['clusters']) <= clusters.cap_of(k)
    assert most >= clusters.MIN_CLUSTERS


def test_a_forked_zoner_shares_no_cluster_state():
    steps = [[blob(groups(3, 6 + (k % 3)))] for k in range(12)]
    res = lambda bs: {'blobs': bs, 'edges': [], 'modules': {'src': {'file': 'src.c'}}}
    z = areas.Zoner()
    straight = [z.apply(res(bs))['blobs'] for bs in steps]
    assert all(any('clusters' in b for b in bs) for bs in straight)
    z = areas.Zoner()
    got = [z.apply(res(steps[k]))['blobs'] for k in range(6)]
    fork = copy.copy(z)
    alien = [dict(f, qual='src.x' + f['name'], name='x' + f['name'], calls=[dict(c, to='src.x' + c['to'][4:]) for c in f['calls']])
             for f in groups(4, 6)]
    for k in range(6, 12):                       # the fork runs ahead on other functions, the original goes on
        fork.apply(res([blob(alien[:18 + k])]))
        got.append(z.apply(res(steps[k]))['blobs'])
    assert json.dumps(got) == json.dumps(straight)
    assert not any('clusters' in b for b in areas.Zoner(smart=False).apply(res(steps[0]))['blobs'])


# -- the timeline: build_state keeps them, the v6 delta encoding carries them as entry patches -----------------------

def cl(i, cap, *funcs):
    return {'id': i, 'cap': cap, 'emoji': '🧩', 'funcs': list(funcs)}


def cl_steps():
    from test_func_patch import blob as fblob, fn as ffn, step
    fs = [ffn(f'm.f{i}', i * 10, i * 10 + 5) for i in range(6)]
    t0 = [cl(1, 'a', 'm.f0', 'm.f1'), cl(2, 'b', 'm.f2', 'm.f3'), cl(3, 'c', 'm.f4', 'm.f5')]
    t2 = [t0[0], cl(2, 'b', 'm.f2', 'm.f3', 'm.f4'), cl(3, 'c', 'm.f5')]
    t3 = [cl(0, 'loose ends', 'm.f5'), t2[0], t2[1]]
    tabs = [t0, t0, t2, t3, None, t0]
    out = []
    for i, t in enumerate(tabs):
        b = fblob('A', fs, 'new' if i == 0 else 'same')
        if i == 1:                                         # the blob is re-sent (a function moved) with the same table
            b['funcs'] = [dict(fs[0], start=1)] + fs[1:]
        out.append(step(i, dict(b, **({'clusters': t} if t is not None else {})), fblob('B', fs[:1], 'new' if i == 0 else 'same')))
    return out


def test_clusters_travel_as_entry_patches_and_round_trip():
    from test_func_patch import node_expand, sent
    steps = cl_steps()
    v6 = json.loads(json.dumps({'schema': 6, 'steps': timeline.compact(steps)}))
    assert timeline.SCHEMA == 6 and not any(s.get('full') for s in v6['steps'][1:])
    assert sent(v6, 0, 'A')['clusters'] == steps[0]['blobs'][0]['clusters']
    assert sent(v6, 1, 'A')['clusters'] == [] and 'clusters_remove' not in sent(v6, 1, 'A')   # unchanged: no entry
    assert sent(v6, 2, 'A')['clusters'] == [cl(2, 'b', 'm.f2', 'm.f3', 'm.f4'), cl(3, 'c', 'm.f5')]
    assert sent(v6, 3, 'A')['clusters'] == [cl(0, 'loose ends', 'm.f5')] and sent(v6, 3, 'A')['clusters_remove'] == [3]
    assert 'clusters' not in sent(v6, 4, 'A')                                     # gone: the blob has none
    assert sent(v6, 5, 'A')['clusters'] == steps[5]['blobs'][0]['clusters']       # back: whole
    assert all('clusters' not in b for s in v6['steps'] for b in s['blobs'] if b['id'] == 'B')
    want = json.dumps(steps, ensure_ascii=False)
    for schema in (3, 4, 5, 6):
        tl = json.loads(json.dumps({'schema': schema, 'steps': timeline.compact(steps, schema)}))
        assert json.dumps(timeline.expand(tl), ensure_ascii=False) == want, schema
    assert timeline.compact(steps, 3)[1]['blobs'][0]['clusters'] == steps[1]['blobs'][0]['clusters']   # v3: whole
    if shutil.which('node'):
        import tempfile
        with tempfile.NamedTemporaryFile('w', suffix='.json') as fh:
            json.dump(v6, fh)
            fh.flush()
            assert json.dumps(node_expand(fh.name), sort_keys=True) == json.dumps(steps, sort_keys=True)   # (step key order)


def test_a_table_a_patch_cannot_carry_sends_the_step_full():
    steps = cl_steps()
    steps[2]['blobs'][0]['clusters'] = [cl(2, 'b', 'm.f2'), cl(1, 'a', 'm.f0')]       # ids not increasing
    out = timeline.compact(steps)
    assert out[2].get('full') and json.dumps(timeline.expand({'schema': 6, 'steps': out})) == json.dumps(steps)


def c_mini_snaps():
    from test_areas import c_mini_steps
    return c_mini_steps()


def lowered(monkeypatch):
    """c-mini's blobs are small: gates lowered so its bigger blobs get clusters (and give up a piece or two)."""
    monkeypatch.setattr(areas, 'SPLIT_LINES', 12)
    monkeypatch.setattr(areas, 'SPLIT_FUNCS', 3)


def test_c_mini_clusters_survive_the_timeline_incremental_equals_full(monkeypatch):
    from test_areas import run_steps
    import replay
    lowered(monkeypatch)
    snaps = c_mini_snaps()
    inc = run_steps(snaps, smart=True)
    pairs = [(replay.Step(i, '', 'Write', f, '', 0), snap) for i, (f, snap) in enumerate(snaps)]
    full = list(timeline.analyzed_steps(iter(pairs), full=True, timings=False))
    assert json.dumps(inc) == json.dumps(full)
    have = [b for s in inc for b in s['blobs'] if 'clusters' in b]
    assert have and all(sorted(q for c in b['clusters'] for q in c['funcs']) == sorted(f['qual'] for f in b['funcs']) for b in have)
    back = timeline.expand(json.loads(json.dumps({'schema': 6, 'steps': timeline.compact(inc)})))
    assert json.dumps(back) == json.dumps(inc)


def test_c_mini_live_equals_batch_with_clusters(tmp_path, monkeypatch):
    import live
    from test_live import _fixture_session, exported_equals_batch, follow
    lowered(monkeypatch)
    root = '/work/c'
    src = _fixture_session(tmp_path, 'c-mini', root, [('main.c', 'int main', 'int  main'), ('util.c', 'return', 'return ')])
    w, store, proj = follow(tmp_path, src, root, restarts=2, check_every=3, seed=2)
    exported_equals_batch(store, proj, root)
    assert '"clusters"' in live.export_text(store)


def test_diffs_leave_a_clustered_timeline_as_it_was_and_the_store_complete(tmp_path, monkeypatch):
    import diffs, replay
    from test_diffs import complete
    from test_live import APP, FLASK, _fixture_session
    lowered(monkeypatch)
    srcs = [('c', _fixture_session(tmp_path / 'c', 'c-mini', '/work/c', [('main.c', 'int main', 'int  main'), ('util.c', 'return', 'return ')]), '/work/c')]
    if os.path.isdir(FLASK):
        srcs.append(('flask', FLASK, APP))
    for name, src, root in srcs:
        rs = replay.steps(replay.transcripts(src), root)
        plain = json.dumps(timeline.timeline(src, root, timings=False, steps=rs), separators=(',', ':'))
        out = str(tmp_path / f'{name}-d')
        dw = diffs.Static(out)
        tl = timeline.timeline(src, root, timings=False, steps=replay.steps(replay.transcripts(src), root), diffs=dw)
        dw.close()
        got = json.dumps(tl, separators=(',', ':'))
        assert got == plain and '"clusters"' in got, name
        (tmp_path / f'{name}.json').write_text(got, encoding='utf-8')
        assert complete(diffs.load_static(str(tmp_path / f'{name}.json'), out)) > 0, name


def test_live_code_lists_every_local_module_the_build_imports():
    # live.py rebuilds a store whose CODE files changed: a module the build imports and CODE misses would let a store
    # built by old code go on as if it were current (clusters.py is one)
    import ast, live
    seen = set()

    def walk(name):
        if name in seen:
            return
        seen.add(name)
        tree = ast.parse(open(os.path.join(REPO, name), encoding='utf-8').read())
        for n in ast.walk(tree):
            mods = [a.name for a in n.names] if isinstance(n, ast.Import) else \
                [n.module] if isinstance(n, ast.ImportFrom) and n.module and not n.level else []
            for m in mods:
                if os.path.exists(os.path.join(REPO, m.split('.')[0] + '.py')):
                    walk(m.split('.')[0] + '.py')
    for f in ('timeline.py', 'areas.py', 'live.py'):
        walk(f)
    assert 'clusters.py' in seen and seen <= set(live.CODE), sorted(seen - set(live.CODE))
