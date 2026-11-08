import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from analyze import analyze


def run(files):
    """analyze() on a {relpath: source} snapshot -> (result, {blob id: blob}, edges)."""
    r = analyze(files)
    return r, {b['id']: b for b in r['blobs']}, r['edges']


def quals(blob):
    return sorted(f['qual'] for f in blob['funcs'])


def call(blob, qual, to):
    """The call entry from function `qual` of `blob` to `to`."""
    f = next(f for f in blob['funcs'] if f['qual'] == qual)
    hits = [c for c in f['calls'] if c['to'] == to]
    assert len(hits) == 1, (qual, to, f['calls'])
    return hits[0]


def edges_of(edges, kind, a=None, b=None):
    return [e for e in edges if e['kind'] == kind and (a is None or e['a'] == a) and (b is None or e['b'] == b)]
