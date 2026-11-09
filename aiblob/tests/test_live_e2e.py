"""The live page in headless Chrome against liveserve.py and a simulated writer: provisional steps faded then solid
when they commit, auto-advance at 4x and the jump past 20, the "+N new" pill, a rebuild keeping the step, Re-layout,
and the static page untouched. Screenshots go to $AIBLOB_SHOTS (default: the test's tmp dir)."""
import json, os, shutil, threading, time

import pytest

import liveserve, timeline
from livesim import SimWriter, dumps, raw_timeline

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
AIG = os.path.join(REPO, 'web', 'timeline-aigource.json')

browser = pytest.importorskip('browser')
pytestmark = pytest.mark.skipif(not (browser.CHROME and shutil.which('node')), reason='needs Chrome and node')


@pytest.fixture
def env(tmp_path):
    shots = os.environ.get('AIBLOB_SHOTS') or str(tmp_path)
    os.makedirs(shots, exist_ok=True)
    made = []

    def start(head, lines, committed, tail):
        w = SimWriter(str(tmp_path / f'store{len(made)}'), head)
        w.commit(lines[:committed])
        w.set_tail(tail)
        srv, live, stop, poller = liveserve.serve(w.store, os.path.join(REPO, 'demo', 'labels.json'), port=0, poll=0.05)
        threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
        made.append((srv, stop))
        return w, f'http://127.0.0.1:{srv.server_address[1]}'

    b = browser.Browser()
    yield b, start, lambda name: b.shot(os.path.join(shots, name))
    b.close()
    for srv, stop in made:
        stop.set()
        srv.shutdown()
        srv.server_close()


def full_step(lines, i, change):
    """Step i as a full step (any blob changed by change(blob) -> blob), a valid provisional step after step i - 1."""
    st = timeline.expand({'schema': 5, 'steps': [json.loads(l) for l in lines[:i + 1]]})[i]
    return dumps(dict(st, full=True, blobs=[change(b) for b in st['blobs']]))


POS = 'JSON.stringify([...U.values()].map(u => [u.id, u.x, u.y]))'
# how far the map canvas is from the background, on average (the fade of a provisional step brings it closer)
INK = '''(() => { const d = ctx.getImageData(0, 0, cv.width, cv.height).data, c = document.createElement('canvas').getContext('2d');
  c.fillStyle = T.bg; c.fillRect(0, 0, 1, 1); const g = c.getImageData(0, 0, 1, 1).data; let s = 0;
  for (let i = 0; i < d.length; i += 4) s += Math.abs(d[i] - g[0]) + Math.abs(d[i + 1] - g[1]) + Math.abs(d[i + 2] - g[2]);
  return s / (d.length / 4); })()'''
# what the steps hold once built (normalizeStep, stains, sessions), to compare a live page with a static load
DIGEST = '''JSON.stringify({ steps: STEPS.map((s, i) => ({ idx: s.idx, sci: s.sci, tint: s.tint, stains: [...s.stains], kids: [...s.kids],
  dataNodes: s.dataNodes, maps: [IDX[i].blobs.size, IDX[i].funcs.size, IDX[i].owner.size], edges: s.edges.length,
  blobs: s.blobs.map(b => [b.id, b.face, b.title, b.size, b.label || null]), areas: s.areas })),
  sess: [...SESS.values()], gits: GITS, bounds: BOUNDS })'''


def test_live_page(env):
    b, start, shot = env
    head, lines = raw_timeline(os.path.join(REPO, 'web', 'timeline.json'))
    w, url = start(head, lines, 10, lines[10:12])
    b.goto(url + '/')                                              # the server's root is the live page
    b.wait('typeof BOOTED !== "undefined" && BOOTED && STEPS.length === 12 && NCOMMIT === 10')
    assert b.eval('location.search') == '?live=1'
    assert b.eval('[cur, TRACK, !!STEPS[11].prov, !!STEPS[10].prov, !!STEPS[9].prov]') == [11, True, True, True, False]
    assert b.eval('!$("#liveBadge").hidden && !$("#relayoutBtn").hidden && $("#newPill").hidden')
    b.wait('FADE > 0.95')
    shot('live-01-provisional-faded.png')
    faded = b.eval(INK)
    pos0, k0 = json.loads(b.eval(POS)), b.eval('K')

    # the tail commits: steps 10 and 11 lock in where they are, the viewer stays on 11, unfaded
    w.commit(lines[10:12])
    w.set_tail([])
    b.wait('NCOMMIT === 12 && STEPS.length === 12')
    assert b.eval('[cur, !!STEPS[11].prov, !!STEPS[10].prov]') == [11, False, False]
    b.wait('FADE === 0')
    shot('live-02-locked-in-solid.png')
    assert faded < 0.7 * b.eval(INK)                               # the same step, drawn solid now
    assert 'provisional' not in b.eval('$("#l1").textContent')

    # a provisional step redone: its stains go with it (a function stained by the first version, not the second)
    st = timeline.expand({'schema': 5, 'steps': [json.loads(l) for l in lines[:13]]})[12]
    b0 = next(x for x in st['blobs'] if x['funcs'] and x['delta']['state'] == 'same')
    f0 = dict(b0['funcs'][0], delta={'added': 0, 'removed': 7, 'state': 'changed'})
    fake = dict(st, full=True, blobs=[dict(x, funcs=[f0] + x['funcs'][1:]) if x is b0 else x for x in st['blobs']])
    w.set_tail([dumps(fake)])
    b.wait(f'STEPS.length === 13 && STEPS[12].stains.has({json.dumps("f:" + f0["qual"])})')

    # on the last step: a new tail step is followed (auto-advance) and drawn faded. __cur records the step shown
    # against the page's own play clock (frames of at most 100 ms, as frame() counts them)
    b.eval('window.__cur = []; window.__pt = 0; window.__lt = performance.now(); (function tick(now) { '
           '__pt += Math.min(0.1, Math.max(0, now - __lt) / 1000); __lt = now; __cur.push([__pt * 1000, cur]); '
           'requestAnimationFrame(tick); })(__lt); 1')
    w.set_tail(lines[12:13])
    b.wait(f'cur === 12 && STEPS[12].prov && FADE > 0.95 && !STEPS[12].stains.has({json.dumps("f:" + f0["qual"])})')
    shot('live-03-followed-provisional.png')

    # away from the end: "+N new" counts what came, and jumps to the newest
    b.eval('setStep(4); 1')
    assert b.eval('TRACK') is False
    w.commit(lines[12:15])
    w.set_tail(lines[15:16])
    b.wait('NCOMMIT === 15 && STEPS.length === 16 && !$("#newPill").hidden')
    assert b.eval('[cur, $("#newPill").textContent]') == [4, '+3 new']
    shot('live-04-new-pill.png')
    # the tail redone (same length, a bigger blob): the pill does not count it, its areas' faces are measured again
    old = b.eval('STEPS[15].areas.map(a => faceR(a.id, 15))')
    big = b.eval('(() => { let best = null, bv = -1; for (const x of STEPS[15].blobs) {   // the blob an area face is sized by\n'
                 '  const l = AU.get(x.area)?.local.get(x.id); if (!l) continue; const v = Math.hypot(l.x, l.y) + radius(x.size);\n'
                 '  if (v > bv) { bv = v; best = x.id; } } return best; })()')
    w.set_tail([full_step(lines, 15, lambda x: dict(x, size=x['size'] * 4) if x['id'] == big else x)])
    b.wait(f'STEPS.length === 16 && IDX[15].blobs.get({json.dumps(big)}).size === 4 * IDX[14].blobs.get({json.dumps(big)}).size')
    assert b.eval('[cur, $("#newPill").textContent]') == [4, '+3 new']
    now, fresh = b.eval('(() => { const ids = STEPS[15].areas.map(a => a.id), c = ids.map(id => faceR(id, 15)); FACE_R.delete(15); '
                        'return [c, ids.map(id => faceR(id, 15))]; })()')
    assert now == fresh and now != old                             # measured again, and the bigger blob shows
    # a tail event that does not follow the committed steps the page has is not drawn
    b.eval('ES.dispatchEvent(new MessageEvent("tail", { data: JSON.stringify({ epoch: EPOCH, base: NCOMMIT - 1, gen: 99, tail: [] }) })); 1')
    assert b.eval('[STEPS.length, NCOMMIT]') == [16, 15]
    # a steps event that repeats a committed step the page has: only what is new is added
    b.eval('fetch("live/timeline.json").then(r => r.json()).then(d => { window.__tl = d; return 1; })')
    b.eval('ES.dispatchEvent(new MessageEvent("steps", { data: JSON.stringify({ epoch: EPOCH, from: NCOMMIT - 1, gen: 99, '
           'steps: [__tl.steps[NCOMMIT - 1]], tail: __tl.steps.slice(NCOMMIT) }) })); 1')
    assert b.eval('[STEPS.length, NCOMMIT]') == [16, 15]
    # an event of another epoch is not drawn: the page loads the timeline again instead
    b.eval('ES.dispatchEvent(new MessageEvent("tail", { data: JSON.stringify({ epoch: EPOCH + 1, base: NCOMMIT, gen: 99, tail: [] }) })); 1')
    b.wait('ES && ES.readyState === 1 && STEPS.length === 16 && NCOMMIT === 15')
    time.sleep(0.5)
    assert b.eval('[STEPS.length, NCOMMIT]') == [16, 15]
    w.set_tail(lines[15:16])
    b.wait(f'STEPS.length === 16 && IDX[15].blobs.get({json.dumps(big)}).size === IDX[14].blobs.get({json.dumps(big)}).size')
    b.eval('$("#newPill").click(); 1')
    assert b.eval('[cur, TRACK, $("#newPill").hidden]') == [15, True, True]

    # following at 4x: two new steps are walked through one by one, 1100 / 4 ms of play clock apart (1x: 1100)
    b.eval('__cur.length = 0; 1')
    w.commit(lines[15:17])
    w.set_tail(lines[17:18])
    b.wait('cur === 17')
    seen = b.eval('(() => { const t = {}; for (const [ms, c] of __cur) if (!(c in t)) t[c] = ms; return t; })()')
    assert '16' in seen and '17' in seen and 200 < seen['17'] - seen['16'] < 550, seen
    shot('live-05-auto-advanced.png')

    # blobs placed at the first load never moved while the others came (K neither)
    assert b.eval('K') == k0 and b.eval('U.size') > len(pos0)
    pos = {i: (x, y) for i, x, y in json.loads(b.eval(POS))}
    assert all(pos[i] == (x, y) for i, x, y in pos0)

    # play does not stop at the end, nor rewind; then the steps built one by one equal a static load of them
    b.eval('speed = 8; togglePlay(); 1')
    time.sleep(1.5)
    assert b.eval('[playing, cur]') == [True, 17]
    b.eval('togglePlay(); speed = 1; 1')
    live = json.loads(b.eval(DIGEST))
    assert b.eval('fetch("live/timeline.json").then(r => r.json()).then(d => { normalize(d); return STEPS.length; })') == 18
    assert json.loads(b.eval(DIGEST)) == live
    b.eval('liveReload(); 1')                                    # (the page as it was)
    b.wait('STEPS.length === 18 && NCOMMIT === 17')

    # a rebuild with other steps: the page loads them again and stays on its step
    b.eval('setStep(5); 1')
    epoch = b.eval('EPOCH')
    other = [dumps(dict(json.loads(l), said='rebuilt: another step 5') if i == 4 else json.loads(l)) for i, l in enumerate(lines[:8])]
    w.rebuild(other, tail=lines[8:9])
    b.wait(f'EPOCH !== {epoch} && STEPS.length === 9 && NCOMMIT === 8')
    assert b.eval('[cur, STEPS[4].said, TRACK, $("#newPill").hidden]') == [5, 'rebuilt: another step 5', False, True]
    shot('live-06-after-rebuild.png')

    # a rebuild that commits fewer steps than the page has, another step 6 in its tail: the page never keeps the old
    # committed step 6 (and the pill counts nothing: as many steps as before)
    epoch = b.eval('EPOCH')
    alt = dumps(dict(json.loads(other[6]), said='rebuilt: another step 7'))
    w.rebuild(other[:6], tail=[alt, other[7], lines[8]])
    b.wait(f'EPOCH !== {epoch} && NCOMMIT === 6 && STEPS.length === 9')
    assert b.eval('[STEPS[6].said, !!STEPS[6].prov, cur, $("#newPill").hidden]') == ['rebuilt: another step 7', True, 5, True]

    # Re-layout lays every blob out again
    b.eval('$("#relayoutBtn").click(); 1')
    assert b.eval('U.size') == b.eval('new Set(STEPS.flatMap(s => s.blobs.map(b => b.id))).size')
    assert b.errors() == []

    # the same server, the static page: no live state at all
    b.goto(url + '/index.html?live=0')
    b.wait('typeof STEPS !== "undefined" && STEPS.length === 18')
    shown = '["#liveBadge", "#relayoutBtn", "#newPill"].map(s => getComputedStyle($(s)).display)'
    assert b.eval(shown) == ['none'] * 3
    assert b.eval('[LIVE, ES, $("#src").textContent, STEPS.some(s => s.prov), cur]') == [
        False, None, 'timeline.json', False, 17]
    shot('static-01-timeline-json.png')
    assert b.errors() == []


@pytest.mark.skipif(not os.path.exists(AIG), reason='web/timeline-aigource.json is not here')
def test_live_page_jumps_when_far_behind(env):
    b, start, shot = env
    head, lines = raw_timeline(AIG)
    lines = lines[:160]
    w, url = start(head, lines, 100, [])
    b.goto(url + '/index.html?live=1&areas=1')
    b.wait('typeof BOOTED !== "undefined" && BOOTED && STEPS.length === 100', 60000)
    b.eval('window.__cur = [cur]; setInterval(() => __cur.push(cur), 10); 1')
    w.commit(lines[100:140])                                     # 40 at once: more than 20 behind
    w.set_tail(lines[140:150])
    b.wait('cur === 149 && NCOMMIT === 140', 30000)
    seen = b.eval('[...new Set(__cur)]')
    assert seen == [99, 149], seen                               # no walk through the 50 new steps
    shot('live-07-jumped-areas.png')
    assert b.errors() == []
