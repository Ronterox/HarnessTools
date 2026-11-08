"""Go through analyze(): the go-mini fixture exactly, plus one Go heuristic per test."""
import os

import pytest

pytest.importorskip('tree_sitter_go')

from conftest import run, quals, call, edges_of
from test_c import load
from test_incremental import check, sequence

MINI = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'go-mini')


@pytest.fixture(scope='module')
def mini():
    return run(load(MINI))


def test_mini_blobs(mini):
    r, blobs, _ = mini
    assert r['entry'] == 'main'
    got = {b['id']: (b['kind'], b['name'], b['module'], quals(b)) for b in blobs.values()}
    assert got == {
        # handlers are methods passed as values to mux.HandleFunc: id = the method's qual; Go 1.22 "POST /p" patterns
        'handlers.notes.Handler.ListNotes': ('route', '/notes', 'handlers.notes',
                                             ['handlers.notes.Handler.ListNotes', 'store.store.DB.AllNotes']),
        'handlers.notes.Handler.CreateNote': ('route', 'POST /notes', 'handlers.notes',
                                              ['handlers.notes.Handler.CreateNote', 'store.store.DB.Insert']),
        # an inline func literal is carved out of main(); db.Ping() is typed through store.Open's *DB result
        'main.<ANY /health>': ('route', '/health', 'main', ['main.<ANY /health>', 'store.store.DB.Ping']),
        # main() with the file's package-level code and init(); `go store.Vacuum(db)` is a (deferred) call
        'main.<module>': ('entry', 'main', 'main', ['handlers.notes.New', 'main.<module>', 'main.init',
                                                    'main.logRequests', 'main.main', 'store.store.Open',
                                                    'store.store.Vacuum']),
        'store.store_test': ('tests', 'store_test', 'store.store_test', ['store.store_test.TestInsertEmpty']),
        # the shared helpers of package handlers live in another file than their callers
        'hub:handlers.util': ('hub', 'util', 'handlers.util', ['handlers.util.WriteJSON', 'handlers.util.writeError']),
        'file:static/app.css': ('file', 'app.css', 'static', []),             # http.Dir("static")
        'file:templates/notes.html': ('file', 'notes.html', 'templates', []),  # template.ParseFiles via var tmpl
    }
    assert 'go_mod' in r['modules'] and not any(b['id'] == 'file:go.mod' for b in blobs.values())


def test_mini_errors_and_data(mini):
    _, blobs, _ = mini
    assert blobs['handlers.notes.Handler.ListNotes']['errors'] == 3     # writeError(); return + AllNotes' 2 return nil, err
    assert blobs['handlers.notes.Handler.CreateNote']['errors'] == 3    # http.Error(); return, writeError(); return, return ErrEmpty
    assert blobs['main.<ANY /health>']['errors'] == 1
    assert blobs['main.<module>']['errors'] == 3                        # 2 log.Fatal, Open's return nil, fmt.Errorf(…)
    assert blobs['handlers.notes.Handler.ListNotes']['data'] == ['global:handlers.Hits', 'global:handlers.tmpl',
                                                                 'table:notes']
    assert blobs['handlers.notes.Handler.CreateNote']['data'] == ['global:store.ErrEmpty', 'table:notes']
    assert blobs['main.<module>']['data'] == ['env:DB_PATH', 'global:handlers.Hits', 'global:main.addr']


def test_mini_edges(mini):
    _, _, edges = mini
    assert [(e['a'], e['b'], e['kind'], e['n'], e.get('via')) for e in edges] == [
        ('handlers.notes.Handler.CreateNote', 'hub:handlers.util', 'main', 1, None),
        ('main.<ANY /health>', 'hub:handlers.util', 'main', 1, None),
        ('main.<module>', 'handlers.notes.Handler.CreateNote', 'main', 1, None),     # registered as a value
        ('main.<module>', 'handlers.notes.Handler.ListNotes', 'main', 1, None),
        ('store.store_test', 'handlers.notes.Handler.CreateNote', 'main', 1, None),  # the test calls d.Insert
        ('handlers.notes.Handler.CreateNote', 'hub:handlers.util', 'error', 1, None),
        ('handlers.notes.Handler.ListNotes', 'hub:handlers.util', 'error', 1, None),
        ('handlers.notes.Handler.CreateNote', 'handlers.notes.Handler.ListNotes', 'data', 1, ['table:notes']),
        ('handlers.notes.Handler.ListNotes', 'main.<module>', 'data', 1, ['global:handlers.Hits']),
        ('handlers.notes.Handler.ListNotes', 'file:templates/notes.html', 'ref', 1, ['templates/notes.html']),
        ('main.<module>', 'file:static/app.css', 'ref', 1, ['static']),
    ]


def test_mini_calls(mini):
    _, blobs, _ = mini
    main = blobs['main.<module>']
    assert call(main, 'main.main', 'store.store.Open') == {'to': 'store.store.Open', 'error': False}
    assert call(main, 'main.main', 'store.store.Vacuum') == {'to': 'store.store.Vacuum', 'error': False, 'deferred': True}
    assert call(main, 'main.main', 'handlers.notes.Handler.ListNotes')['deferred'] is True
    assert call(main, 'main.main', 'main.init') == {'to': 'main.init', 'error': False}
    route = blobs['handlers.notes.Handler.ListNotes']
    # h.db.AllNotes(): receiver *Handler, field db *store.DB, method of store.DB in another package
    assert call(route, 'handlers.notes.Handler.ListNotes', 'store.store.DB.AllNotes') == \
        {'to': 'store.store.DB.AllNotes', 'error': False}
    assert call(route, 'handlers.notes.Handler.ListNotes', 'handlers.util.writeError')['error'] is True
    assert next(f for f in route['funcs'] if f['qual'] == 'handlers.notes.Handler.ListNotes')['refs'] == \
        ['templates/notes.html']


def test_mini_unresolved(mini):
    r, _, _ = mini
    # 13 calls that can be the program's own; the misses are setupMetrics() and the test's checkSchema(t).
    # http / log / os / strconv / sql / fmt calls and methods on their values (mux, r, w, d.conn) never count
    assert (r['stats']['calls'], r['stats']['unresolved']) == (13, 2)


GIN = '''package main

import (
	"net/http"

	"github.com/gin-gonic/gin"
	"github.com/me/api/svc"
)

func main() {
	r := gin.Default()
	r.Use(svc.Auth())
	api := r.Group("/api")
	api.GET("/users/:id", getUser)
	api.POST("/users", func(c *gin.Context) {
		var u svc.User
		if err := c.BindJSON(&u); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
			return
		}
		svc.Save(u)
	})
	r.NoRoute(func(c *gin.Context) { c.JSON(404, nil) })
	r.Run()
}

func getUser(c *gin.Context) {
	s := svc.NewStore()
	u, err := s.Find(c.Param("id"))
	if err != nil {
		c.AbortWithStatus(404)
		return
	}
	u.Touch()
	var rep svc.Repo = s
	rep.Load()
	svc.Gone()
}
'''
SVC = '''package svc

import (
	"errors"

	"github.com/gin-gonic/gin"
)

type User struct{ Name string }
type Store struct{ users map[string]User }
type Repo interface{ Load() error }

func NewStore() *Store { return &Store{} }
func (s *Store) Find(id string) (User, error) {
	if id == "" {
		return User{}, errors.New("empty")
	}
	return s.users[id], nil
}
func (s *Store) Load() error { return nil }
func (u User) Touch() {}
func Save(u User) {}
func Auth() gin.HandlerFunc { return func(c *gin.Context) { c.Next() } }
'''


def test_gin_groups_inline_handlers_hooks_and_types():
    r, blobs, _ = run({'go.mod': 'module github.com/me/api\n', 'main.go': GIN, 'svc/svc.go': SVC})
    got = {b['id']: (b['kind'], b['name']) for b in blobs.values() if b['kind'] != 'hub'}
    assert got == {
        'main.getUser': ('route', '/api/users/:id'),               # r.Group("/api") prefix
        'main.<POST /api/users>': ('route', 'POST /api/users'),
        'main.<error 404>': ('error-handler', 'error 404'),        # r.NoRoute(func …)
        'svc.svc.Auth': ('hook', 'middleware'),                    # r.Use(svc.Auth()): the call's callee
        'main.<module>': ('entry', 'main'),
    }
    get = blobs['main.getUser']
    # s := svc.NewStore() types s; u is Find's first result (User); rep is the Repo interface: its one implementation
    assert [c['to'] for c in next(f for f in get['funcs'] if f['qual'] == 'main.getUser')['calls']] == \
        ['svc.svc.NewStore', 'svc.svc.Store.Find', 'svc.svc.User.Touch', 'svc.svc.Store.Load']
    assert get['errors'] == 2                                     # AbortWithStatus(404); return + Find's errors.New
    assert blobs['main.<POST /api/users>']['errors'] == 1         # c.JSON(http.StatusBadRequest, …); return
    # gin / http calls and c.* / r.* methods are external; svc.Gone() is a missing function of the program
    assert (r['stats']['calls'], r['stats']['unresolved']) == (7, 1)


def test_chi_route_blocks_with_and_use():
    _, blobs, edges = run({'go.mod': 'module m\n', 'routes.go': '''package main

import "github.com/go-chi/chi/v5"

func main() {
	r := chi.NewRouter()
	r.Route("/admin", func(r chi.Router) {
		r.Get("/stats", stats)
		r.With(mw).Post("/reset", reset)
	})
	r.Use(mw)
}
func stats(w, r any) {}
func reset(w, r any) {}
func mw(next any) any { return next }
'''})
    assert {b['id']: (b['kind'], b['name']) for b in blobs.values()} == {
        'routes.stats': ('route', '/admin/stats'), 'routes.reset': ('route', 'POST /admin/reset'),
        'routes.mw': ('hook', 'middleware'), 'routes.<module>': ('entry', 'routes')}
    assert edges_of(edges, 'main', 'routes.<module>', 'routes.mw')[0]['n'] == 2   # With(mw) and Use(mw), deferred


def test_cobra_and_urfave_commands():
    _, blobs, _ = run({'go.mod': 'module m\n', 'main.go': '''package main

import (
	"os"

	"github.com/spf13/cobra"
	"github.com/urfave/cli/v2"
)

func main() {
	root := &cobra.Command{Use: "app", RunE: serve}
	root.AddCommand(&cobra.Command{Use: "migrate [dir]", Run: func(cmd *cobra.Command, args []string) {
		migrate(os.Getenv("DSN"))
	}})
	app := &cli.App{Commands: []*cli.Command{{Name: "sync", Action: syncAll}}}
	root.Execute()
	app.Run(os.Args)
}
func serve(cmd *cobra.Command, args []string) error { return nil }
func migrate(dsn string) {}
func syncAll(c *cli.Context) error { return nil }
'''})
    assert {b['id']: (b['kind'], b['name']) for b in blobs.values() if b['kind'] == 'command'} == {
        'main.serve': ('command', 'app'), 'main.<cmd migrate>': ('command', 'migrate'),
        'main.syncAll': ('command', 'sync')}
    assert blobs['main.<cmd migrate>']['data'] == ['env:DSN']
    assert quals(blobs['main.<cmd migrate>']) == ['main.<cmd migrate>', 'main.migrate']


def test_error_tails_are_strict():
    _, blobs, _ = run({'go.mod': 'module m\n', 'main.go': '''package main

import (
	"fmt"
	"log"
	"os"
)

func main() { run(1) }
func run(x int) (int, error) {
	if x == 0 {
		return 0, fmt.Errorf("zero")
	}
	if x == 1 {
		report(x)
		return 0, nil
	}
	if x == 2 {
		cleanup()
		os.Exit(1)
	}
	if x == 3 {
		log.Printf("three")
		note()
	}
	if x == 4 {
		panic("four")
	}
	return x, nil
}
func report(x int) {}
func cleanup()     {}
func note()        {}
'''})
    run_f = next(f for f in blobs['main.<module>']['funcs'] if f['qual'] == 'main.run')
    assert run_f['errors'] == 3                            # return …, fmt.Errorf / os.Exit(1) / panic
    assert {c['to']: c['error'] for c in run_f['calls']} == {
        'main.report': False,                              # its branch ends in return 0, nil
        'main.cleanup': True,                              # before os.Exit(1)
        'main.note': False}


def test_external_packages_never_count_but_missing_program_packages_do():
    r, _, _ = run({'go.mod': 'module example.com/app\n', 'main.go': '''package main

import (
	"strings"

	"github.com/other/lib"
	"example.com/app/util"
	"example.com/app/gone"
)

func main() {
	strings.ToUpper("x")
	lib.Do()
	util.Help()
	util.Missing()
	gone.F()
	b := strings.Builder{}
	b.WriteString("x")
	var e error
	e.Error()
	len("x")
}
''', 'util/util.go': 'package util\n\nfunc Help() {}\n'})
    # util.Help resolves; util.Missing and gone.F (a package of this module not in the snapshot) are unresolved
    assert (r['stats']['calls'], r['stats']['unresolved']) == (3, 2)


def test_package_wide_names_scoping_and_shadowing():
    r, blobs, _ = run({'go.mod': 'module m\n', 'main.go': '''package main

import "net/url"

func main() {
	helper()
	url, err := url.Parse("http://x")
	_ = url
	if err != nil {
		err.Error()
	}
	if lexer := newLexer(); lexer != nil {
		lexer.Next()
	}
}
''', 'util.go': '''package main

type Lexer struct{}

func helper()            {}
func newLexer() *Lexer   { return &Lexer{} }
func (l *Lexer) Next()   {}
'''})
    main = next(f for f in blobs['main.<module>']['funcs'] if f['qual'] == 'main.main')
    # helper() lives in another file of package main; url.Parse is the package (read before `url` binds)
    assert [c['to'] for c in main['calls']] == ['util.helper', 'util.newLexer', 'util.Lexer.Next']
    assert r['stats']['unresolved'] == 0


def test_go_embed_and_parse_glob_reference_files():
    _, blobs, edges = run({'go.mod': 'module m\n', 'web/web.go': '''package web

import (
	"embed"
	"html/template"
)

//go:embed static
var assets embed.FS

func Pages() *template.Template {
	assets.Open("static/app.css")
	return template.Must(template.ParseGlob("templates/*.html"))
}

func init() { Pages() }
''', 'web/static/app.css': 'x\n', 'templates/a.html': 'a\n', 'templates/b.html': 'b\n', 'README.md': 'r\n'})
    assert blobs['web.web.init']['kind'] == 'hook'          # init() of a package without main
    assert sorted((e['b'], tuple(e['via'])) for e in edges_of(edges, 'ref')) == [
        ('file:templates/a.html', ('templates/*.html',)), ('file:templates/b.html', ('templates/*.html',)),
        ('file:web/static/app.css', ('static/app.css', 'web/static'))]       # //go:embed is relative to the file's directory
    assert blobs['file:README.md']['kind'] == 'text'


def test_incremental_matches_full_recompute():
    edits = [(os.path.relpath(os.path.join(dp, n), MINI), None) for dp, _, fn in os.walk(MINI) for n in fn]
    files = load(MINI)
    steps = [(rel, files[rel]) for rel, _ in sorted(edits)]
    # util.go loses writeError, then gets it back in another file of the package; a new method shadows nothing
    steps += [('handlers/util.go', files['handlers/util.go'].replace('func writeError', 'func writeErr')),
              ('handlers/errors.go', 'package handlers\n\nimport "net/http"\n\nfunc writeError(w http.ResponseWriter, '
                                     'err error) { http.Error(w, err.Error(), 500) }\n'),
              ('store/store.go', files['store/store.go'].replace('func (d *DB) Ping()', 'func (d *DB) Pong()'))]
    check(sequence(steps))


def test_calling_a_func_literal_value_types_its_result():
    _, blobs, edges = run({'go.mod': 'module m\n', 'main.go': '''package main

var pick = func() interface{ Flush() } { return nil }
var mk = func() *Buf { return &Buf{} }

func main() {
	x := pick()
	x.Flush()
	mk().Close()
}
''', 'buf.go': 'package main\n\ntype Buf struct{}\n\nfunc (b *Buf) Flush() {}\nfunc (b *Buf) Close() {}\n'})
    main = next(f for f in blobs['main.<module>']['funcs'] if f['qual'] == 'main.main')
    # pick() returns an anonymous interface: not the program's, so no guess by the unique method name Flush;
    # mk() returns *Buf by the literal's result list
    assert [c['to'] for c in main['calls']] == ['buf.Buf.Close']
    assert 'orphan:buf' in blobs and quals(blobs['orphan:buf']) == ['buf.Buf.Flush']
    assert not edges_of(edges, 'main', 'main.<module>', 'orphan:buf')


def test_echo_or_gin_decided_by_the_routers_type_when_the_file_does_not_import_them():
    files = {
        'go.mod': 'module m\n',
        'server/server.go': '''package server

import (
	"github.com/gin-gonic/gin"
	"github.com/labstack/echo/v4"
)

func Echo() *echo.Echo { return echo.New() }
func Gin() *gin.Engine  { return gin.New() }
''',
        'app/routes.go': '''package app

import "m/server"

func Register() {
	e := server.Echo()
	e.GET("/u", list, auth)
	g := e.Group("/admin", auth)
	g.POST("/x", reset, auth)
	r := server.Gin()
	r.GET("/g", auth, show)
}
func list(c any) error  { return nil }
func reset(c any) error { return nil }
func show(c any)        {}
func auth(next any) any { return next }
''',
    }
    _, blobs, _ = run(files)
    assert {b['id']: (b['kind'], b['name']) for b in blobs.values() if b['kind'] in ('route', 'hook')} == {
        'app.routes.list': ('route', '/u'),                  # echo: the handler is the first function argument
        'app.routes.reset': ('route', 'POST /admin/x'),      # e.Group(...) is still echo
        'app.routes.show': ('route', '/g'),                  # gin: the last one
        'app.routes.auth': ('hook', 'middleware /admin'),    # Group("/admin", auth) guards the group
    }
    reg = next(f for b in blobs.values() for f in b['funcs'] if f['qual'] == 'app.routes.Register')
    assert sorted(c['to'] for c in reg['calls'] if c.get('deferred')) == [
        'app.routes.auth', 'app.routes.auth', 'app.routes.auth', 'app.routes.auth',
        'app.routes.list', 'app.routes.reset', 'app.routes.show']
