"""Incremental analysis (analyze(memo=...)) and the schema v3 / v4 delta timelines against a full recompute per step."""
import json, os

import pytest

import replay, timeline

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SESSION = os.path.expanduser('~/.claude/projects/-home-rontero-Documents-Projects-AIProjects-HarnessTools-aiblob-demo-app')
APP = os.path.join(REPO, 'demo', 'app')
MINI = os.path.join(HERE, 'fixtures', 'js-mini')


def dump(x):
    return json.dumps(x, ensure_ascii=False, separators=(',', ':'))


def run(snapshots, full):
    """Full-state steps of a list of (file, {relpath: text}) snapshots, timings zeroed."""
    pairs = [(replay.Step(i, '', 'Write', f, '', 0), snap) for i, (f, snap) in enumerate(snapshots)]
    return list(timeline.analyzed_steps(iter(pairs), full=full, timings=False))


def check(snapshots):
    """Incremental == full recompute, byte for byte, and v4 and v3 round-trip through compact / expand."""
    inc, full = run(snapshots, False), run(snapshots, True)
    for a, b in zip(inc, full):
        assert dump(a) == dump(b), f"step {a['idx']} ({a['file']}): incremental differs from a full recompute"
    assert len(inc) == len(full) == len(snapshots)
    v3 = {'schema': 3, 'steps': timeline.compact(full, 3)}
    assert dump(timeline.expand(json.loads(dump(v3)))) == dump(full)
    v4 = {'schema': 4, 'steps': timeline.compact(full)}
    assert dump(timeline.expand(json.loads(dump(v4)))) == dump(full)
    return inc, v4


def sequence(edits):
    """[(relpath, text or None to delete)] -> snapshots after each edit."""
    snap, out = {}, []
    for rel, text in edits:
        snap = dict(snap)
        if text is None:
            snap.pop(rel, None)
        else:
            snap[rel] = text
        out.append((rel, snap))
    return out


def test_python_symbol_changes_reach_modules_that_do_not_import_the_change():
    # c.py defines a second `save`: app.py's `note.save()` stops resolving by unique method name, although
    # app.py never imports c.py; then a new module-level global in models.py becomes data of app.py's handler
    app = ('from flask import Flask\nfrom models import Note, load\n\napp = Flask(__name__)\n\n'
           '@app.route("/n")\ndef show():\n    note = load()\n    note.save()\n    return LIMIT\n\n'
           'if __name__ == "__main__":\n    app.run()\n')
    models = 'class Note:\n    def save(self):\n        return 1\n\ndef load():\n    return Note()\n'
    steps = sequence([
        ('models.py', models),
        ('app.py', app),
        ('c.py', 'class Other:\n    def save(self):\n        return 2\n'),
        ('app.py', app.replace('from models import Note, load', 'from models import Note, load, LIMIT')),
        ('models.py', models + '\nLIMIT = 10\n'),
        ('c.py', None),
        ('models.py', models.replace('def load():', 'def helper():\n    return 0\n\ndef load():')),
        ('README.md', '# notes\nsee app.py\n'),
        ('app.py', app.replace('note.save()', 'note.save()\n    helper()').replace('import Note, load', 'import Note, load, helper')),
    ])
    inc, v3 = check(steps)
    calls = lambda i: {f['qual']: [c['to'] for c in f['calls']] for b in inc[i]['blobs'] for f in b['funcs']}
    assert 'models.Note.save' in calls(1)['app.show']           # the only `save`: placed by its unique name
    assert 'models.Note.save' not in calls(2)['app.show']       # two `save`s: no longer
    assert any('global:models.LIMIT' in b['data'] for b in inc[4]['blobs'])
    assert not any(s.get('full') for s in v3['steps'][1:])


def test_js_fixture_built_and_edited_step_by_step():
    files = {}
    for dp, _, fn in os.walk(MINI):
        for n in sorted(fn):
            p = os.path.join(dp, n)
            with open(p, encoding='utf-8') as fh:
                files[os.path.relpath(p, MINI).replace(os.sep, '/')] = fh.read()
    order = ['util.js', 'store.js', 'README.md', 'server.js', 'public/index.html']
    edits = [(rel, files[rel]) for rel in order]
    edits += [
        ('util.js', files['util.js'].replace('module.exports = { isValidName, logError };',
                                             'function list() {\n  return [];\n}\n\nmodule.exports = { isValidName, logError, list };')),
        ('server.js', files['server.js'].replace("res.json(store.list());", "res.json(store.list());\n  console.log('listed');")),
        ('store.js', files['store.js'].replace('  list() {', '  all() {\n    return this.list();\n  }\n\n  list() {')),
        ('public/index.html', None),
        ('server.js', files['server.js'].replace("app.get('/items'", "app.get('/all', (req, res) => res.json(store.all()));\n\napp.get('/items'")),
    ]
    check(sequence(edits))


@pytest.mark.skipif(not os.path.isdir(SESSION), reason='recorded demo session not on this machine')
def test_flask_session_incremental_equals_full():
    inc = timeline.timeline(SESSION, APP, timings=False)
    full = timeline.timeline(SESSION, APP, timings=False, full=True)
    v2 = timeline.timeline(SESSION, APP, timings=False, v2=True)
    assert dump(inc) == dump(full)
    assert inc['schema'] == 4 and 'schema' not in v2
    assert dump(timeline.expand(json.loads(dump(inc)))) == dump(v2['steps'])
    assert len(dump(inc)) < len(dump(v2)) / 3
    v3 = dict(inc, schema=3, steps=timeline.compact(v2['steps'], 3))
    assert dump(timeline.expand(json.loads(dump(v3)))) == dump(v2['steps'])
    assert len(dump(inc)) <= len(dump(v3))


def test_sample_timeline_is_v3_and_expands():
    with open(os.path.join(REPO, 'web', 'sample-timeline.json'), encoding='utf-8') as fh:
        tl = json.load(fh)
    assert tl['schema'] == 3 and tl['steps'][0].get('full')
    steps = timeline.expand(tl)
    assert len(steps) == len(tl['steps']) and all('edges_add' not in s for s in steps)
    assert dump(timeline.compact(steps, 3)) == dump(tl['steps'])
    assert dump(timeline.expand({'schema': 4, 'steps': timeline.compact(steps)})) == dump(steps)


def test_expand_reads_deltas_only_from_a_v3_dict():
    # a blob that disappears: full steps (they carry `edges`), bare or in a dict without schema, are not deltas
    a = {'id': 'a', 'kind': 'hub', 'funcs': [], 'delta': {'added': 1, 'removed': 0, 'moved': 0, 'state': 'new', 'removed_funcs': []}}
    b = dict(a, id='b')
    full = [{'idx': 0, 'blobs': [a, b], 'edges': [], 'stats': {}}, {'idx': 1, 'blobs': [a], 'edges': [], 'stats': {}}]
    assert [len(s['blobs']) for s in timeline.expand(full)] == [2, 1]
    assert [len(s['blobs']) for s in timeline.expand({'steps': full})] == [2, 1]
    v4 = {'schema': timeline.SCHEMA, 'steps': timeline.compact(full)}
    assert v4['steps'][1]['removed_blobs'] == ['b']
    assert dump(timeline.expand(v4)) == dump(full) == dump(timeline.expand(v4['steps']))

