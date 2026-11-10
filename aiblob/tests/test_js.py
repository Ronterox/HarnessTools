"""JavaScript through analyze(): the js-mini Express fixture exactly, plus one JS heuristic per test."""
import os

import pytest

pytest.importorskip('tree_sitter_javascript')

from conftest import run, quals, call, edges_of

MINI = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'js-mini')


def load(root):
    files = {}
    for dp, _, fn in os.walk(root):
        for n in fn:
            p = os.path.join(dp, n)
            with open(p, encoding='utf-8') as fh:
                files[os.path.relpath(p, root).replace(os.sep, '/')] = fh.read()
    return dict(sorted(files.items()))


@pytest.fixture(scope='module')
def mini():
    return run(load(MINI))


def test_mini_blobs(mini):
    r, blobs, _ = mini
    assert r['entry'] == 'server'
    got = {b['id']: (b['kind'], b['name'], b['module'], quals(b)) for b in blobs.values()}
    assert got == {
        'server.<GET /items>': ('route', '/items', 'server', ['server.<GET /items>']),
        'server.<POST /items>': ('route', 'POST /items', 'server',
                                 ['server.<POST /items>', 'store.Store.add', 'util.isValidName']),
        'server.<use>': ('error-handler', 'error handler', 'server', ['server.<use>']),
        'server.<module>': ('entry', 'server', 'server', ['server.<module>']),
        'hub:store': ('hub', 'store', 'store', ['store.Store.list']),     # GET and POST (through Store.add) reach it
        'hub:util': ('hub', 'util', 'util', ['util.logError']),          # POST's catch and the error handler
        'file:README.md': ('text', 'README.md', '.', []),
        'file:public/index.html': ('file', 'index.html', 'public', []),
    }


def test_mini_errors_and_data(mini):
    _, blobs, _ = mini
    assert blobs['server.<POST /items>']['errors'] == 3      # res.status(400), next(err), Store.add's throw
    assert blobs['server.<use>']['errors'] == 1               # res.status(500)
    assert blobs['server.<POST /items>']['data'] == ['ctx:req.body', 'global:server.store',
                                                     'global:store.items', 'global:store.nextId']
    assert blobs['server.<module>']['data'] == ['ctx:env.PORT']
    assert blobs['hub:store']['data'] == ['global:store.items']


def test_mini_edges(mini):
    _, _, edges = mini
    assert [(e['a'], e['b'], e['kind'], e['n'], e.get('via')) for e in edges] == [
        ('server.<GET /items>', 'hub:store', 'main', 1, None),
        ('server.<POST /items>', 'hub:store', 'main', 1, None),
        ('server.<use>', 'hub:util', 'main', 1, None),
        ('server.<POST /items>', 'hub:util', 'error', 1, None),           # logError(err) inside catch
        ('server.<POST /items>', 'server.<use>', 'error', 1, None),       # next(err) -> error middleware
        ('hub:store', 'server.<POST /items>', 'data', 1, ['global:store.items']),
        ('server.<GET /items>', 'server.<POST /items>', 'data', 1, ['global:server.store']),
        ('server.<module>', 'file:public/index.html', 'ref', 1, ['public']),   # express.static('public')
    ]


def test_mini_calls(mini):
    _, blobs, _ = mini
    post = blobs['server.<POST /items>']
    assert call(post, 'server.<POST /items>', 'util.isValidName') == {'to': 'util.isValidName', 'error': False, 'deferred': True}
    assert call(post, 'server.<POST /items>', 'store.Store.add') == {'to': 'store.Store.add', 'error': False}
    assert call(post, 'server.<POST /items>', 'util.logError') == {'to': 'util.logError', 'error': True}
    assert call(post, 'store.Store.add', 'store.Store.list') == {'to': 'store.Store.list', 'error': False}   # this.list()
    assert call(blobs['server.<GET /items>'], 'server.<GET /items>', 'store.Store.list')['error'] is False


def test_mini_unresolved(mini):
    r, _, _ = mini
    assert (r['stats']['calls'], r['stats']['unresolved']) == (6, 1)   # the miss: next(err), a parameter


def test_router_mounted_with_prefix_and_named_handlers():
    r, blobs, edges = run({
        'app.js': '''const express = require('express');
const users = require('./routes/users');
const app = express();
function logger(req, res, next) { next(); }
app.use(logger);
app.use('/users', users);
app.get('/', home);
function home(req, res) { res.render('index'); }
app.listen(3000);
''',
        'routes/users.js': '''const router = require('express').Router();
router.get('/', (req, res) => res.json([]));
router.delete('/:id', (req, res) => res.sendStatus(204));
module.exports = router;
''',
        'views/index.ejs': '<h1>hi</h1>\n',
    })
    names = {b['id']: (b['kind'], b['name']) for b in blobs.values() if b['kind'] in ('route', 'hook')}
    assert names == {'app.logger': ('hook', 'middleware'), 'app.home': ('route', '/'),
                     'routes.users.<GET />': ('route', '/users'),
                     'routes.users.<DELETE /:id>': ('route', 'DELETE /users/:id')}
    assert edges_of(edges, 'ref') == [{'a': 'app.home', 'b': 'file:views/index.ejs', 'kind': 'ref', 'n': 1, 'via': ['index']}]


def test_plain_node_http_branches_become_routes():
    r, blobs, _ = run({'server.js': '''const http = require('node:http');
function createServer() {
  return http.createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/health') { res.end('ok'); return; }
    if (req.method === 'POST' && req.url === '/items') { if (!req.headers.x) { res.writeHead(400); res.end(); return; } res.end(); }
  });
}
if (require.main === module) createServer().listen(3000);
'''})
    assert {b['id']: b['name'] for b in blobs.values() if b['kind'] == 'route'} == {
        'server.<GET /health>': '/health', 'server.<POST /items>': 'POST /items'}
    assert blobs['server.<POST /items>']['errors'] == 1
    assert quals(blobs['server.<module>']) == ['server.<module>', 'server.createServer']


def test_esm_default_export_and_exported_instance_resolve():
    r, blobs, _ = run({
        'index.js': '''import express from 'express';
import save from './save.js';
import db from './db.js';
const app = express();
app.post('/x', (req, res) => { save(req.body); res.json(db.all()); });
app.listen(1);
''',
        'save.js': 'export default function save(x) { return x; }\n',
        'db.js': 'class Db { all() { return []; } }\nmodule.exports = new Db();\n',
    })
    assert quals(blobs['index.<POST /x>']) == ['db.Db.all', 'index.<POST /x>', 'save.save']


def test_test_files_are_a_tests_blob_of_carved_cases():
    r, blobs, edges = run({
        'lib.js': 'function add(a, b) { return a + b; }\nmodule.exports = { add };\n',
        'test/lib.test.js': "const { add } = require('../lib');\ndescribe('lib', () => { it('adds', () => { add(1, 2); }); });\n",
    })
    assert blobs['test.lib_test']['kind'] == 'tests'
    assert quals(blobs['test.lib_test']) == ['test.lib_test.<it adds>']
    assert edges_of(edges, 'main') == [{'a': 'test.lib_test', 'b': 'orphan:lib', 'kind': 'main', 'n': 1}]


def test_languages_do_not_link_across():
    r, blobs, edges = run({
        'app.py': 'from flask import Flask\napp = Flask(__name__)\n@app.route("/")\ndef home():\n    return helper()\n',
        'web/helper.js': 'function helper() { return 1; }\nmodule.exports = { helper };\n',
    })
    assert edges_of(edges, 'main') == []
    assert quals(blobs['app.home']) == ['app.home']
    assert blobs['orphan:web.helper']['kind'] == 'orphan'


def test_static_js_is_a_file_when_another_language_has_the_entry():
    ui_js = "function toggle(el) { el.hidden = !el.hidden; }\ndocument.addEventListener('click', toggle);\n"
    flask = {'app.py': 'from flask import Flask, url_for\napp = Flask(__name__)\n@app.route("/")\ndef home():\n'
                       '    return url_for("static", filename="ui.js")\n',
             'static/ui.js': ui_js}
    r, blobs, edges = run(flask)
    assert r['entry'] == 'app'
    assert blobs['file:static/ui.js']['kind'] == 'file'
    assert 'static.ui' not in r['modules'] and not any(k.endswith(':static.ui') for k in blobs)
    assert edges_of(edges, 'ref') == [{'a': 'app.home', 'b': 'file:static/ui.js', 'kind': 'ref', 'n': 1, 'via': ['ui.js']}]
    # no entry anywhere: the same file is code
    r, blobs, _ = run({'static/ui.js': ui_js})
    assert r['entry'] is None and 'static.ui' in r['modules'] and 'file:static/ui.js' not in blobs
    # a JS entry: public/ scripts stay code next to it
    r, blobs, _ = run({'server.js': "const app = require('express')();\napp.listen(1);\n", 'public/ui.js': ui_js})
    assert r['entry'] == 'server' and 'public.ui' in r['modules'] and 'file:public/ui.js' not in blobs


MINI2 = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'js-mini2')


@pytest.fixture(scope='module')
def mini2():
    return run(load(MINI2))


def test_mini2_regex_routes_and_chains(mini2):
    r, blobs, _ = mini2
    assert r['entry'] == 'server'
    got = {b['id']: (b['kind'], b['name']) for b in blobs.values()}
    assert got == {
        'server.<GET /users/(\\d+)>': ('route', '/users/(\\d+)'),       # app.get(/^\/users\/(\d+)$/, …)
        'server.<GET /files/:name>': ('route', '/files/:name'),         # '/files/:name(\\w+)': the param regex is dropped
        'server.<POST /users>': ('route', 'POST /users'),
        'server.<module>': ('entry', 'server'),
        'notes.list': ('route', '/notes'),                              # router.route('/').get(list).post(create)
        'notes.create': ('route', 'POST /notes'),
        'notes.audit': ('hook', 'middleware /notes/:id'),               # .all(audit)
        'notes.show': ('route', '/notes/:id'),
        'notes.<DELETE /:id>': ('route', 'DELETE /notes/:id'),          # inline verb handler of a chain
    }


def test_mini2_chain_hook_is_attached_to_each_verb(mini2):
    _, _, edges = mini2
    assert [(e['a'], e['b']) for e in edges_of(edges, 'main')] == [
        ('notes.<DELETE /:id>', 'notes.audit'), ('notes.show', 'notes.audit')]


def test_mini2_closure_state_is_data(mini2):
    _, blobs, edges = mini2
    assert blobs['server.<GET /users/(\\d+)>']['data'] == ['ctx:req.params', 'global:server.users']
    assert blobs['server.<GET /files/:name>']['data'] == ['ctx:req.params', 'global:server.cache', 'global:server.hits']   # LIMIT: a plain const
    assert blobs['server.<POST /users>']['data'] == ['ctx:req.body', 'global:server.users']
    assert blobs['server.<module>']['data'] == ['ctx:env.VERB', 'global:server.seed', 'global:server.users']   # users.push(...seed) at module level
    assert blobs['notes.show']['data'] == ['ctx:req.params', 'global:notes.items']
    assert not any(d.endswith(('.app', '.router', '.notes', '.verb', '.LIMIT')) for b in blobs.values() for d in b['data'])
    data = {(e['a'], e['b']): e['via'] for e in edges_of(edges, 'data')}
    assert data[('server.<GET /users/(\\d+)>', 'server.<POST /users>')] == ['global:server.users']
    assert data[('server.<POST /users>', 'server.<module>')] == ['global:server.users']
    assert data[('notes.create', 'notes.list')] == ['global:notes.items']
    assert data[('notes.<DELETE /:id>', 'notes.show')] == ['global:notes.items']
    assert not any(a.startswith('server.') != b.startswith('server.') for a, b in data)     # no state shared across modules


def test_mini2_dynamic_registration_is_unresolved(mini2):
    r, blobs, _ = mini2
    assert (r['stats']['calls'], r['stats']['unresolved']) == (2, 2)     # app[verb]('/dynamic', h) and next() in audit
    assert 'server.<GET /dynamic>' not in blobs and not any('dynamic' in k for k in blobs)


def test_route_chain_with_inline_handlers_and_dynamic_only():
    r, blobs, edges = run({'app.js': '''const app = require('express')();
const log = [];
app.route('/a').all((req, res, next) => { log.push(1); next(); }).get((req, res) => res.json(log)).put((req, res) => { log.length = 0; res.end(); });
app[process.env.VERB]('/b', (req, res) => res.end());
app.listen(1);
'''})
    assert {b['id']: (b['kind'], b['name']) for b in blobs.values() if b['kind'] != 'entry'} == {
        'app.<use /a>': ('hook', 'middleware /a'), 'app.<GET /a>': ('route', '/a'), 'app.<PUT /a>': ('route', 'PUT /a')}
    assert [(e['a'], e['b']) for e in edges_of(edges, 'main')] == [('app.<GET /a>', 'app.<use /a>'), ('app.<PUT /a>', 'app.<use /a>')]
    assert (r['stats']['calls'], r['stats']['unresolved']) == (2, 2)      # the dynamic one, next()


def test_ts_typed_state_and_collection_calls_are_data():
    _, blobs, _ = run({'app.ts': '''import express from 'express';
const app = express();
const users = [] as string[];
const index = Object.create(null);
const NAME = 'x';
app.get('/u', (req, res) => { res.json(users.concat(index[NAME])); });
app.listen(1);
'''})
    assert blobs['app.<GET /u>']['data'] == ['global:app.index', 'global:app.users']


def test_dynamic_registration_never_resolves_to_a_function_of_that_name():
    r, blobs, edges = run({'app.js': '''const app = require('express')();
function dynamic_route() {}
const m = 'get';
app[m]('/p', (req, res) => res.end());
app.listen(1);
'''})
    assert (r['stats']['calls'], r['stats']['unresolved']) == (1, 1)
    assert edges_of(edges, 'main') == [] and not any(c['to'].endswith('dynamic_route') for b in blobs.values() for f in b['funcs'] for c in f['calls'])


def test_only_strings_that_read_like_sql_name_tables():
    r, blobs, edges = run({'server.js': '''const express = require('express');
const db = require('./db');
const app = express();
const SCHEMA = `CREATE TABLE IF NOT EXISTS notes (id INTEGER)`;
const HELP = 'Select the rows from the table, update them from scratch';

app.get('/a', (req, res) => {
  'use strict';
  'SELECT * FROM ghosts';
  res.send('Delete from the list; read from scratch' + HELP);
});

app.get('/b', (req, res) => {
  res.json(db.prepare('SELECT n.id FROM notes n ' +
                      'JOIN users u ON u.id = n.uid').all(req.query.u));
});

app.listen(3000);
''', 'db.js': 'module.exports = { prepare(q) { return { all() { return q.from("the cache"); } }; } };\n'})
    assert blobs['server.<GET /a>']['data'] == []
    assert blobs['server.<GET /b>']['data'] == ['ctx:req.query', 'table:notes', 'table:users']   # a `+` chain: one string
    assert [(e['a'], e['b'], e['via']) for e in edges_of(edges, 'data')] == [   # the schema's table, nothing else
        ('server.<GET /b>', 'server.<module>', ['table:notes'])]
    _, joined, _ = run({'q.js': "function q(db, t) {\n  return db.all('SELECT * FROM ' + t + ' JOIN users u ON u.id = 1');\n}\n"
                                "module.exports = { q };\n"})
    assert joined['orphan:q']['data'] == ['table:users']       # a value between the pieces is no table, nor glues them
