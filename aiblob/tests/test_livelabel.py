"""The labelling worker (livelabel.py) with liveserve.Live, a fake clock, a fake llama-server (fakellama.py) and a fake
nvidia-smi (fakesmi.py): history is never asked, a restart asks nothing new, the 20% rule keeps labels, a burst is one
call, down -> up drains, failures back off, a thermal pause does not hold the steps back, the focused step goes first, a
late label covers the version after it, cache writes merge, and once settled the page shows label.py's labels. Then the
llama-server child: it dies with its parent, starts only when needed and memory allows, sleeps and stops when idle, and
a server the user runs is used, never managed."""
import json, os, random, re, signal, subprocess, sys, threading, time

import pytest

import label, livelabel, liveserve, timeline
from fakellama import FakeLlama
from livesim import SimWriter, dumps, raw_timeline
from test_liveserve import Events, kept_fixture, label_py, same_view

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FLASK = os.path.join(REPO, 'web', 'timeline.json')
FAKE = os.path.join(HERE, 'fakellama.py')
NOCHILD = [sys.executable, '-c', 'import sys; sys.exit(3)']      # a child that would fail at once (never expected)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Smi:
    """fakesmi.py on a state file: set(temp=, free=), calls()."""

    def __init__(self, tmp_path, temp=60, free=5000):
        self.path = str(tmp_path / 'smi.json')
        self.cmd = [sys.executable, os.path.join(HERE, 'fakesmi.py'), self.path]
        self.state = {}
        self.set(temp=temp, free=free)

    def set(self, **kw):
        self.state.update(kw)
        with open(self.path, 'w') as fh:
            json.dump(self.state, fh)

    def calls(self):
        try:
            with open(self.path + '.calls') as fh:
                return len(fh.read().split())
        except FileNotFoundError:
            return 0


class Rig:
    """A store (SimWriter), a Live with its Worker, driven by hand on a fake clock: poll() reads the store, run()
    ticks the worker (the clock jumps to each wake time) until nothing is left to ask or `limit` s went by."""

    def __init__(self, tmp_path, head, cache=None, fake=None, smi=None, cmd=NOCHILD, store='s', clock=None, **kw):
        self.tmp, self.clock = tmp_path, clock or Clock()
        self.cache = str(tmp_path / 'labels.json')
        if cache is not None:
            with open(self.cache, 'w', encoding='utf-8') as fh:
                json.dump(cache, fh)
        self.smi = smi or Smi(tmp_path)
        self.store = str(tmp_path / store)
        self.writer = SimWriter(self.store, head) if not os.path.exists(self.store) else None
        self.log = str(tmp_path / 'run.jsonl')
        self.worker = liveserve.worker(self.cache, self.log, llm_url=fake.url if fake else 'http://127.0.0.1:1',
                                       llm_cmd=cmd, smi=self.smi.cmd, clock=self.clock, **kw)
        self.live = liveserve.Live(self.store, self.cache, 0.2, self.worker, self.clock)
        self.wakes = []

    def poll(self):
        self.live.poll()
        self.live.poll()        # (a cache written by the last ask is read now)

    def run(self, limit=600.0, until_idle=True):
        end = self.clock.t + limit
        while self.clock.t <= end:
            self.poll()
            nxt = self.worker.tick(self.clock())
            self.poll()
            self.wakes.append((self.clock.t, nxt, self.worker.state))
            if until_idle and self.worker.state == 'idle' and not self.live.lab.wants():
                return
            if nxt > self.clock.t:
                self.clock.t = min(nxt, end + 1)

    def advance(self, dt):
        self.clock.t += dt

    def records(self, event=None):
        return [r for r in label.read_log(self.log) if event is None or r.get('event') == event]

    def view(self):
        return json.loads(self.live.timeline_text())


@pytest.fixture
def fake():
    f = FakeLlama()
    yield f
    f.down()


@pytest.fixture(scope='module')
def flask():
    return raw_timeline(FLASK)


def iso(t):
    return liveserve.iso_time(t)


def stamp(lines, k, base=None):
    """lines[k:] as edits made now, after labelling begins (ts from 10 min ahead on, 1 s apart); lines[:k] as they
    are (history: the Flask session's own times, or none)."""
    base = time.time() + 600 if base is None else base
    return lines[:k] + [dumps(dict(json.loads(l), ts=iso(base + j))) for j, l in enumerate(lines[k:])]


def unit_keys(root, lines):
    """Per step position, the keys of its units (label.units on the expanded steps, file heads read from root as
    liveserve reads them)."""
    lab = liveserve.Labeller(root, {}, 0.2)
    out = []
    for l in lines:
        st = lab._expand(lab.exp, json.loads(l))
        out.append({k for _, k, _, _ in label.units(st) if k})
    return out


# -- what is asked -------------------------------------------------------------------------------------------------------
def test_history_is_never_asked_and_a_restart_asks_nothing_new(flask, tmp_path, fake):
    head, lines = flask
    lines = stamp(lines, 10)
    r = Rig(tmp_path, head, {}, fake)
    r.writer.commit(lines[:10])
    r.poll()
    bound = json.load(open(os.path.join(r.store, 'labelling.json')))
    assert bound['steps'] == 10 and liveserve.ts_seconds(bound['since']) > liveserve.ts_seconds(json.loads(lines[9])['ts'])
    assert r.live.lab.wants() == {} and r.worker.wants == {}
    r.advance(3600)
    r.run()
    assert fake.state.requests == [] and json.load(open(r.cache)) == {}      # history: nothing, not even /health

    r.writer.commit(lines[10:13])
    r.poll()
    wants = dict(r.live.lab.wants())
    assert wants
    r.run()
    keys = unit_keys(head['root'], lines[:13])
    new = set().union(*keys[10:])
    cache = json.load(open(r.cache))
    assert set(cache) == set(wants) and set(cache) <= new
    assert len(fake.completions()) == len(wants)
    assert not set(cache) & (set().union(*keys[:10]) - new)                   # no key only history shows

    # stopped; the writer commits two more steps; a new server on the same store asks those, nothing else
    r.writer.commit(lines[13:15])
    r2 = Rig(tmp_path, head, None, fake, store='s', clock=Clock(50.0))
    r2.poll()
    assert json.load(open(os.path.join(r.store, 'labelling.json'))) == bound
    w2 = r2.live.lab.wants()
    assert w2 and not set(w2) & set(cache)
    r2.run()
    assert len(fake.completions()) == len(wants) + len(w2)
    r3 = Rig(tmp_path, head, None, fake, store='s', clock=Clock(9.0))         # and a restart after that: nothing
    r3.poll()
    r3.advance(3600)
    r3.run()
    assert r3.live.lab.wants() == {} and len(fake.completions()) == len(wants) + len(w2)


def test_twenty_percent_rule_keeps_labels(tmp_path, fake):
    # step 0 (history) labelled; then 1 line changed, 1 more (2 of 40), then 12 more (14 of 40 since the label): only
    # that last version is asked, the two before keep the label of step 0
    tl, cache = kept_fixture()
    head, lines = raw_timeline(tl)
    lines = stamp(lines, 1)
    r = Rig(tmp_path, head, cache, fake)
    r.writer.commit(lines[:1])
    r.poll()
    r.writer.commit(lines[1:3])
    r.poll()
    assert r.live.lab.wants() == {}
    r.advance(100)
    r.run()
    assert fake.completions() == []
    r.writer.commit(lines[3:])
    r.poll()
    assert list(r.live.lab.wants()) == [','.join(sorted(["h0'", "h1'", 'h2*', 'h3']))]
    r.run()
    assert len(fake.completions()) == 1
    v = r.view()
    titles = [[b.get('label', {}).get('title') for b in s['blobs']] for s in timeline.expand(
        {'schema': 5, 'steps': [{k: x for k, x in s.items() if k not in ('lk', 'mk')} for s in v['steps']]})]
    want, _ = label_py(tmp_path, head, lines, r.cache)
    assert same_view(v, want)
    assert [t[0] for t in titles[:3]] == ['Key keeper'] * 3


def versions(n, start=0):
    """n versions of the kept_fixture blob, each changing two of its four functions by 12 lines (60%: always asked)."""
    return [[f'h0{k}*', f'h1{k}*', 'h2', 'h3'] for k in range(start, start + n)]


def test_a_small_change_to_an_unlabelled_history_blob_asks_the_new_version(tmp_path, fake):
    # history never labelled: version 1 changes 1 line of 40 (it would keep the label of step 0, if there were one).
    # Its own version is asked, not step 0's
    tl, _ = kept_fixture()
    head, lines = raw_timeline(tl)
    lines = stamp(lines, 1)
    r = Rig(tmp_path, head, {}, fake)
    r.writer.commit(lines[:1])
    r.poll()
    r.writer.commit(lines[1:2])
    r.poll()
    r.run()
    k0, k1 = ','.join(sorted(['h0', 'h1', 'h2', 'h3'])), ','.join(sorted(["h0'", 'h1', 'h2', 'h3']))
    got = json.load(open(r.cache))
    assert k0 not in got and set(got) == {k1} and len(fake.completions()) == 1


def test_a_burst_of_five_versions_is_one_call(tmp_path, fake):
    tl, cache = kept_fixture([['h0', 'h1', 'h2', 'h3']] + versions(5))
    head, lines = raw_timeline(tl)
    lines = stamp(lines, 1)
    r = Rig(tmp_path, head, cache, fake)
    r.writer.commit(lines[:1])
    r.poll()
    for k in range(1, 6):                       # one version every 3 s: never quiet for 20 s until the last
        r.writer.commit(lines[k:k + 1])
        r.poll()
        r.worker.tick(r.clock())
        r.advance(3)
    assert fake.completions() == []
    r.run()
    assert len(fake.completions()) == 1
    newest = ','.join(sorted(versions(5)[-1]))
    assert set(json.load(open(r.cache))) == set(cache) | {newest}
    asked = r.records('ask')
    assert len(asked) == 1 and asked[0]['step'] == 5


def test_the_cap_does_not_wait_for_quiet(tmp_path, fake):
    # a blob edited every 10 s is asked 120 s after it was first wanted
    tl, cache = kept_fixture([['h0', 'h1', 'h2', 'h3']] + versions(16))
    head, lines = raw_timeline(tl)
    lines = stamp(lines, 1)
    r = Rig(tmp_path, head, cache, fake)
    r.writer.commit(lines[:1])
    r.poll()
    t0 = r.clock.t
    for k in range(1, 17):
        r.writer.commit(lines[k:k + 1])
        r.poll()
        r.worker.tick(r.clock())
        if fake.completions():
            break
        r.advance(10)
    assert len(fake.completions()) == 1 and r.clock.t - t0 == 120


def test_the_end_of_a_turn_does_not_wait_for_quiet(tmp_path, fake):
    # a version of turn 0, then a step of turn 1 that leaves the blob alone: its want is due at once
    v0, v1 = ['h0', 'h1', 'h2', 'h3'], versions(1)[0]
    tl, cache = kept_fixture([v0, v1, v1])
    head, lines = raw_timeline(tl)
    lines = stamp(lines, 1)
    lines[2] = dumps(dict(json.loads(lines[2]), turn=1))
    r = Rig(tmp_path, head, cache, fake)
    r.writer.commit(lines[:1])
    r.poll()
    r.writer.commit(lines[1:2])
    r.poll()
    r.advance(5)
    r.worker.tick(r.clock())
    assert fake.completions() == []
    r.writer.commit(lines[2:3])
    r.poll()
    r.worker.tick(r.clock())
    assert len(fake.completions()) == 1


def test_down_then_up_drains_the_queue(flask, tmp_path, fake):
    head, lines = flask
    lines = stamp(lines, 10)
    smi = Smi(tmp_path, free=100)               # no room for a child either
    fake.down()
    r = Rig(tmp_path, head, {}, fake, smi=smi)
    r.writer.commit(lines[:10])
    r.poll()
    r.writer.commit(lines[10:14])
    r.poll()
    wants = r.live.lab.wants()
    assert len(wants) > 1
    r.advance(21)
    probes = []
    for _ in range(5):
        nxt = r.worker.tick(r.clock())
        assert r.worker.state == 'down'
        probes.append(nxt - r.clock.t)
        r.clock.t = nxt
    assert probes == [15, 30, 60, 60, 60]
    r.poll()
    assert r.live.ask['state'] == 'down' and r.records('child-start') == []
    fake.up()
    r.run()
    assert len(fake.completions()) == len(wants) and set(json.load(open(r.cache))) == set(wants)
    assert [x['event'] for x in r.records() if x['event'] in ('down', 'up')] == ['down', 'up']


def test_failures_are_tried_three_times_then_backed_off(flask, tmp_path, fake):
    head, lines = flask
    lines = stamp(lines, 10)
    r = Rig(tmp_path, head, {}, fake)
    r.writer.commit(lines[:10])
    r.poll()
    r.writer.commit(lines[10:11])
    r.poll()
    assert len(r.live.lab.wants()) == 1
    fake.state.fail = 4
    r.advance(21)
    t0, at, ask = r.clock.t, [], r.worker._ask
    r.worker._ask = lambda *a: (at.append(r.clock.t - t0), ask(*a))
    r.run()
    assert [x['status'] for x in fake.completions(500) + fake.completions()] == [500] * 4 + [200]
    assert [a['status'] for a in r.records('ask')] == ['failed'] * 4 + ['ok']
    assert at == [0, 2, 6, 66, 68]          # 3 tries (2 s, 4 s apart), 60 s of backoff, then again
def test_a_server_still_loading_is_waited_on(flask, tmp_path, fake):
    head, lines = flask
    lines = stamp(lines, 10)
    fake.state.loading = True
    r = Rig(tmp_path, head, {}, fake, smi=Smi(tmp_path, free=100))
    r.writer.commit(lines[:10])
    r.poll()
    r.writer.commit(lines[10:12])
    r.poll()
    r.advance(21)
    nxt = r.worker.tick(r.clock())
    assert r.worker.state == 'down' and nxt - r.clock.t == 15 and fake.completions() == []
    fake.state.loading = False
    r.run()
    assert fake.completions() and r.records('child-start') == []


def test_a_server_lost_in_the_middle_is_probed_then_drained(flask, tmp_path, fake):
    head, lines = flask
    lines = stamp(lines, 10)
    r = Rig(tmp_path, head, {}, fake, smi=Smi(tmp_path, free=100))
    r.writer.commit(lines[:10])
    r.poll()
    r.writer.commit(lines[10:14])
    r.poll()
    n = len(r.live.lab.wants())
    r.advance(21)
    r.worker.tick(r.clock())
    r.poll()
    assert len(fake.completions()) == 1
    fake.down()                                 # gone: the next ask fails as refused
    nxt = r.worker.tick(r.clock())
    assert r.worker.state != 'idle' and r.records('down')
    nxt = r.worker.tick(r.clock())
    assert r.worker.state == 'down' and nxt - r.clock.t == 15
    fake.up()
    r.run()
    assert len(fake.completions()) == n and r.live.lab.wants() == {}


def test_a_503_on_an_ask_is_a_server_waking_probed_later(flask, tmp_path, fake):
    # /health says ok but the completion answers 503 (loading, waking): no retry 2 s later, a probe 15 s later
    head, lines = flask
    lines = stamp(lines, 10)
    r = Rig(tmp_path, head, {}, fake)
    r.writer.commit(lines[:10])
    r.poll()
    r.writer.commit(lines[10:11])
    r.poll()
    fake.state.fail, fake.state.fail_code = 1, 503
    r.advance(21)
    r.worker.tick(r.clock())
    nxt = r.worker.tick(r.clock())
    assert r.worker.state == 'down' and nxt - r.clock.t == 15 and fake.completions(503)
    r.run()
    assert len(fake.completions()) == 1 and [x['event'] for x in r.records() if x['event'] in ('down', 'up')] == ['down', 'up']


def test_an_answered_key_is_not_asked_again_before_the_cache_is_read(flask, tmp_path, fake):
    head, lines = flask
    lines = stamp(lines, 10)
    r = Rig(tmp_path, head, {}, fake)
    r.writer.commit(lines[:10])
    r.poll()
    r.writer.commit(lines[10:12])
    r.poll()
    r.advance(21)
    for _ in range(6):                          # no poll in between: the wants still name what was answered (the
        r.worker.tick(r.clock())                # quiet time after an answer gone by too)
        r.advance(150)
    names = [c['blob'] for c in fake.completions()]
    assert len(names) == len(set(names)) == len(r.live.lab.wants())


def test_a_label_written_by_another_restarts_the_cap(tmp_path, fake):
    # wanted from t0 (the GPU too hot to ask); at t0+130 another writer labels that version; at t0+131 the blob
    # changes far again: its new want waits for quiet from then, not for a cap counted from t0
    tl, cache = kept_fixture([['h0', 'h1', 'h2', 'h3']] + versions(2))
    head, lines = raw_timeline(tl)
    lines = stamp(lines, 1)
    smi = Smi(tmp_path, temp=90)
    r = Rig(tmp_path, head, cache, fake, smi=smi)
    r.writer.commit(lines[:1])
    r.poll()
    r.writer.commit(lines[1:2])
    r.poll()
    r.advance(130)
    r.worker.tick(r.clock())
    assert r.worker.state == 'paused'
    label.save_cache(r.cache, {','.join(sorted(versions(2)[0])): {'emoji': 'x', 'title': 'By hand', 'summary': '',
                                                                  'risk': 'low'}})
    r.advance(1)
    r.writer.commit(lines[2:3])                 # (read in the same poll as that label)
    r.poll()
    smi.set(temp=60)
    r.advance(6)
    r.worker.tick(r.clock())
    assert fake.completions() == []
    r.advance(15)
    r.worker.tick(r.clock())
    assert len(fake.completions()) == 1


def test_hot_gpu_pauses_until_below_cool(flask, tmp_path, fake):
    head, lines = flask
    lines = stamp(lines, 10)
    smi = Smi(tmp_path, temp=86)
    r = Rig(tmp_path, head, {}, fake, smi=smi)
    r.writer.commit(lines[:10])
    r.poll()
    r.writer.commit(lines[10:12])
    r.poll()
    r.advance(21)
    for temp, state in ((86, 'paused'), (82, 'paused'), (84, 'paused'), (79, 'idle')):
        smi.set(temp=temp)
        r.advance(5)                            # (a reading is kept 5 s)
        r.worker.tick(r.clock())
        assert (r.worker.state if fake.completions() == [] else 'idle') == state, temp
    assert fake.completions() and [x['event'] for x in r.records() if x['event'] in ('pause', 'resume')] == ['pause', 'resume']


def test_a_store_built_after_labelling_began_asks_only_steps_made_after(flask, tmp_path, fake):
    # labelling starts on an empty store (a writer still in its cold build): the history it writes later is not new
    # (its edits are older than the boundary); edits made after it are
    head, lines = flask
    r = Rig(tmp_path, head, {}, fake)
    r.poll()
    bound = json.load(open(os.path.join(r.store, 'labelling.json')))
    assert bound['steps'] == 0 and abs(liveserve.ts_seconds(bound['since']) - time.time()) < 5
    r.writer.commit(lines[:12])
    r.poll()
    r.advance(100)
    r.run()
    assert r.live.lab.wants() == {} and fake.completions() == []
    later = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(time.time() + 60)) + '.000Z'
    r.writer.commit([dumps(dict(json.loads(l), ts=later)) for l in lines[12:14]])
    r.poll()
    assert r.live.lab.wants()
    r.run()
    assert fake.completions()


def test_pending_names_the_versions_that_will_show_the_label(tmp_path, fake):
    v0, v1 = ['h0', 'h1', 'h2', 'h3'], ['h0a*', 'h1a*', 'h2', 'h3']
    tl, cache = kept_fixture([v0, v1, ["h0a*", "h1a*'", 'h2', 'h3']])
    head, lines = raw_timeline(tl)
    lines = stamp(lines, 1)
    r = Rig(tmp_path, head, cache, fake)
    r.writer.commit(lines[:1])
    r.poll()
    r.writer.commit(lines[1:])
    r.poll()
    r.worker.tick(r.clock())
    r.poll()
    k1, k2 = (','.join(sorted(v)) for v in (v1, ["h0a*", "h1a*'", 'h2', 'h3']))
    assert r.live.ask['state'] == 'waiting' and r.live.ask['pending'] == sorted({liveserve.kid(k1), liveserve.kid(k2)})
    r.run()
    assert r.live.ask['pending'] == [] and len(fake.completions()) == 1
    assert set(json.load(open(r.cache))) == set(cache) | {k1}           # the version asked; the next one keeps it
    want, _ = label_py(tmp_path, head, lines, r.cache)
    assert same_view(r.view(), want)


def test_gpu_temperature_is_read_at_most_every_5_s(tmp_path):
    smi, clock = Smi(tmp_path, temp=70), Clock()
    g = livelabel.Gpu(smi.cmd, clock)
    assert g.read() == (70, 5000)
    smi.set(temp=90)
    clock.t += 4.9
    assert g.read() == (70, 5000) and smi.calls() == 1
    clock.t += 0.1
    assert g.read() == (90, 5000) and smi.calls() == 2


def test_hot_gpu_pauses_asks_not_steps(flask, tmp_path, fake):
    # the worker and the server on threads (the real clock), GPU at 90 °C: steps reach the page at once, nothing is
    # asked, the chip says paused; below 80 °C the asks go and their labels come as a labels event
    head, lines = flask
    lines = stamp(lines, 10)
    smi = Smi(tmp_path, temp=90)
    w = SimWriter(str(tmp_path / 's'), head)
    w.commit(lines[:10])
    cache = str(tmp_path / 'labels.json')
    with open(cache, 'w') as fh:
        json.dump({}, fh)
    wk = liveserve.worker(cache, str(tmp_path / 'run.jsonl'), llm_url=fake.url, llm_cmd=NOCHILD, smi=smi.cmd, quiet=0)
    srv, live, stop, poller = liveserve.serve(w.store, cache, port=0, poll=0.03, ask=wk, wait_cap=0.05)
    threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
    try:
        port = srv.server_address[1]
        end = time.time() + 10
        while not live.ready and time.time() < end:
            time.sleep(0.02)
        ev = Events(port, live.epoch, 10)
        seen = []
        while not any(n == 'ask' for n, _, _ in seen):
            seen.append(ev.next())
        t0 = time.time()
        w.commit(lines[10:13])
        while True:
            name, _, d = ev.next()
            if name == 'steps':
                break
        assert time.time() - t0 < 2 and d['from'] == 10 and len(d['steps']) == 3
        while True:
            name, _, d = ev.next()
            if name == 'ask' and d['state'] == 'paused':
                break
        assert d['temp'] == 90 and d['pending'] and fake.completions() == []
        smi.set(temp=70)
        labels = None
        while labels is None:
            name, _, d = ev.next()
            if name == 'labels':
                labels = d['labels']
        assert labels and fake.completions()
        ev.close()
    finally:
        stop.set()
        srv.shutdown()
        srv.server_close()
        live.worker_thread.join(10)


def test_the_focused_step_goes_first_then_the_newest(flask, tmp_path, fake):
    head, lines = flask
    lines = stamp(lines, 10)

    def order(focus, sub):
        f = FakeLlama()
        try:
            r = Rig(tmp_path / sub, head, {}, f)
            r.writer.commit(lines[:10])
            r.poll()
            for k in range(10, 16):             # one step at a time, 1 s apart
                r.writer.commit(lines[k:k + 1])
                r.poll()
                r.advance(1)
            wants = r.live.lab.wants()
            if focus is not None:
                r.worker.set_focus(focus)
            r.advance(30)                       # all due at once (a queue built while the model was away)
            r.run()
            return [a['step'] for a in r.records('ask')], {w.key: (w.vis, w.pos) for w in wants.values()}
        finally:
            f.down()

    for sub in ('a', 'b'):
        os.makedirs(tmp_path / sub)
    steps, wants = order(None, 'a')
    assert len(steps) > 2 and steps == sorted(steps, reverse=True)       # newest first
    vis = sorted({v for v, _ in wants.values()})
    focus = vis[0]
    steps_f, _ = order(focus, 'b')
    first = [p for (v, p) in wants.values() if v <= focus]
    assert set(steps_f[:len(first)]) == set(first) and steps_f != steps


def test_focus_is_posted_by_the_page(flask, tmp_path, fake):
    import http.client
    head, lines = flask
    w = SimWriter(str(tmp_path / 's'), head)
    w.commit(lines[:3])
    cache = str(tmp_path / 'labels.json')
    with open(cache, 'w') as fh:
        json.dump({}, fh)
    wk = liveserve.worker(cache, str(tmp_path / 'run.jsonl'), llm_url=fake.url, llm_cmd=NOCHILD, smi=Smi(tmp_path).cmd)
    srv, live, stop, poller = liveserve.serve(w.store, cache, port=0, poll=0.03, ask=wk, wait_cap=0.05)
    threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
    try:
        for body, code in ((b'{"step": 7}', 204), (b'{"nope": 1}', 400), (b'x' * 2000, 400)):
            c = http.client.HTTPConnection('127.0.0.1', srv.server_address[1], timeout=10)
            c.request('POST', '/live/focus', body=body, headers={'Content-Type': 'application/json'})
            assert c.getresponse().status == code
            c.close()
        assert wk.focus == 7
        c = http.client.HTTPConnection('127.0.0.1', srv.server_address[1], timeout=10)
        c.request('POST', '/live/focus', body=b'{"step": 1}', headers={'Host': 'evil.example'})
        assert c.getresponse().status == 403 and wk.focus == 7
    finally:
        stop.set()
        srv.shutdown()
        srv.server_close()
        live.worker_thread.join(10)


def test_a_late_label_covers_the_version_the_blob_moved_to(tmp_path, fake):
    # version 1 is asked; while the model answers, version 2 (1 line from it) commits: one call, and both show it.
    # A version 3 far from it (12 lines) is asked on its own.
    tl, cache = kept_fixture([['h0', 'h1', 'h2', 'h3'], ['h0a*', 'h1a*', 'h2', 'h3'], ["h0a*", "h1a*'", 'h2', 'h3'],
                              ["h0a*", "h1a*'", 'h2b*', 'h3']])
    head, lines = raw_timeline(tl)
    lines = stamp(lines, 1)
    r = Rig(tmp_path, head, cache, fake)
    r.writer.commit(lines[:1])
    r.poll()
    r.writer.commit(lines[1:2])
    r.poll()
    r.advance(21)
    fake.state.latency = 0.6
    t = threading.Thread(target=r.worker.tick, args=(r.clock(),))
    t.start()
    end = time.time() + 5
    while r.worker.asking is None and time.time() < end:
        time.sleep(0.01)
    assert r.worker.asking is not None
    r.writer.commit(lines[2:3])
    r.live.poll()
    t.join(10)
    fake.state.latency = 0
    r.poll()
    assert r.live.lab.wants() == {} and len(fake.completions()) == 1
    v = r.view()
    want, _ = label_py(tmp_path, head, lines[:3], r.cache)
    assert same_view(v, want)
    shown = [[b.get('label', {}).get('title') for b in st['blobs']] for st in
             (json.loads(x) for x in __import__('test_liveserve').layered(v['steps'], v['live']['labels']))]
    assert shown[2] == shown[1] == ['A feature']
    r.writer.commit(lines[3:])
    r.poll()
    r.run()
    assert len(fake.completions()) == 2
    want, _ = label_py(tmp_path, head, lines, r.cache)
    assert same_view(r.view(), want)


# -- the cache file ------------------------------------------------------------------------------------------------------
WRITER = r'''
import json, sys, time
sys.path.insert(0, sys.argv[1])
import label
for i in range(int(sys.argv[4])):
    label.save_cache(sys.argv[2], {f'{sys.argv[3]}{i}': {'emoji': 'x', 'title': f'Part {i}', 'summary': '', 'risk': 'low'}})
'''


def test_cache_writes_merge_with_a_concurrent_writer(tmp_path):
    path = str(tmp_path / 'labels.json')
    with open(path, 'w') as fh:
        json.dump({'old': {'emoji': 'o', 'title': 'Old part', 'summary': '', 'risk': 'low'}}, fh)
    procs = [subprocess.Popen([sys.executable, '-c', WRITER, REPO, path, p, '150']) for p in ('a', 'b', 'c')]
    reads = 0
    while any(p.poll() is None for p in procs):
        with open(path, encoding='utf-8') as fh:
            json.load(fh)                       # never half a file
        reads += 1
    assert all(p.wait() == 0 for p in procs)
    got = json.load(open(path))
    assert set(got) == {'old'} | {f'{p}{i}' for p in 'abc' for i in range(150)} and reads > 1


def test_another_writer_labelling_history_meanwhile_is_no_rebuild(flask, tmp_path, fake):
    # a manual label.py run writes history's labels while the worker answers the new steps: no reload, and the page
    # shows label.py's labels with the cache both left
    head, lines = flask
    lines = stamp(lines, 10)
    r = Rig(tmp_path, head, {}, fake)
    r.writer.commit(lines[:10])
    r.poll()
    epoch = r.live.epoch
    r.writer.commit(lines[10:14])
    r.poll()
    full = json.load(open(os.path.join(REPO, 'demo', 'labels.json')))
    hist = set().union(*unit_keys(head['root'], lines[:10])) - set(r.live.lab.wants())
    keys = sorted(k for k in hist if k in full)[:6]
    assert keys
    r.worker.tick(r.clock())
    for k in keys:                                  # as label.py writes them
        label.save_cache(r.cache, {k: full[k]})
    r.run()
    got = json.load(open(r.cache))
    assert set(keys) <= set(got) and r.worker.done <= set(got) and fake.completions()
    assert r.live.epoch == epoch
    want, _ = label_py(tmp_path, head, lines[:14], r.cache)
    assert same_view(r.view(), want)


# -- after it settles ----------------------------------------------------------------------------------------------------
def test_settled_labels_are_label_py_with_the_final_cache(flask, tmp_path, fake):
    # history half labelled; new steps asked; the page (the timeline served + its layer) == label.py with the cache
    # the worker left, back-filled merged labels included
    head, lines = flask
    lines = stamp(lines, 8)
    full = json.load(open(os.path.join(REPO, 'demo', 'labels.json')))
    rnd = random.Random(3)
    r = Rig(tmp_path, head, {k: v for k, v in full.items() if rnd.random() < 0.5}, fake)
    r.writer.commit(lines[:8])
    r.writer.set_tail(lines[8:9])
    r.poll()
    r.writer.commit(lines[8:])
    r.writer.set_tail([])
    r.poll()
    r.run()
    assert fake.completions()
    want, _ = label_py(tmp_path, head, lines, r.cache)
    assert same_view(r.view(), want)


# -- the llama-server child ----------------------------------------------------------------------------------------------
def test_llama_cmd_flags():
    argv, cwd = livelabel.llama_cmd('b11960', '/m.gguf', 60)
    s = ' '.join(argv)
    assert argv[0].endswith('ld-linux-x86-64.so.2') and argv[3] == './llama-server' and cwd.endswith('llama-b11960')
    for flag in ('-c 4096', '-np 1', '--cache-ram 0', '--sleep-idle-seconds 60', '--port {port}', '--host 127.0.0.1'):
        assert flag in s
    assert '--fit' not in s and livelabel.llama_cmd('', '/m.gguf')[0][0] == 'llama-server'


def child_cmd(log, *extra):
    return [sys.executable, FAKE, '--port', '{port}', '--log', str(log)] + list(extra)


def pid_alive(pid):
    try:
        with open(f'/proc/{pid}/stat') as fh:
            return fh.read().rsplit(')', 1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        return False


def fake_events(log, event=None):
    return [r for r in label.read_log(str(log)) if event is None or r['event'] == event]


def wait_for(pred, timeout=20.0, what='condition'):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError(f'timed out waiting for {what}')


def server_proc(tmp_path, store, cache, *args, smi=None):
    """liveserve.py run as its own process (the child's parent). -> (Popen, port)."""
    smi = smi or Smi(tmp_path)
    p = subprocess.Popen([sys.executable, os.path.join(REPO, 'liveserve.py'), 'run', 'x', '--store', store, '--port', '0',
                          '--labels', cache, '--poll', '0.05', '--smi', ' '.join(smi.cmd), '--run-log',
                          str(tmp_path / 'run.jsonl'), *args], stderr=subprocess.PIPE, text=True, cwd=REPO)
    for line in p.stderr:
        m = re.search(r'http://127\.0\.0\.1:(\d+)/', line)
        if m:
            threading.Thread(target=lambda: [None for _ in p.stderr], daemon=True).start()
            return p, int(m.group(1))
    raise AssertionError('liveserve did not start')


def status(port):
    import urllib.request
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/live/status', timeout=10) as r:
        return json.load(r)


def stop_proc(p):
    if p.poll() is None:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(30)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(10)


@pytest.fixture
def store_with_history(flask, tmp_path):
    head, lines = flask
    lines = stamp(lines, 10)
    w = SimWriter(str(tmp_path / 's'), head)
    w.commit(lines[:10])
    cache = str(tmp_path / 'labels.json')
    with open(cache, 'w') as fh:
        json.dump({}, fh)
    return w, lines, cache


def test_child_dies_with_its_parent(tmp_path, store_with_history):
    w, lines, cache = store_with_history
    log = tmp_path / 'llama.jsonl'
    p, port = server_proc(tmp_path, w.store, cache, '--llm-url', 'http://127.0.0.1:1', '--quiet', '0',
                          '--llm-cmd', ' '.join(child_cmd(log)))
    child = None
    try:
        wait_for(lambda: status(port)['ready'], what='ready')
        w.commit(lines[10:12])
        start = wait_for(lambda: fake_events(log, 'start'), what='child start')[0]
        child = start['pid']
        assert start['ppid'] == p.pid and start['pgid'] == child           # its parent's child, its own group
        wait_for(lambda: fake_events(log, 'completion'), what='an ask')
        assert status(port)['ask']['child'] == child
        os.kill(p.pid, signal.SIGKILL)
        p.wait(10)
        wait_for(lambda: not pid_alive(child), timeout=10, what='the child to go')
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(10)
        if child and pid_alive(child):
            os.kill(child, signal.SIGKILL)
            raise AssertionError('the child outlived its parent')


def test_no_child_with_nothing_to_ask_or_no_memory(tmp_path, store_with_history):
    w, lines, cache = store_with_history
    log = tmp_path / 'llama.jsonl'
    smi = Smi(tmp_path, free=1000)
    p, port = server_proc(tmp_path, w.store, cache, '--llm-url', 'http://127.0.0.1:1', '--quiet', '0',
                          '--llm-cmd', ' '.join(child_cmd(log)), smi=smi)
    try:
        wait_for(lambda: status(port)['ready'], what='ready')
        time.sleep(1.5)                                     # history only: nothing to ask, no child
        st = status(port)
        assert not os.path.exists(log) and (st['ask'] is None or st['ask']['child'] is None)
        w.commit(lines[10:12])                              # asks, but 1000 MiB of VRAM free: still no child
        st = wait_for(lambda: (lambda s: s if s['ask'] and s['ask']['state'] == 'down' else None)(status(port)))
        assert st['ask']['queued'] > 0 and st['ask']['child'] is None and not os.path.exists(log)
        downs = [r for r in label.read_log(str(tmp_path / 'run.jsonl')) if r['event'] == 'down']
        assert downs and downs[0]['why'] == 'nomem'
        smi.set(free=5000)                                  # room again: the next probe (15 s) starts one
        wait_for(lambda: fake_events(log, 'completion'), timeout=40, what='an ask after the probe')
    finally:
        stop_proc(p)
    starts = fake_events(log, 'start')
    assert len(starts) == 1 and not pid_alive(starts[0]['pid'])            # stopped with the server,
    assert fake_events(log, 'sigterm')                                      # by a SIGTERM (not only PDEATHSIG)


def test_child_sleeps_and_is_stopped_when_idle(tmp_path, store_with_history):
    w, lines, cache = store_with_history
    log = tmp_path / 'llama.jsonl'
    p, port = server_proc(tmp_path, w.store, cache, '--llm-url', 'http://127.0.0.1:1', '--quiet', '0',
                          '--idle-stop', '4', '--llm-cmd', ' '.join(child_cmd(log, '--sleep-idle', '1')))
    try:
        wait_for(lambda: status(port)['ready'], what='ready')
        w.commit(lines[10:11])
        wait_for(lambda: fake_events(log, 'completion'), what='an ask')
        child = fake_events(log, 'start')[0]['pid']
        wait_for(lambda: fake_events(log, 'sleep'), timeout=5, what='llama-server to sleep')
        n = len(fake_events(log, 'completion'))
        w.commit(lines[11:13])                              # a request wakes it
        wait_for(lambda: len(fake_events(log, 'completion')) > n, what='an ask after sleep')
        assert fake_events(log, 'wake') and pid_alive(child)
        wait_for(lambda: not pid_alive(child), timeout=15, what='the idle stop')
        assert fake_events(log, 'sigterm')
        stops = [r for r in label.read_log(str(tmp_path / 'run.jsonl')) if r['event'] == 'child-idle-stop']
        assert stops and stops[0]['pid'] == child
        wait_for(lambda: status(port)['ask']['child'] is None, timeout=5, what='the chip to say so')
    finally:
        stop_proc(p)


def test_a_server_the_user_runs_is_used_not_managed(tmp_path, store_with_history, fake):
    w, lines, cache = store_with_history
    marker = tmp_path / 'started'
    cmd = [sys.executable, '-c', f'open({str(marker)!r}, "w").close()']
    p, port = server_proc(tmp_path, w.store, cache, '--llm-url', fake.url, '--quiet', '0', '--idle-stop', '1',
                          '--llm-cmd', ' '.join(cmd))
    try:
        wait_for(lambda: status(port)['ready'], what='ready')
        w.commit(lines[10:12])
        wait_for(lambda: fake.completions(), what='an ask')
        st = wait_for(lambda: (lambda s: s if s['ask'] and s['ask']['server'] == 'external' else None)(status(port)))
        assert st['ask']['child'] is None
        time.sleep(2)                                       # past the idle stop: nothing to stop
    finally:
        stop_proc(p)
    assert not marker.exists() and fake.srv is not None
    assert [r for r in label.read_log(str(tmp_path / 'run.jsonl')) if r['event'].startswith('child')] == []


# -- the boundary: what counts as new ----------------------------------------------------------------------------------
def asks_on(r, fake):
    r.advance(3600)
    r.run()
    return len(fake.completions())


def test_labelling_begun_in_the_middle_of_a_cold_build_asks_no_history(flask, tmp_path, fake):
    # the writer had written 5 of its history steps (and a tail) when labelling began; the rest of the same
    # history comes after: none of it is asked. Then an edit made now is.
    head, lines = flask
    lines = stamp(lines, 13)
    r = Rig(tmp_path, head, {}, fake)
    r.writer.commit(lines[:5])
    r.writer.set_tail(lines[5:7])
    r.poll()
    r.writer.commit(lines[5:13])
    r.writer.set_tail([])
    r.poll()
    assert r.live.lab.wants() == {} and asks_on(r, fake) == 0
    r.writer.commit(lines[13:])
    r.poll()
    assert r.live.lab.wants() and asks_on(r, fake) > 0


def test_a_store_behind_its_transcripts_asks_nothing_it_catches_up(flask, tmp_path, fake):
    # the store held 4 steps; its writer catches up on 14 more the transcripts already held (older than the time
    # labelling began): none asked, and a rebuild of the same history asks nothing either
    head, lines = flask
    r = Rig(tmp_path, head, {}, fake)
    r.writer.commit(lines[:4])
    r.poll()
    r.writer.commit(lines[4:])
    r.poll()
    assert asks_on(r, fake) == 0
    r.writer.rebuild(lines)
    r.poll()
    assert asks_on(r, fake) == 0


@pytest.mark.parametrize('steps', [0, 6])
def test_a_clock_behind_when_labelling_begins_asks_no_history(flask, tmp_path, fake, monkeypatch, steps):
    # the wall clock says 1970 when labelling begins, on an empty store and on one with steps: `since` is not taken
    # from it alone (the store's files and steps are newer), so the history written then or later is not asked
    head, lines = flask
    r = Rig(tmp_path, head, {}, fake)
    r.writer.commit(lines[:steps])
    real = time.time
    monkeypatch.setattr(liveserve.time, 'time', lambda: 0.0)
    r.poll()
    monkeypatch.setattr(liveserve.time, 'time', real)
    since = liveserve.ts_seconds(json.load(open(os.path.join(r.store, 'labelling.json')))['since'])
    assert since > real() - 60
    r.writer.commit(lines[steps:])
    r.poll()
    assert asks_on(r, fake) == 0


@pytest.mark.parametrize('ahead', [365 * 86400, 600])
def test_a_transcript_touched_ahead_does_not_hold_back_new_steps(flask, tmp_path, fake, ahead):
    # an input transcript whose mtime is a year (or 10 min) ahead of the clock is not taken for when labelling began
    # (logged): steps made just after are asked, history is not
    head, lines = flask
    proj = tmp_path / 'proj'
    proj.mkdir()
    tr = proj / 'aaaaaaaa-0001.jsonl'
    tr.write_text(json.dumps({'type': 'user', 'timestamp': '2026-01-01T00:00:00.000Z', 'message': {'content': 'x'}}) + '\n')
    os.utime(tr, (time.time() + ahead, time.time() + ahead))
    w = SimWriter(str(tmp_path / 's'), head, inputs=[str(proj)])
    w.commit(lines[:10])
    r = Rig(tmp_path, head, {}, fake, store='s')
    r.poll()
    since = liveserve.ts_seconds(json.load(open(os.path.join(r.store, 'labelling.json')))['since'])
    assert abs(since - time.time()) < 30
    assert [x['files'] for x in r.records('mtime-ahead')] == [[str(tr)]]
    w.commit(stamp(lines[10:], 0, time.time() + 5))     # edits made 5 s after labelling began
    r.poll()
    assert {x.pos for x in r.live.lab.wants().values()} and min(x.pos for x in r.live.lab.wants().values()) >= 10
    assert asks_on(r, fake) > 0


def test_an_empty_store_asks_only_what_is_made_after(flask, tmp_path, fake):
    head, lines = flask
    lines = stamp(lines, 16)
    r = Rig(tmp_path, head, {}, fake)
    r.poll()
    r.writer.commit(lines[:16])
    r.poll()
    assert asks_on(r, fake) == 0
    r.writer.commit(lines[16:])
    r.poll()
    assert asks_on(r, fake) > 0


def test_steps_without_a_time_are_new_only_after_a_new_step(flask, tmp_path, fake):
    # no ts: history (and anything before a step with a time) is never asked; after a step made now, a step without
    # a time is new (steps are committed in time order)
    head, lines = flask
    bare = [dumps(dict(json.loads(l), ts='')) for l in lines]
    r = Rig(tmp_path, head, {}, fake)
    r.writer.commit(bare[:10])
    r.poll()
    r.writer.commit(bare[10:13])
    r.poll()
    assert r.live.lab.wants() == {} and asks_on(r, fake) == 0
    r.writer.commit(stamp(lines[13:14], 0) + bare[14:])
    r.poll()
    assert {w.pos for w in r.live.lab.wants().values()} - {13}       # (a step after it, without a time, is new)
    assert asks_on(r, fake) > 0


def test_a_step_made_at_the_boundary_is_new(flask, tmp_path, fake):
    head, lines = flask
    t = time.time() + 300
    w = SimWriter(str(tmp_path / 's'), head)
    with open(os.path.join(w.store, 'labelling.json'), 'w') as fh:
        json.dump({'since': iso(t), 'steps': 0}, fh)
    r = Rig(tmp_path, head, {}, fake, store='s')
    w.commit(lines[:10] + stamp(lines[10:11], 0, t))     # history, then a step made at `since` to the millisecond
    r.poll()
    assert {x.pos for x in r.live.lab.wants().values()} == {10}


def test_replayed_history_is_not_asked_after_a_cache_change(tmp_path, fake):
    # an unlabelled history blob changed a little in a new step: its new version is wanted, never step 0's, also
    # after a cache change replays the plan from step 0
    tl, _ = kept_fixture([['h0', 'h1', 'h2', 'h3'], ["h0'", 'h1', 'h2', 'h3'], ["h0'", "h1'", 'h2', 'h3']])
    head, lines = raw_timeline(tl)
    lines = stamp(lines, 1)
    r = Rig(tmp_path, head, {}, fake)
    r.writer.commit(lines[:1])
    r.poll()
    r.writer.commit(lines[1:])
    r.poll()
    k0 = ','.join(sorted(['h0', 'h1', 'h2', 'h3']))
    label.save_cache(r.cache, {'other,key': {'emoji': 'x', 'title': 'Other part', 'summary': '', 'risk': 'low'}})
    first = r.live.lab.first
    first['other,key'] = 0                  # (as if step 0 showed it: the replay starts at step 0)
    r.poll()
    assert k0 not in r.live.lab.wants() and k0 not in r.live.lab.asked
    r.run()
    assert k0 not in json.load(open(r.cache)) and len(fake.completions()) == 1


# -- coalescing over time ----------------------------------------------------------------------------------------------
def test_edits_during_each_ask_do_not_undo_the_coalescing(tmp_path, fake):
    # a version far (60%) from the last every 10 s, and one more landing while each ask is in flight, for 600 s:
    # the blob is asked about once per cap (120 s), not after every answer
    tl, cache = kept_fixture([['h0', 'h1', 'h2', 'h3']] + versions(140))
    head, lines = raw_timeline(tl)
    lines = stamp(lines, 1)
    r = Rig(tmp_path, head, cache, fake)
    r.writer.commit(lines[:1])
    r.poll()
    k = [1]

    def edit():
        r.writer.commit(lines[k[0]:k[0] + 1])
        k[0] += 1
    ask = r.worker._ask
    r.worker._ask = lambda *a: (edit(), r.live.poll(), ask(*a))[2]
    t0 = r.clock.t
    while r.clock.t - t0 < 600:
        edit()
        r.poll()
        end = r.clock.t + 10
        while True:
            nxt = r.worker.tick(r.clock())
            r.poll()
            if nxt <= r.clock.t:
                continue
            if nxt >= end:
                break
            r.clock.t = nxt
        r.clock.t = end
    assert 4 <= len(fake.completions()) <= 6, len(fake.completions())


def test_a_blob_back_after_a_removal_waits_afresh(tmp_path, fake):
    # wanted (the GPU too hot to ask), then removed, then back 200 s later with a new version: its cap counts from its
    # return, not from the first want
    v0 = ['h0', 'h1', 'h2', 'h3']
    tl, cache = kept_fixture([v0] + versions(3))
    steps = timeline.expand(tl)
    steps[2]['blobs'] = []
    tl = {'root': tl['root'], 'schema': 5, 'steps': timeline.compact(steps)}
    head, lines = raw_timeline(tl)
    lines = stamp(lines, 1)
    smi = Smi(tmp_path, temp=90)
    r = Rig(tmp_path, head, cache, fake, smi=smi)
    r.writer.commit(lines[:1])
    r.poll()
    r.writer.commit(lines[1:2])
    r.poll()
    r.advance(150)
    r.worker.tick(r.clock())
    r.writer.commit(lines[2:3])                 # gone
    r.poll()
    r.advance(50)
    smi.set(temp=60)
    r.writer.commit(lines[3:4])                 # back, another version
    r.poll()
    r.advance(6)
    r.worker.tick(r.clock())
    assert fake.completions() == []             # quiet 20 s from its return, not 120 s from the first want
    r.advance(15)
    r.worker.tick(r.clock())
    assert len(fake.completions()) == 1


def test_the_focused_step_goes_first_when_the_blob_is_shown_from_before(flask, tmp_path, fake):
    # focus on a step after the one a want is first shown at (the blob unchanged since): that want goes first
    head, lines = flask
    lines = stamp(lines, 10)
    r = Rig(tmp_path, head, {}, fake)
    r.writer.commit(lines[:10])
    r.poll()
    for k in range(10, 16):
        r.writer.commit(lines[k:k + 1])
        r.poll()
        r.advance(1)
    wants = list(r.live.lab.wants().values())
    old = min(wants, key=lambda w: w.vis)
    focus = max(w.vis for w in wants) - 1
    assert old.vis < focus and any(w.vis > focus for w in wants)
    r.worker.set_focus(focus)
    r.advance(30)
    r.run()
    first = [a['step'] for a in r.records('ask')][:len([w for w in wants if w.vis <= focus])]
    assert set(first) == {w.pos for w in wants if w.vis <= focus}


# -- the child: idle, crash, sleep, a wrapper ---------------------------------------------------------------------------
def child_rig(tmp_path, head, fake_log, *extra, smi=None, **kw):
    return Rig(tmp_path, head, {}, None, smi=smi, cmd=child_cmd(fake_log, *extra), **kw)


def drive(r, until, limit=30.0):
    """tick() the worker (the clock jumps 1 s per 'starting' tick; the real time it takes a child to answer) until
    `until()` holds."""
    end = time.time() + limit
    while not until():
        assert time.time() < end, f'timed out ({r.worker.state})'
        r.poll()
        nxt = r.worker.tick(r.clock())
        r.poll()
        if r.worker.state == 'starting':
            time.sleep(0.05)
        if nxt > r.clock.t:
            r.clock.t = nxt


def test_idle_stop_counts_from_the_last_ask_even_with_asks_held_back(flask, tmp_path):
    head, lines = flask
    lines = stamp(lines, 10)
    log = tmp_path / 'llama.jsonl'
    smi = Smi(tmp_path)
    r = child_rig(tmp_path, head, log, smi=smi, idle_stop=600)
    try:
        r.writer.commit(lines[:10])
        r.poll()
        r.writer.commit(lines[10:11])
        r.poll()
        r.advance(21)
        drive(r, lambda: len(fake_events(log, 'completion')) == 1)
        child = r.worker.llama.proc.pid
        r.advance(500)
        r.writer.commit(lines[11:12])           # asked 500 s later: the child is used again
        r.poll()
        r.advance(21)
        drive(r, lambda: len(fake_events(log, 'completion')) == 2)
        r.advance(200)                          # 700 s after its start, 200 s after its last ask: kept
        r.worker.tick(r.clock())
        assert pid_alive(child) and r.worker.llama.proc is not None
        smi.set(temp=92)                        # hot: a new want waits, the child is not asked
        r.writer.commit(lines[12:13])
        r.poll()
        r.advance(30)
        r.worker.tick(r.clock())
        assert r.worker.state == 'paused'
        r.advance(400)                          # 630 s since its last ask, asks pending: stopped all the same
        r.worker.tick(r.clock())
        assert r.worker.llama.proc is None and not pid_alive(child)
        assert [x['event'] for x in r.records() if x['event'] == 'child-idle-stop']
    finally:
        r.worker.llama.stop()


def test_a_child_that_died_is_started_again(flask, tmp_path):
    head, lines = flask
    lines = stamp(lines, 10)
    log = tmp_path / 'llama.jsonl'
    r = child_rig(tmp_path, head, log)
    try:
        r.writer.commit(lines[:10])
        r.poll()
        r.writer.commit(lines[10:11])
        r.poll()
        r.advance(21)
        drive(r, lambda: len(fake_events(log, 'completion')) == 1)
        first = r.worker.llama.proc.pid
        os.kill(first, signal.SIGKILL)          # (our own child, by its pid)
        wait_for(lambda: r.worker.llama.proc.poll() is not None, 5, 'the child to die')
        r.writer.commit(lines[11:13])
        r.poll()
        r.advance(21)
        drive(r, lambda: len(fake_events(log, 'completion')) >= 3, limit=60)
        starts = fake_events(log, 'start')
        assert len(starts) == 2 and starts[1]['pid'] != first and r.records('child-exit')
    finally:
        r.worker.llama.stop()


def test_a_sleeping_child_is_not_woken_without_room(flask, tmp_path):
    head, lines = flask
    lines = stamp(lines, 10)
    log = tmp_path / 'llama.jsonl'
    smi = Smi(tmp_path)
    r = child_rig(tmp_path, head, log, '--sleep-idle', '0.5', smi=smi)
    try:
        r.writer.commit(lines[:10])
        r.poll()
        r.writer.commit(lines[10:11])
        r.poll()
        r.advance(21)
        drive(r, lambda: len(fake_events(log, 'completion')) == 1)
        wait_for(lambda: fake_events(log, 'sleep'), 5, 'the child to sleep')
        smi.set(free=1000)                      # its VRAM went to someone else meanwhile
        r.writer.commit(lines[11:12])
        r.poll()
        r.advance(21)
        nxt = r.worker.tick(r.clock())
        assert r.worker.state == 'down' and r.worker.why == 'vram' and nxt - r.clock.t == 15
        assert len(fake_events(log, 'completion')) == 1 and not fake_events(log, 'wake')
        smi.set(free=5000)
        r.clock.t = nxt
        drive(r, lambda: len(fake_events(log, 'completion')) == 2)
        assert fake_events(log, 'wake')
    finally:
        r.worker.llama.stop()


def test_vram_is_read_afresh_before_a_child_starts(tmp_path):
    smi, clock = Smi(tmp_path, free=5000), Clock()
    gpu = livelabel.Gpu(smi.cmd, clock)
    llama = livelabel.Llama('http://127.0.0.1:1', NOCHILD, gpu=gpu, clock=clock)
    assert gpu.read() == (60, 5000)
    smi.set(free=1000)                          # within the 5 s a reading is kept
    assert llama.ensure(clock()) == (None, 'nomem') and llama.proc is None


GRANDCHILD = r'''
import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)       # (deaf to SIGTERM: only a SIGKILL ends it)
open(sys.argv[1], 'w').write(str(os.getpid()))
time.sleep(600)
'''


def test_a_wrapper_command_takes_its_children_with_it(tmp_path):
    # --llm-cmd a shell that forks: the fake server and a process that ignores SIGTERM. Stopping the child stops its
    # whole process group (PR_SET_PDEATHSIG covers only the direct child)
    log, mark = tmp_path / 'llama.jsonl', tmp_path / 'grand.pid'
    script = tmp_path / 'grand.py'
    script.write_text(GRANDCHILD)
    cmd = ['sh', '-c', f'{sys.executable} {FAKE} --port {{port}} --log {log} & {sys.executable} {script} {mark} & wait']
    clock = Clock()
    llama = livelabel.Llama('http://127.0.0.1:1', cmd, gpu=livelabel.Gpu(Smi(tmp_path).cmd, clock), clock=clock)
    server = grand = None
    try:
        assert llama.ensure(clock()) == (None, 'starting')
        wait_for(lambda: mark.exists() and mark.read_text() and fake_events(log, 'start'), 10, 'the wrapper to fork')
        server, grand = fake_events(log, 'start')[0]['pid'], int(mark.read_text())
        assert server != llama.proc.pid and os.getpgid(grand) == llama.proc.pid
        llama.stop()
        wait_for(lambda: not pid_alive(server) and not pid_alive(grand), 5, 'the whole group to go')
    finally:
        llama.stop()
        for pid in (server, grand):             # (left behind only if the test failed: ours, by pid)
            if pid and pid_alive(pid):
                os.kill(pid, signal.SIGKILL)


def test_the_default_runtime_runs_llama_server_in_place():
    # the b11960 runtime starts llama-server through its private glibc loader: the loader runs it in its own process
    # (no fork), so the child PR_SET_PDEATHSIG is set on is llama-server itself
    argv, cwd = livelabel.llama_cmd('b11960')
    if not os.path.exists(argv[0]):
        pytest.skip('the b11960 runtime is not on this machine')
    p = subprocess.Popen([argv[0], '--library-path', argv[2], '/bin/sleep', '2'])
    try:
        time.sleep(0.3)
        with open(f'/proc/{p.pid}/task/{p.pid}/children') as fh:
            assert fh.read().split() == []
        assert open(f'/proc/{p.pid}/comm').read().strip() == 'ld-linux-x86-64'
    finally:
        p.wait(10)


# -- a worker that dies --------------------------------------------------------------------------------------------------
def test_a_dead_worker_is_restarted_then_shown_as_an_error(flask, tmp_path, fake):
    head, lines = flask
    lines = stamp(lines, 10)
    w = SimWriter(str(tmp_path / 's'), head)
    w.commit(lines[:10])
    cache = str(tmp_path / 'labels.json')
    with open(cache, 'w') as fh:
        json.dump({}, fh)
    wk = liveserve.worker(cache, str(tmp_path / 'run.jsonl'), llm_url=fake.url, llm_cmd=NOCHILD, smi=Smi(tmp_path).cmd,
                          quiet=0)
    tick, deaths = wk.tick, [0]

    def dying(now):
        if deaths[0] < 2:
            deaths[0] += 1
            raise SystemExit('killed')          # (not an Exception: the thread ends)
        return tick(now)
    wk.tick = dying
    srv, live, stop, poller = liveserve.serve(w.store, cache, port=0, poll=0.03, ask=wk, wait_cap=0.05)
    try:
        wait_for(lambda: live.ready and live.worker_restarts == 2, 10, 'two restarts')
        w.commit(lines[10:12])
        wait_for(lambda: fake.completions(), 10, 'an ask after the restarts')
        wk.tick = lambda now: (_ for _ in ()).throw(SystemExit('again'))
        w.commit(lines[12:13])
        st = wait_for(lambda: live.ask if live.ask and live.ask['state'] == 'error' else None, 10, 'the error chip')
        assert 'again' in st['error'] and live.worker_restarts == liveserve.Live.WORKER_RESTARTS
        assert [x['event'] for x in label.read_log(str(tmp_path / 'run.jsonl'))].count('worker-died') == 4
    finally:
        stop.set()                              # (never served: no shutdown())
        srv.server_close()
        if live.worker_thread:
            live.worker_thread.join(10)


def test_an_error_in_a_tick_shows_on_the_chip_and_clears(flask, tmp_path, fake):
    head, lines = flask
    lines = stamp(lines, 10)
    r = Rig(tmp_path, head, {}, fake)
    r.writer.commit(lines[:10])
    r.poll()
    r.writer.commit(lines[10:11])
    r.poll()
    r.advance(21)
    r.worker.gpu.read = lambda fresh=False: (_ for _ in ()).throw(RuntimeError('smi broke'))
    stop = threading.Event()
    t = threading.Thread(target=r.worker.run, args=(stop, 0.02), daemon=True)
    t.start()
    try:
        wait_for(lambda: (r.poll(), r.live.ask and r.live.ask['state'] == 'error')[1], 5, 'the error chip')
        assert 'smi broke' in r.live.ask['error']
        del r.worker.gpu.read                   # (nvidia-smi works again)
        wait_for(lambda: (r.poll(), fake.completions())[1], 10, 'an ask')
        wait_for(lambda: (r.poll(), r.live.ask['error'] is None)[1], 5, 'the chip to clear')
    finally:
        stop.set()
        t.join(5)


# -- the cache: a writer that read it earlier --------------------------------------------------------------------------
def test_a_writer_saves_only_what_it_changed(tmp_path):
    path = str(tmp_path / 'labels.json')
    lab = lambda t: {'emoji': 'x', 'title': t, 'summary': '', 'risk': 'low'}
    label.save_cache(path, {'a': lab('A part'), 'b': lab('B part')})
    mine = json.load(open(path))                # a label.py run reads the cache...
    base = dict(mine)
    label.save_cache(path, {'b': lab('B answered'), 'c': lab('C part')})     # ...the worker answers meanwhile...
    mine['a'] = lab('A redone')                 # ...the run changes a, and still holds the b it read
    got = label.save_cache(path, mine, base)
    assert got == json.load(open(path)) == {'a': lab('A redone'), 'b': lab('B answered'), 'c': lab('C part')}
    assert base['a'] == lab('A redone')
    label.save_cache(path, {'a': lab('A by the worker')})
    label.save_cache(path, mine, base)          # nothing changed since its last save: nothing written over
    assert json.load(open(path))['a'] == lab('A by the worker')
