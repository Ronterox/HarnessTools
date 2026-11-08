"""Static analysis of a Python snapshot into blobs and edges.

analyze(files: {relpath: text}, cache=None) -> {'blobs': [...], 'edges': [...], 'modules': {...}, 'bodies': {...}}
cache: a dict reused across calls; modules whose (path, text) did not change are not re-parsed.

Languages: a file's extension picks its Lang (PyLang here, lang_js.JSLang for .js/.mjs/.cjs/.ts);
a snapshot may mix them. Calls and imports only resolve within one language; files no language
claims are the shared file / text blobs.

A blob is an entry point (route handler, error handler, CLI main, test module)
plus every function only it reaches. Functions reached from several entries
form a hub blob per module. Unreached functions form an orphan blob per module.
Edges: 'main' call, 'error' call (made inside except / abort / raise paths),
'data' (two blobs touch the same data symbol).
"""
import re, hashlib, collections, builtins, time
from tree_sitter import Language, Parser, Query, QueryCursor
import tree_sitter_python as tsp

PY = Language(tsp.language())
parser = Parser(PY)

ROUTE_DECOS = {'route', 'get', 'post', 'put', 'delete', 'patch', 'errorhandler',
               'before_request', 'after_request', 'teardown_request', 'before_app_request',
               'after_app_request', 'teardown_appcontext', 'app_errorhandler', 'cli', 'command'}
ERROR_CALLEES = {'abort', 'raise', 'error', 'exception', 'warning', 'critical', 'fail', 'flash'}
BRANCH_TYPES = {'if_statement', 'elif_clause', 'for_statement', 'while_statement', 'try_statement',
                'except_clause', 'conditional_expression', 'boolean_operator', 'with_statement', 'match_statement'}
SQL = re.compile(r'\b(?:FROM|INTO|UPDATE|TABLE(?: IF NOT EXISTS)?|JOIN)\s+([A-Za-z_][A-Za-z_0-9]*)', re.I)
DATA_ROOTS = {'request', 'session', 'g', 'current_app'}
NOT_SHARED = {'request', 'req'}                  # everyone reads the request; not a shared-state signal
KEY_METHODS = {'get', 'pop', 'setdefault'}       # session.get("uid") -> ctx:session.uid
WHOLE_METHODS = KEY_METHODS | {'clear', 'update', 'keys', 'items', 'values', 'copy', 'popitem'}
# call(..., fn) that makes fn an entry like the matching decorator would
REGISTER_CALLS = {'register_error_handler': 'errorhandler', 'before_request': 'before_request',
                  'after_request': 'after_request', 'teardown_request': 'teardown_request',
                  'before_app_request': 'before_app_request', 'after_app_request': 'after_app_request',
                  'teardown_appcontext': 'teardown_appcontext'}
APP_OBJECT = re.compile(r'(?:\w+\.)*(?:Flask|Quart|FastAPI|Starlette|Sanic|Typer|Celery)\(')
ENTRY_NAMES = {'app', 'main', '__main__', 'wsgi', 'asgi', 'manage', 'run', 'server', 'cli'}
FRAMEWORK = re.compile(r'(?:\w+\.)*(?:Flask|Blueprint|FastAPI|APIRouter|getLogger|SQLAlchemy|LoginManager)\(')
BUILTINS = frozenset(dir(builtins))
# calls on these roots are never the app's own code, imported or not
STDLIB_ROOTS = frozenset({'os', 'sys', 're', 'json', 'time', 'datetime', 'subprocess', 'shutil', 'pathlib', 'logging',
                          'collections', 'itertools', 'functools', 'hashlib', 'random', 'math', 'typing', 'argparse',
                          'threading', 'signal', 'tempfile', 'io', 'glob', 'difflib', 'textwrap', 'string', 'uuid',
                          'sqlite3', 'secrets', 'hmac', 'unittest', 'contextlib', 'dataclasses', 'copy', 'traceback',
                          'urllib', 'http', 'enum', 'asyncio', 'shlex', 'pickle', 'warnings', 'struct', 'socket'})
DOTTED = re.compile(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*')
PATH_LIT = re.compile(r'''[rRbBuU]{0,2}(['"])([\w.@+~-]+(?:/[\w.@+~-]+)*/?)\1''')   # a literal that can name a file
FILE_LIT_IN_TEXT = re.compile(r'''(['"])([^'"\s]{1,200}?)\1''')                     # quoted strings in a template / config
BINARY_EXT = frozenset('png jpg jpeg gif bmp ico webp tiff pdf zip gz tgz bz2 xz 7z tar jar whl so dylib dll exe bin o a '
                       'pyc class woff woff2 ttf otf eot mp3 mp4 wav ogg webm mov avi sqlite sqlite3 db gguf'.split())
TEST_RECEIVERS = frozenset({'client', 'app', 'resp', 'response', 'res'})
STATIC_DIRS = frozenset({'static', 'public', 'assets', 'dist'})
SUBCOMMAND = re.compile(r'''\badd_parser\(\s*[rRuU]?['"]([\w.-]+)['"]''')

def _text(n, src):
    return src[n.start_byte:n.end_byte].decode('utf-8', 'replace')

def _modname(rel):
    m = rel[:-3].replace('/', '.')
    return m[:-9] if m.endswith('.__init__') else m

class Func:
    def __init__(self, module, name, node, src, cls=None):
        self.module, self.name, self.cls = module, name, cls
        self.qual = f'{module}.{cls + "." if cls else ""}{name}'
        self.start, self.end = node.start_point[0] + 1, node.end_point[0] + 1
        self.lines = self.end - self.start + 1
        self.body = _text(node, src)
        self.hash = hashlib.sha1(self.body.encode()).hexdigest()[:10]
        self.decos = []          # (decorator name, args text)
        self.calls = []          # (callee text, is_error, 'call' | 'deferred' (passed as a value) | 'dunder' | 'unplaced')
        self.nested = set()      # names of functions / classes defined inside this one
        self.origins = {}        # local name -> callee text of the first call it was bound from (x = f(), with f() as x)
        self.imports = {}        # imports made inside this function: bound name -> target
        self.deps = []           # modules those imports depend on
        self.pkg = []            # package of the module, for relative imports
        self.self_types = {}     # self.attr -> callee text of the first call assigned to it
        self.cmds = frozenset()  # subcommand names the module registers (add_parser); empty: no dispatch carving
        self.parsers = {}        # local name -> subcommand of the add_parser(...) bound to it
        self.cmd_targets = []    # (subcommand, function text) from .set_defaults(func=f) / {"name": f}
        self.vfuncs = {}         # 'a' / 'a|b' -> Func of the `if args.cmd == "a"` branches carved out of this one
        self.carved = []         # (start byte, end byte, lines) of those branches
        self.direct = []         # (qual, deferred) links that are not calls: dispatch to a carved branch
        self.cmd = None          # the subcommand a carved branch runs (None: shared by several)
        self.strings = []        # string literals that may name a file ('index.html', 'static/app.css')
        self.data = set()
        self.branches = 0
        self.returns_data = False
        self.file = None
        self.errors = 0          # abort / raise / 4xx-return exits
        self.aborts = []         # status code text of each abort(...), None when not a literal
        self.registers = []      # (decorator equivalent, args text, target text) from app.register_*(…, fn)
        self.names = set()       # identifiers read (global candidates)
        self.locals = set()      # identifiers bound locally
        self.declared_global = set()
        self.attr_refs = set()   # (identifier, attr) for module.GLOBAL access
        self.route = None        # set by a Lang for a synthetic handler (JS inline route / middleware / test case)

def parse_module(rel, text):
    src = text.encode('utf-8')
    tree = parser.parse(src)
    mod = _modname(rel)
    info = {'name': mod, 'file': rel, 'imports': {}, 'funcs': [], 'globals': set(), 'classes': set(),
            'blueprints': set(), 'framework': set(), 'lines': text.count('\n') + 1,
            'entry_like': mod.split('.')[-1] in ENTRY_NAMES, 'deps': [], 'global_tables': {}, 'class_attrs': {}}
    pkg = mod.split('.') if rel.endswith('__init__.py') else mod.split('.')[:-1]
    cmds = frozenset(SUBCOMMAND.findall(text))
    def add(f):
        for g in [f] + list(f.vfuncs.values()):
            g.file = rel
            info['funcs'].append(g)
            info['deps'].extend(g.deps)
    root = tree.root_node
    for n in root.children:
        if n.type in ('import_statement', 'import_from_statement'):
            binds, deps = _import_bindings(n, src, pkg)
            info['imports'].update(binds)
            info['deps'].extend(deps)
        elif n.type == 'expression_statement' and n.children and n.children[0].type == 'assignment':
            lhs = n.children[0].child_by_field_name('left')
            rhs = n.children[0].child_by_field_name('right')
            if lhs is not None and lhs.type == 'identifier':
                info['globals'].add(_text(lhs, src))
                if rhs is not None and 'Blueprint(' in _text(rhs, src):
                    info['blueprints'].add(_text(lhs, src))
                if rhs is not None and FRAMEWORK.match(_text(rhs, src)):
                    info['framework'].add(_text(lhs, src))
                if rhs is not None and APP_OBJECT.match(_text(rhs, src)):
                    info['entry_like'] = True
                if rhs is not None and rhs.type in ('string', 'concatenated_string'):
                    tables = {'table:' + t.lower() for t in SQL.findall(_text(rhs, src))}
                    if tables:
                        info['global_tables'][_text(lhs, src)] = tables
        elif n.type == 'if_statement' and re.search(r'__name__\s*==\s*[\'"]__main__', _text(n.child_by_field_name('condition'), src)):
            info['entry_like'] = True
        elif n.type in ('class_definition', 'function_definition', 'decorated_definition'):
            fn = n.named_children[-1] if n.type == 'decorated_definition' else n
            if fn.type == 'class_definition':      # plain or decorated (@dataclass)
                cname = _text(fn.child_by_field_name('name'), src)
                info['classes'].add(cname)
                body = fn.child_by_field_name('body')
                for m in body.named_children if body else []:
                    mf = m if m.type == 'function_definition' else (m.named_children[-1] if m.type == 'decorated_definition' else None)
                    if mf is not None and mf.type == 'function_definition':
                        add(_func(mod, m, mf, src, cname, pkg, cmds))
                    elif m.type == 'expression_statement' and m.named_children and m.named_children[0].type == 'assignment':
                        lhs = m.named_children[0].child_by_field_name('left')    # class-level attr = Cls(...)
                        rhs = m.named_children[0].child_by_field_name('right')
                        if lhs is not None and lhs.type == 'identifier' and rhs is not None and rhs.type == 'call':
                            info['class_attrs'].setdefault(cname, {}).setdefault(
                                _text(lhs, src), _text(rhs.child_by_field_name('function'), src))
            elif fn.type == 'function_definition':
                f = _func(mod, n, fn, src, None, pkg, cmds)
                add(f)
                if f.name in ('main', 'create_app'):
                    info['entry_like'] = True
    # module-level code (if __name__ == '__main__', app = Flask(...), register_blueprint)
    top = Func(mod, '<module>', root, src)
    top.pkg, top.cmds = pkg, cmds
    for n in root.children:
        if n.type not in ('function_definition', 'decorated_definition', 'class_definition', 'import_statement', 'import_from_statement'):
            _walk(n, src, top, False)
    for k, v in top.imports.items():       # imports nested in module-level try / if
        info['imports'].setdefault(k, v)
    stmts = [n for n in root.children if n.type not in ('function_definition', 'decorated_definition', 'class_definition')]
    top.lines = len(stmts)
    # module code only, so editing a function does not count as a change to <module>
    top.body = '\n'.join(_cut(src, n.start_byte, n.end_byte, top.carved) for n in stmts)
    top.hash = hashlib.sha1(top.body.encode()).hexdigest()[:10]
    _finish_branches(top, src)
    top.file = rel
    info['deps'].extend(top.deps)
    for g in top.vfuncs.values():
        g.file = rel
        info['funcs'].append(g)
    info['top'] = top
    return info

def _import_bindings(n, src, pkg):
    """An import / from-import statement -> ([(bound name, target)], [modules it depends on])."""
    binds, deps = [], []
    if n.type == 'import_statement':
        for c in n.named_children:
            if c.type == 'dotted_name':        # `import a.b` binds `a`, depends on a.b
                binds.append((_text(c, src).split('.')[0], _text(c, src).split('.')[0]))
                deps.append(_text(c, src))
            elif c.type == 'aliased_import':
                binds.append((_text(c.child_by_field_name('alias'), src), _text(c.child_by_field_name('name'), src)))
                deps.append(_text(c.child_by_field_name('name'), src))
        return binds, deps
    mn = n.child_by_field_name('module_name')
    base = _text(mn, src) if mn else ''
    if base.startswith('.'):                 # from . / .x / ..x relative to this module's package
        dots = len(base) - len(base.lstrip('.'))
        up = pkg[:len(pkg) - (dots - 1)] if dots - 1 <= len(pkg) else []
        base = '.'.join(up + ([base.lstrip('.')] if base.lstrip('.') else []))
    deps.append(base)
    for c in n.named_children[1:]:
        if c.type == 'dotted_name':
            binds.append((_text(c, src), base + '.' + _text(c, src)))
            deps.append(base + '.' + _text(c, src))
        elif c.type == 'aliased_import':
            binds.append((_text(c.child_by_field_name('alias'), src), base + '.' + _text(c.child_by_field_name('name'), src)))
            deps.append(base + '.' + _text(c.child_by_field_name('name'), src))
        elif c.type == 'wildcard_import':
            binds.append(('*', base))
    return binds, deps

def _cut(src, start, end, carved):
    """Source text of [start, end) without the carved byte ranges inside it."""
    parts, pos = [], start
    for s, e, _ in sorted(carved):
        if s >= start and e <= end:
            parts.append(src[pos:s]); pos = e
    parts.append(src[pos:end])
    return b''.join(parts).decode('utf-8', 'replace')

def _dispatch_names(n, src):
    """`args.cmd == "a"` / `"a" == cmd` / `args.cmd in ("a", "b")`, alone, in an `and` or or-ed -> ['a', 'b']."""
    if n is None:
        return []
    if n.type == 'parenthesized_expression' and n.named_children:
        return _dispatch_names(n.named_children[0], src)
    if n.type == 'boolean_operator':
        a = _dispatch_names(n.child_by_field_name('left'), src)
        b = _dispatch_names(n.child_by_field_name('right'), src)
        op = n.child_by_field_name('operator')
        if op is not None and op.type == 'and':
            return a or b
        return list(dict.fromkeys(a + b)) if a and b else []
    if n.type == 'comparison_operator' and len(n.named_children) == 2:
        ops = [c.type for c in n.children if not c.is_named]
        l, r = n.named_children
        if ops == ['==']:
            for x, y in ((l, r), (r, l)):
                v = _strlit(y, src)
                if v is not None and x.type in ('identifier', 'attribute'):
                    return [v]
        if ops == ['in'] and l.type in ('identifier', 'attribute') and r.type in ('tuple', 'list', 'set'):
            vs = [_strlit(c, src) for c in r.named_children]
            if vs and all(vs):
                return list(dict.fromkeys(vs))
    return []

def _branch_func(f, names, node, src):
    """The Func a dispatch branch `if args.cmd == "a":` is carved into; branches for the same names merge."""
    key = '|'.join(names)
    lines = node.end_point[0] - node.start_point[0] + 1
    vf = f.vfuncs.get(key)
    if vf is None:
        vf = Func(f.module, f'{f.name}:{key}', node, src, f.cls)
        vf.cmd = names[0] if len(names) == 1 else None
        # one Python scope: the branch sees (and binds into) the function's names and imports
        vf.locals, vf.declared_global, vf.imports, vf.origins = f.locals, f.declared_global, f.imports, f.origins
        vf.nested, vf.pkg, vf.self_types, vf.parsers, vf.cmd_targets = f.nested, f.pkg, f.self_types, f.parsers, f.cmd_targets
        vf.node = node
        f.vfuncs[key] = vf
    else:
        vf.body += '\n' + _text(node, src)
        vf.lines += lines
        vf.end = max(vf.end, node.end_point[0] + 1)
    f.carved.append((node.start_byte, node.end_byte, lines))
    return vf

def _finish_branches(f, src):
    """Hash the carved branches; the function dispatches (deferred) to one Func per subcommand, and a branch
    shared by several subcommands (`in ("a", "b")`) is called from each of theirs."""
    for key, vf in list(f.vfuncs.items()):
        if vf.cmd is None:
            for name in key.split('|'):
                if name not in f.vfuncs:      # a subcommand that only appears in shared branches
                    e = _branch_func(f, [name], vf.node, src)
                    f.carved.pop()
                    e.body, e.lines, e.end = '', 0, e.start
                e = f.vfuncs[name]
                e.direct.append((vf.qual, False))
    for vf in f.vfuncs.values():
        vf.hash = hashlib.sha1(vf.body.encode()).hexdigest()[:10]
        if vf.cmd is not None:
            f.direct.append((vf.qual, True))
        vf.__dict__.pop('node', None)

def _func(mod, outer, fn, src, cls=None, pkg=(), cmds=frozenset()):
    f = Func(mod, _text(fn.child_by_field_name('name'), src), outer, src, cls)
    f.pkg, f.cmds = list(pkg), cmds
    if outer.type == 'decorated_definition':
        for d in outer.named_children:
            if d.type == 'decorator':
                dt = _text(d, src)[1:]
                m = re.match(r'([\w.]+)\s*(\((.*)\))?$', dt, re.S)
                if m:
                    f.decos.append((m.group(1), m.group(3) or ''))
    for d, _ in f.decos:
        if d.split('.')[-1] not in ROUTE_DECOS and d not in ('staticmethod', 'classmethod', 'property', 'dataclass'):
            f.calls.append((d, False, 'call'))
    params = fn.child_by_field_name('parameters')
    if params is not None:
        _bind_params(params, src, f)
    _walk(fn.child_by_field_name('body'), src, f, False)
    if f.carved:     # the function's own lines are what is left once its dispatch branches are carved out
        f.body = _cut(src, outer.start_byte, outer.end_byte, f.carved)
        f.lines -= sum(n for _, _, n in f.carved)
        f.hash = hashlib.sha1(f.body.encode()).hexdigest()[:10]
    _finish_branches(f, src)
    return f

def _walk(n, src, f, in_err):
    t = n.type
    if f.cmds and t in ('if_statement', 'elif_clause'):
        cons = n.child_by_field_name('consequence')
        names = _dispatch_names(n.child_by_field_name('condition'), src)
        if cons is not None and names and all(x in f.cmds for x in names):
            f.branches += 1
            vf = _branch_func(f, names, cons, src)
            for c in n.children:
                _walk(c, src, vf if c == cons else f, in_err)
            return
    if t in BRANCH_TYPES:
        f.branches += 1
    err = in_err or t == 'except_clause' or t == 'raise_statement'
    if t == 'raise_statement':
        f.errors += 1
    if t == 'call':
        fn = n.child_by_field_name('function')
        callee = _text(fn, src)
        last = callee.rsplit('.', 1)[-1]
        f.calls.append((callee, err or last in ERROR_CALLEES, 'call'))
        args = n.child_by_field_name('arguments')
        for a in args.named_children if args is not None and args.type == 'argument_list' else ():
            v = a.child_by_field_name('value') if a.type == 'keyword_argument' else a
            if v is not None and v.type in ('identifier', 'attribute'):   # sorted(xs, key=f), Thread(target=f)
                f.calls.append((_text(v, src), err, 'deferred'))
        p = n.parent
        if last[:1].isupper() and p is not None and p.type in ('argument_list', 'keyword_argument', 'lambda', 'return_statement'):
            f.calls.append((callee + '.__call__', err, 'deferred'))   # a callable object handed over: called later
        pos = _positional(n)
        if last == 'set_defaults' and fn.type == 'attribute':           # sub.add_parser("a").set_defaults(func=f)
            obj = fn.child_by_field_name('object')
            cmd = f.parsers.get(_text(obj, src)) if obj.type == 'identifier' else _parser_cmd(obj, src)
            for a in args.named_children if cmd and args is not None else ():
                v = a.child_by_field_name('value') if a.type == 'keyword_argument' else None
                if v is not None and v.type in ('identifier', 'attribute'):
                    f.cmd_targets.append((cmd, _text(v, src)))
        if last == 'abort':
            f.errors += 1
            f.aborts.append(_text(pos[0], src) if pos and pos[0].type == 'integer' else None)
        elif last in REGISTER_CALLS and pos and pos[-1].type in ('identifier', 'attribute'):
            # keep the code / exception class; a loop variable (`for code in CODES`) says nothing
            key = _text(pos[0], src) if len(pos) > 1 else ''
            key = key if pos[0].type == 'integer' or key.rsplit('.', 1)[-1][:1].isupper() else ''
            f.registers.append((REGISTER_CALLS[last], key, _text(pos[-1], src)))
        if last in ('execute', 'executemany', 'executescript'):
            for q in SQL.findall(_text(n, src)):
                f.data.add('table:' + q.lower())
    elif t == 'string':
        txt = _text(n, src)
        for q in SQL.findall(txt):
            f.data.add('table:' + q.lower())
        m = PATH_LIT.fullmatch(txt)
        if m and ('.' in m.group(2) or '/' in m.group(2)):
            f.strings.append(m.group(2))
    elif t == 'dictionary' and f.cmds:                                  # {"a": cmd_a, ...} dispatch table
        for pr in n.named_children:
            k, v = pr.child_by_field_name('key'), pr.child_by_field_name('value')
            if pr.type == 'pair' and _strlit(k, src) in f.cmds and v is not None and v.type in ('identifier', 'attribute'):
                f.cmd_targets.append((_strlit(k, src), _text(v, src)))
                f.calls.append((_text(v, src), in_err, 'deferred'))
    elif t in ('import_statement', 'import_from_statement'):
        binds, deps = _import_bindings(n, src, f.pkg)
        f.imports.update(binds)
        f.deps.extend(deps)
    elif t in ('attribute', 'subscript'):
        obj = n.child_by_field_name('object' if t == 'attribute' else 'value')
        if obj is not None and obj.type == 'identifier':
            b = _text(obj, src)
            if b in DATA_ROOTS:
                f.data.add('ctx:' + _ctx_symbol(n, b, src))
            elif t == 'attribute':
                f.attr_refs.add((b, _text(n.child_by_field_name('attribute'), src)))
    elif t == 'return_statement' and n.named_children:
        v = n.named_children[0]
        if v.type in ('dictionary', 'list', 'call', 'tuple'):
            f.returns_data = True
        if _is_error_return(n, src):
            f.errors += 1
    elif t == 'identifier':
        p = n.parent
        if p is None or not ((p.type == 'attribute' and p.child_by_field_name('attribute') == n) or
                             (p.type == 'keyword_argument' and p.child_by_field_name('name') == n)):
            f.names.add(_text(n, src))
    elif t in ('assignment', 'augmented_assignment', 'for_statement', 'for_in_clause'):
        left = n.child_by_field_name('left')
        _bind(left, src, f)
        right = n.child_by_field_name('right')
        if t == 'assignment' and left is not None and left.type == 'identifier' and right is not None:
            if right.type == 'call':
                f.origins.setdefault(_text(left, src), _text(right.child_by_field_name('function'), src))
                cmd = _parser_cmd(right, src)
                if cmd:
                    f.parsers[_text(left, src)] = cmd
            elif right.type == 'lambda':
                f.nested.add(_text(left, src))
        elif t == 'assignment' and left is not None and left.type == 'attribute' and right is not None and right.type == 'call':
            obj = left.child_by_field_name('object')
            if obj is not None and _text(obj, src) == 'self':                # self.x = Cls(...)
                f.self_types.setdefault(_text(left.child_by_field_name('attribute'), src),
                                        _text(right.child_by_field_name('function'), src))
        if t in ('for_statement', 'for_in_clause'):                         # for x in obj -> obj.__iter__
            base = _callable_base(right, src)
            if base:
                f.calls.append((base + '.__iter__', err, 'dunder'))
    elif t == 'with_item':                                                  # with obj / with Cls() as x
        v = n.child_by_field_name('value')
        expr = v.named_children[0] if v is not None and v.type == 'as_pattern' and v.named_children else v
        base = _callable_base(expr, src)
        if base:
            f.calls += [(base + '.__enter__', err, 'dunder'), (base + '.__exit__', err, 'dunder')]
        if v is not None and v.type == 'as_pattern' and expr is not None and expr.type == 'call':
            tgt = v.child_by_field_name('alias')
            if tgt is not None and tgt.named_children and tgt.named_children[0].type == 'identifier':
                f.origins.setdefault(_text(tgt.named_children[0], src), _text(expr.child_by_field_name('function'), src))
    elif t == 'as_pattern_target':
        _bind(n.named_children[0] if n.named_children else None, src, f)
    elif t in ('parameters', 'lambda_parameters'):
        _bind_params(n, src, f)
    elif t in ('function_definition', 'class_definition') and n.child_by_field_name('name') is not None:
        f.nested.add(_text(n.child_by_field_name('name'), src))
    elif t == 'global_statement':
        f.declared_global.update(_text(c, src) for c in n.named_children if c.type == 'identifier')
    errs = _error_children(n, src) if t == 'block' and n.parent is not None and \
        n.parent.type in ('if_statement', 'elif_clause', 'else_clause') else ()
    for i, c in enumerate(n.children):
        _walk(c, src, f, err or i in errs)

def _callable_base(n, src):
    """obj / self.obj / Cls(...) -> the text a dunder hangs off ('obj', 'self.obj', 'Cls'); None otherwise."""
    if n is None:
        return None
    if n.type in ('identifier', 'attribute'):
        return _text(n, src)
    if n.type == 'call' and n.child_by_field_name('function').type in ('identifier', 'attribute'):
        return _text(n.child_by_field_name('function'), src)
    return None

def _parser_cmd(n, src):
    """sub.add_parser("a", ...) -> 'a'."""
    if n is None or n.type != 'call' or _text(n.child_by_field_name('function'), src).rsplit('.', 1)[-1] != 'add_parser':
        return None
    pos = _positional(n)
    return _strlit(pos[0], src) if pos else None

def _positional(call):
    args = call.child_by_field_name('arguments')
    if args is None or args.type != 'argument_list':
        return []
    return [a for a in args.named_children if a.type not in ('keyword_argument', 'comment', 'dictionary_splat', 'list_splat')]

def _strlit(n, src):
    m = re.fullmatch(r"""[rRbBuU]?(['"])([\w.-]+)\1""", _text(n, src)) if n is not None and n.type == 'string' else None
    return m.group(2) if m else None

def _ctx_symbol(n, root, src):
    """request.form / session["uid"] / session.get("uid") / g.user -> 'session.uid'; session.clear() -> 'session'."""
    if n.type == 'subscript':
        key = _strlit(n.child_by_field_name('subscript'), src)
        return f'{root}.{key}' if key else root
    name = _text(n.child_by_field_name('attribute'), src)
    p = n.parent
    if p is not None and p.type == 'call' and p.child_by_field_name('function') == n and name in WHOLE_METHODS:
        pos = _positional(p)
        key = _strlit(pos[0], src) if pos and name in KEY_METHODS else None
        return f'{root}.{key}' if key else root
    return f'{root}.{name}'

def _bind(n, src, f):
    if n is None:
        return
    if n.type == 'identifier':
        f.locals.add(_text(n, src))
    elif n.type in ('pattern_list', 'tuple_pattern', 'list_pattern', 'tuple', 'list', 'expression_list',
                    'parenthesized_expression', 'list_splat_pattern', 'list_splat'):
        for c in n.named_children:
            _bind(c, src, f)

def _bind_params(params, src, f):
    for c in params.named_children:
        if c.type == 'identifier':
            f.locals.add(_text(c, src))
        elif c.child_by_field_name('name') is not None:
            _bind(c.child_by_field_name('name'), src, f)
        elif c.named_children and c.named_children[0].type == 'identifier':   # typed / splat parameters
            f.locals.add(_text(c.named_children[0], src))

def _is_error_return(n, src):
    """return x, 4xx  /  return (jsonify(...), 5xx)"""
    v = n.named_children[0] if n.named_children else None
    if v is None or v.type not in ('expression_list', 'tuple'):
        return False
    return any(c.type == 'integer' and re.fullmatch(r'[45]\d\d', _text(c, src)) for c in v.named_children[1:])

def _is_error_exit(n, src):
    if n.type == 'raise_statement':
        return True
    if n.type == 'return_statement':
        return _is_error_return(n, src)
    if n.type == 'expression_statement' and n.named_children and n.named_children[0].type == 'call':
        return _text(n.named_children[0].child_by_field_name('function'), src).rsplit('.', 1)[-1] == 'abort'
    return False

def _has_normal_exit(n, src):
    if n.type == 'return_statement':
        return not _is_error_return(n, src)
    if n.type in ('function_definition', 'decorated_definition', 'class_definition', 'lambda'):
        return False
    return any(_has_normal_exit(c, src) for c in n.named_children)

def _error_children(block, src):
    """Children of an if/elif/else block on its error path: when the block ends in abort / raise /
    4xx return, the statements after the last one that can still leave normally. Anything before
    that also runs on the success path, so it is not an error-path call."""
    kids = block.children
    idx = [i for i, c in enumerate(kids) if c.type != 'comment']
    if not idx or not _is_error_exit(kids[idx[-1]], src):
        return set()
    start = max((i + 1 for i in idx[:-1] if _has_normal_exit(kids[i], src)), default=0)
    return {i for i in idx if i >= start}

# ---------------------------------------------------------------- resolution

def load_module(rel, text, cache=None, lang=None):
    """lang.parse_module (Python by default), memoised in `cache` by (path, content hash). Parsed modules are never mutated."""
    lang = lang or PY
    key = (rel, hashlib.sha1(text.encode('utf-8', 'replace')).hexdigest())
    if cache is not None and key in cache:
        return cache[key]
    try:
        info = lang.parse_module(rel, text)
    except Exception as e:  # keep going on a half-written file
        info = {'name': lang.modname(rel), 'file': rel, 'imports': {}, 'funcs': [], 'globals': set(), 'classes': set(),
                'blueprints': set(), 'framework': set(), 'lines': 0, 'top': None, 'entry_like': False,
                'deps': [], 'global_tables': {}, 'error': str(e)}
    if cache is not None:
        cache[key] = info
    return info

def analyze(files, cache=None, memo=None):
    """memo: a dict the caller keeps across the steps of one session (incremental mode). Per function, call
    resolution, data symbols, snippets and file literals are reused from the previous call when nothing they
    looked up changed: every lookup into the cross-module tables (functions, names, classes, aliases, attribute
    types, import graph, module set) is noted as a key, and a function is redone when one of its keys differs
    between the two snapshots or its module was re-parsed. Everything after that (roots, reachability, blobs,
    edges) is recomputed each call, so the result is the same as with memo=None."""
    t_start = time.perf_counter()
    mods, lang_of = {}, {}         # module -> info, module -> its Lang
    old_texts, new_texts = (memo.get('texts', {}), {}) if memo is not None else ({}, None)
    for rel, text in files.items():
        hit = old_texts.get(rel)
        if hit is None or hit[0] is not text:     # the same str object as last call: same parse, no re-hash
            L = lang_for(rel)
            hit = (text, L, load_module(rel, text, cache, L) if L is not None else None)
        if new_texts is not None:
            new_texts[rel] = hit
        _, L, info = hit
        if L is not None:
            mods[info['name']] = info
            lang_of[info['name']] = L
    # JS under static/ public/ assets/ dist/ is a served asset, not code, when another language has the entry
    demoted = set()
    static = [m for m in mods if lang_of[m].name == 'js' and STATIC_DIRS & set(mods[m]['file'].split('/')[:-1])]
    if static:
        rest = {m: v for m, v in mods.items() if lang_of[m].name != 'js'}
        if _entries(rest, lang_of, _import_graph(rest, lang_of), lambda m: lang_of[m].is_test(m, mods[m]['file']), files):
            demoted = {mods[m]['file'] for m in static}
            for m in static:
                del mods[m], lang_of[m]
    test_of = {m: lang_of[m].is_test(m, v['file']) for m, v in mods.items()}
    _is_test = test_of.__getitem__
    t_parsed = time.perf_counter()
    funcs = {}
    for m in mods.values():
        for f in m['funcs']:
            funcs[f.qual] = f
        if m['top']:
            funcs[m['top'].qual] = m['top']
    # name lookups never cross languages
    by_lang = collections.defaultdict(lambda: collections.defaultdict(list))
    for q, f in funcs.items():
        by_lang[lang_of[f.module]][f.name].append(q)
    classes_of = collections.defaultdict(dict)
    for m in mods.values():
        for c in m['classes']:
            classes_of[lang_of[m['name']]][c] = m['name']
    class_keys = {f"{m['name']}.{c}" for m in mods.values() for c in m['classes']}
    ctxs = {L: L.link({m: v for m, v in mods.items() if lang_of[m] is L}, funcs, class_keys)
            for L in dict.fromkeys(lang_of.values())}
    alias = {k: v for c in ctxs.values() for k, v in c.get('alias', {}).items()}   # import target -> what it names
    A = lambda t: alias.get(t, t)
    scope_cache = {}
    rec = None          # incremental mode: the set the lookups below note their keys in (see _env_tables)

    def scope_imports(mod, f=None):
        """Imports visible in f: its module's plus its own (imports inside a function shadow the module's)."""
        if f is None or not f.imports:
            return mods[mod]['imports']
        if f.qual not in scope_cache:
            scope_cache[f.qual] = {**mods[mod]['imports'], **f.imports}
        return scope_cache[f.qual]

    def class_of(mod, text, f=None):
        """'Cls' / 'alias.Cls' / 'pkg.mod.Cls' as seen from mod (and f) -> 'module.Cls' of an app class, else None."""
        parts = text.split('.')
        imp = scope_imports(mod, f)
        classes = classes_of[lang_of[mod]]
        if rec is not None:
            if len(parts) == 1:
                rec.update((('k', f'{mod}.{parts[0]}'), ('c', lang_of[mod], parts[0])))
                if parts[0] in imp:
                    rec.update((('a', imp[parts[0]]), ('k', A(imp[parts[0]]))))
                if '*' in imp:
                    rec.add(('k', f"{imp['*']}.{parts[0]}"))
            elif parts[0] in imp:
                t = imp[parts[0]] + '.' + '.'.join(parts[1:])
                rec.update((('a', t), ('k', A(t))))
        if len(parts) == 1:
            n = parts[0]
            if f'{mod}.{n}' in class_keys:
                return f'{mod}.{n}'
            if n in imp and A(imp[n]) in class_keys:
                return A(imp[n])
            if '*' in imp and f"{imp['*']}.{n}" in class_keys:
                return f"{imp['*']}.{n}"
            return f'{classes[n]}.{n}' if n in classes else None
        if parts[0] in imp:
            cand = A(imp[parts[0]] + '.' + '.'.join(parts[1:]))
            return cand if cand in class_keys else None
        return None

    # self.attr = Cls(...) in any method, or attr = Cls(...) in the class body: self.attr.m() is Cls.m
    attr_types = collections.defaultdict(dict)
    for f in funcs.values():
        for attr, text in f.self_types.items() if f.cls else ():
            ck = class_of(f.module, text, f)
            if ck:
                attr_types[(f.module, f.cls)].setdefault(attr, ck)
    for m in mods.values():
        for cname, attrs in m.get('class_attrs', {}).items():
            for attr, text in attrs.items():
                ck = class_of(m['name'], text)
                if ck:
                    attr_types[(m['name'], cname)].setdefault(attr, ck)
    types_cache = {}

    def local_types(f):
        """Locals bound from an app class constructor (x = Cls(...), with Cls() as x) -> 'module.Cls'."""
        if f.qual not in types_cache:
            origins, top = f.origins, mods[f.module]['top'] if lang_of[f.module].module_types else None
            if top is not None and top is not f:      # JS: a module-level `const s = new Store()` types s everywhere
                origins = {**{n: t for n, t in top.origins.items() if n not in f.locals}, **f.origins}
            types_cache[f.qual] = {n: ck for n, text in origins.items() for ck in [class_of(f.module, text, f)] if ck}
        return types_cache[f.qual]

    def method(ck, name):
        if rec is not None and ck:
            rec.add(('f', f'{ck}.{name}'))
        return f'{ck}.{name}' if ck and f'{ck}.{name}' in funcs else None

    import_graph = {}

    def resolve(mod, callee, f=None, strict=False, ctor=True):
        """callee text as written in f (of module mod) -> qual, or None. strict: no guess by unique method
        name (for values passed around and dunders). ctor: a class name means its __init__ (a call), not
        nothing (a class passed as a value)."""
        parts = callee.split('.')
        imp = scope_imports(mod, f)
        head, tail = parts[0], parts[-1]
        init, by_name, classes = lang_of[mod].ctor, by_lang[lang_of[mod]], classes_of[lang_of[mod]]
        types = local_types(f) if f is not None else {}
        local = f is not None and head in f.locals and head not in f.declared_global and head not in f.imports
        if rec is not None:
            L = lang_of[mod]
            rec.update((('f', f'{mod}.{head}'), ('n', L, tail), ('c', L, head), ('g', mod)))
            if head in imp:
                t = imp[head] + ('.' + '.'.join(parts[1:]) if len(parts) > 1 else '')
                rec.update((('a', imp[head]), ('f', A(imp[head])), ('a', t), ('f', A(t))))
            if '*' in imp:
                rec.add(('f', f"{imp['*']}.{head}"))
            if f is not None and f.cls:
                rec.add(('t', mod, f.cls))
            for q in by_name.get(tail, ()):        # the candidates' class and module, and whether that is a test
                rec.update((('f', q), ('M', funcs[q].module)))
        if len(parts) == 1:
            if head in types:                       # st = Style(...); st(...) -> Style.__call__
                return method(types[head], '__call__')
            if head == 'cls' and f is not None and f.cls:
                return method(f'{mod}.{f.cls}', init) if ctor else None
            if local:
                return None
            if f'{mod}.{head}' in funcs:
                return f'{mod}.{head}'
            if head in imp and A(imp[head]) in funcs:
                return A(imp[head])
            ck = class_of(mod, head, f)
            if ck:
                return method(ck, init) if ctor else None
            if '*' in imp and f"{imp['*']}.{head}" in funcs:
                return f"{imp['*']}.{head}"
            return None
        if not local and head in imp:
            cand = A(imp[head] + '.' + '.'.join(parts[1:]))
            if cand in funcs:
                return cand
            ck = class_of(mod, callee, f)       # sources.Progress(...) -> Progress.__init__
            if ck:
                return method(ck, init) if ctor else None
        if not local:                            # Cls.m / mod.Cls.m / Cls.__enter__
            ck = class_of(mod, '.'.join(parts[:-1]), f)
            if ck and method(ck, tail):
                return method(ck, tail)
        if head == 'self' and f is not None and f.cls:
            if len(parts) == 2 and method(f'{mod}.{f.cls}', tail):
                return method(f'{mod}.{f.cls}', tail)
            ck = attr_types.get((mod, f.cls), {}).get(parts[1])
            if ck and len(parts) <= 3:          # self.x(...) -> X.__call__, self.x.m() -> X.m
                return method(ck, '__call__' if len(parts) == 2 else tail)
        if len(parts) == 2 and head in types:
            return method(types[head], tail)
        if head == 'self' or head in classes:
            cls = classes.get(head)
            for q in by_name.get(tail, []):
                if funcs[q].cls and (head == 'self' and funcs[q].module == mod or funcs[q].cls == head and funcs[q].module == cls):
                    return q
        if strict:
            return None
        # method on an object whose class we know by name (User.load, note.save)
        for q in by_name.get(tail, []):
            fm = funcs[q].module
            if funcs[q].cls and len(by_name[tail]) == 1 and not _is_test(fm) and (fm == mod or fm in import_graph.get(mod, ())):
                return q
        return None

    # entry points
    # incremental: a module's row only changes with its own deps (a re-parse) or with the set of modules
    old_graph = memo.get('graph') if memo is not None else None
    if old_graph is not None and old_graph[0] == lang_of:
        redo = {m: v for m, v in mods.items() if old_graph[1].get(m, (None,))[0] is not v}
        rows = _import_graph(redo, lang_of, mods)
        import_graph.update({m: rows[m] if m in rows else old_graph[1][m][1] for m in mods})
    else:
        import_graph.update(_import_graph(mods, lang_of))
    callgraph = collections.defaultdict(list)   # qual -> [(qual, is_error, deferred)]
    extra_decos = collections.defaultdict(list) # qual -> decorators implied by app.register_*(…, fn)
    cmd_names = collections.defaultdict(list)   # qual -> subcommands it runs (add_parser + set_defaults / dict / if)
    n_calls = n_unresolved = 0
    shown_calls = {}                            # qual -> its calls as the output's dicts (shared, read-only)
    mod_prefixes = {m[:i] for m in mods for i, ch in enumerate(m) if ch == '.'} | set(mods)   # every module and its dotted prefixes

    def resolve_func(q, f):
        """f's links: ([(callee qual, is_error, deferred)], placed or plausible calls, unresolved of those,
        [(registered fn, decorator, args)], [(fn, subcommand)], the first list as the output's call dicts)."""
        calls, nc, nu, regs, cmds = [], 0, 0, [], []
        L = lang_of[f.module]
        seen = rec is not None
        um, up, un = (_Seen(mods, ('M',), rec), _Seen(mod_prefixes, ('p',), rec), _Seen(by_lang[L], ('n', L), rec)) if seen \
            else (mods, mod_prefixes, by_lang[L])
        for callee, err, how in f.calls:
            if how == 'unplaced':   # a call no language can place (JS app[verb](…)): unresolved, nothing to look up or record
                nc += 1
                nu += 1
                continue
            r = resolve(f.module, callee, f, strict=how != 'call', ctor=how != 'deferred')
            if how == 'call':   # only calls that can be the app's own: placed ones and plausible misses
                miss = r is None and _unplaced(callee, f, scope_imports(f.module, f), um, up, un, class_of,
                                               L.builtins, L.stdlib, _is_test(f.module))
                # an import the language's link() aliased to `ext:` (C: a missing library header declares it)
                miss = miss and not A(scope_imports(f.module, f).get(callee.split('.')[0], '')).startswith('ext:')
                nc += r is not None or miss
                nu += miss
            if r and r != q:
                calls.append((r, err, how == 'deferred'))
        for r, deferred in f.direct:
            if seen:
                rec.add(('f', r))
            if r in funcs:
                calls.append((r, False, deferred))
        for deco, args, target in f.registers:
            r = resolve(f.module, target, f, strict=True, ctor=False)
            if r:
                regs.append((r, deco, args))
        for name, target in f.cmd_targets:
            r = resolve(f.module, target, f, strict=True, ctor=False)
            if r:
                cmds.append((r, name))
        shown = [dict({'to': r, 'error': e}, **({'deferred': True} if d else {})) for r, e, d in calls]   # as written out
        return calls, nc, nu, regs, cmds, shown

    # incremental: which keys differ from the previous call's snapshot (memo['env']); a function is redone
    # when its module was re-parsed (a new Func object) or one of the keys it looked up is among them
    if memo is not None:
        env = _env_tables(mods, funcs, by_lang, classes_of, class_keys, alias, attr_types, import_graph, mod_prefixes)
        changed = _env_changes(memo.get('env'), env)
        old_res, old_fd, old_snip = memo.get('res', {}), memo.get('fdata', {}), memo.get('snip', {})
        new_res, new_fd, new_snip = {}, {}, {}
        valid = lambda hit: hit is not None and (changed is None or hit[0].isdisjoint(changed))
    for q, f in funcs.items():
        if memo is None:
            out = resolve_func(q, f)
        else:
            hit = old_res.get(f)
            if valid(hit):
                out = hit[1]
            else:
                rec = {('M', f.module)}
                out = resolve_func(q, f)
                hit = (frozenset(rec), out)
                rec = None
            new_res[f] = hit
        calls, nc, nu, regs, cmds, shown_calls[q] = out
        n_calls += nc
        n_unresolved += nu
        if calls:
            callgraph[q].extend(calls)
        for r, deco, args in regs:
            extra_decos[r].append((deco, args))
        if f.cmd is not None:
            cmd_names[q].append(f.cmd)
        for r, name in cmds:
            if name not in cmd_names[r]:
                cmd_names[r].append(name)
    if memo is None:
        fdata = {q: sorted(f.data | _globals_used(f, mods)) for q, f in funcs.items()}
    else:
        fdata = {}
        for q, f in funcs.items():
            hit = old_fd.get(f)
            if not valid(hit):
                seen = {('M', f.module)}
                d = sorted(f.data | _globals_used(f, _Seen(mods, ('M',), seen)))
                hit = (frozenset(seen), d)
            new_fd[f] = hit
            fdata[q] = hit[1]

    entry_mods = _entries(mods, lang_of, import_graph, _is_test, files)
    entry_mod = entry_mods[0] if entry_mods else None
    entry_set = set(entry_mods)

    roots = []   # (blob_id, name, kind, [quals])
    test_mods = collections.defaultdict(list)
    cmd_quals = collections.defaultdict(list)   # subcommand -> every function / carved branch that runs it
    for q, names in cmd_names.items():
        for name in names:
            cmd_quals[name].append(q)
    for q, f in funcs.items():
        if _is_test(f.module):
            if f.name != '<module>':
                test_mods[f.module].append(q)
            continue
        route = lang_of[f.module].route_of(f, extra_decos.get(q, ()), ctxs[lang_of[f.module]])
        if route:
            roots.append((q, route[0], route[1], [q]))
        elif q in cmd_names:      # one root per subcommand, id = the first place that runs it
            roots.extend((q, name, 'command', cmd_quals[name]) for name in cmd_names[q] if cmd_quals[name][0] == q)
        elif f.name == '<module>' and f.module in entry_set:
            roots.append((q, f.module, 'entry', [q]))
        elif f.name in ('main', 'create_app') and f.module in entry_set:
            roots.append((q, f.name, 'entry', [q]))
    for m, qs in test_mods.items():
        roots.append((m, m.split('.')[-1], 'tests', qs))
    # the entry module's <module> code that calls create_app()/main() is one entry with them
    for em in entry_mods:
        top_id = f'{em}.<module>'
        top_root = next((r for r in roots if r[0] == top_id), None)
        if top_root:
            seen, stack = {top_id}, [top_id]
            while stack:
                for r, *_ in callgraph.get(stack.pop(), []):
                    if r not in seen:
                        seen.add(r); stack.append(r)
            merged = [r for r in roots if r[2] == 'entry' and r[0] != top_id and r[0] in seen]
            roots = [r for r in roots if r not in merged]
            top_root[3].extend(q for r in merged for q in r[3])
    depths = {}     # a language with split_entry (C) cuts a big entry at main's callees: command roots
    for em in entry_mods:
        top_root = next((r for r in roots if r[0] == f'{em}.<module>'), None)
        if top_root and hasattr(lang_of[em], 'split_entry'):
            cmds, depths[top_root[0]] = lang_of[em].split_entry(top_root, roots, callgraph, funcs, _is_test)
            roots.extend(cmds)
    n_roots = collections.Counter(q for _, _, kind, starts in roots if kind != 'tests' for q in starts)
    own = {q: bid for bid, _, kind, starts in roots if kind != 'tests' for q in starts if n_roots[q] == 1}

    # reachability: which roots reach each function (error-path status = any path through error).
    # Another root's own function is where a walk stops: main() handing a handler to set_defaults or
    # create_app() registering an error handler does not make the handler's code shared.
    reach = collections.defaultdict(set)
    for bid, _, kind, starts in roots:
        if kind == 'tests':
            continue
        stack = list(starts); seen = set(starts)
        while stack:
            q = stack.pop()
            reach[q].add(bid)
            for r, *_ in callgraph.get(q, []):
                if r not in seen and own.get(r, bid) == bid:
                    seen.add(r); stack.append(r)
    def snippet(f):
        if memo is None:
            return lang_of[f.module].snippet(f)
        hit = old_snip.get(f)
        if not valid(hit):
            hit = (frozenset({('M', f.module)}), lang_of[f.module].snippet(f))
        new_snip[f] = hit
        return hit[1]

    blob_of = {}
    for q in funcs:
        if _is_test(funcs[q].module):
            blob_of[q] = funcs[q].module
            continue
        if funcs[q].name == '<module>' and funcs[q].module not in entry_set:
            continue
        rs = reach.get(q, set())
        if q in own:          # a root's own function never leaves its blob
            blob_of[q] = own[q]
        elif len(rs) == 1:
            blob_of[q] = next(iter(rs))
        elif len(rs) > 1:
            blob_of[q] = 'hub:' + funcs[q].module
        else:
            blob_of[q] = 'orphan:' + funcs[q].module
    # a module's <module> code that nobody reaches is setup code, not an orphan
    blobs = collections.OrderedDict()
    for bid, name, kind, _ in roots:
        blobs[bid] = {'id': bid, 'name': name, 'kind': kind, 'funcs': []}
        if depths.get(bid):
            blobs[bid]['depth'] = depths[bid]
    for q, bid in blob_of.items():
        if bid not in blobs:
            kind, mod = bid.split(':', 1) if ':' in bid else ('tests', bid)   # test module with no tests yet
            blobs[bid] = {'id': bid, 'name': mod.split('.')[-1], 'kind': kind, 'funcs': []}
        f = funcs[q]
        if f.name == '<module>' and (f.lines == 0 or f.module not in entry_set):
            continue
        blobs[bid]['funcs'].append({
            'qual': q, 'name': f.name if not f.cls else f'{f.cls}.{f.name}', 'file': f.file, 'start': f.start, 'end': f.end,
            'lines': f.lines, 'branches': f.branches, 'errors': f.errors, 'hash': f.hash, 'data': fdata[q],
            'decos': [d for d, _ in f.decos], 'snippet': snippet(f),
            'calls': shown_calls[q],
        })
    for b in blobs.values():
        # a root blob belongs to its root's module, a hub/orphan/tests blob to the module in its id
        b['module'] = funcs[b['id']].module if b['id'] in funcs else b['id'].split(':', 1)[-1]
        b['size'] = sum(f['lines'] for f in b['funcs'])
        b['complexity'] = sum(f['branches'] for f in b['funcs'])
        b['errors'] = sum(f['errors'] for f in b['funcs'])
        b['data'] = sorted({d for f in b['funcs'] for d in f['data']})
        b['emoji'] = _emoji(b)
    blobs = collections.OrderedDict((k, v) for k, v in blobs.items() if v['funcs'])

    # non-code files: `file` when a code literal (or, one hop on, a literal in such a file) names it, else `text`
    others = sorted(rel for rel in files if lang_for(rel) is None or rel in demoted)
    refs = collections.defaultdict(list)          # (blob id, path) -> literals naming it
    frefs = collections.defaultdict(list)         # qual -> paths its own literals name
    # incremental: a function's literal -> path matches and a file's one-hop literals hold while the set of
    # non-code files does not change (and, for a file, its text)
    keep = memo is not None and memo.get('others') == others
    old_match, old_hop = (memo.get('match', {}), memo.get('hop', {})) if keep else ({}, {})
    new_match, new_hop = {}, {}
    def matches(f):
        if memo is None:
            return [(lit, _match_path(lit, index)) for lit in f.strings]
        hit = old_match.get(f)
        if hit is None:
            hit = [(lit, _match_path(lit, index)) for lit in f.strings]
        new_match[f] = hit
        return hit
    if others:
        index = _path_index(others)
        for q, f in funcs.items():
            a = blob_of.get(q)
            L = lang_of[f.module]
            for lit, p in matches(f) + list(L.extra_refs(f, others, ctxs[L])) \
                    if a in blobs else ():
                if p:
                    refs[(a, p)].append(lit)
                    if p not in frefs[q]:
                        frefs[q].append(p)
    for b in blobs.values():
        for fd in b['funcs']:
            fd['refs'] = sorted(frefs.get(fd['qual'], ()))
    for p in sorted({p for _, p in refs}):          # one hop on: files named by the files code references
        if not _is_binary(p, files[p]):
            hit = old_hop.get(p)
            if hit is None or hit[0] != files[p]:
                hit = (files[p], [(lit, _match_path(lit, index)) for lit in (m.group(2) for m in FILE_LIT_IN_TEXT.finditer(files[p]))])
            if memo is not None:
                new_hop[p] = hit
            for lit, t in hit[1]:
                if t and t != p:
                    refs[('file:' + p, t)].append(lit)
    referenced = {p for _, p in refs}
    for p in others:
        binary = _is_binary(p, files[p])
        kind = 'file' if p in referenced else 'text'
        blobs['file:' + p] = {'id': 'file:' + p, 'name': p.rsplit('/', 1)[-1], 'kind': kind,
                              'module': p.split('/', 1)[0] if '/' in p else '.', 'path': p,
                              'size': 0 if binary else len(files[p].splitlines()), 'complexity': 0, 'errors': 0,
                              'data': [], 'funcs': [], 'emoji': '📄' if kind == 'file' else '📃'}

    edges = collections.Counter()
    for q, calls in callgraph.items():
        a = blob_of.get(q)
        for r, err, _ in calls:
            b = blob_of.get(r)
            if a and b and a != b and a in blobs and b in blobs:
                edges[(a, b, 'error' if err else 'main')] += 1
    # abort(code) leaves through the app's error handlers: an error edge per abort to each handler for that code
    handlers = [(bid, _status_code(args)) for bid, _, kind, _ in roots if kind == 'error-handler' and bid in blobs
                for args in [next((a for d, a in list(funcs[bid].decos) + extra_decos.get(bid, [])
                                   if d.split('.')[-1] in ('errorhandler', 'app_errorhandler')), '')]]
    for q, f in funcs.items():
        a = blob_of.get(q)
        if a not in blobs:
            continue
        for code in f.aborts:
            for h, hcode in handlers:
                if h != a and lang_of[funcs[h].module] is lang_of[f.module] and \
                        (code is None or hcode is None or int(code) == hcode):
                    edges[(a, h, 'error')] += 1
    data_index = collections.defaultdict(set)
    for b in blobs.values():
        for d in b['data']:
            kind, _, sym = d.partition(':')
            if kind == 'ctx' and sym.split('.')[0] in NOT_SHARED:
                continue   # everyone reads the request; not a shared-state signal
            data_index[d].add(b['id'])
    data_edges = collections.defaultdict(set)
    def link(xs, ys, via):
        for x in xs:
            for y in ys:
                if x != y:
                    data_edges[tuple(sorted((x, y)))].add(via)
    data_index = {d: sorted(bs) for d, bs in sorted(data_index.items())}
    for d, bs in data_index.items():
        link(bs, bs, d)
        if d.startswith('ctx:') and '.' not in d:   # session.clear() touches every session key
            for d2, bs2 in data_index.items():
                if d2.startswith(d + '.'):
                    link(bs, bs2, d2)
    order = {'main': 0, 'error': 1, 'data': 2, 'ref': 3}
    out_edges = [{'a': a, 'b': b, 'kind': k, 'n': n} for (a, b, k), n in edges.items()]
    out_edges += [{'a': a, 'b': b, 'kind': 'data', 'n': len(ds), 'via': sorted(ds)} for (a, b), ds in data_edges.items()]
    out_edges += [{'a': a, 'b': 'file:' + p, 'kind': 'ref', 'n': len(ls), 'via': sorted(set(ls))} for (a, p), ls in refs.items()]
    out_edges.sort(key=lambda e: (order[e['kind']], e['a'], e['b']))
    if memo is not None:      # only now: a call that raised above leaves the memo as it was
        memo.update(env=env, res=new_res, fdata=new_fd, snip=new_snip, others=others, match=new_match, hop=new_hop,
                    texts=new_texts, graph=(dict(lang_of), {m: (v, import_graph[m]) for m, v in mods.items()}))
    t_end = time.perf_counter()
    stats = {'calls': n_calls, 'unresolved': n_unresolved, 'parse_ms': round((t_parsed - t_start) * 1000, 1),
             'analyze_ms': round((t_end - t_parsed) * 1000, 1)}
    return {'entry': entry_mod, 'blobs': list(blobs.values()), 'edges': out_edges, 'stats': stats,
            'bodies': {f['qual']: funcs[f['qual']].body for b in blobs.values() for f in b['funcs']},
            'modules': {m: {'file': v['file'], 'lines': v['lines'], 'imports': sorted(import_graph[m]), 'error': v.get('error')} for m, v in mods.items()}}

class _Seen:
    """A read-only view of a dict / set that notes each key looked up as tag + (key,) in `out`."""
    __slots__ = ('d', 'tag', 'out')

    def __init__(self, d, tag, out):
        self.d, self.tag, self.out = d, tag, out

    def __contains__(self, k):
        self.out.add(self.tag + (k,))
        return k in self.d

    def get(self, k, default=None):
        self.out.add(self.tag + (k,))
        return self.d.get(k, default)

_MISSING = object()

def _env_tables(mods, funcs, by_lang, classes_of, class_keys, alias, attr_types, import_graph, mod_prefixes):
    """The cross-module tables call resolution and data symbols read, as {tag: {key: value}} (or a set);
    a lookup noted as (tag, *key) depends on exactly that entry. 'M' compares module infos by identity
    (the parse cache hands back the same object while a file's text is unchanged)."""
    return {'M': dict(mods), 'f': {q: (f.module, f.cls) for q, f in funcs.items()},
            'n': {(L, n): tuple(qs) for L, d in by_lang.items() for n, qs in d.items()},
            'c': {(L, n): m for L, d in classes_of.items() for n, m in d.items()},
            'k': set(class_keys), 'a': dict(alias), 't': {k: dict(v) for k, v in attr_types.items()},
            'g': {m: set(v) for m, v in import_graph.items()}, 'p': set(mod_prefixes)}

def _env_changes(old, new):
    """The keys (tag, *key) whose entry differs between two _env_tables results; None when there is no old one."""
    if old is None:
        return None
    out = set()
    for tag, nd in new.items():
        od = old[tag]
        if isinstance(nd, set):
            out.update((tag, k) for k in od ^ nd)
            continue
        same = (lambda x, y: x is y) if tag == 'M' else (lambda x, y: x == y)
        for k in od.keys() | nd.keys():
            if not same(od.get(k, _MISSING), nd.get(k, _MISSING)):
                out.add((tag,) + k if isinstance(k, tuple) else (tag, k))
    return out

def _import_graph(mods, lang_of, universe=None):
    """module -> modules it imports (same language only), for the modules in `mods`; import targets are looked
    up in `universe` (default: mods)."""
    universe = mods if universe is None else universe
    graph = {m: set() for m in mods}
    for m in mods.values():
        for tgt in m['deps']:
            parts = tgt.split('.')   # longest prefix that is a module (of the same language): aic.anchors, not the aic package
            t = next(('.'.join(parts[:i]) for i in range(len(parts), 0, -1)
                      if '.'.join(parts[:i]) in universe and lang_of['.'.join(parts[:i])] is lang_of[m['name']]), None)
            if t and t != m['name']:
                graph[m['name']].add(t)
    return graph

def _entries(mods, lang_of, graph, is_test, files):
    """One entry module per language at most (Lang.entry), in order of the languages' first module."""
    imported = {t for m, s in graph.items() if not is_test(m) for t in s}
    def closure(m, seen=None):
        seen = seen or set()
        for t in graph[m]:
            if t not in seen:
                seen.add(t); closure(t, seen)
        return seen
    langs = dict.fromkeys(lang_of[m] for m in mods)
    return [e for L in langs for e in [L.entry([m for m in mods if lang_of[m] is L], mods, imported, closure, is_test, files)]
            if e is not None]

def _globals_used(f, mods):
    """Module globals a function reads or writes: own globals by name, imported ones by name or module.NAME."""
    m = mods.get(f.module)
    if not m:
        return set()
    local = f.locals - f.declared_global
    imp = m['imports']
    out = set()
    def add(mod, name):
        mm = mods.get(mod)
        if mm and name in mm['globals'] and name not in mm['framework'] and not name.startswith('__'):
            out.add(f'global:{mod}.{name}')
            out.update(mm['global_tables'].get(name, ()))   # init_db runs db.SCHEMA
    for name in f.names - local:
        if name in m['globals']:
            add(f.module, name)
        elif name in imp and '.' in imp[name]:
            add(*imp[name].rsplit('.', 1))
    for obj, attr in f.attr_refs:
        if obj not in local and obj in imp:
            add(imp[obj], attr)
    return out

def _is_binary(rel, text):
    return rel.rsplit('.', 1)[-1].lower() in BINARY_EXT or '\x00' in text

def _path_index(paths):
    """(set of paths, path with a templates/ or static/ prefix removed -> [paths], basename -> [paths])"""
    stripped, base = collections.defaultdict(list), collections.defaultdict(list)
    for p in paths:
        parts = p.split('/')
        base[parts[-1]].append(p)
        for i, part in enumerate(parts[:-1]):
            if part in ('templates', 'static'):
                stripped['/'.join(parts[i + 1:])].append(p)
    return set(paths), stripped, base

def _match_path(lit, index):
    """A literal -> the snapshot file it names: the path itself, the path below templates/ or static/,
    or a basename (with an extension) that only one file has; a literal with directories must then
    also be a tail of that file's path ('notes/form.html', not '.aic/docs/form.html')."""
    paths, stripped, base = index
    lit = lit[2:] if lit.startswith('./') else lit.lstrip('/')
    if not lit or not re.fullmatch(r'[\w.@+~-]+(?:/[\w.@+~-]+)*', lit):
        return None
    if lit in paths:
        return lit
    if len(stripped.get(lit, ())) == 1:
        return stripped[lit][0]
    b = lit.rsplit('/', 1)[-1]
    if '.' in b.strip('.') and len(base.get(b, ())) == 1 and ('/' not in lit or base[b][0].endswith('/' + lit)):
        return base[b][0]
    return None

def _unplaced(callee, f, imp, mods, mod_prefixes, by_name, class_of, builtins=BUILTINS, stdlib=STDLIB_ROOTS,
              in_test=False):
    """A call the resolver could not place that could still be the app's own code: a bare name that is not a
    builtin, nested def or app class (a parameter, a callable local, a missing import), or a method call whose
    name the app defines on an object that may be the app's (not a stdlib / third-party import or a value
    built by one). imp: the imports in scope at the call; mod_prefixes: mods and all their dotted prefixes.
    A method on a call result or on a local bound from a call is the app's only when that call is an app class
    constructor (`get_db().execute`, `conn = get_db(); conn.execute` are not: return types are not tracked);
    in a test module, methods on a client / app / resp parameter are the test client's."""
    if callee.startswith('super()'):
        return False
    rc = re.fullmatch(r'([A-Za-z_][\w.]*)\(.*\)\.([A-Za-z_]\w*)', callee, re.S)
    if rc:                                    # get_db().execute, Store().get
        return bool(class_of(f.module, rc.group(1), f)) and rc.group(2) in by_name
    if not DOTTED.fullmatch(callee):          # foo()(), x[0](), "".join
        tail = re.search(r'\.([A-Za-z_]\w*)$', callee)
        return bool(tail) and callee[:1] not in '"\'' and tail.group(1) in by_name
    parts = callee.split('.')
    head = parts[0]
    def ours(tgt):                             # tgt is a module, a prefix of one, or inside one
        return tgt in mod_prefixes or any(tgt[:i] in mods for i, ch in enumerate(tgt) if ch == '.')
    if head in ('self', 'cls') and len(parts) == 1:
        return False                           # self(...) / cls(...): the own class
    if class_of(f.module, callee, f):
        return False                           # a class: its constructor
    if head in f.locals and head not in f.declared_global and head not in f.imports:
        if len(parts) == 1:
            return head not in f.nested        # calling a parameter / callable local (a lambda is placed)
        o = f.origins.get(head)
        if o is not None and not class_of(f.module, o, f):
            return False                       # bound from a call that is not an app constructor
        if o is None and in_test and head in TEST_RECEIVERS:
            return False                       # the test client / app / response a fixture hands over
        return parts[-1] in by_name
    if head in imp:                            # imported: ours only when the target is in the snapshot
        if not ours(imp[head]):
            return False
        if len(parts) <= 2:
            return True                        # module.func that is not there
        return bool(class_of(f.module, '.'.join(parts[:2]), f))   # module.Class.method, not module.GLOBAL.method
    if head in stdlib:
        return False
    if len(parts) == 1:
        return head not in builtins and head not in f.nested
    return parts[-1] in by_name

def _status_code(args):
    m = re.fullmatch(r'\s*([45]\d\d)\s*', args or '')
    return int(m.group(1)) if m else None

def _is_test(mod):
    last = mod.split('.')[-1]
    return last.startswith('test_') or last.endswith('_test') or bool({'tests', 'test'} & set(mod.split('.')))

def _route_of(f, extra=()):
    for d, args in list(f.decos) + list(extra):
        last = d.split('.')[-1]
        if last in ROUTE_DECOS:
            m = re.search(r'''['"]([^'"]*)['"]''', args)
            if last in ('errorhandler', 'app_errorhandler'):
                code = _status_code(args)
                return (f'error {code if code else args.strip() or "handler"}', 'error-handler')
            if last.startswith(('before_', 'after_', 'teardown_')):
                return (last.replace('_app_', ' ').replace('_', ' '), 'hook')
            if last in ('cli', 'command'):    # @cli.command("name") / name="name"; click's default: init_db -> init-db
                n = re.match(r'''\s*[rRuU]?['"]([^'"]+)['"]''', args) or re.search(r'''\bname\s*=\s*['"]([^'"]+)['"]''', args)
                return (n.group(1) if n else f.name.replace('_', '-'), 'command')
            path = m.group(1) if m else '/' + f.name
            mm = re.search(r"methods\s*=\s*[\[(]([^\])]*)", args)
            meth = [x.strip(" '\"") for x in mm.group(1).split(',')] if mm else ([last.upper()] if last in ('get', 'post', 'put', 'delete', 'patch') else [])
            meth = [x for x in meth if x and x != 'GET']
            return ((' '.join(meth) + ' ' if meth else '') + path, 'route')
    return None

SNIP_EXIT = re.compile(r'\b(abort|raise|redirect|render_template|render|jsonify|execute|executemany|executescript)\b'
                       r'|\breturn\b.*,\s*[45]\d\d\s*$')
SNIP_REQUEST_ASSIGN = re.compile(r'^[\w.,\s\[\]\'"]+=\s*request\.')

def _snippet(f):
    """Up to 3 lines in source order: exits and effects first (abort, raise, status returns, redirect,
    render, jsonify, execute), then other returns, then the rest minus imports and plain reads of the request."""
    lines, in_doc, seen_def = [], None, f.name == '<module>'
    for raw in f.body.splitlines():
        l = raw.strip()
        if in_doc:
            in_doc = None if in_doc in l else in_doc
            continue
        if l[:3] in ('"""', "'''") and not (len(l) > 5 and l.endswith(l[:3])):
            in_doc = l[:3]
            continue
        if not l or l.startswith(('#', '@')) or l[:3] in ('"""', "'''") or re.match(r'if\s+__name__\s*==', l):
            continue
        if not seen_def and re.match(r'(async\s+)?def\b', l):
            seen_def = True
            continue
        lines.append(l)
    def tier(l):
        if SNIP_EXIT.search(l):
            return 0
        if re.match(r'return\b', l):
            return 1
        return 3 if SNIP_REQUEST_ASSIGN.match(l) or re.match(r'(import|from)\s', l) else 2
    best = sorted(range(len(lines)), key=lambda i: (tier(lines[i]), i))
    return [lines[i] for i in sorted(i for i in best[:3] if tier(lines[i]) < 3)]

EMOJI = [
    (r'register|signup', '🪪'), (r'logout', '🚪'), (r'login|signin|auth|password|token|session', '🔐'),
    (r'error|404|403|500|abort', '💥'), (r'share', '🔗'), (r'api', '🧩'), (r'note|post|item|todo', '📝'),
    (r'db|database|schema|sql|connect', '🗄️'), (r'model|user|dataclass', '👤'), (r'test', '🧪'),
    (r'index|home|^/$', '🏠'), (r'rate|limit', '⏱️'), (r'app|create_app|entry|main', '⚙️'),
]
def _emoji(b):
    if b['kind'] == 'tests': return '🧪'
    if b['kind'] == 'error-handler': return '💥'
    if b['kind'] == 'entry': return '⚙️'
    if b['kind'] == 'hook': return '🪝'
    key = f"{b['name']} {b['module']} {b['kind']} {' '.join(f['name'] for f in b['funcs'][:5])}".lower()
    for pat, e in EMOJI:
        if re.search(pat, key):
            return e
    return '🔹'

# ---------------------------------------------------------------- languages

class PyLang:
    """The hooks analyze() calls per module language; lang_js.JSLang implements the same ones."""
    name, ctor, module_types = 'py', '__init__', False
    builtins, stdlib = BUILTINS, STDLIB_ROOTS

    def handles(self, rel):
        return rel.endswith('.py')

    def modname(self, rel):
        return _modname(rel)

    def parse_module(self, rel, text):
        return parse_module(rel, text)

    def is_test(self, mod, rel):
        return _is_test(mod)

    def link(self, mods, funcs, class_keys):
        """Per-analysis context for the other hooks; 'alias': import target -> the qual it names."""
        return {}

    def entry(self, ms, mods, imported, closure, is_test, files):
        """A half-built app has no entry yet: the candidate must look like one (__main__ guard, app object,
        create_app/main, entry-style name) and, with itself, cover at least half of the app's modules."""
        cands = [m for m in ms if m not in imported and not is_test(m) and mods[m]['entry_like']]
        e = max(cands, key=lambda m: (len(closure(m)), m in ('app', 'main', '__main__')), default=None)
        if e is not None and len(closure(e) | {e}) < sum(not is_test(m) for m in ms) / 2:
            e = None
        return e

    def route_of(self, f, extra, ctx):
        return _route_of(f, extra)

    def snippet(self, f):
        return _snippet(f)

    def extra_refs(self, f, others, ctx):
        """(literal, snapshot path) pairs beyond the plain string literals in f.strings."""
        return ()

PY = PyLang()
LANGS = [PY]
try:
    import lang_js
    LANGS.append(lang_js.JSLang(Func))
except ImportError:      # no tree-sitter-javascript: Python only, .js files stay file / text blobs
    pass
try:
    import lang_c
    LANGS.append(lang_c.CLang(Func))
except ImportError:      # no tree-sitter-c: .c / .h files stay file / text blobs
    pass
try:
    import lang_go
    LANGS.append(lang_go.GoLang(Func))
except ImportError:      # no tree-sitter-go: .go files and go.mod stay file / text blobs
    pass

def lang_for(rel):
    return next((L for L in LANGS if L.handles(rel)), None)

if __name__ == '__main__':
    import sys, os, json
    root = sys.argv[1]
    files = {}
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if not d.startswith(('.', '__'))]
        for n in fn:
            p = os.path.join(dp, n)
            with open(p, 'rb') as fh:
                files[os.path.relpath(p, root)] = fh.read().decode('utf-8', 'replace')
    r = analyze(dict(sorted(files.items())))
    print('entry:', r['entry'])
    for b in r['blobs']:
        print(f"{b['emoji']} {b['kind']:14} {b['name']:24} size={b['size']:4} cx={b['complexity']:3} funcs={[f['name'] for f in b['funcs']]}")
    for e in r['edges']:
        print(f"  {e['a']:30} -[{e['kind']}:{e['n']}]-> {e['b']} {e.get('via','')}")
    st = r['stats']
    print(f"calls={st['calls']} unresolved={st['unresolved']} ({st['unresolved'] / max(st['calls'], 1):.1%}) "
          f"parse={st['parse_ms']}ms analyze={st['analyze_ms']}ms")
