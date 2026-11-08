"""Replay a session and write the per-step blob timeline the web page plays.

    python timeline.py <session.jsonl|project-dir> [more ...] [--root DIR] [-o web/timeline.json] [--no-timings]
                       [--full] [--v2] [--no-smart] [--seed-git] [--git-reconcile] [--sessions-out sessions.json]

Several transcripts and/or project directories are merged into one timeline (replay.steps(); a directory means
all its sessions with their subagents); their steps then carry `session` and `agent`. --seed-git seeds missing
bases and the files no step touches from root's git history, --git-reconcile adds a `git` step per commit of root
from the first step up to HEAD that resets the snapshot to the commit (replay.py); --sessions-out writes the session list.

Each step's state is the full blob/edge picture after that edit, with a `delta` per function and blob against
the previous step (see SCHEMA.md), and `stats` of the analysis. Non-code files are `file` / `text` blobs with a
whole-file delta. Parsed modules are cached by content hash, so a step only re-parses the file it changed, and
analysis is incremental (analyze(memo=...)): a function's call resolution, data symbols and snippet are reused
unless its module changed or a cross-module entry it looked up did. --full redoes all of it per step (same output).

The file is schema v5: step 0 (and any step marked "full") carries the whole state, every other step only what
changed, down to the functions of a re-sent blob and the entries of the areas table (compact() / expand() below).
Blobs are the analyzer's with small ones folded in and big ones cut (areas.py; --no-smart keeps them as analyzed,
for debugging and tests), each with its `area`. --v2 writes every step in full instead. --no-timings writes 0 for
parse_ms / analyze_ms, so two runs on the same session are byte-identical.
"""
import argparse, copy, difflib, gc, glob, json, operator, os, sys, time

import replay
from analyze import analyze, _is_binary
from areas import Zoner

def session_file(path):
    if os.path.isdir(path):
        return sorted(glob.glob(os.path.join(path, '*.jsonl')), key=os.path.getmtime)[-1]
    return path

def session_root(path):
    """The cwd replay.steps() falls back to: the first main-thread assistant message's."""
    for o in replay.load_lines(path):
        if o.get('type') == 'assistant' and not o.get('isSidechain') and o.get('cwd'):
            return o['cwd']
    return None

def _line_delta(old, new):
    add = rem = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old.splitlines(), new.splitlines(), autojunk=False).get_opcodes():
        if tag in ('replace', 'delete'):
            rem += i2 - i1
        if tag in ('replace', 'insert'):
            add += j2 - j1
    return add, rem

def _func_delta(f, body, prev, blob, prev_blob):
    """prev: qual -> (hash, body, lines) and prev_blob: qual -> blob id, both of the previous step."""
    old = prev.get(f['qual'])
    if old is None:
        return {'added': f['lines'], 'removed': 0, 'state': 'new'}
    if old[0] == f['hash']:
        moved = prev_blob.get(f['qual'], blob) != blob
        return {'added': 0, 'removed': 0, 'state': 'moved' if moved else 'same'}
    add, rem = _line_delta(old[1], body)
    return {'added': add, 'removed': rem, 'state': 'changed'}

ZERO = {'added': 0, 'removed': 0, 'state': 'same'}
BLOB_ZERO = {'added': 0, 'removed': 0, 'moved': 0, 'state': 'same', 'removed_funcs': []}

def _file_delta(path, text, prev_files):
    """Whole-file delta of a non-code file against the previous analysed step; a binary file counts no lines."""
    old = prev_files.get(path)
    state = 'new' if old is None else 'same' if old == text else 'changed'
    if state == 'same' or _is_binary(path, text):     # an unchanged file is never re-diffed
        return {'added': 0, 'removed': 0, 'moved': 0, 'state': state}
    add, rem = (len(text.splitlines()), 0) if old is None else _line_delta(old, text)
    return {'added': add, 'removed': rem, 'moved': 0, 'state': state}

def build_state(result, prev_funcs, prev_members, files=None, prev_files=None):
    """Schema-shaped blobs + edges for one analysis, with deltas against the previous analysed step.

    A function that kept its qual but sits in another blob than before has moved: it counts as
    `moved` lines (not added/removed) in both blobs. Lines leave a blob as `removed` only when the
    function is gone from the whole app; a function that moved and changed shows its diff in the new blob.
    files / prev_files: {relpath: text} of this and the previous analysed step, for file / text blobs."""
    bodies = result['bodies']
    prev_blob = {q: bid for bid, members in prev_members.items() for q in members}
    now = {f['qual'] for b in result['blobs'] for f in b['funcs']}
    blobs = []
    for b in result['blobs']:
        if b['kind'] in ('file', 'text'):
            blobs.append({'id': b['id'], 'name': b['name'], 'emoji': b['emoji'], 'kind': b['kind'], 'module': b['module'],
                          **({'area': b['area']} if 'area' in b else {}), 'path': b['path'], 'size': b['size'], 'complexity': 0, 'errors': 0, 'data': [],
                          'delta': dict(_file_delta(b['path'], (files or {}).get(b['path'], ''), prev_files or {}),
                                        removed_funcs=[]), 'funcs': []})
            continue
        funcs = []
        for f in b['funcs']:
            d = _func_delta(f, bodies[f['qual']], prev_funcs, b['id'], prev_blob)
            funcs.append({'qual': f['qual'], 'name': f['name'], 'file': f['file'], 'start': f['start'], 'end': f['end'],
                          'lines': f['lines'], 'branches': f['branches'], 'errors': f['errors'], 'hash': f['hash'],
                          'data': f['data'], 'snippet': f['snippet'], 'calls': f['calls'], 'refs': f['refs'], 'delta': d})
        members = {f['qual'] for f in funcs}
        gone = prev_members.get(b['id'], {}).keys() - members
        added = sum(f['delta']['added'] for f in funcs)
        removed = sum(f['delta']['removed'] for f in funcs) + sum(prev_members[b['id']][q] for q in gone if q not in now)
        moved = sum(f['lines'] for f in funcs if f['qual'] in prev_blob and prev_blob[f['qual']] != b['id']) + \
            sum(prev_members[b['id']][q] for q in gone if q in now)
        if b['id'] not in prev_members:
            state = 'new'
        elif added or removed or gone or any(f['delta']['state'] != 'same' for f in funcs):
            state = 'changed'
        else:
            state = 'same'
        blobs.append({'id': b['id'], 'name': b['name'], 'emoji': b['emoji'], 'kind': b['kind'], 'module': b['module'],
                      **({'depth': b['depth']} if 'depth' in b else {}), **({'cut': b['cut']} if 'cut' in b else {}),
                      **({'area': b['area']} if 'area' in b else {}),
                      'size': b['size'], 'complexity': b['complexity'], 'errors': b['errors'], 'data': b['data'],
                      **({'merged': b['merged']} if b.get('merged') else {}),
                      'delta': {'added': added, 'removed': removed, 'moved': moved, 'state': state,
                                'removed_funcs': sorted(q for q in gone if q not in now)}, 'funcs': funcs})
    edges = [{k: e[k] for k in ('a', 'b', 'kind', 'n', 'via') if k in e} for e in result['edges']]
    return blobs, edges

def unchanged(blobs):
    return [dict(b, delta=dict(BLOB_ZERO, removed_funcs=[]), funcs=[dict(f, delta=dict(ZERO)) for f in b['funcs']])
            for b in blobs]

META = ('idx', 'ts', 'tool', 'file', 'said', 'turn', 'added', 'removed', 'session', 'agent', 'applied', 'seeded',
        'author', 'empty')
EDGE_ORDER = {'main': 0, 'error': 1, 'data': 2, 'ref': 3}

def edge_key(e):
    """The order analyze() writes edges in, which expand() restores: kind, then a, then b."""
    return (EDGE_ORDER.get(e['kind'], len(EDGE_ORDER)), e['a'], e['b'])

def analyzed_steps(steps, full=False, timings=True, smart=True):
    """(replay.Step, {relpath: text}) pairs in order -> one full-state step dict per pair (schema v2 shape, plus
    the v5 areas). full: analyze every snapshot from scratch (no memo) instead of incrementally; the steps are the
    same. smart=False leaves the analyzer's blobs as they are (no merges, no cuts; areas still), for debugging."""
    an = Analyzer(full, timings, smart)
    try:
        for st, snap in steps:
            yield an.step(st, snap)
    finally:
        gc.unfreeze()

class Analyzer:
    """The analysis of a session's steps, one at a time: parse cache, memo (incremental analysis), the Zoner and
    the previous step's functions, blobs and files (for deltas). fork() is a copy to run ahead: analyze() and the
    Zoner replace what they change rather than write into it (the memo's tables, the previous step's state), so the
    copy shares the parse cache (keyed by content, never mutated) and the memo's tables and copies the few dicts
    that are written in place. evict() keeps only the parsed modules of the last snapshot."""

    def __init__(self, full=False, timings=True, smart=True):
        self.cache, self.memo, self.timings, self.zoner = {}, None if full else {}, timings, Zoner(smart)
        self.prev_funcs, self.prev_members = {}, {}   # qual -> (hash, body, lines); blob id -> {qual: lines}
        self.prev_files = {}                          # relpath -> text of the last analysed step
        self.blobs, self.edges, self.areas = [], [], []
        self.stats = {'calls': 0, 'unresolved': 0, 'parse_ms': 0, 'analyze_ms': 0}

    def fork(self):
        a = copy.copy(self)
        a.memo = dict(self.memo) if self.memo is not None else None
        a.zoner = copy.copy(self.zoner)
        a.zoner.cut_memo = dict(self.zoner.cut_memo)
        return a

    def evict(self):
        if self.memo is not None:
            live = {id(h[2]) for h in self.memo.get('texts', {}).values() if h[2] is not None}
            self.cache = {k: v for k, v in self.cache.items() if id(v) in live}

    def warm(self, snap):
        """Parse cache and memo from a snapshot (the last analysed one, after a restart): the next step is then
        analysed incrementally, with the same result as from scratch."""
        try:
            analyze(snap, self.cache, self.memo)
        except Exception:
            pass

    def __getstate__(self):
        d = dict(self.__dict__)
        d['cache'], d['memo'] = {}, None if self.memo is None else {}   # rebuilt by warm()
        return d

    def step(self, st, snap):
        try:
            result = analyze(snap, self.cache, self.memo)
        except Exception as e:   # a snapshot the analyzer cannot handle keeps the last good state
            print(f'step {st.idx} ({st.file}): analyze failed: {e!r}', file=sys.stderr)
            result = None
        if result is not None:
            result = self.zoner.apply(result)
            self.areas = result['areas']
            self.blobs, self.edges = build_state(result, self.prev_funcs, self.prev_members, snap, self.prev_files)
            self.prev_funcs = {f['qual']: (f['hash'], result['bodies'][f['qual']], f['lines'])
                               for b in result['blobs'] for f in b['funcs']}
            self.prev_members = {b['id']: {f['qual']: f['lines'] for f in b['funcs']} for b in result['blobs']}
            self.prev_files = snap
            self.stats = dict(result['stats'])
            if not self.timings:
                self.stats.update(parse_ms=0, analyze_ms=0)
            step_blobs = self.blobs
        else:
            step_blobs = unchanged(self.blobs)
        # Most of the heap lives for the whole session (parsed modules, memo, last state) and almost nothing a
        # step leaves behind is cyclic, so after each step the young generations are collected and everything
        # alive is frozen out of the cyclic GC's sight; otherwise full collections rescan it every step or two
        # (~100 ms each on a 580-file tree). Refcounting still frees what later steps drop.
        gc.collect(1)
        gc.freeze()
        return {'idx': st.idx, 'ts': st.ts, 'tool': st.tool, 'file': st.file, 'said': st.said,
                'turn': st.turn, 'added': st.added, 'removed': st.removed,
                **{k: getattr(st, k) for k in ('session', 'agent') if getattr(st, k, '')},
                **({} if getattr(st, 'applied', True) else {'applied': False}),
                **({'seeded': True} if getattr(st, 'seeded', False) else {}),
                **({'author': st.author} if getattr(st, 'session', '') else {}),   # merged / reconciled timelines
                **({'empty': True} if getattr(st, 'empty', False) else {}),
                'blobs': step_blobs, 'edges': self.edges, 'areas': self.areas, 'stats': self.stats}

def _zero(d):
    """A delta with every count 0, lists empty and state 'same' (the keys it has, in its order)."""
    return {k: 'same' if k == 'state' else [] if isinstance(v, list) else 0 for k, v in d.items()}

def rest(b):
    """Blob b one step later when nothing touched it: the same object with zeroed deltas."""
    if 'delta' not in b:
        return b
    return dict(b, delta=_zero(b['delta']), funcs=[dict(f, delta=_zero(f['delta'])) if 'delta' in f else f
                                                   for f in b.get('funcs', [])])

SCHEMA = 5
FUNC_PATCH = ('funcs_remove', 'funcs_order')

def _func_patch(b, base):
    """Blob b as a v4 delta step sends it: its `funcs` a patch on base's (the previous step's blob with zeroed
    deltas, None for a blob new to the state). A function of base that b no longer has is in `funcs_remove`; a new
    one is sent whole; a changed one as its qual plus the keys whose value differs (when its keys are base's, in
    order; otherwise it is removed and sent whole). `funcs_order` only when the order is not base's minus the
    removed ones with the new ones appended. None when a patch cannot say it (a qual twice, a function without a
    qual, b using the patch's key names): the step is then written in full."""
    if 'funcs' not in b:
        return b
    old, new = (base.get('funcs') or []) if base else [], b['funcs']
    if any(k in b for k in FUNC_PATCH) or not all(isinstance(f, dict) and isinstance(f.get('qual'), str)
                                                    for f in [*old, *new]):
        return None
    have, quals = {f['qual']: f for f in old}, [f['qual'] for f in new]
    now = set(quals)
    if len(have) != len(old) or len(now) != len(quals):
        return None
    gone, funcs = [q for q in have if q not in now], []
    for f in new:
        o = have.get(f['qual'])
        if o is None:
            funcs.append(f)
        elif o != f:
            if list(o) == list(f):
                funcs.append({'qual': f['qual'], **{k: v for k, v in f.items() if o[k] != v}})
            else:
                gone.append(f['qual'])
                funcs.append(f)
    left = [q for q in have if q not in set(gone)]
    kept = set(left)
    out = {}
    for k, v in b.items():
        if k != 'funcs':
            out[k] = v
            continue
        out['funcs'] = funcs
        if gone:
            out['funcs_remove'] = gone
        if left + [f['qual'] for f in funcs if f['qual'] not in kept] != quals:
            out['funcs_order'] = quals
    return out

def _apply_func_patch(sent, base):
    """The blob a v4 delta step's re-sent blob stands for (_func_patch() undone): a function the patch does not
    name is base's, with base's zeroed delta."""
    if 'funcs' not in sent:
        return sent
    fs = {f['qual']: f for f in ((base.get('funcs') or []) if base else [])}
    for q in sent.get('funcs_remove', ()):
        fs.pop(q, None)
    for f in sent['funcs']:
        o = fs.get(f['qual'])
        fs[f['qual']] = f if o is None else {**o, **f}
    b = dict(sent, funcs=[fs[q] for q in sent['funcs_order']] if 'funcs_order' in sent else list(fs.values()))
    for k in FUNC_PATCH:
        b.pop(k, None)
    return b

class Compactor:
    """Full-state steps in, schema v5 (or v4, v3) steps out (one at a time, so a writer can stream).

    A blob is left out of a step when it equals the previous step's blob with zeroed deltas (rest()): its delta
    says `same` and nothing else about it changed. A blob whose delta is `same` but whose other fields changed
    (a function's lines shifted, a call now resolves elsewhere) is sent again: in v3 in full, in v4 with its
    blob fields in full and its `funcs` as a patch on that zeroed previous blob (_func_patch()). `order` is
    written only when the blob order is not the previous one minus removed_blobs, with new ids appended as they
    come in `blobs`. A step whose edges are not in edge_key() order is written in full. A step's `areas` table
    (v5, any schema writes it when the steps have one) is whole on a full step; a delta step carries only the
    entries new or changed since the previous step (`areas`) and the ids gone (`areas_remove`), each only when not
    empty. A step whose table is not sorted by id is written in full."""

    def __init__(self, schema=SCHEMA):
        self.schema = schema
        self.rest, self.order, self.edges = None, [], {}     # id -> rest(blob); blob ids in order; key -> edge
        self.areas = None                                      # id -> areas entry of the previous step

    def push(self, step):
        meta = {k: step[k] for k in META if k in step}
        blobs, edges = step['blobs'], step['edges']
        keys = [(e['a'], e['b'], e['kind']) for e in edges]
        ranks = [edge_key(e) for e in edges]
        canonical = all(map(operator.lt, ranks, ranks[1:]))        # sorted, and no (a, b, kind) twice
        ids = [b['id'] for b in blobs]
        areas = step.get('areas')
        aids = [a['id'] for a in areas] if areas is not None else []
        canonical = canonical and all(map(operator.lt, aids, aids[1:])) and (areas is None) == (self.areas is None)
        sent = None
        if self.rest is not None and canonical and len(set(ids)) == len(ids):
            sent = [b for b in blobs if self.rest.get(b['id']) != b]
            if self.schema >= 4:
                sent = [_func_patch(b, self.rest.get(b['id'])) for b in sent]
                if any(b is None for b in sent):
                    sent = None
        if sent is None:
            out = dict(meta, full=True, blobs=blobs, edges=edges)
        else:
            now = set(ids)
            gone = [i for i in self.order if i not in now]
            have = set(self.order) - set(gone)
            expect = [i for i in self.order if i in now] + [b['id'] for b in sent if b['id'] not in have]
            new_edges = dict(zip(keys, edges))
            out = dict(meta, blobs=sent, removed_blobs=gone,
                       edges_add=[e for k, e in new_edges.items() if self.edges.get(k) != e],
                       edges_remove=[{'a': a, 'b': b, 'kind': k} for (a, b, k) in self.edges if (a, b, k) not in new_edges])
            if expect != ids:
                out['order'] = ids
            if areas is not None:
                now = {a['id']: a for a in areas}
                changed = [a for a in areas if self.areas.get(a['id']) != a]
                if changed:
                    out['areas'] = changed
                if self.areas.keys() - now.keys():
                    out['areas_remove'] = sorted(self.areas.keys() - now.keys())
        if out.get('full') and areas is not None:
            out['areas'] = areas
        if 'stats' in step:
            out['stats'] = step['stats']
        prev = self.rest or {}
        sent_ids = {b['id'] for b in (blobs if out.get('full') else out['blobs'])}
        self.rest = {b['id']: rest(b) if b['id'] in sent_ids else prev[b['id']] for b in blobs}
        self.order, self.edges = ids, {(e['a'], e['b'], e['kind']): e for e in edges}
        self.areas = {a['id']: a for a in areas} if areas is not None else None
        return out

def compact(steps, schema=SCHEMA):
    """Full-state steps -> schema v5 steps (v4 / v3 with schema=4 / 3: the same but for the function patches)."""
    c = Compactor(schema)
    return [c.push(s) for s in steps]

def expand(tl):
    """A timeline (dict with 'steps', or the list of steps) -> the list of full-state steps, each shaped as
    schema v2 writes them (META, blobs, edges, stats). A step is full when it is step 0, is marked `full`, or
    carries `edges` (v2 steps always do, v3 / v4 / v5 delta steps never do); a dict without "schema" >= 3 is all
    full. A delta step's blobs carry function patches when the dict says "schema" >= 4, or for a bare list of steps.
    The areas table (v5) is carried from step to step, patched by a delta step's `areas` / `areas_remove`; a file
    whose full steps have no `areas` (v3, v4) rebuilds steps without the key."""
    steps = tl['steps'] if isinstance(tl, dict) else tl
    old = isinstance(tl, dict) and tl.get('schema', 2) < 3
    v4 = not isinstance(tl, dict) or tl.get('schema', 2) >= 4
    out, cur, order, edges, areas = [], {}, [], {}, None
    for st in steps:
        if old or st.get('full') or 'edges' in st or not out:
            areas = {a['id']: a for a in st['areas']} if 'areas' in st else None
            cur = {b['id']: b for b in st['blobs']}
            order = [b['id'] for b in st['blobs']]
            fresh = set(cur)                    # ids whose blob in cur still carries this step's deltas
            edges = {(e['a'], e['b'], e['kind']): e for e in st['edges']}
            st_edges = list(st['edges'])
        else:
            gone = set(st.get('removed_blobs', ()))
            sent = {b['id']: b for b in st['blobs']}
            if v4:             # a patch's base: the blob as the previous state had it, deltas zeroed
                sent = {i: _apply_func_patch(b, (rest(cur[i]) if i in fresh else cur[i]) if i in cur else None)
                        for i, b in sent.items()}
            have = [i for i in order if i not in gone]
            cur = {i: sent[i] if i in sent else rest(cur[i]) if i in fresh else cur[i] for i in have}
            order = have + [i for i in sent if i not in cur]
            cur.update(sent)
            fresh = set(sent)
            if 'order' in st:
                order = list(st['order'])
            for e in st.get('edges_remove', ()):
                edges.pop((e['a'], e['b'], e['kind']), None)
            for e in st.get('edges_add', ()):
                edges[(e['a'], e['b'], e['kind'])] = e
            st_edges = sorted(edges.values(), key=edge_key)
            if areas is not None:
                for i in st.get('areas_remove', ()):
                    areas.pop(i, None)
                areas.update((a['id'], a) for a in st.get('areas', ()))
        step = {k: st[k] for k in META if k in st}
        step.update(blobs=[cur[i] for i in order], edges=st_edges)
        if areas is not None:
            step['areas'] = [areas[i] for i in sorted(areas)]
        if 'stats' in st:
            step['stats'] = st['stats']
        out.append(step)
    return out

def timeline(path, root=None, timings=True, full=False, v2=False, seed_git=False, steps=None, git_reconcile=False,
             smart=True):
    """The timeline dict for a session, or several merged (path: a transcript, a directory or a list of them, see
    replay.transcripts()): schema v5 (compacted) unless v2. steps: replay.steps() already run on path."""
    files = replay.transcripts(path)
    root = os.path.abspath(root or session_root(files[0]) or '.')
    rs = steps if steps is not None else replay.steps(files, root, seed_git=seed_git, git_reconcile=git_reconcile)
    ids = list(dict.fromkeys(st.session for st in rs if st.session and st.author != 'git'))
    session = ','.join(ids) if ids else os.path.splitext(os.path.basename(files[0]))[0]
    sessions = replay.session_list(files, rs) if len(ids) > 1 else []
    head = {'root': root, 'session': session, **({'sessions': sessions} if len(sessions) > 1 else {})}
    steps = analyzed_steps(((st, st.content) for st in rs), full=full, timings=timings, smart=smart)
    if v2:
        return {**head, 'steps': list(steps)}
    c = Compactor()
    return {**head, 'schema': SCHEMA, 'steps': [c.push(s) for s in steps]}

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('session', nargs='+', help='session .jsonl files and/or ~/.claude/projects/<dir>s (all sessions '
                    'with their subagents), merged by timestamp')
    ap.add_argument('--root', help='app root the edits are relative to (default: session cwd)')
    ap.add_argument('-o', '--out', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web', 'timeline.json'))
    ap.add_argument('--no-timings', action='store_true', help='write 0 for parse_ms / analyze_ms (byte-identical runs)')
    ap.add_argument('--full', action='store_true', help='analyze every step from scratch instead of incrementally')
    ap.add_argument('--v2', action='store_true', help='write every step with its full state (schema v2), for debugging')
    ap.add_argument('--no-smart', action='store_true', help="keep the analyzer's blobs (no merges, no cuts), for "
                    'debugging and tests')
    ap.add_argument('--seed-git', action='store_true', help="seed missing bases and untouched files from root's git history")
    ap.add_argument('--git-reconcile', action='store_true', help='a step per commit of root from the first step up to '
                    'HEAD, setting the snapshot to the commit')
    ap.add_argument('--sessions-out', help='also write the sessions with steps (id, start, end, steps, prompt) as JSON')
    ap.add_argument('--checkpoint', metavar='STORE', help='build through live.py and leave its store (e.g. live/NAME) '
                    'for `live.py run NAME` to continue; needs --no-timings (same output)')
    a = ap.parse_args()
    t0 = time.perf_counter()
    inputs = a.session if len(a.session) > 1 else a.session[0]
    files = replay.transcripts(inputs)
    if a.checkpoint:
        if not a.no_timings or a.full or a.v2:
            ap.error('--checkpoint needs --no-timings and takes no --full / --v2')
        import live
        if live.writer_pid(a.checkpoint) is not None:
            ap.error(f'a live writer runs on {a.checkpoint} (pid {live.writer_pid(a.checkpoint)}): stop it first')
        w = live.Writer(a.checkpoint, a.session, a.root, a.seed_git, a.git_reconcile, not a.no_smart)
        w.cold(time.time())
        tl = json.loads(live.export_text(a.checkpoint))
        rs = None
    else:
        rs = replay.steps(files, os.path.abspath(a.root or session_root(files[0]) or '.'), seed_git=a.seed_git,
                         git_reconcile=a.git_reconcile)
        tl = timeline(inputs, a.root, timings=not a.no_timings, full=a.full, v2=a.v2, steps=rs, smart=not a.no_smart)
    if a.sessions_out:
        with open(a.sessions_out, 'w', encoding='utf-8') as fh:
            json.dump(tl.get('sessions') or replay.session_list(files, rs if rs is not None else [
                replay.Step(0, '', '', '', '', 0, session=s.get('session', '')) for s in tl['steps']]),
                fh, ensure_ascii=False, indent=1)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, 'w', encoding='utf-8') as fh:
        json.dump(tl, fh, ensure_ascii=False, separators=(',', ':'))
    steps = expand(tl)
    last = steps[-1] if steps else {'blobs': [], 'edges': [], 'stats': {'calls': 0, 'unresolved': 0}}
    kinds = {k: sum(e['kind'] == k for e in last['edges']) for k in ('main', 'error', 'data', 'ref')}
    st = last['stats']
    parse = sum(s['stats']['parse_ms'] for s in steps)
    an = sum(s['stats']['analyze_ms'] for s in steps)
    print(f"{a.out}: {len(steps)} steps, {len(last['blobs'])} blobs and {len(last.get('areas', []))} areas at end, "
          f"edges main={kinds['main']} error={kinds['error']} data={kinds['data']} ref={kinds['ref']}, "
          f"{time.perf_counter() - t0:.2f}s (parse {parse:.0f}ms, analyze {an:.0f}ms), {os.path.getsize(a.out) / 1e6:.2f} MB\n"
          f"unresolved calls at end: {st['unresolved']}/{st['calls']} ({st['unresolved'] / max(st['calls'], 1):.1%})",
          file=sys.stderr)

if __name__ == '__main__':
    main()
