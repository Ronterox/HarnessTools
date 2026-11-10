"""File formats on the page (web/index.html, SCHEMA.md "v6 additions"): every `fmt` has its badge, drawn on the page
body; an image / svg blob shows its thumbnail (diffs sidecar `t`: static from <map>.diffs/, live from `fv` and
/live/bodies), drawn only through an Image on the canvas, so an svg's <script> never runs; without the sidecar the badge
alone; an image page has a readable size; the drill-down of a code blob is the picture the page at HEAD draws, pixel
for pixel; no console errors."""
import functools, http.server, io, json, os, shutil, subprocess, threading

import pytest

import diffs, replay, thumbs, timeline
from analyze import fmt_of

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PAGE = os.environ.get('AIBLOB_PAGE') or os.path.join(REPO, 'web', 'index.html')
browser = pytest.importorskip('browser')
pytest.importorskip('PIL')
chrome = pytest.mark.skipif(not (browser.CHROME and shutil.which('node')), reason='needs Chrome and node')
from test_diffs_page import APPB, FREEZE, UTILB, Quiet, fixture_build, frames, load      # noqa: E402
from test_replay import msg, result, tool, ts, write_lines                                 # noqa: E402

SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="30"><rect width="40" height="30" fill="#00f"/>'
       '<script>window.pwned = 1; document.title = "pwned"</script></svg>\n')
# The page before format badges and thumbnails (7d8f374 added them): what code blobs, the scale and the layout must
# still look like. Pinned, not HEAD: from 7d8f374 on HEAD is the badge page itself.
BASE = '2fd80cf'
ONE_PER_FMT = {'data.json': '{}\n', 'rows.jsonl': '{}\n', 't.csv': 'a\n', 't.tsv': 'a\n', 'a.xml': '<a/>\n',
               'c.toml': 'a = 1\n', 'c.yaml': 'a: 1\n', 'c.ini': '[a]\n', 'README.md': '# r\n', 'doc.html': '<p>\n',
               'site.css': 'a {}\n', 'f.woff2': '\x00w', '.gitignore': 'x\n', 'Makefile': 'all:\n',
               'Dockerfile': 'FROM x\n', '.env': 'A=1\n', '.editorconfig': 'root = true\n', '.gitattributes': '*.png binary\n',
               'LICENSE': 'MIT\n', 'requirements.txt': 'flask\n', 'uv.lock': 'x\n', 'CMakeLists.txt': 'project(x)\n',
               'Procfile': 'web: x\n'}


def _png(color, size=(120, 80)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', size, color).save(buf, 'PNG')
    return buf.getvalue()


def _repo(tmp_path):
    """A repo with a png, an svg with a <script> and one file per other fmt; a session edits app.py twice."""
    repo = tmp_path / 'repo'
    repo.mkdir()
    for p, text in ONE_PER_FMT.items():
        (repo / p).write_bytes(text.encode())
    (repo / 'shot.png').write_bytes(_png('red'))
    (repo / 'logo.svg').write_text(SVG)
    (repo / 'app.py').write_text('print(1)\n')
    env = {'GIT_AUTHOR_DATE': ts(0), 'GIT_COMMITTER_DATE': ts(0), 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t', 'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'}
    for cmd in (['init', '-q'], ['add', '-A'], ['commit', '-qm', 'base']):
        subprocess.run(['git', '-C', str(repo), *cmd], check=True, env=env, capture_output=True)
    root, src = str(repo), tmp_path / 'src'
    write_lines(src / 'aaaaaaaa-1111-2222-3333-444444444444.jsonl', [
        msg('user', 'go', ts=ts(1)),
        msg('assistant', [tool('e1', 'Edit', file_path=f'{root}/app.py', old_string='1', new_string='2')], ts=ts(2), cwd=root),
        dict(result('e1'), timestamp=ts(2, 1)),
        msg('assistant', [tool('e2', 'Edit', file_path=f'{root}/app.py', old_string='2', new_string='3')], ts=ts(3), cwd=root),
        dict(result('e2'), timestamp=ts(3, 1))])
    return root, str(src)


@pytest.fixture(scope='module')
def site(tmp_path_factory):
    """Served: the page, the formats map with its sidecar (fx.json, fx.diffs) and without (bare.json); the diffs page
    fixture's map (tl.json, tl.diffs) and the page before the badges (head.html, BASE) for the pixel comparison."""
    d = tmp_path_factory.mktemp('fmtpage')
    shutil.copy(PAGE, d / 'index.html')
    head = subprocess.run(['git', 'show', f'{BASE}:aiblob/web/index.html'], cwd=REPO, capture_output=True)
    if head.returncode == 0:
        (d / 'head.html').write_bytes(head.stdout)
    root, src = _repo(d)
    rs = replay.steps(replay.transcripts(src), root, seed_git=True)
    dw = diffs.Static(str(d / 'fx.diffs'))
    dw.sink.images = thumbs.images_for(root)
    tl = timeline.timeline(src, root, timings=False, steps=rs, diffs=dw)
    dw.close()
    (d / 'fx.json').write_text(json.dumps(tl), encoding='utf-8')
    shutil.copy(d / 'fx.json', d / 'bare.json')
    fixture_build(d)
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Quiet, directory=str(d)))
    threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
    yield {'url': f'http://127.0.0.1:{srv.server_address[1]}', 'dir': d, 'root': root}
    srv.shutdown()
    srv.server_close()


def picture(b, url, page, src, step, bid):
    """test_diffs_page.picture(), but waiting for the diffs index on any page (that one waits only on index.html):
    the base page's drill-down is then drawn with its diff marks too, not in the moment before they load."""
    load(b, url, page, src)
    b.wait('DV.on !== null', 30000)
    return b.eval(f'({FREEZE})({step}, {json.dumps(bid)})')


@pytest.fixture
def b():
    br = browser.Browser()
    yield br
    br.close()


THUMBS_IN = '''(() => { const out = {}; for (const x of IDX[cur].blobs.values()) if (x.fmt === 'image' || x.fmt === 'svg') {
  const im = thumbFor(x); out[x.path] = im ? [im.naturalWidth, im.naturalHeight] : null; } return out; })()'''


def _thumbs(b):
    b.eval(THUMBS_IN)                   # asks for them (lazily), then waits until both came in
    b.wait('Object.values(' + THUMBS_IN + ').every(v => v)', 30000)
    return b.eval(THUMBS_IN)


@chrome
def test_every_fmt_has_a_badge_and_it_renders(b, site):
    load(b, site['url'], src='fx.json')
    fmts = set(b.eval('Object.keys(FMT_BADGE)'))
    have = {fmt_of(p) for p in list(ONE_PER_FMT) + ['shot.png', 'logo.svg']}
    assert have <= fmts and have == set(b.eval('[...new Set([...IDX[cur].blobs.values()].map(x => x.fmt).filter(Boolean))]'))
    # each badge, drawn alone at a mid size, is ink on its own colour, and no two look the same
    got = b.eval('''Object.keys(FMT_BADGE).map(f => { const c = document.createElement('canvas'); c.width = c.height = 48;
      const g = c.getContext('2d'); drawFmtBadge(g, f, 24, 24, 40); const d = g.getImageData(0, 0, 48, 48).data;
      let h = 2166136261, ink = 0; for (let i = 0; i < d.length; i++) h = Math.imul(h ^ d[i], 16777619);
      for (let i = 0; i < d.length; i += 4) ink += d[i] > 240 && d[i + 1] > 240 && d[i + 2] > 240 && d[i + 3] > 240;
      return [f, h >>> 0, ink]; })''')
    assert all(ink > 15 for _, _, ink in got), got
    assert len({h for _, h, _ in got}) == len(got)
    # the areas: assets / data / docs / files with their captions and hues
    areas = dict(b.eval('STEPS[cur].areas.map(a => [a.id, a.caption])'))
    assert {k: v for k, v in areas.items() if not k[1:].isdigit()} == {'assets': 'Assets', 'data': 'Data',
                                                                          'docs': 'Docs', 'files': 'Files'}
    assert b.eval('["assets", "data", "docs", "files"].map(a => FIXED_HUE[a])') == [320, 168, 262, 210]
    frames(b)
    assert b.errors() == []


@chrome
def test_image_pages_have_a_readable_size(b, site):
    load(b, site['url'], src='fx.json')
    frames(b)
    b.wait('Math.abs(VIS.get("file:shot.png").r - radius(IMG_SIZE)) < 0.05', 30000)       # (radii ease in)
    r = b.eval('["file:shot.png", "file:logo.svg", "file:c.ini"].map(id => VIS.get(id).r)')
    assert abs(r[0] - r[1]) < 0.05 and r[0] > 3 * r[2], r


@chrome
def test_thumbnails_from_the_sidecar_and_the_svg_script_never_runs(b, site):
    load(b, site['url'], src='fx.json')
    assert _thumbs(b) == {'shot.png': [64, 43], 'logo.svg': [40, 30]}
    assert b.eval('performance.getEntriesByType("resource").some(e => e.name.includes("fx.diffs/s/"))')
    frames(b, 5)
    assert b.eval('typeof window.pwned') == 'undefined' and b.eval('document.title') != 'pwned'
    assert b.eval('document.querySelectorAll("svg script, foreignObject").length') == 0
    assert 'window.pwned' not in b.eval('document.documentElement.outerHTML')
    # the thumbnail is on the canvas, inside the page: the svg's blue at the page's centre
    px = b.eval('''(() => { const v = VIS.get('file:logo.svg');
      const d = ctx.getImageData(Math.round(sx(v.x) * DPR), Math.round(sy(v.y) * DPR), 1, 1).data; return [...d]; })()''')
    assert px[2] > 150 and px[0] < 100, px
    assert b.errors() == []


@chrome
def test_without_the_sidecar_the_badge_alone(b, site):
    load(b, site['url'], src='bare.json')
    assert b.eval('DV.on') is False
    frames(b, 5)
    assert b.eval(THUMBS_IN) == {'shot.png': None, 'logo.svg': None}
    assert not b.eval('performance.getEntriesByType("resource").some(e => e.name.includes("bare.diffs/s/"))')
    assert b.errors() == []


@chrome
def test_code_drill_downs_are_the_picture_head_draws(b, site):
    """The drill-down of a code blob is the pre-badge page's picture (BASE) pixel for pixel, but for the dimmed map behind it where a page
    of a known format (here notes.md) wears its badge instead of ruled lines: only that page's box differs."""
    if not (site['dir'] / 'head.html').exists():
        pytest.skip('no git HEAD to compare with')
    import base64
    from PIL import Image, ImageChops
    url = site['url']
    img = lambda x: Image.open(io.BytesIO(base64.b64decode(x[1].split(',')[1]))).convert('RGB')
    for step, bid in ((2, UTILB), (1, APPB), (5, APPB)):
        head = img(picture(b, url, 'head.html', 'tl.json', step, bid))
        new = img(picture(b, url, 'index.html', 'tl.json', step, bid))
        x0, y0, x1, y1 = b.eval('''(() => { const v = VIS.get('file:notes.md'), r = v.r * VIEW.s;
          return [sx(v.x) - r, sy(v.y) - r, sx(v.x) + r, sy(v.y) + r].map(x => Math.round(x * DPR)); })()''')
        diff = ImageChops.difference(head, new).getbbox()
        assert diff is not None and x0 <= diff[0] and y0 <= diff[1] and diff[2] <= x1 and diff[3] <= y1, (step, bid, diff)
        for im in (head, new):
            im.paste((0, 0, 0), (x0, y0, x1, y1))
        assert head.tobytes() == new.tobytes(), (step, bid)
    assert b.errors() == []


@chrome
def test_live_thumbnails_from_live_bodies(b, tmp_path, monkeypatch):
    import live, liveserve
    from test_live import sec
    from test_liveserve import Served
    web = tmp_path / 'web'
    web.mkdir()
    shutil.copy(PAGE, web / 'index.html')
    monkeypatch.setattr(liveserve, 'WEB', str(web))
    labels = tmp_path / 'labels.json'
    labels.write_text('{}')
    root, src = _repo(tmp_path)
    store = str(tmp_path / 'store')
    w = live.Writer(store, [src], root, seed_git=True, lag=150, scan=0, alive=lambda: set(), prov_every=0)
    w.start(sec(ts(4)))
    s = Served(store, str(labels))
    try:
        b.goto(f'http://127.0.0.1:{s.port}/index.html?live=1&areas=0&follow=0')
        b.wait('typeof BOOTED !== "undefined" && BOOTED && STEPS.length === 2 && DV.on === true', 30000)
        b.eval('window.__fc = 0; (function count() { __fc++; requestAnimationFrame(count); })(); 1')
        assert _thumbs(b) == {'shot.png': [64, 43], 'logo.svg': [40, 30]}
        assert b.eval('performance.getEntriesByType("resource").some(e => e.name.includes("/live/bodies?h="))')
        frames(b, 5)
        assert b.eval('typeof window.pwned') == 'undefined'
        assert b.errors() == []
    finally:
        s.close()


@chrome
def test_a_failing_thumbnail_is_asked_again_only_after_a_while(b, site):
    """The sidecar's chunks answer 500. With the page's clock (performance.now) held by the test: asked for again and
    again, a failed thumbnail is fetched no more until TH_RETRY ms have passed on that clock, then once more."""
    hits = []

    class Failing(Quiet):
        def do_GET(self):
            if '/fx.diffs/s/' in self.path:
                hits.append(self.path)
                self.send_error(500)
                return
            super().do_GET()
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Failing, directory=str(site['dir'])))
    threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
    ask = '(async (n) => { for (let k = 0; k < n; k++) { ' + THUMBS_IN + '; await new Promise(r => setTimeout(r, 20)); } ' \
          'return Object.values(' + THUMBS_IN + ').every(v => v === null); })'
    failed = '[...TH.img.values()].length === 2 && [...TH.img.values()].every(e => e.err > 0)'
    try:
        load(b, f'http://127.0.0.1:{srv.server_address[1]}', src='fx.json')
        b.eval('window.__t = performance.now(); performance.now = () => __t; ' + THUMBS_IN)   # the clock stands still
        assert b.eval('DV.on') is True and b.eval('TH_RETRY') == 4000
        b.wait(failed, 30000)                       # both asked for and failed (their fetches are all answered)
        first, chunks = len(hits), b.eval('new Set([...TH.img.keys()].map(h => DV.where.get(h))).size')
        assert 1 <= chunks <= first
        assert b.eval(f'({ask})(15)') is True       # asked 15 times more: nothing fetched
        assert len(hits) == first
        b.eval('__t = Math.min(...[...TH.img.values()].map(e => e.err)) + TH_RETRY - 1; 1')
        assert b.eval(f'({ask})(5)') is True and len(hits) == first       # not yet
        b.eval('__t = Math.max(...[...TH.img.values()].map(e => e.err)) + TH_RETRY + 1; ' + THUMBS_IN)
        b.wait(failed + ' && [...TH.img.values()].every(e => e.err === __t)', 30000)    # the retries: both in one go,
        assert len(hits) == first + chunks                                  # one fetch per chunk, then quiet
        assert b.eval(f'({ask})(10)') is True and len(hits) == first + chunks
    finally:
        srv.shutdown()
        srv.server_close()


@chrome
def test_each_thumbnail_is_fetched_and_decoded_once(b, site):
    """Many frames and steps over the image pages, the texts slow to come: one diffGet and one Image per hash."""
    load(b, site['url'], src='fx.json')
    _thumbs(b)
    b.eval('''TH.img.clear(); DV.texts.clear(); DV.chunks.clear(); window.__gets = {}; window.__imgs = 0;
      const get = diffGet; diffGet = hs => { for (const h of hs) __gets[h] = (__gets[h] || 0) + 1;
        return new Promise(r => setTimeout(r, 400)).then(() => get(hs)); };
      const I = Image; window.Image = function (...a) { __imgs++; return new I(...a); }; 1''')
    for step in (0, 1, 0, 1, 1):
        b.eval(f'setStep({step}); 1')
        frames(b, 10)
    got = _thumbs(b)
    frames(b, 20)
    assert got == {'shot.png': [64, 43], 'logo.svg': [40, 30]}
    gets = b.eval('__gets')
    assert len(gets) == 2 and set(gets.values()) == {1}, gets
    assert b.eval('__imgs') == 2
    assert b.errors() == []


@chrome
def test_image_pages_leave_the_scale_and_the_code_blobs_as_head_has_them(b, site):
    """IMG_SIZE makes the image pages bigger, never the scale K: K and every other blob's radius are the pre-badge
    page's (BASE)."""
    if not (site['dir'] / 'head.html').exists():
        pytest.skip('no git HEAD to compare with')
    sizes = 'JSON.stringify({ K, R: [...U.values()].map(u => [u.id, u.R]).sort() })'
    load(b, site['url'], 'head.html', 'fx.json')
    head = json.loads(b.eval(sizes))
    load(b, site['url'], 'index.html', 'fx.json')
    new = json.loads(b.eval(sizes))
    assert new['K'] == head['K']
    images = {'file:shot.png', 'file:logo.svg'}
    assert [r for r in new['R'] if r[0] not in images] == [r for r in head['R'] if r[0] not in images]
    big = dict(new['R'])
    assert big['file:shot.png'] == big['file:logo.svg'] == b.eval('radius(IMG_SIZE)') > dict(head['R'])['file:shot.png']
