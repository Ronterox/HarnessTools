"""Diffs inside blobs: the texts the steps show, stored once by hash, and how a client reads a diff (SCHEMA.md "Diffs").

The timeline already names every function body by `funcs[].hash` (sha1(body)[:10], analyze.py). A BodySink, set as
timeline.Analyzer.sink, stores each body (and each file / text blob's text, and each seeded base) the first time a
step shows it, keyed by the same 10-hex hash (sha1 of the UTF-8 text), and leaves one file line per step:

    {"f": {path: hash | "bin:N" | null}, "seed": {path: hash | "bin:N"}, "sq": {qual: hash}, "sqa": [qual]}
                                                                                           (keys only when not empty)

`f`: the file / text blobs whose text changed since the previous analysed step (null: gone; "bin:N": a binary file of
N bytes, no text). `seed`: a seeded step's base (replay.Step.before: the text the edit applied to, from git, an Edit's
originalFile or a Read). `sq`: the bodies of the seeded file's functions (those it has after the step) in that base,
by qual, from the analyzer's own parse of the base (analyze.load_module); where that parse fails, sliced by a line
diff of base and the file after the step to each function's start..end, and those quals listed in `sqa`
(approximate). Only the seen set stays in memory.

Where the texts go:
- static, `timeline.py --diffs DIR` (Static): DIR/index.json.gz {"v": 1, "steps": N, "fv": [file line per step],
  "chunks": {"<step>": [hash, ...]}} and DIR/s/<step>.json.gz {hash: text} with the texts first shown at that step;
  plain gzip files (a static server serves them, the page reads them with DecompressionStream). Byte-identical runs.
- live (live.py): live/NAME/diffs/bodies.ndjson, one ["hash", "text"] line per text, append-only (it outlives
  rebuilds); diffs/steps.ndjson, the file line of each committed step (cut back with steps.ndjson); diffs/tail.json,
  the provisional steps' file lines. liveserve.py serves them (`fv` on each step, GET /live/bodies?h=...).
  export_store() writes the static layout from a store (`live.py export --diffs DIR`), byte for byte what
  timeline.py --diffs writes for the same steps.

Reader: the reference client, which the page mirrors (range_texts: the old and new texts of a blob between steps
a and b)."""
import glob, gzip, hashlib, json, os, re, threading
from difflib import SequenceMatcher

from analyze import _is_binary, lang_for, load_module

HASH = re.compile(r'[0-9a-f]{10}\Z')
MAX_GET = 256               # hashes per /live/bodies request


def h10(text):
    return hashlib.sha1(text.encode('utf-8', 'surrogatepass')).hexdigest()[:10]


def dumps(x):
    return json.dumps(x, ensure_ascii=False, separators=(',', ':'))


def _utf8(x):
    """x as compact JSON in UTF-8 (\\u-escaped when a lone surrogate cannot be encoded)."""
    try:
        return dumps(x).encode('utf-8')
    except UnicodeEncodeError:
        return json.dumps(x, separators=(',', ':')).encode('ascii')


def tail_digest(lines):
    """The provisional steps' id in diffs/tail.json: sha1 of their lines joined as provisional.json holds them."""
    return hashlib.sha1(','.join(lines).encode('utf-8', 'surrogatepass')).hexdigest()[:16]


def _pack_hashes(path):
    """The hashes a pack holds; a half-written last line (a writer that died in an append) is cut off."""
    seen, end = set(), 0
    try:
        fh = open(path, 'rb')
    except FileNotFoundError:
        return seen
    with fh:
        for line in fh:
            if not line.endswith(b'\n'):
                break
            end += len(line)
            seen.add(line[2:12].decode('ascii', 'replace'))
    if os.path.getsize(path) > end:
        os.truncate(path, end)
    return seen


class BodySink:
    """Texts by hash, each once. pack: a bodies.ndjson to append to (its hashes count as seen); without one the new
    texts wait in `new` for their owner (Static.push). flush() appends what is new to the pack: live calls it before
    every write that names a hash, so a hash a reader sees is always in the pack."""

    def __init__(self, pack=None):
        self.seen, self.new, self.fh = set(), [], None
        if pack is not None:
            self.seen = _pack_hashes(pack)
            self.fh = open(pack, 'ab', buffering=0)

    def put(self, text, h=None):
        h = h or h10(text)
        if h not in self.seen:
            self.seen.add(h)
            self.new.append((h, text))
        return h

    def text(self, rel, text):
        return 'bin:%d' % len(text.encode('utf-8', 'surrogatepass')) if _is_binary(rel, text) else self.put(text)

    def flush(self):
        if self.fh is None or not self.new:
            return
        view = memoryview(b''.join(_utf8([h, t]) + b'\n' for h, t in self.new))
        while view:
            view = view[self.fh.write(view):]
        self.new = []

    def close(self):
        self.flush()
        if self.fh is not None:
            self.fh.close()

    def step(self, an, st, result, snap):
        """Store what step st shows (result: its zoned analysis, None when it failed: the state stays) -> its file
        line. an.fmap ({path: hash} of the file / text blobs) moves on with the analysed state."""
        line = {}
        if result is not None:
            fmap, old, prev = {}, an.fmap, an.prev_files
            for b in result['blobs']:
                if b['kind'] in ('file', 'text'):
                    p = b['path']
                    text = snap.get(p, '')
                    fmap[p] = old[p] if p in old and prev.get(p) is text else self.text(p, text)
                    continue
                for f in b['funcs']:
                    if f['hash'] not in self.seen:
                        self.put(result['bodies'][f['qual']], f['hash'])
            f = {p: h for p, h in fmap.items() if old.get(p) != h}
            f.update((p, None) for p in old if p not in fmap)
            an.fmap = fmap
            if f:
                line['f'] = f
        seed, sq, approx = {}, {}, []
        for rel, base in (getattr(st, 'before', None) or {}).items():
            seed[rel] = self.text(rel, base)
            if result is not None and not seed[rel].startswith('bin:'):
                got, rough = self._base_bodies(rel, base, snap.get(rel), [
                    (f, result['bodies'][f['qual']]) for b in result['blobs'] for f in b['funcs'] if f['file'] == rel])
                sq.update(got)
                approx += rough
        if seed:
            line['seed'] = seed
        if sq:
            line['sq'] = sq
        if approx:
            line['sqa'] = approx
        return line

    def _base_bodies(self, rel, base, after, funcs):
        """({qual: hash of its body in base}, [approximate quals]) for the functions (f, body) of rel after the step:
        base parsed as the analyzer parses it, bodies taken by qual (exact); where the parse fails, _sliced()."""
        L = lang_for(rel)
        if L is not None:
            info = load_module(rel, base, None, L)
            if 'error' not in info:
                have = {g.qual: g.body for g in info['funcs'] + ([info['top']] if info.get('top') else [])}
                return {f['qual']: self.put(have[f['qual']]) for f, _ in funcs if f['qual'] in have}, []
        if not isinstance(after, str):
            return {}, []
        got = self._sliced(base, after, funcs)
        return got, list(got)

    def _sliced(self, base, after, funcs):
        """{qual: hash of its body in base}: each function's start..end in `after` mapped to base's lines through
        their line diff (equal runs line by line, a replaced run whole), from the first to the last mapped line (so a
        run deleted inside the function is in it). Lines are split at \\n only, as tree-sitter counts them."""
        bl, al = base.split('\n'), after.split('\n')
        ops = SequenceMatcher(None, bl, al, autojunk=False).get_opcodes()
        out = {}
        for f, body in funcs:
            js, je = f['start'] - 1, f['end']
            if f['name'] == '<module>' or body.count('\n') != je - js - 1:     # not one run of lines
                continue
            lo = hi = None
            for tag, i1, i2, j1, j2 in ops:
                if tag == 'equal':
                    a0, a1 = max(j1, js), min(j2, je)
                    if a0 >= a1:
                        continue
                    i1, i2 = i1 + a0 - j1, i1 + a1 - j1
                elif tag != 'replace' or not (j1 < je and j2 > js):
                    continue
                lo, hi = i1 if lo is None else min(lo, i1), i2 if hi is None else max(hi, i2)
            if lo is not None:
                out[f['qual']] = self.put('\n'.join(bl[lo:hi]).strip())
        return out


# -- static layout ----------------------------------------------------------------------------------------------------
def _write_gz(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'wb') as fh:
        fh.write(gzip.compress(_utf8(obj), 6, mtime=0))
    os.replace(tmp, path)


class Static:
    """The static layout in out/ (module doc), written as the steps are made: push(file line) after each step
    (the step's new texts: what the sink holds, or `pairs`), close() writes the index."""

    def __init__(self, out):
        self.out, self.sink, self.fv, self.chunks = out, BodySink(), [], {}
        os.makedirs(os.path.join(out, 's'), exist_ok=True)
        for f in glob.glob(os.path.join(out, 's', '*.json.gz')):     # (chunks of an earlier build)
            os.unlink(f)

    def push(self, fv, pairs=None):
        if pairs is None:
            pairs, self.sink.new = self.sink.new, []
        i = len(self.fv)
        self.fv.append(fv)
        if pairs:
            d = dict(sorted(pairs))
            _write_gz(os.path.join(self.out, 's', f'{i}.json.gz'), d)
            self.chunks[str(i)] = list(d)

    def close(self):
        _write_gz(os.path.join(self.out, 'index.json.gz'),
                  {'v': 1, 'steps': len(self.fv), 'fv': self.fv, 'chunks': self.chunks})


def step_hashes(st, fv):
    """The hashes a step names, in order: its functions' `hash` (a compacted step names each new one, a patch only
    changed ones) and its file line's."""
    for b in st.get('blobs', ()):
        for f in b.get('funcs', ()):
            if 'hash' in f:
                yield f['hash']
    for k in ('f', 'seed', 'sq'):
        for h in (fv.get(k) or {}).values():
            if h and not h.startswith('bin:'):
                yield h


def store_fvs(store, lines, prov):
    """The file lines of a store's committed steps (`lines`) and provisional ones (`prov`, as live.read_store()
    gives them). ValueError when the store has none for them (a store from before diffs, or written meanwhile)."""
    d = os.path.join(store, 'diffs')
    with open(os.path.join(d, 'steps.ndjson'), 'rb') as fh:
        data = fh.read()
    have = data[:data.rfind(b'\n') + 1].decode('utf-8').split('\n')[:-1]
    if len(have) < len(lines):
        raise ValueError(f'diffs/steps.ndjson has {len(have)} lines for {len(lines)} steps')
    out = [json.loads(l) for l in have[:len(lines)]]
    if prov:
        fv = tail_fvs(store, prov)
        if fv is None:
            raise ValueError('diffs/tail.json does not match provisional.json (written meanwhile: try again)')
        out += fv
    return out


def tail_fvs(store, prov):
    """The file lines of provisional steps `prov` (lines) from diffs/tail.json, None when it has none for them."""
    try:
        with open(os.path.join(store, 'diffs', 'tail.json'), encoding='utf-8') as fh:
            tails = json.load(fh)['tails']
    except (OSError, ValueError, KeyError, TypeError):
        return None
    h = tail_digest(prov)
    return next((t['fv'] for t in tails if t.get('h') == h and len(t.get('fv', ())) == len(prov)), None)


def export_store(store, out):
    """A live store's committed steps and tail -> the static layout in out/ (as timeline.py --diffs writes it)."""
    import live
    _, lines, prov = live.read_store(store)
    fvs = store_fvs(store, lines, prov)
    pack, w, seen = Pack(os.path.join(store, 'diffs', 'bodies.ndjson')), Static(out), set()
    for line, fv in zip(lines + prov, fvs):
        hs = list(dict.fromkeys(h for h in step_hashes(json.loads(line), fv) if h not in seen))
        seen.update(hs)
        got = pack.get(hs)
        lost = [h for h in hs if got[h] is None]
        if lost:
            raise ValueError(f'{len(lost)} texts not in diffs/bodies.ndjson, e.g. {lost[0]}')
        w.push(fv, [(h, got[h]) for h in hs])
    w.close()
    return out


# -- live: reading the pack ---------------------------------------------------------------------------------------------
class Pack:
    """bodies.ndjson read by hash (thread-safe): where each line starts, read on as the file grows. A file replaced,
    shorter than what was read or with another first line is read from the start again; a line being written is
    not read. Every call looks (one stat, one short read when nothing changed), and a line read back must hold the
    hash asked for: otherwise the file is read again once, and a hash still not found is None."""

    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        self.index, self.off, self.ino, self.head = {}, 0, None, b''

    def _reset(self):
        self.index, self.off, self.ino, self.head = {}, 0, None, b''

    def _scan(self):
        try:
            st = os.stat(self.path)
            fh = open(self.path, 'rb')
        except OSError:
            self._reset()
            return
        with fh:
            if st.st_ino != self.ino or st.st_size < self.off or fh.read(len(self.head)) != self.head:
                self._reset()
                self.ino = st.st_ino
            if st.st_size == self.off:
                return
            fh.seek(self.off)
            for line in fh:
                if not line.endswith(b'\n'):
                    break
                if not self.off:
                    self.head = line
                self.index.setdefault(line[2:12].decode('ascii', 'replace'), (self.off, len(line)))
                self.off += len(line)

    def _read(self, hashes):
        """{hash: text, None when not indexed, or False when the line there does not hold it}."""
        out, fh = {}, None
        try:
            for h in hashes:
                at = self.index.get(h)
                if at is None:
                    out[h] = None
                    continue
                try:
                    if fh is None:
                        fh = open(self.path, 'rb')
                    fh.seek(at[0])
                    got = json.loads(fh.read(at[1]))
                    out[h] = got[1] if isinstance(got, list) and len(got) == 2 and got[0] == h \
                        and isinstance(got[1], str) else False
                except (OSError, ValueError):
                    out[h] = False
        finally:
            if fh is not None:
                fh.close()
        return out

    def get(self, hashes):
        """{hash: text, or None when the pack has none}."""
        with self.lock:
            self._scan()
            out = self._read(hashes)
            if any(v is False for v in out.values()):     # (the file changed under the index)
                self._reset()
                self._scan()
                out = self._read(hashes)
            return {h: v if v is not False else None for h, v in out.items()}


# -- the reference client -------------------------------------------------------------------------------------------------
class Reader:
    """What a client does to show a blob's diff (SCHEMA.md "Diffs"): steps (full states, timeline.expand()), their
    file lines `fvs` and get(hashes) -> {hash: text}."""

    def __init__(self, steps, fvs, get):
        self.steps, self.fvs, self.get = steps, fvs, get

    def fmap(self, i):
        """{path: hash | "bin:N"} of the file / text blobs after step i (i = -1: before step 0, empty)."""
        m = {}
        for fv in self.fvs[:i + 1]:
            for p, h in (fv.get('f') or {}).items():
                if h is None:
                    m.pop(p, None)
                else:
                    m[p] = h
        return m

    def _seed(self, a, b, kind, key, first):
        """The earliest seed of path / qual `key` at steps a..first (first: the step of a..b it first shows at; a seed
        after a step that made it is no base of it) -> (hash, step), or (None, None)."""
        for i in range(a, min(b, first) + 1):
            h = (self.fvs[i].get(kind) or {}).get(key)
            if h is not None:
                return h, i
        return None, None

    def _first_qual(self, a, b, q):
        return next((i for i in range(a, b + 1) for x in self.steps[i]['blobs'] if any(f['qual'] == q for f in x['funcs'])), b)

    def _first_path(self, a, b, path):
        return next((i for i in range(a, b + 1) if (self.fvs[i].get('f') or {}).get(path) is not None), b)

    def range_texts(self, a, b, bid):
        """The diff of blob `bid` from before step a to after step b (0 <= a <= b): [entry], each {"kind": "func" |
        "file", "key": qual | path, "group": merged entry id, piece id or bid, "state": "new" | "deleted" |
        "changed" | "same", "oldh", "newh", "old", "new" (texts, None when absent or binary)} plus, when they apply,
        "moved_from" / "moved_to" (blob id), "renamed_from" (qual / path), "seeded" (the old side is a seed's),
        "approx" (that seed was sliced, not parsed), "binary" (no texts)."""
        old = self.steps[a - 1] if a > 0 else {'blobs': []}
        new = self.steps[b]
        b0 = next((x for x in old['blobs'] if x['id'] == bid), None)
        b1 = next((x for x in new['blobs'] if x['id'] == bid), None)
        if b0 is None and b1 is None:
            return []
        if (b1 or b0)['kind'] in ('file', 'text'):
            out = [self._file(a, b, (b1 or b0)['path'])]
        else:
            out = self._code(a, b, old, new, bid, b0, b1)
        hs = list(dict.fromkeys(h for e in out for h in (e['oldh'], e['newh']) if h and not h.startswith('bin:')))
        texts = self.get(hs) if hs else {}
        for e in out:
            e['old'] = texts.get(e['oldh']) if e['oldh'] and not e.get('binary') else None
            e['new'] = texts.get(e['newh']) if e['newh'] and not e.get('binary') else None
        return out

    @staticmethod
    def _state(oh, nh):
        return 'new' if oh is None else 'deleted' if nh is None else 'same' if oh == nh else 'changed'

    def _file(self, a, b, path):
        f0, f1 = self.fmap(a - 1), self.fmap(b)
        oh, nh, e = f0.get(path), f1.get(path), {'kind': 'file', 'key': path, 'group': 'file:' + path}
        if oh is None:
            oh, _ = self._seed(a, b, 'seed', path, self._first_path(a, b, path))
            if oh is not None:
                e['seeded'] = True
        if oh is None and nh is not None:       # renamed: a file gone in a..b with the same text
            gone = next((p for p, h in f0.items() if h == nh and p not in f1), None)
            if gone is not None:
                oh, e['renamed_from'] = nh, gone
        if any(h and h.startswith('bin:') for h in (oh, nh)):
            e['binary'] = True
        e.update(state=self._state(oh, nh), oldh=oh, newh=nh)
        return e

    def _code(self, a, b, old, new, bid, b0, b1):
        fam0 = [x for x in old['blobs'] if x['id'] == bid or x.get('cut') == bid]   # the blob and its cut pieces
        fam1 = [x for x in new['blobs'] if x['id'] == bid or x.get('cut') == bid]
        ids = {x['id'] for x in fam0 + fam1}
        group = {}
        for x in fam0 + fam1:
            for f in x['funcs']:
                group[f['qual']] = x['id']
            for m in x.get('merged', ()):
                for q in m['funcs']:
                    group[q] = m['id']
        q0 = {f['qual']: (f, x['id']) for x in old['blobs'] for f in x['funcs']}
        q1 = {f['qual']: (f, x['id']) for x in new['blobs'] for f in x['funcs']}
        mine0 = [f['qual'] for x in fam0 for f in x['funcs']]
        mine1 = [f['qual'] for x in fam1 for f in x['funcs']]
        out, by = [], {}
        for q in mine1:
            f, _ = q1[q]
            e = {'kind': 'func', 'key': q, 'group': group[q], 'name': f['name']}
            oh = None
            if q in q0:
                oh = q0[q][0]['hash']
                if q0[q][1] not in ids:
                    e['moved_from'] = q0[q][1]
            else:
                oh, s = self._seed(a, b, 'sq', q, self._first_qual(a, b, q))
                if oh is not None:
                    e['seeded'] = True
                    if q in (self.fvs[s].get('sqa') or ()):
                        e['approx'] = True
            e.update(oldh=oh, newh=f['hash'])
            out.append(e)
            by[q] = e
        for q in mine0:
            if q in by:
                continue
            f, _ = q0[q]
            e = {'kind': 'func', 'key': q, 'group': group[q], 'name': f['name'], 'oldh': f['hash'], 'newh': None}
            if q in q1:
                e['moved_to'], e['newh'] = q1[q][1], q1[q][0]['hash']
            out.append(e)
            by[q] = e
        added = [e for e in out if e['oldh'] is None]
        gone = [e for e in out if e['newh'] is None]
        for same in (lambda x, y: x['oldh'] == y['newh'], lambda x, y: x['name'] == y['name']):   # renames
            for e in added:
                g = next((g for g in gone if same(g, e)), None)
                if g is not None and e['oldh'] is None:
                    e['oldh'], e['renamed_from'] = g['oldh'], g['key']
                    gone.remove(g)
                    out.remove(g)
        for e in out:
            e['state'] = self._state(e['oldh'], e['newh'])
            e.pop('name')
        return out


def _gz_json(path):
    with open(path, 'rb') as fh:
        return json.loads(gzip.decompress(fh.read()).decode('utf-8'))


def load_static(timeline_path, ddir):
    """A Reader over timeline.json and its `--diffs` directory."""
    import timeline
    with open(timeline_path, encoding='utf-8') as fh:
        steps = timeline.expand(json.load(fh))
    index = _gz_json(os.path.join(ddir, 'index.json.gz'))
    where = {h: s for s, hs in index['chunks'].items() for h in hs}
    chunks = {}

    def get(hs):
        out = {}
        for h in hs:
            s = where.get(h)
            if s is not None and s not in chunks:
                chunks[s] = _gz_json(os.path.join(ddir, 's', f'{s}.json.gz'))
            out[h] = chunks[s].get(h) if s is not None else None
        return out
    return Reader(steps, index['fv'], get)


def load_live(store):
    """A Reader over a live store (committed steps, then the tail)."""
    import live, timeline
    steps = timeline.expand(json.loads(live.export_text(store)))
    _, lines, prov = live.read_store(store)
    pack = Pack(os.path.join(store, 'diffs', 'bodies.ndjson'))
    return Reader(steps, store_fvs(store, lines, prov), pack.get)
