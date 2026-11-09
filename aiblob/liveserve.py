"""Live server: serve a live.py store to the page, labelled, as it grows.

    python liveserve.py run NAME [--store DIR] [--port 8791] [--host 127.0.0.1] [--labels demo/labels.json]
                        [--relabel-at 0.2] [--poll 0.3]
    python liveserve.py stop NAME [--store DIR]          SIGTERM to the server (serve.pid: its pid and start time)

The store is live/NAME/ next to this file unless --store says otherwise (live.py's format: head.json, steps.ndjson,
provisional.json, status.json). The server only reads it: it serves what is there whether the writer runs or not,
and follows it while it does. Its pid file is serve.pid in the store (`pid start-time`, as live.pid).

HTTP (127.0.0.1 only):
- /                      web/index.html?live=1 (a redirect; ?live=0 or ?src= keep the static page);
  anything else outside /live/ is web/ as a static server serves it.
- /live/timeline.json    the store as `live.py export` writes it, labelled as `label.py T --cache LABELS --server
                         http://127.0.0.1:1` labels that export (cache hits and kept labels, never a model call), plus
                         "live": {epoch, steps (committed), provisional, gen}: steps[:steps] are committed, the rest is
                         the provisional tail.
- /live/events?epoch=E&from=N   server-sent events. "steps" {epoch, from, steps, tail, gen}: committed steps from
                         step `from` on (id: the committed count after them; Last-Event-ID resumes there) and the
                         whole tail as it stands with them. "tail" {epoch, base, gen, tail}: the tail changed alone.
                         "rebuild" {epoch, steps}: what was served no longer holds (the writer rebuilt the store
                         with other steps, head.json or the label cache changed): load /live/timeline.json again.
                         An event whose epoch is not the page's means the same.
- /live/status           {epoch, ready, steps, provisional, gen, rebuilds, writer (status.json), writer_running,
                         labels: {path, keys}, pid}.

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
label.py reads them when it runs. A change of the label cache file relabels everything (a rebuild). One known
difference, by design: label.py applies the final cache to every step at once, so a `merged` entry whose functions
go back to an earlier version that a later step got a kept label for shows that label on the earlier step too;
served step by step, the earlier step shows what was known then. On aigource: no difference with the full cache, 323
merged-entry labels on earlier steps with a random half of it. Phase 4 (labels pushed to the page) will give earlier
steps the labels found later.

Only 127.0.0.1 is bound by default, and a request whose Host is not this machine is refused (DNS rebinding)."""
import argparse, copy, http.server, json, os, signal, socketserver, sys, threading, time, urllib.parse
from types import SimpleNamespace

import label, replay, timeline

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, 'web')
CHUNK = 200                 # committed steps per "steps" event when a client is far behind


def dumps(x):
    return json.dumps(x, ensure_ascii=False, separators=(',', ':'))


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


class Labeller:
    """label.py's apply step (`label.py T --cache C --server http://127.0.0.1:1`, the server down: cache hits and
    kept labels only) one step at a time: push(raw compacted step) -> the labelled compacted step. Its decisions are
    plan_labels' (the Keeper fed every step from step 0, an ask standing in as an empty entry) and main()'s kept-label
    pass (a key reused from a cached label gets that label); apply_labels() and the Compactor then run on the step.
    The expanded state is never written to: each step's blobs are labelled on copies."""

    def __init__(self, root, cache, at=0.2, schema=timeline.SCHEMA):
        self.root, self.cache, self.at = root, cache, at
        self.work = dict(cache)             # plan_labels' `work`: the cache, reuses and stand-ins for asks
        self.applied = dict(cache)          # what apply_labels gets: the cache + kept labels
        self.keeper = label.Keeper(at)
        self.exp, self.comp = Expander(schema), timeline.Compactor(schema)
        self.heads, self.i = {}, 0

    def fork(self):
        f = copy.copy(self)
        f.work, f.applied = dict(self.work), dict(self.applied)
        k = f.keeper = label.Keeper(self.at)
        k.state = {u: list(s) for u, s in self.keeper.state.items()}   # (an entry's list is changed in place)
        f.exp, f.comp = self.exp.fork(), copy.copy(self.comp)
        return f

    def head(self, b):
        """label.file_head(), read again only when the file changed."""
        if b['kind'] not in ('file', 'text') or not b.get('path'):
            return []
        p = os.path.join(self.root, b['path'])
        try:
            s = os.stat(p)
            sig = (s.st_mtime_ns, s.st_size)
        except OSError:
            sig = None
        got = self.heads.get(p)
        if got is None or got[0] != sig:
            got = self.heads[p] = (sig, label.file_head(self.root, b))
        return got[1]

    def push(self, raw):
        step = self.exp.push(raw)
        step['blobs'] = [dict(b, merged=[dict(m) for m in b['merged']]) if 'merged' in b else dict(b)
                         for b in step['blobs']]
        for b in step['blobs']:
            h = self.head(b)
            if h:
                b['_head'] = h
        step['_keys'] = label.step_keys(step)
        for key in self.keeper.step(self.i, step, self.work):
            self.work[key] = {}
        self.keeper.settle(self.work)
        for key in self.keeper.reused:
            src = self.work[key]['reused_from']
            if key not in self.cache and src in self.cache:
                self.applied[key] = dict(self.cache[src], reused_from=src)
        self.i += 1
        label.apply_labels([step], self.applied)
        del step['_keys']
        for b in step['blobs']:
            b.pop('_head', None)
        return self.comp.push(step)


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
    header, status, ready."""

    def __init__(self, store, labels, at=0.2):
        self.store, self.labels_path, self.at = store, labels, at
        self.cond = threading.Condition()
        # epochs start at the clock (ms), so a page that outlived a server restart is told to load again
        self.epoch, self.lines, self.tail, self.tail_v, self.gen, self.ready = int(time.time() * 1000), [], [], 0, 0, False
        self.status, self.header, self.cache_keys, self.stats, self.broken = {}, '', 0, {}, False
        self._reset()

    path = lambda self, name: os.path.join(self.store, name)

    def _reset(self):
        """The poll thread's own state, from nothing."""
        self.head, self.raw, self.lab = None, [], None   # head.json; committed raw lines served; labeller after them
        self.ino, self.file_n, self.offset, self.last = None, 0, 0, None   # steps.ndjson: lines and bytes read,
        self.prov = None                                 # (offset, bytes) of the last line; provisional.json:
        self.tail_raw, self.tail_base = [], 0            # (base, raw lines, gen); the tail served: raw lines, base
        self.ids, self.counts, self.sessions, self.tail_ids, self.fallback = [], {}, [], [], None
        self.tail_counts = {}
        self.rebuilds, self.prov_gen, self.wstatus = 0, None, {}

    # -- polling --------------------------------------------------------------------------------------------------
    def poll(self):
        """Read what changed in the store; True when what is served changed."""
        stats = {n: _stat(self.path(n)) for n in ('head.json', 'steps.ndjson', 'provisional.json', 'status.json')}
        stats['labels'] = _stat(self.labels_path)
        if self.ready and stats == self.stats:
            return False
        old, self.stats = self.stats, stats
        if not self.ready or self.broken or stats['labels'] != old.get('labels') \
                or _read_json(self.path('head.json')) != self.head:
            return self._rebuild()
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
        tail = self._new_tail()
        if not new and tail is None and status is None:
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
            self.cond.notify_all()
        return bool(new) or tail is not None

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
        return dumps(self.lab.push(st))

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
        if raw == self.tail_raw and n == self.tail_base and gen == self.gen:
            return None
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
        self.lab = Labeller(head.get('root') or '.', cache, self.at, head.get('schema', timeline.SCHEMA))
        with self.cond:
            self.gen = 0
        lines = self._read_steps(True) or []
        tail = self._new_tail()
        header = self._header()
        with self.cond:
            self.lines, self.status, self.header, self.cache_keys = lines, status, header, len(cache)
            self.tail, self.gen = tail if tail is not None else ([], 0)
            self.tail_v += 1
            self.epoch += 1
            self.ready = True
            self.cond.notify_all()
        self.broken = False
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
        live = dumps({'epoch': epoch, 'steps': len(lines), 'provisional': len(tail), 'gen': gen})
        return '{' + header + f',"live":{live},"steps":[' + ','.join(lines + tail) + ']}'

    def status_obj(self):
        with self.cond:
            out = {'epoch': self.epoch, 'ready': self.ready, 'steps': len(self.lines), 'provisional': len(self.tail),
                   'gen': self.gen, 'rebuilds': self.status.get('rebuilds', 0), 'writer': self.status,
                   'labels': {'path': self.labels_path, 'keys': self.cache_keys}, 'pid': os.getpid()}
        out['writer_running'] = pid_running(self.path('live.pid')) is not None
        return out

    def follow(self, poll, stop):
        """The poll loop (a thread): poll() every `poll` s until `stop` is set; an error is printed, not fatal."""
        while not stop.is_set():
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
        except ValueError:
            sent = -1
        if sent < 0:
            return self.send_error(400, 'epoch and from are numbers, from a step count')
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        tail_v, quiet = None, 0.0
        try:
            while not self.stop.is_set():
                with lv.cond:
                    if epoch == lv.epoch and sent == len(lv.lines) and tail_v == lv.tail_v:
                        lv.cond.wait(1.0)
                    out = []
                    if epoch != lv.epoch or sent > len(lv.lines):
                        out.append(sse('rebuild', dumps({'epoch': lv.epoch, 'steps': len(lv.lines)})))
                        epoch, sent, tail_v = lv.epoch, len(lv.lines), lv.tail_v
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


def serve(store, labels, host='127.0.0.1', port=8791, at=0.2, poll=0.3, timeout=60):
    """(server, live, stop event, poll thread): the server bound, the store followed; serve_forever() is the
    caller's (stop: set `stop`, then server.shutdown()). timeout: seconds a client may leave a read or write
    hanging."""
    live, stop = Live(store, labels, at), threading.Event()
    handler = type('LiveHandler', (Handler,), {'live': live, 'stop': stop, 'timeout': timeout})
    srv = Server((host, port), handler)
    t = threading.Thread(target=live.follow, args=(poll, stop), daemon=True, name='liveserve-poll')
    t.start()
    return srv, live, stop, t


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('name')
    r.add_argument('--host', default='127.0.0.1')
    r.add_argument('--port', type=int, default=8791)
    r.add_argument('--labels', default=os.path.join(HERE, 'demo', 'labels.json'), help='label cache (read only)')
    r.add_argument('--relabel-at', type=float, default=0.2, help="label.py's --relabel-at")
    r.add_argument('--poll', type=float, default=0.3)
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
    srv, live, stop, _ = serve(store, os.path.abspath(a.labels), a.host, a.port, a.relabel_at, a.poll)
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
        try:
            with open(pidfile) as fh:
                if fh.read().strip() == me:
                    os.unlink(pidfile)
        except OSError:
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
