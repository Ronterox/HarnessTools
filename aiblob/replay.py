"""Turn Claude Code transcripts into file snapshots, one per edit.

    python replay.py <session.jsonl|project-dir> [more ...] [--root DIR] [--seed-git] [--git-reconcile] -> steps as JSON

A step is one Write/Edit/MultiEdit tool call that touched a file under root, a Bash heredoc
(`cat > path <<EOF`, a relative path taken from the call's cwd and any `cd dir` before it in the command;
paths with `$` or `~` are skipped) under root, or one file of a Bash call's `bashEditDiff` (the files Claude Code saw the
command change, with diff hunks, recorded on the tool result). Each step's `content` is the full tree
({relpath: text}) after it.

Several transcripts (paths and/or directories; a directory means its *.jsonl plus <session>/subagents/*.jsonl)
are merged: every transcript is parsed on its own (turns, `said`, tool results), then all steps are ordered by
timestamp (ties: input order, then order within the transcript) and applied to one snapshot. A tool call that
appears in several transcripts (a resumed session's file starts with copies of the earlier one's records)
counts once. With more than one
transcript each step carries `session` (the 8-char session id; a subagent's is its parent session's) and `agent`
(`main`, or the subagent's `type · description` from its agent-<id>.meta.json), and `turn` is renumbered across
transcripts in order of first appearance, so it still names one prompt.

Missing bases, kept consistent:
- A Write is the file's whole content: it always applies.
- An Edit whose old_string is not in the snapshot's file (or whose file the snapshot does not have) is a step
  with `applied: false`; the snapshot is left as it was (the file is not created).
- A bashEditDiff file is applied hunk by hunk (at its line, else where its old lines are nearest); a hunk whose
  new lines are already there counts as done (concurrent Bash calls each report the same change). A file whose
  hunks all were done already is no step; a file with a hunk that fits nowhere is a step with `applied: false`
  and the snapshot is left as it was, unless the report only saw another transcript's change (see _credit): no step.
  A report of a call that names neither the file nor root and runs outside root is no step either (_credit:
  `blind`); its text of the file serves later reports the way a whole-file Read does. `deleted` removes the file; `created` starts from an empty file.
  Files a command changed beyond what the diff lists (`changedFiles` without hunks) are not steps.
- A failing bashEditDiff file is tried again on the file's earlier texts (hunks exactly at their lines: Claude
  Code diffs against the text it last saw, which can be older than the file was), then on the file's text as
  the last whole-file Read (or blind report) showed it; either result gets the snapshot's changes since that text
  merged in by `git merge-file` (merge3), and a conflict rejects it. A hunk whose changed lines do not match but whose context lines do
  replaces what lies between them (a change the transcripts never recorded, e.g. a command whose diff was too
  big to record). An Edit starts from the file's text before it as its tool result recorded it
  (`originalFile`) when the snapshot's differs.
- seed_git: when a file's first touch is an Edit or a non-created bashEditDiff, its base is the file in the
  last commit of root's repository before that step's timestamp (`git log --before`). Without it (or with no
  such commit / file) the base is missing as above. Also, the files tracked in the last commit before the last
  step that no step touches are in every snapshot, from that commit (so names they define resolve).
- `seeded: true` marks a step whose base came from outside the replayed edits: git, `originalFile` or a Read.
- git_reconcile: every commit touching root authored from the first step on, up to HEAD (so the last snapshot is
  HEAD's tree, plus files git never tracked), is a step of its own at its author time (before the transcript steps of that second):
  tool `git`, author / session / agent `git`, file '', said the commit subject, its own turn. It sets every file
  under root the commit tracks to the commit's text and deletes the files its parent (or the previous git
  step's commit) tracked that it does not; files git never tracked stay. added / removed are the real diff
  between the snapshot and the commit; a commit that matches the snapshot is kept as a marker with `empty`.
  Edits after it apply on the committed text, so drift from changes the transcripts never recorded ends there.
"""
import json, os, re, sys, glob, functools, subprocess
from dataclasses import dataclass, field, asdict

@dataclass
class Step:
    idx: int
    ts: str
    tool: str
    file: str              # relative to root
    said: str              # assistant text right before the call (first line)
    turn: int              # index of the human prompt this answers
    added: int = 0
    removed: int = 0
    ok: bool = True
    content: dict = field(default_factory=dict, repr=False)  # relpath -> text after
    session: str = ''      # set when several transcripts are merged
    agent: str = ''
    applied: bool = True   # False: the edit did not fit the snapshot, which stayed as it was
    seeded: bool = False   # the file's base came from git (seed_git)
    author: str = 'ai'     # 'git': a commit's checkpoint (git_reconcile)
    empty: bool = False    # a git step whose commit matched the snapshot

HEREDOC_BODY = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2[ \t]*(?=\n|$)", re.S)
HEREDOC = re.compile(r"cat\s*>\s*['\"]?([^\s'\"<>]+)['\"]?\s*<<-?\s*['\"]?(\w+)['\"]?\n(.*?)\n\2\s*$", re.S | re.M)

SHELL_WORDS = {'if', 'then', 'elif', 'else', 'while', 'until', 'do', '{', '!', 'time', 'builtin', 'command'}  # a command follows
ASSIGN = re.compile(r'[A-Za-z_]\w*=')   # `x=1 cd dir`: the command follows the assignments
CD_OPTS = re.compile(r'-[LPe@]+$')

def cds(cmd):
    """The `cd dir` steps still in effect at the end of shell text cmd, in order: heredoc bodies and quoted
    strings are not commands, a `cd` inside `( ... )` ends at its `)`, one in a pipeline (`cd a | cat`, each part
    a subshell) or a background job (`cd a &`) does not last, one in `if ... then` / `{ ... }` does (they run in
    the shell itself), one in a function body (`f() { cd a; }`) is not run, a `case` pattern's `)` closes nothing
    and a `cd` in a `case` arm makes the directory unknown,
    options (`-P`, `--`) and prefixes (`x=1`, `builtin`, `command`) are skipped, and `cd -` / a bare `cd` are not
    followed (None then: the directory is unknown)."""
    import shlex
    text = HEREDOC_BODY.sub('<<HEREDOC', cmd).replace('\\\n', ' ').replace('\n', ' ; ')
    text = text.replace('&>', ' >')     # `&>file` is a redirection; `& >file` a background job, then one
    lex = shlex.shlex(text, posix=True, punctuation_chars=';&|()')
    lex.whitespace_split = True
    try:
        toks = list(lex)
    except ValueError:
        return []
    out, depth, start = [], 0, True     # out: (depth, pipeline, dir); start: the next token begins a command
    pipe, piped = 0, set()              # the current pipeline's number; the pipelines with a `|`
    skip, cases, fn = -1, 0, 0          # skip: a function body ends there; cases: open `case`s; fn: a body may follow
    op = lambda t: all(ch in ';&|()' for ch in t)   # shlex glues operators together: ');', '&&(' ...
    for i, t in enumerate(toks):
        if i <= skip:
            continue
        if op(t):
            fn = 1 if t == '()' else 0
            k = 0
            while k < len(t):
                if t[k:k + 2] in ('||', '&&', ';;'):
                    pipe, k = pipe + 1, k + 2
                    continue
                if t[k] == '|':
                    piped.add(pipe)
                    k += 2 if t[k:k + 2] == '|&' else 1
                    continue
                if t[k] == '&' and k == 0 and i and toks[i - 1].endswith(('>', '<')):
                    k += 1                  # 2>&1: a redirection
                    continue
                if t[k] == '&':
                    piped.add(pipe)         # a background job runs in a subshell
                pipe += 1
                if t[k] == '(' and t != '()':
                    depth += 1
                elif t[k] == ')' and t != '()' and not cases and depth:
                    out = [c for c in out if c[0] < depth]
                    depth -= 1
                k += 1
            start = True
            continue
        if t == '{' and fn:
            n = 0
            for skip in range(i, len(toks)):
                n += (toks[skip] == '{') - (toks[skip] == '}')
                if n == 0:
                    break
            fn, start = 0, True
            continue
        fn = 2 if start and t == 'function' else max(0, fn - 1)
        if start and t == 'case':
            cases += 1
        elif t == 'esac':
            cases = max(0, cases - 1)
        if start and (t in SHELL_WORDS or ASSIGN.match(t)):
            continue
        if start and t == 'cd' and cases:
            return None                     # in a `case` arm: which arm ran is unknown
        if start and t == 'cd':
            j = i + 1
            while j < len(toks) and CD_OPTS.match(toks[j]):
                j += 1
            if j < len(toks) and toks[j] == '--':
                j += 1
            arg = toks[j] if j < len(toks) else None
            if arg is None or arg == '-' or op(arg):
                return None
            out.append((depth, pipe, arg))
        start = False
    return [d for _, p, d in out if p not in piped]

def load_lines(path):
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, '*.jsonl')), key=os.path.getmtime)
        path = files[-1]
    for line in open(path, encoding='utf-8'):
        line = line.strip()
        if line:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                pass

def first_line(text, n=200):
    for ln in text.splitlines():
        ln = ln.strip().strip('#*`> ').strip()
        if ln and not ln.startswith('```'):
            return ln[:n]
    return ''

def is_subagent(path):
    return os.path.basename(path).startswith('agent-') and f'{os.sep}subagents{os.sep}' in path

def transcripts(inputs):
    """Paths and/or directories -> transcript files: a directory gives its *.jsonl (by their first record's
    timestamp, so an original comes before a resumed session that starts with copies of its records) and, for
    each, <session>/subagents/*.jsonl (by name)."""
    out = []
    for p in [inputs] if isinstance(inputs, str) else inputs:
        if not os.path.isdir(p):
            out.append(p)
            continue
        for f in sorted(glob.glob(os.path.join(p, '*.jsonl')), key=functools.cmp_to_key(_earlier)):
            out.append(f)
            out += sorted(glob.glob(os.path.join(f[:-len('.jsonl')], 'subagents', '*.jsonl')))
    return out

def _earlier(a, b):
    """Order of two transcripts: by first timestamp; on a tie (a resumed file that copies the original's very
    first record), by the first record each has that the other has not ('' when it has none: the original, whose
    records the resumed file all copied, comes first), then by name."""
    ka, kb = first_ts(a), first_ts(b)
    if ka == kb:
        ra, rb = _records(a), _records(b)
        sa, sb = {o for o, _ in ra}, {o for o, _ in rb}
        ka = next((t for o, t in ra if o not in sb), '')
        kb = next((t for o, t in rb if o not in sa), '')
    return -1 if (ka, a) < (kb, b) else (1 if (ka, a) > (kb, b) else 0)

def _records(path):
    """[(identity, timestamp)] of a transcript's records with an ISO timestamp; identity ignores the session id,
    so a record copied into a resumed session matches its original."""
    return [(json.dumps({k: o.get(k) for k in ('type', 'timestamp', 'message')}, sort_keys=True), o['timestamp'])
            for o in load_lines(path) if isinstance(o.get('timestamp'), str) and o['timestamp']]

def first_ts(path):
    """The transcript's first ISO timestamp (records with another kind, e.g. integer ms, are skipped)."""
    return next((o['timestamp'] for o in load_lines(path) if isinstance(o.get('timestamp'), str) and o['timestamp']), '')

def who(path):
    """(session short id, agent label) of a transcript."""
    if is_subagent(path):
        session = os.path.basename(os.path.dirname(os.path.dirname(path)))
        meta = {}
        try:
            with open(path[:-len('.jsonl')] + '.meta.json', encoding='utf-8') as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            pass
        kind = (meta.get('agentType') or 'subagent').rsplit(':', 1)[-1]
        return session[:8], f"{kind} · {meta['description']}" if meta.get('description') else kind
    return os.path.splitext(os.path.basename(path))[0][:8], 'main'

def first_prompt(path, n=120):
    """First line of the transcript's first typed prompt (slash-command and caveat records skipped)."""
    for o in load_lines(path):
        if o.get('type') != 'user' or o.get('isMeta'):
            continue
        c = (o.get('message') or {}).get('content')
        t = c if isinstance(c, str) else ' '.join(b.get('text', '') for b in c or [] if isinstance(b, dict) and b.get('type') == 'text')
        if t.strip() and not t.lstrip().startswith(('<command-', '<local-command')):
            return first_line(t, n)
    return ''

def _ops(path, root, sidechain):
    """One transcript -> its edits in order, not yet applied: dicts with ts, tool, path, op, turn, said (a box
    shared by the calls after one text, spent by the first call that makes a step), tid (tool_use id), and per op:
    write: text; edit: old, new, rall; patch: hunks, created, deleted."""
    out = []
    box = {'said': ''}
    turn = -1
    results = {}          # tool_use_id -> is_error
    calls = {}            # Bash tool_use_id -> (turn, box, ts, paths its heredocs wrote)
    orig = {}             # Edit / Write tool_use_id -> the file's text before it (toolUseResult.originalFile)
    spatch = {}           # Edit tool_use_id -> its recorded hunks (toolUseResult.structuredPatch)
    for o in load_lines(path):
        side = o.get('isSidechain') and not sidechain
        if o.get('type') == 'user' and not side:
            c = (o.get('message') or {}).get('content')
            if isinstance(c, str) or (isinstance(c, list) and any(b.get('type') == 'text' for b in c if isinstance(b, dict))):
                turn += 1
            if isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get('type') == 'tool_result':
                        results[b.get('tool_use_id')] = bool(b.get('is_error'))
                        res = o.get('toolUseResult')
                        if isinstance(res, dict) and isinstance(res.get('originalFile'), str):
                            orig[b.get('tool_use_id')] = res['originalFile']
                        if isinstance(res, dict) and isinstance(res.get('structuredPatch'), list):
                            spatch[b.get('tool_use_id')] = res['structuredPatch']
                        f = res.get('file') if isinstance(res, dict) and res.get('type') == 'text' else None
                        if isinstance(f, dict) and f.get('startLine') == 1 and f.get('numLines') == f.get('totalLines') \
                                and isinstance(f.get('content'), str) and f.get('filePath'):
                            t = f['content']    # a whole-file Read: the file as it was (lines joined, no last newline)
                            out.append({'ts': o.get('timestamp', ''), 'tool': 'Read', 'path': f['filePath'], 'op': 'see',
                                        'text': t + '\n' if t and not t.endswith('\n') else t, 'turn': turn,
                                        'said': box, 'tid': b.get('tool_use_id')})
                        call = calls.get(b.get('tool_use_id'))
                        if call and isinstance(res, dict) and isinstance(res.get('bashEditDiff'), dict):
                            for f in res['bashEditDiff'].get('files') or []:
                                p = f.get('filePath') or ''
                                if p and p not in call[3]:
                                    out.append({'ts': o.get('timestamp') or call[2], 'tool': 'Bash', 'path': p, 'op': 'patch',
                                                'hunks': f.get('hunks') or [], 'created': bool(f.get('created')),
                                                'deleted': bool(f.get('deleted')), 'turn': call[0], 'said': call[1],
                                                'tid': b.get('tool_use_id'), 'start': call[2], 'cmd': call[4],
                                                'cwd': call[5]})
        if o.get('type') != 'assistant' or side:
            continue
        if root is None:
            root = o.get('cwd')
        for b in (o.get('message') or {}).get('content') or []:
            if b.get('type') == 'text' and b.get('text', '').strip():
                box = {'said': first_line(b['text'])}
            if b.get('type') != 'tool_use':
                continue
            name, inp = b.get('name'), b.get('input') or {}
            ts = o.get('timestamp', '')
            base = {'ts': ts, 'tool': name, 'turn': turn, 'said': box, 'tid': b.get('id')}
            fp = inp.get('file_path')
            if name == 'Write' and fp:
                out.append(dict(base, path=fp, op='write', text=inp.get('content', '')))
            elif name == 'Edit' and fp:
                out.append(dict(base, path=fp, op='edit', old=inp.get('old_string', ''), new=inp.get('new_string', ''),
                                rall=inp.get('replace_all', False)))
            elif name == 'MultiEdit' and fp:
                out += [dict(base, path=fp, op='edit', old=e.get('old_string', ''), new=e.get('new_string', ''),
                             rall=e.get('replace_all', False)) for e in inp.get('edits', [])]
            elif name == 'Bash':
                wrote = set()
                cmd = inp.get('command', '')
                for m in HEREDOC.finditer(cmd):
                    dirs = cds(cmd[:m.start()])
                    if dirs is None:
                        continue            # after `cd -` or a bare `cd`: the directory is unknown
                    p = os.path.join(o.get('cwd') or root or '', *dirs, m.group(1))
                    if '$' in p or '~' in p:
                        continue            # a shell variable or ~ the transcript cannot expand
                    wrote.add(p)
                    out.append(dict(base, path=p, op='write', text=m.group(3) + '\n'))
                calls[b.get('id')] = (turn, box, ts, wrote, cmd, o.get('cwd') or '')
    per = {}
    for op in out:
        op['ok'] = not results.get(op['tid'])
        per[op['tid']] = per.get(op['tid'], -1) + 1
        op['id'] = (op['tid'], op['op'], per[op['tid']])
        if op['op'] == 'edit' and per[op['tid']] == 0:
            op['orig'] = orig.get(op['tid'])       # before a MultiEdit's first sub-edit only
            if op['tool'] == 'Edit':
                op['spatch'] = spatch.get(op['tid'])
    return out, root

def git_base(root, rel, ts):
    """rel's text in the last commit of root's repository before ts, or None."""
    try:
        sha = subprocess.run(['git', '-C', root, 'log', '-1', '--format=%H', f'--before={ts}'],
                             capture_output=True, text=True).stdout.strip()
        if not sha:
            return None
        r = subprocess.run(['git', '-C', root, 'show', f'{sha}:./{rel}'], capture_output=True)
        return r.stdout.decode('utf-8', 'replace') if r.returncode == 0 else None
    except OSError:
        return None

def anchored(lines, body, want, slack=200):
    """(start, length) of the region a hunk replaces when its changed lines no longer match (a change the
    transcripts did not record): its leading and trailing context lines (all of them, 1 at least each) found
    near want, in order, enclosing about as many lines as the hunk's old side. (None, 0) when not found."""
    lead, trail = [], []
    for l in body:
        if l[:1] not in (' ', ''):
            break
        lead.append(l[1:])
    for l in reversed(body):
        if l[:1] not in (' ', ''):
            break
        trail.insert(0, l[1:])
    n_old = sum(1 for l in body if l[:1] in (' ', '-', ''))
    if not lead or not trail or len(lead) + len(trail) >= n_old or not any(x.strip() for x in lead + trail):
        return None, 0
    lo, hi = max(0, want - slack), min(len(lines), want + n_old + slack)
    starts = [i for i in range(lo, hi) if lines[i:i + len(lead)] == lead]
    best = None
    for i in starts:
        for j in range(i + len(lead), min(len(lines), i + 2 * n_old + 20) - len(trail) + 1):
            if lines[j:j + len(trail)] == trail:
                span = j + len(trail) - i
                cost = abs(i - want) + abs(span - n_old)
                if best is None or cost < best[0]:
                    best = (cost, i, span)
                break
    return (best[1], best[2]) if best else (None, 0)

def apply_hunks(text, hunks, strict=False):
    """bashEditDiff hunks ({oldStart, lines: [' ctx', '-old', '+new']}) onto text -> the new text, or None
    when a hunk fits nowhere. A hunk whose new lines already stand where it goes is done already; one whose
    old lines are not found but whose context lines are replaces what lies between them (anchored()).
    strict: each hunk's old lines must stand exactly at its line (after the earlier hunks' shift)."""
    lines = text.split('\n')
    offset = 0
    for h in hunks:
        body = [l for l in h.get('lines') or [] if not l.startswith('\\')]
        old = [l[1:] for l in body if l[:1] in (' ', '-', '')]
        new = [l[1:] for l in body if l[:1] in (' ', '+', '')]
        want = max(0, min(len(lines), (h.get('oldStart') or 1) - 1 + offset))

        def near(seq):
            if lines[want:want + len(seq)] == seq:
                return want
            if strict:
                return None
            hits = [i for i in range(len(lines) - len(seq) + 1) if lines[i:i + len(seq)] == seq] if seq else []
            return min(hits, key=lambda i: abs(i - want)) if hits else None
        if not strict and new != old and len(new) > sum(1 for l in body if l[:1] == '+') \
                and lines[want:want + len(new)] == new:
            at, span = want, len(new)       # done already where it goes (context and all): never insert it again
        elif lines[want:want + len(old)] == old:
            at, span = want, len(old)
        else:
            at, span = near(old), len(old)
        if at is None and not strict:
            if near(new) is not None:
                at, span = near(new), len(new)
                new = lines[at:at + span]
            else:
                at, span = anchored(lines, body, want)
        if at is None:
            return None
        lines[at:at + span] = new
        offset = at + len(new) - ((h.get('oldStart') or 1) - 1 + len(old))
    return '\n'.join(lines)

def merge3(o, a, b):
    """o changed into a and, apart, into b -> o with both changes: `git merge-file`'s clean three-way merge
    (bytes kept: CRLF, a missing last newline), or None on a conflict or an error."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for name, text in (('a', a), ('o', o), ('b', b)):
            paths.append(os.path.join(d, name))
            with open(paths[-1], 'wb') as fh:
                fh.write(text.encode('utf-8', 'surrogateescape'))
        r = subprocess.run(['git', 'merge-file', '-p', *paths], capture_output=True)
    return r.stdout.decode('utf-8', 'surrogateescape') if r.returncode == 0 else None

def _rank(op, root):
    """(names the file, runs under root) for a Bash report: whether the call's cwd is under root or its command
    names root (its path or its basename, as a word), and whether its command names the file: its absolute path
    as a word, or, when the call runs under root, its bare basename as a word or a name with a directory part
    that resolves to it from the call's cwd (or the `cd` dirs the command ends in). Outside root a relative name
    points at another file. Without a root or a recorded command: (True, True)."""
    if not root or 'cmd' not in op:
        return True, True
    root = os.path.abspath(root)
    cmd, path = op['cmd'] or '', os.path.abspath(op['path'])
    word = lambda w: bool(w) and re.search(r'(?<![\w.-])' + re.escape(w) + r'(?!\.?[\w-])', cmd) is not None
    cwd = os.path.abspath(op['cwd']) if op['cwd'] else ''
    under = cwd == root or cwd.startswith(root + os.sep) or root in cmd or word(os.path.basename(root))
    dirs = [cwd or root] + ([os.path.join(cwd or root, *d)] if (d := cds(cmd)) else [])
    def resolves(m):                    # the name the basename ends: bare, or a path from one of dirs
        name = m.group(1)
        return '/' not in name or any(os.path.normpath(os.path.join(x, name)) == path for x in dirs)
    base = os.path.basename(path)
    names = word(path) or under and bool(base) and any(
        resolves(m) for m in re.finditer(r'(?<![\w.~/-])([\w.~/-]*?' + re.escape(base) + r')(?!\.?[\w-])', cmd))
    return names, under

def _credit(ops, root=None):
    """A bashEditDiff change of a file that another transcript also changed while the Bash call ran (its tool
    call -> result window) is that transcript's: an Edit / Write at a time inside the window, or a Bash change
    whose own window is shorter and overlaps it. The Bash report is then ordered right after that change, so the
    change is applied and credited there and the report keeps only what is left (often nothing: no step). Such a
    report is marked `observer`: what is left of it is a step only when it applies (never `applied: false`).
    Before window length comes how surely each call made the change (_rank): a report of a call that neither names
    the file nor runs under / names root is `blind` (no step at all, and no credit), and a call that ranks higher
    takes the credit whatever the windows; equal ranks go to the shorter window."""
    rank = {id(op): _rank(op, root) for op in ops if op['op'] == 'patch'}
    for op in ops:
        if op['op'] == 'patch' and rank[id(op)] == (False, False):
            op['blind'] = True
    by = {}
    for op in ops:
        if op['op'] != 'see':
            by.setdefault(op['path'], []).append(op)
    for group in by.values():
        for x in group:
            if x['op'] != 'patch' or not x.get('start') or x.get('blind'):
                continue
            t0, t1 = x['start'], x['ts']
            late = ''
            for y in group:
                if y['src'] == x['src'] or y.get('blind'):
                    continue
                if y['op'] == 'patch' and y.get('start'):
                    y0, y1 = y['start'], y['ts']
                    rx, ry = rank[id(x)], rank[id(y)]
                    if y0 <= t1 and t0 <= y1 and (ry > rx or ry == rx and _span(y0, y1) < _span(t0, t1)):
                        late = max(late, y1)
                elif y['op'] in ('write', 'edit') and t0 <= y['ts'] <= t1:
                    late = max(late, y['ts'])
            if late:
                x['observer'] = True
            if late > x['ts']:
                x['ts'] = late
                x['key'] = (late, 1) + x['key'][2:]
    return sorted(ops, key=lambda op: op['key'])

def _span(a, b):
    from datetime import datetime
    f = lambda s: datetime.fromisoformat(s.replace('Z', '+00:00'))
    return (f(b) - f(a)).total_seconds()

def _utc(iso):
    """An ISO time with an offset -> the transcripts' form, 2026-01-01T00:00:00.000Z, so the two sort together."""
    from datetime import datetime, timezone
    return datetime.fromisoformat(iso).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')

def _git(root, *args, **kw):
    return subprocess.run(['git', '-C', root, *args], capture_output=True, **kw)

def git_tree(root, sha):
    """{relpath: text} of every file under root tracked in commit sha."""
    prefix = _git(root, 'rev-parse', '--show-prefix', text=True).stdout.strip()
    names = [n for n in _git(root, 'ls-tree', '-r', '-z', '--name-only', sha, text=True).stdout.split('\0') if n]
    if not names:
        return {}
    blob = _git(root, 'cat-file', '--batch', input=''.join(f'{sha}:{prefix}{n}\n' for n in names).encode()).stdout
    out, i = {}, 0
    for n in names:
        head, i = blob[i:blob.index(b'\n', i)].split(), blob.index(b'\n', i) + 1
        if head[-1] == b'missing':
            continue
        size = int(head[2])
        out[n] = blob[i:i + size].decode('utf-8', 'replace')
        i += size + 1
    return out

def git_commits(root, first, last):
    """Commits touching root authored from first on (a transcript timestamp; last is the last step's), up to HEAD:
    dicts with ts (author time, UTC), sha, subject, oldest first."""
    lines = _git(root, 'log', '--reverse', '--format=%H%x09%aI%x09%s', '--', '.', text=True).stdout.splitlines()
    out = []
    for ln in lines:
        sha, at, subject = ln.split('\t', 2)
        ts = _utc(at)
        if ts >= first:
            out.append({'ts': ts, 'sha': sha, 'subject': subject})
    return out

def steps(path, root=None, seed_git=False, git_reconcile=False):
    """path: a transcript, a directory or a list of them (see transcripts()) -> [Step], merged by timestamp."""
    files = transcripts(path)
    many = len(files) > 1 or git_reconcile      # merged (or with git steps): steps carry session and agent
    ops = []
    for n, f in enumerate(files):
        o, r = _ops(f, root, sidechain=is_subagent(f))
        root = root or r
        session, agent = who(f) if many else ('', '')
        for i, op in enumerate(o):
            op.update(key=(op['ts'], 0, n, i), session=session, agent=agent, src=n)
        ops += o
    ops.sort(key=lambda op: op['key'])
    first, kept, boxes = {}, [], {}     # a resumed session's file starts with copies of the earlier one's records: once each
    for op in ops:
        if op['id'][0] is not None and op['id'] in first:
            boxes[id(op['said'])] = first[op['id']]['said']   # the copy's text box is the original's: spent together
            continue
        if op['id'][0] is not None:
            first[op['id']] = op
        kept.append(op)
    for op in kept:
        box, n = op['said'], 0
        while id(box) in boxes and n < len(boxes):
            box, n = boxes[id(box)], n + 1
        op['said'] = box
    ops = kept
    ops = _credit(ops, root)
    under = lambda p: not root or os.path.abspath(p).startswith(os.path.abspath(root) + os.sep)
    times = [op['ts'] for op in ops if op['op'] != 'see' and under(op['path'])]
    if git_reconcile and root and times:
        commits = git_commits(root, times[0], times[-1])
        ops = sorted(ops + [{'ts': c['ts'], 'tool': 'git', 'path': os.path.join(root, ''), 'op': 'git', 'sha': c['sha'],
                             'turn': c['sha'], 'said': {'said': c['subject']}, 'ok': True, 'session': 'git',
                             'agent': 'git', 'src': 'git', 'key': (c['ts'], -1, -1, 0)} for c in commits],
                     key=lambda op: (op['key'][0], op['op'] != 'git', op['key'][1:]))   # a commit before its ts' steps
    tracked = None        # files the last git step's commit tracked
    snap, out, turns = {}, [], {}
    hist = {}             # relpath -> the texts it had, oldest first
    seen = {}             # relpath -> (its text as a whole-file Read or a blind report last showed it, the snapshot's then)
    for op in ops:
        if op['op'] == 'git':
            tree = git_tree(root, op['sha'])
            if tracked is None:           # the first commit: what its parent tracked
                tracked = set(_git(root, 'ls-tree', '-r', '-z', '--name-only', op['sha'] + '^', text=True).stdout.split('\0'))
            a = r = 0
            touched = False
            for rel in sorted(set(tree) | (tracked & set(snap))):
                before, after = snap.get(rel), tree.get(rel)
                if before == after:
                    continue
                touched = True
                da, dr = _diff_counts(before or '', after or '')
                a, r = a + da, r + dr
                if after is None:
                    snap.pop(rel)
                else:
                    snap[rel] = after
                    hist.setdefault(rel, []).append(after)
            tracked = set(tree)
            turn = turns.setdefault(('git', op['sha']), len(turns))
            out.append(Step(len(out), op['ts'], 'git', '', op['said']['said'], turn, a, r, True, dict(snap),
                            'git', 'git', True, False, 'git', not touched))
            continue
        p = op['path']
        if not under(p):
            continue
        rel = os.path.relpath(p, root) if root else p
        if op['op'] == 'see':
            seen[rel] = (op['text'], snap.get(rel))
            continue
        if op.get('blind'):     # a call that never pointed at the file or root only saw its change: no step, but
            # the change was made (by someone): like a Read, it shows the file for a later report that fits only it
            got = apply_hunks(snap[rel], op['hunks']) if rel in snap and not op['deleted'] else None
            if got not in (None, snap.get(rel)):
                seen[rel] = (got, snap[rel])
            continue
        before = snap.get(rel)
        seeded = False
        if before is None and seed_git and root and op['op'] != 'write' and not op.get('created'):
            before = git_base(root, rel, op['ts'])
            seeded = before is not None
        applied = True
        if op['op'] == 'write' and (op['ok'] or op['tool'] == 'Bash'):    # a failing command's heredoc still ran
            after = op['text']
        elif not op['ok'] and op['tool'] in ('Write', 'Edit', 'MultiEdit'):
            after, applied = before, False          # the tool reported an error: it changed nothing
        elif op['op'] == 'edit':
            base = before
            if op.get('orig') is not None and op['orig'] != before:
                base, seeded = op['orig'], True      # the file as the Edit tool saw it: the snapshot had drifted
            if base is None and op['old'] == '':
                after = op['new']
            elif base is not None and op['old'] in base:
                after = base.replace(op['old'], op['new']) if op['rall'] else base.replace(op['old'], op['new'], 1)
            elif before is not None and op.get('orig') is None and op.get('spatch') \
                    and (got := apply_hunks(before, op['spatch'])) is not None and got != before:
                after = got                          # no originalFile recorded: the Edit's own hunks, placed as diffs are
            else:
                after, applied = before, False
        elif op['deleted']:
            after = None
        else:
            got = apply_hunks('' if op['created'] or before is None else before, op['hunks']) \
                if before is not None or op['created'] else None
            if got is None and before is not None:
                # Claude Code diffs against the text it last saw of the file, which can be older than the file
                # was when the command ran: the hunks then fit an earlier version; what changed since is merged in.
                got = next((m for v in reversed(hist.get(rel, [])[:-1])
                            if (g := apply_hunks(v, op['hunks'], strict=True)) is not None
                            and (m := merge3(v, before, g)) is not None), None)
            if got is None and rel in seen and seen[rel][0] != before:
                text, then = seen[rel]                 # the file as a later Read showed it, and the snapshot then:
                got = apply_hunks(text, op['hunks'])   # what the snapshot changed since is merged in
                if got is not None and (then, before) != (None, None):
                    got = merge3(then, before, got) if then is not None and before is not None else None
                seeded = got is not None
            if got is None and op.get('observer'):
                continue                    # it only saw another transcript's change, which did not fit either
            if got is None:
                after, applied = before, False
            elif got == before:
                continue                    # done already (a concurrent call's report of the same change)
            else:
                after = got
        if applied:
            if after is None:
                snap.pop(rel, None)
            else:
                snap[rel] = after
                hist.setdefault(rel, []).append(after)
        a, r = _diff_counts(before or '', after or '') if applied else (0, 0)
        turn = op['turn'] if not many else turns.setdefault((op['src'], op['turn']), len(turns))
        box = op['said']                     # spent by the first call with a step: later calls of the text say nothing
        said = box['said'] if box.setdefault('by', op['tid']) == op['tid'] else ''
        out.append(Step(len(out), op['ts'], op['tool'], rel, said, turn, a, r, op['ok'], dict(snap),
                        op['session'], op['agent'], applied, seeded and applied))
    if seed_git and root and out:
        rest = untouched(root, out[-1].ts, {st.file for st in out})
        for st in out:
            if st.author == 'git':
                break                       # from the first commit on, the commits' trees hold those files
            st.content = {**rest, **st.content}
    return out

def untouched(root, ts, touched):
    """{relpath: text} of the files under root tracked in the last commit before ts that no step touches."""
    sha = subprocess.run(['git', '-C', root, 'log', '-1', '--format=%H', f'--before={ts}'], capture_output=True, text=True).stdout.strip()
    if not sha:
        return {}
    names = subprocess.run(['git', '-C', root, 'ls-tree', '-r', '-z', '--name-only', sha], capture_output=True, text=True).stdout
    out = {}
    for rel in filter(None, names.split('\0')):
        if rel not in touched:
            r = subprocess.run(['git', '-C', root, 'show', f'{sha}:./{rel}'], capture_output=True)
            if r.returncode == 0:
                out[rel] = r.stdout.decode('utf-8', 'replace')
    return out

def session_list(inputs, steps):
    """One entry per main session among the inputs with at least one step (its subagents' steps included):
    id (as steps' `session`), uuid, start / end (first and last timestamp of the session and its subagents),
    steps, prompt (first line of its first typed prompt, 120 chars)."""
    count = {}
    for st in steps:
        count[st.session] = count.get(st.session, 0) + 1
    out = []
    for f in transcripts(inputs):
        if is_subagent(f):
            continue
        uuid = os.path.splitext(os.path.basename(f))[0]
        if not count.get(uuid[:8]):
            continue
        ts = [o['timestamp'] for g in [f] + sorted(glob.glob(os.path.join(f[:-len('.jsonl')], 'subagents', '*.jsonl')))
              for o in load_lines(g) if isinstance(o.get('timestamp'), str) and o['timestamp']]
        out.append({'id': uuid[:8], 'uuid': uuid, 'start': min(ts, default=''), 'end': max(ts, default=''),
                    'steps': count[uuid[:8]], 'prompt': first_prompt(f)})
    return sorted(out, key=lambda s: s['start'])

def _diff_counts(a, b):
    import difflib
    al, bl = a.splitlines(), b.splitlines()
    sm = difflib.SequenceMatcher(None, al, bl, autojunk=False)
    add = rem = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ('replace', 'delete'):
            rem += i2 - i1
        if tag in ('replace', 'insert'):
            add += j2 - j1
    return add, rem

if __name__ == '__main__':
    args, root, i = [], None, 1
    while i < len(sys.argv):
        if sys.argv[i] == '--root':
            root, i = sys.argv[i + 1], i + 2
            continue
        if not sys.argv[i].startswith('--'):
            args.append(sys.argv[i])
        i += 1
    for s in steps(args if len(args) > 1 else args[0], root, seed_git='--seed-git' in sys.argv,
                   git_reconcile='--git-reconcile' in sys.argv):
        d = asdict(s); d.pop('content')
        d['files'] = len(s.content)
        print(json.dumps(d))
