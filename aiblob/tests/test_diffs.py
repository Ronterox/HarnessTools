"""diffs.py: timeline.py --diffs leaves timeline.json byte for byte as it was; every hash the steps name is in the
store; diffs.Reader.range_texts() gives the texts the replay snapshots hold; a seeded step diffs against its base."""
import gzip, hashlib, json, os, random, subprocess, sys

import pytest

import diffs, replay, timeline
from test_live import APP, FLASK, _fixture_session, ts
from test_replay import ROOT, SID, msg, result, tool, write_lines

HERE = os.path.dirname(os.path.abspath(__file__))
TL = os.path.join(os.path.dirname(HERE), 'timeline.py')


def _tree(d):
    """{relpath: bytes} of a directory."""
    return {os.path.relpath(os.path.join(p, f), d): open(os.path.join(p, f), 'rb').read()
            for p, _, fs in os.walk(d) for f in fs}


def _sources(tmp_path):
    out = [('js', _fixture_session(tmp_path / 'js', 'js-mini', '/work/js',
                                   [('server.js', 'express', 'express'), ('util.js', 'function', 'function ')]), '/work/js'),
           ('c', _fixture_session(tmp_path / 'c', 'c-mini', '/work/c',
                                  [('main.c', 'int main', 'int  main'), ('util.c', 'return', 'return ')]), '/work/c')]
    if os.path.isdir(FLASK):
        out.append(('flask', FLASK, APP))
    return out


def test_timeline_is_byte_identical_with_and_without_diffs(tmp_path):
    for name, src, root in _sources(tmp_path):
        run = lambda *extra: subprocess.run([sys.executable, TL, src, '--root', root, '--no-timings', *extra],
                                            check=True, capture_output=True)
        run('-o', str(tmp_path / f'{name}-plain.json'))
        run('-o', str(tmp_path / f'{name}-d1.json'), '--diffs', str(tmp_path / f'{name}-d1'))
        run('-o', str(tmp_path / f'{name}-d2.json'), '--diffs', str(tmp_path / f'{name}-d2'))
        plain = (tmp_path / f'{name}-plain.json').read_bytes()
        assert (tmp_path / f'{name}-d1.json').read_bytes() == plain, name
        assert (tmp_path / f'{name}-d2.json').read_bytes() == plain, name
        assert _tree(tmp_path / f'{name}-d1') == _tree(tmp_path / f'{name}-d2'), name     # (gzip without mtime)


def build(tmp_path, src, root, smart=True, **kw):
    """An in-process build with --diffs -> (replay steps, Reader)."""
    rs = replay.steps(replay.transcripts(src), root, **kw)
    out = str(tmp_path / 'd')
    dw = diffs.Static(out)
    tl = timeline.timeline(src, root, timings=False, steps=rs, diffs=dw, smart=smart)
    dw.close()
    with open(tmp_path / 't.json', 'w', encoding='utf-8') as fh:
        json.dump(tl, fh, ensure_ascii=False, separators=(',', ':'))
    return rs, diffs.load_static(str(tmp_path / 't.json'), out)


def named(r):
    """Every hash the steps name: their functions' and their file lines'."""
    hs = {f['hash'] for st in r.steps for b in st['blobs'] for f in b['funcs']}
    hs |= {h for fv in r.fvs for k in ('f', 'seed', 'sq') for h in (fv.get(k) or {}).values()
           if h and not h.startswith('bin:')}
    return hs


def complete(r):
    hs = sorted(named(r))
    got = r.get(hs)
    assert all(got[h] is not None and diffs.h10(got[h]) == h for h in hs), [h for h in hs if got[h] is None][:5]
    return len(hs)


def _slice(snap, f):
    """A function's lines start..end cut from a snapshot."""
    lines = snap[f['file']].split('\n')                 # (tree-sitter's lines: \\n only)
    return '\n'.join(lines[f['start'] - 1:f['end']]).strip()


def check_ranges(rs, r, pairs=60, seed=0):
    """range_texts() for random (a, b) and blobs against the replay snapshots: a function's texts are its lines in
    the snapshot before a and after b (functions that are one run of lines), a file blob's the whole file."""
    rnd, n, checked = random.Random(seed), len(r.steps), 0
    for _ in range(pairs):
        a = rnd.randrange(n)
        b = rnd.randrange(a, n)
        old, new = r.steps[a - 1] if a else {'blobs': []}, r.steps[b]
        ids = sorted({x['id'] for x in old['blobs'] + new['blobs']})
        q0 = {f['qual']: f for x in old['blobs'] for f in x['funcs']}
        q1 = {f['qual']: f for x in new['blobs'] for f in x['funcs']}
        for bid in rnd.sample(ids, min(4, len(ids))):
            for e in r.range_texts(a, b, bid):
                if e['kind'] == 'file':
                    s0 = rs[a - 1].content if a else {}
                    if not e.get('binary') and not e.get('seeded') and not e.get('renamed_from'):
                        assert e['old'] == s0.get(e['key']), (a, b, e['key'])
                    if not e.get('binary'):
                        assert e['new'] == rs[b].content.get(e['key']), (a, b, e['key'])
                    checked += 1
                    continue
                q = e['key']
                for side, qs, snap, h in (('new', q1, rs[b].content, e['newh']), ('old', q0, rs[a - 1].content if a else {}, e['oldh'])):
                    f = qs.get(q)
                    if f is None or e.get('renamed_from') and side == 'old':
                        continue
                    assert h == f['hash'] and diffs.h10(e[side]) == h
                    if f['name'] != '<module>' and e[side].count('\n') == f['end'] - f['start']:
                        assert e[side].strip() in _slice(snap, f), (a, b, q, side)   # (JS: a body may start mid-line)
                        checked += 1
                assert e['state'] == ('new' if e['oldh'] is None else 'deleted' if e['newh'] is None else
                                      'same' if e['oldh'] == e['newh'] else 'changed')
    return checked


def test_store_complete_and_ranges_match_the_snapshots(tmp_path):
    for k, (name, src, root) in enumerate(_sources(tmp_path)):
        rs, r = build(tmp_path / name, src, root)
        assert len(r.fvs) == len(r.steps) == len(rs)
        assert complete(r) > 0
        assert check_ranges(rs, r, seed=k) > 20, name


@pytest.mark.parametrize('seed', [0, 3, 26])
def test_random_sessions_store_complete_and_ranges_match(tmp_path, seed):
    import randsession
    proj, root = randsession.random_project(random.Random(seed), str(tmp_path / 'g'))
    rs, r = build(tmp_path, proj, root or ROOT)
    complete(r)
    check_ranges(rs, r, pairs=40, seed=seed)


def _git_repo(tmp_path, files):
    repo = tmp_path / 'repo'
    repo.mkdir()
    env = {'GIT_AUTHOR_DATE': ts(0), 'GIT_COMMITTER_DATE': ts(0), 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t', 'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'}
    for rel, text in files.items():
        (repo / rel).write_text(text)
    for cmd in (['init', '-q'], ['add', '.'], ['commit', '-qm', 'base']):
        subprocess.run(['git', '-C', str(repo), *cmd], check=True, env=env, capture_output=True)
    return str(repo)


BASE = 'def keep():\n    return 1\n\n\ndef edited(x):\n    y = x + 1\n    return y\n\n\ndef tail():\n    return 3\n'


def test_a_seeded_step_diffs_against_its_base(tmp_path):
    """An Edit of a file the session never wrote, its base from git (--seed-git): the step's functions diff
    against their base bodies, not as new; a file blob the same against its base text."""
    root = _git_repo(tmp_path, {'m.py': BASE, 'notes.md': '# notes\nold line\n', 'x.py': 'import m\n'})
    write_lines(tmp_path / 'src' / f'{SID}.jsonl', [
        msg('user', 'go', ts=ts(1)),
        msg('assistant', [tool('e1', 'Edit', file_path=f'{root}/m.py', old_string='    y = x + 1\n',
                               new_string='    y = x + 2\n    y *= 2\n')], ts=ts(2), cwd=root),
        dict(result('e1'), timestamp=ts(2, 1)),
        msg('assistant', [tool('e2', 'Edit', file_path=f'{root}/notes.md', old_string='old line', new_string='new line')],
            ts=ts(3), cwd=root),
        dict(result('e2'), timestamp=ts(3, 1))])
    rs, r = build(tmp_path, str(tmp_path / 'src'), root, seed_git=True)
    assert [st.seeded for st in rs] == [True, True] and rs[0].before == {'m.py': BASE}
    assert r.fvs[0]['seed'] == {'m.py': diffs.h10(BASE)} and set(r.fvs[0]['sq']) == {'m.keep', 'm.edited', 'm.tail'}
    complete(r)
    blob = next(b['id'] for b in r.steps[0]['blobs'] for f in b['funcs'] if f['qual'] == 'm.edited')
    for a, b in ((0, 0), (0, 1)):                       # this step, and since the start
        got = {e['key']: e for e in r.range_texts(a, b, blob)}
        e = got['m.edited']
        assert e['seeded'] and e['state'] == 'changed'
        assert e['old'] == 'def edited(x):\n    y = x + 1\n    return y'
        assert e['new'] == 'def edited(x):\n    y = x + 2\n    y *= 2\n    return y'
        others = [g for k, g in got.items() if k in ('m.keep', 'm.tail')]
        assert others and all(g['state'] == 'same' and g['seeded'] for g in others)
    f = r.range_texts(1, 1, 'file:notes.md')[0]
    assert f['seeded'] and f['state'] == 'changed' and (f['old'], f['new']) == ('# notes\nold line\n', '# notes\nnew line\n')
    # without the seed, the same edits read as all new
    plain = diffs.Reader(r.steps, [{k: v for k, v in fv.items() if k == 'f'} for fv in r.fvs], r.get)
    assert {e['state'] for e in plain.range_texts(0, 0, blob)} == {'new'}


def test_moves_renames_and_deletes(tmp_path):
    """A function moving to another blob, one moving to another file (another qual, the same body), one deleted, a
    file added (blobs as analysed: --no-smart)."""
    w = lambda k, path, text, t: [msg('assistant', [tool(f'w{k}', 'Write', file_path=f'{ROOT}/{path}', content=text)],
                                      ts=ts(t)), dict(result(f'w{k}'), timestamp=ts(t, 1))]
    app = lambda imp, a, b: f'from flask import Flask\n{imp}\napp = Flask(__name__)\n\n@app.route("/a")\ndef a():\n' \
        f'    return {a}\n\n@app.route("/b")\ndef b():\n    return {b}\n'
    h2 = 'def h2():\n    y = 2\n    return y\n'
    helpers = 'def h1():\n    x = 1\n    return x\n\n' + h2 + '\ndef gone():\n    return 0\n'
    recs = [msg('user', 'go', ts=ts(0))]
    recs += w(1, 'h.py', helpers, 1) + w(2, 'app.py', app('from h import h1, h2', 'h1()', 'h2()'), 2)
    recs += w(3, 'g.py', h2, 3) + w(4, 'h.py', 'def h1():\n    x = 1\n    return x\n', 4)
    recs += w(5, 'app.py', app('from h import h1\nfrom g import h2', '0', 'h1() + h2()'), 5)
    write_lines(tmp_path / 'src' / f'{SID}.jsonl', recs)
    rs, r = build(tmp_path, str(tmp_path / 'src'), ROOT, smart=False)
    complete(r)
    got = {e['key']: e for e in r.range_texts(4, 4, 'app.b')}
    assert got['h.h1']['moved_from'] == 'app.a' and got['h.h1']['state'] == 'same'
    assert got['g.h2']['moved_from'] == 'orphan:g'
    got = {e['key']: e for e in r.range_texts(2, 4, 'app.b')}         # g.py came after step 1: h2 changed file
    assert got['g.h2']['renamed_from'] == 'h.h2' and got['g.h2']['state'] == 'same' and got['g.h2']['old'] == h2.strip()
    assert 'h.h2' not in got
    assert {e['key']: e.get('moved_to') for e in r.range_texts(4, 4, 'app.a')}['h.h1'] == 'app.b'
    home = {f['qual']: b['id'] for b in r.steps[2]['blobs'] for f in b['funcs']}
    gone = {e['key']: e for e in r.range_texts(3, 3, home['h.gone'])}
    assert gone['h.gone']['state'] == 'deleted' and gone['h.gone']['old'] == 'def gone():\n    return 0'
    assert r.range_texts(0, 4, 'file:README.md') == []


def test_reader_file_blobs_renamed_deleted_binary_seeded():
    """Reader rules on file blobs, from hand-made steps: a file gone with the same text as a new one is a rename, a
    blob gone at b reads its old text from a-1, a binary file has no texts, a seed fills an old side a-1 lacks."""
    fb = lambda p: {'id': 'file:' + p, 'kind': 'text', 'path': p, 'funcs': []}
    texts = {diffs.h10(t): t for t in ('one\n', 'base\n', 'after\n')}
    h = {t: diffs.h10(t) for t in texts.values()}
    steps = [{'blobs': [fb('a.txt'), fb('img.png')]}, {'blobs': [fb('b.txt'), fb('img.png')]}, {'blobs': [fb('b.txt'), fb('c.md')]}]
    fvs = [{'f': {'a.txt': h['one\n'], 'img.png': 'bin:12'}}, {'f': {'a.txt': None, 'b.txt': h['one\n'], 'img.png': 'bin:14'}},
           {'f': {'img.png': None, 'c.md': h['after\n']}, 'seed': {'c.md': h['base\n']}}]
    r = diffs.Reader(steps, fvs, lambda hs: {x: texts.get(x) for x in hs})
    [e] = r.range_texts(1, 1, 'file:b.txt')
    assert (e['state'], e['renamed_from'], e['old'], e['new']) == ('same', 'a.txt', 'one\n', 'one\n')
    [e] = r.range_texts(1, 2, 'file:a.txt')
    assert (e['state'], e['old'], e['new']) == ('deleted', 'one\n', None)
    [e] = r.range_texts(1, 1, 'file:img.png')
    assert e['binary'] and e['state'] == 'changed' and e['old'] is e['new'] is None
    [e] = r.range_texts(0, 2, 'file:c.md')
    assert e['seeded'] and (e['state'], e['old'], e['new']) == ('changed', 'base\n', 'after\n')
    assert r.range_texts(0, 2, 'file:nowhere') == []



# seeded files: (path, base, old_string, new_string, the quals' last names the edit changes)
SEEDED = {
    'py-trailing-comment': ('m.py', 'def keep():\n    return 1  # c\n\n\ndef edited(x):\n    return x\n', '    return x\n',
                            '    return x + 1\n', {'edited'}),
    'py-method': ('m.py', 'class K:\n    def keep(self):\n        return 1\n\n    def edited(self):\n        return 2\n',
                  'return 2', 'return 3', {'edited'}),
    'py-decorated': ('m.py', 'import functools\n\n@functools.cache\ndef keep():\n    return 1\n\n\ndef edited(x):\n    return x\n',
                     'return x\n', 'return x * 2\n', {'edited'}),
    'js-callback': ('s.js', "app.get('/a', function (req, res) {\n  res.send('a');\n});\n\nfunction edited(x) {\n  return x;\n}\n",
                    'return x;', 'return x + 1;', {'edited'}),
    'js-arrow': ('s.js', "const keep = () => {\n  return 1;\n};\n\nfunction edited(x) {\n  return x;\n}\n", 'return x;',
                 'return x + 1;', {'edited'}),
    'py-edit-across-two': ('m.py', 'def f():\n    a = 1\n    return a\n\n\ndef g():\n    b = 2\n    return b\n',
                           '    return a\n\n\ndef g():\n    b = 2\n', '    return a + 1\n\n\ndef g():\n    b = 3\n', {'f', 'g'}),
    'py-crlf': ('m.py', 'def keep():\r\n    return 1\r\n\r\n\r\ndef edited(x):\r\n    return x\r\n', 'return x', 'return x + 1',
                {'edited'}),
    'py-formfeed': ('m.py', 'def a():\n    return 1\n\x0c\n\ndef keep():\n    return 2\n\n\ndef edited(x):\n    return x\n',
                    'return x\n', 'return x + 1\n', {'edited'}),
    'py-u2028-in-string': ('m.py', 'def a():\n    return "x\u2028y"\n\n\ndef keep():\n    return 2\n\n\ndef edited(x):\n    return x\n',
                           'return x\n', 'return x + 1\n', {'edited'}),
}


@pytest.mark.parametrize('case', sorted(SEEDED))
def test_seeded_functions_diff_against_their_parsed_base(tmp_path, case):
    """A seeded file's functions read `same` unless the edit changed them, and a changed one's old side is its body
    in the base as the analyzer parses it (CRLF, a form feed, U+2028 in a string, JS callbacks and arrows too)."""
    import analyze
    rel, base, old, new, edited = SEEDED[case]
    root = _git_repo(tmp_path, {rel: base})
    write_lines(tmp_path / 'src' / f'{SID}.jsonl', [
        msg('user', 'go', ts=ts(1)),
        msg('assistant', [tool('e1', 'Edit', file_path=f'{root}/{rel}', old_string=old, new_string=new)], ts=ts(2), cwd=root),
        dict(result('e1'), timestamp=ts(2, 1))])
    rs, r = build(tmp_path, str(tmp_path / 'src'), root, seed_git=True)
    assert rs[0].seeded and rs[0].before == {rel: base} and 'sqa' not in r.fvs[0]
    truth = analyze.analyze({rel: base})['bodies']                   # (the whole analysis of the base alone)
    got = [e for b in r.steps[0]['blobs'] if b['kind'] not in ('file', 'text') for e in r.range_texts(0, 0, b['id'])]
    assert got and all(e['seeded'] for e in got)
    for e in got:
        name = e['key'].rsplit('.', 1)[-1]
        assert e['state'] == ('changed' if name in edited else 'same'), (e['key'], e['old'], e['new'])
        if e['key'] in truth:
            assert e['old'] == truth[e['key']], e['key']


def test_sliced_bases_split_lines_as_tree_sitter_does():
    """The fallback (a base the parser cannot take): lines split at \n only, a run deleted inside a function counted."""
    sink = diffs.BodySink()
    f = lambda q, start, end: {'qual': q, 'name': q, 'start': start, 'end': end}
    after = 'def a():\n    return "x\u2028y"\n\x0c\ndef keep():\r\n    return 2\r\n'
    base = 'def a():\n    return "x\u2028y"\n\x0c\ndef keep():\r\n    x = 0\r\n    return 2\r\n'
    got = sink._sliced(base, after, [(f('m.a', 1, 2), 'def a():\n    return "x\u2028y"'),
                                     (f('m.keep', 4, 5), 'def keep():\r\n    return 2')])
    texts = dict(sink.new)
    assert texts[got['m.a']] == 'def a():\n    return "x\u2028y"'
    assert texts[got['m.keep']] == 'def keep():\r\n    x = 0\r\n    return 2'


def test_a_seed_from_after_the_key_was_made_is_not_its_base():
    """A function / file made inside a..b and seeded later in a..b (a drifted originalFile) reads new, not changed."""
    fn = lambda q, h: {'qual': q, 'name': q, 'hash': h}
    code = lambda *fs: {'id': 'b', 'kind': 'route', 'funcs': list(fs)}
    fb = {'id': 'file:n.md', 'kind': 'text', 'path': 'n.md', 'funcs': []}
    steps = [{'blobs': []}, {'blobs': [code(fn('m.f', 'aaaaaaaaaa')), fb]}, {'blobs': [code(fn('m.f', 'bbbbbbbbbb')), fb]}]
    fvs = [{}, {'f': {'n.md': 'cccccccccc'}}, {'f': {'n.md': 'dddddddddd'}, 'seed': {'n.md': 'eeeeeeeeee'},
                                               'sq': {'m.f': 'ffffffffff'}}]
    r = diffs.Reader(steps, fvs, lambda hs: {h: h for h in hs})
    [e] = r.range_texts(1, 2, 'b')
    assert e['state'] == 'new' and 'seeded' not in e
    [e] = r.range_texts(1, 2, 'file:n.md')
    assert e['state'] == 'new' and 'seeded' not in e
    r = diffs.Reader([{'blobs': []}, {'blobs': []}, steps[2]], fvs, r.get)    # first shown at the seeded step
    [e] = r.range_texts(1, 2, 'b')
    assert e['state'] == 'changed' and e['seeded'] and e['oldh'] == 'ffffffffff'


def test_renames_pair_each_once_and_a_copy_is_no_rename():
    fn = lambda q, h: {'qual': q, 'name': q.rsplit('.', 1)[-1], 'hash': h}
    code = lambda *fs: {'id': 'b', 'kind': 'route', 'funcs': list(fs)}
    fb = lambda p: {'id': 'file:' + p, 'kind': 'text', 'path': p, 'funcs': []}
    steps = [{'blobs': [code(fn('x.f', 'aaaaaaaaaa')), fb('a.txt')]},
             {'blobs': [code(fn('y.f', 'aaaaaaaaaa'), fn('z.f', 'aaaaaaaaaa')), fb('a.txt'), fb('b.txt')]}]
    fvs = [{'f': {'a.txt': '1111111111'}}, {'f': {'b.txt': '1111111111'}}]
    r = diffs.Reader(steps, fvs, lambda hs: {h: h for h in hs})
    got = {e['key']: e for e in r.range_texts(1, 1, 'b')}
    assert [got[q].get('renamed_from') for q in ('y.f', 'z.f')] == ['x.f', None] and got['z.f']['state'] == 'new'
    [e] = r.range_texts(1, 1, 'file:b.txt')                         # a.txt is still there: a copy
    assert e['state'] == 'new' and 'renamed_from' not in e


def test_tail_lines_are_found_by_their_digest(tmp_path):
    d = tmp_path / 'diffs'
    d.mkdir()
    a, b = ['{"idx":1}', '{"idx":2}'], ['{"idx":1}', '{"idx":3}']
    (d / 'tail.json').write_text(json.dumps({'tails': [
        {'base': 1, 'h': diffs.tail_digest(a), 'fv': [{}, {'f': {'x': 'aaaaaaaaaa'}}]},
        {'base': 1, 'h': diffs.tail_digest(b), 'fv': [{}, {'f': {'x': 'bbbbbbbbbb'}}]}]}))
    assert diffs.tail_fvs(str(tmp_path), b) == [{}, {'f': {'x': 'bbbbbbbbbb'}}]
    assert diffs.tail_fvs(str(tmp_path), a)[1] == {'f': {'x': 'aaaaaaaaaa'}}
    assert diffs.tail_fvs(str(tmp_path), ['{"idx":1}', '{"idx":4}']) is None


def test_pack_read_after_the_store_is_recreated_truncated_or_corrupt(tmp_path):
    p = str(tmp_path / 'bodies.ndjson')
    s = diffs.BodySink(p)
    h1, h2 = s.put('one'), s.put('two')
    s.close()
    pk = diffs.Pack(p)
    assert pk.get([h1, h2]) == {h1: 'one', h2: 'two'}
    os.unlink(p)                                    # the store deleted, a writer starts again (maybe the same inode)
    s = diffs.BodySink(p)
    h6 = s.put('six')
    s.put('two'), s.put('one')
    s.close()
    assert pk.get([h1, h2, h6]) == {h1: 'one', h2: 'two', h6: 'six'}
    data = open(p, 'rb').read()
    open(p, 'wb').write(data[:data.index(b'\n') + 5])          # shorter, a half line
    assert pk.get([h1, h2, h6]) == {h1: None, h2: None, h6: 'six'}
    lines = data.split(b'\n')
    open(p, 'wb').write(b'\n'.join([lines[0], lines[1][:14] + b'\x00garbage', lines[2], b'']))   # a corrupt line
    assert diffs.Pack(p).get([h2, h1]) == {h2: None, h1: 'one'}
    assert pk.get([h2, h6]) == {h2: None, h6: 'six'}
    with open(p, 'r+b') as fh:                      # rewritten in place (same inode, same first line), lines moved
        fh.write(b'\n'.join([lines[0], lines[2], lines[1], b'']))
    assert pk.get([h1, h2]) == {h1: 'one', h2: 'two'}


def test_a_seed_only_where_the_base_is_not_the_snapshot(tmp_path):
    """An Edit rebased on originalFile keeps that base; a report merged onto a Read's text applies to the snapshot,
    which the previous step already shows: no seed."""
    import test_replay
    test_replay.test_a_copied_call_counts_once_and_an_edit_rebases_on_original_file(tmp_path / 'o')
    first = msg('assistant', [tool('c1', 'Write', file_path=f'{ROOT}/a.py', content='a = 1\n')], ts=ts(1))
    done = result('c2')
    done['toolUseResult'] = {'originalFile': 'a = 2\n'}
    p = write_lines(tmp_path / 'two.jsonl', [msg('user', 'go', ts=ts(0)), first, msg('assistant', [tool(
        'c2', 'Edit', file_path=f'{ROOT}/a.py', old_string='a = 2', new_string='a = 3')], ts=ts(3)), done])
    st = replay.steps(p, ROOT)
    assert st[1].seeded and st[1].before == {'a.py': 'a = 2\n'}
    read = result('r')
    read['toolUseResult'] = {'type': 'text', 'file': {'filePath': f'{ROOT}/f.py', 'content': 'a\nB\nc\nd\ne',
                                                      'startLine': 1, 'numLines': 5, 'totalLines': 5}}
    read['timestamp'] = ts(3)
    last = replay.steps(test_replay._stale_seen(tmp_path / 's', [
        msg('assistant', [tool('r', 'Read', file_path=f'{ROOT}/f.py')], ts=ts(2)), read]), ROOT)[-1]
    assert last.seeded and last.before is None


def test_an_entry_matches_a_step_only_with_the_same_base():
    import live
    st = replay.Step(3, ts(1), 'Edit', 'a.py', '', 0, content={'a.py': 'a = 3\n'}, seeded=True, before={'a.py': 'a = 2\n'})
    e = live.Entry(st, '{}', '{}', None, None)
    assert e.matches(st)
    assert not e.matches(replay.Step(3, ts(1), 'Edit', 'a.py', '', 0, content={'a.py': 'a = 3\n'}, seeded=True,
                                     before={'a.py': 'a = 1\n'}))
