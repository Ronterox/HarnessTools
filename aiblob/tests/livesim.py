"""A simulated live.py writer for the server tests: a store in live.py's format (live.py's docstring) fed from a
finished timeline, step by step, with a provisional tail, a half-written line and rebuilds."""
import json, os

import areas, timeline


def dumps(x):
    return json.dumps(x, ensure_ascii=False, separators=(',', ':'))


def write_atomic(path, text):
    with open(path + '.tmp', 'w', encoding='utf-8') as fh:
        fh.write(text)
    os.replace(path + '.tmp', path)


def raw_timeline(path_or_tl):
    """A timeline (a file or a dict, labelled or not) as timeline.py writes it before label.py: -> (head fields,
    [step line]), the labels taken off and the areas' captions and faces made again without them."""
    tl = path_or_tl
    if isinstance(tl, str):
        with open(tl, encoding='utf-8') as fh:
            tl = json.load(fh)
    steps = timeline.expand(tl)
    for st in steps:
        for b in st['blobs']:
            b.pop('label', None)
            for m in b.get('merged', ()):
                m.pop('label', None)
        if 'areas' in st:
            st['areas'] = areas.caption_table(st['blobs'], [a['id'] for a in st['areas']])
    schema = tl.get('schema', timeline.SCHEMA)
    lines = [dumps(s) for s in timeline.compact(steps, schema)]
    return {'root': tl.get('root'), 'schema': schema}, lines


class SimWriter:
    """Writes a store the way live.py's Writer does: head.json, steps.ndjson appended, provisional.json and
    status.json rewritten atomically (the tail: the next steps after the committed ones)."""

    def __init__(self, store, head, inputs=('/nonexistent/sim.jsonl',)):
        self.store, self.n, self.gen, self.rebuilds, self.tail = store, 0, 0, 0, []
        os.makedirs(store, exist_ok=True)
        self.head = dict(inputs=list(inputs), root=head['root'], seed_git=False, git_reconcile=False, smart=True,
                         many=False, schema=head['schema'])
        write_atomic(self.path('head.json'), dumps(self.head))
        open(self.path('steps.ndjson'), 'w').close()

    path = lambda self, name: os.path.join(self.store, name)

    def commit(self, lines):
        with open(self.path('steps.ndjson'), 'a', encoding='utf-8') as fh:
            for l in lines:
                fh.write(l + '\n')
        self.n += len(lines)

    def half(self, line, cut=None):
        """The first part of a line, no newline yet (a writer in the middle of an append)."""
        with open(self.path('steps.ndjson'), 'a', encoding='utf-8') as fh:
            fh.write(line[:cut or len(line) // 2])

    def finish_half(self, line, cut=None):
        with open(self.path('steps.ndjson'), 'a', encoding='utf-8') as fh:
            fh.write(line[cut or len(line) // 2:] + '\n')
        self.n += 1

    def set_tail(self, lines):
        self.tail = list(lines)
        self.gen += 1
        write_atomic(self.path('provisional.json'),
                     f'{{"gen":{self.gen},"base":{self.n},"late":0,"w":"","steps":[{",".join(self.tail)}]}}')
        self.status()

    def status(self):
        write_atomic(self.path('status.json'), dumps({'pid': 0, 'time': '', 'steps': self.n,
                                                       'provisional': len(self.tail), 'gen': self.gen, 'w': '',
                                                       'late': 0, 'rebuilds': self.rebuilds, 'files': 1}))

    def rebuild(self, lines, tail=()):
        """A cold build: steps.ndjson truncated and written again, gen from 1, rebuilds + 1."""
        self.rebuilds += 1
        self.gen, self.n = 0, 0
        write_atomic(self.path('head.json'), dumps(self.head))
        open(self.path('steps.ndjson'), 'w').close()
        self.commit(lines)
        self.set_tail(tail)
