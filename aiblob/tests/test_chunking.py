"""replay.Engine fed in pieces (one record at a time, transcripts interleaved, bytes split anywhere) makes exactly
the steps a batch replay.steps() makes."""
import heapq, inspect, json, random
from dataclasses import asdict

import pytest

import replay
import test_replay
from test_replay import ROOT, SID, bash_result, msg, result, text, tool, ts, write_lines


def _inputs(path):
    files = replay.transcripts(path)
    return files, [list(replay.load_lines(f)) for f in files]


def _engine(files, root, seed_git, git_reconcile):
    eng = replay.Engine(root, seed_git, git_reconcile, many=len(files) > 1 or git_reconcile)
    for n, f in enumerate(files):
        eng.add(n, f)
    return eng


def _mark(o):
    """The time a record's ops can carry ('' when a user / assistant record has no ISO timestamp)."""
    if o.get('type') not in ('user', 'assistant'):
        return None
    t = o.get('timestamp')
    return t if isinstance(t, str) else ''


def _floors(recs):
    """Per transcript: the earliest time among its records from position i on."""
    out = []
    for rs in recs:
        f, low = ['~'], '~'
        for o in reversed(rs):
            m = _mark(o)
            low = min(low, m) if m is not None else low
            f.append(low)
        out.append(f[::-1])
    return out


def _feed(path, root, seed_git, git_reconcile, order, seed=0):
    """Feed every record one at a time in `order` (a list of transcript numbers, each once per record), draining
    after each with the exact watermark: the earliest time among the records not fed yet."""
    files, recs = _inputs(path)
    eng = _engine(files, root, seed_git, git_reconcile)
    floors, pos, out = _floors(recs), [0] * len(files), []
    for n in order:
        eng.feed(n, recs[n][pos[n]])
        pos[n] += 1
        out += eng.drain(min([floors[m][pos[m]] for m in range(len(files))] or ['~']))
    out += eng.finish()
    assert eng.late == 0
    return out


def _orders(recs, seed):
    files = 'files', [n for n, rs in enumerate(recs) for _ in rs]
    heap = [(rs[0].get('timestamp') if isinstance(rs[0].get('timestamp'), str) else '', n, 0)
            for n, rs in enumerate(recs) if rs]
    heapq.heapify(heap)
    timed = []
    while heap:
        _, n, i = heapq.heappop(heap)
        timed.append(n)
        if i + 1 < len(recs[n]):
            t = recs[n][i + 1].get('timestamp')
            heapq.heappush(heap, (t if isinstance(t, str) else '', n, i + 1))
    rnd = random.Random(seed)
    shuffled = list(files[1])
    rnd.shuffle(shuffled)            # which transcript speaks next; each one's records stay in their order
    return [files, ('time', timed), ('shuffled', shuffled)]


def _bytes(path, root, seed_git, git_reconcile, seed=0):
    """Each transcript's bytes in random pieces (cut anywhere, even inside a UTF-8 character) through
    replay.Lines, transcripts interleaved at random, draining after each piece."""
    files, recs = _inputs(path)
    eng = _engine(files, root, seed_git, git_reconcile)
    rnd = random.Random(seed)
    data = [open(f, 'rb').read() for f in files]
    lines = [replay.Lines() for _ in files]
    floors, pos, at, out = _floors(recs), [0] * len(files), [0] * len(files), []
    live = [n for n in range(len(files)) if data[n]]
    while live:
        n = rnd.choice(live)
        k = rnd.randint(1, 40)
        got = lines[n].feed(data[n][at[n]:at[n] + k])
        at[n] += k
        if at[n] >= len(data[n]):
            got += lines[n].close()
            live.remove(n)
        for o in got:
            assert o == recs[n][pos[n]]
            eng.feed(n, o)
            pos[n] += 1
        out += eng.drain(min([floors[m][pos[m]] for m in range(len(files))] or ['~']))
    out += eng.finish()
    assert pos == [len(rs) for rs in recs] and eng.late == 0
    return out


def _all_at_once(path, root, seed_git, git_reconcile):
    files, recs = _inputs(path)
    eng = _engine(files, root, seed_git, git_reconcile)
    for n, rs in enumerate(recs):
        for o in rs:
            eng.feed(n, o)
    return eng.finish()


def _dump(steps):
    return [asdict(s) for s in steps]


def _check(path, root=None, seed_git=False, git_reconcile=False, seeds=(0, 1, 2)):
    """Batch steps, after checking that every chunking gives them."""
    want = _batch(path, root, seed_git, git_reconcile)
    args = (path, root, seed_git, git_reconcile)
    assert _dump(_all_at_once(*args)) == _dump(want), 'all at once'
    recs = _inputs(path)[1]
    for seed in seeds:
        for name, order in _orders(recs, seed):
            assert _dump(_feed(*args, order)) == _dump(want), (name, seed)
        assert _dump(_bytes(*args, seed)) == _dump(want), ('bytes', seed)
    return want


_batch = replay.steps
SCENARIOS = [f for name, f in inspect.getmembers(test_replay, inspect.isfunction)
             if name.startswith('test_') and ('replay.steps(' in inspect.getsource(f) or 'timeline(' in inspect.getsource(f)
                                              or '_retry(' in inspect.getsource(f) or '_stale_seen(' in inspect.getsource(f))]


@pytest.mark.parametrize('scenario', SCENARIOS, ids=lambda f: f.__name__)
def test_every_replay_scenario_gives_the_same_steps_in_any_chunking(scenario, tmp_path, monkeypatch):
    calls = []

    def checked(path, root=None, seed_git=False, git_reconcile=False):
        calls.append(path)
        return _check(path, root, seed_git, git_reconcile, seeds=(0,))
    monkeypatch.setattr(replay, 'steps', checked)
    scenario(**({'tmp_path': tmp_path} if 'tmp_path' in inspect.signature(scenario).parameters else {}))
    assert calls


def reply(mid, content, at):
    """An assistant record of API message mid (a new mid: every call of the earlier messages was answered)."""
    o = msg('assistant', content, ts=at)
    o['message']['id'] = mid
    return o


def _mixed(tmp_path):
    """A main session, its subagent and a resumed copy of the main session: a long Bash call in main that only saw
    the subagent's change, a subagent Bash report that keeps its credit, a heredoc of a call cut off before its
    result, an errored Edit, an Edit rebased on originalFile, copied records (counted once, their text spent)."""
    proj = tmp_path / 'proj'
    hunk = {'oldStart': 1, 'lines': [' a = 1', '+b = 2']}
    said = msg('assistant', [text('Writing a'), tool('m1', 'Write', file_path=f'{ROOT}/a.py', content='a = 1\n')], ts=ts(1))
    done = result('m1')
    done['timestamp'] = ts(1, 1)
    bad = result('m5', error=True)
    bad['timestamp'] = ts(11, 1)
    rebased = result('m6')
    rebased.update(timestamp=ts(12, 1), toolUseResult={'originalFile': 'a = 1\nb = 2\nq = 0\n'})
    cut = "cat > d.py <<'EOF'\nd = 1\nEOF\nsleep 600"
    write_lines(proj / f'{SID}.jsonl', [
        msg('user', 'build it', ts=ts(0)),
        said, done,
        reply('r2', [tool('long', 'Bash', command='make test')], ts(2)),
        bash_result('long', ts(9), {'filePath': f'{ROOT}/a.py', 'hunks': [hunk]}, shared=True),
        reply('r3', [tool('cut', 'Bash', command=cut)], ts(10)),
        msg('user', [text('[Request interrupted by user for tool use]')], ts=ts(10, 30)),
        reply('r4', [tool('m5', 'Edit', file_path=f'{ROOT}/a.py', old_string='zz', new_string='y')], ts(11)),
        bad,
        msg('assistant', [tool('m6', 'Edit', file_path=f'{ROOT}/a.py', old_string='q = 0', new_string='q = 1')], ts=ts(12)),
        rebased,
    ])
    sub = write_lines(proj / SID / 'subagents' / 'agent-x.jsonl', [
        msg('user', 'add b', ts=ts(2, 10), isSidechain=True),
        msg('assistant', [text('Sub writes c'), tool('s1', 'Bash', command="cat > c.py <<'EOF'\nc = 1\nEOF")], ts=ts(2, 20),
            isSidechain=True),
        dict(result('s1'), timestamp=ts(2, 25)),
        msg('assistant', [tool('short', 'Bash', command='python3 fix.py')], ts=ts(3), isSidechain=True),
        bash_result('short', ts(5), {'filePath': f'{ROOT}/a.py', 'hunks': [hunk]}),
        msg('assistant', [tool('s2', 'Edit', file_path=f'{ROOT}/c.py', old_string='c = 1', new_string='c = 2')],
            ts=ts(6), isSidechain=True),
        dict(result('s2'), timestamp=ts(6, 1)),
    ])
    (proj / SID / 'subagents' / 'agent-x.meta.json').write_text(json.dumps({'agentType': 'executor', 'description': 'b'}))
    copy = proj / 'bbbbbbbb-copy.jsonl'
    copy.write_text('\n'.join(json.dumps(o) for o in [
        msg('user', 'build it', ts=ts(0)), said, done,
        msg('user', 'resumed', ts=ts(20)),
        msg('assistant', [tool('k2', 'Write', file_path=f'{ROOT}/e.py', content='é = 1\n')], ts=ts(21)),
    ]))                                             # no newline after the last record
    return str(proj), sub


def test_mixed_transcripts_give_the_same_steps_in_any_chunking(tmp_path):
    proj, _ = _mixed(tmp_path)
    st = _check(proj, seeds=range(6))
    assert [(s.tool, s.file, s.agent, s.session, s.said, s.applied) for s in st] == [
        ('Write', 'a.py', 'main', 'aaaaaaaa', 'Writing a', True),
        ('Bash', 'c.py', 'executor · b', 'aaaaaaaa', 'Sub writes c', True),
        ('Bash', 'a.py', 'executor · b', 'aaaaaaaa', '', True),        # the short call keeps its change
        ('Edit', 'c.py', 'executor · b', 'aaaaaaaa', '', True),
        ('Bash', 'd.py', 'main', 'aaaaaaaa', '', True),                 # cut off: no result, its heredoc ran
        ('Edit', 'a.py', 'main', 'aaaaaaaa', '', False),                # the tool said error
        ('Edit', 'a.py', 'main', 'aaaaaaaa', '', True),                 # rebased on originalFile
        ('Write', 'e.py', 'main', 'bbbbbbbb', '', True)]                # the copied text was spent by the original
    assert st[6].seeded and st[-1].content['a.py'] == 'a = 1\nb = 2\nq = 1\n'


def test_a_report_waits_while_a_bash_call_of_another_transcript_that_could_take_its_credit_runs(tmp_path):
    """Strict order: the subagent's report (3 -> 5) is credited only once main's call (2 -> 9), which may still
    name the file, has returned; the Write and the subagent's heredoc before it go out as soon as they are known."""
    proj, _ = _mixed(tmp_path)
    files, recs = _inputs(proj)
    eng = _engine(files, None, False, False)
    floors, pos, when = _floors(recs), [0] * len(files), {}
    for n in _orders(recs, 0)[1][1]:
        eng.feed(n, recs[n][pos[n]])
        pos[n] += 1
        for s in eng.drain(min(floors[m][pos[m]] for m in range(len(files)))):
            when[(s.tool, s.file, s.ts)] = recs[n][pos[n] - 1].get('timestamp')
    eng.finish()
    assert when[('Write', 'a.py', ts(1))] < ts(2, 30)
    assert when[('Bash', 'c.py', ts(2, 20))] < ts(3)
    assert when[('Bash', 'a.py', ts(5))] == ts(9)       # main's result
    assert when[('Bash', 'd.py', ts(10))] == ts(11)     # its call never got a result: over at the next message
    assert eng.late == 0


def _racing(tmp_path):
    """Reports whose credit depends on calls of other transcripts that end later: A's report (1 -> 5) goes to
    B's shorter overlapping call (4 -> 7) and moves after it; D's report (5:30 -> 6) waits for C's call (5:40 ->
    20), which reports nothing, and so does B's; C's prompt at 6 is the record a transcript-by-transcript feed
    still lacks after B's report is in."""
    proj = tmp_path / 'proj'
    hunk = {'oldStart': 1, 'lines': [' a = 1', '+b = 2']}
    write_lines(proj / 'aaaaaaaa-a.jsonl', [
        msg('user', 'go', ts=ts(0)),
        msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/a.py', content='a = 1\n'),
                          tool('w2', 'Write', file_path=f'{ROOT}/b.py', content='a = 1\n')], ts=ts(0, 30)),
        dict(result('w'), timestamp=ts(0, 31)), dict(result('w2'), timestamp=ts(0, 31)),
        msg('assistant', [tool('xa', 'Bash', command='python3 one.py')], ts=ts(1)),
        bash_result('xa', ts(5), {'filePath': f'{ROOT}/a.py', 'hunks': [hunk]})])
    write_lines(proj / 'bbbbbbbb-b.jsonl', [
        msg('user', 'go', ts=ts(0, 40)),
        msg('assistant', [tool('yb', 'Bash', command='python3 two.py')], ts=ts(4)),
        bash_result('yb', ts(7), {'filePath': f'{ROOT}/a.py', 'hunks': [hunk]})])
    write_lines(proj / 'cccccccc-c.jsonl', [
        msg('user', 'go', ts=ts(0, 50)),
        msg('assistant', [tool('slow', 'Bash', command='make all')], ts=ts(5, 40)),
        msg('user', 'meanwhile', ts=ts(6)),
        bash_result('slow', ts(20))])
    write_lines(proj / 'dddddddd-d.jsonl', [
        msg('user', 'go', ts=ts(0, 55)),
        msg('assistant', [tool('zd', 'Bash', command='python3 three.py')], ts=ts(5, 30)),
        bash_result('zd', ts(6), {'filePath': f'{ROOT}/b.py', 'hunks': [hunk]})])
    return str(proj)


def test_reports_racing_across_transcripts_give_the_same_steps_in_any_chunking(tmp_path):
    st = _check(_racing(tmp_path), ROOT, seeds=range(6))
    assert [(s.session, s.file, s.ts) for s in st] == [
        ('aaaaaaaa', 'a.py', ts(0, 30)), ('aaaaaaaa', 'b.py', ts(0, 30)), ('dddddddd', 'b.py', ts(6)),
        ('bbbbbbbb', 'a.py', ts(7))]


def test_a_commit_at_the_second_of_the_first_report_waits_for_its_credit(tmp_path):
    """The first op under root is a report at 0:05 that C's running call could still take; a commit authored at that
    very second is a step only because the report stays there, which is known once C's call returns."""
    import subprocess
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'a.py').write_text('a = 1\nb = 2\n')
    env = {'GIT_AUTHOR_DATE': ts(5), 'GIT_COMMITTER_DATE': ts(5), 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t', 'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'}
    for cmd in (['init', '-q'], ['add', '.'], ['commit', '-qm', 'same second']):
        subprocess.run(['git', '-C', str(repo), *cmd], check=True, env=env)
    proj, root = tmp_path / 'proj', str(repo)
    write_lines(proj / 'aaaaaaaa-a.jsonl', [
        msg('user', 'go', ts=ts(0)),
        msg('assistant', [tool('xa', 'Bash', command='python3 fix.py')], ts=ts(1), cwd=root),
        bash_result('xa', ts(5), {'filePath': f'{root}/a.py', 'hunks': [{'oldStart': 1, 'lines': [' a = 1', '-b = 2', '+b = 3']}]})])
    write_lines(proj / 'bbbbbbbb-c.jsonl', [
        msg('user', 'go', ts=ts(0, 30)),
        msg('assistant', [tool('slow', 'Bash', command='make all')], ts=ts(2), cwd=root),
        msg('user', 'meanwhile', ts=ts(5, 30)),
        bash_result('slow', ts(9))])
    st = _check(str(proj), root, git_reconcile=True, seeds=range(4))
    assert [(s.tool, s.ts) for s in st] == [('git', ts(5)), ('Bash', ts(5))]


def _named(tmp_path, end):
    """A's call names a.py (top rank) and reports a change of it (1 -> 5); B's call, which names it too, runs from 4
    to `end` and reports the same change; C only speaks (its prompts move the time on)."""
    proj = tmp_path / f'named{end}'
    hunk = {'oldStart': 1, 'lines': [' a = 1', '+b = 2']}
    write_lines(proj / 'aaaaaaaa-a.jsonl', [
        msg('user', 'go', ts=ts(0)),
        msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/a.py', content='a = 1\n')], ts=ts(0, 30)),
        dict(result('w'), timestamp=ts(0, 31)),
        msg('assistant', [tool('xa', 'Bash', command='sed -i 2ib=2 a.py')], ts=ts(1)),
        bash_result('xa', ts(5), {'filePath': f'{ROOT}/a.py', 'hunks': [hunk]})])
    write_lines(proj / 'bbbbbbbb-b.jsonl', [
        msg('user', 'go', ts=ts(0, 40)),
        msg('assistant', [tool('yb', 'Bash', command='python3 fix.py a.py')], ts=ts(4)),
        bash_result('yb', ts(end), {'filePath': f'{ROOT}/a.py', 'hunks': [hunk]})])
    write_lines(proj / 'cccccccc-c.jsonl', [msg('user', f'at {m}', ts=ts(m)) for m in (0, 6, 7, 8, 9, 10)])
    return str(proj)


def test_a_running_call_that_can_only_win_on_a_shorter_window_holds_a_report_no_longer_than_that(tmp_path):
    short = _check(_named(tmp_path, 6), ROOT, seeds=range(4))     # B's window (2 min) is the shorter: B's change
    assert [(s.session, s.ts) for s in short] == [('aaaaaaaa', ts(0, 30)), ('bbbbbbbb', ts(6))]
    proj = _named(tmp_path, 20)
    long = _check(proj, ROOT, seeds=range(4))                       # B runs 16 min: A keeps its change
    assert [(s.session, s.ts) for s in long] == [('aaaaaaaa', ts(0, 30)), ('aaaaaaaa', ts(5))]
    files, recs = _inputs(proj)
    eng = _engine(files, ROOT, False, False)
    floors, pos, when = _floors(recs), [0] * len(files), {}
    for n in _orders(recs, 0)[1][1]:
        eng.feed(n, recs[n][pos[n]])
        pos[n] += 1
        for s in eng.drain(min(floors[m][pos[m]] for m in range(len(files)))):
            when[s.ts] = recs[n][pos[n] - 1].get('timestamp')
    # B, running since 4, can no longer be shorter than A's 4 minutes once nothing before 8 is to come: after C's 7
    assert when[ts(5)] == ts(7)


def test_lines_hold_a_line_until_its_newline():
    lines = replay.Lines()
    assert lines.feed(b'{"a": 1}\n{"b"') == [{'a': 1}]
    assert lines.feed(b': "\xc3') == []                 # inside the line, inside a UTF-8 character
    assert lines.feed(b'\xa9"}') == []                  # complete JSON, but no newline yet: a writer may add more
    assert lines.feed(b'\r\n\nnot json\n') == [{'b': 'é'}]
    assert lines.feed(b'{"c": 3}') == [] and lines.close() == [{'c': 3}]
    assert lines.close() == []
