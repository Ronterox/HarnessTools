"""The step highlight in headless Chrome on the static page: a blob created or modified at a step is lit for about
2 s (created: a green bud, modified: yellow rings), with areas off, inside an open area and in the drill-down; it is
gone by 2.3 s and nothing pulses after; a deleted blob or function fades out in red; fast steps shorten it and cap how many run; reduced motion holds a still
outline. Screenshots (Follow on, mid-pulse and after) go to $AIBLOB_SHOTS (default: the test's tmp dir)."""
import functools, http.server, json, os, shutil, threading, time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
WEB = os.path.join(REPO, 'web')
AIG = os.path.join(WEB, 'timeline-aigource.json')

browser = pytest.importorskip('browser')
pytestmark = pytest.mark.skipif(not (browser.CHROME and shutil.which('node')), reason='needs Chrome and node')


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture(scope='module')
def url():
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Quiet, directory=WEB))
    threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
    yield f'http://127.0.0.1:{srv.server_address[1]}'
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def b():
    br = browser.Browser()
    yield br
    br.close()


# the first step from a third of the way on whose biggest edit (as Follow picks it) is a created / a modified blob
PICK = '''(() => { const out = {};
  for (let i = Math.max(1, STEPS.length / 3 | 0); i < STEPS.length; i++) {
    const s = STEPS[i]; if (isGit(s)) continue;
    const hit = s.blobs.map(b => [b, b.delta.state === 'new' ? 'new' : 'mod', b.delta.state === 'new' ? b.size : b.delta.added + b.delta.removed])
      .filter(h => h[2] > 0 && !PAGE(h[0].kind)).sort((p, q) => q[2] - p[2]);
    if (hit.length && !out[hit[0][1]]) out[hit[0][1]] = [i, hit[0][0].id, hit[0][0].area || null];
  }
  return out; })()'''
# pixels near a colour in the ring just outside blob id's outline (R + 2 .. 1.1 R + 32), on the canvas as last drawn
RING = '''((id, hex) => { const v = VIS.get(id), x = sx(v.x) * DPR, y = sy(v.y) * DPR, R = v.r * VIEW.s * DPR, r0 = R + 2 * DPR, r1 = R * 1.1 + 32 * DPR;
  const c = [1, 3, 5].map(k => parseInt(hex.slice(k, k + 2), 16)), x0 = Math.max(0, x - r1 | 0), y0 = Math.max(0, y - r1 | 0);
  const w = Math.min(cv.width, x + r1 + 1 | 0) - x0, h = Math.min(cv.height, y + r1 + 1 | 0) - y0; if (w <= 0 || h <= 0) return 0;
  const d = ctx.getImageData(x0, y0, w, h).data; let n = 0;
  for (let j = 0; j < h; j++) for (let i = 0; i < w; i++) {
    const r = Math.hypot(x0 + i - x, y0 + j - y); if (r < r0 || r > r1) continue;
    const k = (j * w + i) * 4; if (Math.abs(d[k] - c[0]) + Math.abs(d[k + 1] - c[1]) + Math.abs(d[k + 2] - c[2]) < 90) n++;
  }
  return n; })'''


def load(b, url, src, extra=''):
    b.goto(f'{url}/index.html?src={src}{extra}')
    b.wait('typeof BOOTED !== "undefined" && BOOTED && STEPS.length > 0', 60000)
    b.eval(f'window.ring = {RING}; window.__fc = 0; (function count() {{ __fc++; requestAnimationFrame(count); }})(); 1')
    return b.eval(PICK)


def settle(b, ms=1500):
    """no step change for ms (a step after it pulses PULSE_MS long) and no pulse running"""
    b.wait(f'performance.now() - stepAt > {ms} && !PULSES.size && !FPULSES.size', 10000)


def step_to(b, i):
    """setStep(i) -> [the pulse it starts per blob id, measured right away]"""
    return b.eval(f'(() => {{ setStep({i}); const now = performance.now() + 30; '
                  f'return Object.fromEntries([...PULSES.keys()].map(id => [id, [PULSES.get(id).kind, pulseAt(PULSES, id, now)?.a > 0]])); }})()')


def at(b, ms):
    b.wait(f'performance.now() - stepAt > {ms}', 10000)


def frames(b, n):
    """wait for n more frames drawn"""
    k = b.eval('__fc')
    b.wait(f'__fc >= {k + n}', 30000)


def hold(b):
    """keep the running pulses lit while the canvas is read (a loaded machine may draw late); release() ends them
    when they would have"""
    b.eval('for (const p of [...PULSES.values(), ...FPULSES.values()]) { p.end0 = p.end; p.end = Infinity; } 1')


def release(b):
    b.eval('for (const p of [...PULSES.values(), ...FPULSES.values()]) p.end = p.end0; 1')


def ink(b, bid):
    return b.eval(f'[ring({json.dumps(bid)}, T.add), ring({json.dumps(bid)}, T.mix)]')


def test_created_and_modified_blobs_light_up_for_two_seconds(b, url):
    picks = load(b, url, 'sample-timeline.json', '&areas=0&follow=0')
    for kind in ('new', 'mod'):
        i, bid, _ = picks[kind]
        b.eval(f'setStep({i - 1}); 1')
        settle(b)
        got = step_to(b, i)
        assert got[bid] == [kind, True]                                  # lit at ~0 s
        k = json.dumps(bid)                                             # lit at 1.6 s, over by 2.3 s (1x: the full length)
        assert b.eval(f'[pulseAt(PULSES, {k}, stepAt + 1600)?.a > 0, pulseAt(PULSES, {k}, stepAt + 2300)]') == [True, None]
        hold(b)
        at(b, 450)
        frames(b, 2)
        mid = ink(b, bid)
        release(b)
        at(b, 2300)
        frames(b, 1)
        assert b.eval(f'[PULSES.has({json.dumps(bid)}), pulseAt(PULSES, {json.dumps(bid)}, performance.now()), PULSES.size, FPULSES.size]') == [
            False, None, 0, 0]                                          # gone at 2.3 s, nothing else running
        after = ink(b, bid)
        add, mix = mid[0] - after[0], mid[1] - after[1]
        if kind == 'new':
            assert add > 40 and add > 2 * mix, (mid, after)             # a green bud
        else:
            assert mix > 40 and mix > 2 * add, (mid, after)             # yellow rings
        # no pulse is drawn once they ended (the page draws on: its blobs breathe)
        n = b.eval('PULSE_FRAMES')
        time.sleep(0.3)
        assert b.eval('[PULSE_FRAMES, PULSES.size + FPULSES.size]') == [n, 0]
    assert b.errors() == []


def test_drill_down_and_open_area_light_up_too(b, url):
    picks = load(b, url, 'timeline.json', '&areas=1&follow=0')
    i, bid, area = picks['new']
    assert area, 'timeline.json has areas (schema v5)'
    # inside an open area: the created blob's green bud shows where it sits in the area (opened at that step: the
    # area may be new with it; the camera stays while the step goes back and forth)
    b.eval(f'setStep({i}); openArea({json.dumps(area)}); 1')
    b.wait('CAM === null', 10000)
    b.eval(f'setStep({i - 1}); 1')
    settle(b)
    step_to(b, i)
    hold(b)
    at(b, 450)
    frames(b, 2)
    mid = ink(b, bid)
    release(b)
    at(b, 2500)
    after = ink(b, bid)
    assert mid[0] - after[0] > 40, (mid, after)
    # the drill-down (the sample: timeline.json edits no lines of a blob it already has)
    picks = load(b, url, 'sample-timeline.json', '&areas=0&follow=0')
    i, bid, _ = picks['mod']                 # its functions edited at that step pulse, as their blob did
    b.eval(f'setStep({i - 1}); openDrill({json.dumps(bid)}); 1')
    settle(b)
    step_to(b, i)
    want = b.eval(f'IDX[{i}].blobs.get({json.dumps(bid)}).funcs.filter(f => f.delta.state === "new" || f.delta.added + f.delta.removed > 0)'
                  '.map(f => ["f:" + f.qual, f.delta.state === "new" ? "new" : "mod"])')
    assert want
    assert b.eval('Object.fromEntries([...FPULSES].filter(([k]) => k.startsWith("f:")).map(([k, p]) => [k, p.kind]))') == dict(want)
    q = want[0][0]
    assert b.eval(f'drill && pulseAt(FPULSES, {json.dumps(q)}, stepAt + 400).a > 0')
    at(b, 2500)
    assert b.eval('FPULSES.size + PULSES.size') == 0
    assert b.errors() == []


def test_fast_steps_shorten_and_cap_the_pulses(b, url):
    load(b, url, 'sample-timeline.json', '&areas=0')
    b.eval('setStep(0); 1')
    settle(b)
    assert b.eval('STEPS.length') > 17          # (15 presses, then two steps at 1x, all with edits in the sample)
    # a held →: key repeats far faster than PULSE_MIN / 3 apart; the pulses are shortened and at most PULSE_MAX run
    # (the others fade out early)
    most, durs, lo, hi = b.eval('''(() => { let most = 0; const durs = [];
      for (let k = 0; k < 15; k++) {
        dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight' }));
        const on = [...PULSES.values()].filter(p => !p.out); most = Math.max(most, on.length);
        if (k) durs.push(...on.filter(p => p.t0 === stepAt && p.kind !== 'del').map(p => p.end - p.t0));
      }
      return [most, durs, PULSE_MIN, PULSE_MS]; })()''')
    assert 0 < most <= b.eval('PULSE_MAX'), most
    assert durs and set(durs) == {lo} and lo < hi, durs
    b.wait('!PULSES.size && !FPULSES.size', 3000)
    # at 1x play (1100 ms a step) the pulse runs its full length
    b.eval('setStep(cur + 1); 1')
    at(b, 1100)
    got = b.eval('(() => { setStep(cur + 1); return [...PULSES.values()].filter(p => !p.out && p.t0 === stepAt).map(p => p.end - p.t0); })()')
    assert got and all(d == b.eval('PULSE_MS') or d == b.eval('PULSE_DEL') for d in got), got
    assert b.errors() == []


def test_reduced_motion_holds_a_still_outline(b, url):
    b.media(prefers_reduced_motion='reduce')
    picks = load(b, url, 'sample-timeline.json', '&areas=0')
    i, bid, _ = picks['new']
    b.eval(f'setStep({i - 1}); 1')
    settle(b)
    step_to(b, i)
    k = json.dumps(bid)
    # no rise, no fade, no swell: the same outline the whole time (the pulse as drawn at those times after the step)
    assert b.eval(f'[60, 1000, 1800].map(ms => {{ const p = pulseAt(PULSES, {k}, stepAt + ms); return [p.a, pulseScale(p)]; }})') == [[1, 1]] * 3
    assert b.eval(f'pulseAt(PULSES, {k}, stepAt + 2001)') is None
    at(b, 2500)
    assert b.eval(f'PULSES.has({k})') is False
    assert b.errors() == []


# timeline.json (the Flask demo) only creates blobs: no step edits lines of one it has (its `changed` blobs only had
# code moved in); sample-timeline.json (a smaller Flask app) has both
# blobs deleted by a step, functions removed by one (with the blob they left), blobs changed at two steps in a row
FIND = '''(() => { const del = [], ghost = [], again = [];
  const changed = j => new Set(STEPS[j].blobs.filter(b => b.delta.state === 'new' || b.delta.added + b.delta.removed > 0).map(b => b.id));
  for (let i = 1; i < STEPS.length; i++) {
    for (const id of IDX[i - 1].blobs.keys()) if (!IDX[i].blobs.has(id)) del.push([i, id]);
    for (const s of STEPS[i].stains.values()) if (s.gone && s.step === i && IDX[i].blobs.has(s.blob)) ghost.push([i, s.q, s.blob]);
    const a = changed(i - 1); for (const id of changed(i)) if (a.has(id)) again.push([i - 1, id]);
  }
  return { del, ghost, again }; })()'''


def test_deleted_blobs_and_removed_functions_fade_out_in_red(b, url):
    load(b, url, 'sample-timeline.json', '&areas=0&follow=0')
    found = b.eval(FIND)
    i, bid = found['del'][0]
    k, dl = json.dumps(bid), b.eval('PULSE_DEL')
    b.eval(f'setStep({i - 1}); 1')
    settle(b)
    assert step_to(b, i)[bid] == ['del', True]
    assert b.eval(f'[pulseAt(PULSES, {k}, stepAt + {dl - 50})?.a > 0, pulseAt(PULSES, {k}, stepAt + {dl})]') == [True, None]
    at(b, dl + 100)
    frames(b, 1)
    assert b.eval(f'PULSES.has({k})') is False                          # gone by 0.9 s
    # going back over the deletion while it fades: the blob is back, without the red
    assert b.eval(f'setStep({i - 1}); setStep({i}); PULSES.get({k})?.kind') == 'del'
    b.eval(f'setStep({i - 1}); 1')
    assert b.eval('[...PULSES].filter(([id, p]) => p.kind === "del" && IDX[cur].blobs.has(id)).map(([id]) => id)') == []
    # a removed function: a red ring on its ghost in the drill-down of the blob it left; none once it is back
    j, q, owner = found['ghost'][0]
    g = json.dumps('g:' + q)
    b.eval(f'setStep({j - 1}); openDrill({json.dumps(owner)}); 1')
    settle(b)
    step_to(b, j)
    assert b.eval(f'[FPULSES.get({g})?.kind, pulseAt(FPULSES, {g}, stepAt + 400)?.a > 0, !!drill]') == ['del', True, True]
    b.eval(f'setStep({j - 1}); 1')
    assert b.eval(f'FPULSES.has({g})') is False
    assert b.errors() == []


def test_going_back_onto_a_creation_step_keeps_the_size(b, url):
    picks = load(b, url, 'sample-timeline.json', '&areas=0&follow=0')
    i, bid, _ = picks['new']
    k = json.dumps(bid)
    assert b.eval(f'IDX[{i + 1}].blobs.has({k})')
    # forward onto it: it buds
    b.eval(f'setStep({i - 1}); 1')
    settle(b)
    step_to(b, i)
    assert b.eval(f'pulseScale(pulseAt(PULSES, {k}, stepAt + 100))') < 0.9
    # back onto it from the next step, where it is full size: lit, but no bud (its new functions neither)
    b.eval(f'setStep({i + 1}); 1')
    settle(b)
    assert b.eval(f'VIS.get({k}).r >= 0.9 * radius(IDX[{i}].blobs.get({k}).size)')
    step_to(b, i)
    assert b.eval(f'(() => {{ const p = pulseAt(PULSES, {k}, stepAt + 100); return [p.kind, pulseScale(p)]; }})()') == ['new', 1]
    n, ok = b.eval('(() => { const fs = [...FPULSES.keys()].filter(q => q.startsWith("f:") && FPULSES.get(q).kind === "new");'
                   ' return [fs.length, fs.every(q => pulseScale(pulseAt(FPULSES, q, stepAt + 100)) === 1)]; })()')
    assert n > 0 and ok
    assert b.errors() == []


def test_a_blob_changed_again_starts_its_pulse_over(b, url):
    load(b, url, 'sample-timeline.json', '&areas=0&follow=0')
    j, bid = b.eval(FIND)['again'][0]
    k = json.dumps(bid)
    b.eval(f'setStep({j}); 1')
    at(b, 500)
    t0 = b.eval(f'PULSES.get({k}).t0')
    b.eval(f'setStep({j + 1}); 1')
    assert b.eval(f'[PULSES.get({k}).t0 === stepAt, PULSES.get({k}).t0 > {t0}]') == [True, True]
    assert b.errors() == []


def test_a_step_shown_again_quietly_keeps_its_pulses(b, url):
    # live mode's liveShow(i, false): the step shown again as it was (a reload, a tail redone the same) replays nothing;
    # liveShow(i, true) (its content changed) lights it again
    picks = load(b, url, 'sample-timeline.json', '&areas=0&follow=0')
    i = picks['mod'][0]
    b.eval(f'setStep({i}); 1')
    at(b, 300)
    snap = 'JSON.stringify([...PULSES, ...FPULSES])'
    assert b.eval(f'(() => {{ const a = {snap}; liveShow({i}, false); return a === {snap} && PULSES.size > 0; }})()')
    assert b.eval(f'(() => {{ const a = {snap}; liveShow({i}, true); return a !== {snap} && [...PULSES.values()].some(p => p.t0 === stepAt); }})()')
    assert b.errors() == []


SHOTS = [('timeline.json', 'flask'), ('sample-timeline.json', 'flask-sample')] + ([('timeline-aigource.json', 'aigource')] if os.path.exists(AIG) else [])


@pytest.mark.parametrize('src,name', SHOTS, ids=[n for _, n in SHOTS])
def test_screenshots_mid_pulse_and_after(b, url, tmp_path, src, name):
    shots = os.environ.get('AIBLOB_SHOTS') or str(tmp_path)
    os.makedirs(shots, exist_ok=True)
    picks = load(b, url, src, '&areas=0&follow=1')
    assert 'new' in picks and (name == 'flask' or 'mod' in picks), picks
    for kind, what in (('new', 'created'), ('mod', 'modified')):
        if kind not in picks:
            continue
        i, bid, _ = picks[kind]
        b.eval(f'setStep({i - 1}); 1')
        settle(b)
        step_to(b, i)
        assert b.eval('followTo') == bid                                  # Follow lands on the lit blob
        # held mid-pulse for the shot: headless Chrome (no GPU) draws the aigource map about once a second
        b.eval('for (const p of [...PULSES.values(), ...FPULSES.values()]) p.end = Infinity; 1')
        at(b, 700)
        frames(b, 2)
        b.shot(os.path.join(shots, f'{name}-{what}-mid.png'))
        b.eval('PULSES.clear(); FPULSES.clear(); 1')
        at(b, 2600)
        frames(b, 2)
        b.shot(os.path.join(shots, f'{name}-{what}-after.png'))
    assert b.errors() == []
