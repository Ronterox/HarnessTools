"""The recorded Flask demo session, end to end: golden final state plus invariants on every step."""
import json, os, subprocess, sys

import pytest

import timeline

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SESSION = os.path.expanduser('~/.claude/projects/-home-rontero-Documents-Projects-AIProjects-HarnessTools-aiblob-demo-app')
APP = os.path.join(REPO, 'demo', 'app')
GOLDEN = os.path.join(HERE, 'golden', 'flask_final.json')

pytestmark = pytest.mark.skipif(not os.path.isdir(SESSION), reason='recorded demo session not on this machine')


def _run(out):
    subprocess.run([sys.executable, os.path.join(REPO, 'timeline.py'), SESSION, '--root', APP, '--no-timings', '-o', str(out)],
                   check=True, capture_output=True, cwd=REPO)
    return out


@pytest.fixture(scope='module')
def runs(tmp_path_factory):
    d = tmp_path_factory.mktemp('flask')
    a, b = _run(d / 'a.json'), _run(d / 'b.json')
    return a.read_bytes(), b.read_bytes()


@pytest.fixture(scope='module')
def tl(runs):
    """The run's full state per step: timeline.json is schema v5 (deltas), expand() rebuilds each step."""
    data = json.loads(runs[0])
    assert data['schema'] == 5
    return {'steps': timeline.expand(data)}


def summary(step):
    blobs = [{'id': b['id'], 'kind': b['kind'], 'name': b['name'], 'module': b['module'], 'area': b['area'],
              **({'cut': b['cut']} if 'cut' in b else {}),
              **({'merged': [m['id'] for m in b['merged']]} if 'merged' in b else {}),
              'funcs': sorted(f['qual'] for f in b['funcs'])} for b in step['blobs']]
    edges = [{k: e[k] for k in ('a', 'b', 'kind', 'n')} for e in step['edges']]
    return {'blobs': sorted(blobs, key=lambda b: b['id']),
            'edges': sorted(edges, key=lambda e: (e['kind'], e['a'], e['b'])),
            'areas': step['areas']}


def test_final_step_matches_golden(tl):
    got = summary(tl['steps'][-1])
    if os.environ.get('UPDATE_GOLDEN'):
        with open(GOLDEN, 'w') as fh:
            json.dump(got, fh, indent=1)
            fh.write('\n')
    with open(GOLDEN) as fh:
        assert got == json.load(fh)


def test_step_count(tl):
    assert len(tl['steps']) == 18


def test_two_runs_are_byte_identical(runs):
    assert runs[0] == runs[1]


def test_blob_delta_is_sum_of_func_deltas(tl):
    for st in tl['steps']:
        for b in st['blobs']:
            if b['kind'] in ('file', 'text'):
                continue
            d, fs = b['delta'], b['funcs']
            assert d['added'] == sum(f['delta']['added'] for f in fs), (st['idx'], b['id'])
            removed = sum(f['delta']['removed'] for f in fs)
            if d['removed_funcs']:
                assert d['removed'] > removed, (st['idx'], b['id'])    # plus the lines of functions deleted from the app
            else:
                assert d['removed'] == removed, (st['idx'], b['id'])


def test_ids_states_and_edge_invariants(tl):
    blob_states, func_states = {'new', 'changed', 'same'}, {'new', 'changed', 'same', 'moved'}
    for st in tl['steps']:
        i = st['idx']
        ids = [b['id'] for b in st['blobs']]
        assert len(ids) == len(set(ids)), f'step {i}: duplicate blob ids'
        by_id = {b['id']: b for b in st['blobs']}
        for b in st['blobs']:
            assert b['delta']['state'] in blob_states, (i, b['id'])
            for f in b['funcs']:
                assert f['delta']['state'] in func_states, (i, f['qual'])
        seen = set()
        for e in st['edges']:
            assert e['a'] in by_id and e['b'] in by_id, (i, e)
            key = (e['a'], e['b'], e['kind'])
            assert key not in seen, (i, 'duplicate edge', key)
            seen.add(key)
            if e['kind'] == 'data':
                assert e.get('via'), (i, e)
            if e['kind'] == 'ref':
                assert by_id[e['b']]['kind'] == 'file', (i, e)
            for end in (e['a'], e['b']):
                assert by_id[end]['kind'] != 'text', (i, 'text blob with an edge', e)
        for b in st['blobs']:
            if b['kind'] == 'text':
                assert not any(e['a'] == b['id'] or e['b'] == b['id'] for e in st['edges'])
