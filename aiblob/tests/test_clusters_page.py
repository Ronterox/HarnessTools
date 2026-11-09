"""The clusters view (schema v6) in headless Chrome: a C file of five call groups, built by timeline.py, then edited.
The drill-down of its big blob shows the clusters, the step's changed functions pulse in their cluster, labels never
overlap; a click opens a cluster, a cluster pill hops to another, / finds a function, Esc / Backspace go back up;
no console errors. Screenshots go to $AIBLOB_SHOTS (default: the test's tmp dir)."""
import functools, http.server, json, os, shutil, threading

import pytest

import replay, timeline

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(os.path.dirname(HERE), 'web')

browser = pytest.importorskip('browser')
pytestmark = pytest.mark.skipif(not (browser.CHROME and shutil.which('node')), reason='needs Chrome and node')

GROUPS = {'draw': ['scene', 'hud', 'grid', 'text', 'box', 'line'], 'model': ['apply', 'update', 'free', 'load', 'save'],
          'input': ['handle', 'key', 'mouse', 'scroll'], 'file': ['open', 'read', 'close', 'watch'],
          'tl': ['seek', 'step', 'play', 'draw']}


def engine(edit=False):
    """engine.c: five groups of functions calling each other inside the group, their heads called by both tick and
    main_loop (so no piece is cut out); 25 functions, 560 lines. edit: draw_grid rewritten, model_undo added."""
    out = ['#include <stdio.h>', '']
    for g, ws in GROUPS.items():
        for i, w in enumerate(ws):
            body = [f'    x = x * {i + 3} + {len(w) + (5 if edit and g + w == "drawgrid" else 0)};' for _ in range(18)]
            calls = ([f'    x += {g}_{ws[(i + 1) % len(ws)]}(x - 1);', f'    x += {g}_{ws[(i + 2) % len(ws)]}(x - 2);'] if i
                     else [f'    x += {g}_{ws[1]}(x);'])
            out += [f'int {g}_{w}(int x) {{', '    if (x <= 0) return 0;', *body, *calls, '    return x;', '}', '']
    if edit:
        out += ['int model_undo(int x) {', '    x += model_apply(x);', '    return model_update(x);', '}', '']
    heads = [f'{g}_{ws[0]}' for g, ws in GROUPS.items()]
    for name in ('tick', 'main_loop'):
        out += [f'int {name}(int x) {{', *[f'    x += {h}(x);' for h in heads], '    return x;', '}', '']
    out += ['int main(void) {', '    printf("%d\\n", main_loop(3) + tick(4));', '    return 0;', '}', '']
    return '\n'.join(out)


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture(scope='module')
def url(tmp_path_factory):
    d = tmp_path_factory.mktemp('clpage')
    shutil.copy(os.path.join(WEB, 'index.html'), d / 'index.html')
    snaps = [('engine.c', {'engine.c': engine(), 'notes.txt': 'notes\n'}), ('engine.c', {'engine.c': engine(edit=True), 'notes.txt': 'notes\n'})]
    pairs = [(replay.Step(i, '', 'Write', f, f'step {i}', 0), snap) for i, (f, snap) in enumerate(snaps)]
    steps = list(timeline.analyzed_steps(iter(pairs), timings=False))
    (d / 'tl.json').write_text(json.dumps({'schema': timeline.SCHEMA, 'session': 't', 'root': '/work/engine',
                                           'steps': timeline.compact(steps)}), encoding='utf-8')
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Quiet, directory=str(d)))
    threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
    yield f'http://127.0.0.1:{srv.server_address[1]}/index.html?src=tl.json&areas=0&follow=0'
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def b():
    br = browser.Browser()
    yield br
    br.close()


def frames(b, n=3):
    b.eval('window.__fc ??= 0; (function count() { __fc++; requestAnimationFrame(count); })(); 1')
    k = b.eval('__fc')
    b.wait(f'__fc >= {k + n}', 30000)


# label boxes drawn this frame (captions, pills, function names, the opened cluster's header) that overlap
OVERLAPS = '''(() => { const bx = drill.ca.boxes, out = [];
  for (let i = 0; i < bx.length; i++) for (let j = i + 1; j < bx.length; j++) { const p = bx[i], q = bx[j];
    if (p.x0 < q.x1 - 0.5 && q.x0 < p.x1 - 0.5 && p.y0 < q.y1 - 0.5 && q.y0 < p.y1 - 0.5) out.push([i, j]); }
  return out; })()'''
CLICK = '''((x, y) => { cv.dispatchEvent(new MouseEvent('mousemove', { clientX: x, clientY: y, bubbles: true }));
  cv.dispatchEvent(new MouseEvent('click', { clientX: x, clientY: y, bubbles: true })); return 1; })'''
KEY = '''((k, el) => { (el ? document.querySelector(el) : window).dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true })); return 1; })'''


def shot(b, tmp_path, name):
    out = os.environ.get('AIBLOB_SHOTS') or str(tmp_path)
    os.makedirs(out, exist_ok=True)
    b.shot(os.path.join(out, f'clusters-{name}.png'))


def test_drill_into_clusters_open_one_and_come_back(b, url, tmp_path):
    b.goto(url)
    b.wait('typeof BOOTED !== "undefined" && BOOTED && STEPS.length === 2', 60000)
    big = b.eval('STEPS[1].blobs.find(b => b.clusters).id')
    assert b.eval('STEPS[1].blobs.filter(b => b.clusters).length') == 1
    b.eval(f'setStep(0); PULSES.clear(); FPULSES.clear(); setStep(1); for (const p of FPULSES.values()) p.end = Infinity; openDrill({json.dumps(big)}); drill.t0 -= 1000; 1')
    b.wait('drill && drill.cv && CS && CS.cur === 1')
    frames(b)
    assert not b.eval('$("#clsearch").hidden')
    caps = b.eval('CS.cl.list.map(c => c.cap)')
    assert {'draw', 'model', 'input', 'file', 'timeline'} <= set(caps), caps
    # this step changed draw_grid and created model_undo: both pulse, and so do their clusters; others stay still
    lit = b.eval('CS.cl.list.filter(c => clPulse(c, performance.now())).map(c => [c.cap, clPulse(c, performance.now()).kind])')
    assert sorted(lit) == [['draw', 'mod'], ['model', 'new']], lit
    assert b.eval('drill.ca.boxes.length') >= 5 and b.eval(OVERLAPS) == []
    assert b.eval('CS.cl.list.every(c => c.capShown)')
    shot(b, tmp_path, 'overview')
    # the legend and the search box share the top right corner: the box gives way while the legend is open
    b.eval('$("#legendBtn").click(); 1')
    frames(b)
    assert b.eval('[$("#legend").classList.contains("on"), $("#clsearch").hidden]') == [True, True]
    shot(b, tmp_path, 'legend')
    b.eval('$("#legendBtn").click(); 1')
    frames(b)
    assert b.eval('$("#clsearch").hidden') is False
    # open the model cluster with a click on its emoji
    x, y = b.eval('(() => { const h = drill.ca.hits.find(h => h.cluster && h.c.cap === "model"); return [h.x, h.y]; })()')
    b.eval(f'({CLICK})({x}, {y})')
    assert b.eval('CS.cl.list.find(c => c.id === drill.ca.open)?.cap') == 'model'
    b.eval('drill.ca.t0 -= 1000; 1')
    frames(b)
    assert b.eval(OVERLAPS) == []
    assert b.eval('drill.ca.hits.filter(h => h.f).map(h => h.f.n).sort()') == sorted(
        ['model_apply', 'model_update', 'model_free', 'model_load', 'model_save', 'model_undo'])
    shot(b, tmp_path, 'open')
    # a cluster pill on the rim (the clusters this one calls or is called by) hops there
    pill = b.eval('(() => { const h = drill.ca.hits.find(h => h.pill && h.pill.ci != null); return h && [h.pill.t, (h.x0 + h.x1) / 2, (h.y0 + h.y1) / 2]; })()')
    assert pill, 'no cluster pill on the rim'
    b.eval(f'({CLICK})({pill[1]}, {pill[2]})')
    assert b.eval('CS.cl.list.find(c => c.id === drill.ca.open)?.cap') == pill[0]
    # Esc: back to the overview; Backspace: back to the map
    b.eval(f'({KEY})("Escape")')
    assert b.eval('drill && drill.ca.open') is None
    b.eval(f'({KEY})("Backspace")')
    assert b.eval('drill') is None
    frames(b)
    assert b.eval('$("#clsearch").hidden')
    # / finds a function: Enter opens its cluster with it pinned
    b.eval(f'openDrill({json.dumps(big)}); 1')
    frames(b)
    b.eval(f'({KEY})("/")')
    assert b.eval('document.activeElement.id') == 'clq'
    b.eval('$("#clq").value = "grid"; $("#clq").dispatchEvent(new Event("input")); 1')
    assert b.eval('[...CQ.match]') == ['engine.draw_grid']
    b.eval(f'({KEY})("Enter", "#clq")')
    assert b.eval('[CS.cl.list.find(c => c.id === drill.ca.open)?.cap, drill.ca.sel]') == ['draw', 'engine.draw_grid']
    b.eval('drill.ca.t0 -= 1000; 1')
    frames(b)
    assert b.eval(OVERLAPS) == []
    shot(b, tmp_path, 'found')
    # a page's card is placed on the frame the drill closes and the search box hides, as on any other frame
    got = b.eval('''(() => { const id = STEPS[cur].blobs.find(b => PAGE(b.kind)).id; closeDrill(); showFileCard(id); const card = $('#card');
      card.style.left = '-999px'; frame(performance.now()); return [card.style.left, $('#clsearch').hidden, fileCard === id]; })()''')
    assert got[0] != '-999px' and got[1:] == [True, True], got
    assert b.errors() == []
