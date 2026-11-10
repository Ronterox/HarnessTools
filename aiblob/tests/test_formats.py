"""Known file formats (`fmt`), the `assets` area, links between non-code files (file -> file `ref` edges, a page's
<script src> to its module) and image thumbnails in the diffs sidecar (SCHEMA.md "v6 additions")."""
import io, json, os, subprocess, sys

import pytest

import areas, diffs, replay, thumbs, timeline
from analyze import fmt_of
from conftest import run, quals
from test_c import load
from test_incremental import check, sequence
from test_replay import msg, result, tool, ts, write_lines

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TL = os.path.join(REPO, 'timeline.py')
WEB = os.path.join(HERE, 'fixtures', 'web-mini')
SID = '11111111-2222-3333-4444-555555555555'


@pytest.mark.parametrize('path, fmt', [
    ('.gitignore', 'ignore'), ('sub/.dockerignore', 'ignore'), ('.npmignore', 'ignore'), ('.prettierignore', 'ignore'),
    ('Makefile', 'make'), ('makefile', 'make'), ('GNUmakefile', 'make'), ('rules.mk', 'make'),
    ('Dockerfile', 'docker'), ('Dockerfile.dev', 'docker'), ('web.dockerfile', 'docker'),
    ('.env', 'env'), ('.env.local', 'env'), ('.editorconfig', 'editorconfig'), ('.gitattributes', 'git'),
    ('.gitmodules', 'git'), ('LICENSE', 'license'), ('COPYING', 'license'), ('LICENSE.md', 'license'),
    ('licence.txt', 'license'), ('requirements.txt', 'pipreq'), ('requirements-dev.txt', 'pipreq'),
    ('constraints.txt', 'pipreq'), ('uv.lock', 'lock'), ('poetry.lock', 'lock'), ('Cargo.lock', 'lock'),
    ('package-lock.json', 'lock'), ('pnpm-lock.yaml', 'lock'), ('go.sum', 'lock'), ('CMakeLists.txt', 'cmake'),
    ('Procfile', 'procfile'), ('a.css', 'css'), ('a.SCSS', 'css'), ('a.less', 'css'), ('x.png', 'image'),
    ('x.JPG', 'image'), ('x.jpeg', 'image'), ('x.gif', 'image'), ('x.webp', 'image'), ('favicon.ico', 'image'),
    ('x.bmp', 'image'), ('logo.svg', 'svg'), ('f.woff', 'font'), ('f.woff2', 'font'), ('f.ttf', 'font'),
    ('f.otf', 'font'), ('notes.txt', None), ('.json', None), ('README', None), ('data.json', 'json')])
def test_fmt_by_name_and_extension(path, fmt):
    assert fmt_of(path) == fmt


def test_images_svg_and_fonts_are_assets_the_new_names_stay_files():
    files = {'app.py': 'print(1)\n', 'img/a.png': '\x00png', 'img/b.svg': '<svg/>\n', 'fonts/f.woff2': '\x00w',
             'Makefile': 'all:\n', '.gitignore': 'x\n', 'LICENSE': 'MIT\n', 'requirements.txt': 'flask\n',
             'site.css': 'a {}\n', 'notes.txt': 'n\n'}
    out = areas.Zoner().apply(run(files)[0])
    area = {b['id']: b['area'] for b in out['blobs'] if b['id'].startswith('file:')}
    assert {p for p, a in area.items() if a == 'assets'} == {'file:img/a.png', 'file:img/b.svg', 'file:fonts/f.woff2'}
    assert {p for p, a in area.items() if a == 'files'} == {'file:Makefile', 'file:.gitignore', 'file:LICENSE',
                                                            'file:requirements.txt', 'file:site.css', 'file:notes.txt'}
    assert {a['id']: a['caption'] for a in out['areas'] if a['id'] in ('assets', 'files')} == {'assets': 'Assets',
                                                                                              'files': 'Files'}


# --- links between non-code files ------------------------------------------------------------------------------------

def refs(edges):
    return sorted((e['a'], e['b'], e['n'], tuple(e['via'])) for e in edges if e['kind'] == 'ref')


def test_web_mini_links():
    r, blobs, edges = run(load(WEB))
    assert refs(edges) == [
        ('file:README.md', 'file:docs/guide.md', 1, ('docs/guide.md',)),              # reference-style link
        ('file:README.md', 'file:img/logo.svg', 1, ('img/logo.svg',)),                # ![logo](...)
        ('file:README.md', 'file:index.html', 1, ('index.html',)),
        ('file:css/site.css', 'file:css/base.css', 1, ('base.css',)),                  # @import
        ('file:css/site.css', 'file:img/logo.svg', 1, ('../img/logo.svg',)),           # url(...), data: ignored
        ('file:docs/guide.md', 'file:README.md', 1, ('../README.md',)),
        ('file:index.html', 'file:css/site.css', 1, ('css/site.css',)),                # <link href>
        ('file:index.html', 'file:docs/guide.md', 1, ('docs/guide.md',)),              # <a href> (#start dropped)
        ('file:index.html', 'file:img/logo.svg', 1, ('img/logo.svg',)),                # <img src>
        ('file:index.html', 'js.app.<module>', 1, ('js/app.js',)),                     # <script src>: its module
    ]                                                   # https:, #top, mailto:, missing.html: nothing; data.json: none
    kinds = {b['id']: b['kind'] for b in blobs.values() if b['id'].startswith('file:')}
    assert kinds == {'file:README.md': 'file', 'file:css/base.css': 'file', 'file:css/site.css': 'file',
                     'file:data.json': 'text', 'file:docs/guide.md': 'file', 'file:img/logo.svg': 'file',
                     'file:index.html': 'file', 'file:.gitignore': 'text'}
    # the script a page loads runs: its top-level code is an entry
    assert blobs['js.app.<module>']['kind'] == 'entry' and quals(blobs['js.app.<module>']) == ['js.app.<module>',
                                                                                             'js.app.greet']


def test_html_attributes_srcset_style_and_absolute_paths():
    files = {'site/pages/a.html': '<img srcset="../i/s.png 1x, /site/i/l.png 2x"><video poster=../i/p.png>'
                                  '<source src="/i/v.webm"><iframe src="b.html"></iframe>'
                                  '<div style="background:url(\'../i/s.png\')"></div><a href=//cdn.x/y.png>'
                                  '<a href="b.html?x=1">b</a><img src="{{ logo }}"><a href="data:text/plain,x">',
             'site/pages/b.html': '<p>\n', 'site/i/s.png': '\x00', 'site/i/l.png': '\x00', 'site/i/p.png': '\x00',
             'i/v.webm': '\x00'}
    assert refs(run(files)[2]) == [
        ('file:site/pages/a.html', 'file:i/v.webm', 1, ('/i/v.webm',)),       # `/` from the nearest ancestor that has it
        ('file:site/pages/a.html', 'file:site/i/l.png', 1, ('/site/i/l.png',)),
        ('file:site/pages/a.html', 'file:site/i/p.png', 1, ('../i/p.png',)),
        ('file:site/pages/a.html', 'file:site/i/s.png', 2, ('../i/s.png',)),  # srcset and style url()
        ('file:site/pages/a.html', 'file:site/pages/b.html', 2, ('b.html',))]  # iframe, a (query dropped)


def test_jinja_extends_include_import_and_url_for_static():
    files = {'app/views.py': 'from flask import render_template\n\ndef index():\n    return render_template("index.html")\n',
             'app/templates/base.html': '<link href="{{ url_for(\'static\', filename=\'app.css\') }}">'
                                        '<script src="{{ url_for(\'static\', filename=\'js/app.js\') }}"></script>',
             'app/templates/index.html': '{% extends "base.html" %}{% include "parts/nav.html" %}'
                                         '{% import \'macros.html\' as m %}{%- from "macros.html" import x %}',
             'app/templates/parts/nav.html': '<a href="/static/app.css">s</a>', 'app/templates/macros.html': '',
             'app/static/app.css': 'a {}\n', 'app/static/js/app.js': 'console.log(1)\n'}
    got = refs(run(files)[2])
    assert got == [
        ('file:app/templates/base.html', 'app.static.js.app.<module>', 1, ('js/app.js',)),  # JS is code here
        ('file:app/templates/base.html', 'file:app/static/app.css', 1, ('app.css',)),
        ('file:app/templates/index.html', 'file:app/templates/base.html', 1, ('base.html',)),
        ('file:app/templates/index.html', 'file:app/templates/macros.html', 2, ('macros.html',)),
        ('file:app/templates/index.html', 'file:app/templates/parts/nav.html', 1, ('parts/nav.html',)),
        ('file:app/templates/parts/nav.html', 'file:app/static/app.css', 1, ('/static/app.css',)),
        ('orphan:app.views', 'file:app/templates/index.html', 1, ('index.html',))]


def test_unresolvable_and_scheme_links_are_ignored():
    files = {'p.html': '<a href="http://x/a.css"><a href="HTTPS://x"><a href="javascript:void(0)">'
                       '<a href="tel:1"><a href="#a"><a href="nope.css"><a href="../../out.css"><a href="${x}.css">',
             'p.md': '[a](ftp://x/a.css) [b](#h) [c](no.md) ![d](<data:x>)', 'a.css': ''}
    assert refs(run(files)[2]) == []


def test_data_files_name_nothing_and_links_do_not_loop():
    files = {'a.md': '[b](b.md)', 'b.md': '[a](a.md)', 'rows.json': '["a.md"]', 'conf.cfg': 'page = "a.md"'}
    assert refs(run(files)[2]) == [('file:a.md', 'file:b.md', 1, ('b.md',)), ('file:b.md', 'file:a.md', 1, ('a.md',))]


def test_links_incremental_matches_full():
    files = load(WEB)
    steps = [(rel, files[rel]) for rel in sorted(files)]
    steps += [('css/site.css', files['css/site.css'] + '@import "extra.css";\n'), ('css/extra.css', 'p {}\n'),
              ('img/logo.svg', files['img/logo.svg'].replace('red', 'blue')), ('docs/guide.md', None),
              ('index.html', files['index.html'].replace('js/app.js', 'js/other.js')), ('js/other.js', 'go();\n')]
    inc, _ = check(sequence(steps))
    last = {(e['a'], e['b']) for e in inc[-1]['edges'] if e['kind'] == 'ref'}
    assert ('file:css/site.css', 'file:css/extra.css') in last and ('file:index.html', 'js.other.<module>') in last


def test_web_mini_live_equals_batch_with_thumbnails(tmp_path):
    from test_live import _fixture_session, exported_equals_batch, follow
    import live
    root = '/work/web'
    src = _fixture_session(tmp_path, 'web-mini', root, [('img/logo.svg', 'red', 'blue'), ('css/base.css', 'red', 'navy')])
    w, store, proj = follow(tmp_path, src, root, restarts=2, check_every=3, seed=4)
    exported_equals_batch(store, proj, root)
    out_live, out_batch = str(tmp_path / 'dl'), str(tmp_path / 'db')
    diffs.export_store(store, out_live)
    subprocess.run([sys.executable, TL, proj, '--root', root, '--no-timings', '-o', str(tmp_path / 'b.json'),
                    '--diffs', out_batch], check=True, capture_output=True)
    from test_diffs import _tree
    assert _tree(out_live) == _tree(out_batch)
    r = diffs.load_static(str(tmp_path / 'b.json'), out_batch)
    th = r.thumbs(len(r.steps) - 1)
    assert list(th) == ['img/logo.svg'] and th['img/logo.svg'].startswith('data:image/svg+xml;base64,')


# --- thumbnails ------------------------------------------------------------------------------------------------------

def _png(color, size=(120, 60)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', size, color).save(buf, 'PNG')
    return buf.getvalue()


def _image_repo(tmp_path):
    """A repo whose a.png is red at the base commit, green at a commit during the session and blue at one after its
    last edit; the disk then holds a yellow one. With --git-reconcile the steps are Edit, git (green), Edit, git (blue);
    an untouched file is seeded from the last commit before the last step (replay.py), so a.png is blue at step 0."""
    repo = tmp_path / 'repo'
    repo.mkdir()
    env = lambda at: {'GIT_AUTHOR_DATE': at, 'GIT_COMMITTER_DATE': at, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
                      'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t', 'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'}
    git = lambda at, *cmd: subprocess.run(['git', '-C', str(repo), *cmd], check=True, env=env(at), capture_output=True)
    (repo / 'a.png').write_bytes(_png('red'))
    (repo / 'app.py').write_text('print(1)\n')
    git(ts(0), 'init', '-q')
    git(ts(0), 'add', '.')
    git(ts(0), 'commit', '-qm', 'base')
    root = str(repo)
    src = tmp_path / 'src'
    write_lines(src / f'{SID}.jsonl', [
        msg('user', 'go', ts=ts(1)),
        msg('assistant', [tool('e1', 'Edit', file_path=f'{root}/app.py', old_string='1', new_string='2')], ts=ts(2), cwd=root),
        dict(result('e1'), timestamp=ts(2, 1)),
        msg('assistant', [tool('e2', 'Edit', file_path=f'{root}/app.py', old_string='2', new_string='3')], ts=ts(8), cwd=root),
        dict(result('e2'), timestamp=ts(8, 1))])
    (repo / 'a.png').write_bytes(_png('green'))
    git(ts(5), 'commit', '-qam', 'green')
    (repo / 'a.png').write_bytes(_png('blue'))
    (repo / 'app.py').write_text('print(3)\n')
    git(ts(9), 'commit', '-qam', 'blue')
    (repo / 'a.png').write_bytes(_png('yellow'))         # the disk now: never a past step's image
    return root, str(src)


def test_thumbnails_come_from_git_objects_not_the_disk(tmp_path):
    pytest.importorskip('PIL')
    root, src = _image_repo(tmp_path)
    rs = replay.steps(replay.transcripts(src), root, seed_git=True, git_reconcile=True)
    out = str(tmp_path / 'd')
    dw = diffs.Static(out)
    dw.sink.images = thumbs.images_for(root)
    tl = timeline.timeline(src, root, timings=False, steps=rs, diffs=dw)
    dw.close()
    (tmp_path / 't.json').write_text(json.dumps(tl), encoding='utf-8')
    r = diffs.load_static(str(tmp_path / 't.json'), out)
    assert [s['tool'] for s in r.steps] == ['Edit', 'git', 'Edit', 'git']
    green, blue = (thumbs._make(_png(c), 'image/png') for c in ('green', 'blue'))
    assert [r.thumbs(i).get('a.png') for i in range(4)] == [blue, green, green, blue]
    assert ['t' in fv for fv in r.fvs] == [True, True, False, True]   # a thumbnail changes only with the image's bytes
    from PIL import Image
    import base64
    im = Image.open(io.BytesIO(base64.b64decode(green.split(',', 1)[1])))
    assert im.size == (64, 32) and im.getpixel((5, 5))[:3] == (0, 128, 0)
    # the disk's image is in no git object: no thumbnail from it; without git objects, no raster thumbnail at all
    assert thumbs.thumb('a.png', _png('yellow').decode('utf-8', 'replace'), thumbs.images_for(root)) is None
    assert thumbs.thumb('a.png', _png('red').decode('utf-8', 'replace'), thumbs.images_for(root)) == \
        thumbs._make(_png('red'), 'image/png')
    assert thumbs.thumb('a.png', _png('red').decode('utf-8', 'replace'), None) is None


def test_timeline_byte_identical_with_and_without_thumbnails(tmp_path):
    pytest.importorskip('PIL')
    root, src = _image_repo(tmp_path)
    go = lambda *extra: subprocess.run([sys.executable, TL, src, '--root', root, '--seed-git', '--git-reconcile',
                                        '--no-timings', *extra], check=True, capture_output=True)
    go('-o', str(tmp_path / 'plain.json'))
    go('-o', str(tmp_path / 'd1.json'), '--diffs', str(tmp_path / 'd1'))
    go('-o', str(tmp_path / 'd2.json'), '--diffs', str(tmp_path / 'd2'))
    plain = (tmp_path / 'plain.json').read_bytes()
    assert (tmp_path / 'd1.json').read_bytes() == plain == (tmp_path / 'd2.json').read_bytes()
    from test_diffs import _tree
    assert _tree(tmp_path / 'd1') == _tree(tmp_path / 'd2')
    r = diffs.load_static(str(tmp_path / 'd1.json'), str(tmp_path / 'd1'))
    assert r.thumbs(len(r.steps) - 1)['a.png'] == thumbs._make(_png('blue'), 'image/png')


def test_raster_thumbnails_live_equal_batch_and_are_served(tmp_path):
    pytest.importorskip('PIL')
    from test_live import follow, exported_equals_batch
    from test_liveserve import Served
    from test_diffs import _tree
    root, src = _image_repo(tmp_path)
    w, store, proj = follow(tmp_path, src, root, seed_git=True, git_reconcile=True, restarts=1)
    exported_equals_batch(store, proj, root, seed_git=True, git_reconcile=True,
                          rest=json.load(open(os.path.join(store, 'rest.json'))))
    out_live, out_batch = str(tmp_path / 'dl'), str(tmp_path / 'db')
    diffs.export_store(store, out_live)
    rest = json.load(open(os.path.join(store, 'rest.json')))
    rs = replay.steps(replay.transcripts(proj), root, seed_git=True, git_reconcile=True, rest=rest)
    dw = diffs.Static(out_batch)
    dw.sink.images = thumbs.images_for(root)
    timeline.timeline(proj, root, timings=False, steps=rs, diffs=dw)
    dw.close()
    assert _tree(out_live) == _tree(out_batch)
    labels = tmp_path / 'labels.json'
    labels.write_text('{}')
    s = Served(store, str(labels))
    try:
        tl = s.timeline()
        hs = [h for st in tl['steps'] for h in (st.get('fv', {}).get('t') or {}).values() if h]
        r, body = s.get('/live/bodies?h=' + ','.join(hs))
        got = json.loads(body)
        made = {thumbs._make(_png(c), 'image/png') for c in ('red', 'green', 'blue')}
        assert len(hs) >= 2 and all(got[h] in made and diffs.h10(got[h]) == h for h in hs)
    finally:
        s.close()


def test_an_svg_thumbnail_is_text_and_its_script_never_runs(tmp_path):
    """The svg is stored as it is (a data: URI of its text); drawn the way the page draws thumbnails (an Image
    on a canvas), its <script> does not run."""
    from browser import CHROME, Browser
    if not CHROME:
        pytest.skip('no chrome')
    svg = open(os.path.join(WEB, 'img', 'logo.svg'), encoding='utf-8').read()
    uri = thumbs.thumb('img/logo.svg', svg, None)
    import base64
    assert base64.b64decode(uri.split(',', 1)[1]).decode() == svg and '<script>' in svg
    page = tmp_path / 'p.html'
    page.write_text('<canvas id="c" width="64" height="64"></canvas><script>window.pwned = 0;'
                    f'const im = new Image(); im.onload = () => {{ document.getElementById("c").getContext("2d")'
                    f'.drawImage(im, 0, 0); window.done = im.naturalWidth; }}; im.src = {json.dumps(uri)};</script>')
    b = Browser()
    try:
        b.goto('file://' + str(page))
        b.wait('window.done > 0')
        assert b.eval('window.pwned') == 0 and b.eval('window.done') == 40
        assert b.eval('[...document.getElementById("c").getContext("2d").getImageData(10, 10, 1, 1).data]') == \
            [255, 0, 0, 255]
    finally:
        b.close()


# --- review round 2: bounded costs, the html rule, the mutants ---------------------------------------------------------

ADVERSARIAL = {      # ~400 KB each: unclosed or repeated constructs that made the link regexes quadratic
    'css url(': ('css', 'url(' * 100000), 'css url("': ('css', 'url("' * 80000), 'html url(': ('html', 'url(' * 100000),
    'md [': ('md', '[' * 400000), 'md ](': ('md', '](' * 200000), 'md [x](': ('md', '[' + 'x' * 999 + '](' * 199000),
    'md ref': ('md', '[' * 400000 + ']:'), 'html href="': ('html', 'href="' * 70000), 'srcset': ('html', 'srcset="' * 50000),
    'jinja': ('html', '{% extends "' * 35000), 'url_for': ('html', "url_for('static', filename='" * 14000),
    '@import': ('css', '@import "' * 45000), 'spaces': ('html', 'href' + ' ' * 400000),
    'long attr': ('html', ('href="' + 'a' * 3000) * 130)}


@pytest.mark.parametrize('name', sorted(ADVERSARIAL))
def test_link_extraction_stays_linear(name):
    import time
    from analyze import _file_links
    kind, text = ADVERSARIAL[name]
    t0 = time.perf_counter()
    _file_links(kind, text)
    assert time.perf_counter() - t0 < 0.6, name
    big = {'x.' + kind: text, 'a': ''}
    t0 = time.perf_counter()
    run(big)
    assert time.perf_counter() - t0 < 1.5, name           # extraction and resolution of what it found


def test_attributes_far_from_their_tag_look_back_a_bounded_way():
    """~2 MB of attributes after one tag: each looks back at most 512 characters for its `<` (from the file start it
    would copy up to 2 MB per attribute)."""
    import time
    from analyze import LINK_MAX, _file_links
    text = '<p ' + 'src=a ' * 330000
    assert len(text) < LINK_MAX
    t0 = time.perf_counter()
    assert len(_file_links('html', text)) == 330000
    assert time.perf_counter() - t0 < 2.5


def test_files_over_the_link_limit_are_not_read():
    from analyze import LINK_MAX, _file_links
    assert _file_links('html', '<a href="b.html">' + ' ' * LINK_MAX) == []


def _bomb(w, h):
    """A w x h 1-bit PNG of zeros: a few KB that decode to w * h pixels."""
    import struct, zlib
    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    row = b'\x00' * (1 + (w + 7) // 8)
    comp = zlib.compressobj(9)
    data = b''.join(comp.compress(row) for _ in range(h)) + comp.flush()
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 1, 0, 0, 0, 0)) + chunk(b'IDAT', data) + \
        chunk(b'IEND', b'')


def _commit(tmp_path, files):
    repo = tmp_path / 'r'
    repo.mkdir()
    for p, data in files.items():
        (repo / p).write_bytes(data)
    env = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t',
           'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'}
    for cmd in (['init', '-q'], ['add', '-A'], ['commit', '-qm', 'x']):
        subprocess.run(['git', '-C', str(repo), *cmd], check=True, env=env, capture_output=True)
    return str(repo)


def test_decompression_bombs_are_never_decoded(tmp_path):
    pytest.importorskip('PIL')
    bomb, ok = _bomb(9000, 9000), _png('red', (4000, 3000))       # 81 Mpx (under Pillow's own limit), 12 Mpx
    assert len(bomb) < 20000
    root = _commit(tmp_path, {'bomb.png': bomb, 'ok.png': ok})
    code = ('import resource, sys, thumbs\n'
            f'gi = thumbs.images_for({root!r})\n'
            f'b = thumbs.thumb("bomb.png", open({root!r} + "/bomb.png", "rb").read().decode("utf-8", "replace"), gi)\n'
            f'o = thumbs.thumb("ok.png", open({root!r} + "/ok.png", "rb").read().decode("utf-8", "replace"), gi)\n'
            'print(b is None, o is not None, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024)\n')
    out = subprocess.run([sys.executable, '-c', code], cwd=REPO, capture_output=True, text=True, check=True).stdout.split()
    assert out[:2] == ['True', 'True'] and int(out[2]) < 250, out          # MB of peak RSS (the bomb alone: 700+)
    assert thumbs.MAX_PIXELS == 16_000_000


def test_git_blobs_over_the_size_limit_are_not_read(tmp_path, monkeypatch):
    pytest.importorskip('PIL')
    root = _commit(tmp_path, {'a.png': _png('red')})
    monkeypatch.setattr(thumbs, 'BLOB_MAX', 10)
    assert thumbs.thumb('a.png', _png('red').decode('utf-8', 'replace'), thumbs.images_for(root)) is None


def test_two_committed_images_with_the_same_lossy_text_get_no_thumbnail(tmp_path):
    pytest.importorskip('PIL')
    a = bytearray(_png('gray', (20, 20)))
    i = next(k for k in range(len(a)) if a[k] >= 0x80 and bytes(a[k:k + 1]).decode('utf-8', 'replace') == '�'
             and (k + 1 >= len(a) or a[k + 1] < 0x80))
    b = bytearray(a)
    b[i] = 0xff if a[i] != 0xff else 0xfe
    assert bytes(a) != bytes(b) and bytes(a).decode('utf-8', 'replace') == bytes(b).decode('utf-8', 'replace')
    root = _commit(tmp_path, {'a.png': bytes(a), 'b.png': bytes(b), 'c.png': _png('red')})
    gi = thumbs.images_for(root)
    assert thumbs.thumb('a.png', bytes(a).decode('utf-8', 'replace'), gi) is None
    assert thumbs.thumb('c.png', _png('red').decode('utf-8', 'replace'), gi) == thumbs._make(_png('red'), 'image/png')


def test_the_svg_cap():
    head, tail = '<svg>', '</svg>'
    fits = head + 'a' * (thumbs.SVG_MAX - len(head) - len(tail)) + tail
    assert thumbs.thumb('x.svg', fits, None) and thumbs.thumb('x.svg', fits + ' ', None) is None


def test_html_code_uses_is_files_html_only_files_link_is_docs():
    files = {'app.py': 'from flask import render_template\n\ndef index():\n    return render_template("page.html")\n',
             'templates/page.html': '{% extends "base.html" %}', 'templates/base.html': '<p>',
             'README.md': 'See [the demo](demo/index.html).', 'demo/index.html': '<a href="other.html">o</a>',
             'demo/other.html': '<a href="index.html">back</a>'}
    r, blobs, _ = run(files)
    assert blobs['file:demo/index.html']['kind'] == 'file'            # linked by a file: kind `file` still
    area = {b['id']: b['area'] for b in areas.Zoner().apply(r)['blobs'] if b['id'].startswith('file:')}
    assert area == {'file:templates/page.html': 'files',                # code renders it
                    'file:templates/base.html': 'files',                # a page code uses extends it
                    'file:README.md': 'docs', 'file:demo/index.html': 'docs', 'file:demo/other.html': 'docs'}


def test_links_to_code_count_only_from_script_src_and_only_to_js():
    files = {'README.md': 'See [the module](src/m.py) and [the script](web/app.js).',
             'web/index.html': '<a href="app.js">src</a><script src="tool.py"></script><script src="app.js"></script>',
             'src/m.py': 'def f():\n    pass\n', 'web/tool.py': 'print(1)\n', 'web/app.js': 'console.log(1);\n'}
    r, blobs, edges = run(files)
    assert refs(edges) == [('file:web/index.html', 'web.app.<module>', 1, ('app.js',))]
    assert 'web.tool.<module>' not in blobs                            # a .py in <script src> is not run


def test_an_ambiguous_template_or_static_name_links_nothing():
    files = {'a/templates/x.html': '', 'b/templates/x.html': '', 'a/static/s.css': '', 'b/static/s.css': '',
             'docs/page.html': '{% include "x.html" %}<link href="{{ url_for(\'static\', filename=\'s.css\') }}">',
             'c/templates/y.html': '{% include "y2.html" %}', 'c/templates/y2.html': ''}
    assert refs(run(files)[2]) == [('file:c/templates/y.html', 'file:c/templates/y2.html', 1, ('y2.html',))]


@pytest.mark.parametrize('lit, path', [
    ('http://x/a.css', None), ('HTTPS://x', None), ('mailto:a@b.c', None), ('data:image/png;base64,x', None),
    ('javascript:void(0)', None), ('tel:1', None), ('//cdn/x.js', None), ('#top', None), ('{{ x }}.css', None),
    ('${x}.css', None), ('', None), ('a.css?v=1#x', 'a.css'), ('a%20b.png', 'a b.png'), ('../x.md', '../x.md')])
def test_link_literals(lit, path):
    from analyze import _link_lit
    assert _link_lit(lit) == path
