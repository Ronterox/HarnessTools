"""live.py: transcripts written record by record under a simulated clock; the store, exported, is the batch build
(timeline.py --no-timings) byte for byte; committed steps never change; the provisional tail is always what a batch
build of the transcripts as they are would add; restarts from the checkpoint change nothing."""
import gc, json, os, pathlib, random, shutil, subprocess, time, tracemalloc
from datetime import datetime

import pytest

import live, replay, timeline
from test_chunking import _mixed, _racing
from test_replay import ROOT, SID, bash_result, msg, result, text, tool, ts, write_lines

HERE = os.path.dirname(os.path.abspath(__file__))
FLASK = os.path.expanduser('~/.claude/projects/-home-rontero-Documents-Projects-AIProjects-HarnessTools-aiblob-demo-app')
APP = os.path.join(os.path.dirname(HERE), 'demo', 'app')
sec = lambda s: datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()


class Feed:
    """A copy of a project's transcripts, written into dst record by record in the order they were written as far
    as the files tell (a record no earlier than every earlier record of its file, files merged by that time; a
    subagent's meta.json right after its first record), with that time as the clock. mtimes: 'history' (a file's
    mtime is the time it was last written), or a random.Random: random mtimes, earlier or later, as touch, cp, a
    restored backup or clock skew leave them (live must not depend on them)."""

    def __init__(self, src, dst, late_meta=(), mtimes='history'):
        self.dst, self.events, self.mtimes = dst, [], mtimes
        files = [f for f in replay.transcripts(src)]
        known = set()
        for n, f in enumerate(files):
            rel = os.path.relpath(f, src)
            with open(f, encoding='utf-8') as fh:
                lines = [l for l in fh.read().split('\n') if l.strip()]
            ident = [self._ident(l) for l in lines]
            times, run = [], ''
            for line in lines:
                try:
                    t = json.loads(line).get('timestamp')
                except ValueError:
                    t = None
                run = max(run, t) if isinstance(t, str) else run
                times.append(run)
            nxt = ''
            for k in range(len(lines) - 1, -1, -1):      # a copy of an earlier transcript's record is written when
                if ident[k] in known:                        # the session resumes: with its next own record
                    times[k] = max(times[k], nxt)
                else:
                    nxt = times[k]
            known |= set(ident)
            for k, line in enumerate(lines):
                self.events.append((times[k], n, k, rel, line))
            meta = f[:-len('.jsonl')] + '.meta.json'
            if os.path.exists(meta) and lines:          # with its first record, or (late_meta) 2 s after it
                at = self.events[-len(lines)][0]
                late = rel in late_meta
                self.events.append((live.iso(sec(at) + 2) if late else at, n, len(lines) if late else -1,
                                    os.path.relpath(meta, src), open(meta).read()))
        self.events.sort(key=lambda e: (e[0], e[1], e[2]))
        os.makedirs(dst, exist_ok=True)

    @staticmethod
    def _ident(line):
        try:
            o = json.loads(line)
        except ValueError:
            return line
        return json.dumps({k: o.get(k) for k in ('type', 'timestamp', 'message')}, sort_keys=True)

    def __iter__(self):
        """Write each record (meta files whole); yield the clock after it."""
        for at, _, _, rel, line in self.events:
            p = os.path.join(self.dst, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            if rel.endswith('.meta.json'):
                with open(p, 'w') as fh:
                    fh.write(line)
            else:
                with open(p, 'a') as fh:
                    fh.write(line + '\n')
            if self.mtimes != 'history':
                t = sec(ts(0)) + self.mtimes.uniform(-3600, 7200)
                os.utime(p, (t, t))
            elif at:
                os.utime(p, (sec(at), sec(at)))
            yield sec(at) if at else 0.0


def batch(proj, root, seed_git=False, git_reconcile=False, rest=None):
    rs = replay.steps(proj, root, seed_git=seed_git, git_reconcile=git_reconcile, rest=rest)
    return live.dumps(timeline.timeline(proj, root, timings=False, steps=rs))


def follow(tmp_path, src, root, seed_git=False, git_reconcile=False, restarts=0, check_every=1, seed=0, lag=5.0,
           alive=lambda: set(), between=None, late_meta=(), rebuild_ok=('a second transcript',), prov_every=0,
           start_at=0, mtimes='history', settle=60.0):
    """Feed src's transcripts to a Writer; check the invariants after every check_every-th tick; restart from the
    checkpoint at `restarts` random ticks. -> (writer, store, project copy). With prov_every (simulated seconds the
    tail may stay stale) only the committed steps are checked against the batch build at each tick. start_at: the
    record whose tick is the cold build (the ones before are written first)."""
    proj, store = str(tmp_path / 'proj'), str(tmp_path / 'store')
    feed = Feed(src, proj, late_meta, mtimes)
    rnd = random.Random(seed)
    at = set(rnd.sample(range(start_at + 1, len(feed.events)), min(restarts, max(0, len(feed.events) - start_at - 1))))
    mk = lambda: live.Writer(store, [proj], root, seed_git, git_reconcile, lag=lag, idle=600, alive=alive,
                             git_poll=0, scan=0, checkpoint_every=1e9, evict_every=0, prov_every=prov_every, settle=settle)
    w, committed, now, rebuilt = None, [], 0.0, 0
    for k, now in enumerate(feed):
        if between:
            between(k, now)
        if k < start_at:
            continue
        if w is None:
            w = mk()
            assert w.start(now) == 'cold'
            listed = replay.transcripts(proj)
            if any(replay.first_ts(f) == replay.first_ts(g) and _records(f) < _records(g) for f in listed for g in listed):
                # a cold build that finds a resume mid-copy cannot tell it from its original (transcripts() numbers
                # it first, by content); the copy's next records then are late, or flip the order: one rebuild
                rebuild_ok = tuple(rebuild_ok) + ('late', 'order')
        elif k in at and w.engine is not None:
            w.checkpoint(now)
            w = mk()
            assert w.start(now) == 'warm', w.cold_reason
        else:
            w.tick(now)
        if w.engine is None:
            continue
        head, lines, prov = live.read_store(store)
        if w.rebuilds != rebuilt:                   # only a second transcript where steps had no tags (checked below)
            rebuilt, committed = w.rebuilds, []
        assert lines[:len(committed)] == committed, 'a committed step changed'
        committed = lines
        for f in w.tailer.waiting:          # only a file holding nothing but a followed one's records may wait
            assert any(_records(f) <= _records(g) for g in w.tailer.files), f'tick {k}: {f} waits but is no copy'
        if k % check_every == 0 and not w.tailer.waiting:   # (a batch build of a half-copied resume is not one)
            want = [live.dumps(s) for s in json.loads(batch(proj, w.root, seed_git, git_reconcile, _rest(store)))['steps']]
            if prov_every:
                assert lines == want[:len(lines)], f'tick {k}: committed != batch'
            else:
                assert lines + prov == want, f'tick {k}: committed + tail != batch'
    w.tick(now + 36000)
    assert not w.tailer.waiting, w.tailer.waiting
    assert all(r.startswith(rebuild_ok) for r in w.reasons) and w.engine.late == 0, w.reasons
    return w, store, proj


def _records(path):
    return {o for o, _ in replay._records(path)}


def _rest(store):
    p = os.path.join(store, 'rest.json')
    return json.load(open(p)) if os.path.exists(p) else None


def exported_equals_batch(store, proj, root, **kw):
    assert live.export_text(store) == batch(proj, root, **kw)
    assert live.verify(store) == 0


def test_mixed_transcripts_live_equal_batch_with_restarts(tmp_path):
    src, _ = _mixed(tmp_path / 'src')
    w, store, proj = follow(tmp_path, src, ROOT, restarts=4)
    exported_equals_batch(store, proj, ROOT)


def test_racing_reports_live_equal_batch(tmp_path):
    src = _racing(tmp_path / 'src')
    w, store, proj = follow(tmp_path, src, ROOT, restarts=2, seed=1)
    exported_equals_batch(store, proj, ROOT)


def _fixture_session(tmp_path, fixture, root, edits):
    """A session writing a fixture tree file by file, then editing it, plus a subagent that patches one file."""
    base = os.path.join(HERE, 'fixtures', fixture)
    names = sorted(os.path.relpath(os.path.join(d, f), base) for d, _, fs in os.walk(base) for f in fs)
    recs, t = [msg('user', f'build {fixture}', ts=ts(0))], 1
    for k, name in enumerate(names):
        body = open(os.path.join(base, name), encoding='utf-8', errors='replace').read()
        recs += [msg('assistant', [text(f'Writing {name}'), tool(f'w{k}', 'Write', file_path=f'{root}/{name}', content=body)],
                     ts=ts(t), cwd=root), dict(result(f'w{k}'), timestamp=ts(t, 1))]
        t += 1
    for k, (name, old, new) in enumerate(edits):
        recs += [msg('assistant', [tool(f'e{k}', 'Edit', file_path=f'{root}/{name}', old_string=old, new_string=new)],
                     ts=ts(t), cwd=root), dict(result(f'e{k}'), timestamp=ts(t, 2))]
        t += 1
    proj = tmp_path / 'src'
    write_lines(proj / f'{SID}.jsonl', recs)
    name, old, new = edits[0]
    sub = [msg('user', 'patch it', ts=ts(t), isSidechain=True),
           msg('assistant', [tool('sb', 'Bash', command=f'sed -i s/{new}/{old}/ {name}')], ts=ts(t, 10), cwd=root, isSidechain=True),
           bash_result('sb', ts(t, 20), {'filePath': f'{root}/{name}', 'hunks': []})]
    write_lines(proj / SID / 'subagents' / 'agent-p.jsonl', sub)
    (proj / SID / 'subagents' / 'agent-p.meta.json').write_text(json.dumps({'agentType': 'executor', 'description': 'p'}))
    return str(proj)


def test_js_mini_live_equals_batch(tmp_path):
    root = '/work/js'
    src = _fixture_session(tmp_path, 'js-mini', root, [('server.js', 'express', 'express'), ('util.js', 'function', 'function ')])
    w, store, proj = follow(tmp_path, src, root, restarts=2, check_every=3)
    exported_equals_batch(store, proj, root)


def test_c_mini_live_equals_batch(tmp_path):
    root = '/work/c'
    src = _fixture_session(tmp_path, 'c-mini', root, [('main.c', 'int main', 'int  main'), ('util.c', 'return', 'return ')])
    w, store, proj = follow(tmp_path, src, root, restarts=2, check_every=3, seed=2)
    exported_equals_batch(store, proj, root)


@pytest.mark.skipif(not os.path.isdir(FLASK), reason='recorded demo session not on this machine')
def test_flask_session_live_equals_timeline_py(tmp_path):
    w, store, proj = follow(tmp_path, FLASK, APP, restarts=3, check_every=10)
    exported_equals_batch(store, proj, APP)
    # and as timeline.py writes it from the same files
    out = tmp_path / 't.json'
    subprocess.run([os.sys.executable, os.path.join(os.path.dirname(HERE), 'timeline.py'), proj, '--root', APP,
                    '--no-timings', '-o', str(out)], check=True, capture_output=True)
    assert live.export_text(store) == out.read_text()


def test_git_seed_and_reconcile_with_a_commit_made_while_live(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    env = lambda at: {'GIT_AUTHOR_DATE': at, 'GIT_COMMITTER_DATE': at, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
                      'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t', 'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'}
    git = lambda at, *cmd: subprocess.run(['git', '-C', str(repo), *cmd], check=True, env=env(at), capture_output=True)
    (repo / 'a.py').write_text('a = 1\n')
    (repo / 'u.py').write_text('u = 1\n')
    git(ts(0), 'init', '-q')
    git(ts(0), 'add', '.')
    git(ts(0), 'commit', '-qm', 'base')
    root = str(repo)
    src = tmp_path / 'src'
    write_lines(src / f'{SID}.jsonl', [
        msg('user', 'go', ts=ts(1)),
        msg('assistant', [tool('e1', 'Edit', file_path=f'{root}/a.py', old_string='a = 1', new_string='a = 2')], ts=ts(2), cwd=root),
        dict(result('e1'), timestamp=ts(2, 1)),
        msg('assistant', [tool('w1', 'Write', file_path=f'{root}/b.py', content='b = 1\n')], ts=ts(4), cwd=root),
        dict(result('w1'), timestamp=ts(4, 1)),
        msg('assistant', [tool('e2', 'Edit', file_path=f'{root}/b.py', old_string='b = 1', new_string='b = 3')], ts=ts(9), cwd=root),
        dict(result('e2'), timestamp=ts(9, 1))])

    def commit_meanwhile(k, now):
        if now >= sec(ts(5)) and not (repo / 'b.py').exists():
            (repo / 'a.py').write_text('a = 2\n')
            (repo / 'b.py').write_text('b = 1\n')
            git(ts(5), 'add', '.')
            git(ts(5), 'commit', '-qm', 'made while live')
    w, store, proj = follow(tmp_path, str(src), root, seed_git=True, git_reconcile=True, restarts=1,
                            between=commit_meanwhile)
    st = [json.loads(l) for l in open(os.path.join(store, 'steps.ndjson'))]
    # untouched files frozen at the cold build (the first record: no step yet, so a.py too)
    assert [s['tool'] for s in st] == ['Edit', 'Write', 'git', 'Edit'] and _rest(store) == {'a.py': 'a = 1\n', 'u.py': 'u = 1\n'}
    exported_equals_batch(store, proj, root, seed_git=True, git_reconcile=True, rest=_rest(store))


def test_a_subagent_whose_meta_comes_late_gets_its_tags(tmp_path):
    src, sub = _mixed(tmp_path / 'src')
    rel = os.path.relpath(sub, src)
    w, store, proj = follow(tmp_path, src, ROOT, late_meta=(rel,))
    assert any(json.loads(l).get('agent') == 'executor · b' for l in open(os.path.join(store, 'steps.ndjson')))
    exported_equals_batch(store, proj, ROOT)


def test_a_single_transcript_that_gains_a_subagent_is_rebuilt_with_tags(tmp_path):
    proj, store = tmp_path / 'proj', str(tmp_path / 'store')
    main = write_lines(proj / f'{SID}.jsonl', [
        msg('user', 'go', ts=ts(0)),
        msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/a.py', content='a\n')], ts=ts(1)),
        dict(result('w'), timestamp=ts(1, 1))])
    w = live.Writer(store, [str(proj)], ROOT, lag=5, scan=0, alive=lambda: set(), prov_every=0)
    w.start(sec(ts(3)))
    assert not w.many and 'session' not in json.loads(open(os.path.join(store, 'steps.ndjson')).readline())
    write_lines(proj / SID / 'subagents' / 'agent-s.jsonl', [
        msg('user', 'sub', ts=ts(4), isSidechain=True),
        msg('assistant', [tool('s', 'Write', file_path=f'{ROOT}/s.py', content='s\n')], ts=ts(5), isSidechain=True),
        dict(result('s'), timestamp=ts(5, 1))])
    w.tick(sec(ts(9)))
    assert w.rebuilds == 1 and w.many
    exported_equals_batch(store, str(proj), ROOT)
    assert main


def test_a_subagent_that_sorts_before_a_later_session_keeps_batch_order(tmp_path):
    """Fixture D of the review: session 2 starts before session 1's subagent is written; on a tie the subagent's
    transcript number (session 1's place) must still decide, as in transcripts() order."""
    p = tmp_path / 'src'
    s1, s2 = '11111111-0000-0000-0000-000000000000', '22222222-0000-0000-0000-000000000000'
    write_lines(p / f'{s1}.jsonl', [msg('user', 'one', ts=ts(0))])
    write_lines(p / f'{s2}.jsonl', [msg('user', 'two', ts=ts(5)),
                                    msg('assistant', [tool('w2', 'Write', file_path=f'{ROOT}/f.py', content='S2\n')], ts=ts(20)),
                                    dict(result('w2'), timestamp=ts(20, 1))])
    write_lines(p / s1 / 'subagents' / 'agent-1.jsonl', [
        msg('user', 'sub', ts=ts(10), isSidechain=True),
        msg('assistant', [tool('ws', 'Write', file_path=f'{ROOT}/f.py', content='S1 sub\n')], ts=ts(20), isSidechain=True),
        dict(result('ws'), timestamp=ts(20, 1))])
    w, store, proj = follow(tmp_path, str(p), ROOT)
    exported_equals_batch(store, proj, ROOT)


def test_a_dead_sessions_call_without_result_closes_after_idle(tmp_path):
    """Fixture A of the review: A's Bash call never returns (session killed). It holds B's later report until A
    has been idle `idle` seconds and A's session is not running; then the steps equal a batch build that closes A
    at its end."""
    proj, store = tmp_path / 'proj', str(tmp_path / 'store')
    write_lines(proj / 'aaaaaaaa-0000.jsonl', [msg('user', 'go', ts=ts(0)),
                                               msg('assistant', [tool('orphan', 'Bash', command='make all a.py')], ts=ts(1))])
    write_lines(proj / 'bbbbbbbb-0000.jsonl', [
        msg('user', 'go', ts=ts(0, 30)),
        msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/a.py', content='a = 1\n')], ts=ts(0, 40)),
        dict(result('w'), timestamp=ts(0, 41)),
        msg('assistant', [tool('y', 'Bash', command='python3 x.py')], ts=ts(2)),
        bash_result('y', ts(3), {'filePath': f'{ROOT}/a.py', 'hunks': [{'oldStart': 1, 'lines': [' a = 1', '+b = 2']}]})])
    running = {'aaaaaaaa-0000'}
    w = live.Writer(store, [str(proj)], ROOT, lag=5, idle=600, alive=lambda: set(running), scan=0, prov_every=0)
    w.start(sec(ts(4)))
    assert w.n == 1                                       # the Write; the report waits for A's call
    w.tick(sec(ts(30)))
    assert w.n == 1                                       # idle, but A's session runs
    running.clear()
    w.tick(sec(ts(31)))
    assert w.n == 2 and w.engine.late == 0
    exported_equals_batch(store, str(proj), ROOT)


def test_drain_takes_a_watermark_that_is_not_iso(tmp_path):
    proj = tmp_path / 'proj'
    a = write_lines(proj / 'aaaaaaaa-0000.jsonl', [msg('user', 'go', ts=ts(0)),
                                                   msg('assistant', [tool('o', 'Bash', command='make all')], ts=ts(1))])
    b = write_lines(proj / 'bbbbbbbb-0000.jsonl', [
        msg('user', 'go', ts=ts(0, 30)),
        msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/a.py', content='a = 1\n')], ts=ts(0, 40)),
        dict(result('w'), timestamp=ts(0, 41)),
        msg('assistant', [tool('y', 'Bash', command='python3 x.py a.py')], ts=ts(2)),
        bash_result('y', ts(3), {'filePath': f'{ROOT}/a.py', 'hunks': [{'oldStart': 1, 'lines': [' a = 1', '+b = 2']}]})])
    e = replay.Engine(ROOT, many=True)
    for n, f in enumerate((a, b)):
        e.add(n, f)
        for o in replay.load_lines(f):
            e.feed(n, o)
    assert [s.file for s in e.drain('~')] == ['a.py']    # the report waits for A's call, which may still name a.py


def test_memory_stays_flat_over_2000_steps(tmp_path):
    """2,000 writes of a small app (3 KB each), fed live with evictions: what the writer holds grows only by the
    per-call ids it must keep (tool_use ids for dedup of resumed copies, result flags, op counts: ~0.6 KB a call),
    never by texts, ops or snapshots (that would be 3 KB a step and more)."""
    proj, store = tmp_path / 'proj', str(tmp_path / 'store')
    body = lambda k: ''.join(f'def f{i}(x):\n    return x + {i * k % 7}\n\n' for i in range(12)) + f'DATA = {"x" * 2600 + str(k)!r}\n'
    path = proj / f'{SID}.jsonl'
    path.parent.mkdir(parents=True)
    w = live.Writer(store, [str(proj)], ROOT, lag=5, scan=0, alive=lambda: set(), checkpoint_every=1e9,
                    evict_every=0, prov_every=0, gc_every=300)
    t0 = sec(ts(0))
    with open(path, 'w') as fh:
        fh.write(json.dumps(msg('user', 'go', ts=ts(0))) + '\n')
    w.start(t0 + 1)
    gc.collect()
    tracemalloc.start()
    sizes = {}
    for k in range(1, 2001):
        at = t0 + 10 * k
        stamp = live.iso(at)
        with open(path, 'a') as fh:
            fh.write(json.dumps(msg('assistant', [tool(f'w{k}', 'Write', file_path=f'{ROOT}/m{k % 3}.py', content=body(k))],
                                    ts=stamp)) + '\n')
            fh.write(json.dumps(dict(result(f'w{k}'), timestamp=live.iso(at + 1))) + '\n')
        w.tick(at + 2)
        if k in (500, 2000):
            gc.collect()
            sizes[k] = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    assert w.n >= 1990
    grew = sizes[2000] - sizes[500]
    print(f'tracemalloc: {sizes[500] / 1e6:.1f} MB at step 500, {sizes[2000] / 1e6:.1f} MB at 2000')
    assert grew < 1500 * 1000, f'{grew / 1e6:.2f} MB over 1,500 steps'


def test_timeline_py_checkpoint_writes_the_same_map_and_a_store_live_continues(tmp_path):
    """(A last line still without its newline is held by live, so this holds for transcripts that end with one,
    as they do except in the middle of a write.)"""
    src, _ = _mixed(tmp_path / 'src')
    copy = os.path.join(src, 'bbbbbbbb-copy.jsonl')
    with open(copy, 'a') as fh:
        fh.write('\n')
    tl = lambda *extra: subprocess.run([os.sys.executable, os.path.join(os.path.dirname(HERE), 'timeline.py'), src,
                                        '--root', ROOT, '--no-timings', *extra], check=True, capture_output=True)
    tl('-o', str(tmp_path / 'plain.json'))
    store = str(tmp_path / 'store')
    tl('-o', str(tmp_path / 'ckpt.json'), '--checkpoint', store)
    assert (tmp_path / 'ckpt.json').read_bytes() == (tmp_path / 'plain.json').read_bytes()
    w = live.Writer(store, [src], ROOT, alive=lambda: set())
    assert w.start(time.time()) == 'warm', w.cold_reason
    assert live.export_text(store) == (tmp_path / 'plain.json').read_text()



def test_a_resumed_copy_of_a_report_moved_by_credit_is_just_a_copy(tmp_path):
    """A's report is credited to B and moved after B's (racing fixture); A is then resumed: its copy of that report
    comes much later and is a copy of the kept one by the key both had as read, not a late record."""
    src = _racing(tmp_path / 'src')
    a = os.path.join(src, 'aaaaaaaa-a.jsonl')
    with open(a) as fh:
        recs = [json.loads(l) for l in fh]
    write_lines(pathlib.Path(src) / 'ffffffff-resumed.jsonl', recs + [msg('user', 'resumed', ts=ts(30))])
    w, store, proj = follow(tmp_path, src, ROOT, restarts=1)
    exported_equals_batch(store, proj, ROOT)


def test_a_write_that_fails_after_the_tail_showed_it_is_redone(tmp_path):
    """While its result is out, the tail shows a Write as if the session ended there (applied); the error that then
    comes makes it a step that changed nothing. The rerun of the tail must not reuse the earlier apply."""
    src = tmp_path / 'src'
    write_lines(src / f'{SID}.jsonl', [
        msg('user', 'go', ts=ts(0)),
        msg('assistant', [tool('w1', 'Write', file_path=f'{ROOT}/a.py', content='a = 1\n')], ts=ts(1)),
        dict(result('w1'), timestamp=ts(1, 1)),
        msg('assistant', [tool('w2', 'Write', file_path=f'{ROOT}/a.py', content='a = 2\n')], ts=ts(2)),
        dict(result('w2', error=True), timestamp=ts(2, 1)),
        msg('assistant', [tool('w3', 'Write', file_path=f'{ROOT}/b.py', content='b = 1\n')], ts=ts(3)),
        dict(result('w3'), timestamp=ts(3, 1))])
    w, store, proj = follow(tmp_path, str(src), ROOT, lag=600)
    st = [json.loads(l) for l in open(os.path.join(store, 'steps.ndjson'))]
    assert [s.get('applied', True) for s in st] == [True, False, True]
    exported_equals_batch(store, proj, ROOT)


# -- the phase-2 review: the committed map must never part from batch -------------------------------------------------
def _six_writes(proj):
    recs = [msg('user', 'go', ts=ts(0))]
    for k in range(1, 7):
        recs += [msg('assistant', [tool(f'w{k}', 'Write', file_path=f'{ROOT}/m{k}.py', content=f'x = {k}\n')], ts=ts(k)),
                 dict(result(f'w{k}'), timestamp=ts(k, 1))]
    return write_lines(proj / f'{SID}.jsonl', recs)


def _writer(store, proj, **kw):
    return live.Writer(store, [str(proj)], kw.pop('root', ROOT), lag=kw.pop('lag', 5), scan=0, alive=kw.pop('alive', lambda: set()),
                       prov_every=kw.pop('prov_every', 0), **kw)


def test_an_interrupted_tick_leaves_no_checkpoint_past_its_unwritten_steps(tmp_path):
    proj, store = tmp_path / 'proj', str(tmp_path / 'store')
    _six_writes(proj)
    w = _writer(store, proj)
    w.start(sec(ts(2, 30)))
    assert w.n == 2
    orig, calls = w._commit, [0]

    def boom(st):                       # ^C after one more step was appended, before the next
        calls[0] += 1
        if calls[0] == 2:
            raise KeyboardInterrupt
        return orig(st)
    w._commit = boom
    with pytest.raises(KeyboardInterrupt):
        w.tick(sec(ts(9)))
    w.checkpoint(sec(ts(9)))            # what live.main() does in `finally`: nothing, the tick did not end
    w2 = _writer(store, proj)
    assert w2.start(sec(ts(9))) == 'warm' and w2.n == 6
    assert live.verify(store) == 0


@pytest.mark.parametrize('case', ['amend', 'reset'])
def test_a_commit_that_leaves_history_rebuilds(tmp_path, case):
    repo = tmp_path / 'repo'
    repo.mkdir()
    env = lambda at: {'GIT_AUTHOR_DATE': at, 'GIT_COMMITTER_DATE': at, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
                      'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t', 'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'}
    git = lambda at, *c: subprocess.run(['git', '-C', str(repo), *c], check=True, env=env(at), capture_output=True)
    (repo / 'a.py').write_text('a = 1\n')
    git(ts(0), 'init', '-q')
    git(ts(0), 'add', '.')
    git(ts(0), 'commit', '-qm', 'base')
    root, proj, store = str(repo), tmp_path / 'proj', str(tmp_path / 'store')
    write_lines(proj / f'{SID}.jsonl', [msg('user', 'go', ts=ts(1)),
        msg('assistant', [tool('e1', 'Edit', file_path=f'{root}/a.py', old_string='a = 1', new_string='a = 2')], ts=ts(2), cwd=root),
        dict(result('e1'), timestamp=ts(2, 1))])
    w = _writer(store, proj, root=root, git_reconcile=True, lag=60, git_poll=0)
    w.start(sec(ts(3)))
    (repo / 'a.py').write_text('a = 2\n')
    git(ts(4), 'commit', '-qam', 'first msg')
    w.tick(sec(ts(4, 10)))
    if case == 'amend':                 # within the lag, same author date: not late, but the first commit is gone
        git(ts(4, 30), 'commit', '-q', '--amend', '-m', 'fixed msg', '--date', ts(4))
    else:
        git(ts(4, 30), 'reset', '-q', '--hard', 'HEAD~1')
    w.tick(sec(ts(4, 40)))
    w.tick(sec(ts(30)))
    assert w.reasons == ['a commit taken left the history (amend, reset or rebase)']
    assert live.verify(store) == 0


def test_a_result_after_an_idle_close_rebuilds(tmp_path):
    """A's Bash call was taken as over (A idle, its session not running), B's report committed meanwhile; then A's
    result comes after all: the committed map no longer matches batch's, so the store is rebuilt."""
    proj, store = tmp_path / 'proj', str(tmp_path / 'store')
    pa = write_lines(proj / 'aaaaaaaa-a.jsonl', [msg('user', 'go', ts=ts(0)),
                                                 msg('assistant', [tool('xa', 'Bash', command='make all a.py')], ts=ts(1))])
    hunk = {'oldStart': 1, 'lines': [' a = 1', '+b = 2']}
    write_lines(proj / 'bbbbbbbb-b.jsonl', [msg('user', 'go', ts=ts(0, 10)),
        msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/a.py', content='a = 1\n')], ts=ts(0, 30)),
        dict(result('w'), timestamp=ts(0, 31)),
        msg('assistant', [tool('y', 'Bash', command='python3 x.py')], ts=ts(2)),
        bash_result('y', ts(3), {'filePath': f'{ROOT}/a.py', 'hunks': [hunk]})])
    w = _writer(store, proj, idle=600, evict_every=1e9)
    w.start(sec(ts(4)))
    w.tick(sec(ts(20)))
    assert w.n == 2
    with open(pa, 'a') as fh:
        fh.write(json.dumps(bash_result('xa', ts(40), {'filePath': f'{ROOT}/a.py', 'hunks': [hunk]})) + '\n')
    w.tick(sec(ts(41)))
    w.tick(sec(ts(59)))
    assert w.reasons == ['late: 1 records broke the watermark'] and live.verify(store) == 0


@pytest.mark.parametrize('running', [False, True])
def test_eviction_keeps_what_a_running_call_can_still_see(tmp_path, running):
    """A's Bash call runs from 1 to 40; B's Edit at 5 lies in its window, so A's report goes to B (no step). With
    eviction at every tick, the Edit must stay while A's call runs (running: A's session is alive, nothing closes).
    When A's session is not running, A is closed after the idle time and the Edit may go: A's report then comes after
    the close, which rebuilds the store instead of crediting it without the Edit."""
    src = tmp_path / 'src'
    write_lines(src / 'aaaaaaaa-a.jsonl', [msg('user', 'go', ts=ts(0)),
        msg('assistant', [tool('xa', 'Bash', command='python3 fix.py')], ts=ts(1)),
        bash_result('xa', ts(40), {'filePath': f'{ROOT}/a.py', 'hunks': [{'oldStart': 1, 'lines': [' q = 0', '+b = 3']}]})])
    write_lines(src / 'bbbbbbbb-b.jsonl', [msg('user', 'go', ts=ts(0, 10)),
        msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/a.py', content='a = 1\n')], ts=ts(0, 30)),
        dict(result('w'), timestamp=ts(0, 31)),
        msg('assistant', [tool('e', 'Edit', file_path=f'{ROOT}/a.py', old_string='a = 1', new_string='a = 1\nb = 2')], ts=ts(5)),
        dict(result('e'), timestamp=ts(5, 1)), msg('user', 'more', ts=ts(30))])
    w, store, proj = follow(tmp_path, str(src), ROOT, rebuild_ok=('a second transcript',) + (() if running else ('late',)),
                            alive=lambda: {'aaaaaaaa-a'} if running else set())
    assert any(r.startswith('late') for r in w.reasons) != running
    exported_equals_batch(store, proj, ROOT)


@pytest.mark.parametrize('kind', ['summary', 'empty'])
def test_a_transcript_without_timestamps_counts_as_batch_counts_it(tmp_path, kind):
    proj, store = tmp_path / 'proj', str(tmp_path / 'store')
    write_lines(proj / f'{SID}.jsonl', [msg('user', 'go', ts=ts(0)),
        msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/a.py', content='a = 1\n')], ts=ts(1)),
        dict(result('w'), timestamp=ts(1, 1))])
    (proj / '00000000-sum.jsonl').write_text(json.dumps({'type': 'summary', 'summary': 'x', 'leafUuid': 'u'}) + '\n'
                                             if kind == 'summary' else '')
    w = _writer(store, proj)
    w.start(sec(ts(40)))
    assert w.many and len(replay.transcripts(str(proj))) == 2
    exported_equals_batch(store, str(proj), ROOT)


def test_a_transcript_without_timestamps_that_appears_later_turns_many_on(tmp_path):
    proj, store = tmp_path / 'proj', str(tmp_path / 'store')
    _six_writes(proj)
    w = _writer(store, proj)
    w.start(sec(ts(30)))
    assert not w.many
    (proj / '00000000-sum.jsonl').write_text(json.dumps({'type': 'summary', 'summary': 'x', 'leafUuid': 'u'}) + '\n')
    w.tick(sec(ts(31)))
    assert w.many and w.rebuilds == 1
    exported_equals_batch(store, str(proj), ROOT)


@pytest.mark.parametrize('case', ['appended', 'half-line'])
def test_a_crash_between_appends_and_the_checkpoint_restarts_warm(tmp_path, case):
    """Steps appended after the last checkpoint (and maybe a line cut short) are cut back to it on restart."""
    proj, store = tmp_path / 'proj', str(tmp_path / 'store')
    _six_writes(proj)
    w = _writer(store, proj, checkpoint_every=1e9)
    w.start(sec(ts(2, 30)))
    w.tick(sec(ts(9)))                  # more steps, no checkpoint: the process dies here
    p = os.path.join(store, 'steps.ndjson')
    if case == 'half-line':
        data = open(p, 'rb').read()
        open(p, 'wb').write(data[:-20])
    w2 = _writer(store, proj, checkpoint_every=1e9)
    assert w2.start(sec(ts(9))) == 'warm', w2.cold_reason
    assert live.verify(store) == 0


def test_an_idle_closed_transcript_that_comes_back_can_close_again(tmp_path):
    proj, store = tmp_path / 'proj', str(tmp_path / 'store')
    pa = write_lines(proj / 'aaaaaaaa-0000.jsonl', [msg('user', 'go', ts=ts(0)),
                                                     msg('assistant', [tool('o1', 'Bash', command='make all a.py')], ts=ts(1))])
    pb = write_lines(proj / 'bbbbbbbb-0000.jsonl', [msg('user', 'go', ts=ts(0, 30)),
        msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/a.py', content='a = 1\n')], ts=ts(0, 40)),
        dict(result('w'), timestamp=ts(0, 41))])
    w = _writer(store, proj, idle=600)
    w.start(sec(ts(4)))
    w.tick(sec(ts(15)))
    with open(pa, 'a') as fh:           # A comes back in the same file, starts another call that never returns
        fh.write(json.dumps(msg('user', 'again', ts=ts(16))) + '\n')
        fh.write(json.dumps(msg('assistant', [tool('o2', 'Bash', command='make all a.py')], ts=ts(16, 10))) + '\n')
    with open(pb, 'a') as fh:
        fh.write(json.dumps(msg('assistant', [tool('y', 'Bash', command='python3 x.py')], ts=ts(17))) + '\n')
        fh.write(json.dumps(bash_result('y', ts(18), {'filePath': f'{ROOT}/a.py',
                                                         'hunks': [{'oldStart': 1, 'lines': [' a = 1', '+b = 2']}]})) + '\n')
    for m in (18.5, 30, 45):
        w.tick(sec(ts(int(m), int(m % 1 * 60))))
    assert w.n == 2 and live.verify(store) == 0


def test_the_pid_file_names_a_writer_only_with_its_start_time(tmp_path):
    store = str(tmp_path / 'store')
    os.makedirs(store)
    pid = os.getpid()
    live.write_atomic(os.path.join(store, 'live.pid'), f'{pid} {live.proc_start(pid)}')
    assert live.writer_pid(store) == pid
    live.write_atomic(os.path.join(store, 'live.pid'), f'{pid} 1')        # a reused pid
    assert live.writer_pid(store) is None
    assert live.main(['stop', 'x', '--store', store]) == 1                # nothing is signalled


def test_timeline_py_checkpoint_refuses_while_a_writer_runs(tmp_path):
    src, _ = _mixed(tmp_path / 'src')
    store = str(tmp_path / 'store')
    os.makedirs(store)
    live.write_atomic(os.path.join(store, 'live.pid'), f'{os.getpid()} {live.proc_start(os.getpid())}')
    r = subprocess.run([os.sys.executable, os.path.join(os.path.dirname(HERE), 'timeline.py'), src, '--root', ROOT,
                        '--no-timings', '-o', str(tmp_path / 't.json'), '--checkpoint', store], capture_output=True, text=True)
    assert r.returncode != 0 and 'stop it first' in r.stderr and not os.path.exists(os.path.join(store, 'steps.ndjson'))


def _stopped_store(tmp_path):
    proj, store = tmp_path / 'proj', str(tmp_path / 'store')
    _six_writes(proj)
    w = _writer(store, proj)
    w.start(sec(ts(9)))
    w.checkpoint(sec(ts(9)))
    return proj, store


def test_a_checkpoint_from_other_code_is_not_unpickled(tmp_path, monkeypatch):
    proj, store = _stopped_store(tmp_path)
    loads = []
    monkeypatch.setattr(live.pickle, 'load', lambda *a, **k: loads.append(1))
    monkeypatch.setattr(live, 'code_hash', lambda: 'other')
    w = _writer(store, proj)
    assert w.start(sec(ts(9))) == 'cold' and w.cold_reason == 'the code changed' and not loads


def test_a_checkpoint_that_does_not_load_means_a_cold_build(tmp_path):
    proj, store = _stopped_store(tmp_path)
    p = os.path.join(store, 'checkpoint.pkl')
    data = open(p, 'rb').read()
    head = data.index(b'\n') + 1
    open(p, 'wb').write(data[:head] + b'\x80\x05\x95garbage')
    w = _writer(store, proj)
    assert w.start(sec(ts(9))) == 'cold' and w.cold_reason.startswith('the checkpoint does not load')
    assert live.verify(store) == 0


def test_a_transcript_changed_before_its_offset_means_a_cold_build(tmp_path):
    proj, store = _stopped_store(tmp_path)
    p = proj / f'{SID}.jsonl'
    data = p.read_bytes()
    with open(p, 'r+b') as fh:          # same inode, same size, an earlier byte changed
        fh.write(data[:len(data) // 2].replace(b'x = 1', b'x = 9'))
    w = _writer(store, proj)
    assert w.start(sec(ts(9))) == 'cold' and 'changed before its offset' in w.cold_reason
    assert live.verify(store) == 0


def test_a_steps_file_that_does_not_match_the_checkpoint_means_a_cold_build(tmp_path):
    proj, store = _stopped_store(tmp_path)
    p = os.path.join(store, 'steps.ndjson')
    lines = open(p).read().split('\n')
    lines[-2] = lines[-2].replace('"m6.py"', '"m7.py"')
    open(p, 'w').write('\n'.join(lines))
    w = _writer(store, proj)
    assert w.start(sec(ts(9))) == 'cold' and w.cold_reason == 'steps.ndjson does not match'
    assert live.verify(store) == 0


def test_a_steps_line_being_written_is_not_read(tmp_path):
    proj, store = _stopped_store(tmp_path)
    before = live.read_store(store)[1]
    with open(os.path.join(store, 'steps.ndjson'), 'a') as fh:
        fh.write('{"idx":6,"ts":"2026')
    assert live.read_store(store)[1] == before


def test_a_transcript_cut_or_replaced_while_followed_rebuilds(tmp_path):
    proj, store = tmp_path / 'proj', str(tmp_path / 'store')
    p = _six_writes(proj)
    w = _writer(store, proj)
    w.start(sec(ts(9)))
    lines = open(p).read().split('\n')
    open(p, 'r+').truncate(len('\n'.join(lines[:7])) + 1)        # cut back to the first three writes (same inode)
    w.tick(sec(ts(10)))
    assert w.reasons and 'replaced or cut' in w.reasons[0]
    exported_equals_batch(store, str(proj), ROOT)


def test_a_new_transcript_that_sorts_before_a_followed_one_rebuilds(tmp_path):
    """B starts with a copy of A's first record (same first time) but has a record of its own before A's next one:
    transcripts() puts B first, ahead of A, which live already numbered."""
    proj, store = tmp_path / 'proj', str(tmp_path / 'store')
    first = msg('user', 'go', ts=ts(0))
    write_lines(proj / 'bbbbbbbb-a.jsonl', [first,
        msg('assistant', [tool('wa', 'Write', file_path=f'{ROOT}/a.py', content='a\n')], ts=ts(5)),
        dict(result('wa'), timestamp=ts(5, 1))])
    w = _writer(store, proj)
    w.start(sec(ts(6)))
    write_lines(proj / 'cccccccc-b.jsonl', [first,
        msg('assistant', [tool('wb', 'Write', file_path=f'{ROOT}/a.py', content='b\n')], ts=ts(3)),
        dict(result('wb'), timestamp=ts(3, 1))])
    assert [os.path.basename(f)[:8] for f in replay.transcripts(str(proj))] == ['cccccccc', 'bbbbbbbb']
    w.tick(sec(ts(7)))
    assert any('sorts before' in r for r in w.reasons)
    exported_equals_batch(store, str(proj), ROOT)


@pytest.mark.parametrize('seed', [0, 26])
def test_random_sessions_live_equal_batch(tmp_path, seed):
    """Two of the review's random sessions (seed 0 and 26 catch a fork sharing pending ops, history or Reads with
    the committed engine, and eviction of applied ops a report can still see)."""
    import randsession
    proj, root = randsession.random_project(random.Random(seed), str(tmp_path / 'g'))
    w, store, copy = follow(tmp_path / 'f', proj, root or ROOT, restarts=2, seed=seed)
    exported_equals_batch(store, copy, root or ROOT)


def _late_error(tmp_path):
    """An Edit whose error result is written 10 s late (after a record with a later time, as Claude Code's message
    records sometimes are): the tail showed it applied; it commits in the tick its result comes in."""
    src = tmp_path / 'src'
    write_lines(src / f'{SID}.jsonl', [
        msg('user', 'go', ts=ts(0)),
        msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/a.py', content='a = 1\n')], ts=ts(1)),
        dict(result('w'), timestamp=ts(1, 1)),
        msg('assistant', [tool('e', 'Edit', file_path=f'{ROOT}/a.py', old_string='a = 1', new_string='a = 2')], ts=ts(2)),
        {'type': 'attachment', 'timestamp': ts(2, 10), 'attachment': {'type': 'hook_success'}},
        dict(result('e', error=True), timestamp=ts(2, 1)),
        msg('assistant', [tool('w2', 'Write', file_path=f'{ROOT}/b.py', content='b = 1\n')], ts=ts(3)),
        dict(result('w2'), timestamp=ts(3, 1))])
    return str(src)


def test_a_commit_the_tail_did_not_foresee_is_analysed_not_adopted(tmp_path):
    w, store, proj = follow(tmp_path, _late_error(tmp_path), ROOT)
    assert [json.loads(l).get('applied', True) for l in open(os.path.join(store, 'steps.ndjson'))] == [True, False, True]
    exported_equals_batch(store, proj, ROOT)


def test_a_commit_that_adopts_the_tail_leaves_the_rest_of_it_its_own_state(tmp_path):
    """A Write and an Edit in one message: one run of the tail analyses both. The Write commits (adopted with the
    analysis made for it); then the Edit's error result comes, so the Edit is analysed again: from the state right
    after the Write, not from the one after the Edit as the tail had it."""
    src = tmp_path / 'src'
    write_lines(src / f'{SID}.jsonl', [
        msg('user', 'go', ts=ts(0)),
        msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/a.py', content=_app('one')),
                          tool('e', 'Edit', file_path=f'{ROOT}/a.py', old_string='return 1',
                               new_string='return 2\n\n@app.route("/two")\ndef two():\n    return 2')], ts=ts(1)),
        dict(result('w'), timestamp=ts(1, 1)),
        {'type': 'attachment', 'timestamp': ts(2, 40), 'attachment': {'type': 'hook_success'}},
        dict(result('e', error=True), timestamp=ts(1, 2)),
        msg('assistant', [tool('w2', 'Write', file_path=f'{ROOT}/b.py', content=_app('three'))], ts=ts(4)),
        dict(result('w2'), timestamp=ts(4, 1))])
    w, store, proj = follow(tmp_path, str(src), ROOT, lag=60)
    exported_equals_batch(store, proj, ROOT)


def test_a_tail_redone_at_most_every_few_seconds(tmp_path):
    """prov_every > 0: the tail may be stale when steps commit; committed steps still equal batch's."""
    src, _ = _mixed(tmp_path / 'src')
    w, store, proj = follow(tmp_path, src, ROOT, prov_every=90, restarts=1)
    exported_equals_batch(store, proj, ROOT)
    w, store, proj = follow(tmp_path / 'e', _late_error(tmp_path / 'e'), ROOT, prov_every=90)
    exported_equals_batch(store, proj, ROOT)


def _app(*routes, k=1):
    """A Flask module with these routes (each returns k): blobs whose size shows what an edit kept."""
    return 'from flask import Flask\napp = Flask(__name__)\n' + ''.join(
        f'\n@app.route("/{r}")\ndef {r}():\n    return {k}\n' for r in routes)


def test_an_op_read_late_that_sorts_before_the_tail_reruns_what_follows(tmp_path):
    """The tail ran [A's Write, A's Edit]; then B's Write, timed between them but written later, comes in: the rerun
    must not take the Edit's recorded result (made without B's Write) once the order has changed."""
    src = tmp_path / 'src'
    write_lines(src / 'aaaaaaaa-a.jsonl', [
        msg('user', 'go', ts=ts(0)),
        msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/a.py', content=_app('one'))], ts=ts(1)),
        dict(result('w'), timestamp=ts(1, 1)),
        msg('assistant', [tool('e', 'Edit', file_path=f'{ROOT}/a.py', old_string='return 1', new_string='return 2',
                               replace_all=True)], ts=ts(10)),
        dict(result('e'), timestamp=ts(10, 1))])
    write_lines(src / 'bbbbbbbb-b.jsonl', [
        msg('user', 'go', ts=ts(0, 30)),
        {'type': 'attachment', 'timestamp': ts(12), 'attachment': {'type': 'hook_success'}},
        msg('assistant', [tool('z', 'Write', file_path=f'{ROOT}/a.py', content=_app('one', 'two', 'three'))], ts=ts(9)),
        dict(result('z'), timestamp=ts(9, 1))])
    w, store, proj = follow(tmp_path, str(src), ROOT, lag=600)
    last = timeline.expand(json.loads(live.export_text(store)))[-1]
    assert [s.get('session') for s in map(json.loads, open(os.path.join(store, 'steps.ndjson')))] == ['aaaaaaaa', 'bbbbbbbb', 'aaaaaaaa']
    exported_equals_batch(store, proj, ROOT)
    assert last['file'] == 'a.py'


def _regrouping_session(tmp_path, n=12, hold=45):
    """Flat Flask modules m0..m11 with 3 routes each, written one by one; calls first pair them (0,1) (2,3) ...,
    then heavier calls pair them (1,2) (3,4) ...; then edits that change no call, so the new grouping holds for
    areas.HOLD steps and is adopted (a regroup at step 43 of 68): the Zoner's state (assignments, the fresh grouping
    and when it came out, step count) matters to every later step."""
    calls = {i: {} for i in range(n)}

    def text(i, k=0):
        lines = ['from flask import Flask', 'app = Flask(__name__)'] + [f'from m{j} import h{j}' for j in sorted(calls[i])]
        for r in range(3):
            lines += [f'@app.route("/m{i}/{r}")', f'def r{i}_{r}(x={k}):']
            lines += [f'    x = h{j}(x)' for j, c in sorted(calls[i].items()) for _ in range(c)]
            lines += ['    return x', '']
        return '\n'.join(lines + [f'def h{i}(x):', f'    return x + {i}', '']) + '\n'
    writes = [(i, text(i)) for i in range(n)]
    for i in range(0, n - 1, 2):
        calls[i][i + 1] = 1
        writes.append((i, text(i)))
    for i in range(1, n - 1, 2):
        calls[i][i + 1] = 3
        writes.append((i, text(i)))
    writes += [(k % n, text(k % n, k + 1)) for k in range(hold)]
    recs = [msg('user', 'build it', ts=ts(0))]
    for k, (i, body) in enumerate(writes):
        at = sec(ts(0)) + 50 * (k + 1)
        recs += [msg('assistant', [tool(f'w{k}', 'Write', file_path=f'{ROOT}/m{i}.py', content=body)], ts=live.iso(at)),
                 dict(result(f'w{k}'), timestamp=live.iso(at + 1))]
    write_lines(tmp_path / 'src' / f'{SID}.jsonl', recs)
    return str(tmp_path / 'src')


def test_areas_that_regroup_while_the_tail_runs_ahead(tmp_path):
    """68 function-rich steps 50 s apart, a 5-minute lag (tails of ~6 steps, redone at every record), a regroup half-way: the
    tail's analyzer forks must leave the committed analyzer's Zoner and memo as they were."""
    src = _regrouping_session(tmp_path)
    w, store, proj = follow(tmp_path, src, ROOT, lag=300, check_every=4, restarts=1)
    assert w.analyzer.zoner.regroups == 1
    exported_equals_batch(store, proj, ROOT)


def test_a_tail_redone_without_new_steps_analyses_nothing(tmp_path, monkeypatch):
    """A result that changes no step (a Read's) makes the engine rerun the tail; its steps are the same, so their
    analyses are reused."""
    src = tmp_path / 'src'
    read = dict(result('r'), timestamp=ts(3, 1))
    read['toolUseResult'] = {'type': 'text', 'file': {'filePath': f'{ROOT}/z.txt', 'content': 'z', 'startLine': 1,
                                                       'numLines': 1, 'totalLines': 1}}
    write_lines(src / f'{SID}.jsonl', [
        msg('user', 'go', ts=ts(0)),
        msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/a.py', content=_app('one'))], ts=ts(1)),
        dict(result('w'), timestamp=ts(1, 1)),
        msg('assistant', [tool('r', 'Read', file_path=f'{ROOT}/z.txt')], ts=ts(3)), read])
    proj, store = tmp_path / 'proj', str(tmp_path / 'store')
    feed = iter(Feed(str(src), str(proj)))
    for _ in range(3):
        now = next(feed)
    w = _writer(store, proj, lag=600)
    w.start(now)
    assert len(w.tail) == 1
    calls = []
    step = timeline.Analyzer.step
    monkeypatch.setattr(timeline.Analyzer, 'step', lambda self, st, snap: calls.append(st.idx) or step(self, st, snap))
    before, gen = w.engine.version, w.gen
    runs = []
    prov = w.provisional
    w.provisional = lambda now: runs.append(now) or prov(now)
    for now in feed:                    # the Read's call and its result (a whole-file Read: an op, no step)
        w.tick(now)
    assert w.engine.version != before and runs     # the tail was run again ...
    assert calls == [] and len(w.tail) == 1 and w.gen == gen   # ... and nothing analysed, nothing rewritten


def test_a_second_result_before_its_op_is_applied_is_no_late_record(tmp_path):
    """A call with two results (one in the aigource transcripts): while its op waits to be applied, the second one
    still sets it, as a batch read of the file does; eviction must not have dropped the op meanwhile."""
    src = tmp_path / 'src'
    write_lines(src / f'{SID}.jsonl', [
        msg('user', 'go', ts=ts(0)),
        msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/a.py', content=_app('one'))], ts=ts(1)),
        dict(result('w'), timestamp=ts(1, 1)),
        msg('assistant', [tool('e', 'Edit', file_path=f'{ROOT}/a.py', old_string='return 1', new_string='return 2')], ts=ts(2)),
        dict(result('e'), timestamp=ts(2, 1)),
        dict(result('e', error=True), timestamp=ts(2, 2)),
        msg('assistant', [tool('w2', 'Write', file_path=f'{ROOT}/b.py', content=_app('two'))], ts=ts(3)),
        dict(result('w2'), timestamp=ts(3, 1))])
    w, store, proj = follow(tmp_path, str(src), ROOT, lag=60)
    assert w.reasons == []
    exported_equals_batch(store, proj, ROOT)


def test_transcripts_started_together_are_reordered_when_a_copy_gets_its_own_record(tmp_path):
    """A cold build finds a resumed session's file mid-copy (all its records are A's) but older on disk than A, so
    it cannot tell it for the copy and numbers it first, as transcripts() does then. Once the copy has a record of its
    own, transcripts() puts A first: the order is checked again and the store rebuilt."""
    proj, store = tmp_path / 'proj', str(tmp_path / 'store')
    recs = [msg('user', 'go', ts=ts(0)),
            msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/a.py', content=_app('one'))], ts=ts(1)),
            dict(result('w'), timestamp=ts(1, 1)),
            msg('assistant', [tool('e', 'Edit', file_path=f'{ROOT}/a.py', old_string='return 1', new_string='return 2')], ts=ts(2)),
            dict(result('e'), timestamp=ts(2, 1))]
    a = write_lines(proj / 'aaaaaaaa-a.jsonl', recs)
    c = write_lines(proj / 'cccccccc-c.jsonl', recs[:3])
    os.utime(c, (1_000_000_000, 1_000_000_000))
    assert [os.path.basename(f)[:8] for f in replay.transcripts(str(proj))] == ['cccccccc', 'aaaaaaaa']
    w = _writer(store, proj)
    w.start(sec(ts(5)))
    with open(c, 'a') as fh:
        fh.write(json.dumps(msg('user', 'resumed', ts=ts(9))) + '\n')
    assert [os.path.basename(f)[:8] for f in replay.transcripts(str(proj))] == ['aaaaaaaa', 'cccccccc']
    w.tick(sec(ts(10)))
    assert any('no longer sorts first' in r for r in w.reasons) and a
    exported_equals_batch(store, str(proj), ROOT)


def test_a_cold_build_numbers_a_file_that_holds_only_copies_as_transcripts_does(tmp_path):
    """A cold build has seen nothing grow: a resume found mid-copy (all its records are A's) is numbered where
    transcripts() puts it, first, whatever the mtimes say; its own record then flips the order: one rebuild."""
    proj, store = tmp_path / 'proj', str(tmp_path / 'store')
    recs = [msg('user', 'go', ts=ts(0)),
            msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/a.py', content=_app('one'))], ts=ts(1)),
            dict(result('w'), timestamp=ts(1, 1))]
    a = write_lines(proj / 'aaaaaaaa-a.jsonl', recs + [msg('user', 'more', ts=ts(2))])
    os.utime(a, (1_000_000_000, 1_000_000_000))
    c = write_lines(proj / 'cccccccc-c.jsonl', recs)
    w = _writer(store, proj)
    w.start(sec(ts(5)))
    assert not w.tailer.waiting and w.tailer.files[str(c)]['rank'] < w.tailer.files[str(a)]['rank']
    with open(c, 'a') as fh:
        fh.write(json.dumps(msg('user', 'resumed', ts=ts(9))) + '\n')
    w.tick(sec(ts(10)))
    assert len(w.reasons) == 1 and w.reasons[0].startswith('order') and not w.tailer.waiting
    exported_equals_batch(store, str(proj), ROOT)


@pytest.mark.parametrize('where', ['commit', 'feed'])
def test_an_interrupted_cold_build_leaves_no_checkpoint(tmp_path, monkeypatch, where):
    """^C in the first (or --fresh) cold build, then live.main()'s `finally` checkpoint: nothing is saved, and the
    next start builds from scratch."""
    proj, store = tmp_path / 'proj', str(tmp_path / 'store')
    _six_writes(proj)
    w = _writer(store, proj)
    calls = [0]

    def boom(orig):
        def f(*a):
            calls[0] += 1
            if calls[0] == (3 if where == 'commit' else 6):
                raise KeyboardInterrupt
            return orig(*a)
        return f
    if where == 'commit':
        w._commit = boom(w._commit)
    else:
        monkeypatch.setattr(replay.Engine, 'feed', boom(replay.Engine.feed))
    with pytest.raises(KeyboardInterrupt):
        w.start(sec(ts(30)))
    w.checkpoint(sec(ts(30)))
    monkeypatch.undo()
    assert not os.path.exists(os.path.join(store, 'checkpoint.pkl'))
    w2 = _writer(store, proj)
    assert w2.start(sec(ts(31))) == 'cold' and w2.n == 6 and live.verify(store) == 0


def _resumes(scn):
    """Resumed sessions: copies of the resumed file's records, then their own (from the phase-2 review's sc_order)."""
    h = {'oldStart': 1, 'lines': [' a = 1', '+b1 = 2']}
    a = [msg('user', 'go', ts=ts(0)),
         msg('assistant', [tool('a1', 'Write', file_path=f'{ROOT}/a.py', content='a = 1\n')], ts=ts(1)),
         dict(result('a1'), timestamp=ts(1, 1)),
         msg('assistant', [tool('a2', 'Bash', command='python3 fix.py')], ts=ts(2)),
         bash_result('a2', ts(3), {'filePath': f'{ROOT}/a.py', 'hunks': [h]}),
         msg('user', 'bye', ts=ts(4))]

    def own(tag, t0):
        return [msg('user', f'resume {tag}', ts=ts(t0)),
                msg('assistant', [tool(f'{tag}1', 'Edit', file_path=f'{ROOT}/a.py', old_string='a = 1', new_string=f'a = {tag}')],
                    ts=ts(t0 + 1)),
                dict(result(f'{tag}1'), timestamp=ts(t0 + 1, 1)),
                msg('assistant', [tool(f'{tag}2', 'Bash', command='python3 fix.py')], ts=ts(t0 + 2)),
                bash_result(f'{tag}2', ts(t0 + 4), {'filePath': f'{ROOT}/a.py',
                                                    'hunks': [{'oldStart': 1, 'lines': [f' a = {tag}', f'+c{tag} = 1']}]})]
    if scn == 'chain':                  # B resumes A, C resumes B
        return {'00000000-orig': a, 'bbbbbbbb-r1': a + own('B', 10), '11111111-r2': a + own('B', 10) + own('C', 20)}
    return {'00000000-orig': a, 'bbbbbbbb-r1': a + own('B', 10), '11111111-r2': a + own('C', 20)}


@pytest.mark.parametrize('scn,mtimes', [('chain', 'history'), ('two-resumes', 'history'), ('chain', 7), ('two-resumes', 8)])
def test_resumed_sessions_from_a_cold_build_at_every_point(tmp_path, scn, mtimes):
    """A cold build started at each point of the write order of resumed sessions (a chain: C resumes B, which
    resumed A; or B and C both resuming A), then fed on, with mtimes as written or random: no file waits that is not
    a copy, the steps equal batch's at every tick and at the end, and nothing is rebuilt but for the second
    transcript and, when the cold build finds a resume mid-copy (which content alone cannot tell from its original),
    once more."""
    src = tmp_path / 'src'
    for name, recs in _resumes(scn).items():
        write_lines(src / f'{name}.jsonl', recs)
    n = len(Feed(str(src), str(tmp_path / 'probe')).events)
    for start in range(n):
        w, store, proj = follow(tmp_path / f'k{start}', str(src), ROOT, start_at=start, check_every=3,
                                mtimes=mtimes if mtimes == 'history' else random.Random(mtimes * 1000 + start))
        exported_equals_batch(store, proj, ROOT)
        assert len([r for r in w.reasons if not r.startswith('a second')]) <= 1, w.reasons   # (a cold start mid-copy)


def test_a_checkpoint_of_other_inputs_or_flags_is_not_continued(tmp_path):
    proj, store = _stopped_store(tmp_path)
    w = _writer(store, proj, root='/work')
    assert w.start(sec(ts(9))) == 'cold' and w.cold_reason == 'other inputs or flags'


def test_a_steps_file_whose_last_newline_is_missing_is_not_continued(tmp_path):
    """The checkpoint's last step is all there but its newline is not: appending to it would glue two steps."""
    proj, store = tmp_path / 'proj', str(tmp_path / 'store')
    _six_writes(proj)
    w = _writer(store, proj)
    w.start(sec(ts(2, 30)))
    w.checkpoint(sec(ts(2, 30)))
    p = os.path.join(store, 'steps.ndjson')
    data = open(p, 'rb').read()
    open(p, 'wb').write(data[:-1])
    w2 = _writer(store, proj)
    assert w2.start(sec(ts(9))) == 'cold'
    assert live.verify(store) == 0


def test_a_rerun_replays_ops_that_share_a_text_box(tmp_path, monkeypatch):
    """Two Writes after one text: the first spends the text. On a rerun of the tail both are replayed from the
    memo; the second's signature must see the first's (lazily taken) state, or it would be applied again."""
    src = tmp_path / 'src'
    read = dict(result('r'), timestamp=ts(3, 1))
    read['toolUseResult'] = {'type': 'text', 'file': {'filePath': f'{ROOT}/z.txt', 'content': 'z', 'startLine': 1,
                                                       'numLines': 1, 'totalLines': 1}}
    write_lines(src / f'{SID}.jsonl', [
        msg('user', 'go', ts=ts(0)),
        msg('assistant', [text('Writing both'), tool('w1', 'Write', file_path=f'{ROOT}/a.py', content=_app('one')),
                          tool('w2', 'Write', file_path=f'{ROOT}/b.py', content=_app('two'))], ts=ts(1)),
        dict(result('w1'), timestamp=ts(1, 1)), dict(result('w2'), timestamp=ts(1, 2)),
        msg('assistant', [tool('r', 'Read', file_path=f'{ROOT}/z.txt')], ts=ts(3)), read])
    proj, store = tmp_path / 'proj', str(tmp_path / 'store')
    feed = iter(Feed(str(src), str(proj)))
    for _ in range(4):
        now = next(feed)
    w = _writer(store, proj, lag=600)
    w.start(now)
    assert len(w.tail) == 2
    applied = []
    real = replay.Merger._apply
    monkeypatch.setattr(replay.Merger, '_apply', lambda self, op: applied.append(op['tid']) or real(self, op))
    for now in feed:
        w.tick(now)
    assert applied and set(applied) == {'r'}          # the tail ran again; the Writes were replayed, not applied
    assert [e.meta[4] for e in w.tail] == ['Writing both', '']


def test_an_older_file_that_turns_up_after_its_resumed_copy_is_followed(tmp_path):
    """A resumed session's file C is followed; then the file it copied, A (restored, or found late), turns up. A
    holds only records C has: it waits while it might still be a copy being written, then (it does not grow) it is
    numbered, first, so the store is rebuilt, instead of waiting for a record of its own that will never come."""
    proj, store = tmp_path / 'proj', str(tmp_path / 'store')
    a = [msg('user', 'go', ts=ts(0)),
         msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/a.py', content=_app('one'))], ts=ts(1)),
         dict(result('w'), timestamp=ts(1, 1))]
    write_lines(proj / 'cccccccc-c.jsonl', a + [msg('user', 'resumed', ts=ts(9))])
    w = _writer(store, proj)
    w.start(sec(ts(10)))
    old = write_lines(proj / 'aaaaaaaa-a.jsonl', a)
    os.utime(old, (1_000_000_000, 1_000_000_000))
    w.tick(sec(ts(11)))
    assert w.tailer.waiting == [str(old)]               # it may be a copy still being written ...
    w.tick(sec(ts(12, 30)))                             # ... until it stops growing for --settle seconds
    assert not w.tailer.waiting and any(r.startswith('order') for r in w.reasons)
    exported_equals_batch(store, str(proj), ROOT)


def test_an_analyzer_fork_leaves_its_parent_as_it_was(tmp_path):
    """Run a fork ahead over 25 steps (through the regroup), then step the parent: its Zoner, cut memo, memo and
    output are those of an analyzer that was never forked (shared by mistake, the fork's steps would show in all)."""
    steps = replay.steps(_regrouping_session(tmp_path), ROOT)
    a, b = timeline.Analyzer(timings=False), timeline.Analyzer(timings=False)
    for st in steps[:30]:
        a.step(st, st.content)
        b.step(st, st.content)
    f = a.fork()
    for st in steps[30:55]:
        f.step(st, st.content)
    big = 'from flask import Flask\napp = Flask(__name__)\n\n@app.route("/big")\ndef big():\n' + ''.join(
        f'    h{i}()\n' for i in range(30)) + ''.join(
        f'\ndef h{i}():\n    x = {i}\n' + ''.join(f'    x = x + {j}\n' for j in range(20)) + '    return x\n' for i in range(30))
    f.step(steps[54], dict(steps[54].content, **{'big.py': big}))      # a blob of 700 lines: cut
    assert f.zoner.regroups == 1 and set(f.zoner.cut_memo) - set(a.zoner.cut_memo)
    zoner = lambda an: {k: v for k, v in vars(an.zoner).items() if k not in ('cut_memo', 'succ_memo')}
    texts = lambda an: {rel: h[0] for rel, h in an.memo['texts'].items()}
    assert zoner(a) == zoner(b) and set(a.zoner.cut_memo) == set(b.zoner.cut_memo) and texts(a) == texts(b)
    for st in steps[30:]:
        assert live.dumps(a.step(st, st.content)) == live.dumps(b.step(st, st.content))
    assert zoner(a) == zoner(b) and a.zoner.regroups == 1


_O = [msg('user', 'go', ts=ts(0)),
      msg('assistant', [tool('a1', 'Write', file_path=f'{ROOT}/a.py', content='a = 1\n')], ts=ts(1)), dict(result('a1'), timestamp=ts(1, 1)),
      msg('assistant', [tool('a2', 'Edit', file_path=f'{ROOT}/a.py', old_string='a = 1', new_string='a = 2')], ts=ts(2)),
      dict(result('a2'), timestamp=ts(2, 1))]
_OWN = [msg('user', 'resumed', ts=ts(10)),
        msg('assistant', [tool('b1', 'Edit', file_path=f'{ROOT}/a.py', old_string='a = 2', new_string='a = 3')], ts=ts(11)),
        dict(result('b1'), timestamp=ts(11, 1))]


@pytest.mark.parametrize('mode', ['cold', 'following', 'following, many'])
@pytest.mark.parametrize('case', ['touch', 'untimed', 'skew', 'cp', 'cp-p'])
def test_mtimes_do_not_decide_which_transcript_is_the_original(tmp_path, case, mode):
    """From the phase-2 review's sc_mtime: the original O and its full resumed copy C (or a plain copy of O whose
    name sorts first), with mtimes that disagree with history: O touched after C, an untimed cost-state record
    (Claude Code's last at exit) appended to O after C, C older on disk, `cp` (new mtime) and `cp -p` (same mtime).
    transcripts() orders by content only; live, from a cold build or while following (alone, or with another
    session so that `many` is already on), must give the same steps."""
    proj, store = tmp_path / 'proj', str(tmp_path / 'store')
    orig = write_lines(proj / '55555555-orig.jsonl', _O)
    os.utime(orig, (sec(ts(3)), sec(ts(3))))
    if mode == 'following, many':
        write_lines(proj / '33333333-other.jsonl', [msg('user', 'other', ts=ts(0, 30)),
            msg('assistant', [tool('x', 'Write', file_path=f'{ROOT}/x.py', content='x\n')], ts=ts(0, 40)),
            dict(result('x'), timestamp=ts(0, 41))])
    w = None
    if mode != 'cold':
        w = _writer(store, proj)
        w.start(sec(ts(5)))
    if case in ('touch', 'untimed', 'skew'):
        c = write_lines(proj / '99999999-resumed.jsonl', _O + _OWN)
        os.utime(c, (sec(ts(12)), sec(ts(12))))
        if case == 'touch':
            os.utime(orig, (sec(ts(13)), sec(ts(13))))
        elif case == 'untimed':
            with open(orig, 'a') as fh:
                fh.write(json.dumps({'type': 'cost-state', 'sessionId': 'x', 'totalCostUSD': 0}) + '\n')
            os.utime(orig, (sec(ts(13)), sec(ts(13))))
        else:
            os.utime(c, (sec(ts(2)), sec(ts(2))))
    else:
        c = proj / '11111111-copy.jsonl'
        (shutil.copy2 if case == 'cp-p' else shutil.copyfile)(orig, c)
        if case == 'cp':
            os.utime(c, (sec(ts(12)), sec(ts(12))))
    now = sec(ts(20))
    if w is None:
        w = _writer(store, proj)
        w.start(now)
    for t in (now, now + 30, now + 600):
        w.tick(t)
    assert not w.tailer.waiting
    assert all(r.startswith(('a second transcript', 'order')) for r in w.reasons), w.reasons
    assert live.verify(store) == 0
    exported_equals_batch(store, str(proj), ROOT)


def test_a_copy_in_progress_waits_only_while_it_grows(tmp_path):
    """While following, a new transcript holding only a followed one's records waits while it grows (a resume still
    writing its copies) and is numbered once it stops for --settle seconds (a plain copy): first, so the store is
    rebuilt, as transcripts() then puts the copy first."""
    proj, store = tmp_path / 'proj', str(tmp_path / 'store')
    orig = write_lines(proj / '55555555-orig.jsonl', _O)
    write_lines(proj / '33333333-other.jsonl', [msg('user', 'other', ts=ts(0, 30))])
    w = _writer(store, proj, settle=60)
    w.start(sec(ts(5)))
    c = proj / '11111111-copy.jsonl'
    shutil.copyfile(orig, c)
    w.tick(sec(ts(6)))
    assert w.tailer.waiting == [str(c)]
    w.tick(sec(ts(6, 50)))
    assert w.tailer.waiting == [str(c)]
    w.tick(sec(ts(7, 10)))
    assert not w.tailer.waiting and any(r.startswith('order') for r in w.reasons)
    exported_equals_batch(store, str(proj), ROOT)


def test_idle_time_counts_from_when_the_writer_found_a_file_not_its_mtime(tmp_path):
    """A transcript whose mtime is an hour old (restored, touched, clock skew) and whose last call is still running
    when the writer starts: the call is not taken as over before --idle seconds of the writer's own clock, so its
    result, coming a minute later, is no late record."""
    proj, store = tmp_path / 'proj', str(tmp_path / 'store')
    p = write_lines(proj / f'{SID}.jsonl', [msg('user', 'go', ts=ts(0)),
        msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/a.py', content=_app('one'))], ts=ts(1)),
        dict(result('w'), timestamp=ts(1, 1)),
        msg('assistant', [tool('b', 'Bash', command='make test')], ts=ts(4))])
    os.utime(p, (sec(ts(0)) - 3600, sec(ts(0)) - 3600))
    w = _writer(store, proj, idle=600)
    w.start(sec(ts(5)))
    w.tick(sec(ts(5, 30)))
    with open(p, 'a') as fh:
        fh.write(json.dumps(bash_result('b', ts(6), {'filePath': f'{ROOT}/a.py', 'hunks': [
            {'oldStart': 1, 'lines': [' from flask import Flask', '+import os']}]})) + '\n')
    w.tick(sec(ts(6, 10)))
    w.tick(sec(ts(9)))
    assert w.reasons == [] and w.n == 2
    exported_equals_batch(store, str(proj), ROOT)
