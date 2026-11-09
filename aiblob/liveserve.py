"""Live server: serve a live.py store to the page, labelled, as it grows.

    python liveserve.py run NAME [--store DIR] [--port 8791] [--host 127.0.0.1] [--labels demo/labels.json]
                        [--relabel-at 0.2] [--poll 0.3] [--no-ask] [--llm-url http://127.0.0.1:8080] [--llm-runtime b11960]
                        [--llm-model GGUF] [--llm-cmd CMD] [--idle-stop 600] [--hot 85] [--cool 80] [--quiet 20]
                        [--cap 120] [--vram 3300] [--ram 2048] [--run-log demo/label-runs/NAME.live.jsonl]
    python liveserve.py stop NAME [--store DIR]          SIGTERM to the server (serve.pid: its pid and start time)

The store is live/NAME/ next to this file unless --store says otherwise (live.py's format: head.json, steps.ndjson,
provisional.json, status.json). The server only reads it: it serves what is there whether the writer runs or not,
and follows it while it does. Its pid file is serve.pid in the store (`pid start-time`, as live.pid).

HTTP (127.0.0.1 only):
- /                      web/index.html?live=1 (a redirect; ?live=0 or ?src= keep the static page);
  anything else outside /live/ is web/ as a static server serves it.
- /live/timeline.json    the store as `live.py export` writes it, labelled as `label.py T --cache LABELS --server
                         http://127.0.0.1:1` labels that export (cache hits and kept labels, never a model call) with
                         the labels layer below, plus "live": {epoch, steps (committed), provisional, gen, lv, labels,
                         ask}: steps[:steps] are committed, the rest is the provisional tail; labels and lv: the layer.
- /live/events?epoch=E&from=N&lv=V   server-sent events. "steps" {epoch, from, steps, tail, gen}: committed steps from
                         step `from` on (id: the committed count after them; Last-Event-ID resumes there) and the
                         whole tail as it stands with them. "tail" {epoch, base, gen, tail}: the tail changed alone.
                         "rebuild" {epoch, steps}: what was served no longer holds (the writer rebuilt the store
                         with other steps, or head.json changed): load /live/timeline.json again. An event whose
                         epoch is not the page's means the same. "labels" {epoch, lv, labels: {kid: label or null}}:
                         the layer's changes after version V (the page's lv). "ask" {state (idle, waiting, asking,
                         starting, paused, down), queued, asking, temp, server (external / child), child (pid),
                         pending: [kid]}: the labelling chip.
- POST /live/focus {"step": i}   the step the page shows: its blobs are asked first.
- /live/status           {epoch, ready, steps, provisional, gen, rebuilds, writer (status.json), writer_running,
                         labels: {path, keys, lv, layer}, ask, labelling (the new-step boundary), pid}.

Following the store (every --poll s, by size and mtime): steps.ndjson is read from where the last read ended, up to
its last newline (a line being written is not read). Its last line read must still be where it was; when it is not,
or the file shrank, or status.json's `rebuilds` grew, or provisional.json's `gen` went back, the whole file is read
again and compared with the steps served: a file that is their prefix (a writer cutting back to its checkpoint, or
in the middle of a rebuild) is waited on and its lines checked as they come back, until the store says the served
steps past it are not what comes next (provisional.json, once it follows the file, goes on with other steps, or
status.json says the file's count is all that is committed): then, as for a file that differs, a rebuild (epoch + 1).
Committed steps served never contradict the store once it has said so. The tail is provisional.json's steps when its `base` is the committed count (when it is behind,
those of its steps already committed must be the committed ones), labelled on a fork of the labeller.

Labels: label.py's own code runs on each step as it comes (step_keys, Keeper, apply_labels; the per-step loop of
plan_labels and the kept-label pass of main), on the steps expanded one at a time (Expander: timeline.expand's rules)
and compacted again (timeline.Compactor), so the page gets the format it already reads and nothing is ported to JS.
File and text blobs are keyed by their file's first lines (label.file_head), read when the step is labelled, as
label.py reads them when it runs.

The labels layer. A step is labelled once, with what is known then; what is learnt later reaches every step showing it
without a reload: each served step carries `lk` {blob id: kid} / `mk` {merged entry id: kid} for the ids whose label
key changed since the step before (kid: 16 hex of the key's sha1), and the layer {kid: label or null} holds every key
whose label changed after a step showing it was labelled. A cache change (an answer of the worker, a label.py run, an
edit) is read back from the file: the labeller replays only its plan, from the last snapshot (every 32 steps) before
the first step a changed key is shown at, and the keys whose label changed go out as one "labels" event; the page
relabels the blobs with those keys where they are (cost: the steps they are on). A kept label found at a later step
for a key an earlier step shows (a `merged` entry going back to an earlier version) goes out the same way. So the
page shows what `label.py T --cache LABELS --server http://127.0.0.1:1` writes with the cache as it is now, every step
(aigource, a random half of its cache: 323 steps differ without the layer, none with it).

Asks (livelabel.py): only blobs changed at new steps, steps made after labelling was first enabled on the store
(labelling.json, see Live); the worker's thread asks the model and merges each label into LABELS (lock, read again,
merge, rename); --no-ask: none.

Only 127.0.0.1 is bound by default, and a request whose Host is not this machine is refused (DNS rebinding)."""
import argparse, bisect, copy, glob, hashlib, http.server, json, os, signal, socketserver, sys, threading, time, urllib.parse
from datetime import datetime, timezone
from types import SimpleNamespace

import label, replay, timeline

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, 'web')
CHUNK = 200                 # committed steps per "steps" event when a client is far behind


def dumps(x):
    return json.dumps(x, ensure_ascii=False, separators=(',', ':'))


def ts_seconds(ts):
    """A step's `ts` (2026-01-01T00:00:00.000Z) -> seconds since the epoch; None when there is none or it is not one."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def iso_time(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def proc_start(pid):
    try:
        with open(f'/proc/{int(pid)}/stat') as fh:
            return fh.read().rsplit(')', 1)[1].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def pid_running(path):
    """The pid in a `pid start-time` file when that process still runs, else None."""
    try:
        with open(path) as fh:
            pid, start = fh.read().split()
        pid = int(pid)
    except (OSError, ValueError):
        return None
    return pid if proc_start(pid) == start else None


# -- labels ----------------------------------------------------------------------------------------------------------
class Expander:
    """timeline.expand() one step at a time: push(compacted step) -> its full state (the same rules and the same
    objects shared between steps)."""

    def __init__(self, schema):
        self.old, self.v4 = schema < 3, schema >= 4
        self.n, self.cur, self.order, self.fresh, self.edges, self.areas = 0, {}, [], set(), {}, None

    def fork(self):
        f = copy.copy(self)
        f.edges = dict(self.edges)
        f.areas = dict(self.areas) if self.areas is not None else None
        return f

    def push(self, st):
        if self.old or st.get('full') or 'edges' in st or not self.n:
            self.areas = {a['id']: a for a in st['areas']} if 'areas' in st else None
            self.cur = {b['id']: b for b in st['blobs']}
            self.order = [b['id'] for b in st['blobs']]
            self.fresh = set(self.cur)
            self.edges = {(e['a'], e['b'], e['kind']): e for e in st['edges']}
            st_edges = list(st['edges'])
        else:
            cur, fresh = self.cur, self.fresh
            gone = set(st.get('removed_blobs', ()))
            sent = {b['id']: b for b in st['blobs']}
            if self.v4:
                sent = {i: timeline._apply_func_patch(
                    b, (timeline.rest(cur[i]) if i in fresh else cur[i]) if i in cur else None)
                    for i, b in sent.items()}
            have = [i for i in self.order if i not in gone]
            cur = {i: sent[i] if i in sent else timeline.rest(cur[i]) if i in fresh else cur[i] for i in have}
            self.order = have + [i for i in sent if i not in cur]
            cur.update(sent)
            self.cur, self.fresh = cur, set(sent)
            if 'order' in st:
                self.order = list(st['order'])
            for e in st.get('edges_remove', ()):
                self.edges.pop((e['a'], e['b'], e['kind']), None)
            for e in st.get('edges_add', ()):
                self.edges[(e['a'], e['b'], e['kind'])] = e
            st_edges = sorted(self.edges.values(), key=timeline.edge_key)
            if self.areas is not None:
                for i in st.get('areas_remove', ()):
                    self.areas.pop(i, None)
                self.areas.update((a['id'], a) for a in st.get('areas', ()))
        self.n += 1
        step = {k: st[k] for k in timeline.META if k in st}
        step.update(blobs=[self.cur[i] for i in self.order], edges=st_edges)
        if self.areas is not None:
            step['areas'] = [self.areas[i] for i in sorted(self.areas)]
        if 'stats' in st:
            step['stats'] = st['stats']
        return step


def load_cache(path):
    """The label cache as label.py main() reads it: {} when missing or unreadable, titles it rejects dropped."""
    try:
        with open(path, encoding='utf-8') as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in cache.items() if not label.title_problem(v['title'])}


SNAP = 32                   # steps between the labeller's snapshots (a cache change replays the plan from one)
_KIDS = {}


def kid(key):
    """The short id of a cache key the page knows it by (`lk` / `mk`, the labels layer)."""
    k = _KIDS.get(key)
    if k is None:
        if len(_KIDS) > 200000:
            _KIDS.clear()
        k = _KIDS[key] = hashlib.sha1(key.encode()).hexdigest()[:16] if key else ''
    return k


def shown(entry):
    """A cache / applied entry as apply_labels() shows it (without `reused_from`); None for none."""
    return None if entry is None else {f: x for f, x in entry.items() if f != 'reused_from'}


def merged_keys(b):
    """{merged entry id: its key} as apply_labels() and label.units() key a `merged` entry (its own functions)."""
    out = {}
    for m in b.get('merged', ()):
        mine = set(m['funcs'])
        out[m['id']] = ','.join(sorted(f['hash'] for f in b['funcs'] if f.get('qual') in mine))
    return out


class Ctx:
    """What an ask of a step's blob takes from that step: the system prompt (every part of the step), each blob's
    module and key (the other parts' titles, read when it is asked)."""
    __slots__ = ('system', 'module_of', 'keys')

    def __init__(self, step):
        self.system = label.system_prompt(step)
        self.module_of = {b['id']: b['module'] for b in step['blobs']}
        self.keys = dict(step['_keys'])


class Want:
    """A key the worker may ask: its blob (with _edges / _head) and Ctx, the step it is first shown at (vis), the
    newest step and arrival time of a change of a blob holding it (pos, at), when it was first wanted (since), the
    (session, turn) of that newest change and the blob identities that want it (uids)."""
    __slots__ = ('key', 'blob', 'ctx', 'vis', 'pos', 'at', 'since', 'turn', 'uids')

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)

    def __eq__(self, o):
        return isinstance(o, Want) and all(getattr(self, k) == getattr(o, k) for k in self.__slots__ if k != 'ctx') \
            and self.ctx is o.ctx

    def __repr__(self):
        return f'Want({self.key[:24]!r}, pos={self.pos}, at={self.at}, since={self.since})'


class Labeller:
    """label.py's apply step (`label.py T --cache C --server http://127.0.0.1:1`, the server down: cache hits and
    kept labels only) one step at a time: push(raw compacted step) -> the labelled compacted step. Its decisions are
    plan_labels' (the Keeper fed every step from step 0, an ask standing in as an empty entry) and main()'s kept-label
    pass (a key reused from a cached label gets that label); apply_labels() and the Compactor then run on the step.
    The expanded state is never written to: each step's blobs are labelled on copies.

    The labels layer (phase 4). Each labelled step also carries `lk` {blob id: kid(key)} and `mk` {merged entry id:
    kid(key)} for the ids whose key differs from the previous step's. `applied` is what label.py would apply with the
    cache as it is now; when an entry of it changes after a step showing its key was labelled (a kept label found at a
    later step, a cache change), `overlay` gets {kid: shown label or None} and the page relabels every step showing it.
    recache(new cache) replays only the plan (no labelling, no compaction) from the last snapshot before the first step
    a changed key is shown at, so `applied`, the asks and the overlay are what a run from step 0 with that cache gives.

    Asks (the worker's): steps where `boundary(i, ts)` holds are new. wants() -> {key: Want} for the blob identities
    (units with a blob) changed at a new step: the anchor of the identity's label when it was asked at a new step
    (the newest version near it gets its label by the 20% rule), else its newest version; none once the cache holds
    it."""

    def __init__(self, root, cache, at=0.2, schema=timeline.SCHEMA, boundary=None):
        self.root, self.cache, self.at = root, cache, at
        self.work = dict(cache)             # plan_labels' `work`: the cache, reuses and stand-ins for asks
        self.applied = dict(cache)          # what apply_labels gets: the cache + kept labels
        self.keeper = label.Keeper(at)
        self.exp, self.comp = Expander(schema), timeline.Compactor(schema)
        self.heads, self.hist, self.i = {}, {}, 0
        self.track, self.boundary = True, boundary
        self.first, self.ukey, self.lk, self.mk = {}, {}, {}, {}
        self.snaps, self.overlay = [], {}
        self.asked, self.nu, self.since, self.arrived, self.turn, self.ets = {}, {}, {}, [], None, []

    def fork(self):
        """A copy that goes on alone (the provisional tail): it labels, it does not track."""
        f = copy.copy(self)
        f.work, f.applied, f.lk, f.mk = dict(self.work), dict(self.applied), dict(self.lk), dict(self.mk)
        k = f.keeper = label.Keeper(self.at)
        k.state = {u: list(s) for u, s in self.keeper.state.items()}   # (an entry's list is changed in place)
        f.exp, f.comp = self.exp.fork(), copy.copy(self.comp)
        f.track, f.overlay, f.heads = False, {}, dict(self.heads)
        return f

    def head(self, b, at=None):
        """label.file_head(), read again only when the file changed; at a step position replayed (`at`), the head
        that step was labelled with."""
        if b['kind'] not in ('file', 'text') or not b.get('path'):
            return []
        p = os.path.join(self.root, b['path'])
        if at is not None:
            hist = self.hist.get(p, ())
            j = bisect.bisect_right([i for i, _ in hist], at) - 1
            if j >= 0:
                return hist[j][1]
        try:
            s = os.stat(p)
            sig = (s.st_mtime_ns, s.st_size)
        except OSError:
            sig = None
        got = self.heads.get(p)
        if got is None or got[0] != sig:
            got = self.heads[p] = (sig, label.file_head(self.root, b))
        return got[1]

    def _expand(self, exp, raw, at=None):
        step = exp.push(raw)
        step['blobs'] = [dict(b, merged=[dict(m) for m in b['merged']]) if 'merged' in b else dict(b)
                         for b in step['blobs']]
        for b in step['blobs']:
            h = self.head(b, at)
            if h:
                b['_head'] = h
            if at is None and self.track and b['kind'] in ('file', 'text') and b.get('path'):
                hist = self.hist.setdefault(os.path.join(self.root, b['path']), [])
                if not hist or hist[-1][1] != h:
                    hist.append((self.i, h))
        step['_keys'] = label.step_keys(step)
        return step

    def _plan(self, i, step):
        """The Keeper on step i (asks stand in as empty entries). -> (asks {key: blob}, kept keys)."""
        asks = self.keeper.step(i, step, self.work)
        for key in asks:
            self.work[key] = {}
        self.keeper.settle(self.work)
        return asks, self.keeper.reused

    def _snap(self, i, exp):
        """A snapshot before step i; past the last 16, only one in 8 is kept (a long session stays small)."""
        self.snaps.append((i, exp.fork(), {u: list(s) for u, s in self.keeper.state.items()}, dict(self.work)))
        self.snaps = [sn for sn in self.snaps if sn[0] % (SNAP * 8) == 0 or sn[0] >= i - SNAP * 16]

    def _new(self, i):
        """Step i is new (`boundary`, with the step's time: its own ts or the last one before it)."""
        return self.boundary is not None and self.boundary(i, self.ets[i])

    def _ask_blob(self, step, b):
        a = dict(b, _edges=[e for e in step['edges'] if b['id'] in (e['a'], e['b'])])
        a.pop('label', None)
        return a

    def push(self, raw, now=None):
        i = self.i
        if self.track and i % SNAP == 0:
            self._snap(i, self.exp)
        step = self._expand(self.exp, raw)
        asks, reused = self._plan(i, step)
        ov = self.overlay
        for key in reused:
            src = self.work[key]['reused_from']
            if key not in self.cache and src in self.cache:
                self.applied[key] = dict(self.cache[src], reused_from=src)
                if self.track and self.first.get(key, i) < i:     # (shown at an earlier step: the back-fill)
                    ov[kid(key)] = shown(self.applied[key])
        lk = {b['id']: kid(k) for b in step['blobs'] for k in (step['_keys'][b['id']],) if self.lk.get(b['id']) != kid(k)}
        mks = {}
        for b in step['blobs']:
            if 'merged' in b:
                mks.update(merged_keys(b))
        mk = {m: kid(k) for m, k in mks.items() if self.mk.get(m) != kid(k)}
        self.lk = {b['id']: kid(step['_keys'][b['id']]) for b in step['blobs']}
        self.mk = {m: kid(k) for m, k in mks.items()}
        if self.track:
            self._track(i, step, asks, now)
        self.i += 1
        label.apply_labels([step], self.applied)
        del step['_keys']
        for b in step['blobs']:
            b.pop('_head', None)
        out = self.comp.push(step)
        if lk:
            out['lk'] = lk
        if mk:
            out['mk'] = mk
        return out

    def _track(self, i, step, asks, now):
        """First steps of keys, changes of units, and on a new step what the worker may ask."""
        self.arrived.append(now)
        self.ets.append(step.get('ts') or (self.ets[-1] if self.ets else ''))
        changed = []
        for uid, st in self.keeper.state.items():
            if st[5] == i:
                self.first.setdefault(st[3], i)
                if self.ukey.get(uid) != st[3]:
                    self.ukey[uid] = st[3]
                    changed.append(uid)
        self.turn = (step.get('session'), step.get('turn'))
        if not self._new(i):
            return
        ctx = None
        if asks:
            ctx = Ctx(step)
            for key, b in asks.items():
                self.asked.setdefault(key, (self._ask_blob(step, b), ctx))
        if changed:
            blobs = {b['id']: b for b in step['blobs']}
            for uid in changed:
                b = blobs.get(uid)
                if b is None:       # a merged entry: never asked
                    continue
                ctx = ctx or Ctx(step)
                self.nu[uid] = (self._ask_blob(step, b), ctx, i, now, self.turn)

    def take_overlay(self):
        ov, self.overlay = self.overlay, {}
        return ov

    def recache(self, cache, raws):
        """The cache changed (an answer, another writer): replay the plan from the snapshot before the first step a
        changed key is shown at, with `raws` the committed raw steps (dicts or lines). -> {kid: shown label or None}
        for the keys shown so far whose applied label changed."""
        changed = {k for k in self.cache.keys() | cache.keys() if self.cache.get(k) != cache.get(k)}
        if not changed:
            return {}
        self.cache = cache
        seen = [self.first[k] for k in changed if k in self.first]
        if seen:
            p = min(seen)
            j = max(n for n, s in enumerate(self.snaps) if s[0] <= p)
            s0, exp, state, work = self.snaps[j]
            del self.snaps[j + 1:]
            exp = exp.fork()
            self.keeper = label.Keeper(self.at)
            self.keeper.state = {u: list(s) for u, s in state.items()}
            self.work = dict(work)
        for work in [self.work] + [sn[3] for sn in self.snaps]:     # (in a snapshot kept, a changed key is still only
            for k in changed:                                         # the cache's: it is first shown after it)
                if k in cache:
                    work[k] = cache[k]
                else:
                    work.pop(k, None)
        for k in changed & cache.keys():
            self.asked.pop(k, None)                                   # (answered: its blob is not needed any more)
        if seen:
            for i in range(s0, self.i):
                if i % SNAP == 0 and i > s0:
                    self._snap(i, exp)
                raw = raws[i]
                step = self._expand(exp, json.loads(raw) if isinstance(raw, str) else raw, at=i)
                asks, _ = self._plan(i, step)
                if asks and self._new(i):
                    ctx = None
                    for key, b in asks.items():
                        if key not in self.asked:
                            ctx = ctx or Ctx(step)
                            self.asked[key] = (self._ask_blob(step, b), ctx)
        old, self.applied = self.applied, dict(cache)
        for k, v in self.work.items():
            src = v.get('reused_from')
            if src is not None and k not in cache and src in cache:
                self.applied[k] = dict(cache[src], reused_from=src)
        out = {}
        for k in old.keys() | self.applied.keys():
            if k in self.first:
                a, b = shown(old.get(k)), shown(self.applied.get(k))
                if a != b:
                    out[kid(k)] = b
        return out

    def wants(self):
        """{key: Want} the worker may ask now (class doc)."""
        out, last = {}, self.i - 1
        for uid, (blob, ctx, pos, at, turn) in self.nu.items():
            st = self.keeper.state.get(uid)
            anchor = st[0] if st else None
            if st is None or st[5] != last or anchor in self.cache:
                self.since.pop(uid, None)
                continue
            if anchor is not None and anchor in self.asked:
                key, (blob, ctx) = anchor, self.asked[anchor]
            else:
                key = st[3]
                if not key or key in self.cache:
                    self.since.pop(uid, None)
                    continue
            rec = self.since.get(uid)       # (when it was first wanted, what it wanted last)
            if rec is None or rec[1] != key and rec[1] in self.cache:     # (its last want was answered: from now)
                rec = (at, key)
            self.since[uid] = (rec[0], key)
            since = rec[0]
            w = out.get(key)
            if w is None:
                out[key] = Want(key=key, blob=blob, ctx=ctx, vis=self.first.get(key, pos), pos=pos, at=at, since=since,
                                turn=turn, uids=frozenset([uid]))
            else:
                if (at, pos) > (w.at, w.pos):
                    w.at, w.pos, w.turn = at, pos, turn
                w.since, w.uids = min(w.since, since), w.uids | {uid}
        return out


# -- the store ---------------------------------------------------------------------------------------------------------
def _stat(path):
    try:
        s = os.stat(path)
        return (s.st_ino, s.st_size, s.st_mtime_ns)
    except OSError:
        return None


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _complete_lines(data):
    """bytes -> the complete lines (str) and the bytes they take (a line being written is not one)."""
    end = data.rfind(b'\n') + 1
    return (data[:end].decode('utf-8').split('\n')[:-1] if end else []), end




class Live:
    """What is served, kept up to date with the store by poll() (one thread). Readers take `cond` and read epoch,
    lines (labelled committed steps), tail (labelled provisional steps), tail_v (+1 when the tail changes), gen,
    header, status, ready, ov ({kid: (lv, label)}: the labels layer's changes since the epoch began), lv (+1 per
    change of it), ask (the labelling chip: worker status + pending kids), ask_v.

    With a `worker` (livelabel.Worker) the labeller tells it what to ask, and only for new steps: steps made after
    labelling was first enabled on the store. labelling.json in the store holds that time, written once: {since (UTC),
    steps (committed then, for information)}. `since` is the wall clock then, but never earlier than what the store
    and its transcripts already hold: the files' mtimes and the newest step time read (so a clock that is behind, or
    a store still being built or behind its transcripts, cannot put history after it). A transcript whose mtime is
    more than 60 s past that clock (or past the store's own files, if later) is left out and logged: a file touched
    ahead would otherwise put real new steps before the boundary. A step is new when its time
    (`ts`) is not before `since`; a step without a ts takes the time of the last step before it that has one (steps
    are committed in time order), and one with none before it is never new. History rebuilt later, caught up, or read
    again at a restart stays old; steps made while the server was down are new."""

    def __init__(self, store, labels, at=0.2, worker=None, clock=time.monotonic):
        self.store, self.labels_path, self.at, self.worker, self.clock = store, labels, at, worker, clock
        self.cond = threading.Condition()
        # epochs start at the clock (ms), so a page that outlived a server restart is told to load again
        self.epoch, self.lines, self.tail, self.tail_v, self.gen, self.ready = int(time.time() * 1000), [], [], 0, 0, False
        self.status, self.header, self.cache_keys, self.stats, self.broken = {}, '', 0, {}, False
        self.ov, self.lv, self.ask, self.ask_v = {}, 0, None, 0
        self.bound = _read_json(self.path('labelling.json')) if worker is not None else None
        if self.bound is not None:
            self.bound['_since'] = ts_seconds(self.bound.get('since'))
            if self.bound['_since'] is None:     # (unreadable: nothing is new until it is written again)
                os.replace(self.path('labelling.json'), self.path('labelling.json.bad'))
                self.bound = None
        self._ask_raw, self._ask_last, self._wants = None, None, None
        self.worker_thread, self.worker_died, self.worker_restarts, self.worker_cap = None, None, 0, 1.0
        self._reset()
        if worker is not None:
            worker.on_status = self.on_ask
            worker.title_of = lambda k: ((self.lab.applied.get(k) if self.lab else None) or {}).get('title')

    path = lambda self, name: os.path.join(self.store, name)

    def _reset(self):
        """The poll thread's own state, from nothing."""
        self.head, self.raw, self.lab = None, [], None   # head.json; committed raw lines served; labeller after them
        self.ino, self.file_n, self.offset, self.last = None, 0, 0, None   # steps.ndjson: lines and bytes read,
        self.prov = None                                 # (offset, bytes) of the last line; provisional.json:
        self.tail_raw, self.tail_base = [], 0            # (base, raw lines, gen); the tail served: raw lines, base
        self.tail_force = False                          # (labelled again: the labels changed)
        self.ids, self.counts, self.sessions, self.tail_ids, self.fallback = [], {}, [], [], None
        self.tail_counts = {}
        self.rebuilds, self.prov_gen, self.wstatus = 0, None, {}

    # -- polling --------------------------------------------------------------------------------------------------
    def poll(self):
        """Read what changed in the store; True when what is served changed."""
        self._ask_poll()
        stats = {n: _stat(self.path(n)) for n in ('head.json', 'steps.ndjson', 'provisional.json', 'status.json')}
        stats['labels'] = _stat(self.labels_path)
        if self.ready and stats == self.stats:
            return False
        old, self.stats = self.stats, stats
        if not self.ready or self.broken or _read_json(self.path('head.json')) != self.head:
            return self._rebuild()
        ov = self._recache() if stats['labels'] != old.get('labels') else {}
        status = _read_json(self.path('status.json')) or {} if stats['status.json'] != old.get('status.json') \
            else None
        prov = _read_json(self.path('provisional.json')) if stats['provisional.json'] != old.get('provisional.json') \
            else None
        check = False
        if status is not None:
            check = status.get('rebuilds', 0) > self.rebuilds
            self.rebuilds, self.wstatus = status.get('rebuilds', 0), status
        if prov is not None:
            check = check or self.prov_gen is not None and prov.get('gen', 0) < self.prov_gen
            self.prov_gen = prov.get('gen', 0)
            self.prov = (prov.get('base', -1), [dumps(s) for s in prov.get('steps', ())], prov.get('gen', 0))
        new = self._read_steps(check)
        if new is None or not self._behind_ok():    # the steps served no longer hold
            return self._rebuild()
        if new:                                     # a tail written while they were read goes out with them
            pst = _stat(self.path('provisional.json'))
            p2 = _read_json(self.path('provisional.json')) if pst != stats['provisional.json'] else None
            if p2 is not None and p2.get('gen', 0) >= (self.prov_gen or 0):
                stats['provisional.json'], self.prov_gen = pst, p2.get('gen', 0)
                self.prov = (p2.get('base', -1), [dumps(s) for s in p2.get('steps', ())], p2.get('gen', 0))
        tail = self._new_tail()
        ov.update(self.lab.take_overlay())
        if not new and tail is None and status is None and not ov:
            return False
        header = self._header() if new or tail is not None else None
        with self.cond:
            if status is not None:
                self.status = status
            if new:
                self.lines.extend(new)
            if tail is not None:
                self.tail, self.gen = tail
                self.tail_v += 1
            if header is not None:
                self.header = header
            self._overlay(ov)
            self.cond.notify_all()
        self._push_wants()
        self._ask_poll(True)
        return bool(new) or tail is not None or bool(ov)

    # -- labels ---------------------------------------------------------------------------------------------------
    def _overlay(self, ov):
        """(under cond) the labels layer's changes, one version for them all."""
        if ov:
            self.lv += 1
            for k, v in ov.items():
                self.ov[k] = (self.lv, v)

    def _recache(self):
        """The label cache file changed: the labeller replays its plan from where a changed key is first shown; the
        tail is labelled again. -> the layer's changes."""
        cache = load_cache(self.labels_path)
        ov = self.lab.recache(cache, self.raw)
        self.cache_keys = len(cache)
        self.tail_force = True
        return ov

    def is_new(self, i, ts):
        """Step i, whose time is `ts` (its own, else that of the last step before it that has one): made after
        labelling began (class doc)."""
        b = self.bound
        t = ts_seconds(ts)
        return b is not None and t is not None and t >= b['_since']

    def _bound_init(self):
        """labelling.json: written once, the first time labelling runs on this store (class doc)."""
        if self.worker is None or self.bound is not None:
            return
        floor = [time.time()]
        for n in ('head.json', 'steps.ndjson', 'provisional.json', 'status.json'):
            st = _stat(self.path(n))
            if st:
                floor.append(st[2] / 1e9)
        now, ahead = max(floor), []         # (the clock, or the store's own files if they are later: just written)
        for p in (self.head or {}).get('inputs') or ():
            for f in [p] + glob.glob(os.path.join(p, '*.jsonl')) + glob.glob(os.path.join(p, '*', 'subagents', '*.jsonl')):
                if os.path.exists(f):
                    m = os.path.getmtime(f)
                    if m > now + 60:        # (a file touched ahead says nothing of when labelling began)
                        ahead.append((f, iso_time(m)))
                    else:
                        floor.append(m)
        if ahead:
            print(f'liveserve: {len(ahead)} transcript(s) dated ahead of the clock ({iso_time(now)}), not used for '
                  f'the labelling boundary: {ahead[:3]}', file=sys.stderr)
            self.worker.log(event='mtime-ahead', now=iso_time(now), files=[f for f, _ in ahead[:20]],
                            mtimes=[m for _, m in ahead[:20]])
        prov = self.prov[1] if self.prov else []
        floor += [t + 0.001 for line in self.raw + prov for t in (ts_seconds(json.loads(line).get('ts')),)
                  if t is not None]                 # (a step there was made before: after it, by 1 ms)
        since = max(floor)
        self.bound = {'since': iso_time(since), 'steps': len(self.raw)}
        tmp = self.path('labelling.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as fh:
            fh.write(dumps(self.bound))
        os.replace(tmp, self.path('labelling.json'))
        self.bound['_since'] = since

    def _push_wants(self):
        if self.worker is None or self.lab is None:
            return
        wants = (self.lab.wants(), self.lab.turn)
        if wants != self._wants:
            self._wants = wants
            self.worker.set_wants(*wants)

    def on_ask(self, status):
        """The worker's status (its thread): taken up by the next poll."""
        with self.cond:
            self._ask_raw = status

    def _ask_poll(self, force=False):
        """The chip: the worker's status, with the keys it will ask and those kept from them as `pending` kids (again
        when `force`: the labeller moved on)."""
        with self.cond:
            raw, self._ask_raw = self._ask_raw, None
        if raw is not None:
            self._ask_last = raw
        elif not force or self._ask_last is None:
            return
        if self.lab is None:
            return
        raw = dict(self._ask_last)
        keys = set(raw.pop('keys', ()))
        pend = {kid(k) for k in keys}
        if keys:
            for k, v in self.lab.work.items():
                if v.get('reused_from') in keys and k not in self.lab.cache:
                    pend.add(kid(k))
        ask = dict(raw, pending=sorted(pend))
        if ask != self.ask:
            with self.cond:
                self.ask = ask
                self.ask_v += 1
                self.cond.notify_all()

    def _behind_ok(self):
        """While the file holds fewer committed steps than were served (cut back to a checkpoint, a rebuild under
        way): False once the store says the served steps past it are not what comes next. That is when
        provisional.json follows the file (its base is at most the file's count, the steps it repeats are the file's)
        and does not go on with the served steps, or when status.json says the file's count is all that is committed
        and provisional.json does not follow it. A provisional.json from before (its base past the file's count)
        with status.json from before too is a writer in the middle of a write: waited on."""
        n, k = len(self.raw), self.file_n
        if k >= n:
            return True
        if self.prov is not None and self.prov[0] <= k:
            base, lines = self.prov[:2]
            return base >= 0 and lines[:n - base] == self.raw[base:n]
        return self.wstatus.get('steps') != k

    def _read_steps(self, check):
        """The committed steps steps.ndjson gained, labelled; None when it no longer starts with the steps served.
        A file shorter than what is served but its prefix (cut back to a checkpoint, a rebuild under way) is
        waited on: its lines are checked against the served ones as they come back."""
        path = self.path('steps.ndjson')
        st = self.stats.get('steps.ndjson')
        if st is None:                      # (a writer recreates it)
            self.ino, self.file_n, self.offset, self.last = None, 0, 0, None
            return []
        check = check or st[1] < self.offset or (self.ino is not None and st[0] != self.ino)
        try:
            with open(path, 'rb') as fh:
                if not check and self.last is not None:
                    fh.seek(self.last[0])
                    check = fh.read(len(self.last[1])) != self.last[1]
                if check:
                    fh.seek(0)
                    lines, end = _complete_lines(fh.read())
                    k = min(len(lines), len(self.raw))
                    if lines[:k] != self.raw[:k]:
                        return None
                    start, self.file_n, self.offset = 0, len(lines), end
                else:
                    fh.seek(self.offset)
                    lines, end = _complete_lines(fh.read())
                    start = self.file_n
                    self.file_n += len(lines)
                    self.offset += end
                    for j, l in enumerate(lines[:max(0, len(self.raw) - start)]):
                        if l != self.raw[start + j]:
                            return None
                if self.offset:
                    fh.seek(max(0, self.offset - (1 << 20)))
                    chunk = fh.read(self.offset - fh.tell())
                    line = chunk[chunk.rfind(b'\n', 0, len(chunk) - 1) + 1:]
                    self.last = (self.offset - len(line), line)
                else:
                    self.last = None
        except OSError:
            return []
        self.ino = st[0]
        return [self._commit(l) for l in lines[max(0, len(self.raw) - start):]]

    def _commit(self, raw):
        st = json.loads(raw)
        self.raw.append(raw)
        if st.get('session') and st.get('author') != 'git':
            if st['session'] not in self.counts:
                self.ids.append(st['session'])
            self.counts[st['session']] = self.counts.get(st['session'], 0) + 1
        return dumps(self.lab.push(st, self.clock()))

    def _new_tail(self):
        """(labelled tail, gen) when the tail to serve changed, else None. It is provisional.json's steps when its base
        is the committed count, or those after the committed count when the ones before are the committed ones (also
        while the file is behind what is served); else what is left of the tail served on the same terms; else none."""
        n = len(self.raw)
        raw, gen = [], self.gen
        for base, lines, g in ([self.prov] if self.prov else []) + [(self.tail_base, self.tail_raw, self.gen)]:
            if 0 <= base <= n <= base + len(lines) and lines[:n - base] == self.raw[base:n]:
                raw, gen = lines[n - base:], g
                break
        if raw == self.tail_raw and n == self.tail_base and gen == self.gen and not (self.tail_force and raw):
            return None
        self.tail_force = False
        lab, tail, self.tail_ids, self.tail_counts = self.lab.fork(), [], [], {}
        try:
            for line in raw:
                st = json.loads(line)
                if st.get('session') and st.get('author') != 'git':
                    if st['session'] not in self.counts and st['session'] not in self.tail_counts:
                        self.tail_ids.append(st['session'])
                    self.tail_counts[st['session']] = self.tail_counts.get(st['session'], 0) + 1
                tail.append(dumps(lab.push(st)))
        except (ValueError, KeyError, TypeError, AttributeError) as e:   # not deltas on the committed steps
            print(f'liveserve: provisional steps that do not follow the committed ones, none served '
                  f'({type(e).__name__}: {e})', file=sys.stderr)
            tail, self.tail_ids, self.tail_counts = [], [], {}
        self.tail_raw, self.tail_base = raw, n
        return tail, gen

    def _rebuild(self):
        """Everything again from the files (start, a rebuild of the store, head.json or the label cache changed):
        built aside, then served at once under the next epoch."""
        self._reset()
        self.broken = True                  # (until it ends)
        self.head = _read_json(self.path('head.json'))
        status = _read_json(self.path('status.json')) or {}
        prov = _read_json(self.path('provisional.json'))
        self.rebuilds, self.wstatus = status.get('rebuilds', 0), status
        if prov is not None:
            self.prov_gen = prov.get('gen', 0)
            self.prov = (prov.get('base', -1), [dumps(s) for s in prov.get('steps', ())], prov.get('gen', 0))
        cache = load_cache(self.labels_path)
        head = self.head or {}
        self.lab = Labeller(head.get('root') or '.', cache, self.at, head.get('schema', timeline.SCHEMA), self.is_new)
        with self.cond:
            self.gen = 0
        lines = self._read_steps(True) or []
        self._bound_init()
        tail = self._new_tail()
        header = self._header()
        ov = self.lab.take_overlay()
        with self.cond:
            self.lines, self.status, self.header, self.cache_keys = lines, status, header, len(cache)
            self.tail, self.gen = tail if tail is not None else ([], 0)
            self.tail_v += 1
            self.epoch += 1
            self.ov = {}
            self._overlay(ov)
            self.ready = True
            self.cond.notify_all()
        self.broken = False
        self._wants = None
        self._push_wants()
        self._ask_poll(True)
        return True

    def _header(self):
        """The timeline's head fields as live.export_text() writes them (the session list is read again only when
        a session is added; its step counts are the served ones)."""
        head = self.head or {}
        inputs = head.get('inputs') or []
        arg = inputs if len(inputs) > 1 else inputs[0] if inputs else None
        ids = list(dict.fromkeys(self.ids + self.tail_ids))
        session = ','.join(ids)
        if not session and arg is not None:
            if self.fallback is None:
                files = replay.transcripts(arg)
                self.fallback = os.path.splitext(os.path.basename(files[0]))[0] if files else ''
            session = self.fallback
        if len(ids) > 1 and len(self.sessions) < len(ids):
            try:
                self.sessions = replay.session_list(replay.transcripts(arg), [SimpleNamespace(session=s) for s in ids])
            except (OSError, ValueError, IndexError, TypeError):
                self.sessions = []
            self.sessions += [None] * (len(ids) - len(self.sessions))  # (unreadable: not read again for these ids)
        count = lambda s: self.counts.get(s, 0) + self.tail_counts.get(s, 0)
        sessions = [dict(s, steps=count(s['id'])) for s in self.sessions if s] if len(ids) > 1 else []
        return (f'"root":{dumps(head.get("root"))},"session":{dumps(session)}'
                + (f',"sessions":{dumps(sessions)}' if len(sessions) > 1 else '')
                + f',"schema":{head.get("schema", timeline.SCHEMA)}')

    # -- what is served -------------------------------------------------------------------------------------------
    def timeline_text(self):
        with self.cond:
            lines, tail, epoch, gen, header = list(self.lines), list(self.tail), self.epoch, self.gen, self.header
            labels, lv, ask = {k: v for k, (_, v) in self.ov.items()}, self.lv, self.ask
        live = dumps({'epoch': epoch, 'steps': len(lines), 'provisional': len(tail), 'gen': gen, 'lv': lv,
                      'labels': labels, 'ask': ask})
        return '{' + header + f',"live":{live},"steps":[' + ','.join(lines + tail) + ']}'

    def status_obj(self):
        with self.cond:
            out = {'epoch': self.epoch, 'ready': self.ready, 'steps': len(self.lines), 'provisional': len(self.tail),
                   'gen': self.gen, 'rebuilds': self.status.get('rebuilds', 0), 'writer': self.status,
                   'labels': {'path': self.labels_path, 'keys': self.cache_keys, 'lv': self.lv, 'layer': len(self.ov)},
                   'ask': self.ask, 'pid': os.getpid(),
                   'labelling': {k: v for k, v in self.bound.items() if not k.startswith('_')} if self.bound else None}
        out['writer_running'] = pid_running(self.path('live.pid')) is not None
        return out

    WORKER_RESTARTS = 3

    def start_worker(self, stop, wait_cap=1.0):
        """The worker's thread (again, after it died: watch_worker())."""
        def run():
            try:
                self.worker.run(stop, wait_cap)
            except BaseException as e:      # (the thread ends; watch_worker sees it)
                self.worker_died = f'{type(e).__name__}: {e}'[:300]
        self.worker_died = None
        self.worker_thread = threading.Thread(target=run, daemon=True, name='liveserve-ask')
        self.worker_thread.start()

    def watch_worker(self, stop):
        """A worker thread that died is started again, WORKER_RESTARTS times at most; then the chip says error."""
        t = self.worker_thread
        if t is None or t.is_alive() or stop.is_set():
            return
        why = self.worker_died or 'it stopped'
        self.worker.log(event='worker-died', error=why, restarts=self.worker_restarts)
        print(f'liveserve: the labelling worker died ({why})', file=sys.stderr)
        if self.worker_restarts < self.WORKER_RESTARTS:
            self.worker_restarts += 1
            self.start_worker(stop, self.worker_cap)
            return
        self.worker_thread = None
        with self.cond:
            self._ask_raw = dict(self._ask_last or {}, state='error', asking=None, child=None,
                                 error=f'labelling stopped: {why}')

    def follow(self, poll, stop):
        """The poll loop (a thread): poll() every `poll` s until `stop` is set; an error is printed, not fatal."""
        while not stop.is_set():
            self.watch_worker(stop)
            try:
                self.poll()
            except Exception as e:          # read again from scratch once the store changes (or the labels do)
                print(f'liveserve: {type(e).__name__}: {e}', file=sys.stderr)
                self.broken = True
            stop.wait(poll)


# -- HTTP ------------------------------------------------------------------------------------------------------------
def sse(name, data, eid=None):
    return ((f'id: {eid}\n' if eid is not None else '') + f'event: {name}\ndata: {data}\n\n').encode('utf-8')


class Handler(http.server.SimpleHTTPRequestHandler):
    live = None                             # set on the subclass made by serve()
    stop = None
    timeout = 60                            # (a socket timeout: a client that stops reading frees its thread)
    HOSTS = ('127.0.0.1', 'localhost', '[::1]')

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB, **kw)

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        super().end_headers()

    def _host_ok(self):
        """The Host header names this machine (with this server's port, if any), so a page of another site that
        resolves its name to 127.0.0.1 (DNS rebinding) is refused."""
        host = self.headers.get('Host')
        if host is None:
            return True
        name, _, port = host.rpartition(':') if not host.endswith(']') else (host, '', '')
        if not name:
            name, port = port, ''
        ok = name.lower() in self.HOSTS + (self.server.server_address[0],) \
            and port in ('', str(self.server.server_address[1]))
        if not ok:
            self.send_error(403, 'Host not allowed')
        return ok

    def do_HEAD(self):
        if self._host_ok():
            super().do_HEAD()

    def do_GET(self):
        if not self._host_ok():
            return
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ('/', '/index.html') and 'live' not in q and 'src' not in q:
            self.send_response(302)
            self.send_header('Location', '/index.html?' + urllib.parse.urlencode({**{k: v[-1] for k, v in q.items()},
                                                                                   'live': '1'}))
            self.end_headers()
            return
        if u.path == '/live/timeline.json':
            if not self._ready():
                return
            return self._send(self.live.timeline_text().encode('utf-8'), 'application/json')
        if u.path == '/live/status':
            return self._send(dumps(self.live.status_obj()).encode('utf-8'), 'application/json')
        if u.path == '/live/events':
            return self._events(q)
        if u.path.startswith('/live/'):
            return self.send_error(404)
        return super().do_GET()

    def do_POST(self):
        """POST /live/focus {"step": i}: the step the page shows goes first in the labelling queue."""
        if not self._host_ok():
            return
        if urllib.parse.urlsplit(self.path).path != '/live/focus':
            return self.send_error(404)
        try:
            n = int(self.headers.get('Content-Length') or 0)
            step = json.loads(self.rfile.read(n).decode('utf-8'))['step'] if 0 < n <= 1024 else None
            step = int(step)
        except (ValueError, KeyError, TypeError):
            return self.send_error(400, 'a JSON body {"step": i}')
        if self.live.worker is not None:
            self.live.worker.set_focus(step)
        self.send_response(204)
        self.end_headers()

    def _ready(self):
        with self.live.cond:
            ok = self.live.cond.wait_for(lambda: self.live.ready, timeout=600)
        if not ok:
            self.send_error(503, 'store not read yet')
        return ok

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _events(self, q):
        """SSE: from the client's epoch and step (Last-Event-ID, else ?from=), what it lacks, then each change."""
        if not self._ready():
            return
        lv = self.live
        try:
            epoch = int(q.get('epoch', ['0'])[-1])
            sent = int(self.headers.get('Last-Event-ID') or q.get('from', ['0'])[-1])
            lv_sent = int(q.get('lv', ['0'])[-1])
        except ValueError:
            sent = -1
        if sent < 0:
            return self.send_error(400, 'epoch and from are numbers, from a step count')
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        tail_v, quiet, ask_v = None, 0.0, None
        try:
            while not self.stop.is_set():
                with lv.cond:
                    if epoch == lv.epoch and sent == len(lv.lines) and tail_v == lv.tail_v and lv_sent == lv.lv \
                            and ask_v == lv.ask_v:
                        lv.cond.wait(1.0)
                    out = []
                    if epoch != lv.epoch or sent > len(lv.lines):
                        out.append(sse('rebuild', dumps({'epoch': lv.epoch, 'steps': len(lv.lines)})))
                        epoch, sent, tail_v, lv_sent = lv.epoch, len(lv.lines), lv.tail_v, lv.lv
                    elif sent < len(lv.lines):
                        upto = min(len(lv.lines), sent + CHUNK)
                        more = upto < len(lv.lines)
                        out.append(sse('steps', '{"epoch":%d,"from":%d,"gen":%d,"steps":[%s],"tail":[%s]}' % (
                            lv.epoch, sent, lv.gen, ','.join(lv.lines[sent:upto]), '' if more else ','.join(lv.tail)),
                            upto))
                        sent = upto
                        if not more:
                            tail_v = lv.tail_v
                    elif tail_v != lv.tail_v:
                        out.append(sse('tail', '{"epoch":%d,"base":%d,"gen":%d,"tail":[%s]}' % (
                            lv.epoch, len(lv.lines), lv.gen, ','.join(lv.tail))))
                        tail_v = lv.tail_v
                    if lv_sent < lv.lv and epoch == lv.epoch:     # (after the steps they may show on)
                        out.append(sse('labels', dumps({'epoch': lv.epoch, 'lv': lv.lv, 'labels': {
                            k: v for k, (n, v) in lv.ov.items() if n > lv_sent}})))
                        lv_sent = lv.lv
                    if ask_v != lv.ask_v and lv.ask is not None:
                        out.append(sse('ask', dumps(lv.ask)))
                    ask_v = lv.ask_v
                if out:
                    self.wfile.write(b''.join(out))
                    self.wfile.flush()
                    quiet = time.monotonic()
                elif time.monotonic() - quiet > 15:     # a comment: a closed connection shows up as an error
                    self.wfile.write(b': ping\n\n')
                    self.wfile.flush()
                    quiet = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def worker(labels, run_log, llm_url='http://127.0.0.1:8080', llm_cmd=None, llm_cwd=None, smi=('nvidia-smi',),
           meminfo='/proc/meminfo', clock=time.monotonic, llm_out=None, **kw):
    """A livelabel.Worker writing to the cache `labels`, with its Llama (llm_cmd: argv with {port}; default
    livelabel.llama_cmd()). kw: Worker's (quiet, cap, hot, cool) and Llama's (vram, ram, idle_stop, start_timeout)."""
    import livelabel
    if llm_cmd is None:
        llm_cmd, llm_cwd = livelabel.llama_cmd()
    gpu = livelabel.Gpu(smi, clock)
    lk = {k: kw.pop(k) for k in ('vram', 'ram', 'idle_stop', 'start_timeout') if k in kw}
    llama = livelabel.Llama(llm_url, llm_cmd, llm_cwd, gpu, meminfo, clock=clock, out=llm_out, **lk)
    return livelabel.Worker(labels, llama, gpu, run_log, clock, **kw)


def serve(store, labels, host='127.0.0.1', port=8791, at=0.2, poll=0.3, timeout=60, ask=None, clock=time.monotonic,
          wait_cap=1.0):
    """(server, live, stop event, poll thread): the server bound, the store followed; serve_forever() is the
    caller's (stop: set `stop`, then server.shutdown(); live.worker_thread, when there is one, then ends and stops
    the llama-server it started). timeout: seconds a client may leave a read or write hanging. ask: the labelling
    worker (a livelabel.Worker, or a dict of worker() arguments), None for none."""
    if isinstance(ask, dict):
        ask = worker(labels, clock=clock, **ask)
    live, stop = Live(store, labels, at, ask, clock), threading.Event()
    live.worker_cap = wait_cap
    handler = type('LiveHandler', (Handler,), {'live': live, 'stop': stop, 'timeout': timeout})
    srv = Server((host, port), handler)
    t = threading.Thread(target=live.follow, args=(poll, stop), daemon=True, name='liveserve-poll')
    t.start()
    if ask is not None:
        live.start_worker(stop, wait_cap)
    return srv, live, stop, t


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('name')
    r.add_argument('--host', default='127.0.0.1')
    r.add_argument('--port', type=int, default=8791)
    r.add_argument('--labels', default=os.path.join(HERE, 'demo', 'labels.json'),
                   help='label cache (the labelling worker merges its labels into it)')
    r.add_argument('--relabel-at', type=float, default=0.2, help="label.py's --relabel-at")
    r.add_argument('--poll', type=float, default=0.3)
    import livelabel as ll
    g = r.add_argument_group('labelling (livelabel.py): new steps only')
    g.add_argument('--no-ask', action='store_true', help='no labelling worker: cache hits and kept labels only')
    g.add_argument('--llm-url', default='http://127.0.0.1:8080',
                   help="a llama-server the user runs there is used as is (default http://127.0.0.1:8080); else one "
                        "is started as a child on a free port when needed")
    g.add_argument('--llm-runtime', default='b11960', help="the child's runtime in ~/.cache/remcli-runtimes ('' for "
                                                           "the system llama-server)")
    g.add_argument('--llm-model', default=ll.MODEL)
    g.add_argument('--llm-cmd', help='the child command instead, {port} in it (shell words)')
    g.add_argument('--llm-sleep', type=int, default=ll.SLEEP_IDLE, help='--sleep-idle-seconds of the child')
    g.add_argument('--idle-stop', type=float, default=ll.IDLE_STOP, help='s with nothing to ask before the child stops')
    g.add_argument('--vram', type=int, default=ll.VRAM_NEED, help='MiB of free VRAM to start the child')
    g.add_argument('--ram', type=int, default=ll.RAM_NEED, help='MiB of available RAM to start the child')
    g.add_argument('--hot', type=int, default=ll.HOT, help='pause at this GPU °C')
    g.add_argument('--cool', type=int, default=ll.COOL, help='resume below this GPU °C')
    g.add_argument('--quiet', type=float, default=ll.QUIET, help='s a blob is left alone before it is asked')
    g.add_argument('--cap', type=float, default=ll.CAP, help='s at most a wanted blob waits')
    g.add_argument('--run-log', help='default demo/label-runs/NAME.live.jsonl')
    g.add_argument('--smi', default='nvidia-smi', help='nvidia-smi command (shell words)')
    s = sub.add_parser('stop')
    s.add_argument('name')
    for p in (r, s):
        p.add_argument('--store', help='store directory (default: live/NAME next to this file)')
    a = ap.parse_args(argv)
    store = a.store or os.path.join(HERE, 'live', a.name)
    pidfile = os.path.join(store, 'serve.pid')
    pid = pid_running(pidfile)
    if a.cmd == 'stop':
        if pid is None:
            print('liveserve: not running', file=sys.stderr)
            return 1
        os.kill(pid, signal.SIGTERM)
        return 0
    if pid is not None:
        print(f'liveserve: already running (pid {pid})', file=sys.stderr)
        return 1
    os.makedirs(store, exist_ok=True)
    ask = None
    if not a.no_ask:
        import shlex
        cmd, cwd = (shlex.split(a.llm_cmd), None) if a.llm_cmd else ll.llama_cmd(a.llm_runtime, a.llm_model, a.llm_sleep)
        ask = dict(run_log=a.run_log or os.path.join(HERE, 'demo', 'label-runs', f'{a.name}.live.jsonl'),
                   llm_url=a.llm_url, llm_cmd=cmd, llm_cwd=cwd, smi=shlex.split(a.smi),
                   llm_out=os.path.join(store, 'llama-server.log'), vram=a.vram, ram=a.ram, idle_stop=a.idle_stop,
                   hot=a.hot, cool=a.cool, quiet=a.quiet, cap=a.cap)
    srv, live, stop, _ = serve(store, os.path.abspath(a.labels), a.host, a.port, a.relabel_at, a.poll, ask=ask)
    me = f'{os.getpid()} {proc_start(os.getpid())}'
    with open(pidfile + '.tmp', 'w') as fh:
        fh.write(me)
    os.replace(pidfile + '.tmp', pidfile)

    def down(*_):
        stop.set()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, down)
    print(f'liveserve: http://{a.host}:{srv.server_address[1]}/ ({store}, labels {a.labels})', file=sys.stderr)
    try:
        srv.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()
        if live.worker_thread is not None:
            live.worker.wake()
            live.worker_thread.join(30)     # (it stops the llama-server it started)
        try:
            with open(pidfile) as fh:
                if fh.read().strip() == me:
                    os.unlink(pidfile)
        except OSError:
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
