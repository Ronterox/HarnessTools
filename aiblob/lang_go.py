"""Go for analyze.py: packages are directories, every call is placed by link() with Go's own scoping rules.

GoLang(Func) implements the hooks PyLang has (see analyze.py). Module names come from paths like C's:
`internal/auth/login.go` -> `internal.auth.login`, `main.go` -> `main`. A package is the directory (plus the
package clause, so `foo_test` next to `foo` is its own package); an import path names a snapshot directory
through the module path of the snapshot's go.mod (a `go.mod` is read by this layer, so it is no file / text
blob), anything else (stdlib, third-party) is external.

Calls: every call / function value is a site with an import key `__goN` -> `<module>.#N`; link() decides each
key's alias: the qual it names (package-wide for bare names, `pkg.F` through the import, `x.M()` by x's type
from params, receivers, `var x T`, `x := T{}` / `&T{}` / `new(T)` / `NewT()` results, struct fields, range
elements, else the one method of that name the caller can see), `ext:go` when it is not the program's (stdlib,
third-party, builtins, conversions, func values, interface calls with several implementations), or no alias:
a name that should be program code and is not there, which analyze() counts as unresolved.

Roots: `func main` of package main is the entry (merged with the file's package-level code; the file's
`init`s join it, another package main's main is an entry of its own); `init` elsewhere is a `hook`; routes
from net/http / chi / gorilla / gin / echo / fiber registrations (id the handler's qual, inline func literals
carved out as `<GET /p>`), `X.Use(mw)` hooks, cobra / urfave-cli commands; `_test.go` files and files under
`tests/` / `test/` are a tests blob each. Data symbols are found per file (static, see SCHEMA.md), never from
another file's declarations.
"""
import re, hashlib, posixpath, fnmatch, collections
from tree_sitter import Language, Parser
import tree_sitter_go as _tsgo

_GO = Parser(Language(_tsgo.language()))

BUILTIN_FUNCS = frozenset('append cap clear close complex copy delete imag len make max min new panic print println '
                          'real recover'.split())
BUILTIN_TYPES = frozenset('bool byte complex64 complex128 error float32 float64 int int8 int16 int32 int64 rune '
                          'string uint uint8 uint16 uint32 uint64 uintptr any comparable'.split())
BUILTIN_IDENTS = frozenset({'nil', 'true', 'false', 'iota', '_'})
# calls to these never get a site unless the file itself defines the name (min / max / clear still may be a
# pre-1.21 helper of another file, so they get one)
NO_SITE = (BUILTIN_FUNCS | BUILTIN_TYPES) - {'min', 'max', 'clear'}
# first segments of standard library import paths: their calls are dropped at parse time
STDLIB_TOP = frozenset('''archive bufio builtin bytes cmp compress container context crypto database debug embed encoding
errors expvar flag fmt go hash html image index io iter log maps math mime net os path plugin reflect regexp runtime
slices sort strconv strings sync syscall testing text time unicode unique unsafe weak C'''.split())
VERBS = {'Get': 'GET', 'Post': 'POST', 'Put': 'PUT', 'Delete': 'DELETE', 'Patch': 'PATCH', 'Head': 'HEAD',
         'Options': 'OPTIONS', 'Connect': 'CONNECT', 'Trace': 'TRACE', 'Any': '', 'All': '',
         'GET': 'GET', 'POST': 'POST', 'PUT': 'PUT', 'DELETE': 'DELETE', 'PATCH': 'PATCH', 'HEAD': 'HEAD',
         'OPTIONS': 'OPTIONS', 'CONNECT': 'CONNECT', 'TRACE': 'TRACE'}
NOT_FOUND = {'NotFound': 'error 404', 'NoRoute': 'error 404', 'MethodNotAllowed': 'error 405', 'NoMethod': 'error 405'}
PREFIXING = {'Group', 'PathPrefix', 'Subrouter', 'With', 'Route'}
ROUTER_OBJ = re.compile(r'(?:r|e|g|app|api|mux|router|srv|server|engine|v\d+|\w*[Rr]outer|\w*[Mm]ux|\w*[Gg]roup|'
                        r'\w*[Aa]pi|\w*[Aa]pp)')
ERROR_NAME = re.compile(r'\w*(?:Error|Err|Fail|Fatal|Panic|Abort)|(?:error|err|fail|fatal|panic|abort)(?:[A-Z_]\w*)?')
ERR_IDENT = re.compile(r'(?:err|Err)\w*|\w*(?:Err|Error)')
ERROR_CTORS = re.compile(r'(?:\w+\.)?(?:Errorf|NewHTTPError|NewError|Wrap|Wrapf|WithMessage|WithStack)|errors\.New|'
                         r'status\.Error')
FATAL = re.compile(r'(?:\w+\.)*(?:Fatal|Fatalf|Fatalln|Panic|Panicf|Panicln)')
STATUS_CALLS = frozenset({'JSON', 'String', 'XML', 'HTML', 'IndentedJSON', 'PureJSON', 'Data', 'Status', 'WriteHeader',
                          'SendStatus', 'NoContent', 'SendString', 'Blob', 'Render', 'AbortWithStatus',
                          'AbortWithStatusJSON'})
# net/http status constants below 400: any other Status* constant is an error status
OK_STATUS = frozenset('''StatusContinue StatusSwitchingProtocols StatusProcessing StatusEarlyHints StatusOK StatusCreated
StatusAccepted StatusNonAuthoritativeInfo StatusNoContent StatusResetContent StatusPartialContent StatusMultiStatus
StatusAlreadyReported StatusIMUsed StatusMultipleChoices StatusMovedPermanently StatusFound StatusSeeOther
StatusNotModified StatusUseProxy StatusTemporaryRedirect StatusPermanentRedirect'''.split())
ENV_CALLS = frozenset({'Getenv', 'LookupEnv', 'Setenv', 'Unsetenv'})
FILE_CALLS = frozenset({'Open', 'OpenFile', 'Create', 'ReadFile', 'WriteFile', 'Remove', 'RemoveAll', 'ReadDir',
                        'Mkdir', 'MkdirAll', 'Stat', 'Lstat', 'Truncate', 'Chmod'})
SCOPES = {'block', 'if_statement', 'for_statement', 'expression_switch_statement', 'type_switch_statement',
          'select_statement', 'expression_case', 'type_case', 'communication_case', 'default_case'}
BRANCH_TYPES = {'if_statement', 'for_statement', 'expression_case', 'type_case', 'communication_case'}
TYPE_NODES = {'slice_type', 'array_type', 'map_type', 'channel_type', 'pointer_type', 'function_type',
              'interface_type', 'struct_type', 'qualified_type', 'type_identifier', 'generic_type'}
SKIP_TYPES = TYPE_NODES | {'comment', 'field_identifier', 'package_identifier', 'label_name', 'type_parameter_list',
                           'type_arguments', 'import_declaration', 'type_declaration'}
SQL = re.compile(r'\b(?:FROM|INTO|UPDATE|TABLE(?: IF NOT EXISTS)?|JOIN)\s+([A-Za-z_][A-Za-z_0-9]*)', re.I)
SQL_STMT = re.compile(r'\s*(?:SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|REPLACE|WITH|UPSERT)\s', re.I)
PATH = re.compile(r'[\w.@+~-]+(?:/[\w.@+~-]+)*/?')
GLOB = re.compile(r'[\w.@+~*?\[\]-]+(?:/[\w.@+~*?\[\]-]+)*/?')
ECHO = 'github.com/labstack/echo'
ROUTERS = ('github.com/gin-gonic/gin', 'github.com/go-chi/chi', 'github.com/gorilla/mux', 'github.com/gofiber/fiber',
           'github.com/julienschmidt/httprouter')
EXT, DYN, MISS, FUNCT = ('ext',), ('dyn',), ('miss',), ('F', (), None)


def _text(n, src):
    return src[n.start_byte:n.end_byte].decode('utf-8', 'replace')


def _hash(s):
    return hashlib.sha1(s.encode()).hexdigest()[:10]


def _cut(src, start, end, carved):
    parts, pos = [], start
    for s, e, _ in sorted(carved):
        if s >= start and e <= end and s >= pos:
            parts.append(src[pos:s]); pos = e
    parts.append(src[pos:end])
    return b''.join(parts).decode('utf-8', 'replace')


def _strval(n, src):
    if n is None or n.type not in ('interpreted_string_literal', 'raw_string_literal'):
        return None
    t = _text(n, src)
    return t[1:-1] if len(t) >= 2 else None


def _args(call):
    a = call.child_by_field_name('arguments')
    return [c for c in a.named_children if c.type != 'comment'] if a is not None else []


def _stmts(block):
    """The statements of a block (`{ statement_list }`)."""
    sl = next((c for c in block.named_children if c.type == 'statement_list'), None) if block is not None else None
    return [c for c in sl.named_children if c.type != 'comment'] if sl is not None else []


def modname(rel):
    if posixpath.basename(rel) == 'go.mod':
        d = posixpath.dirname(rel)
        return (d.replace('/', '.') + '.' if d else '') + 'go_mod'
    stem = rel[:-3] if rel.endswith('.go') else rel
    parts = stem.split('/')
    return '.'.join(p.replace('.', '_') for p in parts)


def is_test_file(rel):
    """A `_test.go` file, or any file under a `tests/` or `test/` directory (as Python's, JS's and C's tests)."""
    return rel.endswith('_test.go') or bool({'tests', 'test'} & set(rel.split('/')[:-1]))


def import_name(path):
    """The package name an import path binds by default: github.com/go-chi/chi/v5 -> chi, gopkg.in/yaml.v3 -> yaml."""
    segs = path.split('/')
    last = segs[-1]
    if re.fullmatch(r'v\d+', last) and len(segs) > 1:
        last = segs[-2]
    last = re.sub(r'\.v\d+$', '', last)
    last = re.sub(r'^go-|[-.]go$', '', last)
    return last.replace('-', '_').replace('.', '_')


def stdlib(path):
    return path.split('/')[0] in STDLIB_TOP


def type_expr(n, src):
    """A type node -> ('n', package alias or None, name) | ('e', element) | ('f',) | ('x',) (anything else)."""
    if n is None:
        return None
    t = n.type
    if t in ('pointer_type', 'parenthesized_type', 'type_elem', 'type_constraint'):
        inner = n.named_children[-1] if n.named_children else None
        return type_expr(inner, src)
    if t == 'type_identifier' or t == 'identifier':
        return ('n', None, _text(n, src))
    if t == 'qualified_type':
        return ('n', _text(n.child_by_field_name('package'), src), _text(n.child_by_field_name('name'), src))
    if t == 'generic_type':
        return type_expr(n.child_by_field_name('type'), src)
    if t in ('slice_type', 'array_type', 'implicit_length_array_type'):
        return ('e', type_expr(n.child_by_field_name('element'), src))
    if t in ('map_type', 'channel_type'):
        return ('e', type_expr(n.child_by_field_name('value'), src))
    if t == 'function_type':
        return func_type(n, src)
    if t == 'selector_expression':                 # a qualified type in expression position: new(pkg.T)
        op = n.child_by_field_name('operand')
        if op is not None and op.type == 'identifier':
            return ('n', _text(op, src), _text(n.child_by_field_name('field'), src))
    return ('x',)


def func_type(n, src):
    """A function type / func literal -> ('f', (result type, ...))."""
    res = n.child_by_field_name('result')
    if res is None:
        return ('f', ())
    if res.type != 'parameter_list':
        return ('f', (type_expr(res, src),))
    out = []
    for p in res.named_children:
        if p.type == 'parameter_declaration':
            out += [type_expr(p.child_by_field_name('type'), src)] * max(1, len(p.children_by_field_name('name')))
    return ('f', tuple(out))


def expr_desc(n, src):
    """An expression -> a small tuple link() can type: ('id', x) | ('sel', base, name) | ('call', callee, i) |
    ('lit', type) | ('elem', base) | ('func',) | None."""
    if n is None:
        return None
    t = n.type
    if t == 'identifier':
        return ('id', _text(n, src))
    if t == 'selector_expression':
        return ('sel', expr_desc(n.child_by_field_name('operand'), src), _text(n.child_by_field_name('field'), src))
    if t == 'call_expression':
        fn = n.child_by_field_name('function')
        a = _args(n)
        if fn.type == 'identifier' and _text(fn, src) in ('new', 'make') and a:
            return ('lit', type_expr(a[0], src))
        if fn.type == 'parenthesized_expression':     # (*T)(x)
            inner = fn.named_children[0] if fn.named_children else None
            if inner is not None and inner.type == 'unary_expression':
                return ('lit', type_expr(inner.child_by_field_name('operand'), src))
            return ('call', expr_desc(inner, src), 0)
        if fn.type in TYPE_NODES:
            return ('lit', type_expr(fn, src))
        return ('call', expr_desc(fn, src), 0)
    if t == 'type_conversion_expression':
        ty = n.child_by_field_name('type')
        if ty is not None and ty.type == 'generic_type':     # F[int](x): a generic call or a conversion
            base = ty.child_by_field_name('type')
            if base is not None and base.type == 'type_identifier':
                return ('call', ('id', _text(base, src)), 0)
            if base is not None and base.type == 'qualified_type':
                return ('call', ('sel', ('id', _text(base.child_by_field_name('package'), src)),
                                 _text(base.child_by_field_name('name'), src)), 0)
        return ('lit', type_expr(ty, src))
    if t == 'composite_literal':
        return ('lit', type_expr(n.child_by_field_name('type'), src))
    if t == 'unary_expression':
        op = n.child_by_field_name('operator')
        o = _text(op, src) if op is not None else ''
        if o in ('&', '*'):
            return expr_desc(n.child_by_field_name('operand'), src)
        if o == '<-':
            return ('elem', expr_desc(n.child_by_field_name('operand'), src))
        return None
    if t == 'parenthesized_expression':
        return expr_desc(n.named_children[0], src) if n.named_children else None
    if t == 'index_expression':
        return ('elem', expr_desc(n.child_by_field_name('operand'), src))
    if t == 'slice_expression':
        return expr_desc(n.child_by_field_name('operand'), src)
    if t == 'type_assertion_expression':
        return ('lit', type_expr(n.child_by_field_name('type'), src))
    if t == 'func_literal':                        # its declared results type what calling it returns
        return ('lit', func_type(n, src))
    return None


def _status_err(n, src):
    """A 4xx / 5xx literal or an http.StatusXxx constant that is not below 400."""
    if n is None:
        return False
    if n.type == 'int_literal':
        return bool(re.fullmatch(r'[45]\d\d', _text(n, src)))
    if n.type == 'selector_expression':
        f = _text(n.child_by_field_name('field'), src)
        return f.startswith('Status') and f not in OK_STATUS
    return False


def _is_error_call(n, src, http_alias):
    """c.JSON(4xx, …) / c.AbortWithStatus(…) / w.WriteHeader(5xx) / http.Error(w, …) / c.Status(400).JSON(…)."""
    if n is None or n.type != 'call_expression':
        return False
    fn = n.child_by_field_name('function')
    if fn.type != 'selector_expression':
        return False
    last = _text(fn.child_by_field_name('field'), src)
    op = fn.child_by_field_name('operand')
    if op.type == 'identifier' and _text(op, src) == http_alias and last in ('Error', 'NotFound'):
        return True
    if last in ('AbortWithError', 'AbortWithStatus', 'AbortWithStatusJSON') or \
            (last in STATUS_CALLS and _args(n) and _status_err(_args(n)[0], src)):
        return True
    return op.type == 'call_expression' and _is_error_call(op, src, http_alias)


class GoLang:
    name, ctor, module_types = 'go', '__init__', False
    builtins = stdlib = BUILTIN_FUNCS | BUILTIN_TYPES

    def __init__(self, Func):
        self.Func = Func

    def handles(self, rel):
        if 'testdata' in rel.split('/')[:-1]:
            return False
        return rel.endswith('.go') or posixpath.basename(rel) == 'go.mod'

    def modname(self, rel):
        return modname(rel)

    def is_test(self, mod, rel):
        return is_test_file(rel)

    def parse_module(self, rel, text):
        if posixpath.basename(rel) == 'go.mod':
            m = re.search(r'^\s*module\s+"?([^\s"]+)"?', text, re.M)
            return {'name': modname(rel), 'file': rel, 'imports': {}, 'funcs': [], 'globals': set(), 'classes': set(),
                    'blueprints': set(), 'framework': set(), 'lines': text.count('\n') + 1, 'entry_like': False,
                    'deps': [], 'global_tables': {}, 'class_attrs': {}, 'top': None, 'has_main': False,
                    'gomod': m.group(1) if m else '', 'go': {'dir': posixpath.dirname(rel)}}
        return _Module(self, rel, text).info

    # ------------------------------------------------------------ analysis hooks

    def link(self, mods, funcs, class_keys):
        """alias: each site key -> the qual it names, `ext:go` (not the program's), or nothing (unresolved)."""
        return {'alias': _Linker(mods).alias}

    def entry(self, ms, mods, imported, closure, is_test, files):
        """The non-test package main file that defines main(); several: the shallowest, then main.go."""
        cands = [m for m in ms if not is_test(m) and mods[m].get('has_main')]
        return min(cands, key=lambda m: (mods[m]['file'].count('/'), posixpath.basename(mods[m]['file']) != 'main.go',
                                         mods[m]['file']), default=None)

    def route_of(self, f, extra, ctx):
        spec = getattr(f, 'route', None)
        if spec is None and extra:
            spec = extra[0]
        if spec is not None:
            kind, arg = spec
            if kind == 'route':
                return (arg, 'route')
            if kind == 'use':
                return ('middleware' + (' ' + arg if arg else ''), 'hook')
            if kind == 'error':
                return (arg, 'error-handler')
        if getattr(f, 'gomain', False):
            return (f.module, 'entry')
        if getattr(f, 'goinit', False):
            return ('init', 'hook')
        return None

    def snippet(self, f):
        """Up to 3 lines: error exits and effects first, then returns, then the rest minus `if err != nil {`."""
        lines, body = [], f.body.splitlines()
        if f.name != '<module>':
            for i, raw in enumerate(body):
                if '{' in raw:
                    body = [raw.split('{', 1)[1]] + body[i + 1:]
                    break
        for raw in body:
            l = raw.strip()
            if not l or re.fullmatch(r'[{}()\[\];,]+', l) or l.startswith(('//', '/*', '*')):
                continue
            lines.append(l)
        def tier(l):
            if re.search(r'\breturn\b.*\berr\w*$|\bhttp\.Error\(|\bpanic\(|\blog\.Fatal|\bos\.Exit\(|\.JSON\(|'
                         r'\.AbortWith|\.WriteHeader\(|\.(Exec|Query|QueryRow|Execute|ExecuteTemplate)\w*\(|'
                         r'\.Encode\(|\bw\.Write\(', l):
                return 0
            if re.match(r'return\b', l):
                return 1
            return 3 if re.fullmatch(r'if err != nil \{|\} else \{|var \w+ [\w.*\[\]]+|defer \w+\.Close\(\)', l) else 2
        best = sorted(range(len(lines)), key=lambda i: (tier(lines[i]), i))
        return [lines[i] for i in sorted(i for i in best[:3] if tier(lines[i]) < 3)]

    def extra_refs(self, f, others, ctx):
        """Glob literals (ParseGlob, LoadHTMLGlob, //go:embed) -> every file they match; http.Dir / Static
        directories -> every file below them."""
        out = []
        for g in getattr(f, 'goglobs', ()):
            pat = g.removeprefix('./').rstrip('/')
            for p in others:
                if fnmatch.fnmatchcase(p, pat) or p.startswith(pat + '/'):
                    out.append((g, p))
        for d in getattr(f, 'godirs', ()):
            d = d.removeprefix('./').strip('/')
            out += [(d, p) for p in others if d and p.startswith(d + '/')]
        return out


class _Linker:
    """Go's scoping over the whole snapshot: packages, their functions / methods / types / vars, and each site's alias."""

    def __init__(self, mods):
        self.mods = mods
        self.gomods = sorted(((info['go']['dir'], info['gomod']) for info in mods.values() if info.get('gomod')),
                             key=lambda x: -len(x[1]))
        self.pkgs, self.pkg_of, self.test = {}, {}, {}
        self.by_name = collections.defaultdict(list)      # method name -> [(qual, results, module, pkg)] (non-test)
        for m, info in mods.items():
            g = info.get('go')
            if g is None or 'gomod' in info:
                continue
            pid = (g['dir'], g['pkg'])
            self.pkg_of[m] = pid
            test = self.test[m] = is_test_file(info['file'])
            P = self.pkgs.get(pid)
            if P is None:
                P = self.pkgs[pid] = {'funcs': {}, 'tfuncs': {}, 'methods': {}, 'types': {}, 'vars': {}, 'consts': set()}
            for name, (q, res) in g['funcs'].items():
                (P['tfuncs'] if test else P['funcs']).setdefault(name, (q, res, m))
            for key, (q, res) in g['methods'].items():
                P['methods'].setdefault(key, (q, res, m))
                if not test:
                    self.by_name[key[1]].append((q, res, m, pid))
            for name, t in g['types'].items():
                P['types'].setdefault(name, (t, m))
            for name, d in g['vars'].items():
                P['vars'].setdefault(name, (d, m))
            P['consts'].update(g['consts'])
        self.dir_pkg = {}
        for d, name in sorted(self.pkgs, key=lambda p: (p[1].endswith('_test'), p[1] == 'main')):
            if not name.endswith('_test'):
                self.dir_pkg.setdefault(d, (d, name))
        self.path_cache, self.vis_cache = {}, {}
        self.alias = {}
        for m in self.pkg_of:
            for target, kind, ed, loc in mods[m]['go']['sites']:
                if kind == 'pick':                      # (router, echo's choice, the others' choice)
                    hit = self.value(ed[1] if self.is_echo(ed[0], m, loc) else ed[2], m, loc)
                    if hit:
                        self.alias[target] = hit[0]
                    continue
                if kind == 'value':
                    hit = self.value(ed, m, loc)
                    if hit:
                        self.alias[target] = hit[0]
                    continue
                r = self.callee(ed, m, loc, 0)
                if r[0] == 'func':
                    self.alias[target] = r[1]
                elif r[0] != 'miss':
                    self.alias[target] = 'ext:go'

    # ------------------------------------------------------------ packages

    def is_echo(self, ed, m, loc):
        """Is the router expression an echo value: by its type's package, else by the imports of m's package."""
        p = self.type_path(ed, m, loc, 0)
        if p is not None:
            return p.startswith(ECHO)
        pid = self.pkg_of[m]
        return any(path.startswith(ECHO) for mm, q in self.pkg_of.items() if q == pid for path in self.imps(mm).values())

    def te_path(self, te, m):
        return self.imps(m).get(te[1]) if te is not None and te[0] == 'n' and te[1] is not None else None

    def type_path(self, ed, m, loc, d):
        """The import path of the external package whose type an expression has (echo.New(), e.Group(…),
        a program function returning *echo.Echo, a field of that type), or None."""
        if ed is None or d > 6:
            return None
        k = ed[0]
        if k == 'lit':
            return self.te_path(ed[1], m)
        if k == 'id':
            if ed[1] in loc:
                return self.type_path(loc[ed[1]][0], m, loc[ed[1]][1], d + 1)
            hit = self.pkgs[self.pkg_of[m]]['vars'].get(ed[1])
            return self.type_path(hit[0], hit[1], {}, d + 1) if hit else None
        if k == 'call' and ed[1] is not None:
            c = ed[1]
            if c[0] == 'sel' and c[1] is not None and c[1][0] == 'id' and c[1][1] not in loc and c[1][1] in self.imps(m):
                path = self.imps(m)[c[1][1]]
                if self.pkg_for(path)[0] is None:
                    return path                         # echo.New()
            r = self.callee(c, m, loc, d + 1)
            if r[0] == 'func':
                return self.te_path(r[2][ed[2]], r[3]) if ed[2] < len(r[2]) else None
            return self.type_path(c[1], m, loc, d + 1) if c[0] == 'sel' else None   # e.Group(…): e's framework
        if k == 'sel' and ed[1] is not None:
            R = self.typeof(ed[1], m, loc, d + 1)
            if R is not None and R[0] == 'T':
                t, tm = self.typedef(R)
                return self.te_path(t['fields'].get(ed[2]), tm)
        return None

    def pkg_for(self, path):
        """An import path -> (package id or None, whether it is the program's own module)."""
        hit = self.path_cache.get(path)
        if hit is None:
            hit = (None, False)
            for G, MP in self.gomods:
                if MP and (path == MP or path.startswith(MP + '/')):
                    d = posixpath.normpath(posixpath.join(G, path[len(MP) + 1:])) if path != MP else G
                    d = '' if d == '.' else d
                    hit = (self.dir_pkg.get(d), True)
                    break
            else:
                if not self.gomods and not stdlib(path):      # no go.mod yet: a snapshot directory the path ends with
                    best = max((d for d in self.dir_pkg if d and (path == d or path.endswith('/' + d))), key=len,
                               default=None)
                    if best is not None:
                        hit = (self.dir_pkg[best], True)
            self.path_cache[path] = hit
        return hit

    def imps(self, m):
        return self.mods[m]['go']['imports']

    def visible(self, m):
        """Packages the file m can see: its own and the snapshot packages it imports."""
        v = self.vis_cache.get(m)
        if v is None:
            v = {self.pkg_of[m]} | {self.pkg_for(p)[0] for p in self.imps(m).values()} | \
                {self.pkg_for(p)[0] for p in self.mods[m]['go']['dots']}
            self.vis_cache[m] = v
        return v

    def lookup_func(self, P, name, test):
        return P['funcs'].get(name) or (P['tfuncs'].get(name) if test else None)

    # ------------------------------------------------------------ types

    def resolve_te(self, te, m):
        """A type expression as written in file m -> ('T', pkg, name) | ('E', element te, m) | FUNCT | EXT | None."""
        if te is None:
            return None
        k = te[0]
        if k == 'n':
            alias, name = te[1], te[2]
            if alias is None:
                pid = self.pkg_of[m]
                if name in self.pkgs[pid]['types']:
                    return ('T', pid, name)
                for path in self.mods[m]['go']['dots']:
                    q, _ = self.pkg_for(path)
                    if q is not None and name in self.pkgs[q]['types']:
                        return ('T', q, name)
                return EXT if name in BUILTIN_TYPES else None
            path = self.imps(m).get(alias)
            if path is None:
                return None
            q, _ = self.pkg_for(path)
            return ('T', q, name) if q is not None and name in self.pkgs[q]['types'] else EXT
        if k == 'e':
            return ('E', te[1], m)
        if k == 'f':
            return ('F', te[1] if len(te) > 1 else (), m)
        return EXT

    def typedef(self, R):
        return self.pkgs[R[1]]['types'][R[2]]

    def typeof(self, ed, m, loc, d):
        if ed is None or d > 8:
            return None
        k = ed[0]
        if k == 'lit':
            return self.resolve_te(ed[1], m)
        if k == 'id':
            x = ed[1]
            if x in loc:                                # (descriptor, the scope it was bound in)
                return self.typeof(loc[x][0], m, loc[x][1], d + 1)
            hit = self.pkgs[self.pkg_of[m]]['vars'].get(x)
            return self.typeof(hit[0], hit[1], {}, d + 1) if hit else None
        if k == 'sel':
            base, name = ed[1], ed[2]
            if base is None:
                return None
            if base[0] == 'id' and base[1] not in loc and base[1] in self.imps(m):
                q, ours = self.pkg_for(self.imps(m)[base[1]])
                if q is None:
                    return None if ours else EXT
                hit = self.pkgs[q]['vars'].get(name)
                return self.typeof(hit[0], hit[1], {}, d + 1) if hit else None
            R = self.typeof(base, m, loc, d + 1)
            if R is None or R[0] != 'T':
                return EXT if R == EXT else None
            return self.field(R, name, 0)
        if k == 'call':
            if ed[1] is None:
                return None
            r = self.callee(ed[1], m, loc, d + 1)
            if r == DYN:                                # a func value: its declared results, else not the program's
                R = self.typeof(ed[1], m, loc, d + 1)
                if R is not None and R[0] == 'F' and ed[2] < len(R[1]):
                    return self.resolve_te(R[1][ed[2]], R[2]) or EXT
                return EXT
            if r[0] == 'func':
                res = r[2]
                return self.resolve_te(res[ed[2]], r[3]) if ed[2] < len(res) else None
            if r[0] == 'conv':
                return r[1]
            return EXT if r == EXT else None
        if k == 'elem':
            R = self.typeof(ed[1], m, loc, d + 1)
            if R is not None and R[0] == 'T':           # a named slice / map type: its underlying element
                t, tm = self.typedef(R)
                R = self.resolve_te(t['under'], tm) if t['under'] is not None else None
            if R is not None and R[0] == 'E':
                return self.resolve_te(R[1], R[2])
            return EXT if R == EXT else None
        if k == 'func':
            return FUNCT
        return None

    def field(self, R, name, depth):
        """The type of field `name` of the program type R (through embedded structs), or None."""
        if R is None or R[0] != 'T' or depth > 3:
            return None
        t, tm = self.typedef(R)
        if t['kind'] == 'alias':
            return self.field(self.resolve_te(t['under'], tm), name, depth + 1)
        if name in t['fields']:
            return self.resolve_te(t['fields'][name], tm) or EXT
        for e in t['embeds']:
            if e[0] == 'n' and e[2] == name:
                return self.resolve_te(e, tm) or EXT
        for e in t['embeds']:
            r = self.field(self.resolve_te(e, tm), name, depth + 1)
            if r is not None:
                return r
        return None

    def method(self, R, name, depth):
        """Method `name` of the program type R, its own or promoted from an embedded program type."""
        if R is None or R[0] != 'T' or depth > 3:
            return None
        P = self.pkgs[R[1]]
        hit = P['methods'].get((R[2], name))
        if hit:
            return hit
        t, tm = self.typedef(R)
        if t['kind'] == 'alias':
            return self.method(self.resolve_te(t['under'], tm), name, depth + 1)
        for e in t['embeds']:
            hit = self.method(self.resolve_te(e, tm), name, depth + 1)
            if hit:
                return hit
        return None

    def unique(self, name, m, visible):
        """The one non-test method called `name` in the program (in a package m sees, when `visible`)."""
        cands = self.by_name.get(name, ())
        if len(cands) == 1 and (not visible or cands[0][3] in self.visible(m)):
            return cands[0][:3]
        return None

    # ------------------------------------------------------------ sites

    def callee(self, ed, m, loc, d):
        """What a called expression is: ('func', qual, results, module) | ('conv', type) | EXT | DYN | MISS."""
        if ed is None:
            return DYN
        k = ed[0]
        pid = self.pkg_of[m]
        P = self.pkgs[pid]
        if k == 'id':
            x = ed[1]
            if x in loc:
                return DYN
            hit = self.lookup_func(P, x, self.test[m])
            if hit:
                return ('func',) + hit
            if x in P['types']:
                return ('conv', ('T', pid, x))
            if x in P['vars'] or x in P['consts']:
                return DYN
            dots = self.mods[m]['go']['dots']
            for path in dots:
                q, _ = self.pkg_for(path)
                if q is not None:
                    hit = self.lookup_func(self.pkgs[q], x, False)
                    if hit:
                        return ('func',) + hit
                    if x in self.pkgs[q]['types']:
                        return ('conv', ('T', q, x))
            if x in BUILTIN_FUNCS or x in BUILTIN_TYPES or any(self.pkg_for(p)[0] is None for p in dots):
                return EXT
            return MISS
        if k == 'sel':
            base, name = ed[1], ed[2]
            if base is None:
                return DYN
            if base[0] == 'id' and base[1] not in loc:
                x = base[1]
                imp = self.imps(m)
                if x in imp and x not in P['vars']:
                    q, ours = self.pkg_for(imp[x])
                    if q is None:
                        return MISS if ours else EXT
                    Q = self.pkgs[q]
                    hit = Q['funcs'].get(name)
                    if hit:
                        return ('func',) + hit
                    if name in Q['types']:
                        return ('conv', ('T', q, name))
                    return DYN if name in Q['vars'] or name in Q['consts'] else MISS
                if x in P['types'] and x not in P['vars']:            # T.Method: a method expression
                    hit = self.method(('T', pid, x), name, 0)
                    return ('func',) + hit if hit else EXT
            return self.on_type(self.typeof(base, m, loc, d + 1), name, m)
        return DYN

    def on_type(self, R, name, m):
        if R is None:                                   # type unknown: the one method of that name m can see
            hit = self.unique(name, m, True)
            return ('func',) + hit if hit else DYN
        if R[0] != 'T':
            return EXT
        hit = self.method(R, name, 0)
        if hit:
            return ('func',) + hit
        t, tm = self.typedef(R)
        if t['kind'] == 'interface':                    # dynamic dispatch: the one implementation, if there is one
            hit = self.unique(name, m, False)
            return ('func',) + hit if hit else EXT
        if self.field(R, name, 0) is not None:          # a func-typed field
            return DYN
        if t['embeds'] or t['kind'] == 'alias':
            return EXT                                  # promoted from an embedded (maybe external) type
        return MISS

    def value(self, ed, m, loc):
        """A function / method named as a value -> (qual, results, module) or None."""
        if ed is None:
            return None
        pid = self.pkg_of[m]
        P = self.pkgs[pid]
        if ed[0] == 'id':
            return None if ed[1] in loc else self.lookup_func(P, ed[1], self.test[m])
        if ed[0] == 'sel' and ed[1] is not None:
            base, name = ed[1], ed[2]
            if base[0] == 'id' and base[1] not in loc:
                imp = self.imps(m)
                if base[1] in imp:
                    q, _ = self.pkg_for(imp[base[1]])
                    return self.pkgs[q]['funcs'].get(name) if q is not None else None
                if base[1] in P['types'] and base[1] not in P['vars']:
                    return self.method(('T', pid, base[1]), name, 0)
            return self.method(self.typeof(base, m, loc, 1), name, 0)
        return None


class _Module:
    """One parse: the module info dict analyze() expects, plus info['go'] for link()."""

    def __init__(self, lang, rel, text):
        self.Func, self.rel = lang.Func, rel
        self.src = src = text.encode('utf-8')
        root = _GO.parse(src).root_node
        self.mod = mod = modname(rel)
        self.info = info = {'name': mod, 'file': rel, 'imports': {}, 'funcs': [], 'globals': set(), 'classes': set(),
                            'blueprints': set(), 'framework': set(), 'lines': text.count('\n') + 1,
                            'entry_like': False, 'deps': [], 'global_tables': {}, 'class_attrs': {}, 'has_main': False}
        self.pkg = ''
        self.imports, self.dots = {}, []      # alias -> import path; dot-imported paths
        self.ffuncs, self.ftypes, self.fvars, self.fconsts = set(), {}, {}, set()
        self.embeds = {}                      # var name -> //go:embed patterns (relative to the root)
        self.var_refs = {}                    # package var -> (file literals, globs) of its initializer
        self.n = 0
        self.names = {}                       # synthetic labels -> count
        self.sites = []
        self.skip = set()                     # node ids already handled (handlers inside registration wrappers)
        self.ninit = 0
        self.prepass(root)
        self.http = next((a for a, p in self.imports.items() if p == 'net/http'), None)
        paths = self.imports.values()
        # which framework the file's routers are: echo puts the handler first, the others last; None: link() decides
        self.echo = True if any(p.startswith(ECHO) for p in paths) else \
            False if any(p.startswith(ROUTERS) for p in paths) else None
        base = self.pkg[:-5] if self.pkg.endswith('_test') else self.pkg
        self.label = (posixpath.dirname(rel).replace('/', '.') or 'main') if base == 'main' else base
        info['go'] = {'pkg': self.pkg, 'dir': posixpath.dirname(rel), 'imports': self.imports, 'dots': self.dots,
                           'funcs': {}, 'methods': {}, 'types': self.ftypes, 'vars': self.fvars,
                           'consts': self.fconsts, 'sites': self.sites}
        top = self.Func(mod, '<module>', root, src)
        top.golocals, top.goprefix = {}, {}
        self.top = top
        decls = []
        seen, inits, main = set(), [], None
        for n in root.named_children:
            if n.type in ('function_declaration', 'method_declaration'):
                f = self.function(n, seen)
                if f is None:
                    continue
                if not f.cls and f.name.startswith('init') and f.name.split(' ')[0] == 'init':
                    inits.append(f)
                if not f.cls and f.name == 'main' and self.pkg == 'main':
                    main = f
            elif n.type in ('var_declaration', 'const_declaration'):
                decls.append(n)
                for spec in self.descendants(n, 'var_spec') or [n]:
                    ns, ng = len(top.strings), len(getattr(top, 'goglobs', ()))
                    self.walk(spec, top, False)
                    lits, globs = top.strings[ns:], getattr(top, 'goglobs', [])[ng:]
                    for nm in spec.children_by_field_name('name') if lits or globs else ():
                        self.var_refs[_text(nm, src)] = (lits, globs)
        if main is not None:
            info['has_main'] = info['entry_like'] = True
            main.gomain = True
            main.direct.extend((g.qual, False) for g in inits)      # the file's init()s run with it
            top.direct.append((main.qual, False))
        else:
            for g in inits:
                g.goinit = True
        for name, pats in self.embeds.items():
            top.goglobs = getattr(top, 'goglobs', []) + pats
        top.lines = sum(d.end_point[0] - d.start_point[0] + 1 for d in decls)
        top.body = '\n'.join(_cut(src, d.start_byte, d.end_byte, top.carved) for d in decls)
        top.hash = _hash(top.body)
        top.file = rel
        top.locals = set(top.golocals)
        info['top'] = top

    # ------------------------------------------------------------ file scope

    def prepass(self, root):
        src = self.src
        pending = []
        for n in root.named_children:
            t = n.type
            if t == 'comment':
                c = _text(n, src)
                if c.startswith('//go:embed'):
                    pending += [self.embed_path(p) for p in re.findall(r'"[^"]*"|`[^`]*`|\S+', c[len('//go:embed'):])]
                continue
            if t == 'package_clause':
                self.pkg = _text(n.named_children[0], src) if n.named_children else ''
            elif t == 'import_declaration':
                for spec in self.descendants(n, 'import_spec'):
                    path = _strval(spec.child_by_field_name('path'), src) or ''
                    nm = spec.child_by_field_name('name')
                    if nm is None:
                        self.imports.setdefault(import_name(path), path)
                    elif nm.type == 'dot':
                        self.dots.append(path)
                    elif nm.type != 'blank_identifier':
                        self.imports[_text(nm, src)] = path
            elif t == 'function_declaration':
                self.ffuncs.add(_text(n.child_by_field_name('name'), src))
            elif t == 'type_declaration':
                for spec in n.named_children:
                    if spec.type in ('type_spec', 'type_alias'):
                        self.typedecl(spec, spec.type == 'type_alias')
            elif t == 'var_declaration':
                for spec in self.descendants(n, 'var_spec'):
                    names = [c for c in spec.children_by_field_name('name')]
                    for i, (nm, d) in enumerate(zip(names, self.bind_descs(spec, len(names)))):
                        self.fvars.setdefault(_text(nm, src), d)
                        if pending:
                            self.embeds[_text(nm, src)] = pending
                pending = []
                continue
            elif t == 'const_declaration':
                for spec in self.descendants(n, 'const_spec'):
                    self.fconsts.update(_text(c, src) for c in spec.children_by_field_name('name'))
            pending = []

    def embed_path(self, p):
        p = p.strip('"`')
        p = p[4:] if p.startswith('all:') else p
        return posixpath.normpath(posixpath.join(posixpath.dirname(self.rel), p))

    @staticmethod
    def descendants(n, typ):
        out, stack = [], [n]
        while stack:
            c = stack.pop()
            if c.type == typ:
                out.append(c)
            else:
                stack.extend(reversed(c.named_children))
        return out

    def typedecl(self, spec, alias):
        src = self.src
        name = _text(spec.child_by_field_name('name'), src)
        ty = spec.child_by_field_name('type')
        rec = {'kind': 'other', 'fields': {}, 'embeds': [], 'under': None}
        if alias:
            rec['kind'], rec['under'] = 'alias', type_expr(ty, src)
        elif ty is not None and ty.type == 'struct_type':
            rec['kind'] = 'struct'
            for fd in self.descendants(ty, 'field_declaration'):
                te = type_expr(fd.child_by_field_name('type'), src)
                names = fd.children_by_field_name('name')
                if names:
                    for nm in names:
                        rec['fields'][_text(nm, src)] = te
                else:
                    rec['embeds'].append(te)
        elif ty is not None and ty.type == 'interface_type':
            rec['kind'] = 'interface'
            for c in ty.named_children:
                if c.type in ('type_elem', 'constraint_elem', 'type_identifier', 'qualified_type'):
                    rec['embeds'].append(type_expr(c, src))
        else:
            rec['under'] = type_expr(ty, src)
        self.ftypes.setdefault(name, rec)

    def bind_descs(self, spec, count):
        """Descriptors for the names of a var_spec / short var declaration: its type, or its values."""
        src = self.src
        ty = spec.child_by_field_name('type')
        if ty is not None:
            return [('lit', type_expr(ty, src))] * count
        vals = spec.child_by_field_name('value') or spec.child_by_field_name('right')
        vals = [c for c in vals.named_children if c.type != 'comment'] if vals is not None and vals.type == 'expression_list' \
            else ([vals] if vals is not None else [])
        return self.multi(vals, count)

    def multi(self, vals, count):
        if len(vals) == count:
            return [expr_desc(v, self.src) for v in vals]
        if len(vals) == 1:
            d = expr_desc(vals[0], self.src)
            if d is not None and d[0] == 'call':
                return [('call', d[1], i) for i in range(count)]
            return [d] + [None] * (count - 1)
        return [None] * count

    # ------------------------------------------------------------ functions

    def function(self, n, seen):
        src = self.src
        name = _text(n.child_by_field_name('name'), src)
        cls = None
        recv = n.child_by_field_name('receiver')
        if recv is not None:
            p = next((c for c in recv.named_children if c.type == 'parameter_declaration'), None)
            te = type_expr(p.child_by_field_name('type'), src) if p is not None else None
            cls = te[2] if te is not None and te[0] == 'n' else None
            if cls is None:
                return None
        if not cls and name == 'init':                # several init()s in one file: init, init #2, …
            self.ninit += 1
            name = 'init' if self.ninit == 1 else f'init #{self.ninit}'
        elif (cls, name) in seen:
            return None                               # duplicate definition: the first one
        seen.add((cls, name))
        f = self.Func(self.mod, name, n, src, cls)
        f.golocals, f.goprefix = {}, {}
        f._node = (n.start_byte, n.end_byte)
        for tp in self.descendants(n.child_by_field_name('type_parameters'), 'type_parameter_declaration') \
                if n.child_by_field_name('type_parameters') is not None else ():
            for nm in tp.children_by_field_name('name'):
                self.local(f, _text(nm, src), None)
        if recv is not None:
            self.params(recv, f)
        self.params(n.child_by_field_name('parameters'), f)
        res = n.child_by_field_name('result')
        results = []
        if res is not None and res.type == 'parameter_list':
            for p in res.named_children:
                if p.type in ('parameter_declaration', 'variadic_parameter_declaration'):
                    te = type_expr(p.child_by_field_name('type'), src)
                    names = p.children_by_field_name('name')
                    results += [te] * max(1, len(names))
                    for nm in names:
                        f.golocals.setdefault(_text(nm, src), (('lit', te), {}))
        elif res is not None:
            results = [type_expr(res, src)]
        if cls:
            self.info['go']['methods'][(cls, name)] = (f.qual, results)
        else:
            self.info['go']['funcs'].setdefault(name, (f.qual, results))
        body = n.child_by_field_name('body')
        if body is not None:
            self.walk(body, f, False)
        self.add(f)
        return f

    def params(self, plist, f, force=False):
        if plist is None:
            return
        for p in plist.named_children:
            if p.type in ('parameter_declaration', 'variadic_parameter_declaration'):
                te = type_expr(p.child_by_field_name('type'), self.src)
                if p.type == 'variadic_parameter_declaration':
                    te = ('e', te)
                for nm in p.children_by_field_name('name'):
                    self.local(f, _text(nm, self.src), ('lit', te), force)

    def local(self, f, name, desc, force=False, scope=None):
        """Bind a local to (descriptor, the scope its value is read in: the names visible before the statement).
        Copy-on-write, so the sites made before keep the names that were visible to them."""
        if name != '_' and (force or name not in f.golocals):
            f.golocals = {**f.golocals, name: (desc, f.golocals if scope is None else scope)}
            f.locals.add(name)

    def add(self, f):
        start, end = f.__dict__.pop('_node')
        if f.carved:
            f.body = _cut(self.src, start, end, f.carved)
            f.lines -= sum(n for _, _, n in f.carved)
            f.hash = _hash(f.body)
        f.file = self.rel
        self.info['funcs'].append(f)

    def synth(self, parent, label, lit, err, route=None, cmd=None):
        """An inline func literal carved out of parent into a Func `<label>` (a route / hook / command)."""
        k = self.names.get(label, 0) + 1
        self.names[label] = k
        g = self.Func(self.mod, f'<{label}>' if k == 1 else f'<{label} #{k}>', lit, self.src)
        # Go closures see the names declared before them; their own parameters shadow those
        g.golocals, g.goprefix, g.locals = dict(parent.golocals), parent.goprefix, set(parent.locals)
        g.route, g.cmd = route, cmd
        g._node = (lit.start_byte, lit.end_byte)
        parent.carved.append((lit.start_byte, lit.end_byte, lit.end_point[0] - lit.start_point[0] + 1))
        self.params(lit.child_by_field_name('parameters'), g, True)
        body = lit.child_by_field_name('body')
        if body is not None:
            self.walk(body, g, err)
        self.add(g)
        return g

    def closure(self, lit, f, err):
        """A func literal that stays part of f: its calls are f's calls; its parameters and locals are its own
        scope (sites keep the dict they were made in, so they see that scope)."""
        saved = f.golocals
        f.golocals = dict(saved)
        self.params(lit.child_by_field_name('parameters'), f, True)
        body = lit.child_by_field_name('body')
        if body is not None:
            self.walk(body, f, err)
        f.golocals = saved

    # ------------------------------------------------------------ sites

    def site(self, f, ed, how, err):
        """A call / function value -> a fresh import key whose alias link() decides."""
        self.n += 1
        key, target = f'__go{self.n}', f'{self.mod}.#{self.n}'
        self.info['imports'][key] = target
        self.sites.append((target, how if how in ('value', 'pick') else 'call', ed, f.golocals))
        f.calls.append((key, err, 'call' if how == 'call' else 'deferred'))
        return key

    def ext_alias(self, x, f):
        """x is a stdlib package alias (not shadowed by a local)."""
        return x not in f.golocals and x in self.imports and stdlib(self.imports[x])

    def value_site(self, a, f, err):
        """An identifier / selector used as a value: a deferred call when link() finds a function there."""
        src = self.src
        if a.type == 'identifier':
            x = _text(a, src)
            if x in f.golocals or x in BUILTIN_IDENTS or x in self.fconsts or x in self.fvars or x in self.ftypes \
                    or x in self.imports or x in BUILTIN_FUNCS:
                return None
            return self.site(f, ('id', x), 'value', err)
        if a.type == 'selector_expression':
            op = a.child_by_field_name('operand')
            if op.type == 'identifier' and self.ext_alias(_text(op, src), f):
                return None
            return self.site(f, expr_desc(a, src), 'value', err)
        return None

    # ------------------------------------------------------------ the walk

    def walk(self, n, f, err):
        """Blocks and if / for / switch / select statements and their cases are scopes: a `:=` inside one does
        not leak out (sites keep the locals dict they were made in)."""
        if n.type in SCOPES:
            saved = f.golocals
            f.golocals = dict(saved)
            self._walk(n, f, err)
            f.golocals = saved
        else:
            self._walk(n, f, err)

    def _walk(self, n, f, err):
        src, t = self.src, n.type
        if t in SKIP_TYPES or n.id in self.skip:
            return
        if t in BRANCH_TYPES:
            f.branches += 1
        if t == 'binary_expression':
            op = n.child_by_field_name('operator')
            if op is not None and op.type in ('&&', '||'):
                f.branches += 1
        elif t == 'if_statement':
            for i, c in enumerate(n.children):
                fld = n.field_name_for_child(i)
                if fld in ('consequence', 'alternative') and c.type == 'block':
                    self.walk_branch(c, f, err)
                elif c.is_named:
                    self.walk(c, f, err)
            return
        elif t == 'block':
            for s in _stmts(n):
                self.walk(s, f, err)
            return
        elif t in ('call_expression',):
            self.call(n, f, err, 'call')
            return
        elif t == 'go_statement':
            c = next((c for c in n.named_children if c.type == 'call_expression'), None)
            if c is not None:
                self.call(c, f, err, 'go')
                return
        elif t == 'func_literal':
            self.closure(n, f, err)
            return
        elif t == 'type_conversion_expression':
            ty = n.child_by_field_name('type')
            if ty is not None and ty.type == 'generic_type':        # F[int](x): maybe a generic call
                d = expr_desc(n, src)
                if d is not None and d[0] == 'call':
                    self.site(f, d[1], 'call', err)
            op = n.child_by_field_name('operand')
            if op is not None:
                self.walk(op, f, err)
            return
        elif t == 'identifier':
            self.ident(n, f)
            return
        elif t == 'selector_expression':
            self.selector(n, f)
            op = n.child_by_field_name('operand')
            if op is not None:
                self.walk(op, f, err)
            return
        elif t in ('short_var_declaration', 'var_spec'):
            # the right side is read before the left side binds: `url, err := url.Parse(s)`
            self.rhs_values(n.child_by_field_name('right') or n.child_by_field_name('value'), f, err)
            for c in (n.child_by_field_name('right'), n.child_by_field_name('value')):
                if c is not None:
                    self.walk(c, f, err)
            if f is not self.top or t == 'short_var_declaration':
                names = n.child_by_field_name('left') if t == 'short_var_declaration' else None
                ids = [c for c in names.named_children if c.type == 'identifier'] if names is not None \
                    else list(n.children_by_field_name('name'))
                scope = f.golocals
                for nm, d in zip(ids, self.bind_descs(n, len(ids))):
                    self.local(f, _text(nm, src), d, scope=scope)
                rhs = n.child_by_field_name('right') if t == 'short_var_declaration' else n.child_by_field_name('value')
                if len(ids) == 1 and rhs is not None and rhs.named_children:
                    p = self.route_prefix(rhs.named_children[0], f)
                    if p is not None:
                        f.goprefix[_text(ids[0], src)] = p
            return
        elif t == 'assignment_statement':
            left = n.child_by_field_name('left')
            right = n.child_by_field_name('right')
            lt = _text(left, src) if left is not None else ''
            if right is not None and right.named_children and re.search(r'\.HTTPErrorHandler$', lt):
                k = self.value_site(right.named_children[0], f, err)
                if k:
                    f.registers.append(('error', 'error handler', k))
            self.rhs_values(right, f, err)
            for c in (left, right):
                if c is not None:
                    self.walk(c, f, err)
            return
        elif t == 'range_clause':
            left, right = n.child_by_field_name('left'), n.child_by_field_name('right')
            ids = [c for c in left.named_children if c.type == 'identifier'] if left is not None else []
            if right is not None:
                self.walk(right, f, err)
            if any(c.type == ':=' for c in n.children):
                rd, scope = expr_desc(right, src), f.golocals
                for i, nm in enumerate(ids):
                    self.local(f, _text(nm, src), ('elem', rd) if i == 1 else None, scope=scope)
            return
        elif t == 'receive_statement':
            left, right = n.child_by_field_name('left'), n.child_by_field_name('right')
            if right is not None:
                self.walk(right, f, err)
            if left is not None and any(c.type == ':=' for c in n.children):
                scope = f.golocals
                for i, nm in enumerate(c for c in left.named_children if c.type == 'identifier'):
                    self.local(f, _text(nm, src), expr_desc(right, src) if i == 0 else None, scope=scope)
            return
        elif t == 'type_switch_statement':
            al = n.child_by_field_name('alias')
            for nm in al.named_children if al is not None else ():
                if nm.type == 'identifier':
                    self.local(f, _text(nm, src), None)
        elif t == 'composite_literal':
            self.composite(n, f, err)
            return
        elif t == 'keyed_element':
            k, v = n.child_by_field_name('key'), n.child_by_field_name('value')
            if k is not None and not (k.named_children and k.named_children[0].type == 'identifier'):
                self.walk(k, f, err)
            if v is not None:
                inner = v.named_children[0] if v.type == 'literal_element' and v.named_children else v
                self.value_site(inner, f, err)
                self.walk(v, f, err)
            return
        elif t in ('interpreted_string_literal', 'raw_string_literal'):
            self.string(_strval(n, src), f)
            return
        for c in n.named_children:
            self.walk(c, f, err)

    def rhs_values(self, rhs, f, err):
        for c in rhs.named_children if rhs is not None and rhs.type == 'expression_list' else ():
            if c.type in ('identifier', 'selector_expression'):
                self.value_site(c, f, err)

    def string(self, s, f):
        if s is None:
            return
        if SQL_STMT.match(s):
            for q in SQL.findall(s):
                f.data.add('table:' + q.lower())
        if PATH.fullmatch(s) and ('.' in s or '/' in s):
            f.strings.append(s)
        elif '*' in s and GLOB.fullmatch(s) and ('.' in s or '/' in s):
            f.goglobs = getattr(f, 'goglobs', []) + [s]

    def walk_branch(self, block, f, err):
        stmts = _stmts(block)
        errs = self.error_children(stmts, f)
        saved = f.golocals
        f.golocals = dict(saved)
        for i, s in enumerate(stmts):
            self.walk(s, f, err or i in errs)
        f.golocals = saved

    def error_children(self, stmts, f):
        """When a branch ends in an error exit, its statements after the last one that can still leave
        normally are on the error path (only the branch's last statement decides)."""
        if not stmts or not self.is_error_exit(stmts):
            return set()
        last = stmts[-1]
        if not self.fatal_stmt(last):                  # fatal calls count in call()
            f.errors += 1
        start = max((i + 1 for i in range(len(stmts) - 1) if self.has_normal_exit(stmts[i])), default=0)
        return set(range(start, len(stmts)))

    def stmt_call(self, s):
        if s.type == 'expression_statement' and s.named_children and s.named_children[0].type == 'call_expression':
            return s.named_children[0]
        return None

    def fatal_call(self, c):
        src = self.src
        fn = c.child_by_field_name('function')
        callee = re.sub(r'\s+', '', _text(fn, src))
        if callee == 'panic' or FATAL.fullmatch(callee):
            return True
        if fn.type == 'selector_expression' and _text(fn.child_by_field_name('field'), src) == 'Exit':
            op = fn.child_by_field_name('operand')
            a = _args(c)
            return op.type == 'identifier' and self.imports.get(_text(op, src)) == 'os' and \
                not (a and _text(a[0], src) == '0')
        return False

    def fatal_stmt(self, s):
        c = self.stmt_call(s)
        return c is not None and self.fatal_call(c)

    def is_error_exit(self, stmts):
        s = stmts[-1]
        if s.type == 'return_statement':
            if not s.named_children:                   # http.Error(w, …); return
                return len(stmts) > 1 and self.error_stmt(stmts[-2])
            return self.error_return(s)
        return self.error_stmt(s)

    def error_stmt(self, s):
        c = self.stmt_call(s)
        if c is None:
            return False
        last = re.sub(r'\s+', '', _text(c.child_by_field_name('function'), self.src)).rsplit('.', 1)[-1]
        return self.fatal_call(c) or _is_error_call(c, self.src, self.http) or bool(ERROR_NAME.fullmatch(last))

    def error_return(self, s):
        src = self.src
        vals = s.named_children[0] if s.named_children else None
        if vals is None:
            return False
        v = vals.named_children[-1] if vals.type == 'expression_list' and vals.named_children else vals
        if v.type == 'identifier':
            return bool(ERR_IDENT.fullmatch(_text(v, src)))
        if v.type == 'selector_expression':
            return bool(ERR_IDENT.fullmatch(_text(v.child_by_field_name('field'), src)))
        if v.type == 'call_expression':
            callee = re.sub(r'\s+', '', _text(v.child_by_field_name('function'), src))
            return bool(ERROR_CTORS.fullmatch(callee)) or _is_error_call(v, src, self.http)
        if v.type == 'unary_expression' and v.child_by_field_name('operand') is not None:
            lit = v.child_by_field_name('operand')
            ty = lit.child_by_field_name('type') if lit.type == 'composite_literal' else None
            return ty is not None and bool(re.search(r'(?:Err|Error)$', _text(ty, src)))
        return False

    def has_normal_exit(self, n):
        if n.type == 'return_statement':
            return not self.error_return(n)
        if n.type == 'func_literal':
            return False
        return any(self.has_normal_exit(c) for c in n.named_children)

    # ------------------------------------------------------------ data

    def ident(self, n, f):
        """A package-level variable read or written: global:<pkg>.<name>[.<field>]."""
        src = self.src
        x = _text(n, src)
        if x in f.golocals or x in BUILTIN_IDENTS or x in self.imports or x in self.ffuncs or x in self.ftypes or \
                x in self.fconsts:
            return
        if x in self.embeds:
            f.goglobs = getattr(f, 'goglobs', []) + self.embeds[x]
        if x in self.var_refs and f is not self.top:     # var tmpl = template.Must(template.ParseFiles(…))
            lits, globs = self.var_refs[x]
            f.strings += [l for l in lits if l not in f.strings]
            f.goglobs = getattr(f, 'goglobs', []) + [g for g in globs if g not in getattr(f, 'goglobs', ())]
        if x in self.fvars or self.varlike(n):
            f.data.add(self.sym(self.label, x, n))

    def varlike(self, n):
        """Used like a variable, not a constant / function: x.f, x.m(), x[k], x = …, x++, &x."""
        p = n.parent
        if p is None:
            return False
        t = p.type
        if t == 'selector_expression' and p.child_by_field_name('operand') == n:
            return True
        if t == 'index_expression' and p.child_by_field_name('operand') == n:
            return True
        if t in ('inc_statement', 'dec_statement'):
            return True
        if t == 'unary_expression' and _text(p.child_by_field_name('operator'), self.src) == '&':
            return True
        return t == 'expression_list' and p.parent is not None and p.parent.type == 'assignment_statement' and \
            p.parent.child_by_field_name('left') == p

    def sym(self, label, x, n):
        p = n.parent
        if p is not None and p.type == 'selector_expression' and p.child_by_field_name('operand') == n:
            gp = p.parent
            if not (gp is not None and gp.type == 'call_expression' and gp.child_by_field_name('function') == p):
                return f'global:{label}.{x}.{_text(p.child_by_field_name("field"), self.src)}'
        return f'global:{label}.{x}'

    def selector(self, n, f):
        """pkg.Var used like a variable (non-stdlib import): global:<pkg>.<Var>[.<field>]."""
        op = n.child_by_field_name('operand')
        if op is None or op.type != 'identifier':
            return
        x = _text(op, self.src)
        if x in f.golocals or x not in self.imports or stdlib(self.imports[x]) or not self.varlike(n):
            return
        f.data.add(self.sym(import_name(self.imports[x]), _text(n.child_by_field_name('field'), self.src), n))

    # ------------------------------------------------------------ calls and registrations

    def call(self, n, f, err, how):
        """A call: a site for the callee (its key is returned), effects, registrations, then the arguments."""
        src = self.src
        key = None
        fn = n.child_by_field_name('function')
        args = _args(n)
        if fn.type == 'func_literal':                  # func() { … }(): part of f
            self.closure(fn, f, err)
            for a in args:
                self.walk(a, f, err)
            return
        if fn.type in TYPE_NODES or fn.type == 'parenthesized_expression':    # conversions: []byte(x), (*T)(x)
            for a in args:
                self.walk(a, f, err)
            return
        callee = re.sub(r'\s+', '', _text(fn, src))
        last = callee.rsplit('.', 1)[-1]
        head = None
        if fn.type == 'selector_expression':
            op = fn.child_by_field_name('operand')
            if op.type == 'identifier' and _text(op, src) not in f.golocals:
                head = self.imports.get(_text(op, src))
        if self.fatal_call(n):
            f.errors += 1
        e = err or bool(ERROR_NAME.fullmatch(last)) or self.fatal_call(n)
        ed = expr_desc(fn, src)
        if fn.type == 'identifier':
            x = callee
            if x not in f.golocals and not (x in NO_SITE and x not in self.ffuncs and x not in self.ftypes):
                key = self.site(f, ed, how, e)
            self.ident(fn, f)
        else:
            if not (head is not None and stdlib(head)):
                key = self.site(f, ed, how, e)
            self.walk(fn, f, err)
        self.effects(n, f, head, last, args)
        done = self.registration(n, fn, f, args, head, last, err)
        for a in args:
            if a.id in done or a.id in self.skip:
                continue
            if a.type in ('identifier', 'selector_expression'):
                self.value_site(a, f, err)
            self.walk(a, f, err)
        return key

    def effects(self, n, f, head, last, args):
        src = self.src
        first = _strval(args[0], src) if args else None
        if head == 'os' and last in ENV_CALLS and first:
            f.data.add('env:' + first)
        elif head in ('os', 'io/ioutil') and last in FILE_CALLS and first:
            f.data.add('file:' + first)
        elif head == 'net/http' and last == 'Dir' and first:
            f.godirs = getattr(f, 'godirs', []) + [first]
        elif last in ('Static', 'StaticFS') and len(args) >= 2 and _strval(args[1], src):
            f.godirs = getattr(f, 'godirs', []) + [_strval(args[1], src)]

    def obj_prefix(self, obj, f):
        """The mount prefix of a router expression: a local bound from .Group("/p") / r.With(mw) chains."""
        if obj is None:
            return ''
        if obj.type == 'identifier':
            return f.goprefix.get(_text(obj, self.src), '')
        p = self.route_prefix(obj, f)
        return p or ''

    def route_prefix(self, n, f):
        """X.Group("/p") / X.PathPrefix("/p").Subrouter() / X.With(mw) -> the prefix it mounts at, else None."""
        if n is None or n.type != 'call_expression':
            return None
        fn = n.child_by_field_name('function')
        if fn.type != 'selector_expression':
            return None
        last = _text(fn.child_by_field_name('field'), self.src)
        obj = fn.child_by_field_name('operand')
        if last not in PREFIXING or last == 'Route':
            return None
        if obj.type == 'identifier' and obj.child_count == 0 and _text(obj, self.src) in self.imports:
            return None
        a = _args(n)
        if last in ('Subrouter', 'With'):
            return self.obj_prefix(obj, f)
        s = _strval(a[0], self.src) if a else None
        return None if s is None else self.obj_prefix(obj, f) + s.rstrip('/')

    def is_router(self, obj, f, head):
        if obj.type == 'identifier':
            x = _text(obj, self.src)
            return head is None and (x in f.goprefix or bool(ROUTER_OBJ.fullmatch(x)))
        return obj.type in ('call_expression', 'selector_expression')

    def registration(self, n, fn, f, args, head, last, err):
        """Route / hook / group registrations: returns the ids of the argument nodes it took care of."""
        src = self.src
        if fn.type != 'selector_expression':
            return set()
        obj = fn.child_by_field_name('operand')
        first = _strval(args[0], src) if args else None
        if last in ('HandleFunc', 'Handle') and len(args) >= 2 and first is not None and \
                (head == 'net/http' or (head is None and self.is_router(obj, f, head))):
            m = re.fullmatch(r'\s*(?:([A-Z]+)\s+)?(\S*)', first)
            meth, path = (m.group(1) or '', m.group(2)) if m else ('', first)
            if '/' not in path:
                return set()
            meth = meth or self.methods_of(n)
            prefix = self.obj_prefix(obj, f) if head is None else ''
            return self.route(f, meth, prefix, path, args[1], last == 'HandleFunc', err) | {args[0].id}
        if last in VERBS and len(args) >= 2 and first is not None and (first == '' or first.startswith('/')) and \
                self.is_router(obj, f, head):
            prefix = self.obj_prefix(obj, f)
            ref = ('identifier', 'selector_expression')
            if self.echo is None and len(args) > 2 and args[1].type in ref and args[-1].type in ref:
                return self.route_by_type(f, VERBS[last], prefix, first, obj, args, err)
            h = args[1] if self.echo else args[-1]
            done = self.route(f, VERBS[last], prefix, first, h, True, err)
            for a in args[1:]:
                if a.id not in done and a.type in ('identifier', 'selector_expression'):
                    self.value_site(a, f, err)            # route middleware: deferred calls
                    done.add(a.id)
                    self.walk(a, f, err)
            return done | {args[0].id}
        if last == 'Use' and args and self.is_router(obj, f, head):
            return self.hooks(f, args, self.obj_prefix(obj, f), err)
        if last == 'Group' and len(args) >= 2 and first is not None and first.startswith('/') and \
                self.is_router(obj, f, head):          # r.Group("/admin", mw…): mw guards the group
            return self.hooks(f, args[1:], self.obj_prefix(obj, f) + first.rstrip('/'), err) | {args[0].id}
        if last in NOT_FOUND and len(args) == 1 and self.is_router(obj, f, head):
            a = args[0]
            if a.type == 'func_literal':
                self.synth(f, NOT_FOUND[last], a, err, route=('error', NOT_FOUND[last]))
                return {a.id}
            k = self.value_site(a, f, err) if a.type in ('identifier', 'selector_expression') else None
            if k:
                f.registers.append(('error', NOT_FOUND[last], k))
                self.walk(a, f, err)
                return {a.id}
            return set()
        if last == 'Route' and len(args) >= 2 and first is not None and args[-1].type == 'func_literal' and \
                self.is_router(obj, f, head):                 # chi / fiber r.Route("/p", func(r chi.Router) { … })
            lit = args[-1]
            plist = lit.child_by_field_name('parameters')
            p0 = next((c for c in plist.named_children if c.type == 'parameter_declaration'), None) if plist else None
            nm = p0.child_by_field_name('name') if p0 is not None else None
            if nm is None:
                return set()
            name = _text(nm, src)
            old = f.goprefix.get(name)
            f.goprefix[name] = self.obj_prefix(obj, f) + first.rstrip('/')
            self.closure(lit, f, err)
            if old is None:
                f.goprefix.pop(name, None)
            else:
                f.goprefix[name] = old
            return {lit.id, args[0].id}
        return set()

    def hooks(self, f, args, prefix, err):
        """X.Use(mw…) / the middleware of X.Group("/p", mw…): hooks named `middleware [/prefix]`."""
        done = set()
        for a in args:
            if a.type == 'func_literal':
                self.synth(f, f'use {prefix}'.strip(), a, err, route=('use', prefix))
                done.add(a.id)
            elif a.type in ('identifier', 'selector_expression'):
                k = self.value_site(a, f, err)
                if k:
                    f.registers.append(('use', prefix, k))
                self.walk(a, f, err)
                done.add(a.id)
            elif a.type == 'call_expression':        # r.Use(Auth()): Auth builds the middleware
                k = self.call(a, f, err, 'call')
                done.add(a.id)
                if k:
                    f.registers.append(('use', prefix, k))
        return done

    def route_by_type(self, f, meth, prefix, path, obj, args, err):
        """r.GET("/p", a, …, b) in a file that does not say which framework r is: echo's handler is a, the others'
        b. Two `pick` sites let link() decide from r's type (or the package's imports): one names the handler
        (registered as the route), the other the middleware; arguments in between are middleware either way."""
        src = self.src
        full = prefix + (path if path != '/' or not prefix else '')
        name = (meth + ' ' if meth and meth != 'GET' else '') + (full or '/')
        router = expr_desc(obj, src)
        first, last = expr_desc(args[1], src), expr_desc(args[-1], src)
        f.registers.append(('route', name, self.site(f, (router, first, last), 'pick', err)))
        self.site(f, (router, last, first), 'pick', err)
        for a in args[1:]:
            if a is not args[1] and a is not args[-1]:
                self.value_site(a, f, err)
            self.walk(a, f, err)
        return {a.id for a in args}

    def methods_of(self, n):
        """gorilla: r.HandleFunc("/p", h).Methods("POST") -> 'POST'."""
        p = n.parent
        if p is not None and p.type == 'selector_expression' and _text(p.child_by_field_name('field'), self.src) == 'Methods':
            gp = p.parent
            if gp is not None and gp.type == 'call_expression':
                ms = [_strval(a, self.src) for a in _args(gp)]
                ms = [m for m in ms if m]
                return ms[0].upper() if len(ms) == 1 else ''
        return ''

    def handler(self, h, func_slot):
        """The handler inside a registration argument: ('lit', func literal) | ('ref', identifier / selector) | None.
        Wrappers are looked through: http.HandlerFunc(h), auth(h), http.StripPrefix("/x", h)."""
        if h is None:
            return None
        if h.type == 'func_literal':
            return ('lit', h)
        if h.type in ('identifier', 'selector_expression'):
            return ('ref', h) if func_slot else None
        if h.type == 'parenthesized_expression' and h.named_children:
            return self.handler(h.named_children[0], func_slot)
        if h.type == 'call_expression':
            fn = h.child_by_field_name('function')
            conv = fn.type == 'selector_expression' and _text(fn.child_by_field_name('field'), self.src) == 'HandlerFunc'
            for a in _args(h):
                if a.type in ('interpreted_string_literal', 'raw_string_literal'):
                    continue
                r = self.handler(a, func_slot or conv)
                if r is not None:
                    return r
        if h.type == 'type_conversion_expression':
            return self.handler(h.child_by_field_name('operand'), True)
        return None

    def route(self, f, meth, prefix, path, h, func_slot, err):
        """A route registration: an inline handler is carved out, a named one registered for route_of; a wrapper
        around the handler (auth(h), http.HandlerFunc(h)) is walked as f's own code. Returns {h.id}, or an empty
        set when no handler was found (the argument is then walked as usual)."""
        full = prefix + (path if path != '/' or not prefix else '')
        full = full or '/'
        name = (meth + ' ' if meth and meth != 'GET' else '') + full
        found = self.handler(h, func_slot)
        if found is None:
            return set()
        kind, node = found
        if kind == 'lit':
            self.synth(f, f'{meth or "ANY"} {full}', node, err, route=('route', name))
        else:
            k = self.value_site(node, f, err)
            if k:
                f.registers.append(('route', name, k))
            if node is h:
                self.walk(h, f, err)
                return {h.id}
        self.skip.add(node.id)
        if node is not h:
            self.walk(h, f, err)
        return {h.id}

    def composite(self, n, f, err):
        """cobra.Command{Use: "serve", RunE: run} / cli.Command{Name: "x", Action: fn}, also as the elided elements
        of []*cli.Command{{…}, …}: a command per literal."""
        src = self.src
        ty = n.child_by_field_name('type')
        body = n.child_by_field_name('body')
        tname = re.sub(r'\s+', '', _text(ty, src)) if ty is not None else ''
        if body is not None and re.search(r'(?:^|\.)Command$', tname):
            if re.match(r'(?:\[\d*\]|map\[[^\]]*\])\*?', tname):        # []*cli.Command{{…}, {…}}
                for el in body.named_children:
                    lit = el.named_children[0] if el.type == 'literal_element' and el.named_children else None
                    if lit is not None and lit.type == 'literal_value':
                        self.command(lit, f, err)
            else:
                self.command(body, f, err)
        for c in body.named_children if body is not None else ():
            self.walk(c, f, err)

    def command(self, body, f, err):
        """The keyed elements of one command literal: Use / Name and Run / RunE / Action; the handler elements it
        takes care of are skipped by the walk of the literal that follows."""
        src = self.src
        pairs = {}
        for el in body.named_children:
            if el.type == 'keyed_element':
                k, v = el.child_by_field_name('key'), el.child_by_field_name('value')
                if k is not None and v is not None:
                    inner = v.named_children[0] if v.type == 'literal_element' and v.named_children else v
                    pairs[_text(k, src)] = (el, inner)
        nm = pairs.get('Use') or pairs.get('Name')
        name = _strval(nm[1], src) if nm else None
        name = name.split()[0] if name and name.split() else None
        for key in ('Run', 'RunE', 'Action'):
            if name and key in pairs:
                el, v = pairs[key]
                if v.type == 'func_literal':
                    self.synth(f, f'cmd {name}', v, err, cmd=name)
                    self.skip.add(el.id)
                elif v.type in ('identifier', 'selector_expression'):
                    k = self.value_site(v, f, err)
                    if k:
                        f.cmd_targets.append((name, k))
                    self.walk(v, f, err)
                    self.skip.add(el.id)
