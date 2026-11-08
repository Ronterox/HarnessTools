"""Per-step performance harness for the aiblob pipeline (replay -> analyze -> deltas -> JSON).

    python perf.py <session.jsonl|project-dir> [--root DIR] [-o perf.csv] [--max-steps N]
                   [--max-seconds S] [--seed-from-disk] [--json timeline.json] [--page] [--full] [--v2]

Runs the same per-step pipeline as timeline.timeline() (timeline.analyzed_steps, incremental unless --full, then
timeline.Compactor unless --v2) and logs one CSV row per step (flushed, so a killed run keeps what it measured):
parse_ms / analyze_ms come from analyze()'s own stats, diff_ms is the rest of the step's analysis (the deltas of
timeline.build_state + the prev_* maps), compact_ms the v4 delta encoding, ser_ms the step's JSON encoding,
json_bytes its size, sent_blobs the blobs the step carries. The summary (stdout and <csv>.summary.json) has total
wall, p50/p95/max and mean-after-step-0 per-step ms, peak RSS, replay_ms and the page-side numbers: timeline.json
size and blobs x steps / funcs x steps of the full states the page rebuilds. --page also loads the written
timeline in node with the page's own delta code (parse, expand, per-step maps) and reports ms and heap growth.

--seed-from-disk starts the snapshot from root's current tree (git ls-files -co --exclude-standard when
root is a git work tree, else a walk; text only, skipping .git, node_modules, .venv, __pycache__ and
files > 1MB or binary), rewound through the session's own edits in reverse (an Edit's new_string goes
back to its old_string; a file whose first touch in the session is a Write is dropped), so early steps
see the app as it was before the session instead of only the files the transcript wrote.
"""
import argparse, json, os, resource, statistics, subprocess, sys, time

import replay, timeline

SKIP_DIRS = {'.git', 'node_modules', '.venv', 'venv', '__pycache__'}
MAX_BYTES = 1 << 20

def seed_tree(root):
    """{relpath: text} of root's text files as they are on disk now."""
    try:
        out = subprocess.run(['git', '-C', root, 'ls-files', '-co', '--exclude-standard', '-z'],
                             capture_output=True, check=True).stdout.decode('utf-8', 'replace')
        rels = [p for p in out.split('\0') if p]
    except (OSError, subprocess.CalledProcessError):
        rels = []
        for d, dirs, names in os.walk(root):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
            rels += [os.path.relpath(os.path.join(d, n), root) for n in names]
    files = {}
    for rel in rels:
        if SKIP_DIRS & set(rel.split('/')[:-1]):
            continue
        p = os.path.join(root, rel)
        try:
            if not os.path.isfile(p) or os.path.getsize(p) > MAX_BYTES:
                continue
            with open(p, 'rb') as fh:
                raw = fh.read()
            if b'\0' in raw[:8192]:
                continue
            files[rel] = raw.decode('utf-8')
        except (OSError, UnicodeDecodeError):
            continue
    return files

def tool_calls(path, root):
    """The edit-bearing tool calls of a transcript, parsed like replay.steps():
    yields (entry, tool name, tool_use id, said, turn, [(kind, relpath, old, new, replace_all)])."""
    said, turn = '', -1
    for o in replay.load_lines(path):
        if o.get('type') == 'user' and not o.get('isSidechain'):
            c = (o.get('message') or {}).get('content')
            if isinstance(c, str) or (isinstance(c, list) and any(b.get('type') == 'text' for b in c if isinstance(b, dict))):
                turn += 1
        if o.get('type') != 'assistant' or o.get('isSidechain'):
            continue
        for b in (o.get('message') or {}).get('content') or []:
            if b.get('type') == 'text' and b.get('text', '').strip():
                said = replay.first_line(b['text'])
            if b.get('type') != 'tool_use':
                continue
            name, inp = b.get('name'), b.get('input') or {}
            fp, edits = inp.get('file_path'), []
            if name == 'Write' and fp:
                edits = [('write', fp, None, inp.get('content', ''), False)]
            elif name == 'Edit' and fp:
                edits = [('edit', fp, inp.get('old_string', ''), inp.get('new_string', ''), inp.get('replace_all', False))]
            elif name == 'MultiEdit' and fp:
                edits = [('edit', fp, e.get('old_string', ''), e.get('new_string', ''), e.get('replace_all', False))
                         for e in inp.get('edits', [])]
            elif name == 'Bash':
                for m in replay.HEREDOC.finditer(inp.get('command', '')):
                    p = m.group(1) if m.group(1).startswith('/') else os.path.join(root, m.group(1))
                    edits.append(('write', p, None, m.group(3) + '\n', False))
            edits = [(k, os.path.relpath(p, root), old, new, ra) for k, p, old, new, ra in edits
                     if os.path.abspath(p).startswith(root + os.sep)]
            if edits:
                yield o, name, b.get('id'), said, turn, edits
                said = ''

def rewind(files, calls):
    """Undo the session's edits on the end-of-session tree, last first: the tree before the session."""
    files = dict(files)
    first = {}
    for *_, edits in calls:
        for kind, rel, *_ in edits:
            first.setdefault(rel, kind)
    for *_, edits in reversed(calls):
        for kind, rel, old, new, ra in reversed(edits):
            if kind == 'edit' and rel in files and new and new in files[rel]:
                files[rel] = files[rel].replace(new, old) if ra else files[rel].replace(new, old, 1)
    for rel, kind in first.items():
        if kind == 'write':
            files.pop(rel, None)
    return files

def seeded_steps(path, root, seed):
    """replay.steps() starting from `seed`, lazily: yields (Step without content, snapshot dict)."""
    calls = list(tool_calls(path, root))
    files = rewind(seed, calls)
    idx = 0
    for o, name, _tid, said, turn, edits in calls:
        for kind, rel, old, new, ra in edits:
            before = files.get(rel, '')
            if kind == 'write':
                after = new
            else:
                after = before if old not in before else before.replace(old, new) if ra else before.replace(old, new, 1)
            files[rel] = after
            a, r = replay._diff_counts(before, after)
            yield replay.Step(idx, o.get('timestamp', ''), name, rel, said, turn, a, r), dict(files)
            idx += 1

def pct(xs, q):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web', 'index.html')

def page_cost(json_path):
    """Load the timeline in node the way the page does: JSON.parse (r.json()), then the page's own expandSteps()
    (the /* <expand> */ block of web/index.html) and the per-step id -> blob / qual -> function maps normalize()
    builds. Reports ms per phase and heap growth (MB) with everything held, as the page holds it."""
    with open(PAGE, encoding='utf-8') as fh:
        html = fh.read()
    i, j = html.find('/* <expand>'), html.find('/* </expand> */')
    block = html[i:j] if i >= 0 and j > i else 'const expandSteps = tl => tl.steps;'
    js = (block + "\nconst fs=require('fs');const s=fs.readFileSync(process.argv[1],'utf8');global.gc&&gc();"
          "const h0=process.memoryUsage().heapUsed;let t=performance.now();const tl=JSON.parse(s);"
          "const parse=performance.now()-t;t=performance.now();const steps=expandSteps(tl);const expand=performance.now()-t;"
          "t=performance.now();const idx=steps.map(st=>{const blobs=new Map(),funcs=new Map(),owner=new Map();"
          "for(const b of st.blobs){blobs.set(b.id,b);for(const f of b.funcs||[]){funcs.set(f.qual,f);owner.set(f.qual,b.id);}}"
          "return {blobs,funcs,owner};});const maps=performance.now()-t;global.gc&&gc();"
          "const h1=process.memoryUsage().heapUsed;"
          "console.log(JSON.stringify({parse_ms:+parse.toFixed(1),expand_ms:+expand.toFixed(1),maps_ms:+maps.toFixed(1),"
          "heap_mb:+((h1-h0)/1048576).toFixed(1),steps:steps.length,blobs_at_end:steps.length?steps[steps.length-1].blobs.length:0,"
          "keep:idx.length}))")
    try:
        r = subprocess.run(['node', '--expose-gc', '-e', js, json_path], capture_output=True, text=True, timeout=600)
        return json.loads(r.stdout) if r.returncode == 0 else {'error': r.stderr.strip()[-300:]}
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return {'error': repr(e)}

COLS = ['idx', 'tool', 'file', 'files_in_snapshot', 'py_files', 'total_lines', 'parse_ms', 'analyze_ms', 'diff_ms',
        'compact_ms', 'ser_ms', 'step_ms', 'blobs', 'funcs', 'edges', 'sent_blobs', 'json_bytes', 'rss_mb']

def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

def run(session, root=None, out='perf.csv', max_steps=None, max_seconds=None, seed=False, json_out=None, page=False,
        full=False, v2=False):
    t_start = time.perf_counter()
    path = timeline.session_file(session)
    root = os.path.abspath(root or timeline.session_root(path) or '.')
    summary = {'session': path, 'root': root, 'seeded': seed, 'full': full, 'schema': 2 if v2 else timeline.SCHEMA}
    t = time.perf_counter()
    if seed:
        tree = seed_tree(root)
        summary.update(seed_ms=round((time.perf_counter() - t) * 1000, 1), seed_files=len(tree))
        it = seeded_steps(path, root, tree)
    else:
        it = ((st, st.content) for st in replay.steps(path, root))
    replay_s = time.perf_counter() - t - summary.get('seed_ms', 0) / 1000    # replay.steps() is eager
    stopped, n_in = None, 0

    def feed():          # the snapshots, with replay time and the stop rules accounted here
        nonlocal replay_s, stopped, n_in
        while True:
            t = time.perf_counter()
            nxt = next(it, None)
            replay_s += time.perf_counter() - t
            if nxt is None:
                return
            if max_steps is not None and n_in >= max_steps:
                stopped = f'max-steps {max_steps}'
                return
            if max_seconds is not None and time.perf_counter() - t_start > max_seconds:
                stopped = f'max-seconds {max_seconds}'
                return
            n_in += 1
            yield nxt

    snaps = {}           # the snapshot of the step being analysed, for the per-row file / line counts
    def remember():
        for st, snap in feed():
            snaps['cur'] = snap
            yield st, snap
    steps = timeline.analyzed_steps(remember(), full=full, timings=True)
    comp = timeline.Compactor()
    nlines = {}
    rows, n_blob_steps, n_func_steps, json_total = [], 0, 0, 0
    jfh = None
    if json_out:
        jfh = open(json_out, 'w', encoding='utf-8')
        meta = {'root': root, 'session': os.path.splitext(os.path.basename(path))[0]}
        if not v2:
            meta['schema'] = timeline.SCHEMA
        head = json.dumps(meta, ensure_ascii=False, separators=(',', ':'))
        jfh.write(head[:-1] + ',"steps":[')
        json_total += len(head.encode()) + 10
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    fh = open(out, 'w', encoding='utf-8')
    fh.write(','.join(COLS) + '\n')
    while True:
        t0 = time.perf_counter()
        step = next(steps, None)          # analyze() + timeline.build_state()
        if step is None:
            break
        t1 = time.perf_counter()
        wire = step if v2 else comp.push(step)
        t2 = time.perf_counter()
        s = json.dumps(wire, ensure_ascii=False, separators=(',', ':'))
        t3 = time.perf_counter()
        nb = len(s.encode('utf-8'))
        if jfh:
            jfh.write((',' if rows else '') + s)
        json_total += nb + 1
        snap = snaps['cur']
        for rel, text in snap.items():           # line counts, memoised per text object (not timed)
            c = nlines.get(rel)
            if c is None or c[0] is not text:
                nlines[rel] = (text, text.count('\n') + (1 if text and not text.endswith('\n') else 0))
        stats, step_blobs = step['stats'], step['blobs']
        nfuncs = sum(len(b['funcs']) for b in step_blobs)
        n_blob_steps += len(step_blobs)
        n_func_steps += nfuncs
        an_ms = stats['parse_ms'] + stats['analyze_ms']
        row = {'idx': step['idx'], 'tool': step['tool'], 'file': step['file'], 'files_in_snapshot': len(snap),
               'py_files': sum(r.endswith('.py') for r in snap), 'total_lines': sum(nlines[r][1] for r in snap),
               'parse_ms': stats['parse_ms'], 'analyze_ms': stats['analyze_ms'],
               'diff_ms': round(max(0.0, (t1 - t0) * 1000 - an_ms), 1), 'compact_ms': round((t2 - t1) * 1000, 1),
               'ser_ms': round((t3 - t2) * 1000, 1), 'step_ms': round((t3 - t0) * 1000, 1), 'blobs': len(step_blobs),
               'funcs': nfuncs, 'edges': len(step['edges']), 'sent_blobs': len(wire['blobs']),
               'json_bytes': nb, 'rss_mb': round(rss_mb(), 1)}
        rows.append(row)
        fh.write(','.join(json.dumps(row[c]) if isinstance(row[c], str) else str(row[c]) for c in COLS) + '\n')
        fh.flush()
    fh.close()
    if jfh:
        jfh.write(']}')
        jfh.close()
    col = lambda k: [r[k] for r in rows]
    last = rows[-1] if rows else {}
    summary.update(
        steps=len(rows), stopped=stopped, total_s=round(time.perf_counter() - t_start, 2),
        replay_ms=round(replay_s * 1000, 1),
        files=last.get('files_in_snapshot', 0), py_files=last.get('py_files', 0), lines=last.get('total_lines', 0),
        blobs=last.get('blobs', 0), funcs=last.get('funcs', 0), edges=last.get('edges', 0),
        **{f'{k}_sum_ms': round(sum(col(k)), 1) for k in ('parse_ms', 'analyze_ms', 'diff_ms', 'compact_ms', 'ser_ms', 'step_ms')},
        step_p50_ms=pct(col('step_ms'), .5), step_p95_ms=pct(col('step_ms'), .95), step_max_ms=max(col('step_ms'), default=0),
        step_mean_after_first_ms=round(sum(col('step_ms')[1:]) / max(1, len(rows) - 1), 1),
        analyze_p50_ms=pct(col('analyze_ms'), .5), analyze_p95_ms=pct(col('analyze_ms'), .95),
        peak_rss_mb=round(rss_mb(), 1), json_mb=round(json_total / 1e6, 2),
        blobs_x_steps=n_blob_steps, funcs_x_steps=n_func_steps, sent_blobs=sum(col('sent_blobs')))
    if page and json_out:
        summary['page'] = page_cost(json_out)
    with open(os.path.splitext(out)[0] + '.summary.json', 'w', encoding='utf-8') as sfh:
        json.dump(summary, sfh, indent=1)
    return rows, summary

def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('session', help='session .jsonl or a ~/.claude/projects/<dir> (latest .jsonl)')
    ap.add_argument('--root', help='app root the edits are relative to (default: session cwd)')
    ap.add_argument('-o', '--out', default='perf.csv', help='per-step CSV (summary goes to <out>.summary.json)')
    ap.add_argument('--max-steps', type=int, help='stop after N steps')
    ap.add_argument('--max-seconds', type=float, help='stop (with a summary) once the run passes S seconds')
    ap.add_argument('--seed-from-disk', action='store_true', help="start from root's on-disk tree, rewound through the session")
    ap.add_argument('--json', help='also write the timeline.json this run produces (streamed)')
    ap.add_argument('--page', action='store_true', help='with --json: load it in node like the page (parse + rebuild '
                    'every step from the deltas), report ms and heap')
    ap.add_argument('--full', action='store_true', help='analyze every step from scratch (timeline.py --full)')
    ap.add_argument('--v2', action='store_true', help='write / measure full-state steps (timeline.py --v2)')
    a = ap.parse_args()
    _, s = run(a.session, a.root, a.out, a.max_steps, a.max_seconds, a.seed_from_disk, a.json, a.page, a.full, a.v2)
    print(f"{a.out}: {s['steps']} steps{' (stopped: ' + s['stopped'] + ')' if s['stopped'] else ''}, "
          f"{s['files']} files ({s['py_files']} .py, {s['lines']} lines), {s['blobs']} blobs, {s['edges']} edges | "
          f"total {s['total_s']}s, replay {s['replay_ms']:.0f}ms, step p50 {s['step_p50_ms']}ms p95 {s['step_p95_ms']}ms "
          f"max {s['step_max_ms']}ms (parse {s['parse_ms_sum_ms']:.0f} analyze {s['analyze_ms_sum_ms']:.0f} "
          f"diff {s['diff_ms_sum_ms']:.0f} compact {s['compact_ms_sum_ms']:.0f} ser {s['ser_ms_sum_ms']:.0f} ms), "
          f"mean after step 0 {s['step_mean_after_first_ms']}ms | peak RSS {s['peak_rss_mb']}MB | "
          f"json {s['json_mb']}MB, blobs x steps {s['blobs_x_steps']}, funcs x steps {s['funcs_x_steps']}"
          + (f" | page {s['page']}" if 'page' in s else ''))

if __name__ == '__main__':
    main()
