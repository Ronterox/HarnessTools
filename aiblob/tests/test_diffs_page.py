"""Diffs inside blobs on the page (web/index.html, SCHEMA.md "Diffs"): the page's <diffcore> block, run in node on the
Flask and c-mini builds, gives what diffs.Reader.range_texts() gives, with difflib's hunks; in headless Chrome the
drill-down of a changed blob wears Skins (a bite per hunk with removed lines, a hair per added line, 5 to a thick
one, all on the blob's own rim, the notch in the scar colour) or Twins, with hover / click / scope switching; a page
(text blob) wears its marks on its edge; live mode reads texts from /live/bodies; the drill-down of a blob nothing
changed in (and of a map without the sidecar) is the same picture as the page at HEAD draws, pixel for pixel; no
console errors. AIBLOB_PAGE: another page to test (the mutant runner's); screenshots go to $AIBLOB_SHOTS."""
import functools, gzip, http.server, json, os, random, shutil, subprocess, sys, threading, time
from difflib import SequenceMatcher

import pytest

import diffs, replay, timeline

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PAGE = os.environ.get('AIBLOB_PAGE') or os.path.join(REPO, 'web', 'index.html')
TL = os.path.join(REPO, 'timeline.py')
NODE = shutil.which('node')
browser = pytest.importorskip('browser')
chrome = pytest.mark.skipif(not (browser.CHROME and NODE), reason='needs Chrome and node')


# -- the reference: Reader.range_texts() plus difflib's hunks (2 lines of context), lines split at \n -----------------
def lines(t):
    if not t:
        return []
    L = t.split('\n')
    if len(L) > 1 and L[-1] == '':
        L.pop()
    return L


def hunks(old, new):
    A, B, out = lines(old), lines(new), []
    for g in SequenceMatcher(None, A, B, autojunk=False).get_grouped_opcodes(2):
        rows = []
        for tag, i1, i2, j1, j2 in g:
            if tag == 'equal':
                rows += [[' ', x] for x in A[i1:i2]]
                continue
            if tag != 'insert':
                rows += [['-', x] for x in A[i1:i2]]
            if tag != 'delete':
                rows += [['+', x] for x in B[j1:j2]]
        out.append({'os': g[0][1] + 1, 'ns': g[0][3] + 1, 'lines': rows})
    return out


def want(reader, a, b, bid):
    return [dict(e, hunks=hunks(e['old'], e['new'])) for e in reader.range_texts(a, b, bid)]


def hairs_and_bites(hs):
    """What Skins draws for hunks: a bite per hunk with removed lines; per hunk, its added lines bundled by 5 into
    thick hairs, the rest one thin hair each."""
    add = [sum(1 for op, _ in h['lines'] if op == '+') for h in hs]
    return sum(n // 5 + n % 5 for n in add), sum(1 for h in hs if any(op == '-' for op, _ in h['lines']))


# -- the page's code in node ----------------------------------------------------------------------------------------
def block(html, name):
    return html[html.index(f'/* <{name}>'):html.index(f'/* </{name}> */')]


NODE_DRIVER = r"""
const fs = require('fs'), zlib = require('zlib'), [tlPath, dir, casesPath] = process.argv.slice(1);
const steps = expandSteps(JSON.parse(fs.readFileSync(tlPath, 'utf8'))), ix = JSON.parse(zlib.gunzipSync(fs.readFileSync(dir + '/index.json.gz')));
const where = new Map(); for (const s of Object.keys(ix.chunks)) for (const h of ix.chunks[s]) where.set(h, s);
const chunks = new Map(), texts = { get(h) { const s = where.get(h); if (s == null) return undefined;
  if (!chunks.has(s)) chunks.set(s, JSON.parse(zlib.gunzipSync(fs.readFileSync(`${dir}/s/${s}.json.gz`)))); return chunks.get(s)[h]; } };
const FM = [], D = { steps, fvs: ix.fv, funcsAt: i => FM[i] ||= new Map(steps[i].blobs.flatMap(b => (b.funcs || []).map(f => [f.qual, f]))) };
const out = JSON.parse(fs.readFileSync(casesPath, 'utf8')).map(([a, b, bid]) =>
  diffFill(diffEntries(D, a, b, bid), texts).map(e => { const h = diffHunks(e.old, e.new); return { ...e, hunks: h.hunks }; }));
process.stdout.write(JSON.stringify(out));
"""


def node_ranges(tl, ddir, cases, tmp_path):
    with open(PAGE, encoding='utf-8') as fh:
        html = fh.read()
    js = block(html, 'expand') + block(html, 'diffcore') + NODE_DRIVER
    (tmp_path / 'cases.json').write_text(json.dumps(cases))
    r = subprocess.run([NODE, '-e', js, str(tl), str(ddir), str(tmp_path / 'cases.json')], capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout)


def builds(tmp_path):
    """(name, timeline.json, diffs dir) for the Flask session (when it is on this machine) and c-mini."""
    from test_live import APP, FLASK, _fixture_session
    srcs = [('c', _fixture_session(tmp_path / 'c', 'c-mini', '/work/c', [('main.c', 'int main', 'int  main'), ('util.c', 'return', 'return ')]), '/work/c')]
    if os.path.isdir(FLASK):
        srcs.append(('flask', FLASK, APP))
    out = []
    for name, src, root in srcs:
        tl, d = tmp_path / f'{name}.json', tmp_path / f'{name}.diffs'
        subprocess.run([sys.executable, TL, src, '--root', root, '--no-timings', '-o', str(tl), '--diffs', str(d)], check=True, capture_output=True)
        out.append((name, tl, d))
    (tmp_path / 'fx').mkdir()
    return out + [('fx', *fixture_build(tmp_path / 'fx'))]


def cases_for(reader, n, seed):
    """Random (a, b, blob) with a <= b, plus each blob's this-step and since-the-start ranges at a few steps."""
    rnd, N, out = random.Random(seed), len(reader.steps), []
    for _ in range(n):
        a = rnd.randrange(N)
        b = rnd.randrange(a, N)
        ids = sorted({x['id'] for x in (reader.steps[a - 1]['blobs'] if a else []) + reader.steps[b]['blobs']})
        out += [[a, b, bid] for bid in rnd.sample(ids, min(4, len(ids)))]
    for b in rnd.sample(range(N), min(6, N)):
        out += [[a, b, x['id']] for x in reader.steps[b]['blobs'] for a in (b, 0)]
    return out


HUNK_DRIVER = r"""
const fs = require('fs'), pairs = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
process.stdout.write(JSON.stringify(pairs.map(([a, b]) => diffHunks(a, b).hunks)));
"""


@pytest.mark.skipif(not NODE, reason='node is not installed: the page code cannot run')
def test_page_hunks_are_difflibs_for_every_run_between_changes(tmp_path):
    """two edits (replace / insert / delete) apart by 0..9 unchanged lines, at the start, the middle and the end: the
    page's hunks are difflib's (whether they merge into one hunk turns on 2 x DIFF_CTX)"""
    with open(PAGE, encoding='utf-8') as fh:
        js = block(fh.read(), 'diffcore') + HUNK_DRIVER
    base, pairs = [f'line {i}' for i in range(30)], []
    for gap in range(10):
        for at in (0, 8, 30 - gap - 2):
            for kind in ('replace', 'insert', 'delete'):
                new = list(base)
                for p in sorted((at, at + gap + 1), reverse=True):
                    if p >= len(new):
                        continue
                    if kind == 'replace':
                        new[p] = new[p] + ' (edited)'
                    elif kind == 'insert':
                        new[p:p] = ['added a', 'added b']
                    else:
                        del new[p]
                pairs.append(['\n'.join(base) + '\n', '\n'.join(new) + '\n'])
    (tmp_path / 'pairs.json').write_text(json.dumps(pairs))
    r = subprocess.run([NODE, '-e', js, str(tmp_path / 'pairs.json')], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(r.stdout)
    for (a, c), g in zip(pairs, got):
        assert g == hunks(a, c), (a.count('\n'), c)
    assert {len(g) for g in got} == {1, 2}


@pytest.mark.skipif(not NODE, reason='node is not installed: the page code cannot run')
def test_page_diffcore_is_the_reference_reader(tmp_path):
    states, n = set(), 0
    for k, (name, tl, d) in enumerate(builds(tmp_path)):
        reader = diffs.load_static(str(tl), str(d))
        cases = cases_for(reader, 80, k)
        got = node_ranges(tl, d, cases, tmp_path)
        for (a, b, bid), g in zip(cases, got):
            w = want(reader, a, b, bid)
            assert g == w, (name, a, b, bid)
            states.update(e['state'] for e in w)
            n += sum(len(e['hunks']) for e in w)
    assert {'new', 'changed', 'same', 'deleted'} <= states and n > 100, (states, n)       # (the cases reach every state)


# -- a session made for the page: app.py (an entry blob), util.py (never edited), notes.md (a text blob) -------------
def _body(n):
    return [f'    v{i} = v{i - 1} * {i} + 1' if i else '    v0 = x' for i in range(n)]


def app(stage):
    """Step 1 rewrites edited() in two hunks (-3 +8: a thick hair and 3 thin ones; -1 +3), deletes gone_soon(),
    adds fresh() and edits main(); step 2 edits helper() only, and so does step 5 (steps 3-4 edit util.py)."""
    edited = ['def edited(x):', *_body(4), '    w1 = v3 - 1', '    w2 = w1 * 2', '    w3 = w2 + 7', *[f'    u{i} = w3 + {i}' for i in range(8)], '    return u7']
    if stage >= 1:
        edited = ['def edited(x):', *_body(4), *[f'    n{i} = v3 + {i * 3}' for i in range(7)], '    w3 = n6 + 7', *[f'    u{i} = w3 + {i}' for i in range(8)],
                  '    q = u7 * 2', '    q += 1', '    return q']
    helper = ['def helper(x):', '    return x + 1' if stage < 2 else '    return x + 2' if stage < 5 else '    return x + 3']
    gone = ['def gone_soon(y):', '    z = y * 2', '    z += 1', '    z -= 4', '    return z']
    fresh = ['def fresh(k):', '    s = 0', '    for i in range(k):', '        s += i', '    return s']
    main = ['def main():', '    print(edited(1), helper(2))', '    print(gone_soon(3))' if stage < 1 else '    print(fresh(3))']
    parts = [helper, edited] + ([gone] if stage < 1 else [fresh]) + [main, ["if __name__ == '__main__':", '    main()']]
    return '\n\n\n'.join('\n'.join(p) for p in parts) + '\n'


def util(stage):
    return '\n\n\n'.join('\n'.join([f'def tool{i}(a):', f'    b = a + {i + 10 * (stage >= 3 + i)}', '    return b * 2']) for i in range(3)) + '\n'


def notes(stage):
    L = ['# notes', '', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight']
    return '\n'.join(L[:3] + ['new a', 'new b', 'new c', 'new d', 'new e', 'new f'] + L[4:] if stage else L) + '\n'


APPB, UTILB, NOTES = 'app.<module>', 'orphan:util', 'file:notes.md'


def fixture_build(d):
    """The session's six steps analysed with their texts: d/tl.json and d/tl.diffs -> (both paths)."""
    snaps = [{'app.py': app(k), 'util.py': util(k), 'notes.md': notes(k)} for k in range(6)]
    pairs = [(replay.Step(i, '', 'Edit' if i else 'Write', 'util.py' if i in (3, 4) else 'app.py', f'step {i}', 0), s) for i, s in enumerate(snaps)]
    dw = diffs.Static(str(d / 'tl.diffs'))
    steps = list(timeline.analyzed_steps(iter(pairs), timings=False, diffs=dw))
    dw.close()
    (d / 'tl.json').write_text(json.dumps({'schema': timeline.SCHEMA, 'session': 't', 'root': '/work/fx', 'steps': timeline.compact(steps)}), encoding='utf-8')
    return d / 'tl.json', d / 'tl.diffs'


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture(scope='module')
def site(tmp_path_factory):
    """A directory served on a port of its own: the page, the session's map with its texts (tl.json, tl.diffs) and the
    same map without them (bare.json); the page at HEAD as head.html; the reference Reader."""
    d = tmp_path_factory.mktemp('diffpage')
    shutil.copy(PAGE, d / 'index.html')
    head = subprocess.run(['git', 'show', 'HEAD:aiblob/web/index.html'], cwd=REPO, capture_output=True)
    if head.returncode == 0:
        (d / 'head.html').write_bytes(head.stdout)
    fixture_build(d)
    shutil.copy(d / 'tl.json', d / 'bare.json')
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Quiet, directory=str(d)))
    threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
    yield {'url': f'http://127.0.0.1:{srv.server_address[1]}', 'dir': d, 'reader': diffs.load_static(str(d / 'tl.json'), str(d / 'tl.diffs'))}
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def b():
    br = browser.Browser()
    yield br
    br.close()


def load(b, url, page='index.html', src='tl.json'):
    b.goto(f'{url}/{page}?src={src}&areas=0&follow=0')
    b.wait('typeof BOOTED !== "undefined" && BOOTED && STEPS.length > 0', 60000)
    if page == 'index.html':
        b.wait('DV.on !== null', 30000)
    b.eval('window.__fc = 0; (function count() { __fc++; requestAnimationFrame(count); })(); 1')


def frames(b, n=3):
    k = b.eval('__fc')
    b.wait(f'__fc >= {k + n}', 30000)


def drill(b, step, bid, look='A', scope='step', since=None):
    """the drill-down of bid at step, in a look and a scope, once its range is in and drawn"""
    b.eval(f'closeDrill(); setStep({step}); openDrill({json.dumps(bid)}); setDiffLook({json.dumps(look)}); '
           f'setDiffScope({json.dumps(scope)}{"" if since is None else ", " + str(since)}); DV.at = performance.now() - 5000; 1')
    b.wait('(() => { const [a, c] = diffSpan(drill.id), r = diffRange(drill.id, a, c); return r.ok || !!r.err; })()', 30000)
    b.eval('DV.at = performance.now() - 5000; 1')
    frames(b)


MARKS = '''DV.marks.map(m => ({ q: m.q, page: !!m.page, hairs: m.hairs.length, thick: m.hairs.filter(h => h.big).length, bites: m.bites.length }))'''


def skins_want(reader, a, c):
    """{qual or 'file': (hairs, bites)} of the changed functions of app.py and of notes.md, from the reference"""
    out = {}
    for bid in (APPB, NOTES):
        for e in want(reader, a, c, bid):
            if e['state'] != 'same' and e['hunks'] and (bid == NOTES or e['newh'] or e['state'] == 'deleted'):
                out['file' if bid == NOTES else e['key']] = hairs_and_bites(e['hunks'])
    return out


@chrome
def test_skins_marks_count_hunks_and_bundles(b, site):
    load(b, site['url'])
    r = site['reader']
    for step, scope, a in ((1, 'step', 1), (2, 'step', 2), (2, 'since', 1), (2, 'session', 0)):
        w = skins_want(r, a, step)
        drill(b, step, APPB, 'A', scope, None if scope != 'since' else a - 1)
        assert b.eval('diffSpan(drill.id)') == [a, step]
        got = {m['q']: (m['hairs'], m['bites']) for m in b.eval(MARKS)}
        assert got == {k: v for k, v in w.items() if k != 'file'}, (step, scope)
        drill(b, step, NOTES, 'A', scope, None if scope != 'since' else a - 1)
        got = {m['q']: (m['hairs'], m['bites']) for m in b.eval(MARKS)}
        assert got == ({'file': w['file']} if 'file' in w else {}), (step, scope)
    # step 1 as made: edited() has two hunks with removals (-3 +8, -1 +3): 2 bites, 1 thick hair + 6 thin
    drill(b, 1, APPB)
    m = {x['q']: x for x in b.eval(MARKS)}
    assert (m['app.edited']['bites'], m['app.edited']['thick'], m['app.edited']['hairs']) == (2, 1, 7)
    assert m['app.gone_soon']['bites'] == 1 and m['app.fresh']['thick'] == 1
    assert b.eval('[diffDrill(drill.id).tot.changed, diffDrill(drill.id).tot.fresh, diffDrill(drill.id).tot.gone]') == [2, 1, 1]
    assert 'hair = an added line' in b.eval('$("#dleg").textContent') and 'bite = a block of removed lines' in b.eval('$("#dleg").textContent')
    assert b.errors() == []


# where blobPath draws an outline: its quadratic segments sampled densely, and the distance from each point asked
RIM = '''((m, pts) => { const segs = []; let at = null;
  blobPath({ beginPath() {}, closePath() {}, moveTo(x, y) { at = [x, y]; }, quadraticCurveTo(cx, cy, x, y) { segs.push([at, [cx, cy], [x, y]]); at = [x, y]; } }, m.x, m.y, m.r, m.seed, m.t, m.wob);
  const S = []; for (const [p, c, q] of segs) for (let k = 0; k <= 40; k++) { const u = k / 40, v = 1 - u; S.push([v * v * p[0] + 2 * u * v * c[0] + u * u * q[0], v * v * p[1] + 2 * u * v * c[1] + u * u * q[1]]); }
  return pts.map(([x, y]) => Math.min(...S.map(s => Math.hypot(s[0] - x, s[1] - y)))); })'''


@chrome
def test_marks_ride_the_wobbling_rim_and_notches_are_scarred(b, site):
    load(b, site['url'])
    drill(b, 1, APPB)
    b.eval(f'window.rimDist = {RIM}; 1')
    ts = set()
    for _ in range(4):                       # several frames: the rim breathes, the marks go with it
        got = b.eval('''DV.marks.filter(m => !m.page && m.wob === 1).map(m => ({ q: m.q, t: m.t,
          d: rimDist(m, [...m.hairs.map(h => [h.x, h.y]), ...m.bites.flatMap(x => x.mouth)]) }))''')
        assert got and all(max(g['d']) < 1.0 for g in got), got
        ts.update(round(g['t'], 3) for g in got)
        time.sleep(0.25)
        frames(b, 2)
    assert len(ts) >= 3, ts
    # a notch's edge is the scar's colour (--rem over the fill), never the border's: the pixels on its inner points
    votes = b.eval('''(() => { const c = s => { const x = document.createElement('canvas').getContext('2d'); x.fillStyle = s; x.fillRect(0, 0, 1, 1); return [...x.getImageData(0, 0, 1, 1).data].slice(0, 3); };
      const col = kindColors(IDX[cur].blobs.get(drill.id).kind), fill = c(col.fill), mix = (p, q) => p.map((v, i) => 0.7 * v + 0.3 * q[i]);
      const rem = mix(c(T.rem), fill), out = [];
      for (const m of DV.marks.filter(m => !m.page && m.bites.length && m.stroke !== T.rem)) {       // (a deleted function's ghost is red all over)
        const border = mix(c(m.stroke), fill);
        for (const b of m.bites) for (const [x, y] of b.pts.filter((_, i) => i % 3 === 1)) {        // the line's core: the 3x3 pixel farthest from the fill
          const D = ctx.getImageData(Math.round(x * DPR) - 1, Math.round(y * DPR) - 1, 3, 3).data, far = q => Math.hypot(...q.map((v, i) => v - fill[i]));
          let px = fill; for (let k = 0; k < 9; k++) { const q = [D[4 * k], D[4 * k + 1], D[4 * k + 2]]; if (far(q) > far(px)) px = q; }
          const d = q => Math.hypot(...px.map((v, i) => v - q[i]));
          out.push(d(rem) < d(border)); } }
      return out; })()''')
    assert len(votes) > 20 and sum(votes) > 0.8 * len(votes), (sum(votes), len(votes))
    # the border stops at a notch's mouth: the notch's first and last inner points are off its stroke
    inside = b.eval('''(() => { ctx.save(); ctx.setTransform(DPR, 0, 0, DPR, 0, 0); ctx.lineWidth = 1; const out = [];
      for (const m of DV.marks.filter(m => m.border)) for (const x of m.bites) for (const [px, py] of [x.pts[0], x.pts[x.pts.length - 1]]) out.push(ctx.isPointInStroke(m.border, px * DPR, py * DPR));
      ctx.restore(); return out; })()''')
    assert len(inside) >= 6 and not any(inside), inside
    assert b.errors() == []


@chrome
def test_hover_a_hunk_shows_it_and_a_click_opens_it(b, site, tmp_path):
    load(b, site['url'])
    drill(b, 1, APPB)
    feats = b.eval('DV.feats.filter(f => f.nd.q === "app.edited").map(f => ({ k: f.k, kind: f.kind, x: f.x, y: f.y, a: f.g.a, r: f.g.r }))')
    assert sorted(f['kind'] for f in feats) == ['+', '+', '-', '-']
    hair = next(f for f in feats if f['kind'] == '+' and f['a'] == 8)
    move = 'cv.dispatchEvent(new MouseEvent("{t}", {{ clientX: {x}, clientY: {y}, bubbles: true }})); 1'
    b.eval(move.format(t='mousemove', **hair))
    tip = b.eval('[$("#tip").style.display, $("#tip").textContent]')
    assert tip[0] == 'block' and 'n4 = v3 + 12' in tip[1] and 'w2 = w1 * 2' in tip[1] and '+3 more lines' in tip[1] and 'u0 = w3' not in tip[1], tip   # (changed lines, 8 at most)
    assert b.eval('featHot(DV.hov.g)') is True
    b.eval(move.format(t='click', **hair))
    b.wait('$("#dbloom").classList.contains("on")', 5000)
    txt = b.eval('$("#dbloom").textContent')
    assert '+8' in txt and '−3' in txt and 'n0 = v3 + 0' in txt and 'w1 = v3 - 1' in txt
    shots = os.environ.get('AIBLOB_SHOTS') or str(tmp_path)
    frames(b)
    b.shot(os.path.join(shots, 'diffs-fixture-bloom.png'))
    # the body: the whole function's diff; a deleted function's ghost: its removed lines
    p = b.eval('drill.nodes.find(n => n.q === "app.edited")')
    b.eval(move.format(t='click', x=p['x'], y=p['y']))
    b.wait('$("#dlens").style.display === "flex"', 5000)
    lens = b.eval('$("#dlens").textContent')
    assert 'edited' in lens and 'q += 1' in lens and 'return u7' in lens and 'unchanged line' in lens
    assert b.eval('$("#dbloom").style.display') == 'none'
    b.eval('closeDrill(); 1')
    assert b.eval('[$("#dlens").style.display, $("#dbar").hidden, $("#dlook").hidden]') == ['none', True, True]
    drill(b, 1, APPB)
    g = b.eval('DV.gones.find(g => g.q === "app.gone_soon")')
    b.eval(move.format(t='mousemove', x=g['x'], y=g['y']))
    assert 'gone_soon' in b.eval('$("#tip").textContent') and 'deleted' in b.eval('$("#tip").textContent')
    assert b.errors() == []


@chrome
def test_twins_side_by_side_and_scope_switching(b, site, tmp_path):
    load(b, site['url'])
    r = site['reader']
    drill(b, 1, APPB, 'B')
    assert b.eval('DV.marks.length') == 0 and b.eval('DV.gones.filter(g => g.twin).map(g => g.q).sort()') == ['app.edited', 'app.main']
    p = b.eval('drill.nodes.find(n => n.q === "app.edited")')
    b.eval(f'cv.dispatchEvent(new MouseEvent("click", {{ clientX: {p["x"]}, clientY: {p["y"]}, bubbles: true }})); 1')
    b.wait('$("#dlens").style.display === "flex"', 5000)
    assert b.eval('[!!$("#dlens .code.sbs"), $("#dlens .heads").textContent]') == [True, 'beforeafter']
    rows = b.eval('[...$("#dlens").querySelectorAll(".row2")].map(r => [...r.children].map(c => c.textContent))')
    assert any('w1 = v3 - 1' in x[0] and 'n0 = v3 + 0' in x[1] for x in rows), rows
    b.eval('DV.morphTo = 1; 1')
    b.wait('DV.morph > 0.9', 10000)
    shots = os.environ.get('AIBLOB_SHOTS') or str(tmp_path)
    b.shot(os.path.join(shots, 'diffs-fixture-twins-morphed.png'))
    # the scopes by key: 7 this step, 8 the session, 9 since a step picked on the strip (←/→ move the eye)
    tot = lambda: b.eval('(diffDrill(drill.id) || { tot: null }).tot')
    exp = lambda a, c: {e['key']: e['state'] for e in want(r, a, c, APPB) if e['state'] != 'same'}
    b.eval('closeDrill(); setStep(2); openDrill("app.<module>"); setDiffLook("A"); 1')
    for key, a in (('7', 2), ('8', 0), ('9', None), ('7', 2)):
        b.eval(f'dispatchEvent(new KeyboardEvent("keydown", {{ key: "{key}" }})); 1')
        sp = b.eval('diffSpan(drill.id)')
        if a is not None:
            assert sp == [a, 2], (key, sp)
        b.wait(f'diffRange(drill.id, {sp[0]}, 2).ok', 30000)
        e = exp(sp[0], 2)
        got = b.eval('diffDrill(drill.id) ? [...diffDrill(drill.id).by.keys(), ...diffDrill(drill.id).gone.map(g => g.key)].sort() : []')
        assert got == sorted(e), (key, got, e)
    b.eval('dispatchEvent(new KeyboardEvent("keydown", { key: "9" })); 1')
    assert b.eval('document.activeElement === dstrip')
    pts, n0 = b.eval('[diffStrip(drill.id).map(p => p.n), diffBase(drill.id)]')
    assert pts == [-1, 0, 1] and n0 in pts, (pts, n0)
    for key, k in (('ArrowLeft', max(0, pts.index(n0) - 1)), ('ArrowLeft', max(0, pts.index(n0) - 2)), ('ArrowRight', max(0, pts.index(n0) - 1))):
        b.eval(f'dstrip.dispatchEvent(new KeyboardEvent("keydown", {{ key: "{key}", bubbles: true }})); 1')
        assert b.eval('[cur, diffBase(drill.id)]') == [2, pts[k]], key            # the eye moved, the step did not
    assert b.eval('[DV.scope, diffSpan(drill.id)]') == ['since', [1, 2]]
    frames(b)
    assert 'since you looked' in b.eval('$("#dsum").textContent')
    assert b.errors() == []


@chrome
def test_the_eye_marks_the_step_you_looked_at(b, site):
    """closed at step 3 (no change of app.py there), opened at 5 with 9: the eye is on 3, in order on the strip, the
    span from it lit; ←/→ walk the strip from there; a step older than the strip's own points goes in its place too"""
    load(b, site['url'])
    drill(b, 3, APPB)
    b.eval('closeDrill(); setStep(5); openDrill("app.<module>"); setDiffLook("A"); setDiffScope("step"); 1')
    frames(b)
    b.eval('dispatchEvent(new KeyboardEvent("keydown", { key: "9" })); 1')
    assert b.eval('[DV.scope, diffBase(drill.id), diffSpan(drill.id)]') == ['since', 3, [4, 5]]
    pts = b.eval('diffStrip(drill.id).map(p => p.n)')
    assert pts == sorted(pts) and 3 in pts and 3 not in b.eval('diffStripPts(drill.id).map(p => p.n)'), pts
    frames(b)
    assert b.eval('DV.stripEye') == 3 and 'step 3 → step 6' not in b.eval('$("#dsum").textContent')
    k = pts.index(3)
    for key, want_n in (('ArrowLeft', pts[k - 1]), ('ArrowRight', 3), ('ArrowRight', pts[k + 1])):
        b.eval(f'dstrip.dispatchEvent(new KeyboardEvent("keydown", {{ key: "{key}", bubbles: true }})); 1')
        assert b.eval('[cur, diffBase(drill.id)]') == [5, want_n], key
    b.eval('DV.since.set("orphan:util", 1); closeDrill(); openDrill("orphan:util"); setDiffScope("since"); 1')
    frames(b)
    assert b.eval('[diffStrip(drill.id).map(p => p.n), DV.stripEye]') == [[-1, 0, 1, 3, 4], 1]
    assert b.errors() == []


@chrome
def test_range_keys_clash_with_nothing(b, site):
    """7 / 8 / 9 pick the range inside a drill-down with diffs and do nothing elsewhere; L (legend), S (sessions), T keep
    their meaning everywhere, inside a drill-down with diffs too; 1 / 2 / M only touch the diff in one"""
    key = lambda k: b.eval(f'dispatchEvent(new KeyboardEvent("keydown", {{ key: "{k}" }})); 1')
    legend = lambda: b.eval('$("#legend").classList.contains("on")')
    for src, opened in (('tl.json', False), ('bare.json', True), ('tl.json', True)):
        load(b, site['url'], src=src)
        if opened:
            b.eval(f'setStep(1); openDrill({json.dumps(APPB)}); 1')
            frames(b)
        diffs = opened and src == 'tl.json'
        b.eval('DV.scope = "step"; DV.look = "A"; 1')
        for k, scope in (('8', 'session'), ('9', 'since'), ('7', 'step')):
            key(k)
            assert b.eval('DV.scope') == (scope if diffs else 'step'), (src, opened, k)
        for k in ('t', 'T', 's', 'S'):
            key(k)
        assert b.eval('[DV.scope, cur, !!drill]') == ['step', 1 if opened else b.eval('STEPS.length - 1'), opened], (src, opened)
        assert legend() is False
        key('l')
        assert legend() is True, (src, opened)
        key('L')
        assert legend() is False
        key('2')
        assert b.eval('DV.look') == ('B' if diffs else 'A'), (src, opened)
    assert b.errors() == []


@chrome
def test_a_text_blob_wears_its_marks_on_the_page_edge(b, site, tmp_path):
    load(b, site['url'])
    drill(b, 1, NOTES)
    rec = b.eval('DV.marks.find(m => m.page)')
    assert rec and len(rec['bites']) == 1 and len(rec['hairs']) == 2 and sum(h['big'] for h in rec['hairs']) == 1
    page = b.eval('DV.page')
    assert all(abs(h['x'] - (page['C']['x'] - page['w'])) < 0.5 for h in rec['hairs'] + rec['bites'])      # on the left edge
    f = b.eval('(f => ({ x: f.x, y: f.y }))(DV.feats.find(f => f.kind === "+"))')
    b.eval(f'cv.dispatchEvent(new MouseEvent("mousemove", {{ clientX: {f["x"]}, clientY: {f["y"]}, bubbles: true }})); 1')
    assert 'new f' in b.eval('$("#tip").textContent')
    row = b.eval('DV.rows.find(r => r.row.op === "-")')
    b.eval(f'cv.dispatchEvent(new MouseEvent("click", {{ clientX: {(row["x0"] + row["x1"]) / 2}, clientY: {row["y"]}, bubbles: true }})); 1')
    b.wait('$("#dlens").style.display === "flex"', 5000)
    assert 'three' in b.eval('$("#dlens").textContent') and 'new a' in b.eval('$("#dlens").textContent')
    shots = os.environ.get('AIBLOB_SHOTS') or str(tmp_path)
    b.shot(os.path.join(shots, 'diffs-fixture-page.png'))
    drill(b, 1, NOTES, 'B')
    assert b.eval('DV.page.twins') is True and b.eval('DV.rows.length') == 10 + 15            # before | after: every line of each
    assert b.errors() == []


@chrome
def test_an_opened_cluster_wears_the_diff(b, tmp_path):
    """the clusters view (v6, tests/test_clusters_page.py's engine.c, draw_grid rewritten and model_undo added): the
    overview marks the clusters that changed; an opened one's functions wear Skins, hover and click as elsewhere"""
    from test_clusters_page import engine
    shutil.copy(PAGE, tmp_path / 'index.html')
    big = engine(edit=True).split('\n')                    # step 2: draw_grid gains 403 lines, draw_hud one changed: a scale past 5
    at = big.index('int draw_grid(int x) {') + 2
    big[at:at] = [f'    x += {k} * x % 7;' for k in range(403)]
    big[big.index('int draw_hud(int x) {') + 3] += '  /* hud */'
    snaps = [{'engine.c': engine(), 'notes.txt': 'notes\n'}, {'engine.c': engine(edit=True), 'notes.txt': 'notes\n'}, {'engine.c': '\n'.join(big), 'notes.txt': 'notes\n'}]
    pairs = [(replay.Step(i, '', 'Write', 'engine.c', f'step {i}', 0), s) for i, s in enumerate(snaps)]
    dw = diffs.Static(str(tmp_path / 'tl.diffs'))
    steps = list(timeline.analyzed_steps(iter(pairs), timings=False, diffs=dw))
    dw.close()
    (tmp_path / 'tl.json').write_text(json.dumps({'schema': timeline.SCHEMA, 'session': 't', 'root': '/w', 'steps': timeline.compact(steps)}), encoding='utf-8')
    reader = diffs.load_static(str(tmp_path / 'tl.json'), str(tmp_path / 'tl.diffs'))
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Quiet, directory=str(tmp_path)))
    threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
    try:
        load(b, f'http://127.0.0.1:{srv.server_address[1]}')
        bid = b.eval('STEPS[1].blobs.find(x => x.clusters?.length).id')
        drill(b, 1, bid)
        assert b.eval('drill.cv') is True
        w = {e['key']: hairs_and_bites(e['hunks']) for e in want(reader, 1, 1, bid) if e['state'] != 'same'}
        assert set(w) == {'engine.draw_grid', 'engine.model_undo'}, w
        # the overview: each cluster with a changed function wears that function's hairs and bites on its own rim
        got = b.eval('DV.marks.filter(m => m.q.startsWith("cluster:")).map(m => [CS.cl.list.find(c => "cluster:" + c.id === m.q).ids.map(i => CS.funcs[i].q), m.hairs.length, m.bites.length])')
        assert len(got) == 2 and all([h, bt] == list(w[next(q for q in qs if q in w)]) for qs, h, bt in got), got
        f = b.eval('(f => ({ x: f.x, y: f.y }))(DV.feats.find(f => f.nd.cluster && f.kind === "-"))')
        b.eval(f'cv.dispatchEvent(new MouseEvent("mousemove", {{ clientX: {f["x"]}, clientY: {f["y"]}, bubbles: true }})); 1')
        assert 'draw_grid' in b.eval('$("#tip").textContent') and 'hunk 1' in b.eval('$("#tip").textContent')
        shots = os.environ.get('AIBLOB_SHOTS') or str(tmp_path)
        b.shot(os.path.join(shots, 'diffs-fixture-cluster-overview.png'))
        b.eval(f'cv.dispatchEvent(new MouseEvent("click", {{ clientX: {f["x"]}, clientY: {f["y"]}, bubbles: true }})); 1')
        assert b.eval('[CS.funcs.find(x => x.q === drill.ca.sel)?.n, CS.cl.list.find(c => c.id === drill.ca.open)?.ids.some(i => CS.funcs[i].n === "draw_grid")]') == ['draw_grid', True]
        b.eval('drill.ca.open = null; setDiffLook("B"); 1')
        frames(b)
        assert len(b.eval('DV.marks.filter(m => m.twin && m.q.startsWith("cluster:"))')) == 1        # (draw_grid's kept its size: no ghost)
        b.eval('setDiffLook("A"); 1')
        # honest hairs at a scale past 5: one scale for the overview, said in the legend; each cluster's hairs hold its
        # added lines (a part left over is a thinner hair of its own lines), never rounded up
        drill(b, 2, bid)
        added = {}
        for e in want(reader, 2, 2, bid):
            if e['state'] != 'same':
                added[e['key']] = sum(1 for h in e['hunks'] for op, _ in h['lines'] if op == '+')
        got = b.eval('''(() => { const v = diffDrill(drill.id); return [v.clB, DV.marks.filter(m => m.q.startsWith("cluster:")).map(m => ({
          qs: CS.cl.list.find(c => "cluster:" + c.id === m.q).ids.map(i => CS.funcs[i].q), held: m.hairs.reduce((s, h) => s + h.n, 0),
          parts: m.hairs.filter(h => !h.big && h.n !== v.clB / 5).length, marks: m.hairs.length + m.bites.length }))]; })()''')
        B, cls = got
        assert B == 10 and len(cls) >= 1, got
        for c in cls:
            real = sum(added.get(q, 0) for q in c['qs'])
            assert c['held'] == real and c['marks'] <= 60, (c, real)       # (within one thin hair: here exactly)
        assert any(c['parts'] >= 1 for c in cls), cls          # (403 = 40 x 10 + 2 + 1: a thin hair and a 1-line one)
        frames(b)
        assert 'thick hair = 10 added lines here' in b.eval('$("#dleg").textContent')
        b.eval('setStep(1); 1')
        frames(b)
        assert 'thick hair = 5 added lines' in b.eval('$("#dleg").textContent')
        for q in w:
            b.eval(f'(() => {{ const f = CS.byQ.get({json.dumps(q)}); drill.ca.open = CS.cl.list[CS.cl.of[f.i]].id; drill.ca.t0 = performance.now() - 5000; }})(); 1')
            frames(b)
            got = b.eval(f'DV.marks.filter(m => m.q === {json.dumps(q)}).map(m => [m.hairs.length, m.bites.length])')
            assert got == [list(w[q])], (q, got)
        f = b.eval('(f => ({ x: f.x, y: f.y }))(DV.feats.find(f => f.kind === "+"))')
        b.eval(f'cv.dispatchEvent(new MouseEvent("mousemove", {{ clientX: {f["x"]}, clientY: {f["y"]}, bubbles: true }})); 1')
        assert 'model_undo' in b.eval('$("#tip").textContent')
        b.eval(f'cv.dispatchEvent(new MouseEvent("click", {{ clientX: {f["x"]}, clientY: {f["y"]}, bubbles: true }})); 1')
        b.wait('$("#dbloom").classList.contains("on")', 5000)
        assert 'return model_update(x);' in b.eval('$("#dbloom").textContent')
        shots = os.environ.get('AIBLOB_SHOTS') or str(tmp_path)
        b.shot(os.path.join(shots, 'diffs-fixture-cluster.png'))
        assert b.errors() == []
    finally:
        srv.shutdown()
        srv.server_close()


@chrome
def test_the_overview_merges_hunks_before_raising_the_scale(b, tmp_path):
    """engine.c with each draw_* function edited in three places (a line out, four in): unmerged, the draw cluster needs
    more than CL_MARKS marks at scale 5, merged per function it fits; so the overview stays at 5 and its hairs hold the
    real added lines"""
    from test_clusters_page import GROUPS, engine
    shutil.copy(PAGE, tmp_path / 'index.html')
    src = engine().split('\n')
    for w in GROUPS['draw']:
        at = src.index(f'int draw_{w}(int x) {{') + 2
        for j in (12, 6, 0):
            src[at + j:at + j + 1] = [f'    x += {len(w) * 7 + j * 3 + k} * {k + 1};' for k in range(4)]        # (lines of their own)
    snaps = [{'engine.c': engine(), 'notes.txt': 'notes\n'}, {'engine.c': '\n'.join(src), 'notes.txt': 'notes\n'}]
    pairs = [(replay.Step(i, '', 'Write', 'engine.c', f'step {i}', 0), s) for i, s in enumerate(snaps)]
    dw = diffs.Static(str(tmp_path / 'tl.diffs'))
    steps = list(timeline.analyzed_steps(iter(pairs), timings=False, diffs=dw))
    dw.close()
    (tmp_path / 'tl.json').write_text(json.dumps({'schema': timeline.SCHEMA, 'session': 't', 'root': '/w', 'steps': timeline.compact(steps)}), encoding='utf-8')
    reader = diffs.load_static(str(tmp_path / 'tl.json'), str(tmp_path / 'tl.diffs'))
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Quiet, directory=str(tmp_path)))
    threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
    try:
        load(b, f'http://127.0.0.1:{srv.server_address[1]}')
        bid = b.eval('STEPS[1].blobs.find(x => x.clusters?.length).id')
        drill(b, 1, bid)
        added = {e['key']: sum(1 for h in e['hunks'] for op, _ in h['lines'] if op == '+') for e in want(reader, 1, 1, bid) if e['state'] != 'same'}
        assert sorted(added) == sorted(f'engine.draw_{w}' for w in GROUPS['draw']) and sum(added.values()) == 72, added
        got = b.eval('''(() => { const v = diffDrill(drill.id); return [v.clB, CS.cl.list.map(c => diffClusterPrep(v, c)).filter(Boolean).map(p => [clCost(p.levels[0], 5), clCost(p.levels[1], 5)]),
          DV.marks.filter(m => m.q.startsWith("cluster:")).map(m => ({ qs: CS.cl.list.find(c => "cluster:" + c.id === m.q).ids.map(i => CS.funcs[i].q),
            held: m.hairs.reduce((s, h) => s + h.n, 0), marks: m.hairs.length + m.bites.length }))]; })()''')
        B, costs, cls = got
        assert costs and all(c0 > 60 >= c1 for c0, c1 in costs), costs          # (the case: fits only once merged)
        assert B == 5, got
        assert cls and all(c['held'] == sum(added.get(q, 0) for q in c['qs']) and c['marks'] <= 60 for c in cls), cls
        frames(b)
        assert 'thick hair = 5 added lines' in b.eval('$("#dleg").textContent')
        assert b.errors() == []
    finally:
        srv.shutdown()
        srv.server_close()


# -- the same picture: an unchanged blob's drill-down, and any drill-down of a map without texts, as at HEAD --------
FREEZE = '''(async (step, bid) => { window.requestAnimationFrame = () => 0; await new Promise(r => setTimeout(r, 50));
  setStep(step); openDrill(bid); PULSES.clear(); FPULSES.clear();
  for (let k = 0; k < 160; k++) frame(1e9 + k * 16);
  if (typeof DV !== 'undefined') { for (let k = 0; k < 400 && DV.on && [...DV.ranges.values()].some(r => !r.ok && !r.err); k++) await new Promise(r => setTimeout(r, 25)); }
  for (let k = 160; k < 200; k++) frame(1e9 + k * 16);
  const d = ctx.getImageData(0, 0, cv.width, cv.height).data; let h = 2166136261;
  for (let i = 0; i < d.length; i += 1) h = Math.imul(h ^ d[i], 16777619);
  return [h >>> 0, cv.toDataURL()]; })'''


def picture(b, url, page, src, step, bid):
    load(b, url, page, src)
    return b.eval(f'({FREEZE})({step}, {json.dumps(bid)})')


@chrome
def test_unchanged_drill_downs_are_the_picture_head_draws(b, site, tmp_path):
    """Pixel for pixel HEAD's picture, but for the boxes of the file / text pages of a known format in the dimmed map
    behind the drill-down: they wear their format's badge (SCHEMA.md "v6 additions"), masked in both pictures."""
    if not (site['dir'] / 'head.html').exists():
        pytest.skip('no git HEAD to compare with')
    import base64, io
    from PIL import Image, ImageChops
    url, shots = site['url'], os.environ.get('AIBLOB_SHOTS') or str(tmp_path)
    img = lambda x: Image.open(io.BytesIO(base64.b64decode(x[1].split(',')[1]))).convert('RGB')

    def same(head, new):
        """head == new outside the boxes of the badged pages (read on the new page, which is the one loaded)"""
        boxes = b.eval('''[...VIS.entries()].filter(([id, v]) => PAGE(v.blob.kind) && FMT_BADGE[v.blob.fmt]).map(([id, v]) => {
          const r = v.r * VIEW.s + 3; return [sx(v.x) - r, sy(v.y) - r, sx(v.x) + r, sy(v.y) + r].map(x => Math.round(x * DPR)); })''')
        h, n = img(head), img(new)
        for im in (h, n):
            for box in boxes:
                im.paste((0, 0, 0), tuple(box))
        return h.tobytes() == n.tobytes(), boxes
    for step, bid, src in ((2, UTILB, 'tl.json'), (1, UTILB, 'tl.json'), (0, UTILB, 'bare.json'), (1, APPB, 'bare.json'), (2, APPB, 'bare.json')):
        head = picture(b, url, 'head.html', 'bare.json', step, bid)
        new = picture(b, url, 'index.html', src, step, bid)
        ok, boxes = same(head, new)
        if not ok:
            for n, x in (('head', head), ('new', new)):
                open(os.path.join(shots, f'pixel-{n}-{step}.png'), 'wb').write(base64.b64decode(x[1].split(',')[1]))
        assert ok, (step, bid, src)
        assert len(boxes) == 1        # notes.md: the one page of a known format (the masks hide nothing else)
        if src == 'bare.json':
            assert 'diffs unavailable' in b.eval('$("#dnote").textContent') and b.eval('$("#dlook").hidden') is True
    # (and a changed one is not: the test can see a difference outside the masks)
    head = picture(b, url, 'head.html', 'bare.json', 1, APPB)
    assert not same(head, picture(b, url, 'index.html', 'tl.json', 1, APPB))[0]
    assert b.errors() == []


# -- live: texts from /live/bodies, a provisional step's from the tail ---------------------------------------------
@chrome
def test_live_mode_reads_texts_from_live_bodies(b, tmp_path, monkeypatch):
    import liveserve
    from test_liveserve import Served, _diffs_store
    web = tmp_path / 'web'
    web.mkdir()
    shutil.copy(PAGE, web / 'index.html')
    monkeypatch.setattr(liveserve, 'WEB', str(web))
    labels = tmp_path / 'labels.json'
    shutil.copy(os.path.join(REPO, 'demo', 'labels.json'), labels)
    _, store, _ = _diffs_store(tmp_path)
    s = Served(store, str(labels))
    try:
        b.goto(f'http://127.0.0.1:{s.port}/index.html?live=1&areas=0&follow=0')
        b.wait('typeof BOOTED !== "undefined" && BOOTED && STEPS.length === 5 && DV.on === true', 30000)
        b.eval('window.__fc = 0; (function count() { __fc++; requestAnimationFrame(count); })(); 1')
        assert b.eval('[NCOMMIT, STEPS[3].prov, STEPS.map(s => !!s.fv)]') == [3, True, [True, False, True, False, True]]     # (a code-only step's {} is left out)
        bid = b.eval('STEPS[3].blobs.find(x => x.funcs.some(f => f.name === "g")).id')
        b.eval(f'setStep(3); openDrill({json.dumps(bid)}); 1')
        b.wait(f'diffRange({json.dumps(bid)}, 3, 3).ok', 30000)
        es = b.eval(f'diffRange({json.dumps(bid)}, 3, 3).es.map(e => [e.key, e.state, e.old, e.new])')
        assert sorted(es) == [['m.f', 'changed', 'def f():\n    return 1', 'def f():\n    return 2'], ['m.g', 'new', None, 'def g():\n    return f()']], es
        assert b.eval('performance.getEntriesByType("resource").some(e => e.name.includes("/live/bodies?h="))')
        frames(b)
        assert b.eval('DV.marks.map(m => m.q).sort()') == ['m.f', 'm.g']
        # the provisional step 3 replaced (a tail redone: here step 4's content in its place): its range is made again
        b.eval('liveApply([], [JSON.parse(LTAIL[1])]); 1')
        fresh = b.eval(f'diffEntries({{ steps: STEPS, fvs: diffFvs(), funcsAt: i => IDX[i].funcs }}, 3, 3, {json.dumps(bid)}).map(e => [e.key, e.state])')
        assert sorted(fresh) != sorted([e[:2] for e in es]), fresh
        b.wait(f'diffRange({json.dumps(bid)}, 3, 3).ok', 30000)
        assert b.eval(f'diffRange({json.dumps(bid)}, 3, 3).es.map(e => [e.key, e.state])') == fresh
        assert b.errors() == []
    finally:
        s.close()


@chrome
def test_big_texts_are_diffed_in_a_worker_as_difflib_does(b, site):
    """over DIFF_BIG lines a Web Worker runs the same functions: the hunks are difflib's, and the page did not do it"""
    rnd = random.Random(5)
    old = [rnd.choice(['', '    }', '    x = f(y);', f'    v{rnd.randrange(40)} = {rnd.randrange(9)};']) for _ in range(1600)]
    new = [l + ' // e' if rnd.random() < 0.05 else l for l in old]
    new[700:700] = old[100:150]
    a, c = '\n'.join(old) + '\n', '\n'.join(new) + '\n'
    load(b, site['url'])
    got = b.eval(f'(async () => {{ const t = performance.now(), p = diffHunksOff({json.dumps(a)}, {json.dumps(c)}), mine = performance.now() - t, h = await p;'
                 f' return [!!DW, p instanceof Promise, mine < 50, h.hunks, h.a, h.r]; }})()')
    assert got[:3] == [True, True, True], got[:3]
    assert got[3] == hunks(a, c) and got[4] > 50 and got[5] > 50
    assert b.errors() == []


# -- the page in a real build: the headless page's own fetch / gzip / cache path equals the reference -------------------
@chrome
def test_headless_page_ranges_equal_the_reference(b, site):
    load(b, site['url'])
    r, n = site['reader'], len(site['reader'].steps)
    for a, c in [(x, y) for y in range(n) for x in range(y + 1)]:
        for bid in (APPB, UTILB, NOTES):
            got = b.eval(f'''(async () => {{ const r = diffRange({json.dumps(bid)}, {a}, {c});
              for (let k = 0; k < 400 && !r.ok && !r.err; k++) await new Promise(z => setTimeout(z, 25));
              return r.ok ? r.es.map(e => {{ const o = {{ ...e }}; for (const k of ['a', 'r', 'L', 'Lo', 'missing', 'nd', 'gnd']) delete o[k]; return o; }}) : r.err; }})()''')
            assert got == want(r, a, c, bid), (a, c, bid)
    ix = json.loads(gzip.decompress((site['dir'] / 'tl.diffs' / 'index.json.gz').read_bytes()))
    assert b.eval('[DV.texts.size, DV.chunks.size]') == [sum(map(len, ix['chunks'].values())), len(ix['chunks'])]     # each chunk read once
    assert b.errors() == []
