"""The page's live mode builds the same steps as a static load, one step at a time (node runs the page's own code):
expander() pushed in chunks, with provisional tails expanded on forks and dropped, equals expandSteps(); and
normalizeStep / stainStep / sessStep carried on step by step (liveStep, tails on forked state) equal normalize(). The
labels layer (liveKeys / liveLabels) on the served steps, with the labels events of a cache growing under them, shows what
label.py writes with the final cache (labels, faces, titles, folded-in labels, area captions)."""
import json, os, random, re, shutil, subprocess

import pytest

import liveserve, timeline
import test_func_patch as tf
from livesim import dumps, raw_timeline
from test_liveserve import label_py

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PAGE = os.path.join(REPO, 'web', 'index.html')
AIG = os.path.join(REPO, 'web', 'timeline-aigource.json')

pytestmark = pytest.mark.skipif(not shutil.which('node'), reason='node is not installed: the page code cannot run')


def page_code():
    """The page script up to the layout section, regionOf() and liveStep(), with what they touch of the DOM stubbed."""
    with open(PAGE, encoding='utf-8') as fh:
        html = fh.read()
    script = html[html.index('<script>') + len('<script>'):html.index('</script>')]
    head = script[:script.index('/* ───────────────────────── stable layout')]
    region = re.search(r'^function regionOf\(b\) \{.*?^\}\n', script, re.S | re.M).group(0)
    live = re.search(r'^function liveStep\(.*?^\}\n', script, re.S | re.M).group(0)
    stubs = ("const document = { querySelector: () => null, body: { classList: { toggle() {} } }, documentElement: {} };"
             "const matchMedia = () => ({ matches: false, addEventListener() {} });")
    return stubs + head + region + live


# chunk sizes and tail lengths cycled over the steps: the page gets steps a few at a time, each time with a tail
CHUNKS, TAILS = [1, 3, 1, 7, 2, 30, 1], [0, 2, 5, 1, 0, 3]

DRIVER = r"""
const fs = require('fs'), tl = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const CHUNKS = @CHUNKS@, TAILS = @TAILS@;
// deep equality that remembers the pairs it checked (steps share their blob objects)
const seen = new WeakMap();
function eq(a, b, path) {
  if (a === b) return true;
  if (typeof a !== typeof b || a === null || b === null || typeof a !== 'object') throw new Error(`${path}: ${JSON.stringify(a)?.slice(0, 80)} != ${JSON.stringify(b)?.slice(0, 80)}`);
  if (seen.get(a) === b) return true;
  if (a instanceof Map || a instanceof Set) { if (a.size !== b.size) throw new Error(`${path}: size`); eq([...a], [...b], path); }
  else if (Array.isArray(a)) { if (!Array.isArray(b) || a.length !== b.length) throw new Error(`${path}: length ${a.length} != ${b.length}`); a.forEach((x, i) => eq(x, b[i], path + '[' + i + ']')); }
  else { const ka = Object.keys(a), kb = Object.keys(b); if (ka.join() !== kb.join()) throw new Error(`${path}: keys ${ka} != ${kb}`); for (const k of ka) eq(a[k], b[k], path + '.' + k); }
  seen.set(a, b); return true;
}
const raw = () => JSON.parse(JSON.stringify(tl.steps));          // the steps as each path gets them, unshared
// 1. expander() in chunks, tails on forks, against expandSteps()
const want = expandSteps({ ...tl, steps: raw() }), steps = raw(), ex = expander(tl), got = [];
let forks = 0;
for (let i = 0, k = 0; i < steps.length; k++) {
  const n = CHUNKS[k % CHUNKS.length], t = TAILS[k % TAILS.length];
  for (const st of steps.slice(i, i + n)) got.push(ex.push(st));
  i += n;
  const f = ex.fork(), tail = steps.slice(i, i + t).map(st => f.push(JSON.parse(JSON.stringify(st))));
  tail.forEach((st, j) => eq(st, want[i + j], `tail at ${i} step ${i + j}`)); forks += tail.length;
}
eq(got, want, 'steps');
// 2. normalize() against liveStep() step by step, tails on forked state (dropped, as the page drops them)
const digest = () => ({ steps: STEPS.map((s, i) => ({ sci: s.sci, tint: s.tint, stains: [...s.stains], kids: [...s.kids],
  spokes: s.spokes.size, dataNodes: s.dataNodes, idx: [IDX[i].blobs.size, IDX[i].funcs.size, IDX[i].owner.size],
  blobs: s.blobs.map(b => [b.id, b.region, b.face, b.title, b.size]), edges: s.edges.length, areas: s.areas })),
  sess: [...SESS.values()], gits: GITS, bounds: BOUNDS, multi: MULTI, on: SESS_ON });
normalize({ ...tl, steps: raw() });
const a = digest();
STEPS = []; IDX.length = 0;
const lsteps = raw(), lex = expander(tl), stains = new Map(), ss = sessIndex();
for (let i = 0, k = 0; i < lsteps.length; k++) {
  const n = CHUNKS[k % CHUNKS.length], t = TAILS[k % TAILS.length];
  STEPS.length = IDX.length = i;
  for (const st of lsteps.slice(i, i + n)) liveStep(lex, stains, ss, st, false);
  i += n;
  const f = lex.fork(), fs2 = new Map(stains), fss = sessFork(ss);
  for (const st of lsteps.slice(i, i + t)) liveStep(f, fs2, fss, JSON.parse(JSON.stringify(st)), true);
  useSessions(fss);
}
STEPS.length = IDX.length = lsteps.length;
useSessions(ss);
const b = digest();
eq(b, a, 'normalized');
console.log(JSON.stringify({ steps: got.length, forks }));
"""


def run_node(path):
    js = page_code() + DRIVER.replace('@CHUNKS@', json.dumps(CHUNKS)).replace('@TAILS@', json.dumps(TAILS))
    r = subprocess.run(['node', '--max-old-space-size=2048', '-e', js, path], capture_output=True, text=True,
                       timeout=600)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout)


CASES = ['fixture', 'fixture5', 'timeline.json', 'sample-timeline.json'] + (['timeline-aigource.json']
                                                                            if os.path.exists(AIG) else [])


@pytest.mark.parametrize('name', CASES)
def test_live_steps_equal_static_load(name, tmp_path):
    if name in ('fixture', 'fixture5'):
        steps = tf.fixture() if name == 'fixture' else tf.fixture5()
        path = tmp_path / f'{name}.json'
        path.write_text(json.dumps({'schema': 5, 'steps': timeline.compact(steps)}), encoding='utf-8')
        path = str(path)
    else:
        path = os.path.join(REPO, 'web', name)
    with open(path, encoding='utf-8') as fh:
        n = len(json.load(fh)['steps'])
    out = run_node(path)
    assert out['steps'] == n and out['forks'] > 0


def layer_code():
    """page_code() and the live section's labels layer."""
    with open(PAGE, encoding='utf-8') as fh:
        html = fh.read()
    script = html[html.index('<script>') + len('<script>'):html.index('</script>')]
    lets = re.search(r'^let NCOMMIT = 0.*?\n(?=function liveStart)', script, re.S | re.M).group(0)
    layer = re.search(r'^const sameLabel = .*?(?=^function liveAsk)', script, re.S | re.M).group(0)
    return page_code() + lets + layer


LAYER = r"""
const fs = require('fs'), tl = JSON.parse(fs.readFileSync(process.argv[1], 'utf8')), want = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
let evs = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const TAIL = +process.argv[4], LATE = process.argv[5] === 'late';
// the page: the timeline as loaded (its layer), then steps and labels events as they come; the last TAIL steps
// provisional. LATE: a page loading once it is all over (every change in the layer it loads, no event)
OV = new Map(Object.entries(tl.live.labels)); LKS = lkState(); LIDX = new Map(); STEPS = []; IDX.length = 0;
if (LATE) { for (const e of evs) for (const k in e.labels) OV.set(k, e.labels[k]); evs = []; }
const ex = expander(tl), stains = new Map(), ss = sessIndex(), steps = tl.steps, n = steps.length - TAIL;
for (let i = 0; i < n; i++) {
  liveStep(ex, stains, ss, steps[i], false, LKS); NCOMMIT = STEPS.length;
  for (const e of evs) if (e.at === i) liveLabels(e.labels);
}
const f = ex.fork(), ks = lkFork(LKS);
for (const st of steps.slice(n)) liveStep(f, new Map(stains), sessFork(ss), st, true, ks);
for (const e of evs) if (e.at >= n) liveLabels(e.labels);
// (an unlabelled cut piece borrows its parent's face; on a blob object shared by several steps the last step's parent
// wins, so which one depends on how the steps share objects, which a labels layer changes: not compared)
const digest = () => STEPS.map(s => ({ blobs: s.blobs.map(b => [b.id, b.label || null, b.cut && !b.label ? null : b.face, b.title, (b.merged || []).map(m => m.label || null)]),
  areas: s.areas.map(a => [a.id, a.caption, a.faces]) }));
const got = digest(); let relabelled = 0;
for (const s of STEPS) for (const b of s.blobs) if (b.label !== b.lb) relabelled++;
normalize(want);
const exp = digest();
for (let i = 0; i < exp.length; i++) { const a = JSON.stringify(got[i]), b = JSON.stringify(exp[i]); if (a !== b) {
  const ga = got[i], ea = exp[i]; let what = '';
  for (let j = 0; j < ea.blobs.length; j++) if (JSON.stringify(ga.blobs[j]) !== JSON.stringify(ea.blobs[j])) { what = JSON.stringify(ga.blobs[j]) + ' != ' + JSON.stringify(ea.blobs[j]); break; }
  if (!what) what = JSON.stringify(ga.areas) + ' != ' + JSON.stringify(ea.areas);
  throw new Error(`step ${i}: ${what.slice(0, 600)}`); } }
if (got.length !== exp.length) throw new Error('length');
console.log(JSON.stringify({ steps: got.length, relabelled }));
"""


def served_with_events(head, lines, caches, cut):
    """liveserve's Labeller as the server runs it: steps[:cut] labelled with caches[0] (what the page loads, with its
    layer), the cache changed to caches[1] (a labels event), the other steps labelled (their back-fill: an event), the
    cache changed to caches[2] (an event). -> (served timeline, [{at, labels}])."""
    lab = liveserve.Labeller(head['root'], caches[0], 0.2, head['schema'])
    out = [json.loads(dumps(lab.push(json.loads(l), 0.0))) for l in lines[:cut]]
    tl = {'root': head['root'], 'schema': head['schema'], 'live': {'labels': lab.take_overlay()}}
    evs = [{'at': cut - 1, 'labels': lab.recache(caches[1], lines[:cut])}]
    out += [json.loads(dumps(lab.push(json.loads(l), 0.0))) for l in lines[cut:]]
    evs.append({'at': len(lines) - 1, 'labels': lab.take_overlay()})
    evs.append({'at': len(lines) - 1, 'labels': lab.recache(caches[2], lines)})
    tl['steps'] = out
    return tl, evs


LAYER_CASES = [('timeline.json', 'labels.json')] + ([('timeline-aigource.json', 'labels.aigource.json')]
                                                    if os.path.exists(AIG) else [])


@pytest.mark.parametrize('name,labels', LAYER_CASES, ids=[n for n, _ in LAYER_CASES])
def test_labels_layer_shows_label_py_with_the_final_cache(name, labels, tmp_path):
    head, lines = raw_timeline(os.path.join(REPO, 'web', name))
    with open(os.path.join(REPO, 'demo', labels), encoding='utf-8') as fh:
        full = json.load(fh)
    rnd = random.Random(4)
    keys = sorted(full)
    final = {k: full[k] for k in keys if rnd.random() < 0.6}
    gone = next(k for k in keys if k not in final)       # a label that comes, then goes again
    mid = dict({k: v for k, v in final.items() if rnd.random() < 0.5}, **{gone: full[gone]})
    first = {k: v for k, v in mid.items() if rnd.random() < 0.4 and k != gone}
    renamed = next(k for k in sorted(first) if k in final)
    final[renamed] = dict(final[renamed], title='Renamed later')
    tl, evs = served_with_events(head, lines, [first, mid, final], len(lines) * 3 // 5)
    assert any(e['labels'] for e in evs)
    c = tmp_path / 'final.json'
    c.write_text(json.dumps(final), encoding='utf-8')
    want, _ = label_py(tmp_path, head, lines, str(c))
    paths = []
    for nm, obj in (('served', tl), ('want', {'root': head['root'], 'schema': head['schema'], 'steps': want}), ('evs', evs)):
        paths.append(tmp_path / f'{nm}.json')
        paths[-1].write_text(json.dumps(obj, ensure_ascii=False), encoding='utf-8')
    for tail, late in ((0, ''), (3, ''), (3, 'late')):
        r = subprocess.run(['node', '--max-old-space-size=2048', '-e', layer_code() + LAYER] + [str(p) for p in paths]
                           + [str(tail), late], capture_output=True, text=True, timeout=600)
        assert r.returncode == 0, r.stderr[-3000:]
        out = json.loads(r.stdout)
        assert out['steps'] == len(lines) and out['relabelled'] > 0


CAPTIONS = r"""
const fs = require('fs'), cases = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
console.log(JSON.stringify(cases.map(c => captionTable(c.blobs, c.ids))));
"""


def test_caption_table_is_areas_caption_table(tmp_path):
    # the page's port of areas.caption_table(): titles of generic and short words, ties of score and of faces (an
    # emoji above U+FFFF against one below it in UTF-16), cut pieces taking their parent's emoji, FIXED areas
    import areas
    rnd = random.Random(9)
    words = ['login', 'Note', 'notes', 'share', 'ui', 'UI', 'the', 'data', 'db', 'x', 'token', 'user', 'Users', 'page',
             'handler', 'auth', 'é', 'İstanbul', 'map', 'Map']
    emojis = ['🔑', '📝', '\uff01', '⏱️', '🗄️', 'a', '\ue000', '😀']
    cases = []
    for n in range(300):
        blobs = []
        for k in range(rnd.randint(1, 9)):
            b = {'id': f'b{k}', 'name': ' '.join(rnd.choice(words) for _ in range(rnd.randint(1, 3))),
                 'size': rnd.choice([1, 2, 4, 9, 16]), 'area': rnd.choice(['a', 'b', 'tests', 'files', 'data', 'docs'])}
            if rnd.random() < 0.6:
                b['label'] = {'emoji': rnd.choice(emojis), 'title': ' '.join(rnd.choice(words) for _ in range(rnd.randint(1, 4)))}
            if rnd.random() < 0.3:
                b['emoji'] = rnd.choice(emojis)
            if k and rnd.random() < 0.2:
                b['cut'] = 'b0'
            blobs.append(b)
        cases.append({'blobs': blobs, 'ids': sorted({b['area'] for b in blobs} | {'z'})})
    path = tmp_path / 'cases.json'
    path.write_text(json.dumps(cases), encoding='utf-8')
    r = subprocess.run(['node', '-e', page_code() + CAPTIONS, str(path)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    assert json.loads(r.stdout) == [areas.caption_table(c['blobs'], c['ids']) for c in cases]
