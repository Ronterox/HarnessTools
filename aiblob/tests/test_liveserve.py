"""liveserve.py against a simulated writer (livesim.SimWriter): what it serves equals label.py on the export, SSE
resumes from Last-Event-ID, the tail follows provisional.json, a rebuild is announced, a half-written line waits."""
import http.client, json, os, random, shutil, signal, socket, subprocess, sys, threading, time

import pytest

import areas, label, liveserve, timeline
from livesim import SimWriter, dumps, raw_timeline

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FLASK = os.path.join(REPO, 'web', 'timeline.json')
AIG = os.path.join(REPO, 'web', 'timeline-aigource.json')


@pytest.fixture(scope='module')
def flask():
    return raw_timeline(FLASK)


@pytest.fixture
def cache(tmp_path):
    p = tmp_path / 'labels.json'
    shutil.copy(os.path.join(REPO, 'demo', 'labels.json'), p)
    return str(p)


class Served:
    """liveserve.serve() on a port of its own, polling fast; stopped at the end."""

    def __init__(self, store, labels, **kw):
        self.srv, self.live, self.stop, self.poller = liveserve.serve(store, labels, port=0, poll=0.03, **kw)
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        self.t.start()

    def get(self, path, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        c.request('GET', path, headers=headers or {})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r, body

    def timeline(self):
        r, body = self.get('/live/timeline.json')
        assert r.status == 200
        return json.loads(body)

    def settle(self, store):
        """Until a poll has read the store as it is now (and a little more, for that poll to end)."""
        names = ('head.json', 'steps.ndjson', 'provisional.json', 'status.json')
        now = {n: liveserve._stat(os.path.join(store, n)) for n in names}
        self.wait(lambda lv: all(lv.stats.get(n) == now[n] for n in names))
        time.sleep(0.1)

    def wait(self, pred, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if pred(self.live):
                return True
            time.sleep(0.02)
        raise AssertionError('timed out')

    def close(self):
        self.stop.set()
        self.srv.shutdown()
        self.srv.server_close()
        self.poller.join(5)


@pytest.fixture
def served():
    out = []
    yield lambda store, labels, **kw: out.append(Served(store, labels, **kw)) or out[-1]
    for s in out:
        s.close()


class Events:
    """An SSE client: next() -> (event, id, data) of the next event (comments skipped)."""

    def __init__(self, port, epoch, frm=0, last=None):
        self.c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        self.c.request('GET', f'/live/events?epoch={epoch}&from={frm}',
                       headers={'Last-Event-ID': str(last)} if last is not None else {})
        self.r = self.c.getresponse()
        assert self.r.status == 200 and self.r.getheader('Content-Type') == 'text/event-stream'

    def next(self):
        ev, eid, data = None, None, []
        while True:
            line = self.r.fp.readline().decode('utf-8')
            if not line:
                raise EOFError
            line = line.rstrip('\n')
            if not line:
                if ev or data:
                    return ev, eid, json.loads('\n'.join(data))
                continue
            if line.startswith(':'):
                continue
            k, _, v = line.partition(': ')
            if k == 'event':
                ev = v
            elif k == 'id':
                eid = int(v)
            elif k == 'data':
                data.append(v)

    def close(self):
        self.c.close()


def export(head, lines):
    """The store as live.export_text() writes it (one session, no session tags)."""
    return f'{{"root":{dumps(head["root"])},"session":"sim","schema":{head["schema"]},"steps":[{",".join(lines)}]}}'


def label_py(tmp_path, head, lines, cache):
    """label.py on the export, its server down (cache hits and kept labels only) -> (labelled steps, its report)."""
    t, c = tmp_path / 'export.json', tmp_path / 'cache-copy.json'
    t.write_text(export(head, lines), encoding='utf-8')
    shutil.copy(cache, c)
    r = subprocess.run([sys.executable, os.path.join(REPO, 'label.py'), str(t), '--cache', str(c),
                        '--server', 'http://127.0.0.1:1'], capture_output=True, text=True, check=True, cwd=REPO)
    with open(t, encoding='utf-8') as fh:
        return json.load(fh)['steps'], r.stdout


def bare(steps):
    """Served steps without the labels layer's key ids (`lk`, `mk`)."""
    return [{k: v for k, v in s.items() if k not in ('lk', 'mk')} for s in steps]


def same(a, b):
    return [dumps(s) for s in bare(a)] == [dumps(s) for s in bare(b)]


# -- labels ---------------------------------------------------------------------------------------------------------------
def first_versions_only(lines, cache):
    """The cache without the keys of a unit's later versions: label.py then keeps the first label where the
    change since is under 20%."""
    seen, drop = {}, set()
    for st in timeline.expand({'schema': 5, 'steps': [json.loads(l) for l in lines]}):
        for uid, key, _, _ in label.units(st):
            if key and seen.setdefault(uid, key) != key:
                drop.add(key)
    return {k: v for k, v in cache.items() if k not in drop}


CHANGE = {"'": (1, 0), '*': (12, 0), '+': (4, 4)}   # a hash's last character: the lines its version added, removed


def kept_fixture(versions=(['h0', 'h1', 'h2', 'h3'], ["h0'", 'h1', 'h2', 'h3'], ["h0'", "h1'", 'h2', 'h3'],
                           ["h0'", "h1'", 'h2*', 'h3'])):
    """One blob of 4 functions (40 lines), a step per version (its functions' hashes; CHANGE says what changed), and
    a cache labelling the first version. By default one line changed, then another, then 12: the first two versions
    keep the label of step 0, the third (13 of 40 lines since) does not."""
    def fn(q, h, lines=10):
        added, removed = CHANGE.get(h[-1], (0, 0))
        return {'qual': q, 'name': q, 'file': 'a.py', 'start': 1, 'end': lines, 'lines': lines, 'hash': h, 'data': [],
                'snippet': [], 'calls': [], 'refs': [],
                'delta': {'added': added, 'removed': removed, 'state': 'changed' if added or removed else 'same'}}
    def step(i, hs):
        fs = [fn(f'a.f{k}', h) for k, h in enumerate(hs)]
        return {'idx': i, 'ts': '', 'tool': 'Edit', 'file': 'a.py', 'said': '', 'turn': 0, 'added': 0, 'removed': 0,
                'blobs': [{'id': 'a:A', 'name': 'A', 'emoji': '', 'kind': 'route', 'module': 'a', 'area': 'a',
                           'size': 40, 'complexity': 1, 'errors': 0, 'data': [],
                           'delta': {'added': 0, 'removed': 0, 'moved': 0, 'state': 'changed', 'removed_funcs': []},
                           'funcs': fs}],
                'edges': [], 'areas': [{'id': 'a', 'caption': 'A', 'faces': []}],
                'stats': {'calls': 0, 'unresolved': 0}}
    steps = [step(i, hs) for i, hs in enumerate(versions)]
    tl = {'root': '/nonexistent', 'schema': 5, 'steps': timeline.compact(steps)}
    key0 = ','.join(sorted(versions[0]))
    return tl, {key0: {'emoji': '🔑', 'title': 'Key keeper', 'summary': 'keeps keys', 'risk': 'low'}}


def test_labeller_keeps_labels_like_label_py(tmp_path):
    tl, cache = kept_fixture()
    head, lines = raw_timeline(tl)
    c = tmp_path / 'c.json'
    c.write_text(json.dumps(cache), encoding='utf-8')
    want, report = label_py(tmp_path, head, lines, str(c))
    assert '2 kept labels' in report
    lab = liveserve.Labeller(head['root'], liveserve.load_cache(str(c)), 0.2, head['schema'])
    got = [json.loads(dumps(lab.push(json.loads(l)))) for l in lines]
    assert same(got, want)
    shown = [b.get('label', {}).get('title') for s in timeline.expand({'schema': 5, 'steps': got}) for b in s['blobs']]
    assert shown == ['Key keeper', 'Key keeper', 'Key keeper', None]


def test_tail_labelled_on_a_fork_leaves_the_committed_labels_alone(tmp_path):
    # the tail's kept labels and Keeper state stay on the fork: the steps committed after it are labelled as if the
    # tail had never been
    tl, cache = kept_fixture()
    head, lines = raw_timeline(tl)
    c = tmp_path / 'c.json'
    c.write_text(json.dumps(cache), encoding='utf-8')
    want, _ = label_py(tmp_path, head, lines, str(c))
    lab = liveserve.Labeller(head['root'], liveserve.load_cache(str(c)), 0.2, head['schema'])
    got = [json.loads(dumps(lab.push(json.loads(lines[0]))))]
    fork = lab.fork()
    assert same([json.loads(dumps(fork.push(json.loads(l)))) for l in lines[1:]], want[1:])
    got += [json.loads(dumps(lab.push(json.loads(l)))) for l in lines[1:]]
    assert same(got, want)
    # a tail step that leaves the blob as it was, then another step 1 commits: 8 of 40 lines changed right after
    # step 0 (adjacent: counted from the delta, 20%: not kept), whatever the tail did
    v0 = ['h0', 'h1', 'h2', 'h3']
    _, tail = raw_timeline(kept_fixture([v0, v0])[0])
    tl, cache = kept_fixture([v0, ['h0+', 'h1', 'h2', 'h3']])
    _, lines = raw_timeline(tl)
    want, report = label_py(tmp_path, head, lines, str(c))
    assert ' 0 kept labels' in report
    lab = liveserve.Labeller(head['root'], liveserve.load_cache(str(c)), 0.2, head['schema'])
    got = [json.loads(dumps(lab.push(json.loads(lines[0]))))]
    lab.fork().push(json.loads(tail[1]))
    got.append(json.loads(dumps(lab.push(json.loads(lines[1])))))
    assert same(got, want) and 'label' not in timeline.expand({'schema': 5, 'steps': got})[1]['blobs'][0]


SOURCES = [(FLASK, 'labels.json')] + ([(AIG, 'labels.aigource.json')] if os.path.exists(AIG) else [])


@pytest.mark.parametrize('src,labels', SOURCES, ids=[os.path.basename(s) for s, _ in SOURCES])
def test_served_timeline_is_label_py_on_the_export(src, labels, tmp_path, served):
    # the full cache and one with only each unit's first version (kept labels: aigource keeps 73)
    head, lines = raw_timeline(src)
    with open(os.path.join(REPO, 'demo', labels), encoding='utf-8') as fh:
        full = json.load(fh)
    rnd = random.Random(7)      # (on aigource this half has a merged entry labelled on 323 steps only by the layer)
    half = {k: v for k, v in full.items() if rnd.random() < 0.5}
    for name, cache in (('full', full), ('first', first_versions_only(lines, full)), ('half', half)):
        c = tmp_path / f'{name}.json'
        c.write_text(json.dumps(cache), encoding='utf-8')
        want, report = label_py(tmp_path, head, lines, str(c))
        store = str(tmp_path / f'store-{name}')
        w = SimWriter(store, head)
        k = len(lines) if name == 'half' else len(lines) * 2 // 3
        w.commit(lines[:k])
        w.set_tail(lines[k:])
        s = served(store, str(c))
        tl = s.timeline()
        assert tl['live']['steps'] == k and tl['live']['provisional'] == len(lines) - k
        assert same_view(tl, want), f'{name}: the page would not show what label.py writes'
        assert {x: tl[x] for x in ('root', 'schema')} == head
        if name != 'half':
            assert same(tl['steps'], want), f'{name}: served steps differ from label.py on the export'
        elif src == AIG:          # a merged entry back on an earlier version gets a label kept later: the layer's
            assert tl['live']['labels'] and not same(tl['steps'], want)
        if src == AIG and name == 'first':
            assert ' 0 kept labels' not in report


# -- the store followed ------------------------------------------------------------------------------------------------
def test_sse_resumes_from_last_event_id(flask, cache, tmp_path, served):
    head, lines = flask
    w = SimWriter(str(tmp_path / 's'), head)
    w.commit(lines[:5])
    s = served(w.store, cache)
    tl = s.timeline()
    epoch = tl['live']['epoch']
    assert tl['live']['steps'] == 5
    ev = Events(s.port, epoch, 5)
    assert ev.next()[0] == 'tail'            # the tail as it stands (none) comes first
    w.commit(lines[5:8])
    name, eid, d = ev.next()
    assert (name, eid, d['from'], len(d['steps'])) == ('steps', 8, 5, 3)
    got = tl['steps'] + d['steps']
    ev.close()
    w.commit(lines[8:12])
    s.wait(lambda lv: len(lv.lines) == 12)
    ev = Events(s.port, epoch, 5, last=eid)  # the header wins over ?from=
    name, eid, d = ev.next()
    assert (name, eid, d['from'], len(d['steps'])) == ('steps', 12, 8, 4)
    got += d['steps']
    ev.close()
    want, _ = label_py(tmp_path, head, lines[:12], cache)
    assert same(got, want)


def test_tail_changes_and_locks_in(flask, cache, tmp_path, served):
    head, lines = flask
    w = SimWriter(str(tmp_path / 's'), head)
    w.commit(lines[:6])
    w.set_tail(lines[6:9])
    s = served(w.store, cache)
    want, _ = label_py(tmp_path, head, lines, cache)
    tl = s.timeline()
    assert same(tl['steps'], want[:9]) and tl['live']['steps'] == 6
    ev = Events(s.port, tl['live']['epoch'], 6)
    name, _, d = ev.next()
    assert name == 'tail' and same(d['tail'], want[6:9]) and d['base'] == 6
    w.set_tail(lines[6:10])                  # the tail grows: replaced whole
    name, _, d = ev.next()
    assert name == 'tail' and same(d['tail'], want[6:10]) and d['gen'] == 2
    w.commit(lines[6:8])                     # two of it commit; provisional.json not rewritten yet
    name, eid, d = ev.next()
    assert name == 'steps' and eid == 8 and same(d['steps'], want[6:8]) and same(d['tail'], want[8:10])
    w.set_tail(lines[8:9])                   # the writer's tail after them
    name, _, d = ev.next()
    assert name == 'tail' and same(d['tail'], want[8:9]) and d['base'] == 8
    redo = dumps(dict(json.loads(lines[8]), said='redone'))
    w.set_tail([redo])                       # the tail redone (a late record): replaced
    name, _, d = ev.next()
    assert name == 'tail' and len(d['tail']) == 1 and d['tail'][0]['said'] == 'redone' and d['gen'] == 4
    w.set_tail([lines[10]])                  # steps that do not continue the committed ones: no tail, no harm
    name, _, d = ev.next()
    assert name == 'tail' and d['tail'] == []
    w.commit(lines[8:9])
    name, eid, d = ev.next()
    assert name == 'steps' and eid == 9 and same(d['steps'], want[8:9])
    ev.close()


def test_rebuild_with_other_steps_is_announced(flask, cache, tmp_path, served):
    head, lines = flask
    w = SimWriter(str(tmp_path / 's'), head)
    w.commit(lines[:10])
    s = served(w.store, cache)
    tl = s.timeline()
    epoch = tl['live']['epoch']
    ev = Events(s.port, epoch, 10)
    assert ev.next()[0] == 'tail'
    other = raw_timeline({'schema': 5, 'root': head['root'], 'steps': json.loads(f'[{",".join(lines)}]')[:4]})[1]
    other = other[:3] + [dumps(dict(json.loads(other[3]), said='a different step 4'))]
    w.rebuild(other, tail=[lines[4]])
    name, _, d = ev.next()
    assert name == 'rebuild' and d['epoch'] == epoch + 1
    tl = s.timeline()
    assert tl['live']['epoch'] == epoch + 1 and tl['live']['steps'] == 4 and tl['live']['provisional'] == 1
    assert tl['steps'][3]['said'] == 'a different step 4'
    ev.close()
    ev = Events(s.port, epoch, 10)           # a page still on the old epoch is told at once
    assert ev.next()[0] == 'rebuild'
    ev.close()


def test_steps_rewritten_longer_without_a_word_are_a_rebuild(flask, cache, tmp_path, served):
    # steps.ndjson replaced by other, longer content, status.json and provisional.json untouched (a writer that died
    # between the two): only the check of the last line read can tell
    head, lines = flask
    w = SimWriter(str(tmp_path / 's'), head)
    w.commit(lines[:5])
    s = served(w.store, cache)
    epoch = s.timeline()['live']['epoch']
    other = [dumps(dict(json.loads(l), said=f'other {i}')) for i, l in enumerate(lines[:12])]
    with open(w.path('steps.ndjson'), 'w', encoding='utf-8') as fh:
        fh.write(''.join(l + '\n' for l in other))
    s.wait(lambda lv: lv.epoch == epoch + 1 and len(lv.lines) == 12)
    assert [st['said'] for st in s.timeline()['steps']] == [f'other {i}' for i in range(12)]


def test_rebuild_to_the_same_steps_and_cut_back_are_waited_on(flask, cache, tmp_path, served):
    head, lines = flask
    w = SimWriter(str(tmp_path / 's'), head)
    w.commit(lines[:10])
    w.set_tail(lines[10:12])
    s = served(w.store, cache)
    tl = s.timeline()
    epoch = tl['live']['epoch']
    ev = Events(s.port, epoch, 10)
    assert ev.next()[0] == 'tail'
    # the writer cuts steps.ndjson back to its checkpoint (7 steps) and commits the same steps again
    with open(w.path('steps.ndjson'), 'w', encoding='utf-8') as fh:
        fh.write(''.join(l + '\n' for l in lines[:7]))
    time.sleep(0.3)
    assert s.live.epoch == epoch and len(s.live.lines) == 10 and len(s.live.tail) == 2
    w.n = 7
    w.commit(lines[7:12])
    name, eid, d = ev.next()
    assert (name, eid, d['from']) == ('steps', 12, 10) and len(d['steps']) == 2 and d['tail'] == []
    # a cold rebuild that gives the same steps: gen starts over, nothing to reload
    w.rebuild(lines[:12], tail=lines[12:13])
    name, _, d = ev.next()
    assert name == 'tail' and d['epoch'] == epoch and len(d['tail']) == 1 and d['gen'] == 1
    ev.close()


def test_half_written_line_waits(flask, cache, tmp_path, served):
    head, lines = flask
    w = SimWriter(str(tmp_path / 's'), head)
    w.commit(lines[:3])
    s = served(w.store, cache)
    tl = s.timeline()
    ev = Events(s.port, tl['live']['epoch'], 3)
    assert ev.next()[0] == 'tail'
    w.half(lines[3])
    time.sleep(0.3)
    assert len(s.live.lines) == 3 and s.timeline()['live']['steps'] == 3
    w.finish_half(lines[3])
    name, eid, d = ev.next()
    assert (name, eid, len(d['steps'])) == ('steps', 4, 1)
    want, _ = label_py(tmp_path, head, lines[:4], cache)
    assert same(tl['steps'] + d['steps'], want)
    ev.close()


def layered(steps, overlay, full=True):
    """The served steps as the page shows them with the labels layer, one at a time (a generator of JSON texts):
    expanded, each blob and `merged` entry whose key id (`lk` / `mk`, carried on from step to step) the overlay names
    gets its label (None: none), and the areas of a step where one changed captioned again (areas.caption_table, as
    label.py does). Not `full`: only what labels touch (each blob's id, label and merged labels, the areas)."""
    lk, mk = {}, {}
    exp = liveserve.Expander(5)
    for s in steps:
        lk.update(s.get('lk', {}))
        mk.update(s.get('mk', {}))
        st = exp.push(s)
        st = json.loads(dumps(st)) if full else dict(st, blobs=[dict(b, merged=[dict(m) for m in b.get('merged', ())])
                                                                for b in st['blobs']])
        hit = False
        for b in st['blobs']:
            for x, k in [(b, lk.get(b['id']))] + [(m, mk.get(m['id'])) for m in b.get('merged', ())]:
                if k in overlay:
                    hit = True
                    if overlay[k] is None:
                        x.pop('label', None)
                    else:
                        x['label'] = overlay[k]
        if hit and 'areas' in st:
            st['areas'] = areas.caption_table(st['blobs'], [a['id'] for a in st['areas']])
        yield dumps(st) if full else digest(st)


def digest(st):
    return dumps([[(b['id'], b.get('label'), [m.get('label') for m in b.get('merged', ())]) for b in st['blobs']],
                  st.get('areas')])


def same_view(tl, want, overlay=None):
    """The served timeline (with its labels layer) shows what label.py writes, step by step: whole steps, or on a
    long timeline (where the steps but for labels are the same lines) what labels touch."""
    full = len(want) < 200
    got = layered(tl['steps'], tl['live']['labels'] if overlay is None else overlay, full)
    exp = liveserve.Expander(5)
    for st in want:
        st = exp.push(st)
        if next(got, None) != (dumps(st) if full else digest(st)):
            return False
    return next(got, None) is None


def test_new_label_cache_is_a_labels_event_not_a_rebuild(flask, cache, tmp_path, served):
    head, lines = flask
    w = SimWriter(str(tmp_path / 's'), head)
    w.commit(lines)
    s = served(w.store, cache)
    tl = s.timeline()
    epoch = tl['live']['epoch']
    ev = Events(s.port, epoch, len(lines))
    assert ev.next()[0] == 'tail'
    with open(cache, encoding='utf-8') as fh:
        c = json.load(fh)
    key = next(iter(c))
    c[key] = dict(c[key], title='Renamed part')
    with open(cache, 'w', encoding='utf-8') as fh:
        json.dump(c, fh)
    name, _, d = ev.next()
    assert name == 'labels' and d['epoch'] == epoch and d['labels'] == {liveserve.kid(key): c[key]}
    want, _ = label_py(tmp_path, head, lines, cache)
    assert same_view(tl, want, d['labels'])                 # the page's steps + the event == label.py
    tl2 = s.timeline()
    assert tl2['live']['epoch'] == epoch and same_view(tl2, want)   # a page loading now gets the layer with them
    ev.close()


def test_a_label_learnt_relabels_the_tail_too(flask, cache, tmp_path, served):
    head, lines = flask
    w = SimWriter(str(tmp_path / 's'), head)
    w.commit(lines[:12])
    w.set_tail(lines[12:])
    s = served(w.store, cache)
    tl = s.timeline()
    ev = Events(s.port, tl['live']['epoch'], 12)
    assert ev.next()[0] == 'tail'
    with open(cache, encoding='utf-8') as fh:
        c = json.load(fh)
    shown = {k for st in tl['steps'][12:] for k in (st.get('lk') or {}).values()}
    key = next(k for k in sorted(c) if liveserve.kid(k) in shown)
    c[key] = dict(c[key], title='Renamed tail part')
    with open(cache, 'w', encoding='utf-8') as fh:
        json.dump(c, fh)
    name, _, d = ev.next()
    assert name == 'tail'
    want, _ = label_py(tmp_path, head, lines, cache)
    assert same(d['tail'], want[12:]) and 'Renamed tail part' in json.dumps(d['tail'], ensure_ascii=False)
    ev.close()


def test_empty_store_and_no_writer(cache, tmp_path, served):
    store = str(tmp_path / 'none')
    os.makedirs(store)
    s = served(store, cache)
    tl = s.timeline()
    assert tl['steps'] == [] and tl['live']['steps'] == 0
    r, body = s.get('/live/status')
    st = json.loads(body)
    assert st['ready'] and st['writer_running'] is False and st['steps'] == 0


def test_root_redirects_to_live_page_and_static_files_are_served(cache, tmp_path, served):
    store = str(tmp_path / 'none')
    os.makedirs(store)
    s = served(store, cache)
    r, _ = s.get('/')
    assert r.status == 302 and r.getheader('Location') == '/index.html?live=1'
    r, _ = s.get('/index.html?step=3')
    assert r.status == 302 and r.getheader('Location') == '/index.html?step=3&live=1'
    for path in ('/index.html?live=1', '/index.html?live=0', '/index.html?src=timeline.json', '/timeline.json'):
        r, body = s.get(path)
        assert r.status == 200 and body
    assert s.get('/live/focus')[0].status == 404


def test_run_and_stop_by_pid(flask, cache, tmp_path):
    head, lines = flask
    w = SimWriter(str(tmp_path / 's'), head)
    w.commit(lines[:2])
    import socket
    with socket.socket() as so:
        so.bind(('127.0.0.1', 0))
        port = so.getsockname()[1]
    p = subprocess.Popen([sys.executable, os.path.join(REPO, 'liveserve.py'), 'run', 'x', '--store', w.store,
                          '--port', str(port), '--labels', cache], stderr=subprocess.PIPE, text=True)
    try:
        pidfile = os.path.join(w.store, 'serve.pid')
        for _ in range(200):
            if os.path.exists(pidfile):
                break
            time.sleep(0.05)
        assert int(open(pidfile).read().split()[0]) == p.pid
        again = subprocess.run([sys.executable, os.path.join(REPO, 'liveserve.py'), 'run', 'x', '--store', w.store,
                                '--port', str(port)], capture_output=True, text=True, timeout=30)
        assert again.returncode == 1 and 'already running' in again.stderr
        c = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
        for _ in range(100):
            try:
                c.request('GET', '/live/status')
                break
            except ConnectionRefusedError:
                time.sleep(0.05)
        assert json.loads(c.getresponse().read())['pid'] == p.pid
        c.close()
        stop = subprocess.run([sys.executable, os.path.join(REPO, 'liveserve.py'), 'stop', 'x', '--store', w.store],
                              capture_output=True, text=True, timeout=30)
        assert stop.returncode == 0
        assert p.wait(10) == 0
        assert not os.path.exists(pidfile)
    finally:
        if p.poll() is None:
            p.send_signal(signal.SIGTERM)
            p.wait(10)


# -- served steps never contradict the store ---------------------------------------------------------------------------
def store_now(store):
    """(committed lines, (base, provisional lines)) as the store holds them."""
    with open(os.path.join(store, 'steps.ndjson'), 'rb') as fh:
        committed, _ = liveserve._complete_lines(fh.read())
    p = liveserve._read_json(os.path.join(store, 'provisional.json')) or {}
    return committed, (p.get('base', 0), [dumps(x) for x in p.get('steps', ())])


def contradicted(store, served):
    """The first served committed step the store holds neither as committed nor in its tail, else None."""
    committed, (base, prov) = store_now(store)
    for i, line in enumerate(served):
        held = committed[i] if i < len(committed) else prov[i - base] if base <= i < base + len(prov) else None
        if line != held:
            return i
    return None


def test_rebuild_shorter_than_served_with_another_tail(flask, cache, tmp_path, served):
    # 12 committed steps served; the rebuild commits 0-9 the same and puts another step 10 in provisional.json:
    # served steps 10 and 11 are no longer the store's, now, not when the writer gets past step 12
    head, lines = flask
    w = SimWriter(str(tmp_path / 's'), head)
    w.commit(lines[:12])
    w.set_tail(lines[12:14])
    s = served(w.store, cache)
    epoch = s.timeline()['live']['epoch']
    ev = Events(s.port, epoch, 12)
    assert ev.next()[0] == 'tail'
    alt = dumps(dict(json.loads(lines[10]), said='rebuilt step 10'))
    w.rebuild(lines[:10], tail=[alt] + lines[11:14])
    name, _, d = ev.next()
    assert name == 'rebuild' and d['epoch'] == epoch + 1 and d['steps'] == 10
    tl = s.timeline()
    assert tl['live']['steps'] == 10 and tl['live']['provisional'] == 4 and tl['steps'][10]['said'] == 'rebuilt step 10'
    assert contradicted(w.store, s.live.raw) is None
    ev.close()


def test_rebuild_shorter_that_still_holds_the_served_steps_in_its_tail_is_waited_on(flask, cache, tmp_path, served):
    head, lines = flask
    w = SimWriter(str(tmp_path / 's'), head)
    w.commit(lines[:12])
    s = served(w.store, cache)
    epoch = s.timeline()['live']['epoch']
    w.rebuild(lines[:10], tail=lines[10:13])             # the same steps, two of them back in the tail
    s.settle(w.store)
    assert s.live.epoch == epoch and len(s.live.lines) == 12 and len(s.live.tail) == 1
    w.commit(lines[10:13])
    w.set_tail([])
    s.wait(lambda lv: len(lv.lines) == 13)
    assert s.live.epoch == epoch and contradicted(w.store, s.live.raw) is None


def test_served_steps_never_contradict_the_store(flask, cache, tmp_path, served):
    # a writer doing everything it may, in random order; after each change the server has read, every committed
    # step it serves is the store's, committed or in the tail
    head, lines = flask
    rnd = random.Random(7)
    w = SimWriter(str(tmp_path / 's'), head)
    truth = list(lines)
    w.commit(truth[:3])
    w.set_tail(truth[3:5])
    s = served(w.store, cache)
    kinds = []
    for step in range(60):
        n = w.n
        kind = rnd.choice(['commit', 'commit', 'tail', 'rebuild', 'cut'])
        if kind == 'commit' and n < len(truth):
            k = rnd.randint(1, 3)
            w.commit(truth[n:n + k])
            w.set_tail(truth[n + k:n + k + rnd.randint(0, 3)])
        elif kind == 'tail' and n < len(truth):
            t = truth[n:n + rnd.randint(1, 4)]
            if rnd.random() < 0.5:                       # a guess that will not come true
                t[0] = dumps(dict(json.loads(t[0]), said=f'guess {step}'))
            w.set_tail(t)
        elif kind == 'cut' and n > 2:
            c = rnd.randint(1, n - 1)                    # cut back to a checkpoint, the same steps again
            with open(w.path('steps.ndjson'), 'w', encoding='utf-8') as fh:
                fh.write(''.join(l + '\n' for l in truth[:c]))
            w.n = c
            w.commit(truth[c:n])
        else:
            m = rnd.randint(1, len(truth) - 1)           # a rebuild; from step j on the steps are other ones
            j = rnd.randint(m - 1, len(truth) - 1)
            truth = truth[:j] + [dumps(dict(json.loads(l), said=f'rebuild {step} step {i}'))
                                 for i, l in enumerate(truth[j:], j)]
            w.rebuild(truth[:m], tail=truth[m:m + rnd.randint(0, 4)])
        kinds.append(kind)
        s.settle(w.store)
        bad = contradicted(w.store, s.live.raw)
        assert bad is None, f'after {kinds}: served step {bad} is not the store\'s'
    assert set(kinds) == {'commit', 'tail', 'rebuild', 'cut'}


def same_size_rebuild(w, lines, n):
    """The first n lines again, step 3's time another of the same length: the file's size and its last line stay
    where they were, only status.json / provisional.json can tell."""
    st = json.loads(lines[3])
    ts = st['ts'][:-2] + ('1' if st['ts'][-2] != '1' else '2') + 'Z'
    other = lines[:3] + [dumps(dict(st, ts=ts))] + lines[4:n]
    assert other[3] != lines[3] and len(other[3]) == len(lines[3])
    with open(w.path('steps.ndjson'), 'w', encoding='utf-8') as fh:
        fh.write(''.join(l + '\n' for l in other))
    return other


def test_rebuild_told_only_by_status_rebuilds(flask, cache, tmp_path, served):
    head, lines = flask
    w = SimWriter(str(tmp_path / 's'), head)
    w.commit(lines[:8])
    w.set_tail([])                           # gen 1: a rebuild's gen 1 does not go back
    s = served(w.store, cache)
    epoch = s.timeline()['live']['epoch']
    other = same_size_rebuild(w, lines, 8)
    w.rebuilds += 1
    w.status()
    s.wait(lambda lv: lv.epoch == epoch + 1)
    assert s.live.raw == other


def test_rebuild_told_only_by_provisional_gen_going_back(flask, cache, tmp_path, served):
    head, lines = flask
    w = SimWriter(str(tmp_path / 's'), head)
    w.commit(lines[:8])
    for _ in range(3):
        w.set_tail([])                       # gen 3
    s = served(w.store, cache)
    epoch = s.timeline()['live']['epoch']
    other = same_size_rebuild(w, lines, 8)
    w.gen = 0                                # gen back to 1, status.json's rebuilds as they were
    w.set_tail([])
    s.wait(lambda lv: lv.epoch == epoch + 1)
    assert s.live.raw == other


def test_status_alone_saying_fewer_committed_steps_is_a_rebuild(flask, cache, tmp_path, served):
    # the file cut back to 10 steps, provisional.json still the old one (base 12), status.json says 10 committed:
    # the served steps 10 and 11 are in neither: a rebuild, not a wait
    head, lines = flask
    w = SimWriter(str(tmp_path / 's'), head)
    w.commit(lines[:12])
    w.set_tail([])
    s = served(w.store, cache)
    epoch = s.timeline()['live']['epoch']
    with open(w.path('steps.ndjson'), 'w', encoding='utf-8') as fh:
        fh.write(''.join(l + '\n' for l in lines[:10]))
    w.n = 10
    w.status()
    s.wait(lambda lv: lv.epoch == epoch + 1)
    assert len(s.live.lines) == 10 and contradicted(w.store, s.live.raw) is None


def test_lines_that_come_back_other_after_a_cut_back_are_a_rebuild(flask, cache, tmp_path, served):
    head, lines = flask
    w = SimWriter(str(tmp_path / 's'), head)
    w.commit(lines[:10])
    w.set_tail([])
    s = served(w.store, cache)
    epoch = s.timeline()['live']['epoch']
    with open(w.path('steps.ndjson'), 'w', encoding='utf-8') as fh:
        fh.write(''.join(l + '\n' for l in lines[:7]))
    s.settle(w.store)
    assert s.live.epoch == epoch and len(s.live.lines) == 10       # waited on: status.json still says 10
    with open(w.path('steps.ndjson'), 'a', encoding='utf-8') as fh:
        fh.write(''.join(dumps(dict(json.loads(l), said='other')) + '\n' for l in lines[7:9]))
    s.wait(lambda lv: lv.epoch == epoch + 1)
    assert [st['said'] for st in s.timeline()['steps'][7:9]] == ['other', 'other']


def test_client_ahead_of_the_server_is_told_to_reload(flask, cache, tmp_path, served):
    head, lines = flask
    w = SimWriter(str(tmp_path / 's'), head)
    w.commit(lines[:5])
    s = served(w.store, cache)
    ev = Events(s.port, s.timeline()['live']['epoch'], 0, last=9)
    name, _, d = ev.next()
    assert name == 'rebuild' and d['steps'] == 5
    ev.close()


def test_bad_from_is_refused(flask, cache, tmp_path, served):
    head, lines = flask
    w = SimWriter(str(tmp_path / 's'), head)
    w.commit(lines[:5])
    s = served(w.store, cache)
    epoch = s.timeline()['live']['epoch']
    for q in ('from=-3', 'from=x', 'epoch=y'):
        c = http.client.HTTPConnection('127.0.0.1', s.port, timeout=10)
        c.request('GET', f'/live/events?epoch={epoch}&{q}')
        assert c.getresponse().status == 400, q     # (headers only: a stream would never end)
        c.close()


def test_bound_to_loopback_and_other_hosts_refused(cache, tmp_path, served):
    store = str(tmp_path / 'none')
    os.makedirs(store)
    s = served(store, cache)
    assert s.srv.server_address[0] == '127.0.0.1'
    for host in ('127.0.0.1', f'127.0.0.1:{s.port}', f'localhost:{s.port}', 'LOCALHOST', f'[::1]:{s.port}', '[::1]'):
        assert s.get('/live/status', {'Host': host})[0].status == 200, host
    for host in ('evil.example', f'evil.example:{s.port}', '127.0.0.1:1', f'127.0.0.1.evil.example:{s.port}',
                 'localhost.evil.com', f'localhost.evil.com:{s.port}', f'evil.com:{s.port}.localhost',
                 'evil.localhost', f'evil.localhost:{s.port}', f'evil.127.0.0.1:{s.port}'):
        for path in ('/live/status', '/live/timeline.json', '/index.html?live=1', '/'):
            assert s.get(path, {'Host': host})[0].status == 403, (host, path)
    for host, want in (('evil.example', 403), ('localhost.evil.com', 403), (f'localhost:{s.port}', 200)):
        c = http.client.HTTPConnection('127.0.0.1', s.port, timeout=10)
        c.request('HEAD', '/index.html?live=1', headers={'Host': host})
        assert c.getresponse().status == want, ('HEAD', host)
        c.close()


def test_a_client_that_never_reads_frees_its_thread(flask, cache, tmp_path, served):
    head, lines = flask
    w = SimWriter(str(tmp_path / 's'), head)
    w.commit(lines[:1] + [dumps(dict(json.loads(lines[0]), full=True, said=f'{i} ' + 'x' * 60_000))
                          for i in range(200)])          # 12 MB to send: more than any socket buffer holds
    s = served(w.store, cache, timeout=1)
    epoch = s.timeline()['live']['epoch']
    handlers = lambda: {t for t in threading.enumerate() if 'process_request' in t.name}
    before = handlers()                                  # (other tests' handlers may still be ending: not counted)
    c = socket.socket()
    c.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    c.connect(('127.0.0.1', s.port))
    c.sendall(f'GET /live/events?epoch={epoch}&from=0 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n'.encode())
    try:
        s.wait(lambda lv: handlers() - before, 5)
        mine = next(iter(handlers() - before))
        mine.join(20)                                    # the write timed out: the thread is gone
        assert not mine.is_alive()
    finally:
        c.close()


# -- labels, more --------------------------------------------------------------------------------------------------------
def test_a_tail_guessing_a_kept_label_does_not_leak_into_the_commits(tmp_path):
    # the tail kept the label of step 0 for a small edit; the commits went another way (a big edit, then that small
    # one): the committed labels are label.py's
    v0, small, big = ['h0', 'h1', 'h2', 'h3'], ["h0'", 'h1', 'h2', 'h3'], ['h0*', 'h1*', 'h2', 'h3']
    tl, cache = kept_fixture([v0, big, small])
    head, lines = raw_timeline(tl)
    _, guess = raw_timeline(kept_fixture([v0, small])[0])
    c = tmp_path / 'c.json'
    c.write_text(json.dumps(cache), encoding='utf-8')
    want, _ = label_py(tmp_path, head, lines, str(c))
    lab = liveserve.Labeller(head['root'], liveserve.load_cache(str(c)), 0.2, head['schema'])
    got = [json.loads(dumps(lab.push(json.loads(lines[0]))))]
    tail = json.loads(dumps(lab.fork().push(json.loads(guess[1]))))
    got += [json.loads(dumps(lab.push(json.loads(l)))) for l in lines[1:]]
    assert same(got, want)
    title = lambda st: [b.get('label', {}).get('title') for b in st['blobs']]
    assert title(tail) == ['Key keeper'] and [title(x) for x in timeline.expand({'schema': 5, 'steps': got})] == [
        ['Key keeper'], [None], [None]]


def test_a_merged_label_known_from_a_step_on_is_served_from_that_step(tmp_path):
    # blob Y holds a folded-in entry M and does not change; at step 1 another blob Z keeps its label for a version
    # whose functions are M's: from step 1 on M shows it, so step 1 sends Y again (the labels are put on copies:
    # written on the blobs the steps share, step 0's copy would change with it and Y would not be sent)
    def fn(q, h, added=0):
        return {'qual': q, 'name': q, 'file': 'a.py', 'start': 1, 'end': 10, 'lines': 10, 'hash': h, 'data': [],
                'snippet': [], 'calls': [], 'refs': [],
                'delta': {'added': added, 'removed': 0, 'state': 'changed' if added else 'same'}}
    def blob(bid, funcs, **kw):
        return {'id': bid, 'name': bid, 'emoji': '', 'kind': 'route', 'module': 'a', 'area': 'a', 'size': 10 * len(funcs),
                'complexity': 1, 'errors': 0, 'data': [],
                'delta': {'added': 0, 'removed': 0, 'moved': 0, 'state': 'same', 'removed_funcs': []}, 'funcs': funcs, **kw}
    y = blob('a:Y', [fn('a.y0', 'y0')] + [fn(f'a.m{k}', h) for k, h in enumerate(['kx', 'k1', 'k2', 'k3'])],
             merged=[{'id': 'a:M', 'name': 'M', 'emoji': '', 'kind': 'route', 'lines': 40,
                      'funcs': [f'a.m{k}' for k in range(4)]}])
    z = [blob('a:Z', [fn(f'a.z{k}', h, added) for k, (h, added) in enumerate(hs)]) for hs in
         ([('k0', 0), ('k1', 0), ('k2', 0), ('k3', 0)], [('kx', 1), ('k1', 0), ('k2', 0), ('k3', 0)])]
    steps = [{'idx': i, 'ts': '', 'tool': 'Edit', 'file': 'a.py', 'said': '', 'turn': 0, 'added': 0, 'removed': 0,
              'blobs': [y, z[i]], 'edges': [], 'areas': [{'id': 'a', 'caption': 'A', 'faces': []}],
              'stats': {'calls': 0, 'unresolved': 0}} for i in range(2)]
    head, lines = raw_timeline({'root': '/nonexistent', 'schema': 5, 'steps': timeline.compact(steps)})
    lab = liveserve.Labeller(head['root'], {'k0,k1,k2,k3': {'emoji': '⏱', 'title': 'Clock keeper', 'summary': 's',
                                                            'risk': 'low'}}, 0.2, head['schema'])
    got = [json.loads(dumps(lab.push(json.loads(l)))) for l in lines]
    shown = [[(b['id'], b.get('label', {}).get('title'), [m.get('label', {}).get('title') for m in b.get('merged', ())])
              for b in st['blobs']] for st in timeline.expand({'schema': 5, 'steps': got})]
    assert shown == [[('a:Y', None, [None]), ('a:Z', 'Clock keeper', [])],
                     [('a:Y', None, ['Clock keeper']), ('a:Z', 'Clock keeper', [])]]


def test_rejected_titles_in_the_cache_are_dropped_as_label_py_drops_them(flask, tmp_path, served):
    head, lines = flask
    with open(os.path.join(REPO, 'demo', 'labels.json'), encoding='utf-8') as fh:
        cache = json.load(fh)
    bad = sorted(cache)[:5]
    for k in bad:
        cache[k] = dict(cache[k], title='View a single')
    c = tmp_path / 'c.json'
    c.write_text(json.dumps(cache), encoding='utf-8')
    assert not set(bad) & set(liveserve.load_cache(str(c)))
    want, report = label_py(tmp_path, head, lines, str(c))
    w = SimWriter(str(tmp_path / 's'), head)
    w.commit(lines)
    s = served(w.store, str(c))
    tl = s.timeline()
    assert same(tl['steps'], want) and 'View a single' not in json.dumps(tl)


def test_file_heads_are_read_again_when_the_file_changes(tmp_path):
    (tmp_path / 'page.html').write_text('<h1>one</h1>\n', encoding='utf-8')
    lab = liveserve.Labeller(str(tmp_path), {})
    b = {'id': 'file:page.html', 'kind': 'file', 'path': 'page.html'}
    assert lab.head(b) == ['<h1>one</h1>']
    (tmp_path / 'page.html').write_text('<h1>two!</h1>\n', encoding='utf-8')
    st = os.stat(tmp_path / 'page.html')
    os.utime(tmp_path / 'page.html', ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    assert lab.head(b) == ['<h1>two!</h1>']


def test_session_list_counts_tail_steps(flask, cache, tmp_path, served):
    # two sessions (transcripts in a directory), the second one only in the tail: the head lists both, with the
    # steps each has, tail included, as live.export_text() would
    head, lines = flask
    proj = tmp_path / 'proj'
    proj.mkdir()
    for uuid, ts, said in (('aaaaaaaa-0001', '2026-01-01T00:00:00.000Z', 'first'), ('bbbbbbbb-0002', '2026-01-02T00:00:00.000Z', 'second')):
        (proj / f'{uuid}.jsonl').write_text(json.dumps({'type': 'user', 'timestamp': ts, 'message': {'content': said}}) + '\n')
    tagged = [dumps(dict(json.loads(l), session='aaaaaaaa' if i < 10 else 'bbbbbbbb')) for i, l in enumerate(lines)]
    w = SimWriter(str(tmp_path / 's'), head, inputs=[str(proj)])
    w.commit(tagged[:10])
    w.set_tail(tagged[10:13])
    s = served(w.store, cache)
    tl = s.timeline()
    assert tl['session'] == 'aaaaaaaa,bbbbbbbb'
    assert [(x['id'], x['steps'], x['prompt']) for x in tl['sessions']] == [('aaaaaaaa', 10, 'first'), ('bbbbbbbb', 3, 'second')]


# -- the expander ------------------------------------------------------------------------------------------------------
def test_expander_is_timeline_expand():
    import test_func_patch as tf
    cases = [{'schema': 4, 'steps': timeline.compact(tf.fixture(), 4)}, {'schema': 5, 'steps': timeline.compact(tf.fixture5())},
             {'schema': 3, 'steps': timeline.compact(tf.fixture(), 3)}]
    with open(FLASK, encoding='utf-8') as fh:
        cases.append(json.load(fh))
    for tl in cases:
        e = liveserve.Expander(tl['schema'])
        want = timeline.expand(tl)
        got = [e.push(s) for s in tl['steps']]
        assert [dumps(s) for s in got] == [dumps(s) for s in want]
        f, k = liveserve.Expander(tl['schema']), len(tl['steps']) // 2   # a fork goes on as the original would
        for s in tl['steps'][:k]:
            f.push(s)
        g = f.fork()
        assert [dumps(g.push(s)) for s in tl['steps'][k:]] == [dumps(s) for s in want[k:]]
        assert [dumps(f.push(s)) for s in tl['steps'][k:]] == [dumps(s) for s in want[k:]]


# -- the labels layer -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize('snap', [1, 3, 32])
def test_recaches_in_any_order_plan_as_a_fresh_labeller(flask, tmp_path, monkeypatch, snap):
    # caches changing under the labeller (answers, a key going, a title renamed), at any point: its plan, `applied`
    # and the layer it served are a fresh labeller's with the last cache, and label.py's
    monkeypatch.setattr(liveserve, 'SNAP', snap)
    head, lines = flask
    with open(os.path.join(REPO, 'demo', 'labels.json'), encoding='utf-8') as fh:
        full = json.load(fh)
    rnd = random.Random(snap)
    for trial in range(4):
        keys = sorted(full)
        caches = [{k: full[k] for k in keys if rnd.random() < p} for p in (0.2, 0.5, 0.8, 0.6)]
        caches[-1][keys[0]] = dict(full[keys[0]], title='Renamed later')
        lab = liveserve.Labeller(head['root'], caches[0], 0.2, head['schema'])
        cuts = sorted(rnd.sample(range(1, len(lines)), 3))
        out, ov, at = [], {}, 0
        for c, cache in zip(cuts + [len(lines)], caches[1:] + [caches[-1]]):
            out += [json.loads(dumps(lab.push(json.loads(l), 0.0))) for l in lines[at:c]]
            ov.update(lab.take_overlay())
            ov.update(lab.recache(cache, lines[:c]))
            at = c
        fresh = liveserve.Labeller(head['root'], caches[-1], 0.2, head['schema'])
        for l in lines:
            fresh.push(json.loads(l), 0.0)
        assert lab.work == fresh.work and lab.applied == fresh.applied
        at = [sn[0] for sn in lab.snaps]
        assert at == sorted(set(at))          # (one snapshot per position: the replayed ones replace the old)
        c = tmp_path / f'c{trial}.json'
        c.write_text(json.dumps(caches[-1]), encoding='utf-8')
        want, _ = label_py(tmp_path, head, lines, str(c))
        assert same_view({'steps': out, 'live': {'labels': ov}}, want)


def test_recaches_keep_labels_as_a_fresh_labeller(tmp_path, monkeypatch):
    # one blob, versions that keep labels and versions that do not; the caches label some of them: after any order of
    # changes the kept labels (applied) are a fresh labeller's, and the page's view label.py's
    monkeypatch.setattr(liveserve, 'SNAP', 2)
    vs = [['h0', 'h1', 'h2', 'h3'], ["h0'", 'h1', 'h2', 'h3'], ["h0'", "h1'", 'h2', 'h3'], ["h0'", "h1'", 'h2*', 'h3'],
          ["h0'", "h1'", "h2*", "h3'"], ['h0*', "h1'", 'h2*', "h3'"], ['h0*', "h1+", 'h2*', "h3'"]]
    tl, _ = kept_fixture(vs)
    head, lines = raw_timeline(tl)
    keys = [','.join(sorted(v)) for v in vs]
    lab_of = lambda k: {'emoji': '🔑', 'title': f'Keeper {keys.index(k)}', 'summary': 's', 'risk': 'low'}
    rnd, kept = random.Random(2), 0
    for trial in range(12):
        caches = [{k: lab_of(k) for k in keys if rnd.random() < p} for p in (0.3, 0.5, 0.7)]
        lab = liveserve.Labeller(head['root'], caches[0], 0.2, head['schema'])
        out, ov, at = [], {}, 0
        for c, cache in zip(sorted(rnd.sample(range(1, len(lines)), 2)) + [len(lines)], caches[1:] + [caches[-1]]):
            out += [json.loads(dumps(lab.push(json.loads(l), 0.0))) for l in lines[at:c]]
            ov.update(lab.take_overlay())
            ov.update(lab.recache(cache, lines[:c]))
            at = c
        fresh = liveserve.Labeller(head['root'], caches[-1], 0.2, head['schema'])
        for l in lines:
            fresh.push(json.loads(l), 0.0)
        assert lab.work == fresh.work and lab.applied == fresh.applied
        at = [sn[0] for sn in lab.snaps]
        assert at == sorted(set(at))          # (one snapshot per position: the replayed ones replace the old)
        c = tmp_path / f'k{trial}.json'
        c.write_text(json.dumps(caches[-1]), encoding='utf-8')
        want, _ = label_py(tmp_path, head, lines, str(c))
        assert same_view({'steps': out, 'live': {'labels': ov}}, want)
        kept += any('reused_from' in v for v in fresh.applied.values())
    assert kept >= 3


def test_a_tail_written_while_the_steps_are_read_goes_out_with_them(flask, cache, tmp_path):
    # the writer commits steps, then rewrites provisional.json while the server is reading them: the same poll serves
    # the steps with that tail (one event, no step shown first with no tail and then again with it)
    head, lines = flask
    w = SimWriter(str(tmp_path / 's'), head)
    w.commit(lines[:5])
    live = liveserve.Live(w.store, cache)
    live.poll()
    w.commit(lines[5:8])
    read = live._read_steps

    def read_then_tail(check):
        got = read(check)
        w.set_tail(lines[8:10])
        return got
    live._read_steps = read_then_tail
    v = live.tail_v
    assert live.poll()
    assert len(live.lines) == 8 and len(live.tail) == 2 and live.tail_v == v + 1
    want, _ = label_py(tmp_path, head, lines[:10], cache)
    assert same([json.loads(x) for x in live.lines + live.tail], want)
