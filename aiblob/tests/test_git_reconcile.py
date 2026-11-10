"""--git-reconcile and --seed-git on small scratch repos: a git step merges its commit's change onto the snapshot and
never undoes an edit; the seed is the session's start (replay.py, SCHEMA.md "Sessions"). Each batch result is also
checked in every chunking (test_chunking._check), and one scenario live == batch (test_live.follow)."""
import json, os, subprocess

import replay, timeline
from test_chunking import _check
from test_live import _rest, exported_equals_batch, follow, sec
from test_replay import SID, msg, result, tool, ts, write_lines


def _repo(tmp_path, files):
    """A repo holding `files` in a base commit at ts(0) -> (root, commit(at, subject, files, gone=())); also
    commit.git(at, *args) runs a git command in it at that time."""
    repo = tmp_path / 'repo'
    repo.mkdir(parents=True)

    def commit(at, subject, files, gone=()):
        for name, body in files.items():
            (repo / name).write_text(body)
        for name in gone:
            (repo / name).unlink()
        env = {'GIT_AUTHOR_DATE': at, 'GIT_COMMITTER_DATE': at, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
               'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t', 'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'}
        for cmd in (['add', '-A'], ['commit', '-qm', subject]):
            subprocess.run(['git', '-C', str(repo), *cmd], check=True, env=env, capture_output=True)
    def git(at, *args):
        env = {'GIT_AUTHOR_DATE': at, 'GIT_COMMITTER_DATE': at, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
               'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t', 'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'}
        subprocess.run(['git', '-C', str(repo), *args], check=True, env=env, capture_output=True)
    commit.git = git
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    commit(ts(0), 'base', files)
    return str(repo), commit


def _edit(root, id_, path, old, new, m, s=0):
    """An Edit call at ts(m, s) and its result a second later."""
    return [msg('assistant', [tool(id_, 'Edit', file_path=f'{root}/{path}', old_string=old, new_string=new)], ts=ts(m, s),
                cwd=root),
            dict(result(id_), timestamp=ts(m, s + 1))]


def _write(root, id_, path, content, m, s=0):
    return [msg('assistant', [tool(id_, 'Write', file_path=f'{root}/{path}', content=content)], ts=ts(m, s), cwd=root),
            dict(result(id_), timestamp=ts(m, s + 1))]


def _session(tmp_path, root, calls):
    src = tmp_path / 'src'
    write_lines(src / f'{SID}.jsonl', [msg('user', 'go', ts=ts(1))] + [r for c in calls for r in c])
    return str(src)


def _steps(src, root, seed_git=True, git_reconcile=True):
    return _check(src, root, seed_git=seed_git, git_reconcile=git_reconcile, seeds=(0, 1))


def test_a_commit_made_while_edits_wait_rolls_nothing_back(tmp_path):
    """a.py: x is committed, y is not yet; c.py is edited and left out of the commit; b.py is never committed. The
    commit changes none of them in the snapshot (it used to set a.py and c.py back to its text), and the next
    edits of y and c fit."""
    root, commit = _repo(tmp_path, {'a.py': 'x = 1\ny = 1\n', 'c.py': 'c = 1\n'})
    src = _session(tmp_path, root, [
        _edit(root, 'e1', 'a.py', 'x = 1', 'x = 2', 2),
        _edit(root, 'e2', 'a.py', 'y = 1', 'y = 2', 3),
        _edit(root, 'e3', 'c.py', 'c = 1', 'c = 2', 3, 10),
        [msg('assistant', [tool('w', 'Write', file_path=f'{root}/b.py', content='b = 1\n')], ts=ts(3, 20), cwd=root)],
        _edit(root, 'e4', 'a.py', 'y = 2', 'y = 3', 5),
        _edit(root, 'e5', 'c.py', 'c = 2', 'c = 3', 5, 10)])
    commit(ts(4), 'x only', {'a.py': 'x = 2\ny = 1\n'})
    st = _steps(src, root)
    assert [s.tool for s in st] == ['Edit', 'Edit', 'Edit', 'Write', 'git', 'Edit', 'Edit']
    g = st[4]
    assert g.content == st[3].content == {'a.py': 'x = 2\ny = 2\n', 'b.py': 'b = 1\n', 'c.py': 'c = 2\n'}
    assert g.empty and (g.added, g.removed) == (0, 0) and g.kept is None
    assert all(s.applied for s in st) and st[-1].content == {'a.py': 'x = 2\ny = 3\n', 'b.py': 'b = 1\n', 'c.py': 'c = 3\n'}


def test_a_commit_by_someone_else_in_another_region_is_merged(tmp_path):
    """The session edits a.py's first line; another hand commits a change of its last line and of u.py, which no
    step touched: both come in, the session's edit stays, and the session's next edit, of the merged line, fits."""
    base = 'a = 1\n\n\ndef f():\n    return 0\n\n\ne = 1\n'
    root, commit = _repo(tmp_path, {'a.py': base, 'u.py': 'u = 1\n'})
    src = _session(tmp_path, root, [
        _edit(root, 'e1', 'a.py', 'a = 1', 'a = 2', 2),
        _edit(root, 'e2', 'a.py', 'e = 9', 'e = 10', 5)])
    commit(ts(3), 'theirs', {'a.py': base.replace('e = 1', 'e = 9'), 'u.py': 'u = 2\n'})
    st = _steps(src, root)
    assert [s.tool for s in st] == ['Edit', 'git', 'Edit']
    g = st[1]
    assert g.content == {'a.py': base.replace('a = 1', 'a = 2').replace('e = 1', 'e = 9'), 'u.py': 'u = 2\n'}
    assert not g.empty and g.kept is None
    assert st[2].applied and st[2].content['a.py'] == base.replace('a = 1', 'a = 2').replace('e = 1', 'e = 10')


def test_a_conflicting_commit_keeps_the_snapshot_and_names_the_file(tmp_path):
    """The session changes a.py's line and d.py; a commit changes the same line otherwise and deletes d.py: the
    snapshot keeps both texts, the git step lists them in `kept` (also in the timeline), and later edits fit."""
    root, commit = _repo(tmp_path, {'a.py': 'v = 1\n', 'd.py': 'd = 1\n'})
    src = _session(tmp_path, root, [
        _edit(root, 'e1', 'a.py', 'v = 1', 'v = 2\nq = 1', 2),
        _edit(root, 'e2', 'd.py', 'd = 1', 'd = 2', 2, 10),
        _edit(root, 'e3', 'a.py', 'q = 1', 'q = 2', 5)])
    commit(ts(3), 'conflicting', {'a.py': 'v = 3\nw = 1\nx = 1\n'}, gone=['d.py'])
    st = _steps(src, root)
    assert [s.tool for s in st] == ['Edit', 'Edit', 'git', 'Edit']
    g = st[2]
    assert g.kept == ['a.py', 'd.py'] and g.empty and g.content == {'a.py': 'v = 2\nq = 1\n', 'd.py': 'd = 2\n'}
    assert st[3].applied and st[3].content['a.py'] == 'v = 2\nq = 2\n'
    tl = timeline.expand(timeline.timeline(src, root, timings=False, steps=st))
    assert [s.get('kept') for s in tl] == [None, None, ['a.py', 'd.py'], None]


def test_a_commit_resets_drift_the_transcripts_never_recorded(tmp_path):
    """n.py is written by the session, then changed by something the transcripts never recorded (b -> B), and
    committed with that change while the session's later edit (c -> C) waits. The merge base is the snapshot's text
    nearest the commit's (the Write's): the drift comes in, the waiting edit stays, the next edit of B fits."""
    root, commit = _repo(tmp_path, {'keep.py': 'k = 1\n'})
    src = _session(tmp_path, root, [
        [msg('assistant', [tool('w', 'Write', file_path=f'{root}/n.py', content='a\nb\n\n\n\nc\n')], ts=ts(2), cwd=root)],
        _edit(root, 'e1', 'n.py', 'c', 'C', 3),
        _edit(root, 'e2', 'n.py', 'B', 'BB', 5)])
    commit(ts(4), 'n.py as it was on disk', {'n.py': 'a\nB\n\n\n\nc\n'})
    st = _steps(src, root)
    assert [s.tool for s in st] == ['Write', 'Edit', 'git', 'Edit']
    assert st[2].kept is None and st[2].content['n.py'] == 'a\nB\n\n\n\nC\n'
    assert st[2].reset == ['n.py']                   # the drifted line b went: a reset, listed
    assert st[3].applied and st[3].content['n.py'] == 'a\nBB\n\n\n\nC\n'


def test_reverts_and_a_line_a_commit_takes_back_reach_the_snapshot(tmp_path):
    """A revert takes the file back to a text it had before (the session's, or its seed base), and a line one
    commit adds and the next removes leaves: the snapshot is the parent's text, so it takes the commit's."""
    # the session writes a.py and edits x; commit B; it edits y; commit C; `git revert HEAD` -> B again
    root, commit = _repo(tmp_path / 'b', {'k.py': 'k\n'})
    src = _session(tmp_path / 'b', root, [_write(root, 'w', 'a.py', 'x = 1\ny = 1\n', 2),
                                          _edit(root, 'e1', 'a.py', 'x = 1', 'x = 2', 3),
                                          _edit(root, 'e2', 'a.py', 'y = 1', 'y = 2', 5)])
    commit(ts(4), 'B', {'a.py': 'x = 2\ny = 1\n'})
    commit(ts(6), 'C', {'a.py': 'x = 2\ny = 2\n'})
    commit.git(ts(7), 'revert', '--no-edit', 'HEAD')
    st = _steps(src, root)
    assert [(s.tool, s.content['a.py']) for s in st] == [
        ('Write', 'x = 1\ny = 1\n'), ('Edit', 'x = 2\ny = 1\n'), ('git', 'x = 2\ny = 1\n'), ('Edit', 'x = 2\ny = 2\n'),
        ('git', 'x = 2\ny = 2\n'), ('git', 'x = 2\ny = 1\n')]
    # x 1 -> 2 -> 1 -> 3, committed; the revert goes back to 1, a text the session had too (and the seed's)
    root, commit = _repo(tmp_path / 't', {'a.py': 'x = 1\n'})
    src = _session(tmp_path / 't', root, [_edit(root, 'e1', 'a.py', 'x = 1', 'x = 2', 2),
                                          _edit(root, 'e2', 'a.py', 'x = 2', 'x = 1', 2, 10),
                                          _edit(root, 'e3', 'a.py', 'x = 1', 'x = 3', 2, 20)])
    commit(ts(3), 'x = 3', {'a.py': 'x = 3\n'})
    commit.git(ts(4), 'revert', '--no-edit', 'HEAD')
    st = _steps(src, root)
    assert [(s.tool, s.content['a.py']) for s in st][-2:] == [('git', 'x = 3\n'), ('git', 'x = 1\n')]
    # a commit adds DBG to a file the session edited and committed; the next commit removes it
    base = 'x = 1\n\n\n\nz = 1\n'
    root, commit = _repo(tmp_path / 'd', {'a.py': base})
    src = _session(tmp_path / 'd', root, [_edit(root, 'e1', 'a.py', 'x = 1', 'x = 2', 2)])
    mine = base.replace('x = 1', 'x = 2')
    commit(ts(3), 'mine', {'a.py': mine})
    commit(ts(4), 'debug', {'a.py': mine + 'DBG\n'})
    commit(ts(5), 'no debug', {'a.py': mine})
    st = _steps(src, root)
    assert [s.content['a.py'] for s in st] == [mine, mine, mine + 'DBG\n', mine]
    assert all(s.kept is None and s.reset is None for s in st)


def test_a_reset_that_drops_an_edit_is_listed(tmp_path):
    """The session edits ten lines and line L; a commit holds the ten and a different L. The snapshot is nearer
    the commit than the parent is, so the drift rule takes the commit's L and drops the session's: the git step
    lists the file in `reset`, which the timeline carries (and expand() keeps)."""
    body = ''.join(f'v{i} = {i}\n' for i in range(12))
    root, commit = _repo(tmp_path, {'a.py': body})
    src = _session(tmp_path, root, [_edit(root, f'e{i}', 'a.py', f'v{i} = {i}\n', f'v{i} = {i}0\n', 2, 2 * i)
                                    for i in range(10)] + [_edit(root, 'eL', 'a.py', 'v11 = 11', 'v11 = AI', 2, 30)])
    ten = ''.join(f'v{i} = {i}0\n' for i in range(10)) + 'v10 = 10\n'
    commit(ts(3), 'theirs', {'a.py': ten + 'v11 = USER\n'})
    st = _steps(src, root)
    g = st[-1]
    assert g.tool == 'git' and g.content['a.py'] == ten + 'v11 = USER\n' and (g.kept, g.reset) == (None, ['a.py'])
    tl = timeline.timeline(src, root, timings=False, steps=st)
    v2 = timeline.timeline(src, root, timings=False, steps=st, v2=True)
    assert [s.get('reset') for s in timeline.expand(json.loads(json.dumps(tl)))] == [None] * 11 + [['a.py']]
    assert json.dumps(timeline.expand(json.loads(json.dumps(tl)))) == json.dumps(v2['steps'])


def test_the_seed_is_the_session_start(tmp_path):
    """u.py, which no step touches, is in every snapshot as the commit before the first step has it, and later.py,
    which a later commit brings, is in none (both used to come from the commit before the last step). With
    --git-reconcile they change at the git steps that bring them."""
    root, commit = _repo(tmp_path, {'a.py': 'a = 1\n', 'u.py': 'u = 1\n'})
    src = _session(tmp_path, root, [
        _edit(root, 'e1', 'a.py', 'a = 1', 'a = 2', 2),
        _edit(root, 'e2', 'a.py', 'a = 2', 'a = 3', 6)])
    commit(ts(4), 'later', {'u.py': 'u = 2\n', 'later.py': 'l = 1\n'})
    commit(ts(8), 'after the last step', {'u.py': 'u = 3\n'})
    seeded = _steps(src, root, git_reconcile=False)
    assert [s.content for s in seeded] == [{'a.py': 'a = 2\n', 'u.py': 'u = 1\n'}, {'a.py': 'a = 3\n', 'u.py': 'u = 1\n'}]
    st = _steps(src, root)
    assert [(s.tool, s.content.get('u.py'), 'later.py' in s.content) for s in st] == [
        ('Edit', 'u = 1\n', False), ('git', 'u = 2\n', True), ('Edit', 'u = 2\n', True), ('git', 'u = 3\n', True)]
    assert st[-1].content['a.py'] == 'a = 3\n'


def test_merged_and_kept_commits_made_while_live_equal_batch(tmp_path):
    """Commits made while the writer runs, one merged (another region) and one conflicting: the store equals a batch
    build at every tick and after a restart."""
    base = 'a = 1\n\n\ndef f():\n    return 0\n\n\ne = 1\n'
    root, commit = _repo(tmp_path, {'a.py': base, 'v.py': 'v = 1\n'})
    src = _session(tmp_path, root, [
        _edit(root, 'e1', 'a.py', 'a = 1', 'a = 2', 2),
        _edit(root, 'e2', 'v.py', 'v = 1', 'v = 2\nq = 1', 2, 10),
        _edit(root, 'e3', 'a.py', 'e = 9', 'e = 10', 7),
        _edit(root, 'e4', 'v.py', 'q = 1', 'q = 2', 9)])
    repo = tmp_path / 'repo'

    def meanwhile(k, now):
        if now >= sec(ts(5)) and 'e = 9' not in (repo / 'a.py').read_text():
            commit(ts(5), 'theirs', {'a.py': base.replace('e = 1', 'e = 9')})
        if now >= sec(ts(8)) and 'w = 1' not in (repo / 'v.py').read_text():
            commit(ts(8), 'conflicting', {'v.py': 'v = 3\nw = 1\nx = 1\n'})
    w, store, proj = follow(tmp_path, src, root, seed_git=True, git_reconcile=True, restarts=1, between=meanwhile)
    exported_equals_batch(store, proj, root, seed_git=True, git_reconcile=True, rest=_rest(store))
    st = [json.loads(l) for l in open(os.path.join(store, 'steps.ndjson'))]
    assert [(s['tool'], s.get('kept')) for s in st] == [
        ('Edit', None), ('Edit', None), ('git', None), ('Edit', None), ('git', ['v.py']), ('Edit', None)]
    assert all(s.get('applied', True) for s in st)
    rs = replay.steps(replay.transcripts(proj), root, seed_git=True, git_reconcile=True, rest=_rest(store))
    assert rs[-1].content == {'a.py': base.replace('a = 1', 'a = 2').replace('e = 1', 'e = 10'), 'v.py': 'v = 2\nq = 2\n'}


def test_a_tail_that_runs_ahead_through_commits_leaves_the_committed_replay_alone(tmp_path):
    """Two commits, authored a minute ahead, come at 0:05 while the writer runs: the first conflicts with the
    session's v.py (kept: its parent is v.py's seed base), the second takes the session's text. The tail run then
    (a fork) applies both, the second recording v.py's new git text. An edit written at 0:05:30 sorts before the
    commits, and they commit at 0:06:20 before the tail runs again (prov_every 60 s), so the committed replay applies
    them itself, without the tail's record: it must still find the seed base as v.py's git text (a fork sharing
    that record with it would make the first commit a reset). The committed steps equal a batch build's at every
    tick."""
    root, commit = _repo(tmp_path, {'v.py': 'v = 1\n'})
    src = _session(tmp_path, root, [_edit(root, 'e1', 'v.py', 'v = 1', 'v = 2\nq = 1', 2),
                                    _write(root, 'w', 'k.py', 'k\n', 3),
                                    [msg('user', 'go on', ts=ts(5))],
                                    _edit(root, 'e2', 'k.py', 'k', 'kk', 5, 30),
                                    _edit(root, 'e3', 'k.py', 'kk', 'kkk', 6, 20),
                                    _edit(root, 'e4', 'k.py', 'kkk', 'kkkk', 8)])
    repo = tmp_path / 'repo'

    def meanwhile(k, now):
        if now >= sec(ts(5)) and not (repo / 'done').exists():
            commit(ts(6), 'conflicting', {'v.py': 'v = 3\nw = 1\nx = 1\n'})
            commit(ts(6, 10), "the session's", {'v.py': 'v = 2\nq = 1\n', 'done': ''})
    w, store, proj = follow(tmp_path, src, root, seed_git=True, git_reconcile=True, prov_every=60.0,
                            between=meanwhile)
    exported_equals_batch(store, proj, root, seed_git=True, git_reconcile=True, rest=_rest(store))
    st = [json.loads(l) for l in open(os.path.join(store, 'steps.ndjson'))]
    assert [(s['tool'], s.get('kept'), s.get('reset')) for s in st if s['tool'] == 'git'] == [
        ('git', ['v.py'], None), ('git', None, None)]
