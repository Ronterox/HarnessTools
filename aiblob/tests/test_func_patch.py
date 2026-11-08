"""Schema v4: a re-sent blob's `funcs` is a patch on the previous step's functions (SCHEMA.md "v4"), and the page's
expandSteps() rebuilds the same states as timeline.expand()."""
import json, os, shutil, subprocess

import pytest

import timeline

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PAGE = os.path.join(REPO, 'web', 'index.html')


def dump(x, **kw):
    return json.dumps(x, ensure_ascii=False, separators=(',', ':'), **kw)


def fn(qual, start, end, h='h', state='same', added=0, removed=0, **extra):
    return {'qual': qual, 'name': qual.split('.')[-1], 'file': 'a.py', 'start': start, 'end': end,
            'lines': end - start + 1, 'branches': 0, 'errors': 0, 'hash': h, 'data': [], 'snippet': [],
            'calls': [], 'refs': [], **extra, 'delta': {'added': added, 'removed': removed, 'state': state}}


def blob(bid, funcs, state='same', added=0, removed=0, moved=0, removed_funcs=()):
    return {'id': bid, 'name': bid, 'emoji': '', 'kind': 'route', 'module': 'a',
            'size': sum(f['lines'] for f in funcs), 'complexity': 1, 'errors': 0, 'data': [],
            'delta': {'added': added, 'removed': removed, 'moved': moved, 'state': state,
                      'removed_funcs': list(removed_funcs)}, 'funcs': funcs}


def step(i, *blobs):
    return {'idx': i, 'ts': '', 'tool': 'Edit', 'file': 'a.py', 'said': '', 'turn': 0, 'added': 0, 'removed': 0,
            'blobs': list(blobs), 'edges': [], 'stats': {'calls': 0, 'unresolved': 0}}


def encode(steps):
    """v4 and v3 encodings of full-state steps; both expand back to them byte for byte (through JSON)."""
    v4 = json.loads(dump({'schema': 4, 'steps': timeline.compact(steps, 4)}))
    v3 = json.loads(dump({'schema': 3, 'steps': timeline.compact(steps, 3)}))
    assert dump(timeline.expand(v4)) == dump(steps) == dump(timeline.expand(v3))
    assert not any(s.get('full') for s in v4['steps'][1:])
    return v4


def sent(v4, i, bid):
    return next(b for b in v4['steps'][i]['blobs'] if b['id'] == bid)


A0 = [fn('m.a1', 1, 5), fn('m.a2', 7, 10), fn('m.a3', 12, 20)]


def test_changed_func_sends_only_its_changed_keys_and_shifted_func_only_its_position():
    a2 = fn('m.a2', 7, 11, h='h2', state='changed', added=2, removed=1)
    steps = [step(0, blob('A', A0, 'new')),
             step(1, blob('A', [A0[0], a2, fn('m.a3', 13, 21)], 'changed', 2, 1))]
    b = sent(encode(steps), 1, 'A')
    assert b['funcs'] == [{'qual': 'm.a2', 'end': 11, 'lines': 5, 'hash': 'h2', 'delta': a2['delta']},
                          {'qual': 'm.a3', 'start': 13, 'end': 21}]
    assert 'funcs_remove' not in b and 'funcs_order' not in b and b['size'] == 19


def test_new_func_is_sent_whole_and_unsent_funcs_rebuild_with_zeroed_delta():
    a2 = fn('m.a2', 7, 10, h='h2', state='changed', added=1, removed=1)
    a4 = fn('m.a4', 22, 25, state='new', added=4)
    steps = [step(0, blob('A', A0, 'new')),
             step(1, blob('A', [A0[0], a2, A0[2]], 'changed', 1, 1)),
             step(2, blob('A', [A0[0], dict(a2, delta=dict(timeline.ZERO)), A0[2], a4], 'changed', 4))]
    v4 = encode(steps)
    assert sent(v4, 2, 'A')['funcs'] == [a4]                     # a2's delta went back to 0: not sent
    assert timeline.expand(v4)[2]['blobs'][0]['funcs'][1]['delta'] == timeline.ZERO


def test_removed_func_is_listed_by_qual():
    steps = [step(0, blob('A', A0, 'new')),
             step(1, blob('A', [A0[1], A0[2]], 'changed', 0, 5, removed_funcs=['m.a1']))]
    b = sent(encode(steps), 1, 'A')
    assert b['funcs'] == [] and b['funcs_remove'] == ['m.a1'] and 'funcs_order' not in b


def test_moved_func_leaves_one_blob_and_joins_the_other_whole():
    b1 = fn('m.b1', 30, 40)
    a3 = dict(A0[2], delta={'added': 0, 'removed': 0, 'state': 'moved'})
    steps = [step(0, blob('A', A0, 'new'), blob('B', [b1], 'new')),
             step(1, blob('A', A0[:2], 'changed', moved=9), blob('B', [b1, a3], 'changed', moved=9))]
    v4 = encode(steps)
    assert sent(v4, 1, 'A')['funcs'] == [] and sent(v4, 1, 'A')['funcs_remove'] == ['m.a3']
    assert sent(v4, 1, 'B')['funcs'] == [a3] and 'funcs_remove' not in sent(v4, 1, 'B')


def test_reordered_funcs_send_the_order_and_an_inserted_func_too():
    a0 = fn('m.a0', 1, 3, state='new', added=3)
    steps = [step(0, blob('A', A0, 'new')),
             step(1, blob('A', [A0[1], A0[0], A0[2]])),               # only the order changed
             step(2, blob('A', [a0, A0[1], A0[0], A0[2]], 'changed', 3))]
    v4 = encode(steps)
    b = sent(v4, 1, 'A')
    assert b['funcs'] == [] and b['funcs_order'] == ['m.a2', 'm.a1', 'm.a3']
    b = sent(v4, 2, 'A')
    assert b['funcs'] == [a0] and b['funcs_order'] == ['m.a0', 'm.a2', 'm.a1', 'm.a3']


def test_blob_that_disappears_and_comes_back_is_sent_whole():
    b1 = fn('m.b1', 30, 40)
    b1x = fn('m.b1', 30, 41, h='h2', state='new', added=12)
    steps = [step(0, blob('A', A0, 'new'), blob('B', [b1], 'new')),
             step(1, blob('A', A0)),
             step(2, blob('A', A0), blob('B', [b1x], 'new', 12))]
    v4 = encode(steps)
    assert v4['steps'][1]['removed_blobs'] == ['B']
    assert sent(v4, 2, 'B')['funcs'] == [b1x] and 'funcs_order' not in sent(v4, 2, 'B')


def test_func_whose_keys_change_is_replaced_and_repeated_quals_send_the_step_full():
    a2 = fn('m.a2', 7, 10, deferred=True)                     # a key base's a2 does not have: removed, sent whole
    steps = [step(0, blob('A', A0, 'new')), step(1, blob('A', [A0[0], a2, A0[2]]))]
    b = sent(encode(steps), 1, 'A')
    assert b['funcs'] == [a2] and b['funcs_remove'] == ['m.a2'] and b['funcs_order'] == ['m.a1', 'm.a2', 'm.a3']
    twice = [step(0, blob('A', A0, 'new')), step(1, blob('A', A0 + [A0[0]]))]
    out = timeline.compact(twice, 4)
    assert out[1].get('full') and dump(timeline.expand({'schema': 4, 'steps': out})) == dump(twice)


def fixture():
    """Every case above in one timeline: changed, shifted, new, removed, moved, reordered, gone and back."""
    b1 = fn('m.b1', 30, 40)
    a2 = fn('m.a2', 7, 11, h='h2', state='changed', added=2, removed=1)
    a3m = dict(A0[2], start=13, end=21, delta={'added': 0, 'removed': 0, 'state': 'moved'})
    return [step(0, blob('A', A0, 'new'), blob('B', [b1], 'new')),
            step(1, blob('A', [A0[0], a2, fn('m.a3', 13, 21)], 'changed', 2, 1), blob('B', [b1])),
            step(2, blob('A', [a2], 'changed', 0, 5, 9, ['m.a1']), blob('B', [b1, a3m], 'changed', moved=9)),
            step(3, blob('A', [fn('m.a4', 1, 4, state='new', added=4), dict(a2, delta=dict(timeline.ZERO))], 'changed', 4),
                 blob('B', [dict(a3m, delta=dict(timeline.ZERO)), b1])),
            step(4, blob('A', [dict(a2, delta=dict(timeline.ZERO))], 'changed', 0, 4, removed_funcs=['m.a4'])),
            step(5, blob('A', [dict(a2, delta=dict(timeline.ZERO))]), blob('B', [b1], 'new'))]


def fixture5():
    """fixture() as schema v5: blobs with `area` (B moves area at step 2), a cut piece, a folded-in blob, and an
    areas table whose entries change, appear and go."""
    tables = [[('a1', 'One'), ('a2', 'Two')], [('a1', 'One'), ('a2', 'Two')], [('a1', 'One'), ('a2', 'Second')],
              [('a1', 'One'), ('a2', 'Second'), ('a3', 'Three')], [('a1', 'One'), ('a3', 'Three')], [('a3', 'Three')]]
    steps = []
    for i, st in enumerate(fixture()):
        st = dict(st, blobs=[dict(b, area='a3' if i == 5 else 'a2' if b['id'] == 'B' and i < 2 else 'a1')
                             for b in st['blobs']])
        if i == 3:
            p = dict(blob('A>m.p', [fn('m.p', 40, 60, state='new', added=21)], 'new', 21), cut='A', area='a3')
            st['blobs'].append(p)
            st['blobs'][0] = dict(st['blobs'][0], merged=[{'id': 'T', 'name': 't', 'emoji': '', 'kind': 'hub',
                                                           'lines': 4, 'funcs': ['m.a4']}])
        stats = st.pop('stats')            # a step's keys in the order timeline.py writes them
        st['areas'] = [{'id': a, 'caption': c, 'faces': ['x']} for a, c in tables[i]]
        st['stats'] = stats
        steps.append(st)
    return steps


def node_expand(path):
    """The page's /* <expand> */ block run in node on a timeline file -> its expanded steps."""
    with open(PAGE, encoding='utf-8') as fh:
        html = fh.read()
    block = html[html.index('/* <expand>'):html.index('/* </expand> */')]
    js = block + ("\nconst tl=JSON.parse(require('fs').readFileSync(process.argv[1],'utf8'));"
                  "process.stdout.write(JSON.stringify(expandSteps(tl)));")
    r = subprocess.run(['node', '-e', js, path], capture_output=True, text=True, check=True, timeout=300)
    return json.loads(r.stdout)


def py_expand(path):
    with open(path, encoding='utf-8') as fh:
        return timeline.expand(json.load(fh))


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed: the page block cannot run')
@pytest.mark.parametrize('name', ['fixture', 'fixture5', 'timeline.json', 'sample-timeline.json'])
def test_page_expand_agrees_with_python(name, tmp_path):
    # the demo timeline (v5) and the committed sample (v3) as the page loads them, and the fixtures above in v4 / v5.
    # Step-level key order differs (the page keeps `stats` where the step had it), so keys are compared sorted.
    if name == 'fixture':
        path = tmp_path / 'fixture.json'
        path.write_text(dump({'schema': 4, 'steps': timeline.compact(fixture(), 4)}), encoding='utf-8')
        path = str(path)
    elif name == 'fixture5':
        path = tmp_path / 'fixture5.json'
        v5 = timeline.compact(fixture5())
        assert [sorted(k for k in s if k.startswith('areas')) for s in v5] == [
            ['areas'], [], ['areas'], ['areas'], ['areas_remove'], ['areas_remove']]
        path.write_text(dump({'schema': 5, 'steps': v5}), encoding='utf-8')
        path = str(path)
    else:
        path = os.path.join(REPO, 'web', name)
    py, js = py_expand(path), node_expand(path)
    assert len(py) == len(js) > 1
    for i, (p, j) in enumerate(zip(py, js)):
        assert dump(p, sort_keys=True) == dump(j, sort_keys=True), f'{name} step {i}: page and expand() differ'
    if name == 'fixture':
        assert dump(py) == dump(fixture())
    if name == 'fixture5':
        assert dump(py) == dump(fixture5())
    if name == 'timeline.json':
        assert all('areas' in s for s in py)
