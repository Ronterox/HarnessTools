# aiblob timeline.json contract (v1)

Produced by `timeline.py`, consumed by `web/index.html`. Stable ids across steps:
route blob id = `module.func` of the handler; hub = `hub:module`; orphan = `orphan:module`;
tests = module name; entry = `module.<module>`.

```jsonc
{
  "root": "/abs/path/of/analyzed/app",
  "session": "<session id>",
  "steps": [
    {
      "idx": 0, "ts": "2026-11-05T00:01:10.000Z", "tool": "Write|Edit|MultiEdit|Bash",
      "file": "auth.py", "said": "first line the agent said before this edit", "turn": 0,
      "added": 52, "removed": 0,                 // lines in this step (whole file)
      "blobs": [
        {
          "id": "auth.login", "name": "POST /login", "emoji": "🔐",
          "kind": "route|entry|hook|error-handler|command|tests|hub|orphan",
          "module": "auth", "size": 52, "complexity": 7, "errors": 3,   // errors = abort/raise/4xx-return exits
          "data": ["table:users", "ctx:session.clear"],
          "delta": { "added": 52, "removed": 0, "moved": 0, "state": "new|changed|same" },
          "funcs": [
            { "qual": "auth.login", "name": "login", "file": "auth.py", "start": 40, "end": 91,
              "lines": 52, "branches": 7, "errors": 2, "hash": "ab12cd34ef",
              "data": ["ctx:request.form"], "snippet": ["if not user: abort(401)", "session['uid'] = user.id", "return redirect(next)"],
              "calls": [ { "to": "models.get_user_by_username", "error": false } ],
              "delta": { "added": 52, "removed": 0, "state": "new|changed|same|moved" } }
          ]
        }
      ],
      "edges": [
        { "a": "auth.login", "b": "hub:models", "kind": "main",  "n": 2 },
        { "a": "auth.login", "b": "hub:errors", "kind": "error", "n": 1 },
        { "a": "auth.login", "b": "hub:models", "kind": "data",  "n": 1, "via": ["table:users"] }
      ]
    }
  ]
}
```

Rules: a blob absent from a step no longer exists (the page fades it out itself).
Every step carries the full state (no diffs across steps needed by the page); since v3 the file carries
deltas instead and readers rebuild this state per step (see v3 and v4 below).
`delta` compares function bodies by `hash` with the previous step; a blob's delta is
the sum over its functions plus the lines of functions deleted from the app. A function with the
same `qual` and `hash` that sits in another blob than in the previous step is `moved`: its delta is 0
and its lines count as the blob's `moved` (in the blob it left and the one it joined), never as
added/removed. A blob whose only change is moved functions is `changed` with added 0, removed 0. Spots on the page use
`delta` with fading recency over the following 6 steps.

## v2 additions (2026-11-05)

- New blob kinds: `file` (a non-code file that code references: templates, static assets, configs, SQL files) and `text` (a non-code file nothing references: README, LICENSE, docs). Both: id `file:<relpath>`, `name` = basename, `module` = top directory (`templates`, `static`, `docs`, `.` for root), `path`, `size` = lines, `complexity` 0, `funcs` empty, `delta` from a whole-file diff (`state` new/changed/same). `text` blobs have no incoming edges (nothing references them); since 2026-11-10 a page, stylesheet or markdown file may have outgoing `ref` edges to the files it links (v6 additions). A binary file (by extension or a NUL byte) has `size` 0 and a delta of 0 lines (its `state` still says new/changed/same).
- New edge kind `ref`: from a code blob to a `file` blob, `n` = number of references, `via` = the literals that matched. One hop on, a `file` blob that names another file (`{% extends "base.html" %}`) has a `ref` edge from itself (`a` = `file:templates/notes/view.html`, `b` = `file:templates/base.html`).
- Function `calls` entries may carry `"deferred": true` when the callee was passed as a value (callback, target=, key=) rather than called. A deferred target is reachable like a called one: it joins the blob of the function that passes it, or the hub when another root reaches it too (a target that is itself a root, e.g. an error handler given to `register_error_handler`, stays in its own blob).
- Function `refs`: `["templates/notes/view.html", ...]`, the snapshot files the function's own string literals name (sorted). A blob's `ref` edges are the union over its functions.
- Blob `delta.removed_funcs`: `[qual, ...]`, functions that left this blob in this step because they are gone from the app (a function that moved to another blob is not listed). Always present, `[]` for file / text blobs.
- Step-level `stats`: `{"calls": N, "unresolved": M, "parse_ms": x, "analyze_ms": y}`; `calls` counts only call sites that can be the app's own code (resolved ones plus `unresolved`), never calls to builtins, the stdlib or third-party imports; `unresolved` counts calls to non-builtin names the resolver could not place, so `1 - unresolved / calls` is the resolved ratio. `timeline.py --no-timings` writes 0 for both `_ms` fields so runs compare byte for byte.
- Blob kind `command` now also covers subcommands registered by call (argparse `add_parser`, click groups), named by the subcommand string. Its id is the handler's qual for `.set_defaults(func=f)`, a `{"name": f}` dispatch table or `@group.command()`. For `if args.cmd == "name":` dispatch the branch body is carved out of the dispatching function into a function of its own, qual `<module>.<func>:<name>` (e.g. `aic.cli.main:compile`), which is the blob id; a branch several subcommands share (`args.cmd in ("compile", "fix")`) becomes `<func>:compile|fix` and lands in the module's hub. The dispatching function keeps only its own lines and has deferred calls to each branch.
- JavaScript / TypeScript modules (`.js .mjs .cjs .jsx .ts .mts .cts .tsx`, see `lang_js.py`) produce the same blobs and edges. Module names come from paths like Python's (`routes/users.js` -> `routes.users`, `lib/index.js` -> `lib`, dots in a file name become `_`). Inline handlers are carved out of the code that registers them into synthetic functions whose qual is the blob id: `app.get('/p', (req, res) => …)` and plain `node:http` branches `if (req.method === 'GET' && pathname === '/p')` -> `<module>.<GET /p>` (kind `route`, name with a mount prefix from `app.use('/users', router)`, GET omitted as for Flask); `app.use((req, res, next) => …)` -> `<module>.<use>` or `<module>.<use /prefix>` (kind `hook`, name `middleware [/prefix]`; with 4 parameters kind `error-handler`, name `error handler`); in a test module `test('name', …)` / `it` / `before…` -> `<module>.<test name>` / `<module>.<it name>` / `<module>.<before>`. A label used twice in a module gets a ` #2`, ` #3` … suffix (`<use #2>`). Calls and imports never resolve across languages. JS/TS files under `static/`, `public/`, `assets/` or `dist/` are `file` / `text` blobs when another language has the entry, and code when the entry is JS or there is none. Regex routes (`app.get(/^\/users\/(\d+)$/, h)` -> `<GET /users/(\d+)>`, name `/users/(\d+)`) and Express param patterns (`'/u/:id(\\d+)'` -> `/u/:id`) are routes like any other. Route chains `app.route('/p').get(a).post(b)` give one route per verb (inline handlers `<GET /p>`, named ones keep their own id); `.all(mw)` is a `hook` (`middleware /p`) every verb of the chain defers to, so each route has a `main` edge to it. `app[verb]('/p', h)` has no static verb: no route, one unresolved call. Module-level `let` / `var` bindings and `const` objects, arrays, `new …` instances, `Object.create` / `Array.from` results (also behind `as` / `satisfies`) that a handler, a function or the module's own code mentions are data symbols `global:<module>.<name>`; routes sharing one are linked by `data` edges. Imports, functions and plain scalar `const`s are not.
- C modules (`.c .h`, see `lang_c.py`) produce the same blobs and edges. Module names come from paths (`src/render.c` -> `src.render`); a header is its own module with an `_h` suffix (`src/render.h` -> `src.render_h`) so it never collides with its `.c`, and `#include "render.h"` depends on both. A header has no blob unless it defines functions (`static inline`), which form `hub:src.render_h` / `orphan:src.render_h` like any module's. Calls resolve by name across the program (one global namespace): the file's own definition first, else the one non-static definition anywhere (`.c` before header, a test module's only for test modules); `static` functions resolve only within their file, a header's static inline ones everywhere. Roots: `main` of the non-test module that defines it is the `entry` (id `<module>.<module>`, merged with the file-scope code, as for Python); a function only ever reached from a file-scope initializer (`T[] = { on_a }`, `.fn = on_b`, `static void (*cb)(int) = on_c`) and never from a function body is a `hook` (id its qual, name the function name). A function passed as a value inside a function (`signal(SIGINT, f)`, `pthread_create(.., f, ..)`, `atexit(f)`, `qsort(.., cmp)`, `.handler = f`, `p->cb = f`, any identifier argument) is a `deferred` call. Test modules (under `tests/` / `test/`, or `test_*.c` / `*_test.c`) are one `tests` blob each. Error path: the tail of an `if` / `else` branch that ends in `return -1` / `-EINVAL` / `NULL` / `false`, `exit(…)` / `abort()` / `die(…)`, or `goto fail` (any label like `fail` / `err` / `error` / `bail`), and every statement after such a label; `errors` counts those exits. Data: file-scope and `extern` variables a function reads or writes are `global:<name>`, a field access on one `global:<name>.<field>`; a `static` file-scope variable is `global:<module>.<name>` (two files' statics are different variables); `fopen` / `open` / `openat` / `opendir` with a literal path -> `file:<path>`, `getenv` / `setenv("X")` -> `env:X`; a literal naming a snapshot file is a `ref` as for Python. Unresolved: libc / POSIX / pthread / math names and the `pthread_ sem_ epoll_ SDL_ gl glfw rl …` families (`lang_c.EXTERNAL`, `EXTERNAL_PREFIX`) and macro-looking callees (ALL_CAPS, or a `#define f(…)` of the same file) never count; they still resolve when the program defines them. Function-like macro bodies are not read; a macro's identifier arguments that name a program function are deferred calls. GNU `typeof(…)` is read as a plain type so `(typeof(x)){…}` compound literals parse. An `#include` (quoted or `<…>`) that resolves to no snapshot file, directly or through the program's own headers, is external: names it would provide (`raylib.h` / `raymath.h` a bundled list, `tree_sitter/api.h` the `ts_` / `tree_sitter_` prefixes, `rlgl.h` the `rl` prefix, any other header its stem plus `_`, e.g. `yyjson_*`) are aliased to `ext:<name>` when the program defines nothing of that name and never count as unresolved. A C program has one `main`, so a big entry is split (`CLang.split_entry`; Python / JS entries never are): when the entry alone reaches 20 functions or more (`split_min`), every function `main` calls directly (called or deferred, in any module, static or not) becomes a `command` root, id its qual, name the function name, unless it is already a root, a test's, shorter than 3 lines (except the one that owns more than 50%, below, e.g. a `run() { loop(); }` wrapper), or called by 2+ functions other than `main` (a helper). `main` keeps its own lines plus what only it reaches; what several commands reach goes to the hubs as usual. When one of the qualifying callees (however many there are) owns more than 50% of the entry's functions (reaches them while no other callee does and `main` does not except through it) (a main loop: `main -> run() -> {draw_x, update_y, handle_z}`), the split is one level deeper, at that function's qualifying callees (helper = called by 2+ functions other than `main` and it, the depth-1 commands included); it stays with `main`, the other depth-1 callees stay commands. Never deeper than 2. A split entry blob carries `"depth": 1|2`; an unsplit one has no `depth`.
- Go modules (`.go`, not under `testdata/`, see `lang_go.py`) produce the same blobs and edges. Module names come from paths like C's (`internal/auth/login.go` -> `internal.auth.login`); a method's qual is `<module>.<Type>.<Method>`. A package is a directory (plus its package clause, so `foo_test` beside `foo` is its own package): bare names resolve package-wide, across the files of the directory (a `_test.go` file's functions only for test files), and `pkg.F` through the import path, which names a snapshot directory when it starts with the module path of a snapshot `go.mod` (`go.mod` files are read by the Go layer, so they are no `file` / `text` blob; module `go_mod`). Methods resolve by the receiver's static type: parameters and receivers, `var x T`, `x := T{}` / `&T{}` / `new(T)`, `x := NewT()` / `a, b := pkg.F()` (the function's declared results, func-typed values included), struct fields (`h.db.Query`, through embedded structs), range elements, type assertions; each local is read in the scope it was bound in (blocks and `if` / `for` / `switch` are scopes; `url, err := url.Parse(s)` reads the package). A value of a program interface type, or of unknown type, resolves to the one non-test method of that name in the program (for unknown types: in a package the caller sees); otherwise nothing. Roots: `func main` of package main is the `entry` (id `<module>.<module>`, merged with the file's package-level `var` / `const` code; that file's `init()`s join it; the shallowest such file is the entry, another package main's `main` is an `entry` blob of its own named after its module); `init()` in a file without `main` is a `hook` named `init` (several in one file: `init`, `init #2`, …). Routes (kind `route`, id the handler's qual, named like Flask: `/p`, `POST /p`, GET omitted): `http.HandleFunc("/p", h)` / `mux.HandleFunc` / `r.HandleFunc(…).Methods("POST")` (gorilla) / Go 1.22 patterns `"POST /p"`, and `r.Get` / `r.Post` … (chi, fiber), `r.GET` … (gin, echo; echo's handler is the second argument, the others' the last), with mount prefixes from `g := r.Group("/api")`, `r.PathPrefix("/api").Subrouter()`, `r.With(mw)` and `r.Route("/api", func(r chi.Router) {…})`. A handler wrapped in `http.HandlerFunc(h)`, `http.StripPrefix(…, h)` or `auth(h)` is looked through; `http.Handle("/p", h)` with a handler value (not a function) is only a deferred call. Inline func literals are carved out of the registering function like JS handlers: `<GET /p>`, `<POST /p>`, `<ANY /p>` for a method-less pattern, `<use>` / `<use /prefix>` for `r.Use(func …)`, `<error 404>` for `r.NoRoute` / `r.NotFound` (kind `error-handler`, also `e.HTTPErrorHandler = f`), `<cmd name>` for commands. `X.Use(mw)` makes `mw` a `hook` named `middleware [/prefix]`; `r.Use(Auth())` makes `Auth` the hook. `cobra.Command{Use: "name …", Run / RunE: f}` and `cli.Command{Name: "name", Action: f}` (also as elided elements of `[]*cli.Command{{…}}`) are `command` blobs named by the first word. `_test.go` files are one `tests` blob each. `go f(x)` is a `deferred` call, `defer f()` a call; an identifier or selector naming a program function as an argument, a composite-literal value or an assignment's right side is a deferred call. Error path (strict tail rule, `if` / `else` branches): a branch whose last statement is `return …, err` (any `err…` / `…Err` / `…Error` identifier or selector), `return fmt.Errorf(…)` / `errors.New(…)` / `….NewHTTPError(…)`, `return c.JSON(4xx|5xx, …)`, a call `http.Error(w, …)` / `c.JSON(4xx|5xx, …)` (also `c.String` … with a status, `http.StatusXxx` constants of 400 and up) / `c.AbortWith…` / an error-named call (`writeError(…)`), or a bare `return` right after one of those; `log.Fatal*` / `….Panic*` / `panic(…)` / `os.Exit(non-zero)` anywhere. `errors` counts those exits. Data (per file, never from another file's declarations): the file's own package-level `var`s, read or written, are `global:<pkg>.<name>`, a field access on one `global:<pkg>.<name>.<field>` (`<pkg>` the package clause; for package main the directory, dotted, or `main` at the root); a package-level variable of another file or package counts only where it is used like one (`x.f`, `x.m()`, `x[k]`, `x = …`, `x++`, `&x`, also `pkg.Var.f` for a non-stdlib import, named by the import path's last segment); `os.Getenv` / `LookupEnv` / `Setenv("X")` -> `env:X`; `os.Open` / `ReadFile` / `WriteFile` / `Create` … and `ioutil.ReadFile` … with a literal path -> `file:<path>`; a string literal that starts like SQL (`SELECT`, `INSERT`, …) -> `table:<name>` with the Python regex. Refs: string literals naming a snapshot file as for Python; a package `var`'s initializer literals (`var tmpl = template.Must(template.ParseFiles("templates/x.html"))`) count for the functions of that file that use the var; glob literals (`ParseGlob`, `LoadHTMLGlob`, `"templates/*.html"`) ref every file they match; `//go:embed pattern` (relative to the file's directory) refs what it matches for the functions that use the embedded var; `http.Dir("static")` / `r.Static("/s", "./assets")` ref every file below the directory. Unresolved (`stats`): every call is placed by `GoLang.link()`; calls into imports outside the snapshot's module path (the standard library, third-party; with no `go.mod` in the snapshot, an import path ending in a snapshot directory is the program's unless it is a standard library path), methods on their values, builtins, conversions, calls of func-typed values and fields, and methods promoted from embedded types never count. A bare name the package does not define, `pkg.F` of a package of this module that lacks `F` (or is missing from the snapshot), and a method a program struct type (without embedded fields) lacks count as unresolved. A file that imports none of the known routers decides echo-vs-gin argument order in `link()` from the router variable's type (`echo.New()`, a program function returning `*echo.Echo`, `e.Group(...)`, a struct field), falling back to whether any file of the package imports echo. `X.Group("/p", mw...)` middleware become `hook` blobs named `middleware /p`, like `Use`. Known gap: a plain read (`return limit`) of a package var declared in another file of the same package produces no data symbol; `x.f`, `x[k]`, `x = …`, `x++`, `&x` do.
- Shell (2026-11-10): shell scripts (`.sh .bash`, and extensionless files whose first line is a `sh` / `bash` / `dash` shebang, see `lang_sh.py`, tree-sitter-bash) produce the same blobs and edges. Module names come from paths with the extension kept as a suffix so a script never collides with a module of another language (`scripts/fetch-deps.sh` -> `scripts.fetch-deps_sh`, `x.bash` -> `x_bash`, extensionless `bin/deploy` -> `bin.deploy_sh`). Roots: every non-test script's top-level code (the statements outside function definitions, comments left out) is an `entry`, id `<module>.<module>`, named after the file (`run.sh`, `deploy`); a script of only functions (a library) has no entry blob. Functions: `f() { … }` and `function f { … }` (a second definition of a name, e.g. in if / else twins: the first). Test scripts (under `tests/` / `test/`, or `test_*` / `*_test`) are one `tests` blob each, their top-level code included. Calls: a command naming a function resolves to the script's own, else to one of the scripts it sources (`source x` / `. x`, transitively, in order), else to the one script that (transitively) sources it and defines it. `source x` is also an import (dependency) and a call to x's entry. Running another script (`bash x.sh`, `sh x`, `./x.sh`, `"$DIR/x.sh"`, `exec x.sh`) or another language's module (`python foo.py`, `.venv/bin/python -I foo.py`, `python3 -m pkg.mod` -> `pkg/mod.py` or `pkg/mod/__main__.py`, `node x.js`, `bun` / `tsx`) is a call to that module's `<module>` when the snapshot has it and it has top-level code; a module of another language run that way becomes an `entry` root of its own (id `<module>.<module>`, name the module), and a test module run that way keeps its `tests` blob, which then holds its `<module>` code too (analyze() takes these modules from the `tops` that `ShLang.link()` returns). Paths are static when built from literals, variables assigned a static value (the latest assignment in scope; a function body, subshell, pipeline or `$( )` keeps its own), `$0` / `${BASH_SOURCE[0]}`, `$(dirname X)`, `${0%/*}`, `$(cd X && pwd)`, `$(realpath X)`, `$(pwd)` / `$PWD`; a relative path is taken from the directory the last `cd` in scope went to, else from the script's directory, then from the snapshot root, the first that exists wins. A run whose path is static but names no snapshot code is unresolved when it has a code extension (`bash missing.sh`, `python gone.py`), and external otherwise (`./build/app`); a dynamic one (`"$BIN"`, `"$1"`) and `python -c` / `python -` / `-m` of a module not in the snapshot are no call. `exec env nohup time timeout nice sudo doas command builtin setsid stdbuf` and `uv / poetry / pipenv run` are looked through (`env A=1 cmd` sets `env:A`). `trap f EXIT` and a command argument naming one of the script's own functions (`check name fn …`) are `deferred` calls. Error path: `exit N` / `return N` with a literal N other than 0 count in `errors`, and so does a `||` tail that exits otherwise (`|| exit`, `|| exit $?`, `|| return`, `|| die "…"`, a command named `die` / `fatal` / `fail` / `error` / `err` / `abort` / `panic` / `bail` or ending in `_` / `-` and one of those, e.g. `log_error`, not `make_error_page` or `failover`; `|| { …; exit 1; }` counted once). A bare `exit` / `return`, or one whose status is made at run time (`exit $?`, `exit "$fail"`), passes the last command's status on: an error exit only in a `||` tail (`usage; exit` is a normal exit). Calls in such a tail, in an `if` / `elif` / `else` branch or `case` item whose last statement exits with an error (from after its last statement that can still leave normally), and calls to an error-named function are `error` calls. Data: `env:X` for a variable that is exported (`export X[=…]`, `declare -x`), set for one command (`X=1 cmd`) or read (`$X`, `${X…}`) when the script never assigns it (assignments, `local`, `declare`, `for`, `read`, `mapfile`, `getopts`) or reads it with a default (`${X:-v}`, `-`, `:=`, `:?`, `:+`); positional, special (`$@ $# $?`) and bash-internal variables (`RANDOM BASH_SOURCE PWD IFS …`, `lang_sh.SPECIAL`) never are. A redirection target (`> out.log`, `>> x`, `< in.txt`, `&> x`; not `/dev/…` or `>&2`) and the file arguments of `cat tac head tail tee wc sort uniq cut paste cp mv rm ln touch chmod diff cmp …` (`lang_sh.FILE_CMDS`) with a static path are `file:<path>` (the root-relative path when it is known, else as written), whether or not the file is in the snapshot, as C's `fopen`; any command argument or redirection with a static path naming a snapshot non-code file is a `ref` to it (the path as resolved above, else matched like a Python literal), via the argument as written (`$here/page.tpl.html`). Quoted heredoc bodies (`<<'PY'`) are not read; unquoted ones are read for expansions. A `cd`, and a variable's value, last until the end of the subshell, pipeline, `$( )` or function body they are in. A script whose text cannot be encoded (an unpaired surrogate; bytes the replay escaped are encoded back) or that nests deeper than the walk's recursion allows (hundreds of nested `if`s or `$( )`, thousands of `&&` terms) is a module with an `error` and no blob, as a Python file that does not parse; the rest of the snapshot is analysed as always. Unresolved: shell builtins and common commands (`lang_sh.EXTERNAL`: coreutils, git, curl, make, cc, python, node, docker, tmux, nvim …) never count, nor do names with a `-` (`llama-server`); they still resolve when a script (or a sourced one) defines them. Any other bare command name nothing defines counts.

## v3 (2026-11-05)

The file now carries deltas. Top level gains `"schema": 3`; a file without it (v1/v2) has every step full. Within v3 (and
for a bare list of steps handed to `timeline.expand()`), a step is full when it is step 0, has `"full": true`
or carries `edges`: full steps always carry `edges`, delta steps never do, so a list of v2 steps reads as full.
The *state* of a step, what the page draws and the rules above describe, is unchanged; only its encoding is.

- Step 0, and any step with `"full": true`, carries the whole state as before: `blobs`, `edges`, `stats`.
- Every other step carries `idx ts tool file said turn added removed` as before, `stats`, and:
  - `blobs`: the blobs that are new or changed since the previous step, each a full blob object as in v2.
  - `removed_blobs`: `[id, ...]`, blobs gone this step.
  - `edges_add`: edges that are new or changed (`n` or `via`), full edge objects. An edge is identified by
    `(a, b, kind)`.
  - `edges_remove`: `[{"a", "b", "kind"}, ...]`, edges gone this step.
  - `order` (only when needed): every blob id of the state, in order.
- Rebuilding a step (`timeline.expand()` in Python, `expandSteps()` in web/index.html, which must agree):
  - A blob the step does not send and does not remove is the previous step's blob with zeroed deltas: every
    count in `delta` and in each function's `delta` 0, `state` `"same"`, `removed_funcs` `[]`. That is exactly
    what an unchanged blob looks like in a full state, so the recency fade and stains read the same: a blob
    absent from a delta step has `delta.state` `"same"` and its last change sits in the step that last sent it.
  - A blob is left out only when it equals that zeroed copy. A blob whose `delta.state` is `same` but whose
    other fields changed (a function's lines shifted after an edit above it, a call now resolves elsewhere) is
    sent again in full.
  - Blob order: the previous order without `removed_blobs`, a sent blob that already existed in its old place,
    new ones appended in the order `blobs` lists them; `order` replaces that when given.
  - Edges: the previous set, minus `edges_remove`, plus `edges_add` (replacing by `(a, b, kind)`), sorted by
    kind (`main`, `error`, `data`, `ref`), then `a`, then `b`, the order analyze.py writes them. The writer
    sends a step in full when its edges are not in that order.
- `timeline.py --v2` writes every step in full (no `schema` key), for debugging; `expand()` of a v3 file equals
  that output step for step. `timeline.py --full` recomputes the whole analysis per step instead of reusing
  the previous step's (incremental is the default); both write the same bytes.

## v4 (2026-11-08)

Same as v3 except for the functions of a blob a delta step sends. Top level `"schema": 4`. Step 0 and `full` steps
are unchanged (every blob with its whole `funcs`). The rebuilt state is the v3 one: `expand()` of a v4 file equals
`expand()` of the v3 file and the `--v2` output, step for step. Readers take both: `expand()` and the page patch
only when `schema` >= 4 (a bare list of steps handed to `expand()` reads as v4), so a v3 file reads as before.

- A delta step's sent blob carries its blob fields in full (`delta`, `size`, `label`, ...) as in v3, but its
  `funcs` is a patch on the *base*: the previous step's blob of that id with zeroed deltas, i.e. what an unsent
  blob rebuilds to (none when the blob was not in the previous step's state, then every function is new):
  - `funcs`: the functions that are new to the base, whole, and the ones whose object differs from the base's
    function of that qual, as `{"qual", <only the keys whose value differs>}` (a function shifted by an edit
    above it is `{"qual", "start", "end"}`; a changed body adds `lines`, `hash`, `delta`, ...). A function whose
    keys are not the base function's keys in the same order is listed in `funcs_remove` and sent whole instead.
  - `funcs_remove` (only when not empty): `[qual, ...]`, base functions the blob no longer has (deleted from the
    app, or moved to another blob: there the function is new to that blob's base and sent whole).
  - `funcs_order` (only when needed): every qual of the blob, in order.
  - Rebuild: start from the base's functions; drop `funcs_remove`; for each `funcs` entry, a qual the list still
    has is replaced in place by the base function with the entry's keys over it (key order the base's), any other
    qual is appended whole; `funcs_order` then replaces the order. A function the patch does not name is the
    base's: the previous step's function with its `delta` zeroed, as for unsent blobs. `funcs_remove` and
    `funcs_order` are not part of the rebuilt blob.
  - A sent blob without `funcs` has none (no patch). The writer sends the step in full when a patch cannot say
    it: a qual twice in the blob or its base, a function without a string `qual`, a blob with a key named
    `funcs_remove` / `funcs_order`.
- Measured on the aigource map (1270 steps): of the 39.7 MB of function objects that re-sent blobs carried in v3,
  91% of the changed functions differ only in `start` / `end`; whole changed objects would be 13.6 MB, a
  `{qual, start, end}` patch for shifted-only ones and whole objects for the rest 3.9 MB, changed keys only 3.1 MB
  (chosen: one rule, smallest). `funcs_order` is 0.4 MB (300 blobs, mostly a function inserted above others).
- `timeline.compact(steps, schema=3)` / `Compactor(3)` still write v3; `label.py` writes a file back in the
  schema it read.
- "Unchanged" is Python `==`: a value that changes only between `1` / `True` / `1.0` (or `0` / `False`), or a
  function whose keys only change order, is not re-sent and rebuilds with the previous value / key order. The
  analyzer never writes such changes (checked on every key path of the aigource map).

## v5 (2026-11-08): merges, cuts, areas

Top level `"schema": 5`. The blobs a step holds are no longer exactly the analyzer's: `areas.py` (applied by
timeline.py to each step's analysis, before the deltas) folds small blobs into others and cuts pieces out of big
ones, recounts the edges over the result, and puts every blob in an area. `timeline.py --no-smart` skips the folds
and cuts (areas stay), for debugging and tests. The encoding is v4's plus the areas table; `expand()` and the
page's `expandSteps()` read v3, v4 and v5 (a file whose full steps carry no `areas` rebuilds steps without it).

- Blob fields (code, file and text blobs alike; re-sent like any other field when they change):
  - `area`: the id of the blob's area at this step (`a<n>`, `tests` or `files`; since 2026-11-10 also `data` and
    `docs`, see "v6 additions").
  - `merged` (only on a blob something was folded into): `[{"id", "name", "emoji", "kind", "lines", "funcs": [qual, ...]}]`,
    the folded-in blobs as the analyzer drew them, flattened (a blob that took a fold before folding itself lists
    it too); `lines` / `funcs` are that blob's own, which now sit in this blob's `funcs`. `label.py` gives each
    entry the `label` of its own functions.
  - `cut` (only on a piece): the id of the blob it was cut from, present in the same step. A piece's id is
    `<parent id>><root qual>` (the parent id may itself contain `>`, as in `main.<module>`: read `cut`, not the
    id), `name` its root function's name, `kind` / `emoji` the parent's, `module` the root function's module.
  - `size`, `complexity`, `errors`, `data` are the sums / union over the blob's functions after the move, as
    analyze() computes them.
- A piece's `module` is the module of its root function's file, so a method root (`app.Svc.run`) lands in `app`,
  not in a module `app.Svc` that does not exist.
- Folds (merges): a code blob under 20 lines (`areas.TINY`) folds into the biggest blob of its module (a C header
  with its .c), else into the blob it shares the most call sites with (both directions), then the most data
  symbols; tests only into tests, nothing into or out of file / text blobs. Never folded: routes, entries, error
  handlers, hooks (the entry points a small app's map is made of) and cut parents. Smallest first (ties by id).
- Cuts: a blob of 500+ lines or 50+ functions (never a tests blob) gives up callee-rooted pieces: dominator tree
  over its own calls from its starts (root function, functions nothing in the blob calls, functions another
  blob calls), candidates read like `CLang.split_entry` (start-level children at most one other function calls;
  past one owning more than half, one level down; descending into the blob's own root function is free). A
  candidate whose owned functions hold 10%+ of the blob's lines and 20+ lines, with a root of 3+ lines, is a piece.
- Edges are analyze()'s count over the new blobs: call sites per pair (`main` / `error`), shared data symbols per
  unordered pair (same `NOT_SHARED`, generic `env:` and `ctx:` prefix rules), file literals per (blob, file) for `ref`. What
  analyze() counted beyond the functions' own calls and `refs` (an `abort` to an error handler, module-level code)
  stays with the blob that holds the original blob's id, or the blob it folded into. A cut parent's `ref` edge is
  split one literal per function whose `refs` name the file, the rest staying with the parent; each part keeps
  the parent's `via`. With no fold and no cut the edges are analyze()'s, unchanged.
- Step-level `areas`, after `edges`: `[{"id", "caption", "faces": [emoji, ...]}]`, sorted by id, one entry per
  area a blob of the step names. A full step carries the whole table; a delta step carries only the entries new
  or changed since the previous step (`areas`) and the ids gone (`areas_remove`), each only when not empty.
  Rebuild: the previous table, minus `areas_remove`, entries of `areas` replacing or adding by id, sorted by id.
  `timeline.compact()` writes the table this way for any schema it is asked for, so steps round-trip.
- Areas: code blobs are grouped by module stem (a C header with its .c): modules of one top directory (`src`,
  `draft`, `.` for files at the root) are joined pairwise, most call sites between them per line of the smaller
  first, while a join holds at most 9 blobs, down to min(7, ceil(code blobs / 9)) areas plus one per further top
  directory. Past 12 areas (`areas.MAX_AREAS`) joins cross top directories, still under the cap, and when the cap
  blocks every join it gives way: the best pairs keep joining until 12 code areas are left, so the top view never
  shows more (TechnicalAnalysis, 397 code blobs: 31 areas without this). `tests` holds tests blobs and modules
  under a test directory, `files` the file / text blobs (since 2026-11-10 not those of a data `fmt`, in `data`, nor
  markdown and unreferenced html, in `docs`). The joins use a heap of pair scores (O(n^2 log n) in
  modules; 200 modules ~0.09 s, 400 ~0.4 s); the order of joins is the plain scan's, pair by pair.
- Stability: a module keeps its area id from step to step. The grouping is recomputed only when the set of
  modules or the call sites between them change; each fresh group is matched to the area it overlaps most (lines
  + 1 per module, greedy). New modules join their group's area at once (a group no area matches gets a new id).
  Existing modules move only when the fresh grouping is adopted wholesale (matched groups keep their ids), which
  needs both: the same fresh grouping has come out on the step it first appeared and on each of the 40 steps after
  it (`areas.HOLD`; adopted at the earliest on the 41st step it has held), or for half the session so far when that
  is shorter, and it would change some area's existing modules by more than half of their lines
  (`areas.REGROUP`, symmetric difference over union). The 12-area bound holds between regroups too: once 12 code
  areas are in use, a new module whose group no area matches joins the area it shares the most call sites with
  (then the smallest by lines, then by id) instead of opening a 13th, and an assignment still over 12 adopts the
  fresh grouping at once, without the hold (150 modules added one per step: 12 areas, 0 regroups). Ids are never
  reused. Measured: Flask demo 6 reassignments
  (one regroup at step 13 of 18); aigource 96 blob reassignments in 6 regroups over 1,270 steps.
- Captions and faces: `caption` is word frequency over the members' titles (label title, else name; an
  unlabelled piece is skipped): count of blobs using a word x sqrt(their lines), generic words skipped
  (`areas.GENERIC`), a second word when it scores at least half the first, joined by ` · `; `Tests` and `Files`
  for those areas (`Data`, `Docs` too). `faces` are the top 3 member emojis by lines (label emoji, else heuristic; an unlabelled piece
  takes its parent's label emoji). timeline.py writes them from names; `label.py` recomputes them from the labels
  it applies (`areas.caption_table()`), so they follow the labels for free.
- Labels (`label.py`): a blob's cache key leaves out functions folded in from a small blob, and a cut parent's
  key also covers its pieces' own functions, so both keep the key (and label) of the blob the analyzer drew; a
  piece has a key of its own and shows its root function's name and its parent's emoji until a manual
  `label.py --per-step` run labels it (live labelling asks only steps made after its boundary). On the aigource map every key the v4 labels cached still matches (1,130 distinct cache hits, 0
  calls); the only uncached keys are the 192 versions of the 15 pieces.
- Label reuse (`label.py --relabel-at X`, default 0.2, 2026-11-08): per blob identity, a label is kept until the
  lines changed since it was made reach X of the identity's lines at that label; then the model is asked again.
  Identities are the analyzer's blobs: a blob's id (a cut parent with its pieces' functions, keyed as above),
  each `merged` entry's id (its own functions; never asked, as before, so it only keeps a label it had), and a
  piece's id (`<parent>><root qual>`, its own functions: a piece gets its own label on its first version, then
  follows the rule like any blob; the parent's label describes the whole and is not inherited). Changed lines
  per version: a function whose hash changed counts its `delta` added + removed (after a gap, or with no delta,
  its line-count difference, at least 1); an added or removed function its whole length; summed since the
  label, not since the previous version, so a run of small edits cannot avoid a relabel. File and text blobs
  are outside the rule. A kept label is a cache entry under the new key, a copy of the label with
  `"reused_from": "<labelled key>"` (always a model-labelled key); the timeline shows it without that field.
  A key in the cache is always a hit: a model label anchors the identity there, a reused one counts as the
  reuse it records (one from another anchor re-anchors there at today's lines). No state is stored: label.py
  replays it from step 0 against the cache each run, so a `--per-step` resume decides as an uninterrupted run
  (steps before `--steps A` replay without writing; an uncached key there leaves the blob unlabelled). The run
  log has `reused` per step; steps that only kept labels are logged with `asked` 0. `X = 0` asks for every new
  key, as before. `--count` prints the label requests from an empty cache at X and at 0. aigource: 1,322 -> 533
  requests at 0.2 (59.7% saved; non-piece blobs and files 1,128 -> 468, pieces 194 -> 65); in the measurement's
  universe (merged entries askable, no piece identities) 1,221 -> 555 (54.5%; the measurement's replay gives
  576, 52.8%, = 875 of 1,656 calls. Here 49 revisits of a key that kept a label are cache hits, where the
  measurement decides them afresh; through the anchors they leave, they account for the 21 fewer requests).
- Live labels (`liveserve.py`, phase 4, 2026-11-09): the steps served to the live page carry two more fields,
  `lk` {blob id: kid} and `mk` {`merged` entry id: kid}, for the ids whose label key differs from the previous
  step's (kid = the first 16 hex of the key's sha1; `""` for a file blob without a readable head). The page keeps
  them from step to step; `live.labels` in /live/timeline.json and the "labels" events give {kid: label or null},
  the labels learnt after the steps showing them were labelled, which the page puts on every blob (and `merged`
  entry) with that kid and from which it recaptions those steps' areas (`areas.caption_table()`, ported). A static
  timeline.json never has them. Only blobs changed at steps after the store's labelling boundary
  (`live/NAME/labelling.json`: {since, steps}: steps made at `since` or later) are ever asked; the rest of the rule is the one above, applied as
  label.py applies it, so the page shows what label.py writes with the cache as it stands.
- A failed ask leaves that blob with no anchor, so its later versions are asked (and then cached, hits on any
  rerun): a `--retry-failed` run can end slightly different from a run with no failures (534 vs 533 labels in a
  verifier run on aigource). In the whole-timeline mode, versions that would keep the label of a key whose ask
  failed stay unlabelled until a later run labels that key.

## v6 (2026-11-09): clusters inside big blobs

Top level `"schema": 6`. A code blob (after the folds and cuts; never a tests blob, never with `timeline.py
--no-smart`) of 12+ functions (`clusters.MIN_FUNCS`) whose functions fall into 3+ call groups besides the loose ends
(`clusters.MIN_GROUPS`), whatever its lines, carries the call communities of its functions, which the page's
drill-down draws instead of every function; any other blob keeps the plain drill-down. Under 12 functions the plain
drill-down names every function and draws every call readably; with 1-2 groups the view would be a split, not a map.
(First rule, 2026-11-09: blobs at the cut size, 500+ lines or 50+ functions; changed 2026-11-10 by the user: "any
size, 3+ groups". The page's dials keep the cut size as their limits.) Everything else is v5;
`expand()` and the page's `expandSteps()` read v3 to v6 (a file without clusters is a v5 file).

- Blob field `clusters` (after `funcs`): `[{"id", "cap", "emoji", "funcs": [qual, ...]}]`, sorted by id, never
  empty. Every function of the blob is in exactly one entry, `funcs` in the blob's order. `id` is an int, 0 for
  the loose ends (functions with no call inside the blob; `cap` "loose ends", 🧷), others from 1, never reused
  for that blob while it stays clustered. `cap` is 1-2 words and `emoji` one emoji, both guessed from the names
  (below). Reserved: `label: {"title", "emoji"}` on an entry, for `label.py` to name clusters later (not written
  yet); the page shows it over `cap` / `emoji`.
- Grouping (`clusters.py`, the approved page draft's, bit for bit): Louvain modularity over the blob's own calls,
  undirected, weighted by call sites (resolution 1.3; the visit order shuffled by the page's `rng()` seeded 7);
  functions with no call inside the blob are the loose ends; a single-function community joins the one it has the
  most call sites with per sqrt(its lines); then the smallest by lines joins its best-linked one the same way (the
  loose ends when it has no link) until at most round(1.1 x sqrt(functions)) clusters are left, clamped to 3..14,
  loose ends included (12-15 functions: 4; 30: 6; 39: 7; 86: 10; 162+: 14). The draft clamped to 6..14, which only
  bound under 30 functions (15 functions forced into up to 6 clusters); from 30 functions on the bounds are the
  draft's. Deterministic: nothing depends on dict order of unordered data or the hash seed.
- Stability: a blob's clusters keep their ids and members from step to step, as areas do. The fresh grouping is
  recomputed only when the blob's functions, their lines or the calls between them changed; each fresh group is
  matched to the cluster it overlaps most (lines + 1 per function, greedy; the loose ends always to 0). A function
  new to the blob joins its fresh group's cluster (an unmatched group opens one; at the cap, it joins the cluster
  it has the most call sites with, then the smallest, then the lowest id); a function gone leaves; the others move
  only when the fresh grouping is adopted wholesale (matched groups keep their ids, others get new ones), which
  needs both: the same fresh grouping has come out on each of the 40 steps after the one it first came out on
  (`areas.HOLD`; or half the steps the blob has existed, when shorter), and it would change some cluster's
  members by more than half of their lines (`areas.REGROUP`); or at once, when the clusters would exceed the cap or
  fewer than 3 are left besides the loose ends while the fresh grouping has 3+.
- Clustered or plain holds the same way: the verdict (12+ functions and 3+ fresh groups) changes a blob's view only
  once it has come out the same on each of the 40 steps after the one it first came out on (or half the steps the
  blob has existed, when fewer): the verdict on the step a blob first appears applies at once; a blob that has been
  around and reaches the line (or drops under it) waits the hold, so it is never clustered for a step or two. A blob crossing the line back and forth keeps its view (a
  blob turned plain shows no `clusters`; one kept clustered under the line shows what it had, updated as above).
  Turned on, a blob adopts the fresh grouping with new ids (never reused while it exists); turned off, it forgets its
  members. A held view is never a single cluster: while fewer than 2 are left besides the loose ends (a blob shrunk
  to a function or two) the blob has no `clusters` that step, and goes on as clustered.
- Captions: the members' first name word (split at `_`, other non-alphanumerics and camelCase humps; `tl` ->
  `timeline` and the draft's other abbreviations) weighted by sqrt(lines) + 1, when it covers half the cluster;
  else the best-connected member's first two words; a caption another cluster took gets the hub's words. Emoji: the
  first keyword of `clusters.EMO` in the caption, else in the hub's and the four biggest members' names, else 🧩.
- Encoding: a full step's blobs carry `clusters` whole. A delta step's sent blob (v4 rules) carries it as a patch
  on the base's table when the base had one: `clusters` holds only the entries new or changed (`[]` when none),
  `clusters_remove` (only when not empty) the ids gone. Rebuild: the base's table minus `clusters_remove`, the
  sent entries replacing or adding by id, sorted by id; `clusters_remove` is not part of the rebuilt blob. Without
  a base table `clusters` is whole; a sent blob without `clusters` has none. The writer sends the step in full when
  a table cannot be patched (ids not ints, not increasing, an empty table, a blob with a key `clusters_remove`).
  v3 (`compact(steps, 3)`) sends the table whole with the blob, like any field.
- Measured on the aigource map (1,270 steps, README build with --diffs): 28 blobs get clusters on 5,143 blob-steps;
  the file grows 8.67 -> 8.93 MB (+2.9%; whole tables on every re-sent blob would be 10.22 MB). Clustering costs
  ~2 ms per clustered blob-step; build 2:57, max RSS 651 MB (650 MB before). `--diffs` leaves the file as it was.

## v6 additions (2026-11-10): file formats, links between files, thumbnails

Still `"schema": 6`: nothing about the encoding changed, and a v6 reader reads these files as before.

- Blob field `fmt` (file / text blobs only, after `path`, only when known; `analyze.fmt_of()`, case-insensitive).
  By basename first (`analyze.NAME_FMT` and the patterns): `ignore` (`.gitignore`, `.dockerignore`, any `.xignore`),
  `make` (`Makefile`, `makefile`, `GNUmakefile`, `*.mk`), `docker` (`Dockerfile`, `Dockerfile.*`, `*.dockerfile`), `env`
  (`.env`, `.env.*`), `editorconfig`, `git` (`.gitattributes`, `.gitmodules`), `license` (`LICENSE` / `LICENCE` /
  `COPYING`, any extension), `pipreq` (`requirements*.txt`, `constraints*.txt`), `lock` (`*.lock`, and
  `package-lock.json`, `npm-shrinkwrap.json`, `pnpm-lock.yaml`, `go.sum`: generated pins, not data to read, so they are
  badged as lockfiles and sit in `files`), `cmake` (`CMakeLists.txt`, `*.cmake`), `procfile`. Then by extension
  (`analyze.FMT`; a dotfile such as `.json` has none). Data: `json`, `jsonl` (`.jsonl` `.ndjson`), `csv`, `tsv`, `xml`,
  `toml`, `yaml` (`.yaml` `.yml`), `ini` (`.ini` `.cfg`); docs: `md` (`.md` `.markdown`), `html` (`.html` `.htm`);
  `css` (`.css` `.scss` `.sass` `.less`); assets: `image` (`.png .jpg .jpeg .gif .webp .ico .bmp`), `svg`, `font`
  (`.woff .woff2 .ttf .otf .eot`). Nothing is parsed for `fmt`; `kind` is `file` / `text` by references, as before
  (references now include links from other files, below).
- Areas (`areas.file_area`): `data` holds the file / text blobs of a data `fmt` (`analyze.DATA_FMTS`), referenced or
  not; `docs` the markdown ones and the html the app does not use; `files` the rest, including the html code uses
  (`areas.app_html()`: the html a code blob's `ref` edge names, a template, e.g. Flask's `templates/*.html`,
  aigource's `draft/*.tpl.html`, and the html those link in turn, transitively, e.g. the base template they extend).
  It goes by code references, not by `kind`: a page only other files link (a README's link to a demo page, pages
  linking each other) is `file` but stays in `docs`. An html file moves between `docs` and `files` when code starts or
  stops using it (Flask: `templates/base.html`, written before anything extends it, one step in `docs`). `assets` holds
  images, svg and fonts. Captions `Data` / `Docs` / `Assets` (`Files` stays `Files`: what is left there, build files,
  ignore files, licenses, lockfiles, styles, templates, is the app's own plumbing, and the badges already tell them
  apart), faces as for any area (from label emojis). Like `tests` and `files` they are fixed ids outside the code-area
  grouping, its stability rules and its 12-area cap (the top view shows at most 12 code areas plus these five).
- Why no schema bump: `fmt` rides the blob fields, which the delta encoding carries whole (Compactor compares blobs
  whole, `expand()` / `expandSteps()` copy them, v4 patches cover `funcs` only); it derives from the path in the blob's
  id, so it never changes for a blob and never causes a re-send. The new area ids come with their own entries in the
  areas table. A reader that does not know `fmt` shows data / doc files as plain pages and `data` / `docs` as areas with
  their captions. What no longer holds is v5's wording that every file / text blob is in `files`.
- Labels: a file / text blob's cache key is its path and head hash, as before, so the move costs no label request.
- Links between non-code files (`ref` edges from a `file:` blob; `analyze._file_links()`, `_resolve_link()`): pages
  (`.html .htm .xhtml` and templates `.jinja .jinja2 .j2 .hbs .handlebars .njk .mustache`), styles (`.css .scss .sass
  .less`) and markdown are read with tolerant regexes, not parsed: html `href` / `src` / `poster` / `data-src`
  attributes (`<link>`, `<script>`, `<img>`, `<source>`, `<a>`, `<iframe>` ...), `srcset` candidates, `url(...)` in
  styles and `style=""`; Jinja `{% extends / include / import / from "x" %}` and `url_for('static', filename='x')`; css
  `@import "x"` and `url(x)`; markdown `[t](x)`, `![t](x)`, `[id]: x` (and html inside markdown). A link counts when
  it is a literal path that names a snapshot file: query, fragment and %-escapes dropped; a scheme (`http:`, `data:`,
  `mailto:`, `javascript:` ...), `//host`, `#anchor` or template expression (`{{ }}`, `${}`) is no link. Paths resolve
  from the linking file's directory; `/x` from its nearest ancestor that has `x` (an app in a subdirectory serves its
  own `static/`), the root last; Jinja names from the nearest `templates` directory above it, then its own;
  `url_for('static', ...)` from `static/` beside that templates directory; failing those, a Jinja / static name that
  exactly one `templates/` / `static/` path ends with. Edges are directed (linking file -> linked file), `n` = links,
  `via` = the literals as written; being linked makes a blob `file`, so a stylesheet a page loads is a `file`. They are
  read from every such file, linked or not, in one pass (no fixpoint: a link never depends on another). A link to a code
  file counts only from `<script src>` (also `url_for('static', ...)` in one) and only to a JS / TS module (lang_js;
  a `.py` in a `<script src>` is nothing): the edge goes to the blob holding that module's `<module>` code, which runs when the page loads it and so becomes an entry root (a top, as for a script a
  shell runs); a module with no top-level code draws no edge. JS under `static/` that analyze() demotes to a file
  blob is linked as a file. The recount after folds and cuts keeps these edges, a page's script edge moving with its
  module blob. Handlebars partials (`{{> name}}`) are not resolved. Costs: every repeat in the patterns is bounded and
  possessive (an attribute value 2 KB, a `url()` 512 bytes, link text 1,000 characters, a markdown link found from its
  `](` with a bounded look back), so reading a file is linear in its size (400 KB of unclosed `url(` or `[`: a few
  ms); a file over 2 MB (`analyze.LINK_MAX`) is not read for links.
- One hop on (v2 `ref`): the quoted literals of a file code references still name files, except from a data `fmt`
  (an expected-output `.ndjson` that mentions `README.md` draws no `ref` to it) and from pages, styles and markdown,
  whose links above replace it.
- Thumbnails (`thumbs.py`; diffs sidecar, `timeline.py --diffs`, live stores): file line key `t` {path: hash | null},
  the image / svg blobs whose thumbnail changed since the previous analysed step (null: gone, or none now); the text
  under the hash is a `data:` URI. Raster images: their bytes are found back in git (the object whose bytes decode, with
  replay's 'replace', to the snapshot's text; any commit, any path; none or several different ones: no thumbnail),
  never read from the disk; Pillow makes a PNG of at most 64 px on the long side (aiblob's 298 KB screenshot: 5.3 KB);
  without Pillow an original of at most 24 KB is used as it is. Bounds: git blobs over 8 MB are never read (sizes
  asked first, `thumbs.BLOB_MAX`), an image over 16 Mpx is never decoded (`thumbs.MAX_PIXELS`, from its header; a
  10 KB PNG of 9000 x 9000 pixels costs nothing), a JPEG decodes at a reduced scale; the index keeps one blob sha per
  text, a hit reads its blob again. svg: its own text (at most 64 KB), base64; a reader
  draws it through an image (`<img>` / `Image()` on a canvas), never inlined, so its scripts never run. A thumbnail is
  made once per image content and changes only when the image's bytes do. Why the diffs sidecar and not one of its own:
  thumbnails are small (a few KB each, one per image version) next to the texts the sidecar already holds, and the
  sidecar already has every piece they need: hashed texts written once, a per-step line, the static layout, the live
  store and its `/live/bodies` endpoint, live == batch. timeline.json is byte-identical with or without it. Known
  limits: replay's 'replace' decoding maps every invalid byte to U+FFFD, so two different committed images can decode
  to the same text (differing only where the bytes are invalid UTF-8); such an image gets no thumbnail. A repository
  whose `.gitattributes` normalises line endings for binaries (`* text`, `*.png text`) holds its images with CRLFs
  turned to LFs, i.e. corrupt: Pillow rejects them, so they get no thumbnail (the badge only); `*.png binary` or
  `* text=auto` (git's own binary detection) keeps them whole. In live mode a
  thumbnail is found from the commits that exist when its step is analysed; replay puts an image in a snapshot only
  from git, so that commit exists then (a later commit whose image decodes to the very same text could make batch and
  live disagree on that ambiguity).
- Generic environment variables (all languages; `analyze.GENERIC_ENV`, `analyze.shared()`): `env:` symbols of `HOME
  PATH USER LOGNAME PWD OLDPWD SHELL TERM LANG TMPDIR TMP TEMP HOSTNAME EDITOR PAGER DISPLAY XDG_RUNTIME_DIR` and any
  `LC_*` stay in the blob's and function's `data` but make no `data` edge, in analyze() and in the recount after folds
  and cuts, and do not count as shared data when a small blob picks the blob to fold into (as `ctx:request`). They are
  read by nearly every program, so they say nothing about shared state. `XDG_DATA_HOME`, `XDG_CONFIG_HOME` and
  `XDG_CACHE_HOME` do link: they say where an app keeps its own files. App-specific ones (`CLAUDE_CONFIG_DIR`,
  `AIG_NVIM_DIR`, ...) link as before. Python emits no `env:` symbols; C, Go and shell do.
- Measured (2026-11-10, before the formats, links and thumbnails above; same transcripts frozen, old and new code): aigource (1,270 steps): at the last
  step 33 blobs in `data` (jsonl fixtures and expected outputs, json), 10 in `docs` (md), 5 left in `files` (`Makefile`,
  `.gitignore` x 2, `draft/structure.tpl.html` and `draft/context/context.tpl.html`, which the draft scripts reference).
  File 9.08 -> 9.23 MB, the shell scripts' code blobs included. Data edges at the last step 99 -> 92: the shell scripts
  add `env:` links (`CLAUDE_CONFIG_DIR`, `AIG_NVIM_DIR`, `AIG_NO_DEFS`, `XDG_DATA_HOME`), generic variables drop 28
  `env:HOME`-only edges between C hubs that the old map had. label.py with the committed cache: 0 requests before, 39
  after, none for a file / text blob (23 shell-script versions, 14 versions of Python blobs a script now runs, 2
  pieces). aiblob (1,064 steps, measured before the html and env rules): at the last step 37 in `data` (perf csv /
  summary json, label caches, fixtures), 8 in `docs`, 12 left in `files` (`Makefile`, `.txt`, `.css`, `.png` ...);
  `record.sh` and `serve-llm.sh` are code blobs now.

## Sessions (2026-11-06)

A timeline can merge several transcripts (`timeline.py a.jsonl b.jsonl ~/.claude/projects/<dir> ...`; a directory means
all its `*.jsonl` sessions plus each one's `<session>/subagents/*.jsonl`). Steps of all of them are ordered by timestamp
(a Write / Edit at its tool call, a bashEditDiff file at its tool result) and applied to one snapshot; a tool call
copied into several transcripts (a resumed session) counts once. The top-level `session` is then the comma-joined
short ids. Optional step fields, written only when they say something (a single-transcript timeline has none of them
unless an edit failed or was seeded, so its bytes stay as before):
- `session`: the 8-char session id (a subagent's is its parent session's); `agent`: `main`, or a subagent's
  `type · description` from its `agent-<id>.meta.json` (`executor · P2a: analysis core port`). `turn` is renumbered
  across transcripts in order of first appearance, one number per (transcript, prompt).
- `tool` `Bash` also covers one file of a command's `bashEditDiff` (the hunks Claude Code recorded for it).
- `applied: false`: the edit did not fit the snapshot (an Edit's `old_string` not in the file or no file; a
  bashEditDiff hunk that fits nowhere). The step is kept with `added` / `removed` 0 and the snapshot stays as it was
  (a missing file is not created). A Write always applies (the first full Write of a file is its base).
- `seeded: true`: the step's base came from outside the replayed edits. Rule (`--seed-git`): start from an empty
  snapshot and apply the steps; when the first touch of a file is an Edit or a non-created bashEditDiff (no base),
  the base is `git show <last commit before that step's ts>:path` of root's repository (`git log --before`).
  Also seeded: an Edit whose tool result recorded the file before it (`originalFile`) differing from the snapshot
  (that text is the base), and a failing bashEditDiff that fits the file as the last whole-file Read showed it.
  Before giving up, a failing bashEditDiff is also tried on the file's earlier texts (Claude Code diffs against
  the text it last saw), and a hunk whose changed lines no longer match but whose context lines do replaces what
  lies between them; both are not marked seeded. With `--seed-git`, the files tracked in the last commit before
  the last step that no step touches are in every snapshot, from that commit, so the names they define resolve.
`timeline.py --sessions-out F` writes the sessions with steps: `[{"id", "uuid", "start", "end", "steps", "prompt"}]`
(`id` as the steps' `session`, start / end over the session and its subagents, `prompt` the first line of the first
typed prompt, 120 chars).
When the timeline has steps from more than one session, the same list is also the top-level `"sessions"` array of the
timeline (v3 and --v2), right after `"session"`; with one session the key is absent (byte-identical to before).
- `author`: on every step of a merged or reconciled timeline (every step that has a `session`), `"git"` on git steps
  and `"ai"` on the others; a single-transcript timeline without git steps has no `author` (all `"ai"`). With
  `--git-reconcile`, each commit touching root authored from the first step on, up to HEAD (so the last snapshot is
  HEAD's tree plus files git never tracked), is a step at its author time: `tool` `"git"`, `session` and `agent` `"git"`, `file` `""`, `said` the commit
  subject, a `turn` of its own, and the usual blobs / edges / stats. It sets every file under root the commit
  tracks to the commit's text and deletes the files the previous commit tracked that it does not (files git never
  tracked stay); `added` / `removed` are the real line diff between the snapshot and the commit tree. A commit
  that matches the snapshot is kept as a marker with `"empty": true` (`added` / `removed` 0). Edits after it apply
  on the committed text, so drift from changes the transcripts never recorded ends at the next commit.
- Ordering and credit: a directory's sessions are read in the order of their first ISO timestamp (records whose
  `timestamp` is not an ISO string, e.g. integer ms, are skipped here and in `--sessions-out` start / end), so a call
  copied into a resumed session counts for the original. On a tie (a resumed file that copies the original's very
  first record) the file whose first record the other lacks is earlier, and a file with no such record (all its
  records copied into the other: the original) comes first; then the file name. A dropped copy shares the
  original's text, so the resumed file's next call does not repeat it as `said`. A bashEditDiff change of a file that another transcript
  also changed while the Bash call ran (between its tool call and its result) is that transcript's: an Edit /
  Write timed inside the window, or an overlapping Bash change whose call ranks higher, or ranks the same and has
  the shorter window. A Bash call's rank, per file, from low to high: blind (below), runs under root (its cwd, as
  the transcript records it, is under root, or the command names root's path or basename), runs under root and
  names the file (its absolute path as a whole word; or its bare basename as a word; or a name with a directory
  part that resolves to the file from the call's cwd or the `cd` dirs the command ends in: `docs/README.md` does
  not name `README.md`). A relative name counts only under root: from another repo, `README.md` is that repo's
  file. A Write / Edit ranks top. Known limits, by design: a call from an unrelated cwd whose command mentions
  root's basename as a word (`ls ../aigource`) counts as running under root, and so its bare `README.md` names
  root's; a bare basename is not resolved, so `cd ../other && sed … README.md` from root, or `README.md` from
  root's `docs/`, still names root's `README.md`. The report is
  ordered right after that change, which is applied and credited there; what is left of the report (often
  nothing, then no step) stays the Bash call's when it applies. Such a report never makes an `applied: false`
  step: when what is left fits nowhere, the call only saw the other transcript's change (e.g. a smoke test
  running while another session patched the file) and makes no step. A long-running call (a build, a sleep)
  thus never takes credit, or blame, for a change another agent made meanwhile.
- Blind reports: a Bash call that neither names the file nor runs under root (another project's tests, a
  tmux / nvim check from elsewhere) only saw the change, whoever made it, even when no other transcript recorded
  it (e.g. a `sed -i` that a call undid before its own diff was taken). Such a report makes no step and takes no
  credit; its text of the file, when it fits, serves like a whole-file Read: a later failing report that fits
  only that text applies on it (`seeded: true`), so a change the transcripts never recorded still reaches the
  snapshot. Such a result (from a Read's text too) is merged with what the snapshot changed in the file since
  that text was seen (another edit, a git step): `merge3` runs `git merge-file` with the snapshot's text then as
  the base, the snapshot's text now as one side and the result as the other. A clean merge is the step's text;
  a conflict is `applied: false`. The guarantee is diff3's, no more: changes that touch the same or adjacent lines
  conflict, and where lines repeat (`}`, blank lines) a clean merge can still pair them differently than the
  edits meant, e.g. keep a change twice when the result already carried it.
  Costs: a real change made by a call that names neither the file nor root (a script run from outside
  root) is not a step; and since an Edit / Write always ranks top, a Bash call that authored a change of a file
  another session edited inside its window, and whose leftover does not fit, makes no step (no `applied: false`)
  rather than a phantom one for the watcher. The next git step resets either drift.
- A Write / Edit / MultiEdit whose tool result is an error changes nothing (`applied: false`). An Edit that does not
  fit and has no `originalFile` applies its own recorded hunks (`structuredPatch`) the way bashEditDiff hunks are
  applied. A failing bashEditDiff retried on an earlier text of the file is merged the same way: the hunks
  applied exactly at their lines of that text, then `merge3` with that text as the base and the snapshot's text
  as the other side (a conflict: the next earlier text, else `applied: false`). A hunk whose
  new lines already stand at its line counts as done only when it has context lines (a context-free `+a` before an
  `a` is inserted). Heredoc paths follow the `cd dir` commands in effect (heredoc bodies and quoted strings are no
  commands, a `cd` in `( ... )` ends at `)`, one in a pipeline such as `cd a | cat` does not last, one after
  `then` / `do` / `{` does, one in a background job (`cd a &`) or a function body (`f() { cd a; }`) does not, a
  `case` pattern's `)` closes nothing, a `cd` in a `case` arm makes the directory unknown, `&>file` is a
  redirection, `cd -P dir` / `cd -- dir` / `x=1 cd dir` / `builtin cd dir` /
  `command cd dir` go to dir); after `cd -` or a bare `cd` the heredoc is skipped.

## Diffs (2026-11-09): texts for diffs inside blobs

The timeline never carries code text beyond `snippet`; the texts a diff needs sit beside it, keyed by the hash the
steps already carry: a function's `hash` (sha1 of its body, first 10 hex, analyze.py) and, for a file / text blob,
the same hash of the whole file text. timeline.json is byte-identical with or without them (`timeline.py --diffs`),
and live steps.ndjson still equals `timeline.py --no-timings`. Writer and reference reader: diffs.py.

Per step a **file line** `fv` (keys only when not empty, `{}` when nothing):
- `f`: {path: hash | `"bin:N"` | null}: the file / text blobs whose text changed since the previous analysed step
  (null: the blob is gone; `"bin:N"`: a binary file, N the UTF-8 bytes of its snapshot text, no text stored). F(i),
  the file map after step i, is F(i-1) with `f` applied (F(-1) = {}). A step whose analysis failed has no `f` (its
  state is the previous one).
- `seed`: {path: hash | `"bin:N"`}: on a `seeded` step, the text the edit applied to (the git base, an Edit's
  `originalFile`, a Read's text), when it is not what the snapshot held.
- `sq`: {qual: hash}: that base's body of each function the seeded file has after the step, by qual, from the
  analyzer's own parse of the base (analyze.load_module, the file's language): exact, `<module>` and carved bodies
  included; a qual the base does not define has none. Where the base does not parse, the bodies are sliced by a line
  diff of base and the file after the step (lines split at `\n` only, as tree-sitter counts them) and their quals are
  listed in `sqa` [qual, ...] (approximate).

Where they are:
- Static (`timeline.py ... --diffs DIR`, `live.py export --diffs DIR`, the same bytes for the same steps): plain
  gzip files a static server serves as they are (the page: `fetch` + `DecompressionStream('gzip')`).
  `DIR/index.json.gz` {"v": 1, "steps": N, "fv": [file line per step], "chunks": {"<step>": [hash, ...]}} and
  `DIR/s/<step>.json.gz` {hash: text}, the texts first named at that step (`step` = the step's place, = `idx`).
  To fetch hash h: its chunk is the step whose list holds it.
- Live (liveserve.py): each served step carries `fv` (left out when `{}`), committed and provisional alike, and
  `/live/timeline.json`'s "live" says `"diffs": true` (false: a store written before diffs, no `fv`, every text
  unknown). `GET /live/bodies?h=H1,H2,...` (1 to 256 hashes of 10 lowercase hex, else 400) -> {hash: text, or null
  when the store has none}; same Host check and timeouts as the other endpoints. The server checks the pack on every
  request and each line it reads back against the hash asked (a store recreated, cut or damaged under it: read again
  once, else null). A tail is served only with its file lines (else the tail served before, or none). The store: live/NAME/diffs/
  (bodies.ndjson, steps.ndjson, tail.json; live.py's docstring). Every hash a served step names is there before the
  step is: texts are appended before the lines that name them, a step's file line before the step.

A client's diff of blob B from before step a to after step b (0 <= a <= b; this step: a = b; since the start:
a = 0; since step N: a = N). Old state = the expanded step a-1 (none for a = 0), new state = step b; texts by hash
from the store (only those not cached). `diffs.Reader.range_texts(a, b, B)` is the reference; the page mirrors it.
- Code blob: its family is B plus its cut pieces (blobs whose `cut` is B) in both states. Functions: the quals of
  the family's functions at b, then those at a-1 that b's family lacks. Group: the `merged` entry whose `funcs`
  names the qual (a folded-in blob), else the piece or B (pieces show as collapsed sections).
  - At b: new = its hash. Old = the qual's hash anywhere in the old state (`moved_from`: its old blob, when that
    is not in the family); else the earliest `sq` of the qual at steps a..k, k the first step of a..b showing the
    qual (`seeded`; `approx` when the qual is in that step's `sqa`): a seed after the step that made it is no base of
    it; else none (new).
  - Only at a-1: old = its hash; when the qual is elsewhere at b, `moved_to` (that blob) and new = its hash there;
    else deleted (old text from a-1's hash, which the store always holds).
  - Renames: a new function (no old) takes the old side of a deleted one with the same hash (a move to another file:
    another qual, the same body), else of one with the same `name` (`renamed_from`), each at most once.
  - State: new (no old), deleted (no new), same (equal hashes), changed.
- File / text blob (its `path`): old = F(a-1)[path], else the earliest `seed` of the path at a..k, k the first step
  of a..b whose F has it (`seeded`); new
  = F(b)[path] (none: deleted; the blob, gone at b, is found at a-1). A new path whose text hash equals that of a path
  in F(a-1) gone from F(b) is that path renamed (`renamed_from`). `"bin:N"` on either side: binary, no texts.
- Display: a client line-diffs old against new (Myers; in a Web Worker for big files); `<module>` bodies and carved
  ones are joined statements (no line numbers); a function body starts at its definition (decorators included; a
  JS callback can start mid-line). A step whose edit did not apply changes nothing; the next step's diff covers both.
- Costs (aigource): PLAN.md "Diffs inside blobs, data side outcome". Provisional texts never committed stay in the
  live pack (each once); `live.py export --diffs` writes only what the steps name. 10-hex collisions (~7e-6 over the
  store) are the timeline's own blind spot.
