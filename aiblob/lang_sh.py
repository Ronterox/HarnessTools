"""Shell scripts for analyze.py: `.sh` / `.bash` files and extensionless files with a sh / bash shebang.

ShLang(Func, lang_for, path_index, match_path) implements the hooks PyLang has (see analyze.py). Module names come
from paths: `scripts/fetch-deps.sh` -> `scripts.fetch-deps_sh`, `x.bash` -> `x_bash`, an extensionless `bin/deploy`
-> `bin.deploy_sh` (the suffix keeps a script from colliding with a `.py` / `.c` module of the same stem).

Entry: every script's top-level code (`<module>`, the statements outside function definitions) is an `entry` blob
named after the file; a script with nothing but functions has none. Functions: `f() {}` and `function f {}`.
Calls: a command naming a function of the script, or of a script it sources (`source x` / `. x`, transitively;
else of the one script that sources it), is a call; `source x` is also a call to x's entry and an import (deps).
Running another script, `bash x.sh` / `sh x` / `./x.sh` / `"$DIR/x.sh"` / `exec x.sh`, is a call to its entry;
running another language, `python foo.py` / `.venv/bin/python -m pkg.mod` / `node x.js`, is a call to that module's
entry (`<module>`), which then becomes an `entry` root of its own (analyze() reads the `tops` link() returns).
Paths resolve through literals, variables assigned a literal at top level, `$0` / `${BASH_SOURCE[0]}` and the
script-dir idioms (`$(dirname "$0")`, `$(cd "$(dirname "$0")/.." && pwd)`, `${0%/*}`), relative to the directory a
top-level `cd` went to, else to the script's directory, then to the snapshot root. A path that resolves to no
snapshot module is unresolved when it has a code extension, else external (a binary, `./build/app`).
Wrappers (`exec env nohup time timeout nice sudo command builtin setsid stdbuf`, `uv / poetry / pipenv run`) are
looked through; `trap f EXIT` and a command argument naming one of the script's functions are deferred calls.
Data: environment variables are `env:X`: exported (`export X=`, `declare -x`), set for one command (`X=1 cmd`), or
read (`$X`, `${X}`) when the script never assigns X, or reads it with a default (`${X:-v}`, `:=`, `:?`, `:+`);
positional and bash-internal variables never are. Redirections (`> out.log`, `< in.txt`) and the file arguments of
cat / head / tail / tee / cp / rm ... are `file:<path>`; every argument naming a snapshot file is a `ref`.
Errors: `exit N` / `return N` with a literal N != 0, and a `||` tail that exits (`|| exit`, `|| return`,
`|| die "..."`, `|| { ...; exit 1; }`) count in `errors`; calls in such a tail, or in an `if` branch that ends in
one, are on the error path. Builtins and common external commands never count as unresolved.
"""
import re, hashlib, posixpath
from tree_sitter import Language, Parser
import tree_sitter_bash as _tsb

_SH = Parser(Language(_tsb.language()))
EXTS = ('.sh', '.bash')
SHEBANG = re.compile(r'#![ \t]*(?:/\S*/)?(?:env[ \t]+(?:-\S+[ \t]+)*)?(?:ba|da)?sh(?:[ \t]|$)')

BUILTINS = frozenset('''
. : [ [[ alias bg bind break builtin caller cd command compgen complete compopt continue declare dirs disown echo
enable eval exec exit export false fc fg getopts hash help history jobs kill let local logout mapfile popd printf
pushd pwd read readarray readonly return set shift shopt source suspend test times trap true type typeset ulimit
umask unalias unset wait
'''.split())
# common commands outside the snapshot: never the app's own, never unresolved (unless a script defines them)
EXTERNAL = BUILTINS | frozenset('''
ls cat tac head tail cp mv rm mkdir rmdir ln touch chmod chown chgrp stat du df find xargs grep egrep fgrep rg ag sed
awk gawk mawk cut tr sort uniq wc tee diff cmp comm paste join split fold fmt nl od hexdump xxd base64 md5sum sha1sum
sha256sum sha512sum shasum cksum date sleep basename dirname realpath readlink mktemp env printenv which whereis
whoami id uname hostname tty yes seq expr bc dc true false timeout nohup nice renice time watch killall pkill pgrep
ps top htop free uptime tar gzip gunzip zcat bzip2 bunzip2 xz unxz zstd zip unzip curl wget ssh scp rsync sftp git
gh make cmake ninja meson cc gcc clang ld ar nm objdump strip gdb lldb valgrind python python3 pip pip3 uv pipx
poetry pipenv pytest node npm npx yarn pnpm bun deno tsc tsx go cargo rustc java javac mvn gradle ruby gem perl php
lua luajit docker podman kubectl helm systemctl journalctl service sudo su doas apt dpkg yum dnf pacman brew snap
flatpak tmux screen nvim vim vi nano emacs less more file jq yq sqlite3 psql mysql openssl gpg tput stty clear reset
logger open pbcopy xclip xsel convert ffmpeg nproc lsof netstat ss ip ifconfig ping dig nslookup host nc socat
strace ltrace install patch iconv locale getopt flock setsid script column numfmt shuf rev truncate sync mount
umount dd crontab claude Xvfb Xephyr xdotool wmctrl xrandr xset stdbuf unbuffer parallel entr fzf bat fd
'''.split())
WRAPPERS = frozenset({'exec', 'command', 'builtin', 'env', 'nohup', 'time', 'nice', 'sudo', 'doas', 'timeout',
                      'setsid', 'stdbuf'})
RUNNERS = frozenset({'uv', 'poetry', 'pipenv'})              # `uv run cmd ...`
SH_RUN = frozenset({'sh', 'bash', 'dash'})
PY_RUN = re.compile(r'(?:python|pypy)(?:\d+(?:\.\d+)*)?')
JS_RUN = frozenset({'node', 'nodejs', 'bun', 'tsx', 'ts-node'})
VALUE_FLAGS = {'py': {'-X', '-W', '-Q'}, 'js': {'-r', '--require', '--import', '--loader', '-e', '--eval', '-p'},
               'sh': {'-o', '-O'}}
STOP_FLAGS = {'py': {'-c', '-'}, 'js': {'-e', '--eval', '-p', '--print', '-'}, 'sh': {'-c', '-s', '-'}}
FILE_CMDS = frozenset('''cat tac head tail tee wc sort uniq cut paste nl od xxd base64 md5sum sha1sum sha256sum
sha512sum cmp diff touch rm cp mv ln chmod truncate less more'''.split())
SPECIAL = frozenset('''BASH_SOURCE BASH_REMATCH BASH_LINENO BASH_VERSION BASH_VERSINFO BASH_ARGV BASH_ARGC
BASH_COMMAND BASH_SUBSHELL BASHPID BASHOPTS LINENO FUNCNAME RANDOM SRANDOM SECONDS EPOCHSECONDS EPOCHREALTIME
PIPESTATUS IFS OPTARG OPTIND OPTERR REPLY PWD OLDPWD PPID UID EUID GROUPS SHLVL HOSTTYPE OSTYPE MACHTYPE DIRSTACK
SHELLOPTS COLUMNS LINES COMP_WORDS COMP_CWORD COMPREPLY'''.split())
DEFAULT_OPS = frozenset({':-', '-', ':=', '=', ':?', '?', ':+', '+'})
ERROR_CALLEE = re.compile(r'(?:[\w-]*[_-])?(?:die|fatal|panic|error|err|fail|abort|bail)')   # die, log_error; not failover
BRANCH_TYPES = frozenset({'if_statement', 'elif_clause', 'for_statement', 'c_style_for_statement',
                          'while_statement', 'case_item'})
PATH = re.compile(r'[\w.@+~-]+(?:/[\w.@+~-]+)*/?')
HERE = '\x01'            # a value that is a path in the snapshot: HERE + path relative to the root ('' = the root)


def modname(rel):
    parts = rel.split('/')
    stem, ext = posixpath.splitext(parts[-1])
    parts[-1] = stem.replace('.', '_') + '_' + ext[1:] if ext in EXTS else parts[-1].replace('.', '_') + '_sh'
    return '.'.join([p.replace('.', '_') for p in parts[:-1]] + [parts[-1]])


def _text(n, src):
    return src[n.start_byte:n.end_byte].decode('utf-8', 'replace')


def _hash(s):
    return hashlib.sha1(s.encode()).hexdigest()[:10]


def _norm(p):
    """A path relative to the root, normalised; None when it leaves the root."""
    p = posixpath.normpath(p) if p else '.'
    if p == '.':
        return ''
    return None if p == '..' or p.startswith(('../', '/')) else p


class ShLang:
    name, ctor, module_types = 'sh', '__init__', False
    builtins = stdlib = EXTERNAL

    def __init__(self, Func, lang_for, path_index, match_path):
        self.Func, self.lang_for, self.path_index, self.match_path = Func, lang_for, path_index, match_path
        self._others, self._set, self._index = None, set(), None

    def handles(self, rel):
        return rel.endswith(EXTS)

    def claims(self, rel, text):
        """An extensionless file whose first line is a sh / bash shebang."""
        return '.' not in rel.rsplit('/', 1)[-1] and text.startswith('#!') and bool(SHEBANG.match(text.split('\n', 1)[0]))

    def modname(self, rel):
        return modname(rel)

    def is_test(self, mod, rel):
        parts = rel.split('/')
        stem = posixpath.splitext(parts[-1])[0]
        return bool({'test', 'tests'} & set(parts[:-1])) or stem.startswith('test_') or stem.endswith('_test')

    def parse_module(self, rel, text):
        return _Module(self, rel, text).info

    # ------------------------------------------------------------ analysis hooks

    def link(self, mods, funcs, class_keys):
        """alias: `<module>.<name>` import targets -> the function a command names (own script first, then the
        scripts it sources, transitively, then the one script sourcing it that defines it), or -> `ext:<name>` for
        builtins / known commands; `<module>.__run<k>__` -> the `<module>` entry of the script or module that command
        runs (`ext:run` when it names no snapshot code and has no code extension). tops: the modules whose
        `<module>` is an entry: every script, and the modules of other languages a script runs."""
        file_mod = {f.file: f.module for f in funcs.values() if f.name == '<module>' and f.file}
        sh_of = {info['file']: m for m, info in mods.items()}
        src_of = {m: [next((sh_of[p] for p in cands if p in sh_of), None) for cands in info.get('sources', ())]
                  for m, info in mods.items()}
        by = {m: [s for s in ss if s and s != m] for m, ss in src_of.items()}
        rev = {}
        for m, ss in by.items():
            for s in ss:
                rev.setdefault(s, []).append(m)

        def closure(m, graph):
            seen, order, stack = {m}, [], list(reversed(graph.get(m, ())))
            while stack:
                s = stack.pop()
                if s not in seen:
                    seen.add(s)
                    order.append(s)
                    stack.extend(reversed(graph.get(s, ())))
            return order

        alias, tops = {}, set(mods)
        for m, info in mods.items():
            for key, (cands, count) in info.get('runs', {}).items():
                q = next((f'{file_mod[p]}.<module>' for p in cands if p in file_mod and
                          f'{file_mod[p]}.<module>' in funcs), None)
                if q and funcs[q].lines:
                    alias[f'{m}.{key}'] = q
                    tops.add(funcs[q].module)
                elif q or not count:          # a library without top-level code runs nothing; a binary is not ours
                    alias[f'{m}.{key}'] = 'ext:run'
            down = None
            for name, tgt in info['imports'].items():
                if name in info.get('runs', ()) or tgt in funcs:
                    continue
                if down is None:
                    down = closure(m, by)
                q = next((f'{s}.{name}' for s in down if f'{s}.{name}' in funcs), None)
                if q is None:
                    up = [f'{s}.{name}' for s in closure(m, rev) if f'{s}.{name}' in funcs]
                    q = up[0] if len(up) == 1 else None
                if q:
                    alias[tgt] = q
                elif name in EXTERNAL:
                    alias[tgt] = 'ext:' + name
        return {'alias': alias, 'tops': tops}

    def entry(self, ms, mods, imported, closure, is_test, files):
        return None          # every script is an entry of its own (route_of), none is "the" entry

    def route_of(self, f, extra, ctx):
        return (posixpath.basename(f.file), 'entry') if f.name == '<module>' else None

    def snippet(self, f):
        """Up to 3 lines: error exits and effects first (exit / return N, || die, >&2, rm, curl, exec), then the
        rest minus comments, braces and bare declarations."""
        lines, body = [], f.body.splitlines()
        if f.name != '<module>' and body:
            first = body[0].split('{', 1)
            body = ([first[1]] if len(first) > 1 else []) + body[1:]
        for raw in body:
            l = raw.strip()
            if not l or l.startswith('#') or re.fullmatch(r'[{}();&|]+|then|do|done|fi|esac|else|;;', l):
                continue
            lines.append(l)

        def tier(l):
            if re.search(r'\b(?:exit|return)\s+[1-9]|\|\|\s*(?:exit|return|\w*(?:die|fail|fatal|err))\b|>&2|'
                         r'\b(?:rm|curl|wget|exec|kill|mv)\s', l):
                return 0
            return 3 if re.fullmatch(r'(?:local|declare|typeset|readonly)\s+[\w\s-]+', l) else 2
        best = sorted(range(len(lines)), key=lambda i: (tier(lines[i]), i))
        return [lines[i] for i in sorted(i for i in best[:3] if tier(lines[i]) < 3)]

    def extra_refs(self, f, others, ctx):
        """(literal, snapshot file) for each argument / redirection of f naming a non-code file: its first
        candidate path that is one, else the literal matched as Python's are (analyze._match_path)."""
        if self._others is not others:          # a new analyze() call: index again only when the files changed
            if others != self._others:
                self._set, self._index = set(others), self.path_index(others)
            self._others = others
        out = []
        for lit, cands in getattr(f, 'paths', ()):
            p = next((c for c in cands if c in self._set), None) or self.match_path(lit, self._index)
            if p:
                out.append((lit, p))
        return out


class _Module:
    """One parse: the module info dict analyze() expects, plus runs / sources for link()."""

    def __init__(self, lang, rel, text):
        self.lang, self.Func, self.rel = lang, lang.Func, rel
        self.src = src = text.encode('utf-8', 'surrogateescape')    # merge3 texts may carry escaped bytes
        root = _SH.parse(src).root_node
        self.mod = mod = modname(rel)
        self.dir = posixpath.dirname(rel)
        self.info = info = {'name': mod, 'file': rel, 'imports': {}, 'funcs': [], 'globals': set(), 'classes': set(),
                            'blueprints': set(), 'framework': set(), 'lines': text.count('\n') + 1,
                            'entry_like': False, 'deps': [], 'global_tables': {}, 'class_attrs': {},
                            'runs': {}, 'sources': []}
        self.cwd = None           # directory of a top-level `cd` (relative to the root); None unknown, False outside
        self.vals = {}            # variable -> its value (a top-level literal assignment); None: not one value
        self.assigned = set()     # every variable the script assigns (any scope)
        self.exported = set()
        self.reads = {}           # Func -> [(variable, read with a default)]
        self.seen = set()
        self.fnames = {_text(n.child_by_field_name('name'), src) for n in self.defs(root)
                       if n.child_by_field_name('name') is not None}
        top = self.Func(mod, '<module>', root, src)
        self.new(top)
        stmts = []
        for c in root.named_children:
            if c.type == 'comment':
                continue
            if c.type != 'function_definition':
                stmts.append(c)
            self.stmt(c, top, False)
        top.lines = sum(n.end_point[0] - n.start_point[0] + 1 for n in stmts)
        top.body = '\n'.join(_text(n, src) for n in stmts)
        top.hash = _hash(top.body)
        info['top'] = top
        for f in [top] + info['funcs']:
            for name, dflt in self.reads.get(f, ()):
                if name in self.exported or dflt or name not in self.assigned:
                    f.data.add('env:' + name)

    def new(self, f):
        f.file = self.rel
        f.paths = []
        self.reads[f] = []

    def defs(self, n):
        stack = [n]
        while stack:
            c = stack.pop()
            if c.type == 'function_definition':
                yield c
            stack.extend(c.named_children)

    # ------------------------------------------------------------ values and paths

    def var(self, name):
        if name in ('0', 'BASH_SOURCE'):
            return HERE + self.rel
        if name == 'PWD':
            return HERE + self.cwd if isinstance(self.cwd, str) else None
        return self.vals.get(name)

    def value(self, n):
        """The string an argument expands to, as far as it is static; None otherwise."""
        src, t = self.src, n.type
        if t in ('word', 'number'):
            s = _text(n, src)
            return None if any(ch in s for ch in '*?[\\') else s
        if t == 'raw_string':
            return _text(n, src)[1:-1]
        if t in ('string', 'concatenation'):
            out = []
            for c in n.children:
                if c.type == '"':
                    continue
                v = _text(c, src) if c.type == 'string_content' else self.value(c)
                if v is None:
                    return None
                out.append(v)
            return ''.join(out)
        if t == 'simple_expansion':
            nm = next((c for c in n.named_children if c.type in ('variable_name', 'special_variable_name')), None)
            return self.var(_text(nm, src)) if nm is not None else None
        if t == 'expansion':
            kids = [c for c in n.children if c.type not in ('${', '}')]
            if len(kids) == 1 and kids[0].type in ('variable_name', 'special_variable_name'):
                return self.var(_text(kids[0], src))
            if len(kids) == 1 and kids[0].type == 'subscript' and _text(kids[0], src) == 'BASH_SOURCE[0]':
                return HERE + self.rel
            if len(kids) == 3 and kids[0].type in ('variable_name', 'special_variable_name') and \
                    _text(kids[1], src) == '%' and _text(kids[2], src) == '/*':
                v = self.var(_text(kids[0], src))
                return self.dirname(v)
            return None
        if t == 'command_substitution':
            return self.subst(n)
        return None

    def dirname(self, v):
        if v is None:
            return None
        if v.startswith(HERE):
            return HERE + posixpath.dirname(v[1:])
        return posixpath.dirname(v) or '.'

    def subst(self, n):
        """$(dirname X), $(realpath X), $(pwd), $(cd X && pwd): the directory idioms."""
        cmds = [c for c in n.named_children if c.type in ('command', 'list')]
        if len(cmds) != 1:
            return None
        c = cmds[0]
        if c.type == 'list':
            parts = [x for x in c.named_children if x.type == 'command']
            if len(parts) == 2 and self.cmd_name(parts[0]) == 'cd' and self.cmd_name(parts[1]) == 'pwd':
                args = self.args(parts[0])
                v = self.value(args[-1]) if args else None
                return self.at(v)
            return None
        name, args = self.cmd_name(c), self.args(c)
        args = [a for a in args if not _text(a, self.src).startswith('-')]
        if name == 'dirname' and len(args) == 1:
            return self.dirname(self.value(args[0]))
        if name in ('realpath', 'readlink') and len(args) == 1:
            return self.at(self.value(args[0]))
        if name == 'pwd' and not args:
            return HERE + self.cwd if isinstance(self.cwd, str) else None
        return None

    def at(self, v):
        """v as a snapshot path value (HERE + path), when it is one."""
        c = self.cands(v)
        return HERE + c[0] if len(c) == 1 else None

    def cands(self, v):
        """The root-relative paths v may name: exact for HERE values or after a known `cd`, else the script's
        directory first, then the root."""
        if not v:
            return []
        if v.startswith(HERE):
            p = v[1:].lstrip("/")             # "$root/x" with root the snapshot root
            if HERE in p or not (p == '' or PATH.fullmatch(p.lstrip('./') or '.')):
                return []
            p = _norm(p)
            return [p] if p is not None else []
        if HERE in v or v.startswith(('/', '~', '-')) or not PATH.fullmatch(v) or not ('.' in v or '/' in v):
            return []
        if self.cwd is False:
            return []
        if self.cwd is not None:
            p = _norm(posixpath.join(self.cwd, v))
            return [p] if p else []
        out = [_norm(posixpath.join(self.dir, v)), _norm(v)]
        return [p for i, p in enumerate(out) if p and p not in out[:i]]

    # ------------------------------------------------------------ the walk

    def stmt(self, n, f, err):
        """One node of f's code (err: on the error path). A `cd` or assignment lasts until the end of the
        enclosing subshell, pipeline, substitution or function body."""
        t = n.type
        if t == 'comment':
            return
        if t == 'function_definition':
            self.function(n)
            return
        if t in BRANCH_TYPES:
            f.branches += 1
        if t == 'list':
            self.list(n, f, err)
            return
        if t in ('if_statement', 'elif_clause', 'else_clause'):
            self.branches(n, f, err)
            return
        if t == 'case_item':
            self.case_item(n, f, err)
            return
        if t == 'command':
            self.command(n, f, err)
            return
        if t in ('variable_assignment', 'variable_assignments'):
            self.assign(n, f, err)
            return
        if t == 'declaration_command':
            self.declare(n, f, err)
            return
        if t == 'for_statement':
            v = n.child_by_field_name('variable')
            if v is not None:
                self.assigned.add(_text(v, self.src))
        if t == 'file_redirect':
            self.redirect(n, f, err)
            return
        if t in ('simple_expansion', 'expansion'):
            self.read(n, f)
        if t in ('subshell', 'pipeline', 'command_substitution', 'process_substitution'):
            saved = (self.cwd, dict(self.vals))
            for c in n.named_children:
                self.stmt(c, f, err)
            self.cwd, self.vals = saved
            return
        for c in n.named_children:
            self.stmt(c, f, err)

    def function(self, n):
        nm = n.child_by_field_name('name')
        name = _text(nm, self.src) if nm is not None else None
        if not name or name in self.seen:       # a second definition (if / else twins): the first one
            return
        self.seen.add(name)
        f = self.Func(self.mod, name, n, self.src)
        self.new(f)
        saved = (self.cwd, dict(self.vals))
        body = n.child_by_field_name('body')
        if body is not None:
            self.stmt(body, f, False)
        self.cwd, self.vals = saved
        self.info['funcs'].append(f)

    def read(self, n, f):
        src = self.src
        kids = n.named_children
        nm = next((c for c in kids if c.type == 'variable_name'), None)
        if nm is None:
            sub = next((c for c in kids if c.type == 'subscript'), None)
            nm = sub.child_by_field_name('name') if sub is not None else None
        if nm is None:
            return
        name = _text(nm, src)
        if name in SPECIAL or not re.fullmatch(r'[A-Za-z_]\w*', name):
            return
        dflt = n.type == 'expansion' and any(n.field_name_for_child(i) == 'operator' and _text(c, src) in DEFAULT_OPS
                                             for i, c in enumerate(n.children))
        self.reads[f].append((name, dflt))

    def list(self, n, f, err):
        """a && b || c: the operands in order; an operand after `||` that exits is an error tail."""
        prev = None
        for c in n.children:
            if c.type in ('&&', '||'):
                f.branches += 1
                prev = c.type
                continue
            if not c.is_named or c.type == 'comment':
                continue
            tail = prev == '||' and self.exits(c, True)
            if tail and not self.hard_exit(c):
                f.errors += 1
            self.stmt(c, f, err or tail)

    def branches(self, n, f, err):
        """if / elif / else: the condition as is (what comes before `then`); a branch whose last statement
        exits is the error path from after its last statement that can still leave normally."""
        body, inner, cond = [], [], n.type != 'else_clause'
        for c in n.children:
            if c.type == 'then':
                cond = False
            elif c.type in ('elif_clause', 'else_clause'):
                inner.append(c)
            elif c.is_named and c.type != 'comment':
                if cond:
                    self.stmt(c, f, err)
                else:
                    body.append(c)
        self.block(body, f, err)
        for c in inner:
            f.branches += c.type == 'elif_clause'
            self.branches(c, f, err)

    def case_item(self, n, f, err):
        """`pat) ...;;`: the patterns as is, the body like an if branch."""
        body = []
        for i, c in enumerate(n.children):
            if n.field_name_for_child(i) == 'value':
                self.stmt(c, f, err)
            elif c.is_named and c.type != 'comment':
                body.append(c)
        self.block(body, f, err)

    def block(self, body, f, err):
        """A branch's statements: when the last one exits, those after the last one that can still leave
        normally are on the error path."""
        on = set()
        if body and self.exits(body[-1]):
            start = max((i + 1 for i, c in enumerate(body[:-1]) if self.normal_exit(c)), default=0)
            on = set(range(start, len(body)))
        for i, c in enumerate(body):
            self.stmt(c, f, err or i in on)

    def assign(self, n, f, err):
        for a in ([n] if n.type == 'variable_assignment' else n.named_children):
            if a.type != 'variable_assignment':
                self.stmt(a, f, err)
                continue
            nm, v = a.child_by_field_name('name'), a.child_by_field_name('value')
            name = _text(nm, self.src) if nm is not None and nm.type == 'variable_name' else None
            if v is not None:
                self.stmt(v, f, err)
            if name:
                self.assigned.add(name)
                self.vals[name] = self.value(v) if v is not None else ''      # the latest assignment wins

    def declare(self, n, f, err):
        kw = _text(n.children[0], self.src)
        flags = [_text(c, self.src) for c in n.named_children if c.type == 'word' and _text(c, self.src).startswith('-')]
        exp = kw == 'export' or any('x' in fl for fl in flags)
        for c in n.named_children:
            if c.type == 'variable_assignment':
                nm = c.child_by_field_name('name')
                name = _text(nm, self.src) if nm is not None else None
                self.assign(c, f, err)
            elif c.type == 'variable_name':
                name = _text(c, self.src)
                self.assigned.add(name)
            else:
                self.stmt(c, f, err)
                continue
            if exp and name:
                self.exported.add(name)
                f.data.add('env:' + name)

    def redirect(self, n, f, err):
        d = n.child_by_field_name('destination')
        if d is None:
            return
        self.stmt(d, f, err)
        if d.type == 'number' or _text(n, self.src).lstrip('0123456789').startswith(('>&', '<&')):
            return
        self.path(d, f, data=True)

    def path(self, a, f, data=False):
        """Note argument a as a path f names (a ref when it is a snapshot file; data with data=True)."""
        c = self.cands(self.value(a))
        if not c:
            return
        f.paths.append((_text(a, self.src).strip('"\''), c))
        if data:
            f.data.add('file:' + c[-1])

    # ------------------------------------------------------------ commands

    def cmd_name(self, n):
        if n.type != 'command':
            return None
        nm = n.child_by_field_name('name')
        return _text(nm, self.src) if nm is not None else None

    def args(self, n):
        return [c for i, c in enumerate(n.children) if n.field_name_for_child(i) == 'argument']

    def exits(self, n, tail=False):
        """n leaves with an error: exit / return with a literal non-zero status, a die-like call, or a { } group /
        subshell whose last statement does. A bare exit / return or one with a status made at run time (`$?`,
        `"$fail"`) passes the last command's status on: an error only in a `||` tail (tail=True)."""
        if n.type in ('compound_statement', 'subshell'):
            kids = [c for c in n.named_children if c.type != 'comment']
            return bool(kids) and self.exits(kids[-1], tail)
        if n.type == 'redirected_statement':
            b = n.child_by_field_name('body')
            return b is not None and self.exits(b, tail)
        name = self.cmd_name(n)
        if name in ('exit', 'return'):
            args = self.args(n)
            if args and args[0].type == 'number':
                return _text(args[0], self.src) != '0'
            return tail
        return bool(name) and bool(ERROR_CALLEE.fullmatch(name))

    def hard_exit(self, n):
        """exit / return with a literal non-zero status (counted where it stands)."""
        if n.type in ('compound_statement', 'subshell'):
            kids = [c for c in n.named_children if c.type != 'comment']
            return bool(kids) and self.hard_exit(kids[-1])
        if n.type == 'redirected_statement':
            b = n.child_by_field_name('body')
            return b is not None and self.hard_exit(b)
        if self.cmd_name(n) in ('exit', 'return'):
            args = self.args(n)
            return bool(args) and args[0].type == 'number' and _text(args[0], self.src) != '0'
        return False

    def normal_exit(self, n):
        if self.cmd_name(n) in ('exit', 'return'):
            return not self.exits(n)
        return any(self.normal_exit(c) for c in n.named_children)

    def command(self, n, f, err):
        src = self.src
        for i, c in enumerate(n.children):            # X=1 cmd: X is set in cmd's environment
            fld = n.field_name_for_child(i)
            if c.type == 'variable_assignment':
                nm, v = c.child_by_field_name('name'), c.child_by_field_name('value')
                if nm is not None:
                    f.data.add('env:' + _text(nm, src))
                if v is not None:
                    self.stmt(v, f, err)
            elif fld != 'name' and fld != 'argument':
                self.stmt(c, f, err)
        nm = n.child_by_field_name('name')
        if nm is None:
            return
        words = [nm] + self.args(n)
        for w in words:
            self.stmt(w, f, err)                      # expansions and substitutions inside the words
        self.run(words, f, err)

    def run(self, words, f, err):
        src = self.src
        while words:
            head = words[0]
            name = self.value(head.named_children[0] if head.type == 'command_name' and head.named_children else head)
            if name is None:
                return                                  # "$cmd" ...: nothing static to place
            base = name.rsplit('/', 1)[-1] if not name.startswith(HERE) else posixpath.basename(name[1:])
            rest = words[1:]
            if name in WRAPPERS:
                words = self.unwrap(name, rest, f)
                continue
            if name in RUNNERS and rest and _text(rest[0], src) == 'run':
                words = [w for w in rest[1:]]
                while words and _text(words[0], src).startswith('-'):
                    words = words[1:]
                continue
            if name == 'cd':
                self.cd(rest)
                return
            if name in ('source', '.'):
                if rest:
                    c = self.cands(self.value(rest[0]))
                    self.info['sources'].append(c)
                    for p in c:
                        dep = modname(p)
                        if dep != self.mod and dep not in self.info['deps']:
                            self.info['deps'].append(dep)
                    self.runs(c, True, f, err)
                    self.path(rest[0], f)
                    for a in rest[1:]:
                        self.path(a, f)
                return
            kind = 'sh' if base in SH_RUN else 'py' if PY_RUN.fullmatch(base) else 'js' if base in JS_RUN else None
            if kind:
                self.interp(kind, rest, f, err)
                return
            if '/' in name or name.startswith(HERE) or (self.lang.lang_for(name) is not None and '.' in name):
                c = self.cands(name if name.startswith(HERE) or '/' in name else './' + name)
                self.runs(c, self.lang.lang_for(c[0] if c else name) is not None, f, err)
                for a in rest:
                    self.path(a, f)
                return
            self.call(name, rest, f, err)
            return

    def unwrap(self, name, rest, f):
        src = self.src
        i = 0
        while i < len(rest):
            t = _text(rest[i], src)
            if name == 'env' and re.fullmatch(r'[A-Za-z_]\w*=.*', t, re.S):
                f.data.add('env:' + t.split('=', 1)[0])
            elif not t.startswith('-'):
                break
            elif name in ('nice', 'sudo', 'timeout', 'stdbuf') and t in ('-n', '-u', '-g', '-s', '-k', '-i', '-o', '-e'):
                i += 1
            i += 1
        if name == 'timeout' and i < len(rest):
            i += 1                                      # the duration
        return rest[i:]

    def interp(self, kind, rest, f, err):
        """python / node / bash with flags, then the script (or -m module) it runs."""
        src = self.src
        i = 0
        while i < len(rest):
            t = _text(rest[i], src).strip('"\'')
            if t in STOP_FLAGS[kind]:               # -c code / stdin: no script, its arguments may name files
                for a in rest[i + 1:]:
                    self.path(a, f)
                return
            if kind == 'py' and t == '-m' and i + 1 < len(rest):
                m = self.value(rest[i + 1])
                if m and re.fullmatch(r'[\w.]+', m):
                    stem = m.replace('.', '/')
                    c = [p for s in (stem + '.py', stem + '/__main__.py') for p in self.cands('./' + s)]
                    self.runs(c, False, f, err)
                return
            if t.startswith('-'):
                i += 1 + (t in VALUE_FLAGS[kind])
                continue
            v = self.value(rest[i])
            if v is not None:
                c = self.cands(v if v.startswith(HERE) or '/' in v else './' + v)
                self.runs(c, self.lang.lang_for(v[1:] if v.startswith(HERE) else v) is not None, f, err)
            for a in rest[i:]:
                self.path(a, f)
            return

    def cd(self, rest):
        args = [a for a in rest if not _text(a, self.src).startswith('-')]      # cd -P dir, cd -- dir
        v = self.value(args[0]) if args else '~'
        if v is None or v == '-':
            self.cwd = None
        elif v.startswith(HERE):
            p = _norm(v[1:].lstrip('/'))                 # "$here/s" with here the snapshot root
            self.cwd = p if p is not None else False
        elif v.startswith(('/', '~')):
            self.cwd = False
        elif isinstance(self.cwd, str):
            p = _norm(posixpath.join(self.cwd, v))
            self.cwd = p if p is not None else False
        else:
            self.cwd = None if self.cwd is None else False

    def runs(self, cands, count, f, err):
        """A call to the entry of the script / module at the first of cands that is one (link() decides)."""
        if not cands:
            return                                      # a path made at run time: nothing to place
        key = f'__run{len(self.info["runs"])}__'       # a name no script uses (dots or `@` would never count)
        self.info['runs'][key] = (cands, count)
        self.info['imports'][key] = f'{self.mod}.{key}'
        f.calls.append((key, err, 'call'))

    def call(self, name, rest, f, err):
        src = self.src
        if name in ('exit', 'return'):
            if rest and rest[0].type == 'number' and _text(rest[0], self.src) != '0':
                f.errors += 1
            return
        if name == 'trap' and rest:                    # trap 'cleanup' EXIT: cleanup runs later
            v = self.value(rest[0])
            if v and re.fullmatch(r'[A-Za-z_][\w-]*', v):
                self.bind(v)
                f.calls.append((v, err, 'deferred'))
        if re.fullmatch(r'[A-Za-z_][\w-]*', name):
            self.bind(name)
            f.calls.append((name, err or bool(ERROR_CALLEE.fullmatch(name)), 'call'))
        for a in rest:
            t = _text(a, src)
            if a.type == 'word' and t in self.fnames and t != name:
                self.bind(t)
                f.calls.append((t, err, 'deferred'))   # check name fn args: fn is run by check
            elif name in FILE_CMDS and not t.startswith('-'):
                self.path(a, f, data=True)
            else:
                self.path(a, f)
        if name in ('read', 'mapfile', 'readarray', 'getopts'):
            names = [_text(a, src) for a in rest if a.type == 'word' and re.fullmatch(r'[A-Za-z_]\w*', _text(a, src))]
            self.assigned.update(names[1:] if name == 'getopts' else names)

    def bind(self, name):
        self.info['imports'].setdefault(name, f'{self.mod}.{name}')
