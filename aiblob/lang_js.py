"""JavaScript / TypeScript for analyze.py: CommonJS + ESM modules, Node / Express heuristics.

JSLang(Func) implements the hooks PyLang has (see analyze.py). Module names come from paths like
Python's: `routes/users.js` -> `routes.users`, `lib/index.js` -> `lib`, dots in a file name become `_`.
Relative imports resolve to those names (`./x` is x.js or x/index.js alike); packages are `npm:<name>`.

Synthetic functions (Func.route set), carved out of the code that holds them:
  app.get('/p', (req, res) => …)        -> `<GET /p>` route (named handlers become routes through registers)
  app.use((req, res, next) => …)        -> `<use>` / `<use /prefix>` hook; 4 parameters: error handler
  if (req.method === 'GET' && pathname === '/p') { … }  (plain node:http) -> `<GET /p>` route
  test('name', () => …) / it / before…  -> `<test name>` in a test module
"""
import re, json, hashlib, posixpath
from tree_sitter import Language, Parser
import tree_sitter_javascript as _tsjs

try:
    import tree_sitter_typescript as _tsts
except ImportError:
    _tsts = None

_JS = Parser(Language(_tsjs.language()))
_TS = Parser(Language(_tsts.language_typescript())) if _tsts else None
_TSX = Parser(Language(_tsts.language_tsx())) if _tsts else None
EXTS = ('.js', '.mjs', '.cjs', '.jsx') + (('.ts', '.mts', '.cts', '.tsx') if _tsts else ())

HTTP_METHODS = {'get', 'post', 'put', 'delete', 'patch', 'all', 'options', 'head'}
TEST_CALLS = {'test', 'it', 'specify'}
TEST_HOOKS = {'before', 'after', 'beforeEach', 'afterEach', 'beforeAll', 'afterAll'}
ENTRY_NAMES = {'server', 'index', 'app', 'main'}
FUNC_TYPES = {'arrow_function', 'function_expression', 'function', 'generator_function'}
DECL_FUNC_TYPES = {'function_declaration', 'generator_function_declaration'}
CLASS_TYPES = {'class_declaration', 'abstract_class_declaration', 'class'}
BRANCH_TYPES = {'if_statement', 'for_statement', 'for_in_statement', 'while_statement', 'do_statement', 'try_statement',
                'catch_clause', 'ternary_expression', 'switch_case'}
ERROR_CALLEES = {'error', 'warn', 'fatal', 'fail'}
STATUS_CALLEE = re.compile(r'(status|sendStatus|writeHead|send\w*|respond\w*|reply\w*|json|end|fail\w*|\w*[Ee]rror)$')
ROUTER_OBJ = re.compile(r'(app|api|server|router|\w*Router|\w*router|routes)$')
FRAMEWORK = re.compile(r'(?:\w+\.)*(?:express|Router|createServer|createApp|Koa|fastify)\(')
COLLECTION = re.compile(r'(Map|Set|WeakMap|WeakSet|Array|Object)$')
COLLECTION_CALL = re.compile(r'(?:Object\.(?:create|assign|fromEntries)|Array\.from|structuredClone)\(')
SESSION_WHOLE = {'destroy', 'regenerate', 'save', 'reload', 'touch'}
STORAGE_KEY = {'getItem', 'setItem', 'removeItem'}
PATH_LIT = re.compile(r'''(['"`])([\w.@+~-]+(?:/[\w.@+~-]+)*/?)\1''')
VIEW_EXTS = ('ejs', 'pug', 'jade', 'hbs', 'handlebars', 'html', 'njk', 'mustache', 'twig')
GLOBALS = frozenset('''console JSON Math Object Array String Number Boolean Date Promise Error TypeError RangeError SyntaxError
    Map Set WeakMap WeakSet Symbol RegExp BigInt Reflect Proxy Intl parseInt parseFloat isNaN isFinite setTimeout setInterval
    clearTimeout clearInterval setImmediate clearImmediate queueMicrotask structuredClone fetch require process Buffer URL
    URLSearchParams encodeURIComponent decodeURIComponent encodeURI decodeURI globalThis AbortController TextEncoder TextDecoder
    crypto performance document window localStorage sessionStorage alert confirm prompt module exports __dirname __filename
    super import eval atob btoa Response Request Headers FormData Blob File Event EventTarget CustomEvent navigator location
    history describe it test expect before after beforeEach afterEach beforeAll afterAll jest vi Function undefined NaN
    Infinity arguments this'''.split())

def _text(n, src):
    return src[n.start_byte:n.end_byte].decode('utf-8', 'replace')

def _norm(callee):
    """Callee text as the resolver reads it: `this.` is `self.`, `?.` is `.`, no whitespace."""
    c = re.sub(r'\s+', '', callee).replace('?.', '.')
    return 'self' + c[4:] if c == 'this' or c.startswith('this.') else c

def _strval(n, src):
    """'x' / "x" / `x` (no substitutions) -> x; None otherwise."""
    if n is None or n.type not in ('string', 'template_string') or any(c.type == 'template_substitution' for c in n.children):
        return None
    return _text(n, src)[1:-1]

def _plus(n, src):
    """Is n a `a + b` expression?"""
    op = n.child_by_field_name('operator') if n is not None and n.type == 'binary_expression' else None
    return op is not None and _text(op, src) == '+'

def _leaves(n, src):
    """The operands of a `a + b + c` chain, left to right."""
    return _leaves(n.child_by_field_name('left'), src) + _leaves(n.child_by_field_name('right'), src) if _plus(n, src) else [n]

def _route_path(n, src):
    """A route path argument -> readable path or None: '/u/:id(\\d+)' -> /u/:id, /^\\/u\\/(\\d+)$/ -> /u/(\\d+)."""
    if n is None:
        return None
    if n.type == 'regex':
        pat = n.child_by_field_name('pattern')
        v = _text(pat, src) if pat is not None else _text(n, src).strip('/')
        v = re.sub(r'(?<!\\)\$$', '', v.lstrip('^')).replace('\\/', '/')
        return v if v.startswith('/') else None
    v = _strval(n, src)
    if v is None or not v.startswith(('/', '*')):
        return None
    return re.sub(r'(:\w+)\((?:[^()\\]|\\.)*\)', r'\1', v)

def _args(call):
    a = call.child_by_field_name('arguments')
    return [c for c in a.named_children if c.type != 'comment'] if a is not None and a.type == 'arguments' else []

def _params(fn):
    p = fn.child_by_field_name('parameters')
    if p is not None:
        return [c for c in p.named_children if c.type != 'comment']
    p = fn.child_by_field_name('parameter')       # x => …
    return [p] if p is not None else []

def _cut(src, start, end, carved):
    parts, pos = [], start
    for s, e, _ in sorted(carved):
        if s >= start and e <= end and s >= pos:
            parts.append(src[pos:s]); pos = e
    parts.append(src[pos:end])
    return b''.join(parts).decode('utf-8', 'replace')

def _hash(s):
    return hashlib.sha1(s.encode()).hexdigest()[:10]

def _status(n, src):
    """A literal 4xx / 5xx argument of an HTTP-reply-looking call in n's member chain -> the code text."""
    while n is not None and n.type == 'call_expression':
        fn = n.child_by_field_name('function')
        last = _text(fn, src).rsplit('.', 1)[-1].strip()
        if STATUS_CALLEE.fullmatch(last):
            for a in _args(n):
                if a.type == 'number' and re.fullmatch(r'[45]\d\d', _text(a, src)):
                    return _text(a, src)
        n = fn.child_by_field_name('object') if fn.type == 'member_expression' else None
    return None

def _is_next_err(n, src, f):
    """next(err) with next a parameter: hands the error to Express's error middleware."""
    if n is None or n.type != 'call_expression':
        return False
    fn, a = n.child_by_field_name('function'), _args(n)
    return fn.type == 'identifier' and _text(fn, src) == 'next' and 'next' in f.locals and len(a) >= 1 \
        and _strval(a[0], src) not in ('route', 'router')

def modname(rel):
    stem = posixpath.splitext(rel)[0] if rel.endswith(EXTS) else rel
    parts = stem.split('/')
    if parts[-1] == 'index' and len(parts) > 1:
        parts = parts[:-1]
    return '.'.join(p.replace('.', '_') for p in parts)

def resolve_spec(rel, spec):
    """require / import specifier seen from file rel -> module name, 'npm:<pkg>', or None (not code / outside)."""
    if not spec.startswith(('.', '/')):
        return 'npm:' + spec
    p = posixpath.normpath(posixpath.join(posixpath.dirname(rel), spec)).lstrip('/')
    if p.startswith('..') or p == '.':
        return None
    ext = posixpath.splitext(p)[1]
    if ext and ext not in EXTS:
        return None                       # ./data.json, ./style.css: a file, not a module
    return modname(p if ext else p + '.js')

class JSLang:
    name, ctor, module_types = 'js', 'constructor', True
    builtins = stdlib = GLOBALS

    def __init__(self, Func, sql_tables):
        self.Func, self.sql_tables = Func, sql_tables     # analyze.sql_tables: one SQL rule for every language

    def handles(self, rel):
        return rel.endswith(EXTS)

    def modname(self, rel):
        return modname(rel)

    def is_test(self, mod, rel):
        parts = rel.split('/')
        base = parts[-1]
        return bool({'test', 'tests', '__tests__', 'spec'} & set(parts[:-1])) or \
            bool(re.search(r'[.-](test|spec)\.[cm]?[jt]sx?$', base)) or posixpath.splitext(base)[0] in ('test', 'tests')

    def parse_module(self, rel, text):
        return _Module(self, rel, text).info

    # ------------------------------------------------------------ analysis hooks

    def link(self, mods, funcs, class_keys):
        """alias: what an import target names once module.exports / export default are read
        ('store' -> 'store.Store' for `module.exports = Store`, 'store.get' -> 'store.Store.get' for
        `module.exports = new Store()`); prefix: (module, router object) -> path it is mounted at."""
        alias = {}
        for m, info in mods.items():
            imp = info['imports']
            for name, (kind, val) in info.get('exports', {}).items():
                keys = [m, m + '.default'] if name in ('', 'default') else [f'{m}.{name}']
                if kind == 'local':
                    tgt = f'{m}.{val}' if f'{m}.{val}' in funcs or f'{m}.{val}' in class_keys else imp.get(val)
                    for k in keys:
                        if tgt and tgt != k and k not in funcs:
                            alias[k] = tgt
                elif kind == 'new':
                    ck = f'{m}.{val}' if f'{m}.{val}' in class_keys else imp.get(val)
                    for q in funcs:
                        if ck and q.startswith(ck + '.') and q.count('.') == ck.count('.') + 1:
                            alias.setdefault(f'{m}.{q.rsplit(".", 1)[1]}', q)
        mounted = {}                                 # (module, object) -> (parent module, parent object, prefix)
        for m, info in mods.items():
            for obj, prefix, target in info.get('mounts', ()):
                head = target.split('.')[0]
                tm = info['imports'].get(head, '')
                tm = tm[:-len('.default')] if tm.endswith('.default') else tm     # import users from './routes/users'
                if tm in mods:
                    tobj = mods[tm].get('exports', {}).get('', ('local', None))[1]
                    mounted.setdefault((tm, tobj), (m, obj, prefix))
                    mounted.setdefault((tm, None), (m, obj, prefix))
                else:
                    mounted.setdefault((m, target), (m, obj, prefix))
        def prefix(m, obj, depth=0):
            up = mounted.get((m, obj)) or mounted.get((m, None)) if obj is not None else mounted.get((m, None))
            if up is None or depth > 8 or (up[0], up[1]) == (m, obj):
                return ''
            return prefix(up[0], up[1], depth + 1) + up[2].rstrip('/')
        return {'alias': alias, 'prefix': prefix}

    def entry(self, ms, mods, imported, closure, is_test, files):
        """package.json `main` / `scripts.start` when it names one of ms; else the entry-like module nobody
        imports with the biggest import closure (server / index / app / main, an app object, a listen call)."""
        try:
            pkg = json.loads(files.get('package.json') or '{}')
        except ValueError:
            pkg = {}
        named = [pkg.get('main')] if isinstance(pkg, dict) and isinstance(pkg.get('main'), str) else []
        start = (pkg.get('scripts') or {}).get('start') if isinstance(pkg, dict) and isinstance(pkg.get('scripts'), dict) else None
        if isinstance(start, str):
            named += re.findall(r'[\w./-]+\.[cm]?[jt]s\b', start)
        for p in named:
            m = modname(posixpath.normpath(p))
            if m in ms:
                return m
        cands = [m for m in ms if m not in imported and not is_test(m) and mods[m]['entry_like']]
        return max(cands, key=lambda m: (len(closure(m)), m.split('.')[-1] in ENTRY_NAMES), default=None)

    def route_of(self, f, extra, ctx):
        """(blob name, kind) for a synthetic handler or a named function registered by app.get / app.use."""
        prefix = ctx.get('prefix', lambda m, o: '')
        spec = f.route
        if spec is None:
            for deco, args in extra:
                obj, _, path = args.partition('\0')
                spec = ('use', path, obj) if deco == 'use' else ('http', deco, path, obj)
                break
        if spec is None:
            return None
        if spec[0] == 'test':
            return None
        if spec[0] == 'use':
            if getattr(f, 'nparams', 0) == 4:
                return ('error handler', 'error-handler')
            p = prefix(f.module, spec[2]) + spec[1] if spec[1] else ''
            return ('middleware' + (' ' + p if p else ''), 'hook')
        _, meth, path, obj = spec
        full = prefix(f.module, obj) + (path if path != '/' or not prefix(f.module, obj) else '')
        meth = (meth or '').upper()
        return ((meth + ' ' if meth and meth != 'GET' else '') + (full or '/'), 'route')

    def snippet(self, f):
        """Up to 3 lines: replies, throws and next(err) first, then returns, then the rest minus requires."""
        lines = []
        body = f.body.splitlines()
        for i, raw in enumerate(body):
            l = raw.strip()
            if i == 0 and getattr(f, 'is_fn', False):     # signature line: keep what follows `=> {` / `) {`
                m = re.match(r'.*(?:=>|\))\s*\{?\s*(.*)$', l)
                l = m.group(1).strip() if m else ''
            if not l or re.fullmatch(r'[{}()\[\];,]+', l) or l.startswith(('//', '/*', '*')) or l == 'try {':
                continue
            lines.append(l)
        def tier(l):
            if re.search(r'\bthrow\b|\bres\.(status|json|send|render|redirect|sendFile|sendStatus|end)\b|\bnext\(\w|'
                         r'\bsend\w*\(|\bquery\(|\bexec\w*\(|\breject\(', l):
                return 0
            if re.match(r'return\b', l):
                return 1
            return 3 if re.search(r'\brequire\(|^import\b', l) else 2
        best = sorted(range(len(lines)), key=lambda i: (tier(lines[i]), i))
        return [lines[i] for i in sorted(i for i in best[:3] if tier(lines[i]) < 3)]

    def extra_refs(self, f, others, ctx):
        """res.render('view') -> the one template it names; express.static('public') -> every file below public/."""
        out = []
        for v in getattr(f, 'views', ()):
            v = v.lstrip('./')
            hits = [p for p in others if p.rsplit('.', 1)[-1] in VIEW_EXTS and
                    (posixpath.splitext(p)[0] == v or posixpath.splitext(p)[0].endswith('/' + v) or p == v or p.endswith('/' + v))]
            pref = [p for p in hits if '/views/' in '/' + p]
            hits = pref if len(pref) == 1 else hits
            if len(hits) == 1:
                out.append((v, hits[0]))
        for d in getattr(f, 'dirs', ()):
            d = d.strip('/').removeprefix('./')
            out += [(d, p) for p in others if d and p.startswith(d + '/')]
        return out

class _Module:
    """One parse: builds the module info dict analyze() expects, plus the JS extras (exports, mounts)."""

    def __init__(self, lang, rel, text):
        self.lang, self.Func, self.rel = lang, lang.Func, rel
        self.src = src = text.encode('utf-8')
        parser = _TSX if rel.endswith('.tsx') else _TS if rel.endswith(('.ts', '.mts', '.cts')) else _JS
        root = parser.parse(src).root_node
        self.mod = mod = modname(rel)
        self.test = lang.is_test(mod, rel)
        base = posixpath.splitext(rel.rsplit('/', 1)[-1])[0]
        self.info = info = {'name': mod, 'file': rel, 'imports': {}, 'funcs': [], 'globals': set(), 'classes': set(),
                            'blueprints': set(), 'framework': set(), 'lines': text.count('\n') + 1,
                            'entry_like': base in ENTRY_NAMES, 'deps': [], 'global_tables': {},
                            'class_attrs': {}, 'exports': {}, 'mounts': []}
        self.names = {}            # synthetic names used so far -> count (unique quals)
        self.chain_hooks = []      # (named verb handler, hooks of its route chain) for the end of the module
        self.skip = set()          # node ids of functions / classes that are Funcs of their own (the top walk skips them)
        top = self.Func(mod, '<module>', root, src)
        top.is_fn = False
        self.top = top
        stmts = []
        for n in root.named_children:
            if n.type == 'comment':
                continue
            self.declare(n, top)
            if n.type not in DECL_FUNC_TYPES and n.type not in CLASS_TYPES and not (
                    n.type == 'export_statement' and self._decl(n) is not None and self._decl(n).type in DECL_FUNC_TYPES | CLASS_TYPES):
                stmts.append(n)
        for n in stmts:
            self.walk(n, top, False)
        for target, hooks in self.chain_hooks:
            for f in info['funcs']:
                if f.name == target and not f.cls:
                    f.calls.extend((h, False, 'deferred') for h in hooks)
        for k, v in top.imports.items():
            info['imports'].setdefault(k, v)
        top.locals -= info['globals']      # module code reads and mutates its own state like every handler does
        top.lines = len(stmts)
        cuts = top.carved + [(n.start_byte, n.end_byte, 0) for n in self._skipped(root)]
        top.body = '\n'.join(_cut(src, n.start_byte, n.end_byte, cuts) for n in stmts)
        top.hash = _hash(top.body)
        top.file = rel
        info['deps'].extend(top.deps)
        info['top'] = top
        for f in info['funcs']:
            info['deps'].extend(f.deps)

    def _decl(self, n):
        d = n.child_by_field_name('declaration')
        if d is None:
            d = next((c for c in n.named_children if c.type in DECL_FUNC_TYPES | CLASS_TYPES | FUNC_TYPES), None)
        return d

    def _skipped(self, root):
        out, stack = [], [root]
        while stack:
            n = stack.pop()
            if n.id in self.skip:
                out.append(n)
            else:
                stack.extend(n.children)
        return out

    # ------------------------------------------------------------ module level declarations

    def declare(self, n, top):
        """Module-level bindings: imports, functions, classes, globals, exports, entry signals."""
        src, info, t = self.src, self.info, n.type
        if t == 'import_statement':
            self.import_stmt(n, info['imports'], info['deps'])
        elif t == 'export_statement':
            d = self._decl(n)
            default = any(c.type == 'default' for c in n.children)
            if d is not None and d.type in DECL_FUNC_TYPES | CLASS_TYPES:
                name = d.child_by_field_name('name')
                nm = _text(name, src) if name is not None else 'default'
                self.define(d, nm)
                info['exports']['' if default else nm] = ('local', nm)
                if default:
                    info['exports']['default'] = ('local', nm)
            elif d is not None and d.type in ('lexical_declaration', 'variable_declaration'):
                self.declare(d, top)
                for v in d.named_children:
                    nm = v.child_by_field_name('name')
                    if v.type == 'variable_declarator' and nm is not None and nm.type == 'identifier':
                        info['exports'][_text(nm, src)] = ('local', _text(nm, src))
            elif default:
                v = n.child_by_field_name('value') or (n.named_children[-1] if n.named_children else None)
                self.export_value('', v)
            for c in n.named_children:
                if c.type == 'export_clause':
                    srcn = n.child_by_field_name('source')
                    tgt = resolve_spec(self.rel, _strval(srcn, src) or '') if srcn is not None else None
                    for s in c.named_children:
                        nm, al = s.child_by_field_name('name'), s.child_by_field_name('alias')
                        if nm is None:
                            continue
                        key = _text(al if al is not None else nm, src)
                        if tgt:
                            info['imports'].setdefault(key, f'{tgt}.{_text(nm, src)}')
                        info['exports'][key] = ('local', key if tgt else _text(nm, src))
        elif t in DECL_FUNC_TYPES or t in CLASS_TYPES:
            name = n.child_by_field_name('name')
            if name is not None:
                self.define(n, _text(name, src))
        elif t in ('lexical_declaration', 'variable_declaration'):
            mutable = t == 'variable_declaration' or (n.children and n.children[0].type == 'let')
            for v in n.named_children:
                if v.type == 'variable_declarator':
                    self.declarator(v, mutable, top)
        elif t == 'expression_statement' and n.named_children:
            e = n.named_children[0]
            if e.type == 'assignment_expression':
                left, right = e.child_by_field_name('left'), e.child_by_field_name('right')
                lt = _text(left, src).replace(' ', '')
                m = re.fullmatch(r'(?:module\.)?exports(?:\.(\w+))?', lt)
                if m and (m.group(1) or lt == 'module.exports'):
                    self.export_value(m.group(1) or '', right)
        elif t == 'if_statement':
            if re.search(r'require\.main\s*===?\s*module|import\.meta\.url', _text(n.child_by_field_name('condition'), src)):
                info['entry_like'] = True

    def define(self, n, name, cls=None):
        """A function / class node -> Func(s) of this module."""
        if n.type in CLASS_TYPES:
            self.info['classes'].add(name)
            self.skip.add(n.id)
            body = n.child_by_field_name('body')
            for m in body.named_children if body is not None else ():
                if m.type == 'method_definition':
                    self.func(m, m, _text(m.child_by_field_name('name'), self.src), name)
                elif m.type in ('field_definition', 'public_field_definition'):
                    p, v = m.child_by_field_name('property') or m.child_by_field_name('name'), m.child_by_field_name('value')
                    if p is not None and v is not None and v.type in FUNC_TYPES:
                        self.func(m, v, _text(p, self.src), name)
                    elif p is not None and v is not None and v.type == 'new_expression':
                        self.info['class_attrs'].setdefault(name, {}).setdefault(
                            _text(p, self.src), _norm(_text(v.child_by_field_name('constructor'), self.src)))
            return
        self.skip.add(n.id)
        self.func(n, n, name, cls)

    def func(self, outer, fn, name, cls=None):
        f = self.Func(self.mod, name, outer, self.src, cls)
        f.is_fn = True
        self.walk_fn(fn, f, False)
        self.add(f)
        return f

    def add(self, f):
        if f.carved:
            node_start = f.__dict__.pop('_node')
            f.body = _cut(self.src, node_start[0], node_start[1], f.carved)
            f.lines -= sum(n for _, _, n in f.carved)
            f.hash = _hash(f.body)
        else:
            f.__dict__.pop('_node', None)
        f.file = self.rel
        self.info['funcs'].append(f)

    def walk_fn(self, fn, f, err):
        f._node = (fn.start_byte, fn.end_byte) if not hasattr(f, '_node') else f._node
        params = _params(fn)
        f.nparams = getattr(f, 'nparams', 0) or len(params)
        for p in params:
            self.bind(p, f)
        body = fn.child_by_field_name('body')
        if body is not None:
            self.walk(body, f, err)

    def declarator(self, v, mutable, top):
        src, info = self.src, self.info
        name, val = v.child_by_field_name('name'), v.child_by_field_name('value')
        if name is None:
            return
        nm = _text(name, src) if name.type == 'identifier' else None
        while val is not None and val.type in ('as_expression', 'satisfies_expression', 'non_null_expression') and val.named_children:
            val = val.named_children[0]        # [] as Item[] / {} satisfies State
        req = self.require_of(val)
        if req is not None:
            self.bind_require(name, req, info['imports'], info['deps'])
            return
        if nm is None:
            return
        if val is not None and val.type in FUNC_TYPES:
            self.define(val, nm)
            return
        if val is not None and val.type in CLASS_TYPES:
            self.define(val, nm)
            return
        vt = _text(val, src) if val is not None else ''
        if val is not None and val.type == 'object' and any(
                c.type == 'method_definition' or (c.type == 'pair' and c.child_by_field_name('value').type in FUNC_TYPES)
                for c in val.named_children):
            info['classes'].add(nm)              # an object of functions: a namespace, `api.get()` -> api.get
            data = False
            for c in val.named_children:
                if c.type == 'method_definition':
                    self.skip.add(c.id)
                    self.func(c, c, _text(c.child_by_field_name('name'), src), nm)
                elif c.type == 'pair' and c.child_by_field_name('value').type in FUNC_TYPES:
                    self.skip.add(c.child_by_field_name('value').id)
                    self.func(c, c.child_by_field_name('value'), _strval(c.child_by_field_name('key'), src)
                              or _text(c.child_by_field_name('key'), src), nm)
                else:
                    data = True
            if data or mutable:
                info['globals'].add(nm)
            return
        if FRAMEWORK.match(vt) or (val is not None and val.type == 'call_expression' and
                                   re.match(r'(?:\w+\.)*Router\(|express\(', vt)):
            info['framework'].add(nm)
            if re.match(r'express\(|(?:\w+\.)*createServer\(', vt):
                info['entry_like'] = True
            return
        if val is not None and val.type == 'new_expression':
            top.origins.setdefault(nm, _norm(_text(val.child_by_field_name('constructor'), src)))
        if mutable or (val is not None and (val.type in ('object', 'array') or
                                             (val.type == 'new_expression' and not FRAMEWORK.match(vt)) or
                                             COLLECTION_CALL.match(vt))):
            info['globals'].add(nm)
            if val is not None and val.type in ('string', 'template_string'):
                tables = self.lang.sql_tables(vt[1:-1])
                if tables:
                    info['global_tables'][nm] = tables
        elif val is not None and val.type in ('string', 'template_string'):
            tables = self.lang.sql_tables(vt[1:-1])
            if tables:                            # const SCHEMA = `CREATE TABLE …`: whoever runs it touches the tables
                info['globals'].add(nm)
                info['global_tables'][nm] = tables

    def export_value(self, key, v):
        """module.exports = X / exports.k = X / export default X."""
        if v is None:
            return
        src, ex = self.src, self.info['exports']
        if v.type in FUNC_TYPES or v.type in CLASS_TYPES:
            nm = v.child_by_field_name('name')
            nm = _text(nm, src) if nm is not None else (key or 'default')
            self.define(v, nm)
            ex[key] = ('local', nm)
        elif v.type == 'identifier':
            ex[key] = ('local', _text(v, src))
        elif v.type == 'new_expression':
            ex[key] = ('new', _norm(_text(v.child_by_field_name('constructor'), src)))
        elif v.type == 'object' and not key:
            for c in v.named_children:
                if c.type == 'shorthand_property_identifier':
                    ex[_text(c, src)] = ('local', _text(c, src))
                elif c.type == 'method_definition':
                    nm = _text(c.child_by_field_name('name'), src)
                    self.skip.add(c.id)
                    self.func(c, c, nm)
                    ex[nm] = ('local', nm)
                elif c.type == 'pair':
                    k, val = c.child_by_field_name('key'), c.child_by_field_name('value')
                    nm = _strval(k, src) or _text(k, src)
                    if val.type in FUNC_TYPES:
                        self.skip.add(val.id)
                        self.func(c, val, nm)
                        ex[nm] = ('local', nm)
                    elif val.type in ('identifier', 'member_expression'):
                        ex[nm] = ('local', _text(val, src))

    # ------------------------------------------------------------ imports

    def require_of(self, n):
        """require('x') / require('x').a / await import('x') -> (spec, member or None)."""
        while n is not None and n.type in ('await_expression', 'parenthesized_expression'):
            n = n.named_children[0] if n.named_children else None
        if n is None:
            return None
        if n.type == 'member_expression':
            r = self.require_of(n.child_by_field_name('object'))
            return (r[0], _text(n.child_by_field_name('property'), self.src)) if r and r[1] is None else None
        if n.type == 'call_expression':
            fn, a = n.child_by_field_name('function'), _args(n)
            if fn is not None and _text(fn, self.src) in ('require', 'import') and a and _strval(a[0], self.src) is not None:
                return (_strval(a[0], self.src), None)
        return None

    def bind_require(self, pattern, req, imports, deps):
        spec, member = req
        tgt = resolve_spec(self.rel, spec)
        if tgt is None:
            self.top.strings.append(spec)          # require('./data.json'): a file reference
            return
        if not tgt.startswith('npm:'):
            deps.append(tgt)
        base = f'{tgt}.{member}' if member else tgt
        if pattern.type == 'identifier':
            imports[_text(pattern, self.src)] = base
        elif pattern.type == 'object_pattern':
            for c in pattern.named_children:
                if c.type == 'shorthand_property_identifier_pattern':
                    imports[_text(c, self.src)] = f'{base}.{_text(c, self.src)}'
                elif c.type == 'pair_pattern':
                    k, v = c.child_by_field_name('key'), c.child_by_field_name('value')
                    if v is not None and v.type == 'assignment_pattern':
                        v = v.child_by_field_name('left')
                    if v is not None and v.type == 'identifier':
                        imports[_text(v, self.src)] = f'{base}.{_text(k, self.src)}'
                elif c.type == 'object_assignment_pattern':
                    l = c.child_by_field_name('left')
                    if l is not None:
                        imports[_text(l, self.src)] = f'{base}.{_text(l, self.src)}'

    def import_stmt(self, n, imports, deps):
        src = self.src
        s = n.child_by_field_name('source')
        spec = _strval(s, src) if s is not None else None
        if spec is None:
            return
        tgt = resolve_spec(self.rel, spec)
        if tgt is None:
            self.top.strings.append(spec)
            return
        if not tgt.startswith('npm:'):
            deps.append(tgt)
        for c in n.named_children:
            if c.type != 'import_clause':
                continue
            for d in c.named_children:
                if d.type == 'identifier':
                    imports[_text(d, src)] = f'{tgt}.default'
                elif d.type == 'namespace_import':
                    ids = [x for x in d.named_children if x.type == 'identifier']
                    if ids:
                        imports[_text(ids[-1], src)] = tgt
                elif d.type == 'named_imports':
                    for sp in d.named_children:
                        nm, al = sp.child_by_field_name('name'), sp.child_by_field_name('alias')
                        if nm is not None:
                            imports[_text(al if al is not None else nm, src)] = f'{tgt}.{_text(nm, src)}'

    # ------------------------------------------------------------ bindings

    def bind(self, p, f):
        """Identifiers a parameter / declaration pattern binds -> f.locals."""
        if p is None:
            return
        t = p.type
        if t in ('identifier', 'shorthand_property_identifier_pattern'):
            f.locals.add(_text(p, self.src))
        elif t in ('required_parameter', 'optional_parameter'):
            self.bind(p.child_by_field_name('pattern'), f)
        elif t in ('assignment_pattern', 'object_assignment_pattern'):
            self.bind(p.child_by_field_name('left'), f)
        elif t == 'pair_pattern':
            self.bind(p.child_by_field_name('value'), f)
        elif t in ('object_pattern', 'array_pattern', 'rest_pattern'):
            for c in p.named_children:
                self.bind(c, f)

    # ------------------------------------------------------------ synthetic handlers

    def synth(self, parent, label, node, route, fn=None, closure=True):
        """A Func `<label>` carved out of parent: an inline handler (fn: its function node) or an if-branch block.
        closure: it sees (and binds into) the parent's names, like the code around it."""
        n = self.names.get(label, 0) + 1
        self.names[label] = n
        f = self.Func(self.mod, f'<{label}>' if n == 1 else f'<{label} #{n}>', node, self.src)
        f.route, f.is_fn = route, fn is not None
        if closure:
            f.locals, f.nested, f.imports, f.origins = parent.locals, parent.nested, parent.imports, parent.origins
        f._node = (node.start_byte, node.end_byte)
        parent.carved.append((node.start_byte, node.end_byte, node.end_point[0] - node.start_point[0] + 1))
        return f

    def handler_args(self, f, n, label, route, err):
        """The inline function arguments of call n become one synthetic Func; identifier arguments are its
        deferred calls (route middleware). Returns that Func or None when there is no inline function."""
        args = _args(n)
        fns = [a for a in args if a.type in FUNC_TYPES]
        if not fns:
            return None
        top = f is self.top
        g = self.synth(f, label, fns[-1], route, fn=fns[-1], closure=not top)
        g.nparams = len(_params(fns[-1]))
        for a in args:
            if a.type in FUNC_TYPES:
                if a is not fns[-1]:
                    for p in _params(a):
                        self.bind(p, g)
                    body = a.child_by_field_name('body')
                    if body is not None:
                        self.walk(body, g, err)
                    f.carved.append((a.start_byte, a.end_byte, a.end_point[0] - a.start_point[0] + 1))
            elif a.type in ('identifier', 'member_expression'):
                g.calls.append((_norm(_text(a, self.src)), err, 'deferred'))
            else:
                self.walk(a, f, err)
        self.walk_fn(fns[-1], g, err)
        if len(fns) > 1:              # inline middleware before the handler: one Func spans them all
            g.body = _text(n, self.src)
            g.start, g.end = fns[0].start_point[0] + 1, fns[-1].end_point[0] + 1
            g.lines = g.end - g.start + 1
            g.hash = _hash(g.body)
            g._node = (n.start_byte, n.end_byte)
        self.add(g)
        return g

    def registration(self, f, n, err):
        """app.get('/p', …) / router.use(…) / test('…', …): handled here (True) or not a registration (False)."""
        src = self.src
        fn = n.child_by_field_name('function')
        args = _args(n)
        callee = _norm(_text(fn, src))
        if self.test and (callee.split('.')[0] in TEST_CALLS or callee.split('.')[0] in TEST_HOOKS):
            head = callee.split('.')[0]
            name = _strval(args[0], src) if args else None
            if head in TEST_CALLS and name is None:
                return False
            label = f'{head} {name}' if name is not None else head
            return self.handler_args(f, n, label, ('test',), err) is not None
        if fn.type != 'member_expression':
            return False
        chain = self.route_chain(n)
        if chain is not None:
            return self.chain_routes(f, chain, err)
        obj, meth = _norm(_text(fn.child_by_field_name('object'), src)), _text(fn.child_by_field_name('property'), src)
        if '.' in obj or '(' in obj:
            return False
        if meth in HTTP_METHODS and len(args) >= 2 and _route_path(args[0], src) is not None:
            path = _route_path(args[0], src)
            g = self.handler_args(f, n, f'{meth.upper()} {path}', ('http', meth, path, obj), err)
            if g is None:                           # app.get('/p', auth, listUsers): the last one handles it
                last = args[-1]
                if last.type in ('identifier', 'member_expression'):
                    f.registers.append((meth, f'{obj}\0{path}', _norm(_text(last, src))))
                return False                        # still walked: its identifiers are deferred calls
            return True
        if meth == 'use' and (ROUTER_OBJ.fullmatch(obj) or obj in self.info['framework']) and args:
            prefix = _strval(args[0], src) or ''
            if prefix and not prefix.startswith('/'):
                return False
            rest = args[1:] if prefix else args
            for a in rest:
                if a.type in ('identifier', 'member_expression'):
                    t = _norm(_text(a, src))
                    self.info['mounts'].append((obj, prefix, t))
                    f.registers.append(('use', f'{obj}\0{prefix}', t))
            g = self.handler_args(f, n, f'use {prefix}'.strip(), ('use', prefix, obj), err)
            return g is not None
        return False

    def route_chain(self, n):
        """app.route('/p').all(a).get(b).post(c) -> (object, path, [(verb, call node)…]) or None."""
        src, calls = self.src, []
        while n is not None and n.type == 'call_expression':
            fn = n.child_by_field_name('function')
            if fn is None or fn.type != 'member_expression':
                return None
            meth, o = _text(fn.child_by_field_name('property'), src), fn.child_by_field_name('object')
            if meth == 'route':
                a, obj = _args(n), _norm(_text(o, src))
                path = _route_path(a[0], src) if a else None
                return (obj, path, calls[::-1]) if calls and path is not None and o.type == 'identifier' else None
            if meth not in HTTP_METHODS:
                return None
            calls.append((meth, n))
            n = o
        return None

    def chain_routes(self, f, chain, err):
        """One route per verb of a route chain; `.all(mw)` is a hook that every verb of the chain defers to."""
        src, (obj, path, calls) = self.src, chain
        hooks = []
        for meth, c in [x for x in calls if x[0] == 'all']:
            g = self.handler_args(f, c, f'use {path}', ('use', path, obj), err)
            if g is not None:
                hooks.append(g.name)
            for a in _args(c):
                if a.type in ('identifier', 'member_expression'):
                    t = _norm(_text(a, src))
                    f.registers.append(('use', f'{obj}\0{path}', t))
                    f.calls.append((t, err, 'deferred'))
                    hooks.append(t)
        for meth, c in [x for x in calls if x[0] != 'all']:
            g = self.handler_args(f, c, f'{meth.upper()} {path}', ('http', meth, path, obj), err)
            if g is not None:
                g.calls.extend((h, err, 'deferred') for h in hooks)
                continue
            for a in _args(c):
                if a.type in ('identifier', 'member_expression'):
                    f.calls.append((_norm(_text(a, src)), err, 'deferred'))
                else:
                    self.walk(a, f, err)
            last = _args(c)[-1] if _args(c) else None
            if last is not None and last.type in ('identifier', 'member_expression'):
                t = _norm(_text(last, src))
                f.registers.append((meth, f'{obj}\0{path}', t))
                self.chain_hooks.append((t, hooks))
        return True

    def http_branch(self, cond):
        """`req.method === 'GET' && pathname === '/p'` -> ('GET', '/p'); None when it does not name a path."""
        meth = path = None
        stack = [cond]
        while stack:
            c = stack.pop()
            if c.type == 'parenthesized_expression':
                stack.extend(c.named_children)
            elif c.type == 'binary_expression':
                op = c.child_by_field_name('operator')
                op = _text(op, self.src) if op is not None else ''
                l, r = c.child_by_field_name('left'), c.child_by_field_name('right')
                if op == '&&':
                    stack += [l, r]
                elif op in ('===', '=='):
                    for x, y in ((l, r), (r, l)):
                        v = _strval(y, self.src)
                        if v is None:
                            continue
                        xt = _text(x, self.src)
                        if re.search(r'(^|\.)method$', xt) and v.isalpha():
                            meth = v.upper()
                        elif re.search(r'(^|\.)(pathname|url|path)$', xt) and v.startswith('/'):
                            path = v
                elif op == '||':
                    return None
        return (meth, path) if path else None

    # ------------------------------------------------------------ the walk

    def walk(self, n, f, err):
        src, t = self.src, n.type
        if n.id in self.skip:
            return
        if t in BRANCH_TYPES:
            f.branches += 1
        elif t == 'binary_expression':
            op = n.child_by_field_name('operator')
            if op is not None and _text(op, src) in ('&&', '||', '??'):
                f.branches += 1
        if t == 'if_statement':
            cons = n.child_by_field_name('consequence')
            hb = self.http_branch(n.child_by_field_name('condition'))
            if hb and cons is not None and cons.type == 'statement_block':
                meth, path = hb
                g = self.synth(f, f'{meth or "ALL"} {path}', cons, ('http', meth, path, None))
                self.walk(cons, g, err)
                self.add(g)
                for c in n.children:
                    if c is not cons:
                        self.walk(c, f, err)
                return
            self.walk_if(n, f, err)
            return
        if t == 'catch_clause':
            self.bind(n.child_by_field_name('parameter'), f)
            for c in n.children:
                self.walk(c, f, True)
            return
        if t == 'throw_statement':
            f.errors += 1
            err = True
        if t == 'call_expression':
            if self.registration(f, n, err):
                return
            self.call(n, f, err)
        elif t == 'new_expression':
            ctor = n.child_by_field_name('constructor')
            callee = _norm(_text(ctor, src))
            f.calls.append((callee, err, 'call'))
            p = n.parent
            if p is not None and p.type == 'variable_declarator' and p.child_by_field_name('name') is not None and \
                    p.child_by_field_name('name').type == 'identifier':
                f.origins.setdefault(_text(p.child_by_field_name('name'), src), callee)
            for a in _args(n):
                if a.type in ('identifier', 'member_expression'):
                    f.calls.append((_norm(_text(a, src)), err, 'deferred'))
        elif t in ('string', 'template_string'):
            txt = _text(n, src)
            f.data |= self.sql(n)
            m = PATH_LIT.fullmatch(txt)
            if m and ('.' in m.group(2) or '/' in m.group(2)):
                f.strings.append(m.group(2))
        elif t in ('member_expression', 'subscript_expression'):
            self.member(n, f)
        elif t in ('identifier', 'shorthand_property_identifier'):
            f.names.add(_text(n, src))
        elif t == 'variable_declarator':
            name, val = n.child_by_field_name('name'), n.child_by_field_name('value')
            req = self.require_of(val)
            if req is not None:          # module level: already in the module's imports (declare)
                if f is not self.top:
                    self.bind_require(name, req, f.imports, f.deps)
                return
            self.bind(name, f)
            if name is not None and name.type == 'identifier' and val is not None:
                if val.type in FUNC_TYPES:
                    f.nested.add(_text(name, src))
                elif val.type == 'call_expression':
                    f.origins.setdefault(_text(name, src), _norm(_text(val.child_by_field_name('function'), src)))
            if val is not None:
                self.walk(val, f, err)
            return
        elif t == 'assignment_expression':
            left, right = n.child_by_field_name('left'), n.child_by_field_name('right')
            if left is not None and left.type == 'member_expression' and right is not None and \
                    _text(left.child_by_field_name('object'), src) == 'this' and right.type in ('new_expression', 'call_expression'):
                ctor = right.child_by_field_name('constructor' if right.type == 'new_expression' else 'function')
                f.self_types.setdefault(_text(left.child_by_field_name('property'), src), _norm(_text(ctor, src)))
            elif left is not None and left.type == 'identifier' and right is not None and right.type == 'new_expression':
                f.origins.setdefault(_text(left, src), _norm(_text(right.child_by_field_name('constructor'), src)))
        elif t in DECL_FUNC_TYPES or t in CLASS_TYPES:
            name = n.child_by_field_name('name')
            if name is not None:
                f.nested.add(_text(name, src))
                f.locals.add(_text(name, src))
        elif t in FUNC_TYPES or t == 'method_definition':
            for p in _params(n):
                self.bind(p, f)
            body = n.child_by_field_name('body')
            if body is not None:
                self.walk(body, f, err)
            return
        elif t == 'for_in_statement':
            self.bind(n.child_by_field_name('left'), f)
        elif t == 'return_statement' and n.named_children:
            if n.named_children[0].type in ('object', 'array', 'call_expression'):
                f.returns_data = True
        for c in n.children:
            self.walk(c, f, err)

    def walk_if(self, n, f, err):
        """Blocks of an if / else that end in an error exit: their tail is on the error path."""
        for c in n.children:
            if c.type == 'else_clause':
                for d in c.children:
                    self.walk_branch(d, f, err)
            elif c == n.child_by_field_name('consequence'):
                self.walk_branch(c, f, err)
            else:
                self.walk(c, f, err)

    def walk_branch(self, b, f, err):
        if b.type == 'statement_block':
            errs = self.error_children(b, f)
            for i, c in enumerate(b.children):
                self.walk(c, f, err or i in errs)
        else:
            self.walk(b, f, err or self.is_error_exit(b, f))

    def error_children(self, block, f):
        kids = block.children
        idx = [i for i, c in enumerate(kids) if c.is_named and c.type != 'comment']
        while len(idx) > 1 and kids[idx[-1]].type == 'return_statement' and not kids[idx[-1]].named_children:
            last = idx[-1]
            idx = idx[:-1]                            # `res.status(400).send(x); return;`
            if self.is_error_exit(kids[idx[-1]], f):
                idx.append(last)
                break
            return set()
        if not idx or not self.is_error_exit(kids[idx[-1]], f):
            return set()
        start = max((i + 1 for i in idx[:-1] if self.has_normal_exit(kids[i], f)), default=0)
        return {i for i in idx if i >= start}

    def is_error_exit(self, n, f):
        if n.type == 'throw_statement':
            return True
        e = n.named_children[0] if n.type in ('return_statement', 'expression_statement') and n.named_children else None
        while e is not None and e.type in ('await_expression', 'parenthesized_expression') and e.named_children:
            e = e.named_children[0]
        return e is not None and e.type == 'call_expression' and (_status(e, self.src) is not None or _is_next_err(e, self.src, f))

    def has_normal_exit(self, n, f):
        if n.type == 'return_statement':
            return not self.is_error_exit(n, f)
        if n.type in FUNC_TYPES or n.type in DECL_FUNC_TYPES or n.type in CLASS_TYPES:
            return False
        return any(self.has_normal_exit(c, f) for c in n.named_children)

    def call(self, n, f, err):
        src = self.src
        fn = n.child_by_field_name('function')
        callee = _norm(_text(fn, src))
        if callee == 'await':          # `await (x)` parses as a call to `await`
            return
        how = 'unplaced' if self.dynamic_route(fn, n) else 'call'     # app[method]('/p', h): never resolves, counts as unresolved
        last = callee.rsplit('.', 1)[-1]
        code = _status(n, src)
        p = n.parent
        outer = p is not None and p.type == 'member_expression' and p.child_by_field_name('object') == n and \
            p.parent is not None and p.parent.type == 'call_expression'     # res.status(404) inside res.status(404).json()
        is_err = False
        if code is not None and not outer:
            f.errors += 1
            is_err = True
            if code.startswith('5'):
                f.aborts.append(None)
        if _is_next_err(n, src, f):
            f.errors += 1
            f.aborts.append(None)
            is_err = True
        f.calls.append((callee, err or is_err or last in ERROR_CALLEES, how))
        if f is self.top and (last == 'listen' or callee == 'process.on'):
            self.info['entry_like'] = True
        args = _args(n)
        err_cb = last == 'catch' or (last in ('on', 'once') and args and _strval(args[0], src) == 'error')
        for a in args:
            if a.type in ('identifier', 'member_expression') and _text(a, src) not in ('undefined', 'null'):
                f.calls.append((_norm(_text(a, src)), err or err_cb, 'deferred'))
        if err_cb:                     # .catch(err => …) / .on('error', …): the callback body is an error path
            for a in args:
                self.walk(a, f, True)
            self.walk(fn, f, err)
            return
        if last == 'render' and args and _strval(args[0], src):
            f.views = getattr(f, 'views', []) + [_strval(args[0], src)]
        if last in ('static',) or callee in ('serveStatic', 'express.static'):
            lits = [_strval(x, src) for a in args for x in self._strings(a)]
            f.dirs = getattr(f, 'dirs', []) + [x for x in lits if x and x not in ('.',)][-1:]
        if fn.type == 'member_expression':
            o = fn.child_by_field_name('object')
            if o.type == 'identifier' and _text(o, src) in ('localStorage', 'sessionStorage') and last in STORAGE_KEY and args:
                k = _strval(args[0], src)
                f.data.add(f'ctx:{_text(o, src)}' + (f'.{k}' if k else ''))

    def sql(self, n):
        """The tables a string names when it reads like SQL (analyze.sql_tables). A `'a' + x + 'b'` chain is one string,
        read at its leftmost piece, each other operand a `?`; a bare string statement ('use strict') names none."""
        src, top = self.src, n
        if n.parent is not None and n.parent.type == 'expression_statement':
            return set()
        while _plus(top.parent, src):
            top = top.parent
        leaves = _leaves(top, src)
        if leaves[0] != n:
            return set()
        return self.lang.sql_tables(''.join(_text(x, src)[1:-1] if x.type in ('string', 'template_string') else ' ? '
                                            for x in leaves))      # a value in between: a placeholder, no identifier

    def dynamic_route(self, fn, n):
        """app[method]('/p', handler): a registration whose verb is a variable."""
        if fn.type != 'subscript_expression':
            return False
        o, args = fn.child_by_field_name('object'), _args(n)
        return o.type == 'identifier' and (ROUTER_OBJ.fullmatch(_text(o, self.src)) or _text(o, self.src) in self.info['framework']) \
            and _strval(fn.child_by_field_name('index'), self.src) is None and bool(args) and _route_path(args[0], self.src) is not None

    def _strings(self, n):
        out, stack = [], [n]
        while stack:
            c = stack.pop()
            if c.type in ('string', 'template_string'):
                out.append(c)
            else:
                stack.extend(c.named_children)
        return out

    def member(self, n, f):
        """Data symbols: req.body / req.session.x / res.locals.x / process.env.X / localStorage.k; module.GLOBAL."""
        src = self.src
        obj = n.child_by_field_name('object')
        if obj is None or obj.type not in ('identifier', 'this'):
            return
        b = _text(obj, src)
        key = self._key(n)
        p = n.parent
        if b in ('req', 'request') and key:
            if key == 'session':
                sub = self._key(p) if p is not None and p.type in ('member_expression', 'subscript_expression') and \
                    p.child_by_field_name('object') == n else None
                whole = sub is None or (sub in SESSION_WHOLE and p.parent is not None and p.parent.type == 'call_expression')
                f.data.add('ctx:session' + ('' if whole else '.' + sub))
            else:
                f.data.add(f'ctx:req.{key}')
        elif b in ('res', 'response') and key == 'locals':
            sub = self._key(p) if p is not None and p.type in ('member_expression', 'subscript_expression') and \
                p.child_by_field_name('object') == n else None
            f.data.add('ctx:locals' + (f'.{sub}' if sub else ''))
        elif b == 'process' and key == 'env':
            sub = self._key(p) if p is not None and p.type in ('member_expression', 'subscript_expression') and \
                p.child_by_field_name('object') == n else None
            f.data.add('ctx:env' + (f'.{sub}' if sub else ''))
        elif b in ('localStorage', 'sessionStorage') and key and key not in STORAGE_KEY | {'clear', 'key', 'length'}:
            f.data.add(f'ctx:{b}.{key}')
        elif n.type == 'member_expression' and obj.type == 'identifier' and key:
            f.attr_refs.add((b, key))

    def _key(self, n):
        if n is None:
            return None
        if n.type == 'member_expression':
            p = n.child_by_field_name('property')
            return _text(p, self.src) if p is not None else None
        if n.type == 'subscript_expression':
            return _strval(n.child_by_field_name('index'), self.src)
        return None
