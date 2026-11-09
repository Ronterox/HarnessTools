"""The page's live mode builds the same steps as a static load, one step at a time (node runs the page's own code):
expander() pushed in chunks, with provisional tails expanded on forks and dropped, equals expandSteps(); and
normalizeStep / stainStep / sessStep carried on step by step (liveStep, tails on forked state) equal normalize()."""
import json, os, re, shutil, subprocess

import pytest

import timeline
import test_func_patch as tf

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
