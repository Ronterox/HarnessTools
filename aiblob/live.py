"""Live map: follow Claude Code transcripts as they are written and keep the timeline up to date.

    python live.py run NAME INPUT [INPUT ...] [--root DIR] [--seed-git] [--git-reconcile] [--no-smart]
                   [--lag 60] [--idle 600] [--settle 60] [--poll 0.3] [--fresh] [--store DIR]
    python live.py export NAME [-o FILE] [--diffs DIR] [--store DIR]   committed steps + provisional tail -> a
                                                         timeline.json (and its diffs layout, as timeline.py --diffs)
    python live.py verify NAME [--store DIR]             a batch build of the same inputs compared with the store
    python live.py stop NAME [--store DIR]               SIGTERM to the writer (live.pid: its pid and start time)

INPUTs are what timeline.py takes (transcripts and/or ~/.claude/projects/<dir>s). The store is live/NAME/ next to
this file unless --store says otherwise:
- head.json: inputs, root, flags; rest.json: with --seed-git, the untouched files frozen at the cold build.
- steps.ndjson: committed steps, one per line, exactly as timeline.py --no-timings writes them (schema v5).
- provisional.json: {"gen", "base", "late", "w", "steps"}: the steps a batch build would add after the committed
  `base` ones if the transcripts ended now, as deltas continuing steps.ndjson; rewritten atomically when it changes
  (`gen` counts the versions; it starts over at 1 after a rebuild, which status.json's `rebuilds` counts).
- checkpoint.pkl: a JSON header line (version, code hash, inputs and flags, committed steps, hash of the last one),
  checked before anything is unpickled, then the pickle: engine, analyzer (no parse cache: warmed from the last
  snapshot on load), compactor, tailer. Written only after a tick that ended (an exception or ^C in the middle of
  one leaves the previous checkpoint, and steps.ndjson is cut back to it on restart). A checkpoint that does not
  load, or does not match the code, the flags, steps.ndjson or the transcripts before their offsets, means a cold
  build.
- status.json; live.pid (`pid start-time`: a pid now used by another process is no writer; timeline.py
  --checkpoint refuses a store whose writer runs).
- diffs/ (diffs.py, SCHEMA.md "Diffs"): bodies.ndjson, the texts the steps show by hash, append-only (it outlives
  rebuilds and restarts; its hashes are the writer's seen set); steps.ndjson, the file line of each committed step
  (cut back and rebuilt with steps.ndjson, and checked with it at a warm start); tail.json {"tails": [{base, h, fv}]}:
  the provisional steps' file lines, for the last two provisional.json versions (h: diffs.tail_digest() of their
  steps), written before provisional.json. Order: the new texts are appended to bodies.ndjson before any line that
  names them is written, and a step's file line before the step (steps.ndjson is cut or truncated before
  diffs/steps.ndjson), so a reader that sees a step finds its file line and every text it names.

Committed vs provisional (plan decision 4b). Steps are committed by replay.Engine under the watermark
w = now - lag: every record a transcript holds with an earlier time is assumed written by then (phase 0 measured
writes up to 38 s late, so the default lag is 60 s). Committed steps are the batch build's, byte for byte, and never
change. Everything read but not committed (held calls, reports whose credit waits for a running Bash call, the last
`lag` seconds) is the provisional tail: Engine.fork().finish(), analysed on forks of the analyzer and compactor,
i.e. what a batch build would give if the transcripts ended now. It is redone only when the engine was told
something new (Engine.version), and then cheaply: the fork's Merger replays the previous run's applies while the ops
match (Merger.memo), and the analysis of a tail step is reused while the steps before it match. A tail step that
later commits unchanged is adopted with its analysis (nothing is analysed twice). When the promise breaks (Engine.late: a record older than an
earlier watermark, a result after its call was taken as over or after its transcript was closed, a subagent's tags
changing after its first step), a transcript shrinks or is replaced, a new transcript would take an earlier one's
place in transcripts() order, a commit taken leaves the history (amend, reset, rebase), or a second transcript
appears where `many` was off (one transcript, no --git-reconcile: steps carry no session tags; transcripts are counted
as transcripts() counts them, those without a timestamp too), the store is rebuilt from scratch (a cold build).

Liveness. A call without a result holds what sorts after it (a Bash call cut off by a killed session, an Edit
waiting for a permission prompt; the tail shows the steps meanwhile). A transcript's open calls are closed (as a
batch build closes them at the end of the file) once it has not grown for --idle seconds (by this writer's clock,
from when it found the file: mtimes are never used) and its session is not
running (~/.claude/sessions/<pid>.json with that pid alive), again whenever it grew and left another call open;
a result that still comes for a call closed that way forces a rebuild.

Other rules: transcripts are numbered by their place in transcripts() order: (input, first timestamp, place among
transcripts with that same first timestamp, subagent name); that order is transcripts()', which looks at content
only, never at mtimes, and so does live. A cold build numbers every transcript there is. Later, a new transcript
holding only records of a followed one with the same first timestamp, and seen growing in the last --settle seconds
(60), is a resumed session still writing its copies: it waits until it has a record of its own or stops growing.
One that would sort before a numbered transcript with the same first timestamp, or a change of their order while one
of them still has nothing of its own, rebuilds (a plain copy of a transcript, a resume that stopped mid-copy). A subagent's tags are read when its first step is made
(its meta.json comes after its transcript starts); git commits (--git-reconcile) are polled every 10 s (and
every commit taken must still be in the history); with
--seed-git the untouched files are frozen at the cold build (verify uses the same set). Memory: applied ops no report
can still look at, finished calls' tables, parse-cache entries not in the last snapshot are dropped, the history
keeps 16 texts per file (replay.HIST_KEEP), and the GC is unfrozen and run every 10 minutes."""
import argparse, collections, copy, functools, gc, glob, hashlib, json, os, pickle, signal, sys, time
from datetime import datetime, timezone
from types import SimpleNamespace

import diffs, replay, timeline

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = ('replay.py', 'analyze.py', 'areas.py', 'timeline.py', 'live.py', 'lang_c.py', 'lang_go.py', 'lang_js.py',
        'diffs.py')
VERSION = 2


class Restart(Exception):
    """The store cannot go on exactly: build it again from scratch."""


def iso(t):
    """Seconds since the epoch -> the transcripts' time form, 2026-01-01T00:00:00.000Z."""
    return datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def dumps(x):
    return json.dumps(x, ensure_ascii=False, separators=(',', ':'))


def code_hash():
    h = hashlib.sha1()
    for f in CODE:
        with open(os.path.join(HERE, f), 'rb') as fh:
            h.update(fh.read())
    return h.hexdigest()


def write_atomic(path, text):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        fh.write(text)
    os.replace(tmp, path)


def session_of(path):
    """The full session id a transcript belongs to (a subagent's is its parent session's)."""
    if replay.is_subagent(path):
        return os.path.basename(os.path.dirname(os.path.dirname(path)))
    return os.path.splitext(os.path.basename(path))[0]


def proc_start(pid):
    """The start time of process pid (/proc/<pid>/stat field 22), None when there is none."""
    try:
        with open(f'/proc/{int(pid)}/stat') as fh:
            return fh.read().rsplit(')', 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def writer_pid(store):
    """The pid of the writer running on store, None when none is (live.pid holds `pid start-time`; a pid that now
    belongs to another process does not count)."""
    try:
        with open(os.path.join(store, 'live.pid')) as fh:
            pid, start = fh.read().split()
        pid = int(pid)
    except (OSError, ValueError):
        return None
    return pid if proc_start(pid) == start else None


def sessions_alive():
    """Session ids of the Claude Code processes running now (~/.claude/sessions/<pid>.json whose pid is alive and
    started when the file says)."""
    out = set()
    for f in glob.glob(os.path.expanduser('~/.claude/sessions/*.json')):
        try:
            with open(f, encoding='utf-8') as fh:
                d = json.load(fh)
            with open(f"/proc/{int(d['pid'])}/stat") as fh:
                start = fh.read().rsplit(')', 1)[1].split()[19]
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            continue
        if str(d.get('procStart', start)) == start and d.get('sessionId'):
            out.add(d['sessionId'])
    return out


class Tailer:
    """The inputs' transcripts as they grow, the way aic/aic/watch.py reads them: a byte offset per file, a line
    counts once its newline came (replay.Lines), a file that shrinks or changes inode cannot be followed. Each file
    gets its place in transcripts() order as its rank."""

    def __init__(self, inputs, settle=60.0):
        self.inputs, self.settle = inputs, settle
        self.files = {}         # path -> {rank, off, ino, lines, grown / grew (times this writer saw it grow)}
        self.waiting = []       # transcripts found but not followed yet (a resumed copy still copying)
        self.sizes = {}         # transcript not followed yet -> (its size, the time it was first seen at that size)
        self.untimed = []       # main transcripts with no timestamp (yet): transcripts() lists them, nothing to read
        self.ties = {}          # followed transcripts with the same first timestamp -> (their sizes, settled)

    def discover(self, now, cold=False):
        """The transcripts not followed yet, [(rank, path)] in rank order, as transcripts() orders them (by content,
        never by mtime). A main transcript waits until it has a timestamp (where it sorts is not known before). A
        new transcript that holds only records of a followed one with the same first timestamp and grew in the last
        `settle` seconds (as this writer saw it: in a cold build only ones a rebuild carried over from the tailer
        before) is a resumed session still writing its copies: it waits until it has a record of its own or stops
        growing. Restart when a new
        one would sort before one already followed with that first timestamp."""
        new, self.waiting, self.untimed = [], [], []
        for i, p in enumerate(self.inputs):
            if not os.path.isdir(p):
                if p not in self.files and os.path.exists(p):
                    new.append(((i, '', 0, 0, ''), p))
                continue
            first = {}
            for f in glob.glob(os.path.join(p, '*.jsonl')):
                first[f] = self.files[f]['rank'][1] if f in self.files else replay.first_ts(f)
            mains, cand = [], {}
            for f, ft in first.items():
                if not ft:
                    self.untimed.append(f)
                    continue
                if f in self.files:
                    mains.append((self.files[f]['rank'], f))
                    continue
                group = [g for g, t in first.items() if t == ft]
                grows = (not cold or f in self.sizes) and self._growing(f, now)
                if grows and self._copy_so_far(f, [g for g in group if g != f and (cold or g in self.files)]):
                    self.waiting.append(f)
                    continue
                self.sizes.pop(f, None)
                cand.setdefault(ft, []).append(f)
            for ft, fs in cand.items():
                # new ones go after the transcripts with that first timestamp already numbered (ones still waiting
                # do not count): sorting before one could change what that one's applied ops were credited
                known = sorted((self.files[g]['rank'][2], g) for g, t in first.items() if t == ft and g in self.files)
                order = sorted([g for _, g in known] + fs, key=functools.cmp_to_key(replay._earlier))
                if order[:len(known)] != [g for _, g in known]:
                    raise Restart(f'order: {order[0]} sorts before {known[0][1]}')
                base = known[-1][0] + 1 if known else 0
                for k, f in enumerate(order[len(known):]):
                    rank = (i, ft, base + k, 0, '')
                    new.append((rank, f))
                    mains.append((rank, f))
            self._recheck_ties(mains)
            for rank, f in mains:
                for s in glob.glob(os.path.join(f[:-len('.jsonl')], 'subagents', '*.jsonl')):
                    if s not in self.files:
                        new.append((rank[:3] + (1, os.path.basename(s)), s))
        new.sort()
        for rank, f in new:
            st = os.stat(f)
            self.files[f] = {'rank': rank, 'off': 0, 'ino': st.st_ino, 'lines': replay.Lines(),
                             'grown': now}         # (idle counts from when it was found, not mtime)
        return new

    def _recheck_ties(self, mains):
        """Transcripts followed with the same first timestamp sort by what each has that the other has not
        (replay._earlier). While one of them has nothing of its own (a resumed session's file still copying, which a
        cold build may have numbered first), that order can still flip: it is checked again whenever one of them grew,
        and a flip means Restart. Once each has a record of its own the order holds."""
        groups = {}
        for rank, f in mains:
            groups.setdefault(rank[:2], []).append((rank, f))
        for key, g in groups.items():
            if len(g) < 2:
                continue
            paths = tuple(sorted(f for _, f in g))
            sizes = tuple(os.path.getsize(f) for f in paths)
            seen = self.ties.get(paths)
            if seen and (seen[1] or seen[0] == sizes):
                continue
            ranked = [f for _, f in sorted(g)]
            if sorted(ranked, key=functools.cmp_to_key(replay._earlier)) != ranked:
                raise Restart(f'order: {ranked[0]} no longer sorts first among transcripts started together')
            settled = not any(self._copy_so_far(f, [x for x in paths if x != f]) for f in paths)
            self.ties[paths] = (sizes, settled)

    def count(self):
        """How many transcripts batch's transcripts() lists now (followed, waiting or without a timestamp)."""
        return len(self.files) + len(self.waiting) + len(self.untimed)

    def _growing(self, f, now):
        size = os.path.getsize(f)
        seen = self.sizes.get(f)
        if seen is None or seen[0] != size:
            self.sizes[f] = seen = (size, now)
        return now - seen[1] < self.settle

    def _copy_so_far(self, f, group):
        """Whether every record f has is also in a transcript already followed with the same first timestamp."""
        if not group:
            return False
        mine = {o for o, _ in replay._records(f)}
        return any(mine <= {o for o, _ in replay._records(g)} for g in group)

    def read(self, path, now, chunk=8 << 20):
        """The new complete records of path (read 8 MB at a time)."""
        info = self.files[path]
        try:
            st = os.stat(path)
        except FileNotFoundError:
            raise Restart(f'{path} is gone')
        if st.st_ino != info['ino'] or st.st_size < info['off']:
            raise Restart(f'{path} was replaced or cut')
        if st.st_size == info['off']:
            return
        if info['off']:
            info['grown'] = info['grew'] = now      # (the first read keeps the time it was found with; `grew`
            #                                          is growth this writer saw, never an mtime)
        with open(path, 'rb') as fh:
            fh.seek(info['off'])
            while info['off'] < st.st_size:
                data = fh.read(min(chunk, st.st_size - info['off']))
                if not data:
                    break
                info['off'] += len(data)
                yield from info['lines'].feed(data)

    def tail_hash(self, path):
        info = self.files[path]
        with open(path, 'rb') as fh:
            fh.seek(max(0, info['off'] - 4096))
            return hashlib.sha1(fh.read(info['off'] - max(0, info['off'] - 4096))).hexdigest()


class Entry:
    """One provisional step: the replay step it came from, its line and file line, the analyzer and compactor
    after it."""
    __slots__ = ('meta', 'content', 'before', 'line', 'fv', 'analyzer', 'compactor', 'session', 'author')

    def __init__(self, st, line, fv, analyzer, compactor):
        self.meta, self.content, self.before = _meta(st), st.content, st.before
        self.line, self.fv, self.analyzer, self.compactor = line, fv, analyzer, compactor
        self.session, self.author = st.session, st.author

    def matches(self, st):
        return self.meta == _meta(st) and self.content == st.content and self.before == st.before


def _meta(st):
    return (st.idx, st.ts, st.tool, st.file, st.said, st.turn, st.added, st.removed, st.ok, st.session, st.agent,
            st.applied, st.seeded, st.author, st.empty)


class Writer:
    """The live store of NAME: start() (warm from the checkpoint when it is still valid, else a cold build), then
    tick(now) as often as wanted; each tick reads what the transcripts gained, commits what the watermark allows,
    and rewrites the provisional tail when anything changed."""

    def __init__(self, store, inputs, root=None, seed_git=False, git_reconcile=False, smart=True, lag=60.0,
                 idle=600.0, alive=sessions_alive, git_poll=10.0, scan=2.0, checkpoint_every=30.0, evict_every=30.0,
                 gc_every=600.0, prov_every=1.0, settle=60.0):
        self.store, self.settle = store, settle
        self.args = {'inputs': [os.path.abspath(p) for p in inputs], 'root': os.path.abspath(root) if root else None,
                     'seed_git': seed_git, 'git_reconcile': git_reconcile, 'smart': smart}
        self.lag, self.idle, self.alive, self.git_poll, self.scan = lag, idle, alive, git_poll, scan
        self.every = {'checkpoint': checkpoint_every, 'evict': evict_every, 'gc': gc_every}
        self.engine, self.stale, self.prov_every, self.clean = None, False, prov_every, False
        self.rebuilds, self.reasons, self.cold_reason = 0, [], ''
        os.makedirs(self.path('diffs'), exist_ok=True)
        open(self.path('diffs/steps.ndjson'), 'a').close()
        self.sink = diffs.BodySink(self.path('diffs/bodies.ndjson'))

    path = lambda self, name: os.path.join(self.store, name)

    # -- start -----------------------------------------------------------------------------------------------
    def start(self, now, fresh=False):
        """Warm from the checkpoint if it is still valid, else a cold build. A writer is unclean (checkpoint() writes
        nothing) from its creation until one of them has ended, and during each tick, so a ^C in the middle of
        either leaves no checkpoint behind."""
        if not fresh and self._warm(now):
            return 'warm'
        self.cold(now)
        return 'cold'

    def cold(self, now):
        """Build the store from scratch from what the transcripts hold now."""
        old = getattr(self, 'tailer', None)
        self.tailer = Tailer(self.args['inputs'], self.settle)
        if old is not None:                 # a rebuild: what it saw growing still counts (a resume mid-copy waits)
            self.tailer.sizes = dict(old.sizes)
            for p, i in old.files.items():  # (bytes not read yet: it grew just now)
                grew = now if os.path.exists(p) and os.path.getsize(p) > i['off'] else i.get('grew')
                if grew is not None:
                    self.tailer.sizes[p] = (i['off'], grew)
        self.engine, self.tail, self.prov_lines, self.gen, self.n, self.tails = None, [], None, 0, 0, []
        self.prov_version, self.prov_base = -1, -1
        self.analyzer, self.compactor = timeline.Analyzer(timings=False, smart=self.args['smart']), timeline.Compactor()
        self.analyzer.sink = self.sink
        self.counts = {}            # session -> committed steps (git ones too), in order of first step
        self.last = {'scan': now, 'git': now, 'checkpoint': now, 'evict': now, 'gc': now}
        self.dirty = True
        with open(self.path('steps.ndjson'), 'w'):
            pass
        with open(self.path('diffs/steps.ndjson'), 'w'):       # (after steps.ndjson: never fewer lines than it)
            pass
        new = self.tailer.discover(now, cold=True)
        if not new:
            self._head(None, False)
            return
        files = [p for _, p in new]
        a = self.args
        listed = replay.transcripts(a['inputs'] if len(a['inputs']) > 1 else a['inputs'][0])
        self.root = os.path.abspath(a['root'] or timeline.session_root(listed[0]) or '.')
        self.many = self.tailer.count() > 1 or a['git_reconcile']
        rest = None
        if a['seed_git']:
            got = replay.steps(files, self.root, seed_git=True, git_reconcile=a['git_reconcile'])
            rest = replay.untouched(self.root, got[-1].ts if got else iso(now), {st.file for st in got})
            write_atomic(self.path('rest.json'), dumps(rest))
        self.engine = replay.Engine(self.root, a['seed_git'], a['git_reconcile'], self.many, keep=False, rest=rest)
        self._head(self.root, self.many)
        for rank, p in new:
            self.engine.add(rank, p)
            for o in self.tailer.read(p, now):
                self.engine.feed(rank, o)
        self._advance(now)
        self.clean = True
        self.checkpoint(now)

    def _head(self, root, many):
        write_atomic(self.path('head.json'), dumps(dict(self.args, root=root, many=many, schema=timeline.SCHEMA)))

    # -- running -----------------------------------------------------------------------------------------------
    def tick(self, now):
        """Read, commit, rewrite the tail. True when the store changed. A tick that does not end (an exception, ^C)
        leaves the writer unclean: checkpoint() then writes nothing, and a restart continues from the last
        checkpoint (steps.ndjson cut back to it)."""
        self.clean = False
        try:
            if self.engine is None:
                self.cold(now)
                changed = self.engine is not None
            else:
                changed = self._tick(now)
        except Restart as e:
            print(f'live: rebuilding ({e})', file=sys.stderr)
            self.rebuilds += 1
            self.reasons.append(str(e))
            self.cold(now)
            changed = True
        self.clean = True
        if self.engine is not None and now - self.last['checkpoint'] >= self.every['checkpoint'] and self.dirty:
            self.checkpoint(now)
        return changed

    def _tick(self, now):
        eng = self.engine
        if now - self.last['scan'] >= self.scan:
            self.last['scan'] = now
            new = self.tailer.discover(now)
            if not self.many and self.tailer.count() > 1:
                raise Restart('a second transcript: steps now carry session tags')
            for rank, p in new:
                eng.add(rank, p)
        for p, info in sorted(self.tailer.files.items(), key=lambda kv: kv[1]['rank']):
            for o in self.tailer.read(p, now):
                eng.feed(info['rank'], o)
            n = info['rank']
            if replay.is_subagent(p) and n not in eng.started:
                eng.retag(n)                # (its meta.json read again until its first step: content, not mtime)
            if eng.readers[n].open and now - info['grown'] >= self.idle \
                    and session_of(p) not in self._alive():
                eng.close(n)
        self._alive_cache = None
        if eng.git_reconcile and eng.commits is not None and now - self.last['git'] >= self.git_poll:
            self.last['git'] = now
            commits = replay.git_commits(eng.root, '', '')
            if eng.commits - {c['sha'] for c in commits}:
                raise Restart('a commit taken left the history (amend, reset or rebase)')
            eng.add_commits(commits)
        changed = self._advance(now)
        if now - self.last['evict'] >= self.every['evict']:
            self.last['evict'] = now
            eng.evict(eng.w)
            self.analyzer.evict()
        if now - self.last['gc'] >= self.every['gc']:
            self.last['gc'] = now
            gc.unfreeze()
            gc.collect()
        return changed

    _alive_cache = None

    def _alive(self):
        if self._alive_cache is None:
            self._alive_cache = self.alive()
        return self._alive_cache

    def _advance(self, now):
        got = self.engine.drain(iso(now - self.lag))
        if self.engine.late:                # (before anything drained is written)
            raise Restart(f'late: {self.engine.late} records broke the watermark')
        if got:
            with open(self.path('steps.ndjson'), 'a', encoding='utf-8') as fh, \
                    open(self.path('diffs/steps.ndjson'), 'a', encoding='utf-8') as fd:
                for st in got:
                    line, fv = self._commit(st)
                    self.sink.flush()                   # texts, then the file line, then the step
                    fd.write(fv + '\n')
                    fd.flush()
                    fh.write(line + '\n')
            self.dirty = True
        # the tail is fork().finish(): only what the engine was told since (or a commit it did not foresee) changes it
        self.stale = self.stale or self.engine.version != self.prov_version
        if self.prov_lines is None or self.stale and now - self.last.get('prov', 0) >= self.prov_every:
            self.last['prov'] = now
            return self.provisional(now) or bool(got)
        if got:
            self._write_tail(now)           # the steps it foresaw are committed: what is left of it
        return bool(got)

    def _commit(self, st):
        if self.tail and self.tail[0].matches(st):
            e = self.tail.pop(0)                    # the tail was right: its analysis is this step's
            self.analyzer, self.compactor, line, fv = e.analyzer, e.compactor, e.line, e.fv
        else:
            self.tail, self.stale = [], True
            line = dumps(self.compactor.push(self.analyzer.step(st, st.content)))
            fv = dumps(self.analyzer.fv)
        self.n += 1
        self.counts[st.session] = self.counts.get(st.session, 0) + 1
        return line, fv

    def provisional(self, now):
        """Rewrite provisional.json from a fork of the engine run to the end; True when it changed."""
        self.prov_version, self.stale = self.engine.version, False
        f = self.engine.fork()
        m = self.engine.merger
        f.merger.memo, f.merger.trace = collections.deque(m.memo or ()), []
        steps = f.finish()
        m.memo = collections.deque(f.merger.trace)  # the committed merger consumes it as it applies the same ops
        keep = 0
        while keep < len(steps) and keep < len(self.tail) and self.tail[keep].matches(steps[keep]):
            keep += 1
        tail = self.tail[:keep]
        a, c = (tail[-1].analyzer, tail[-1].compactor) if tail else (self.analyzer, self.compactor)
        a, c = a.fork(), copy.copy(c)
        for st in steps[keep:]:
            line = dumps(c.push(a.step(st, st.content)))
            tail.append(Entry(st, line, dumps(a.fv), a.fork(), copy.copy(c)))
        self.tail = tail
        return self._write_tail(now)

    def _write_tail(self, now):
        lines = [e.line for e in self.tail]
        if lines == self.prov_lines and self.n == self.prov_base:
            return False
        self.prov_lines, self.prov_base = lines, self.n
        self.gen += 1
        self.sink.flush()
        self.tails = [f'{{"base":{self.n},"h":"{diffs.tail_digest(lines)}","fv":[{",".join(e.fv for e in self.tail)}]}}'] \
            + self.tails[:1]
        write_atomic(self.path('diffs/tail.json'), f'{{"tails":[{",".join(self.tails)}]}}')
        write_atomic(self.path('provisional.json'),
                     f'{{"gen":{self.gen},"base":{self.n},"late":{self.engine.late},"w":{dumps(self.engine.w)},'
                     f'"steps":[{",".join(lines)}]}}')
        self._status(now)
        return True

    def _status(self, now):
        write_atomic(self.path('status.json'), dumps({
            'pid': os.getpid(), 'time': iso(now), 'steps': self.n, 'provisional': len(self.tail), 'gen': self.gen,
            'w': self.engine.w if self.engine else '', 'late': self.engine.late if self.engine else 0,
            'rebuilds': self.rebuilds, 'files': len(self.tailer.files)}))

    # -- checkpoint ---------------------------------------------------------------------------------------------
    def checkpoint(self, now):
        """Save the state after the last tick that ended (nothing after one that did not)."""
        if self.engine is None or not self.clean:
            return
        last = _last_line(self.path('steps.ndjson'))
        dlast = _last_line(self.path('diffs/steps.ndjson'))
        head = {'version': VERSION, 'code': code_hash(), 'args': self.args, 'steps': self.n,
                'last': hashlib.sha1(last.encode()).hexdigest() if last is not None else None,
                'dlast': hashlib.sha1(dlast.encode()).hexdigest() if dlast is not None else None}
        state = {'engine': self.engine, 'analyzer': self.analyzer, 'compactor': self.compactor,
                 'files': {p: dict(i, hash=self.tailer.tail_hash(p)) for p, i in self.tailer.files.items()},
                 'root': self.root, 'many': self.many, 'counts': self.counts, 'rebuilds': self.rebuilds}
        tmp = self.path('checkpoint.pkl.tmp')
        with open(tmp, 'wb') as fh:
            fh.write((dumps(head) + '\n').encode())
            pickle.dump(state, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, self.path('checkpoint.pkl'))
        self.last['checkpoint'] = now
        self.dirty = False

    def _warm(self, now):
        why = self._why_cold()
        if why:
            self.cold_reason = why
            return False
        st, tailer = self._loaded
        self._loaded = None
        self.tailer, self.engine, self.analyzer, self.compactor = tailer, st['engine'], st['analyzer'], st['compactor']
        self.root, self.many, self.counts, self.rebuilds = st['root'], st['many'], st['counts'], st['rebuilds']
        self.n, self.tail, self.prov_lines, self.gen, self.dirty = st['steps'], [], None, 0, False
        self.analyzer.sink, self.tails = self.sink, []
        self.prov_version, self.prov_base = -1, -1
        self.last = {'scan': now - self.scan, 'git': now, 'checkpoint': now, 'evict': now, 'gc': now}
        self.analyzer.warm(self.analyzer.prev_files)
        self.tick(now)
        return True

    def _why_cold(self):
        """Why the checkpoint cannot be continued ('' when it can; then self._loaded holds it). Its header line
        (version, code hash, inputs and flags, steps) is checked before the pickle is loaded."""
        try:
            fh = open(self.path('checkpoint.pkl'), 'rb')
        except OSError:
            return 'no checkpoint'
        with fh:
            try:
                head = json.loads(fh.readline())
            except ValueError:
                return 'no checkpoint header'
            if not isinstance(head, dict) or head.get('version') != VERSION or head.get('code') != code_hash():
                return 'the code changed'
            if head.get('args') != self.args:
                return 'other inputs or flags'
            try:
                st = dict(pickle.load(fh), steps=head['steps'], last=head['last'])
            except Exception as e:          # whatever a damaged or foreign pickle raises
                return f'the checkpoint does not load ({e.__class__.__name__})'
        if _count_lines(self.path('steps.ndjson'), st['steps'], st['last']) is None:
            return 'steps.ndjson does not match'
        if _count_lines(self.path('diffs/steps.ndjson'), st['steps'], head.get('dlast')) is None:
            return 'diffs/steps.ndjson does not match'
        tailer = Tailer(self.args['inputs'], self.settle)
        for p, info in st['files'].items():
            try:
                s = os.stat(p)
            except OSError:
                return f'{p} is gone'
            if s.st_ino != info['ino'] or s.st_size < info['off']:
                return f'{p} was replaced or cut'
            tailer.files[p] = {k: v for k, v in info.items() if k != 'hash'}
            if tailer.tail_hash(p) != info['hash']:
                return f'{p} changed before its offset'
        self._loaded = (st, tailer)
        return ''


def _last_line(path):
    last = None
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            last = line.rstrip('\n')
    return last


def _count_lines(path, n, digest):
    """Keep the first n lines of path when the n-th hashes to digest (a crash after an append, before the
    checkpoint, leaves more); None when they do not match."""
    try:
        with open(path, 'rb') as fh:
            data = fh.read()
    except OSError:
        return None
    lines = data[:data.rfind(b'\n') + 1].split(b'\n')[:-1]     # (a half-written last line is no line)
    if len(lines) < n:
        return None
    got = hashlib.sha1(lines[n - 1]).hexdigest() if n else None
    if got != digest:
        return None
    if len(data) > sum(len(l) + 1 for l in lines[:n]):
        with open(path, 'wb') as fh:
            fh.write(b''.join(l + b'\n' for l in lines[:n]))
    return n


# -- export / verify ----------------------------------------------------------------------------------------------
def read_store(store):
    with open(os.path.join(store, 'head.json'), encoding='utf-8') as fh:
        head = json.load(fh)
    with open(os.path.join(store, 'steps.ndjson'), 'rb') as fh:
        data = fh.read()
    lines = data[:data.rfind(b'\n') + 1].decode('utf-8').split('\n')[:-1]   # a line being written is not read
    prov = []
    try:
        with open(os.path.join(store, 'provisional.json'), encoding='utf-8') as fh:
            p = json.load(fh)
        if p['base'] == len(lines):
            prov = [dumps(s) for s in p['steps']]
    except (OSError, ValueError):
        pass
    return head, lines, prov


def export_text(store):
    """The store as timeline.py --no-timings writes the same inputs (committed steps, then the tail)."""
    head, lines, prov = read_store(store)
    files = replay.transcripts(head['inputs'] if len(head['inputs']) > 1 else head['inputs'][0])
    meta = [json.loads(l) for l in lines + prov]
    ids = list(dict.fromkeys(s['session'] for s in meta if s.get('session') and s.get('author') != 'git'))
    session = ','.join(ids) if ids else os.path.splitext(os.path.basename(files[0]))[0]
    sessions = replay.session_list(files, [SimpleNamespace(session=s.get('session', '')) for s in meta]) \
        if len(ids) > 1 else []
    return (f'{{"root":{dumps(head["root"])},"session":{dumps(session)}'
            + (f',"sessions":{dumps(sessions)}' if len(sessions) > 1 else '')
            + f',"schema":{timeline.SCHEMA},"steps":[{",".join(lines + prov)}]}}')


def batch_text(store):
    """timeline.py --no-timings on the store's inputs now (with its frozen untouched files)."""
    head, _, _ = read_store(store)
    inputs = head['inputs'] if len(head['inputs']) > 1 else head['inputs'][0]
    files = replay.transcripts(inputs)
    rest = None
    if head['seed_git']:
        with open(os.path.join(store, 'rest.json'), encoding='utf-8') as fh:
            rest = json.load(fh)
    rs = replay.steps(files, head['root'], seed_git=head['seed_git'], git_reconcile=head['git_reconcile'], rest=rest)
    return dumps(timeline.timeline(inputs, head['root'], timings=False, steps=rs, smart=head['smart']))


def verify(store):
    """0 when the committed steps are a batch build's first steps and the export is the batch build."""
    _, lines, prov = read_store(store)
    want = json.loads(batch_text(store))['steps']
    have = len(lines)
    bad = next((i for i, (a, b) in enumerate(zip(lines, (dumps(s) for s in want))) if a != b), None)
    if len(want) < have:
        bad = len(want) if bad is None else bad
    tail = [dumps(s) for s in want[have:]] == prov
    print(f'committed: {have} steps, ' + ('equal to the batch build' if bad is None else f'differ from step {bad}')
          + f'; provisional: {len(prov)} steps, batch adds {len(want) - have}: ' + ('equal' if tail else 'differ'))
    return 0 if bad is None and tail else 1


# -- command line ---------------------------------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('name')
    r.add_argument('inputs', nargs='+')
    r.add_argument('--root')
    r.add_argument('--seed-git', action='store_true')
    r.add_argument('--git-reconcile', action='store_true')
    r.add_argument('--no-smart', action='store_true')
    r.add_argument('--lag', type=float, default=60.0, help='seconds a record may be written after its time')
    r.add_argument('--idle', type=float, default=600.0, help='seconds without growth before a dead session\'s calls close')
    r.add_argument('--settle', type=float, default=60.0, help='seconds a new transcript holding only copies may wait')
    r.add_argument('--poll', type=float, default=0.3)
    r.add_argument('--fresh', action='store_true', help='ignore the checkpoint: build from scratch')
    for name in ('export', 'verify', 'stop'):
        p = sub.add_parser(name)
        p.add_argument('name')
        if name == 'export':
            p.add_argument('-o', '--out')
            p.add_argument('--diffs', metavar='DIR', help='also write the diffs layout (timeline.py --diffs) to DIR')
    for p in sub.choices.values():
        p.add_argument('--store', help='store directory (default: live/NAME next to live.py)')
    a = ap.parse_args(argv)
    store = a.store or os.path.join(HERE, 'live', a.name)
    if a.cmd == 'export':
        text = export_text(store)
        out = a.out or os.path.join(store, 'timeline.json')
        write_atomic(out, text)
        print(out)
        if a.diffs:
            print(diffs.export_store(store, a.diffs))
        return 0
    if a.cmd == 'verify':
        return verify(store)
    pidfile = os.path.join(store, 'live.pid')
    pid = writer_pid(store)
    if a.cmd == 'stop':
        if pid is None:
            print('live: not running', file=sys.stderr)
            return 1
        os.kill(pid, signal.SIGTERM)
        return 0
    os.makedirs(store, exist_ok=True)
    if pid is not None and pid != os.getpid():
        print(f'live: already running (pid {pid})', file=sys.stderr)
        return 1
    me = f'{os.getpid()} {proc_start(os.getpid())}'
    write_atomic(pidfile, me)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    w = Writer(store, a.inputs, a.root, a.seed_git, a.git_reconcile, not a.no_smart, a.lag, a.idle, settle=a.settle)
    try:
        print(f'live: {w.start(time.time(), fresh=a.fresh)} start, {w.n} steps committed', file=sys.stderr)
        while not stop:
            w.tick(time.time())
            time.sleep(a.poll)
    except KeyboardInterrupt:
        pass
    finally:
        w.checkpoint(time.time())           # (nothing when the last tick did not end)
        if open(pidfile).read().strip() == me:
            os.unlink(pidfile)
    return 0


if __name__ == '__main__':
    sys.exit(main())
