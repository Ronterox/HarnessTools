"""Small inline snippets through analyze(): one heuristic per test, exact blobs / edges / calls."""
import pytest

from conftest import run, quals, call, edges_of

FLASK = 'from flask import Flask, abort, render_template, session\napp = Flask(__name__)\n'


def test_route_blob_owns_private_helper_and_shared_one_goes_to_hub():
    r, blobs, edges = run({
        'app.py': FLASK + '''
import models

@app.route("/a")
def a():
    models.only_a()
    return models.shared()

@app.route("/b")
def b():
    return models.shared()
''',
        'models.py': 'def only_a(): return 1\ndef shared(): return helper()\ndef helper(): return 2\n',
    })
    assert blobs['app.a']['kind'] == 'route' and blobs['app.a']['name'] == '/a'
    assert quals(blobs['app.a']) == ['app.a', 'models.only_a']          # reached from one root: owned
    assert quals(blobs['app.b']) == ['app.b']
    assert blobs['hub:models']['kind'] == 'hub'
    assert quals(blobs['hub:models']) == ['models.helper', 'models.shared']   # reached from two roots: hub
    assert [(e['a'], e['b'], e['n']) for e in edges_of(edges, 'main')] == [
        ('app.a', 'hub:models', 1), ('app.b', 'hub:models', 1)]


def test_error_edge_comes_from_the_abort_tail_only():
    r, blobs, edges = run({
        'app.py': FLASK + '''
import models

@app.route("/a")
def a():
    models.before()
    item = models.find()
    if item is None:
        models.audit()
        abort(404)
    models.after()
    return "ok"

@app.route("/b")
def b():
    models.before()
    models.audit()
    models.find()
    models.after()
    return "ok"

@app.errorhandler(404)
def not_found(e):
    return "nf", 404
''',
        'models.py': 'def before(): pass\ndef find(): pass\ndef audit(): pass\ndef after(): pass\n',
    })
    a = blobs['app.a']
    assert call(a, 'app.a', 'models.before')['error'] is False
    assert call(a, 'app.a', 'models.find')['error'] is False
    assert call(a, 'app.a', 'models.after')['error'] is False
    assert call(a, 'app.a', 'models.audit')['error'] is True               # inside the branch that ends in abort
    assert call(blobs['app.b'], 'app.b', 'models.audit')['error'] is False  # same callee, no abort branch
    # one error edge per abort(404) to the 404 handler; the hub edges stay main except the branch's audit
    assert [(e['a'], e['b'], e['n']) for e in edges_of(edges, 'error')] == [
        ('app.a', 'app.not_found', 1), ('app.a', 'hub:models', 1)]
    assert edges_of(edges, 'main', 'app.a', 'hub:models')[0]['n'] == 3     # before, find, after
    assert edges_of(edges, 'main', 'app.b', 'hub:models')[0]['n'] == 4


def test_call_before_an_early_normal_return_in_the_branch_is_not_error():
    r, blobs, _ = run({
        'app.py': FLASK + '''
import models

@app.route("/a")
def a():
    if models.broken():
        models.warm()
        if models.ok():
            return "fine"
        models.cleanup()
        abort(500)
    return "x"
''',
        'models.py': 'def broken(): pass\ndef warm(): pass\ndef ok(): pass\ndef cleanup(): pass\n',
    })
    a = blobs['app.a']
    assert call(a, 'app.a', 'models.broken')['error'] is False
    assert call(a, 'app.a', 'models.warm')['error'] is False      # runs on the success path too
    assert call(a, 'app.a', 'models.ok')['error'] is False
    assert call(a, 'app.a', 'models.cleanup')['error'] is True    # only reached on the way to abort


def test_data_edge_via_table_name_and_via_session_key():
    r, blobs, edges = run({
        'app.py': FLASK + '''
import db

@app.route("/x")
def x():
    db.run("SELECT * FROM users WHERE id = 1")
    session["uid"] = 1
    return db.run("SELECT 1 FROM widgets")

@app.route("/y")
def y():
    db.run("INSERT INTO users (a) VALUES (1)")
    return str(session.get("uid"))

@app.route("/z")
def z():
    session.clear()
    return "gone"
''',
        'db.py': 'def run(q): pass\n',
    })
    assert blobs['app.x']['data'] == ['ctx:session.uid', 'table:users', 'table:widgets']
    assert blobs['app.y']['data'] == ['ctx:session.uid', 'table:users']
    assert blobs['app.z']['data'] == ['ctx:session']        # session.clear() is a whole-session touch
    data = {(e['a'], e['b']): e['via'] for e in edges_of(edges, 'data')}
    assert data[('app.x', 'app.y')] == ['ctx:session.uid', 'table:users']
    assert data[('app.x', 'app.z')] == data[('app.y', 'app.z')] == ['ctx:session.uid']   # ...so it touches every key
    assert 'table:users' not in data[('app.x', 'app.z')]


def test_callback_passed_as_key_is_a_deferred_call():
    r, blobs, edges = run({
        'app.py': FLASK + '''
import util

@app.route("/k")
def k():
    return sorted([3, 1], key=util.score)
''',
        'util.py': 'def score(x): return x\n',
    })
    c = call(blobs['app.k'], 'app.k', 'util.score')
    assert c == {'to': 'util.score', 'error': False, 'deferred': True}
    assert quals(blobs['app.k']) == ['app.k', 'util.score']   # reachable like a called one


def test_called_function_is_not_marked_deferred():
    r, blobs, _ = run({
        'app.py': FLASK + 'import util\n\n@app.route("/k")\ndef k():\n    return util.score(1)\n',
        'util.py': 'def score(x): return x\n',
    })
    assert 'deferred' not in call(blobs['app.k'], 'app.k', 'util.score')


def test_method_call_on_constructor_typed_local_resolves():
    r, blobs, _ = run({
        'app.py': FLASK + '''
from store import Store

@app.route("/c")
def c():
    s = Store()
    return s.put(1)
''',
        'store.py': 'class Store:\n    def put(self, x):\n        return self.inner()\n    def inner(self):\n        return 1\n',
    })
    assert call(blobs['app.c'], 'app.c', 'store.Store.put')['error'] is False
    assert quals(blobs['app.c']) == ['app.c', 'store.Store.inner', 'store.Store.put']
    assert r['stats']['unresolved'] == 0


def test_argparse_set_defaults_subcommands_are_roots():
    r, blobs, _ = run({
        'cli.py': '''import argparse

def build(a): return 1
def clean(a): return 2

def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    sub.add_parser("build").set_defaults(func=build)
    c = sub.add_parser("clean")
    c.set_defaults(func=clean)
    args = p.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
''',
    })
    assert (blobs['cli.build']['kind'], blobs['cli.build']['name']) == ('command', 'build')
    assert (blobs['cli.clean']['kind'], blobs['cli.clean']['name']) == ('command', 'clean')
    assert quals(blobs['cli.build']) == ['cli.build']
    assert 'cli.main' not in quals(blobs['cli.build']) + quals(blobs['cli.clean'])


def test_click_command_name_is_the_decorator_string_or_the_function_name():
    r, blobs, _ = run({
        'cli.py': '''import click

@click.group()
def cli(): pass

@cli.command()
def sync(): return 1

@cli.command("purge-all")
def purge(): return 2

if __name__ == "__main__":
    cli()
''',
    })
    assert (blobs['cli.sync']['kind'], blobs['cli.sync']['name']) == ('command', 'sync')
    assert (blobs['cli.purge']['kind'], blobs['cli.purge']['name']) == ('command', 'purge-all')


def test_template_named_by_render_template_is_a_file_blob_with_a_ref_edge():
    r, blobs, edges = run({
        'app.py': FLASK + '@app.route("/")\ndef home():\n    return render_template("home.html")\n',
        'templates/home.html': '<p>hi</p>\n',
    })
    assert blobs['file:templates/home.html']['kind'] == 'file'
    assert blobs['file:templates/home.html']['name'] == 'home.html'
    assert blobs['file:templates/home.html']['module'] == 'templates'
    assert edges_of(edges, 'ref') == [{'a': 'app.home', 'b': 'file:templates/home.html', 'kind': 'ref', 'n': 1, 'via': ['home.html']}]


def test_extends_counts_one_reference():
    r, blobs, edges = run({
        'app.py': FLASK + '''
@app.route("/")
def home():
    return render_template("home.html")

@app.route("/b")
def other():
    return "b"

@app.route("/c")
def third():
    return "c"
''',
        'templates/home.html': '{% extends "base.html" %}\n',
        'templates/base.html': '<html></html>\n',
    })
    e = edges_of(edges, 'ref', 'file:templates/home.html', 'file:templates/base.html')
    assert [(x['n'], x['via']) for x in e] == [(1, ['base.html'])]


def test_unreferenced_readme_is_a_text_blob_without_edges():
    r, blobs, edges = run({
        'app.py': FLASK + '@app.route("/")\ndef home():\n    return "x"\n',
        'README.md': '# demo\nnothing here\n',
    })
    b = blobs['file:README.md']
    assert (b['kind'], b['name'], b['module'], b['size'], b['funcs']) == ('text', 'README.md', '.', 2, [])
    assert not [e for e in edges if 'file:README.md' in (e['a'], e['b'])]


def test_half_written_file_does_not_crash_and_the_rest_is_analysed():
    r, blobs, edges = run({
        'good.py': 'def f(): return 1\n',
        'bad.py': 'def broken(:\n  x = (\n',
        'cut.py': 'def ok(): return 1\n\ndef half(a,\n',
        'app.py': FLASK + 'import good\n\n@app.route("/")\ndef i():\n    return good.f()\n',
    })
    assert quals(blobs['app.i']) == ['app.i', 'good.f']
    assert 'cut.ok' in {f['qual'] for b in r['blobs'] for f in b['funcs']}


def test_module_level_sql_global_propagates_table_symbols():
    r, blobs, edges = run({
        'app.py': FLASK + '''
import db

@app.route("/1")
def one():
    return db.q1()

@app.route("/2")
def two():
    return db.q2()

@app.route("/3")
def three():
    return db.q3()
''',
        'db.py': 'Q = "SELECT * FROM users"\n\ndef q1(): return Q\ndef q2(): return Q\ndef q3(): return 1\n',
    })
    assert 'table:users' in blobs['app.one']['data'] and 'table:users' in blobs['app.two']['data']
    assert not any(d.startswith('table:') for d in blobs['app.three']['data'])
    data = edges_of(edges, 'data')
    assert [(e['a'], e['b']) for e in data] == [('app.one', 'app.two')]
    assert 'table:users' in data[0]['via']


def test_sql_global_reaches_the_user_across_modules():
    r, blobs, _ = run({
        'app.py': FLASK + 'import db\n\n@app.route("/1")\ndef one():\n    return db.run(db.Q_USERS)\n',
        'db.py': 'Q_USERS = "SELECT * FROM users"\ndef run(q): pass\n',
    })
    assert 'table:users' in blobs['app.one']['data']


def test_prose_and_docstrings_name_no_tables_real_sql_does():
    from analyze import sql_tables
    r, blobs, edges = run({
        'app.py': FLASK + '''
import db

@app.route("/a")
def a():
    """Select the rows from the table, update the cache from scratch."""
    db.rows("Usage: " "SELECT * FROM ghosts")             # a piece reads like SQL, the whole string does not
    return db.rows("Delete from the list; with --full, recompute from scratch")

@app.route("/b")
def b():
    """SELECT * FROM users"""
    flash("Update steps from a log")
    return db.rows("SELECT n.id FROM notes n "
                   "JOIN users u ON u.id = n.uid")
''',
        'db.py': 'def rows(q):\n    "INSERT INTO ghosts VALUES (1)"\n    return q\n',
    })
    assert blobs['app.a']['data'] == [] and edges_of(edges, 'data') == []
    assert blobs['app.b']['data'] == ['table:notes', 'table:users']          # a concatenation counts as one string
    assert 'table:ghosts' not in blobs['hub:db']['data']                     # a docstring is never SQL
    assert sql_tables('-- note\n/* x */ insert or replace into "Kv" values (1)') == {'table:kv'}
    assert sql_tables('UPDATE OR IGNORE `t1` SET x = 1') == {'table:t1'}
    assert sql_tables('DROP TABLE IF EXISTS old; CREATE TEMP TABLE [new] (x)') == {'table:old', 'table:new'}
    assert sql_tables('WITH r AS (SELECT a FROM b) SELECT * FROM r') == {'table:b', 'table:r'}
    assert sql_tables('SELECT x FROM the -- from a table') == set()       # a keyword or English word is no table
    for prose in ('Select the rows from users', 'Update users from git', 'update notes from git',
                  'With t as (x) from users', 'from users select', 'select', 'PRAGMA foreign_keys = ON',
                  'explain steps from cache'):
        assert sql_tables(prose) == set(), prose
    # schema-qualified names keep their last part; BEGIN [TRANSACTION]; leads like a comment; EXPLAIN, TRUNCATE
    assert sql_tables('SELECT * FROM main.notes JOIN public.users u') == {'table:notes', 'table:users'}
    assert sql_tables('select * from `db`.`tbl`') == {'table:tbl'} == sql_tables('SELECT * FROM [dbo].[Tbl]')
    assert sql_tables('UPDATE main.notes SET x = 1') == {'table:notes'}
    assert sql_tables('BEGIN;\nINSERT INTO kv VALUES (1);\nCOMMIT;') == {'table:kv'}
    assert sql_tables('begin immediate transaction; delete from kv') == {'table:kv'}
    assert sql_tables('EXPLAIN QUERY PLAN SELECT * FROM notes') == {'table:notes'}
    assert sql_tables('TRUNCATE TABLE logs') == {'table:logs'} == sql_tables('truncate logs')


def test_sql_comments_cost_linear_time():
    import time
    from analyze import sql_tables
    rules = ''.join(f'/* rule {i} */ .c{i} {{ color: red; }}\n' for i in range(40))
    t = time.perf_counter()
    assert sql_tables('/* a */' * 40 + ' hello') == set() and sql_tables('/* a ' * 2000) == set()
    assert sql_tables('/* a */' * 40 + ' SELECT * FROM t') == {'table:t'}
    run({'style.py': 'STYLE = """\n' + rules + '"""\n'})
    assert time.perf_counter() - t < 1.0      # was exponential in the comments: 22 of them took 3.5 s


def test_flask_context_only_where_flask_imports_it():
    r, blobs, _ = run({
        'app.py': FLASK + '''from flask import g, request
import util

@app.route("/a")
def a():
    g.user = util.walk()
    return request.args.get("q")

@app.route("/b")
def b(request):
    g = request.node
    return g.qual
''',
        'util.py': 'def walk():\n    g = make()\n    return g.qual, session.uid\n\ndef make(): return 1\n',
    })
    assert blobs['app.a']['data'] == ['ctx:g.user', 'ctx:request.args']
    assert blobs['app.b']['data'] == []                    # a parameter `request`, a local `g`: not Flask's
    assert not any(d.startswith('ctx:') for b in r['blobs'] for d in b['data'] if b['module'] == 'util')
    _, other, _ = run({'app.py': FLASK + 'from mylib import session\n\n@app.route("/a")\ndef a():\n    return session.query(1)\n'})
    assert other['app.a']['data'] == []                    # a `session` from elsewhere is not Flask's


def test_flask_context_names_bound_in_nested_scopes_shadow_only_there():
    r, blobs, _ = run({'app.py': FLASK + '''from flask import g, request

@app.route("/a")
def a():
    f = lambda g: g.x
    xs = [request for request in range(3)]
    def inner(request):
        return request.form
    class V:
        g = 1
        def m(self):
            return g.inner
    return f(1), g.y, request.args, inner(2), xs

@app.route("/b")
def b():
    try:
        pass
    except Exception as session:      # binds `session` in b's own scope (Python 3): never Flask's in b
        pass
    return g.z, session.get("u")
''', 'other.py': 'from flask import g\ng = None\n\ndef h():\n    return g.user\n'})
    assert blobs['app.a']['data'] == ['ctx:g.inner', 'ctx:g.y', 'ctx:request.args']
    assert blobs['app.b']['data'] == ['ctx:g.z']
    assert not any(d.startswith('ctx:') for b in r['blobs'] if b['module'] == 'other' for d in b['data'])   # rebound


def test_every_main_guard_is_an_entry_besides_the_app_entry():
    import os
    d = os.path.join(os.path.dirname(__file__), 'fixtures', 'py-mains')
    r, blobs, edges = run({os.path.relpath(os.path.join(p, n), d): open(os.path.join(p, n)).read()
                           for p, _, ns in os.walk(d) for n in sorted(ns) if n.endswith('.py')})
    assert r['entry'] == 'web'                            # no guard: the app a runner starts, as before
    assert sorted(k for k, b in blobs.items() if b['kind'] == 'entry') == ['cli.<module>', 'tool.<module>', 'web.<module>']
    assert quals(blobs['cli.<module>']) == ['cli.<module>', 'cli.main']    # main(), called from the guard, joins it
    assert quals(blobs['tool.<module>']) == ['core.words', 'tool.<module>', 'tool.run']
    assert quals(blobs['hub:core']) == ['core.connect', 'core.count']    # reached by two programs: shared
    assert blobs['tests.check_core']['kind'] == 'tests'                # a test module's guard makes no entry
    assert quals(blobs['tests.check_core']) == ['tests.check_core.test_words']   # (nor runs its module code)
    assert not any(k.startswith('orphan:') for k in blobs)
    assert blobs['hub:core']['data'] == ['global:core.SCHEMA', 'table:notes']
    _, pkg, _ = run({'pkg/__init__.py': '', 'pkg/__main__.py': 'from pkg import work\nwork.run()\n',
                     'pkg/work.py': 'def run():\n    return 1\n',   # four more: not the biggest-closure app entry
                     **{f'lib/m{i}.py': 'X = 1\n' for i in range(4)}})
    assert pkg['pkg.__main__.<module>']['kind'] == 'entry'     # `python -m pkg` runs it: always a program
    assert quals(pkg['pkg.__main__.<module>']) == ['pkg.__main__.<module>', 'pkg.work.run']


def test_instances_bound_in_a_tuple_assignment_are_typed_pair_by_pair():
    r, blobs, _ = run({
        'app.py': FLASK + '''
from store import Store, Log

class Svc:
    def __init__(self):
        self.n, self.store = 0, Store()

    def save(self):
        return self.store.put(1)

@app.route("/c")
def c():
    a, log = 1, Log()
    log.put(a)
    return Svc().save()
''',
        'store.py': 'class Store:\n    def put(self, x):\n        return x\n\nclass Log:\n    def put(self, x):\n        return x\n',
    })   # both classes have `put`: no guess by a unique method name, only the types place the calls
    assert call(blobs['app.c'], 'app.Svc.save', 'store.Store.put') and call(blobs['app.c'], 'app.c', 'store.Log.put')
    assert r['stats']['unresolved'] == 0
