"""Shell scripts through analyze(): the sh-mini fixture exactly, one shell heuristic per test, incremental == full,
live == batch; and the `fmt` of data / doc files with their `data` / `docs` areas."""
import os

import pytest

pytest.importorskip('tree_sitter_bash')

import areas
from conftest import run, quals, call
from test_c import load
from test_incremental import check, sequence

MINI = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'sh-mini')


@pytest.fixture(scope='module')
def mini():
    return run(load(MINI))


def test_mini_blobs(mini):
    _, blobs, _ = mini
    got = {b['id']: (b['kind'], b['name'], b['module'], quals(b)) for b in blobs.values()}
    assert got == {
        # every script's top-level code is an entry; what only it reaches (its own and sourced functions) joins it
        'run_sh.<module>': ('entry', 'run.sh', 'run_sh', ['lib.common_sh.cleanup', 'lib.common_sh.die', 'run_sh.<module>',
                                                         'run_sh.report', 'run_sh.serve', 'run_sh.usage']),
        'scripts.build_sh.<module>': ('entry', 'build.sh', 'scripts.build_sh', ['scripts.build_sh.<module>',
                                                                               'scripts.build_sh.check']),
        'bin.deploy_sh.<module>': ('entry', 'deploy', 'bin.deploy_sh', ['bin.deploy_sh.<module>']),   # shebang, no extension
        # the modules other languages' scripts are run as: entries of their own
        'tools.report.<module>': ('entry', 'tools.report', 'tools.report', ['tools.report.<module>', 'tools.report.main',
                                                                           'tools.report.summarize']),
        'tools.gen.<module>': ('entry', 'tools.gen', 'tools.gen', ['tools.gen.<module>', 'tools.util.write_all']),  # -m
        'web.app.<module>': ('entry', 'web.app', 'web.app', ['web.app.<module>', 'web.app.start']),
        'hub:lib.common_sh': ('hub', 'common_sh', 'lib.common_sh', ['lib.common_sh.log']),      # run.sh and build.sh
        'tests.test_run_sh': ('tests', 'test_run_sh', 'tests.test_run_sh', ['tests.test_run_sh.<module>',
                                                                           'tests.test_run_sh.expect']),
        'file:README.md': ('text', 'README.md', '.', []),
        'file:config.toml': ('file', 'config.toml', '.', []),
        'file:data/in.txt': ('file', 'in.txt', 'data', []),
        'file:data/table.csv': ('text', 'table.csv', 'data', []),
        'file:package.json': ('text', 'package.json', '.', []),
    }


def test_mini_errors_and_data(mini):
    _, blobs, _ = mini
    # exit 2 in the case's fallback, `|| die`, die's own exit 1
    assert blobs['run_sh.<module>']['errors'] == 3
    # `|| return 3`, `check || exit`, exit 2
    assert blobs['scripts.build_sh.<module>']['errors'] == 3
    assert blobs['bin.deploy_sh.<module>']['errors'] == 1          # `|| exit 1`
    # PORT read with a default, MODE set for one command; OUT is assigned (no env); redirect / cat targets
    assert blobs['run_sh.<module>']['data'] == ['env:MODE', 'env:PORT', 'env:TMPDIR', 'file:build/report.txt',
                                                'file:data/in.txt']
    assert blobs['scripts.build_sh.<module>']['data'] == ['env:BUILD_ID', 'env:CC', 'file:build/out.txt']
    assert blobs['bin.deploy_sh.<module>']['data'] == ['env:BUILD_ID', 'env:DEPLOY_TOKEN', 'file:run.log']
    assert blobs['hub:lib.common_sh']['data'] == ['file:run.log']


def test_mini_edges(mini):
    _, _, edges = mini
    assert [(e['a'], e['b'], e['kind'], e['n'], e.get('via')) for e in edges] == [
        ('bin.deploy_sh.<module>', 'scripts.build_sh.<module>', 'main', 1, None),   # ./scripts/build.sh after cd ..
        ('run_sh.<module>', 'hub:lib.common_sh', 'main', 2, None),
        ('run_sh.<module>', 'scripts.build_sh.<module>', 'main', 1, None),          # bash scripts/build.sh
        ('run_sh.<module>', 'tools.gen.<module>', 'main', 1, None),                 # python3 -m tools.gen
        ('run_sh.<module>', 'tools.report.<module>', 'main', 1, None),              # .venv/bin/python tools/report.py
        ('run_sh.<module>', 'web.app.<module>', 'main', 1, None),                   # node web/app.js
        ('scripts.build_sh.<module>', 'hub:lib.common_sh', 'main', 1, None),        # . "$here/../lib/common.sh"
        ('bin.deploy_sh.<module>', 'hub:lib.common_sh', 'data', 1, ['file:run.log']),
        ('bin.deploy_sh.<module>', 'scripts.build_sh.<module>', 'data', 1, ['env:BUILD_ID']),
        ('run_sh.<module>', 'file:config.toml', 'ref', 1, ['config.toml']),
        ('run_sh.<module>', 'file:data/in.txt', 'ref', 1, ['data/in.txt']),
    ]


def test_mini_calls(mini):
    _, blobs, _ = mini
    run_sh = blobs['run_sh.<module>']
    assert call(run_sh, 'run_sh.<module>', 'lib.common_sh.cleanup') == {'to': 'lib.common_sh.cleanup', 'error': False,
                                                                         'deferred': True}     # trap cleanup EXIT
    assert call(run_sh, 'run_sh.report', 'lib.common_sh.die') == {'to': 'lib.common_sh.die', 'error': True}
    assert call(run_sh, 'run_sh.report', 'tools.report.<module>') == {'to': 'tools.report.<module>', 'error': False}
    assert call(run_sh, 'run_sh.<module>', 'run_sh.usage') == {'to': 'run_sh.usage', 'error': True}   # `*) usage; exit 2`
    assert call(run_sh, 'lib.common_sh.die', 'lib.common_sh.log') == {'to': 'lib.common_sh.log', 'error': False}


def test_mini_unresolved(mini):
    r, _, _ = mini
    # builtins, echo / curl / mkdir ..., `$CC`, a sourced library without top-level code never count; the one
    # miss is helper_not_here
    assert (r['stats']['calls'], r['stats']['unresolved']) == (20, 1)


def test_shebang_decides_extensionless_files():
    _, blobs, _ = run({'go': '#!/bin/sh\necho hi\n', 'tool': '#!/usr/bin/env python3\nprint(1)\n', 'LICENSE': 'MIT\n',
                       'b': '#! /usr/bin/env -S bash -e\necho b\n', 'x.sh': './go\n'})
    assert blobs['go_sh.<module>']['kind'] == 'entry' and blobs['b_sh.<module>']['name'] == 'b'
    assert blobs['file:tool']['kind'] == blobs['file:LICENSE']['kind'] == 'text'
    assert call(blobs['x_sh.<module>'], 'x_sh.<module>', 'go_sh.<module>')


def test_script_dir_idioms_and_wrappers():
    r, blobs, edges = run({
        'scripts/a.sh': '#!/bin/bash\nd=${0%/*}\nexec "$d/b.sh"\n',
        'scripts/b.sh': 'D="$(dirname "${BASH_SOURCE[0]}")"\ntimeout 5 "$D/c.sh"\nenv -i A=1 bash ./c.sh\n'
                        'uv run python -m pkg.tool --x\nnohup node "$D/../web/w.js" &\n',
        'scripts/c.sh': 'root=$(cd "$(dirname "$0")/.." && pwd)\npython3 -I "$root/pkg/tool.py"\ncd "$root"\n'
                        'bash scripts/d.sh\n',
        'scripts/d.sh': 'echo d\n',
        'pkg/tool.py': 'print(1)\n', 'web/w.js': 'console.log(1);\n',
        # a script at the root: cd into a directory of the root, by $0 and by a variable holding the root
        'top.sh': 'cd "$(dirname "$0")/scripts"\n./d.sh\nhere=$(cd "$(dirname "$0")" && pwd)\ncd "$here/pkg"\n'
                  'python3 tool.py\n',
    })
    got = sorted((e['a'], e['b']) for e in edges if e['kind'] == 'main')
    assert got == [('scripts.a_sh.<module>', 'scripts.b_sh.<module>'), ('scripts.b_sh.<module>', 'pkg.tool.<module>'),
                   ('scripts.b_sh.<module>', 'scripts.c_sh.<module>'), ('scripts.b_sh.<module>', 'web.w.<module>'),
                   ('scripts.c_sh.<module>', 'pkg.tool.<module>'), ('scripts.c_sh.<module>', 'scripts.d_sh.<module>'),
                   ('top_sh.<module>', 'pkg.tool.<module>'), ('top_sh.<module>', 'scripts.d_sh.<module>')]
    assert [c['to'] for c in blobs['scripts.b_sh.<module>']['funcs'][0]['calls']].count('scripts.c_sh.<module>') == 2
    assert blobs['scripts.b_sh.<module>']['data'] == ['env:A']
    assert r['stats']['unresolved'] == 0


def test_runs_that_name_no_snapshot_code():
    r, blobs, _ = run({'a.sh': 'bash missing.sh\npython3 gone.py\n./build/app --x\n"$BIN" --y\npython3 -m pytest -q\n'
                               'python3 -c "print(1)"\n'})
    assert r['stats'] == dict(r['stats'], calls=2, unresolved=2)     # the two code files; binaries / -m / -c never count
    assert blobs['a_sh.<module>']['funcs'][0]['calls'] == []


def test_error_tails_and_branches():
    r, blobs, _ = run({'e.sh': 'f() {\n  if [ -z "$1" ]; then\n    echo no >&2\n    warn\n    exit 3\n  fi\n'
                               '  g || { log; return 1; }\n  h || exit\n  [ -n "$1" ] && return 0\n  return\n}\n'
                               'f\nlog() { :; }\ng() { :; }\nh() { :; }\nwarn() { :; }\n'
                               'case "$1" in\n  a) log ;;\n  *) h; exit 0 ;;\nesac\n'})
    b = blobs['e_sh.<module>']
    f = next(x for x in b['funcs'] if x['qual'] == 'e_sh.f')
    assert f['errors'] == 3                 # exit 3, `|| { ...; return 1; }`, `|| exit`; return 0 / a bare return are not
    assert [(c['to'], c['error']) for c in f['calls']] == [('e_sh.warn', True), ('e_sh.g', False), ('e_sh.log', True),
                                                           ('e_sh.h', False)]
    assert call(b, 'e_sh.<module>', 'e_sh.h') == {'to': 'e_sh.h', 'error': False}   # exit 0 is no error
    assert b['errors'] == 3


def test_env_rules():
    _, blobs, _ = run({'v.sh': 'out=build\necho "$out" "$1" "$#" "$RANDOM" "$HOME" "${MODEL:-m}"\nMODEL=x\n'
                               'for f in a b; do echo "$f"; done\nread -r line\necho "$line"\nexport TOKEN\n'
                               'echo "$TOKEN"\nTOKEN=1\nLANG=C sort a.txt > sorted.txt\n'})
    assert blobs['v_sh.<module>']['data'] == ['env:HOME', 'env:LANG', 'env:MODEL', 'env:TOKEN', 'file:a.txt',
                                              'file:sorted.txt']


def test_sourced_functions_resolve_through_the_chain_and_back():
    r, blobs, edges = run({
        'main.sh': 'source ./a.sh\nhello\nwho\n',
        'a.sh': '. ./b.sh\nhello() { greet; }\n',
        'b.sh': 'greet() { echo hi; }\nwho() { callback; }\n',
        'c.sh': 'callback() { :; }\n',
        'd.sh': 'source b.sh\ncallback() { :; }\n',
    })
    funcs = {f['qual']: f for b in blobs.values() for f in b['funcs']}
    assert [c['to'] for c in funcs['main_sh.<module>']['calls']] == ['a_sh.<module>', 'a_sh.hello', 'b_sh.who']
    assert [c['to'] for c in funcs['a_sh.hello']['calls']] == ['b_sh.greet']
    assert [c['to'] for c in funcs['b_sh.who']['calls']] == ['d_sh.callback']   # the one script sourcing b.sh with it
    assert ('main_sh.<module>', 'a_sh.<module>', 'main') in {(e['a'], e['b'], e['kind']) for e in edges}
    assert r['stats']['unresolved'] == 0


def test_a_function_two_sourcing_scripts_define_stays_unresolved():
    r, blobs, _ = run({'b.sh': 'who() { callback; }\n', 'c.sh': 'source b.sh\ncallback() { :; }\nwho\n',
                       'd.sh': 'source b.sh\ncallback() { :; }\nwho\n'})
    funcs = {f['qual']: f for b in blobs.values() for f in b['funcs']}
    assert funcs['b_sh.who']['calls'] == [] and r['stats']['unresolved'] == 1


@pytest.mark.parametrize('scoped', ['(cd sub && ./x.sh)', 'cd sub | cat', 'v=$(cd sub && pwd)', 'f() { cd sub; }'])
def test_cd_and_variables_end_with_their_subshell_pipeline_substitution_or_function(scoped):
    _, blobs, _ = run({'a.sh': f'D=sub\n{scoped}\n(D=elsewhere)\n./y.sh\nbash "$D/x.sh"\n',
                       'y.sh': 'echo\n', 'sub/y.sh': 'echo\n', 'sub/x.sh': 'echo\n'})
    top = blobs['a_sh.<module>']['funcs'][-1]
    assert [c['to'] for c in top['calls'] if c['to'] != 'sub.x_sh.<module>'] == ['y_sh.<module>']
    assert 'sub.x_sh.<module>' in [c['to'] for c in top['calls']]


def test_error_names_and_bare_exits():
    _, blobs, _ = run({'e.sh': 'usage() { :; }\nmake_error_page() { :; }\nfailover() { :; }\nlog_error() { :; }\n'
                               'if [ -z "$1" ]; then\n  usage\n  exit\nfi\nmake_error_page\nfailover\nlog_error\n'
                               'run || exit\n'})
    top = next(f for f in blobs['e_sh.<module>']['funcs'] if f['name'] == '<module>')
    assert [(c['to'], c['error']) for c in top['calls']] == [('e_sh.usage', False), ('e_sh.make_error_page', False),
                                                             ('e_sh.failover', False), ('e_sh.log_error', True)]
    assert top['errors'] == 1                     # only `run || exit`


@pytest.mark.parametrize('name, text', [
    ('lone surrogate', 'echo "caf\udce9"\nf() { :; }\nf\n'),
    ('unpaired high surrogate', 'echo "\ud800"\n'),
    ('600 nested ifs', 'if true; then\n' * 600 + 'echo x\n' + 'fi\n' * 600),
    ('500 nested substitutions', 'echo ' + '$(' * 500 + 'x' + ')' * 500 + '\n'),
    ('3000-term && chain', 'a' + ' && a' * 3000 + '\n'),
    ('syntax error', 'if then fi (( {{ \nf() {\n'),
    ('nul byte', 'echo \x00 hi\n'),
])
def test_hostile_scripts_never_crash_the_analysis(name, text):
    r, blobs, _ = run({'bad.sh': text, 'ok.sh': 'g() { :; }\ng\n', 'ok.py': 'def h():\n    pass\n'})
    assert quals(blobs['ok_sh.<module>']) == ['ok_sh.<module>', 'ok_sh.g']     # the rest of the snapshot as always
    assert r['modules']['bad_sh']['error'] is None or not any(b['module'] == 'bad_sh' for b in blobs.values())


def test_data_files_name_no_files_one_hop_on():
    _, _, edges = run({'run.sh': 'diff out.json expected/out.ndjson\ncat page.html\n',
                       'expected/out.ndjson': '{"see": "README.md"}\n', 'out.json': '["README.md"]\n',
                       'page.html': '<a href="README.md">x</a>\n', 'README.md': '# r\n'})
    assert sorted((e['a'], e['b']) for e in edges if e['kind'] == 'ref') == [
        ('file:page.html', 'file:README.md'), ('run_sh.<module>', 'file:expected/out.ndjson'),
        ('run_sh.<module>', 'file:out.json'), ('run_sh.<module>', 'file:page.html')]


def test_python_scripts_a_shell_runs_become_entries_but_tests_stay_tests():
    files = {'run.sh': 'python3 app/cli.py\npython3 tests/test_x.py\n',
             'app/cli.py': 'from app import lib\n\ndef main():\n    lib.work()\n\nif __name__ == "__main__":\n    main()\n',
             'app/lib.py': 'def work():\n    return 1\n',
             # three more modules: app/cli.py alone does not cover half the app, so Python has no entry of its own
             'app/a.py': 'def a():\n    pass\n', 'app/b.py': 'def b():\n    pass\n', 'app/c.py': 'def c():\n    pass\n',
             'tests/test_x.py': 'def test_a():\n    assert True\n\nif __name__ == "__main__":\n    test_a()\n'}
    _, blobs, edges = run(files)
    assert quals(blobs['app.cli.<module>']) == ['app.cli.<module>', 'app.cli.main', 'app.lib.work']
    assert quals(blobs['tests.test_x']) == ['tests.test_x.<module>', 'tests.test_x.test_a']   # a tests blob still
    assert {(e['a'], e['b']) for e in edges if e['kind'] == 'main'} == {('run_sh.<module>', 'app.cli.<module>'),
                                                                       ('run_sh.<module>', 'tests.test_x')}
    _, alone, _ = run({k: v for k, v in files.items() if k != 'run.sh'})
    assert 'app.cli.<module>' not in alone and quals(alone['orphan:app.cli']) == ['app.cli.main']


def test_refs_and_heredocs():
    _, blobs, edges = run({'tools/mk.sh': 'here=$(cd "$(dirname "$0")" && pwd)\n'
                                          'python3 -I - "$here/page.tpl.html" "$here/../data.json" <<\'PY\'\n'
                                          'import sys\nprint(open(sys.argv[1]).read())\nPY\n'
                                          'cat <<EOF > "$here/out.md"\nhello $USER\nEOF\n',
                           'tools/page.tpl.html': '<p>x</p>\n', 'data.json': '{}\n'})
    assert sorted((e['b'], e['via'][0]) for e in edges if e['kind'] == 'ref') == [
        ('file:data.json', '$here/../data.json'), ('file:tools/page.tpl.html', '$here/page.tpl.html')]
    assert blobs['tools.mk_sh.<module>']['data'] == ['env:USER', 'file:tools/out.md']


def test_incremental_matches_full_recompute():
    files = load(MINI)
    steps = [(rel, files[rel]) for rel in sorted(files)]
    steps += [('lib/common.sh', files['lib/common.sh'].replace('die() {', 'fatal() {')),     # die stops resolving
              ('lib/extra.sh', 'die() {\n    exit 4\n}\n'),
              ('run.sh', files['run.sh'].replace('source lib/common.sh', 'source lib/common.sh\n. lib/extra.sh')),
              ('scripts/build.sh', None),                                                  # its runs go unresolved
              ('tools/report.py', files['tools/report.py'].replace("if __name__", "x = 1\nif __name__")),
              ('docs/guide.md', '# guide\n'), ('config.toml', None)]
    inc, _ = check(sequence(steps))
    last = {b['id']: b for b in inc[-1]['blobs']}
    assert call(last['run_sh.<module>'], 'run_sh.report', 'lib.extra_sh.die')['error'] is True


def test_mini_live_equals_batch(tmp_path):
    from test_live import _fixture_session, exported_equals_batch, follow
    root = '/work/sh'
    src = _fixture_session(tmp_path, 'sh-mini', root, [('run.sh', 'pipefail', 'pipefail -x'),
                                                        ('lib/common.sh', 'exit 1', 'exit  1')])
    w, store, proj = follow(tmp_path, src, root, restarts=2, check_every=3, seed=3)
    exported_equals_batch(store, proj, root)


# --- data / doc formats ---------------------------------------------------------------------------------------------

def test_fmt_marks_data_and_doc_files_and_their_areas():
    files = {'app.py': 'open("conf/app.toml")\nopen("README.md")\n', 'README.md': '# x\n', 'docs/a.markdown': 'a\n',
             'web/index.htm': '<p>\n', 'conf/app.toml': 'a = 1\n', 'b.yml': 'a: 1\n', 'c.json': '{}\n', 'd.jsonl': '{}\n',
             'e.ndjson': '{}\n', 'f.csv': 'a\n', 'g.tsv': 'a\n', 'h.xml': '<a/>\n', 'i.ini': '[a]\n', 'j.cfg': '[a]\n',
             'k.txt': 'x\n', 'LICENSE': 'MIT\n', 'l.png': '\x00png', 'M.JSON': '{}\n', '.json': '{}\n'}
    r, blobs, _ = run(files)
    fmt = {b['path']: b.get('fmt') for b in blobs.values() if b['kind'] in ('file', 'text')}
    assert fmt == {'README.md': 'md', 'docs/a.markdown': 'md', 'web/index.htm': 'html', 'conf/app.toml': 'toml',
                   'b.yml': 'yaml', 'c.json': 'json', 'd.jsonl': 'jsonl', 'e.ndjson': 'jsonl', 'f.csv': 'csv',
                   'g.tsv': 'tsv', 'h.xml': 'xml', 'i.ini': 'ini', 'j.cfg': 'ini', 'k.txt': None, 'LICENSE': 'license',
                   'l.png': 'image', 'M.JSON': 'json', '.json': None}
    assert blobs['file:README.md']['kind'] == 'file' and blobs['file:c.json']['kind'] == 'text'   # kind as before
    out = areas.Zoner().apply(r)
    area = {b['id']: b['area'] for b in out['blobs']}
    assert {p for p, a in area.items() if a == 'docs'} == {'file:README.md', 'file:docs/a.markdown', 'file:web/index.htm'}
    assert {p for p, a in area.items() if a == 'files'} == {'file:k.txt', 'file:LICENSE', 'file:.json'}
    assert {p for p, a in area.items() if a == 'assets'} == {'file:l.png'}
    assert len([a for a in area.values() if a == 'data']) == 11
    assert {a['id']: a['caption'] for a in out['areas'] if not a['id'].startswith('a')} == {
        'data': 'Data', 'docs': 'Docs', 'files': 'Files'}


def test_referenced_html_stays_in_files_unreferenced_html_and_markdown_are_docs():
    files = {'app.py': 'render_template("page.html")\nopen("guide.md")\nopen("rows.json")\n',
             'templates/page.html': '<p>\n', 'web/about.html': '<p>\n', 'guide.md': '# g\n', 'notes.md': '# n\n',
             'rows.json': '[]\n', 'cache.json': '{}\n'}
    r, blobs, _ = run(files)
    assert (blobs['file:templates/page.html']['kind'], blobs['file:web/about.html']['kind']) == ('file', 'text')
    area = {b['id']: b['area'] for b in areas.Zoner().apply(r)['blobs'] if b['id'].startswith('file:')}
    assert area == {'file:templates/page.html': 'files', 'file:web/about.html': 'docs', 'file:guide.md': 'docs',
                    'file:notes.md': 'docs', 'file:rows.json': 'data', 'file:cache.json': 'data'}


def test_generic_env_variables_link_nothing_in_any_language():
    r, blobs, edges = run({
        'a.sh': 'echo "$HOME $PATH $LC_ALL $TMPDIR $XDG_RUNTIME_DIR $CLAUDE_CONFIG_DIR $XDG_DATA_HOME"\n',
        'b.sh': 'cd "$HOME"; export LC_ALL=C PATH=/bin; echo "$CLAUDE_CONFIG_DIR" "$XDG_DATA_HOME"\n',
        'main.c': '#include <stdlib.h>\nint main(void) { return getenv("HOME") && getenv("CLAUDE_CONFIG_DIR"); }\n'})
    assert 'env:HOME' in blobs['a_sh.<module>']['data'] and 'env:LC_ALL' in blobs['b_sh.<module>']['data']  # still data
    got = {(e['a'], e['b']): e['via'] for e in edges if e['kind'] == 'data'}
    assert got == {('a_sh.<module>', 'b_sh.<module>'): ['env:CLAUDE_CONFIG_DIR', 'env:XDG_DATA_HOME'],
                   ('a_sh.<module>', 'main.<module>'): ['env:CLAUDE_CONFIG_DIR'],
                   ('b_sh.<module>', 'main.<module>'): ['env:CLAUDE_CONFIG_DIR']}
    # recounted the same way after the folds and cuts
    assert sorted((e['a'], e['b'], e['via']) for e in areas.data_edges(r['blobs'])) == \
        sorted((a, b, v) for (a, b), v in got.items())


def test_fmt_rides_the_deltas():
    # a doc file appears, changes, goes; the encoded steps expand back to the full ones (check())
    inc, v5 = check(sequence([('app.py', 'print(1)\n'), ('notes.md', '# a\n'), ('notes.md', '# a\nb\n'),
                              ('rows.csv', 'a\n'), ('notes.md', None)]))
    assert [[(b['id'], b.get('fmt'), b['area']) for b in s['blobs'] if b['kind'] != 'entry'] for s in inc] == [
        [], [('file:notes.md', 'md', 'docs')], [('file:notes.md', 'md', 'docs')],
        [('file:notes.md', 'md', 'docs'), ('file:rows.csv', 'csv', 'data')], [('file:rows.csv', 'csv', 'data')]]
    assert [{a['id'] for a in s.get('areas', ())} for s in v5['steps']][1:] == [{'docs'}, set(), {'data'}, set()]
