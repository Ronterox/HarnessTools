"""replay.steps() on a hand-built transcript."""
import json

import replay

ROOT = '/work/app'


def msg(kind, content, **kw):
    o = {'type': kind, 'message': {'content': content}, 'timestamp': kw.pop('ts', '2026-01-01T00:00:00Z')}
    if kind == 'assistant':
        o['cwd'] = ROOT
    o.update(kw)
    return o


def tool(id_, name, **inp):
    return {'type': 'tool_use', 'id': id_, 'name': name, 'input': inp}


def text(t):
    return {'type': 'text', 'text': t}


def result(id_, error=False):
    return msg('user', [{'type': 'tool_result', 'tool_use_id': id_, 'content': 'x', 'is_error': error}])


def transcript(tmp_path, lines):
    p = tmp_path / 's.jsonl'
    p.write_text('\n'.join(json.dumps(l) for l in lines) + '\nnot json at all\n\n')
    return str(p)


def test_edit_kinds_ok_flags_said_and_turns(tmp_path):
    heredoc = "cat > b.py <<'EOF'\nx = 1\ny = 2\nEOF\n"
    lines = [
        msg('user', 'build it'),
        msg('assistant', [text('## Writing the module\nmore detail'), tool('t1', 'Write', file_path=f'{ROOT}/a.py', content='a = 1\nb = 2\nc = 3\n')]),
        result('t1'),
        msg('assistant', [tool('t2', 'Edit', file_path=f'{ROOT}/a.py', old_string='b = 2', new_string='b = 20')]),
        result('t2'),
        msg('assistant', [text('Now a bad edit'), tool('t3', 'Edit', file_path=f'{ROOT}/a.py', old_string='NOT THERE', new_string='z')]),
        result('t3', error=True),
        msg('assistant', [tool('t4', 'MultiEdit', file_path=f'{ROOT}/a.py', edits=[
            {'old_string': 'a = 1', 'new_string': 'a = 10'},
            {'old_string': 'c = 3', 'new_string': 'c = 30\nd = 4'}])]),
        result('t4'),
        msg('assistant', [tool('t5', 'Bash', command=heredoc)]),
        result('t5'),
        msg('assistant', [tool('t6', 'Write', file_path='/elsewhere/ignored.py', content='nope\n')]),
        msg('assistant', [tool('side', 'Write', file_path=f'{ROOT}/side.py', content='s\n')], isSidechain=True),
        msg('user', 'now more'),
        msg('assistant', [text('Second prompt'), tool('t7', 'Edit', file_path=f'{ROOT}/b.py', old_string='x = 1', new_string='x = 5', replace_all=True)]),
        result('t7'),
    ]
    st = replay.steps(transcript(tmp_path, lines))
    assert [(s.idx, s.tool, s.file) for s in st] == [
        (0, 'Write', 'a.py'), (1, 'Edit', 'a.py'), (2, 'Edit', 'a.py'),
        (3, 'MultiEdit', 'a.py'), (4, 'MultiEdit', 'a.py'), (5, 'Bash', 'b.py'), (6, 'Edit', 'b.py')]

    assert st[0].content == {'a.py': 'a = 1\nb = 2\nc = 3\n'}
    assert (st[0].added, st[0].removed) == (3, 0)

    assert st[1].content['a.py'] == 'a = 1\nb = 20\nc = 3\n'
    assert (st[1].added, st[1].removed) == (1, 1)

    # old string not found: the step exists, the file is unchanged, nothing counted
    assert st[2].content['a.py'] == st[1].content['a.py']
    assert (st[2].added, st[2].removed) == (0, 0)
    assert [s.ok for s in st] == [True, True, False, True, True, True, True]

    # MultiEdit: one step per sub-edit, applied in order
    assert st[3].content['a.py'] == 'a = 10\nb = 20\nc = 3\n'
    assert st[4].content['a.py'] == 'a = 10\nb = 20\nc = 30\nd = 4\n'
    assert (st[4].added, st[4].removed) == (2, 1)

    # heredoc: relative path joins the root; content gets its trailing newline back
    assert st[5].content['b.py'] == 'x = 1\ny = 2\n'
    assert st[5].content['a.py'] == st[4].content['a.py']      # earlier files stay in the snapshot
    assert set(st[6].content) == {'a.py', 'b.py'}              # outside-root and sidechain writes never appear
    assert st[6].content['b.py'] == 'x = 5\ny = 2\n'

    # `said` is the first line of the text right before, markdown stripped, and is spent after one use
    assert [s.said for s in st] == ['Writing the module', '', 'Now a bad edit', '', '', '', 'Second prompt']
    assert [s.turn for s in st] == [0, 0, 0, 0, 0, 0, 1]


def test_said_stays_when_a_call_touches_nothing_under_root(tmp_path):
    lines = [
        msg('user', 'go'),
        msg('assistant', [text('Planning'), tool('t1', 'Write', file_path='/elsewhere/x.py', content='x\n')]),
        msg('assistant', [tool('t2', 'Write', file_path=f'{ROOT}/a.py', content='a\n')]),
    ]
    st = replay.steps(transcript(tmp_path, lines))
    assert [(s.file, s.said) for s in st] == [('a.py', 'Planning')]


def test_root_defaults_to_the_first_assistant_cwd_and_explicit_root_wins(tmp_path):
    lines = [msg('user', 'go'), msg('assistant', [tool('t1', 'Write', file_path=f'{ROOT}/sub/a.py', content='a\n')])]
    p = transcript(tmp_path, lines)
    assert [s.file for s in replay.steps(p)] == ['sub/a.py']
    assert [s.file for s in replay.steps(p, f'{ROOT}/sub')] == ['a.py']
    assert replay.steps(p, '/other') == []


SID = 'aaaaaaaa-1111-2222-3333-444444444444'


def ts(m, s=0):
    return f'2026-01-01T00:{m:02d}:{s:02d}.000Z'


def bash_result(id_, at, *files, **kw):
    """A Bash tool result whose toolUseResult records the files it changed (bashEditDiff)."""
    o = result(id_)
    o.update(timestamp=at, toolUseResult={'stdout': '', 'bashEditDiff': {'files': list(files), **kw}}, **{})
    return o


def write_lines(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(json.dumps(l) for l in lines) + '\n')
    return str(path)


def two_transcripts(tmp_path):
    """A main session and one subagent of it, laid out as ~/.claude/projects/<dir> keeps them."""
    proj = tmp_path / 'proj'
    write_lines(proj / f'{SID}.jsonl', [
        msg('user', 'build it\nwith details', ts=ts(0)),
        msg('assistant', [text('Main writes a'), tool('m1', 'Write', file_path=f'{ROOT}/a.py', content='x = 1\ny = 2\n')], ts=ts(1)),
        result('m1'),
        msg('assistant', [tool('m2', 'Edit', file_path=f'{ROOT}/a.py', old_string='y = 2', new_string='y = 3')], ts=ts(4)),
        msg('assistant', [tool('m3', 'Edit', file_path=f'{ROOT}/missing.py', old_string='nope', new_string='z')], ts=ts(5)),
        msg('assistant', [tool('m4', 'Edit', file_path=f'{ROOT}/a.py', old_string='NOT THERE', new_string='z')], ts=ts(6)),
        result('m4', error=True),
    ])
    sub = proj / SID / 'subagents' / 'agent-abc123.jsonl'
    hunk = {'oldStart': 1, 'oldLines': 2, 'newStart': 1, 'newLines': 3, 'lines': [' x = 1', '+w = 0', ' y = 2']}
    write_lines(sub, [
        msg('user', 'add the b module', ts=ts(1, 30), isSidechain=True),
        msg('assistant', [text('Sub adds b'), tool('s1', 'Write', file_path=f'{ROOT}/b.py', content='b = 1\n')], ts=ts(2), isSidechain=True),
        msg('assistant', [tool('s2', 'Bash', command='python3 fix.py')], ts=ts(3), isSidechain=True),
        bash_result('s2', ts(3, 30), {'filePath': f'{ROOT}/a.py', 'hunks': [hunk]}),
        msg('assistant', [tool('s3', 'Bash', command='make')], ts=ts(3, 40), isSidechain=True),
        # a concurrent call reporting the same change again: done already, no step
        bash_result('s3', ts(3, 50), {'filePath': f'{ROOT}/a.py', 'hunks': [hunk]}, shared=True),
    ])
    (proj / SID / 'subagents' / 'agent-abc123.meta.json').write_text(
        json.dumps({'agentType': 'oh-my-claudecode:executor', 'description': 'add b'}))
    return proj, sub


def test_two_transcripts_merge_by_time_with_tags_and_applied(tmp_path):
    proj, sub = two_transcripts(tmp_path)
    assert replay.transcripts(str(proj)) == [str(proj / f'{SID}.jsonl'), str(sub)]
    st = replay.steps(str(proj))                               # root: the main session's cwd
    assert [(s.ts, s.tool, s.file) for s in st] == [
        (ts(1), 'Write', 'a.py'), (ts(2), 'Write', 'b.py'), (ts(3, 30), 'Bash', 'a.py'),
        (ts(4), 'Edit', 'a.py'), (ts(5), 'Edit', 'missing.py'), (ts(6), 'Edit', 'a.py')]
    assert [s.idx for s in st] == list(range(6))
    assert {s.session for s in st} == {'aaaaaaaa'}
    assert [s.agent for s in st] == ['main', 'executor · add b', 'executor · add b', 'main', 'main', 'main']
    assert [s.turn for s in st] == [0, 1, 1, 0, 0, 0]          # one number per (transcript, prompt), in order
    assert [s.said for s in st] == ['Main writes a', 'Sub adds b', '', '', '', '']

    # the subagent's bashEditDiff lands between the main session's Write and Edit
    assert st[2].content['a.py'] == 'x = 1\nw = 0\ny = 2\n' and (st[2].added, st[2].removed) == (1, 0)
    assert st[3].content['a.py'] == 'x = 1\nw = 0\ny = 3\n'

    # an edit that does not fit: a step with applied False, the snapshot as it was, a missing file not created
    assert [s.applied for s in st] == [True, True, True, True, False, False]
    assert [s.ok for s in st] == [True, True, True, True, True, False]
    assert st[4].content == st[3].content and 'missing.py' not in st[5].content
    assert (st[4].added, st[4].removed, st[5].added, st[5].removed) == (0, 0, 0, 0)
    assert st[5].content == {'a.py': 'x = 1\nw = 0\ny = 3\n', 'b.py': 'b = 1\n'}
    assert not any(s.seeded for s in st)

    # one transcript alone: no tags, its own turns
    alone = replay.steps(str(sub), ROOT)
    assert [(s.file, s.session, s.agent, s.turn, s.applied) for s in alone] == [
        ('b.py', '', '', 0, True), ('a.py', '', '', 0, False), ('a.py', '', '', 0, False)]   # no a.py to patch here

    assert replay.session_list(str(proj), st) == [{'id': 'aaaaaaaa', 'uuid': SID, 'start': ts(0), 'end': ts(6),
                                                    'steps': 6, 'prompt': 'build it'}]


def test_a_copied_call_counts_once_and_an_edit_rebases_on_original_file(tmp_path):
    first = msg('assistant', [tool('c1', 'Write', file_path=f'{ROOT}/a.py', content='a = 1\n')], ts=ts(1))
    edit = msg('assistant', [tool('c2', 'Edit', file_path=f'{ROOT}/a.py', old_string='a = 2', new_string='a = 3')], ts=ts(3))
    done = result('c2')
    done['toolUseResult'] = {'originalFile': 'a = 2\n'}        # the file had changed outside the transcripts
    p1 = write_lines(tmp_path / 'one.jsonl', [msg('user', 'go', ts=ts(0)), first])
    p2 = write_lines(tmp_path / 'two.jsonl', [msg('user', 'go on', ts=ts(2)), first, edit, done])
    st = replay.steps([p1, p2], ROOT)
    assert [(s.tool, s.session) for s in st] == [('Write', 'one'), ('Edit', 'two')]
    assert st[1].content['a.py'] == 'a = 3\n' and st[1].applied and st[1].seeded


def test_seed_git_bases_and_untouched_files(tmp_path):
    import subprocess
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'c.py').write_text('c = 1\n')
    (repo / 'd.py').write_text('d = 1\n')
    env = {'GIT_AUTHOR_DATE': '2025-12-31T00:00:00Z', 'GIT_COMMITTER_DATE': '2025-12-31T00:00:00Z',
           'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t',
           'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'}
    for cmd in (['init', '-q'], ['add', '.'], ['commit', '-qm', 'base']):
        subprocess.run(['git', '-C', str(repo), *cmd], check=True, env=env)
    p = write_lines(tmp_path / 's.jsonl', [
        msg('user', 'go', ts=ts(0)),
        msg('assistant', [tool('g1', 'Edit', file_path=f'{repo}/c.py', old_string='c = 1', new_string='c = 2')], ts=ts(1))])
    st = replay.steps(p, str(repo), seed_git=True)
    assert [(s.file, s.applied, s.seeded) for s in st] == [('c.py', True, True)]
    assert st[0].content == {'c.py': 'c = 2\n', 'd.py': 'd = 1\n'}
    assert (st[0].added, st[0].removed) == (1, 1)
    plain = replay.steps(p, str(repo))
    assert [(s.applied, s.seeded) for s in plain] == [(False, False)] and plain[0].content == {}


def test_git_reconcile_steps_reset_drift_delete_and_mark_empty(tmp_path):
    import subprocess
    repo, app = tmp_path / 'repo', tmp_path / 'repo' / 'app'
    app.mkdir(parents=True)

    def commit(at, msg_, files, gone=()):
        for name, body in files.items():
            (app / name).write_text(body)
        for name in gone:
            (app / name).unlink()
        env = {'GIT_AUTHOR_DATE': at, 'GIT_COMMITTER_DATE': at, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
               'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t', 'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'}
        for cmd in (['add', '-A'], ['commit', '-qm', msg_]):
            subprocess.run(['git', '-C', str(repo), *cmd], check=True, env=env)

    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    commit('2025-12-31T00:00:00Z', 'base', {'old.py': 'o\n'})                    # before the steps: no step
    commit('2026-01-01T01:02:00+01:00', 'drift', {'a.py': 'a = 1\n', 'c.py': 'c = 1\n'}, gone=['old.py'])
    commit(ts(4), 'same', {'c.py': 'c = 2\n'})
    commit(ts(6), 'after', {'a.py': 'a = 5\n', 'new.py': 'n\n'})              # after the last step: up to HEAD
    commit(ts(7), 'head', {'a.py': 'a = 7\n'}, gone=['new.py'])
    root = str(app)
    p = write_lines(tmp_path / 's.jsonl', [
        msg('user', 'go', ts=ts(0)),
        msg('assistant', [tool('w1', 'Write', file_path=f'{root}/a.py', content='a = 1\n'),
                          tool('w2', 'Write', file_path=f'{root}/c.py', content='c = 0\n'),
                          tool('w3', 'Write', file_path=f'{root}/old.py', content='o\n')], ts=ts(1)),
        msg('assistant', [tool('e1', 'Edit', file_path=f'{root}/c.py', old_string='c = 1', new_string='c = 2')], ts=ts(3)),
        msg('assistant', [tool('e2', 'Edit', file_path=f'{root}/a.py', old_string='a = 1', new_string='a = 5')], ts=ts(5))])

    st = replay.steps(p, root, git_reconcile=True)
    assert [(s.ts, s.tool, s.file, s.author) for s in st] == [
        (ts(1), 'Write', 'a.py', 'ai'), (ts(1), 'Write', 'c.py', 'ai'), (ts(1), 'Write', 'old.py', 'ai'),
        (ts(2), 'git', '', 'git'), (ts(3), 'Edit', 'c.py', 'ai'), (ts(4), 'git', '', 'git'),
        (ts(5), 'Edit', 'a.py', 'ai'), (ts(6), 'git', '', 'git'), (ts(7), 'git', '', 'git')]
    g = [s for s in st if s.author == 'git']
    assert {(s.session, s.agent) for s in st if s.author == 'ai'} == {('s', 'main')}   # one transcript, tagged
    assert [(s.said, s.session, s.agent, s.empty) for s in g] == [
        ('drift', 'git', 'git', False), ('same', 'git', 'git', True), ('after', 'git', 'git', False),
        ('head', 'git', 'git', False)]
    # the drifted c.py and the deleted old.py are the real diff; the matching commit is an empty marker
    assert st[3].content == {'a.py': 'a = 1\n', 'c.py': 'c = 1\n'} and (st[3].added, st[3].removed) == (1, 2)
    assert st[4].applied and st[4].content['c.py'] == 'c = 2\n'                  # applies on the committed text
    assert (st[5].added, st[5].removed) == (0, 0)
    assert st[7].content == {'a.py': 'a = 5\n', 'c.py': 'c = 2\n', 'new.py': 'n\n'} and (st[7].added, st[7].removed) == (1, 0)
    assert st[8].content == {'a.py': 'a = 7\n', 'c.py': 'c = 2\n'} and (st[8].added, st[8].removed) == (1, 2)  # == HEAD
    assert len({s.turn for s in g}) == 4 and not {s.turn for s in g} & {s.turn for s in st if s.author == 'ai'}

    plain = replay.steps(p, root)                                                  # without: the Edit misses
    assert [s.tool for s in plain] == ['Write', 'Write', 'Write', 'Edit', 'Edit'] and not plain[3].applied
    assert all(s.author == 'ai' and not s.empty for s in plain)


def test_original_transcript_wins_over_a_resumed_copy(tmp_path):
    import os
    proj = tmp_path / 'proj'
    call = msg('assistant', [tool('k1', 'Write', file_path=f'{ROOT}/a.py', content='a\n')], ts=ts(2))
    orig = write_lines(proj / 'bbbbbbbb-orig.jsonl', [msg('user', 'go', ts=ts(0)), call, msg('user', 'later', ts=ts(9))])
    copy = write_lines(proj / 'aaaaaaaa-copy.jsonl', [call, msg('user', 'resumed', ts=ts(5))])   # starts with copies
    os.utime(orig, (2_000_000_000, 2_000_000_000))      # the original was written to last
    os.utime(copy, (1_000_000_000, 1_000_000_000))
    assert replay.transcripts(str(proj)) == [orig, copy]
    assert [(s.file, s.session) for s in replay.steps(str(proj))] == [('a.py', 'bbbbbbbb')]


def test_a_change_made_inside_a_long_bash_call_is_credited_to_its_maker(tmp_path):
    proj = tmp_path / 'proj'
    hunk = {'oldStart': 1, 'lines': [' a = 1', '+b = 2']}
    write_lines(proj / f'{SID}.jsonl', [
        msg('user', 'go', ts=ts(0)),
        msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/a.py', content='a = 1\n')], ts=ts(0, 30)),
        msg('assistant', [tool('long', 'Bash', command='make test')], ts=ts(1)),        # runs from 1 to 4
        bash_result('long', ts(4), {'filePath': f'{ROOT}/a.py', 'hunks': [hunk]}, shared=True),
        msg('assistant', [tool('long2', 'Bash', command='sleep 300')], ts=ts(6)),       # runs from 6 to 9
        bash_result('long2', ts(9), {'filePath': f'{ROOT}/a.py',
                                     'hunks': [{'oldStart': 1, 'lines': [' a = 1', ' b = 2', '+c = 3']}]}, shared=True),
    ])
    write_lines(proj / SID / 'subagents' / 'agent-x.jsonl', [
        msg('user', 'add b', ts=ts(2), isSidechain=True),
        msg('assistant', [tool('short', 'Bash', command='python3 fix.py')], ts=ts(3), isSidechain=True),  # 3 to 5
        bash_result('short', ts(5), {'filePath': f'{ROOT}/a.py', 'hunks': [hunk]}),
        msg('assistant', [tool('e', 'Edit', file_path=f'{ROOT}/a.py', old_string='b = 2', new_string='b = 2\nc = 3')],
            ts=ts(7), isSidechain=True),
    ])
    st = replay.steps(str(proj))
    assert [(s.tool, s.agent, s.ts) for s in st] == [('Write', 'main', ts(0, 30)), ('Bash', 'subagent', ts(5)),
                                                     ('Edit', 'subagent', ts(7))]
    assert st[-1].content['a.py'] == 'a = 1\nb = 2\nc = 3\n'   # the long calls' reports of the same changes: no step


def test_apply_hunks_does_not_insert_again_where_the_new_lines_stand():
    assert replay.apply_hunks('a\nb\nq\na\nx\nb\n', [{'oldStart': 4, 'lines': [' a', '+x', ' b']}]) == 'a\nb\nq\na\nx\nb\n'
    assert replay.apply_hunks('a\nb\nq\na\nb\n', [{'oldStart': 4, 'lines': [' a', '+x', ' b']}]) == 'a\nb\nq\na\nx\nb\n'


def test_cd_tracking_for_heredoc_paths():
    assert replay.cds('(cd sub && make); cat > f') == []
    assert replay.cds('(cd sub && cat > f') == ['sub']
    assert replay.cds("cat > g <<'EOF'\ncd inner\nEOF\ncd /a && cat > f") == ['/a']
    assert replay.cds('cd - && cat > f') is None
    assert replay.cds('cd "my dir" && cat > f') == ['my dir']
    assert replay.cds('echo "cd x"; echo cd y; cat > f') == []
    assert replay.cds('mkdir -p /x/y && cd /x/y && cd z; cat > f') == ['/x/y', 'z']


def test_errored_edits_change_nothing_and_structured_patch_rescues_a_drifted_edit(tmp_path):
    bad_w, bad_e = result('w2', error=True), result('e1', error=True)
    fix = result('e2')
    fix['toolUseResult'] = {'originalFile': None,
                            'structuredPatch': [{'oldStart': 2, 'lines': [' x = 1', '-y = 20', '+y = 21', ' z = 3']}]}
    p = write_lines(tmp_path / 's.jsonl', [
        msg('user', 'go', ts=ts(0)),
        msg('assistant', [tool('w1', 'Write', file_path=f'{ROOT}/a.py', content='x = 1\ny = 2\nz = 3\n')], ts=ts(1)),
        msg('assistant', [tool('w2', 'Write', file_path=f'{ROOT}/a.py', content='broken\n')], ts=ts(2)), bad_w,
        msg('assistant', [tool('e1', 'Edit', file_path=f'{ROOT}/a.py', old_string='x = 1', new_string='x = 9')], ts=ts(3)), bad_e,
        # the file had y = 20 (a change the transcript never recorded): old_string misses, the recorded hunk fits
        msg('assistant', [tool('e2', 'Edit', file_path=f'{ROOT}/a.py', old_string='y = 20', new_string='y = 21')], ts=ts(4)), fix,
    ])
    st = replay.steps(p)
    assert [(s.ok, s.applied) for s in st] == [(True, True), (False, False), (False, False), (True, True)]
    assert st[2].content['a.py'] == 'x = 1\ny = 2\nz = 3\n'
    assert st[3].content['a.py'] == 'x = 1\ny = 21\nz = 3\n'


def test_a_retry_on_an_older_text_must_keep_the_later_changes(tmp_path):
    def run(hunks):
        p = write_lines(tmp_path / 'r.jsonl', [
            msg('user', 'go', ts=ts(0)),
            msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/f', content='a\nb\nc\nd\ne\nf\n')], ts=ts(1)),
            msg('assistant', [tool('b1', 'Bash', command='sed -i s/c/C/ f')], ts=ts(2)),
            bash_result('b1', ts(2, 5), {'filePath': f'{ROOT}/f', 'hunks': [{'oldStart': 3, 'lines': ['-c', '+C']}]}),
            msg('assistant', [tool('b2', 'Bash', command='python3 x.py')], ts=ts(3)),
            bash_result('b2', ts(3, 5), {'filePath': f'{ROOT}/f', 'hunks': hunks})])   # diffed against the old text
        return replay.steps(p)[-1]
    lost = run([{'oldStart': 3, 'lines': ['-c', '+X']}])                    # would drop the recorded c -> C
    assert not lost.applied and lost.content['f'] == 'a\nb\nC\nd\ne\nf\n'
    kept = run([{'oldStart': 3, 'lines': [' c', ' d', '-e', '+E']}])        # apart from c -> C: merged in
    assert kept.applied and kept.content['f'] == 'a\nb\nC\nd\nE\nf\n'
    near = run([{'oldStart': 3, 'lines': ['-c', '-d', '+C', '+D']}])        # touches c -> C: a diff3 conflict
    assert not near.applied and near.content['f'] == 'a\nb\nC\nd\ne\nf\n'


def test_timeline_writes_author_only_when_steps_are_tagged(tmp_path):
    import timeline
    proj, sub = two_transcripts(tmp_path)
    single = timeline.timeline(str(sub), ROOT, timings=False)
    merged = timeline.timeline(str(proj), ROOT, timings=False)
    assert not any('author' in s or 'session' in s for s in single['steps'])
    assert all(s['author'] == 'ai' and s['session'] == 'aaaaaaaa' for s in merged['steps'])


def test_every_step_of_one_call_says_the_text_before_it(tmp_path):
    cmd = "cat > a.html <<'EOF'\na\nEOF\ncat > b.html <<'EOF'\nb\nEOF\n"
    p = write_lines(tmp_path / 's.jsonl', [
        msg('user', 'go', ts=ts(0)),
        msg('assistant', [text('Now the templates.'), tool('h', 'Bash', command=cmd)], ts=ts(1)),
        msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/c.py', content='c\n')], ts=ts(2))])
    assert [(s.file, s.said) for s in replay.steps(p)] == [
        ('a.html', 'Now the templates.'), ('b.html', 'Now the templates.'), ('c.py', '')]


def test_records_with_a_non_iso_timestamp_are_skipped_for_order_and_session_times(tmp_path):
    proj = tmp_path / 'proj'
    a = write_lines(proj / 'aaaaaaaa-a.jsonl', [
        {'type': 'display', 'timestamp': 1761566036590},                     # integer ms, not ISO
        msg('user', 'go', ts=ts(5)),
        msg('assistant', [tool('w2', 'Write', file_path=f'{ROOT}/b.py', content='b\n')], ts=ts(6))])
    b = write_lines(proj / 'bbbbbbbb-b.jsonl', [
        msg('user', 'go', ts=ts(1)),
        msg('assistant', [tool('w1', 'Write', file_path=f'{ROOT}/a.py', content='a\n')], ts=ts(2))])
    assert replay.transcripts(str(proj)) == [b, a]
    st = replay.steps(str(proj), ROOT)
    assert [(s['id'], s['start']) for s in replay.session_list(str(proj), st)] == [('bbbbbbbb', ts(1)), ('aaaaaaaa', ts(5))]


def test_a_bash_report_that_only_saw_another_sessions_change_makes_no_step(tmp_path):
    proj = tmp_path / 'proj'
    seen = {'oldStart': 1, 'lines': [' a', ' NOT IN THE SNAPSHOT', '+x']}      # fits nowhere in the snapshot
    write_lines(proj / 'aaaaaaaa-maker.jsonl', [
        msg('user', 'go', ts=ts(0)),
        msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/a.py', content='a\nb\n')], ts=ts(0, 30)),
        msg('assistant', [text('Patching it'), tool('m', 'Bash', command='python3 patch.py')], ts=ts(2, 30)),  # 2:30 to 4
        bash_result('m', ts(4), {'filePath': f'{ROOT}/a.py', 'hunks': [seen]})])
    write_lines(proj / 'bbbbbbbb-watcher.jsonl', [
        msg('user', 'test it', ts=ts(0, 50)),
        msg('assistant', [text('Smoke test'), tool('o', 'Bash', command='make smoke')], ts=ts(1)),        # 1 to 3
        bash_result('o', ts(3), {'filePath': f'{ROOT}/a.py', 'hunks': [seen]})])
    st = replay.steps(str(proj), ROOT)
    assert [(s.session, s.applied) for s in st] == [('aaaaaaaa', True), ('aaaaaaaa', False)]


def test_a_stale_retry_must_keep_an_inserted_blank_line():
    assert replay.merge3('a\nb\nc\n', 'a\n\nb\nc\n', 'a\nB\nc\n') is None
    assert replay.merge3('a\nb\nc\nd\n', 'a\n\nb\nc\nd\n', 'a\nb\nc\nD\n') == 'a\n\nb\nc\nD\n'


def test_a_context_free_insertion_of_the_line_already_there_is_inserted():
    assert replay.apply_hunks('a\nb\nc\n', [{'oldStart': 1, 'lines': ['+a']}]) == 'a\na\nb\nc\n'


def test_cd_in_compound_commands_options_and_pipelines():
    assert replay.cds('if [ -d sub ]; then cd sub; fi; cat > k.py') == ['sub']
    assert replay.cds('{ cd a; }; cat > f') == ['a']
    assert replay.cds('cd -P dir && cat > f') == ['dir']
    assert replay.cds('cd -- dir; cat > f') == ['dir']
    assert replay.cds('cd b && cd a | cat; cat > f') == ['b']


def test_a_deduplicated_copy_spends_its_text_so_the_next_call_says_nothing(tmp_path):
    proj = tmp_path / 'proj'
    said = msg('assistant', [text('Writing a'), tool('k1', 'Write', file_path=f'{ROOT}/a.py', content='a\n')], ts=ts(2))
    write_lines(proj / 'aaaaaaaa-orig.jsonl', [msg('user', 'go', ts=ts(0)), said])
    write_lines(proj / 'bbbbbbbb-copy.jsonl', [msg('user', 'resumed', ts=ts(1)), said,                   # copies the call
                                               msg('user', 'next', ts=ts(5)),
                                               msg('assistant', [tool('k2', 'Write', file_path=f'{ROOT}/b.py', content='b\n')], ts=ts(6))])
    assert [(s.file, s.session, s.said) for s in replay.steps(str(proj), ROOT)] == [
        ('a.py', 'aaaaaaaa', 'Writing a'), ('b.py', 'bbbbbbbb', '')]


def test_a_resumed_copy_of_the_very_first_record_still_comes_after_its_original(tmp_path):
    proj = tmp_path / 'proj'
    first = [msg('user', 'go', ts=ts(1)), msg('assistant', [tool('t1', 'Write', file_path=f'{ROOT}/a.py', content='a\n')], ts=ts(2))]
    orig = write_lines(proj / 'bbbbbbbb-orig.jsonl', first)
    copy = write_lines(proj / 'aaaaaaaa-copy.jsonl', first + [
        msg('user', 'more', ts=ts(10)),
        msg('assistant', [tool('t2', 'Edit', file_path=f'{ROOT}/a.py', old_string='a', new_string='b')], ts=ts(11))])
    assert replay.transcripts(str(proj)) == [orig, copy]
    assert [s.session for s in replay.steps(str(proj), ROOT)] == ['bbbbbbbb', 'aaaaaaaa']


def test_a_bash_call_that_never_points_at_root_or_the_file_makes_no_step_but_shows_the_file(tmp_path):
    def run(cwd, cmd):
        p = write_lines(tmp_path / 's.jsonl', [
            msg('user', 'go', ts=ts(0)),
            msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/run.sh', content='a\nBIN=x\nc\n')], ts=ts(1)),
            msg('assistant', [tool('o', 'Bash', command=cmd)], ts=ts(2), cwd=cwd),
            bash_result('o', ts(3), {'filePath': f'{ROOT}/run.sh', 'hunks': [{'oldStart': 1, 'lines': [' a', '-BIN=x', '+BIN=y', ' c']}]}),
            # a later report diffed against the text the first one showed
            msg('assistant', [tool('l', 'Bash', command='sed -i s/c/C/ run.sh')], ts=ts(4)),
            bash_result('l', ts(5), {'filePath': f'{ROOT}/run.sh', 'hunks': [{'oldStart': 2, 'lines': [' BIN=y', '-c', '+C']}]})])
        return replay.steps(p, ROOT)
    blind = run('/home/u/other', 'cd /home/u/other && python3 -m pytest tests')     # another project's tests
    assert [(s.tool, s.applied, s.seeded) for s in blind] == [('Write', True, False), ('Bash', True, True)]
    assert blind[-1].content['run.sh'] == 'a\nBIN=y\nC\n'
    own = run(f'{ROOT}/tests', 'python3 fix.py')        # under root, a relative path: the call's own change
    assert [(s.tool, s.applied) for s in own] == [('Write', True), ('Bash', True), ('Bash', True)]


def test_a_call_naming_the_file_keeps_its_change_over_a_shorter_call_that_does_not(tmp_path):
    proj = tmp_path / 'proj'
    hunk = {'oldStart': 1, 'lines': [' a', '-b', '+B']}
    write_lines(proj / 'aaaaaaaa-watch.jsonl', [
        msg('user', 'test', ts=ts(0)),
        msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/m.c', content='a\nb\n')], ts=ts(0, 30)),
        msg('assistant', [tool('o', 'Bash', command='make check')], ts=ts(1)),            # 1:00 to 2:00
        bash_result('o', ts(2), {'filePath': f'{ROOT}/m.c', 'hunks': [hunk]})])
    write_lines(proj / 'bbbbbbbb-make.jsonl', [
        msg('user', 'fix', ts=ts(0, 50)),
        msg('assistant', [tool('x', 'Bash', command="python3 - <<'EOF'\np='m.c'\nEOF\nmake")], ts=ts(1, 30)),   # 1:30 to 4:00
        bash_result('x', ts(4), {'filePath': f'{ROOT}/m.c', 'hunks': [hunk]})])
    st = replay.steps(str(proj), ROOT)
    assert [(s.session, s.ts) for s in st] == [('aaaaaaaa', ts(0, 30)), ('bbbbbbbb', ts(4))]
    assert st[-1].content['m.c'] == 'a\nB\n'


def test_cd_in_case_background_prefixed_and_function_bodies():
    assert replay.cds('cd a; case x in x) ls;; esac; cat > f') == ['a']
    assert replay.cds('cd a & cat > f') == []
    assert replay.cds('cd a 2>&1 && cat > f') == ['a']
    assert replay.cds('x=1 cd zz; builtin cd a; command cd b') == ['zz', 'a', 'b']
    assert replay.cds('f() { cd a; }; function g { cd b; }; cat > f') == []


def test_a_basename_named_from_another_repo_does_not_name_the_file(tmp_path):
    proj = tmp_path / 'proj'
    hunk = {'oldStart': 1, 'lines': [' a', '-b', '+B', ' c']}
    write_lines(proj / '00000000-base.jsonl', [
        msg('user', 'go', ts=ts(0)),
        msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/README.md', content='a\nb\nc\n')], ts=ts(0, 10))])
    write_lines(proj / 'aaaaaaaa-other.jsonl', [                 # its own README.md, in another repo
        msg('user', 'docs', ts=ts(0, 50)),
        msg('assistant', [tool('o', 'Bash', command="sed -i 's/foo/bar/' README.md && ./slow-tests")], ts=ts(1), cwd='/work/other'),
        bash_result('o', ts(5), {'filePath': f'{ROOT}/README.md', 'hunks': [hunk]})])
    write_lines(proj / 'bbbbbbbb-app.jsonl', [
        msg('user', 'docs', ts=ts(0, 50)),
        msg('assistant', [tool('m', 'Bash', command='make docs')], ts=ts(2)),
        bash_result('m', ts(3), {'filePath': f'{ROOT}/README.md', 'hunks': [hunk]})])
    st = replay.steps(str(proj), ROOT)
    assert [(s.session, s.ts) for s in st] == [('00000000', ts(0, 10)), ('bbbbbbbb', ts(3))]


def _stale_seen(tmp_path, shown, start='a\nb\nc\nd\ne\n', old='e', new='E'):
    """f.py a..e; `shown` (a blind report or a Read) shows b -> B; then an Edit e -> E; then a context-free
    report B -> BB that fits only the shown text."""
    return write_lines(tmp_path / 's.jsonl', [
        msg('user', 'go', ts=ts(0)),
        msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/f.py', content=start)], ts=ts(1)),
        *shown,
        msg('assistant', [tool('e', 'Edit', file_path=f'{ROOT}/f.py', old_string=old, new_string=new)], ts=ts(4)),
        msg('assistant', [tool('y', 'Bash', command='sed -i s/B/BB/ f.py')], ts=ts(5)),
        bash_result('y', ts(6), {'filePath': f'{ROOT}/f.py', 'hunks': [{'oldStart': 2, 'lines': ['-B', '+BB']}]})])


def test_a_blind_reports_text_does_not_revert_a_later_change(tmp_path):
    p = _stale_seen(tmp_path, [
        msg('assistant', [tool('o', 'Bash', command='ls')], ts=ts(2), cwd='/work/other'),
        bash_result('o', ts(3), {'filePath': f'{ROOT}/f.py', 'hunks': [{'oldStart': 2, 'lines': ['-b', '+B']}]})])
    last = replay.steps(p, ROOT)[-1]
    assert last.applied and last.seeded and last.content['f.py'] == 'a\nBB\nc\nd\nE\n'     # e -> E merged in


def test_a_reads_text_does_not_revert_a_later_change(tmp_path):
    read = result('r')
    read['toolUseResult'] = {'type': 'text', 'file': {'filePath': f'{ROOT}/f.py', 'content': 'a\nB\nc\nd\ne',
                                                      'startLine': 1, 'numLines': 5, 'totalLines': 5}}
    read['timestamp'] = ts(3)
    p = _stale_seen(tmp_path, [msg('assistant', [tool('r', 'Read', file_path=f'{ROOT}/f.py')], ts=ts(2)), read])
    last = replay.steps(p, ROOT)[-1]
    assert last.applied and last.seeded and last.content['f.py'] == 'a\nBB\nc\nd\nE\n'


def test_cd_before_an_and_redirect_lasts():
    assert replay.cds('cd a &>/dev/null; cat > f') == ['a']


def test_cd_in_a_case_arm_makes_the_directory_unknown():
    assert replay.cds('case x in a) cd A;; b) cd B;; esac; cat > f') is None


def _read(text, at):
    o = result('r')
    o['toolUseResult'] = {'type': 'text', 'file': {'filePath': f'{ROOT}/f.py', 'content': text.rstrip('\n'),
                                                   'startLine': 1, 'numLines': text.count('\n'), 'totalLines': text.count('\n')}}
    o['timestamp'] = at
    return [msg('assistant', [tool('r', 'Read', file_path=f'{ROOT}/f.py')], ts=ts(2)), o]


def test_a_reads_text_does_not_bring_back_a_deleted_last_line(tmp_path):
    p = _stale_seen(tmp_path, _read('a\nB\nc\nd\ne\n', ts(3)), old='d\ne\n', new='d\n')
    last = replay.steps(p, ROOT)[-1]
    assert last.content['f.py'] == 'a\nBB\nc\nd\n'


def test_a_reads_text_does_not_drop_an_inserted_blank_line(tmp_path):
    p = _stale_seen(tmp_path, _read('a\nB\nc\n\nd\ne\n', ts(3)), start='a\nb\nc\n\nd\ne\n', old='d\ne\n', new='d\n\ne\n')
    last = replay.steps(p, ROOT)[-1]
    assert last.content['f.py'] == 'a\nBB\nc\n\nd\n\ne\n'


def test_a_retry_on_an_older_text_does_not_drop_an_inserted_blank_line(tmp_path):
    p = write_lines(tmp_path / 's.jsonl', [
        msg('user', 'go', ts=ts(0)),
        msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/f.py', content='a\n\nb\nc\nd\n')], ts=ts(1)),
        msg('assistant', [tool('e', 'Edit', file_path=f'{ROOT}/f.py', old_string='c\nd', new_string='c\n\nd')], ts=ts(2)),
        msg('assistant', [tool('y', 'Bash', command='sed -i s/d/D/ f.py')], ts=ts(3)),
        bash_result('y', ts(4), {'filePath': f'{ROOT}/f.py', 'hunks': [{'oldStart': 4, 'lines': [' c', '-d', '+D']}]})])
    last = replay.steps(p, ROOT)[-1]       # diffed against the Write's text: merging it would cut the blank line
    assert not last.applied and last.content['f.py'] == 'a\n\nb\nc\n\nd\n'


def test_a_name_with_a_directory_names_only_the_file_it_resolves_to():
    rm = f'{ROOT}/README.md'
    rank = lambda cmd, cwd=ROOT: replay._rank({'cmd': cmd, 'path': rm, 'cwd': cwd}, ROOT)
    assert rank("sed -i s/a/b/ docs/README.md") == (False, True)
    assert rank("sed -i s/a/b/ README.md") == (True, True)
    assert rank("sed -i s/a/b/ ../README.md", f'{ROOT}/docs') == (True, True)
    assert rank("cd docs && sed -i s/a/b/ ../README.md") == (True, True)


def test_an_absolute_path_names_the_file_only_as_a_whole_word():
    rank = lambda cmd: replay._rank({'cmd': cmd, 'path': f'{ROOT}/f.py', 'cwd': '/work/other'}, ROOT)
    assert rank(f'cp {ROOT}/f.py.bak /tmp/x') == (False, True)
    assert rank(f'sed -i s/a/b/ {ROOT}/f.py') == (True, True)


def test_cd_before_a_spaced_background_and_is_a_job():
    assert replay.cds('cd a & >x') == []
    assert replay.cds('cd a &>x; cat > f') == ['a']


def test_merge3_is_gits_clean_three_way_merge():
    m = replay.merge3
    assert m('a\ny\n', 'a\nb\n', 'a\ny\nb\n') is None                   # replace against an insert beside it
    assert m('b\na\n', '\na\nb\n', 'b\n') is None                       # a move against a delete
    assert m('    }\n    }\n    }\n', '    }\n    }\n', '    }\n    log();\n    }\n    }\n') == '    }\n    log();\n    }\n'
    assert m('a\nb\nc\nd\ne\n', 'a\nb\nc\nd\nE\n', 'a\nBB\nc\nd\ne\n') == 'a\nBB\nc\nd\nE\n'


def test_merge3_keeps_crlf_and_a_missing_last_newline():
    assert replay.merge3('a\r\nb\r\nc\r\n', 'A\r\nb\r\nc\r\n', 'a\r\nb\r\nC\r\n') == 'A\r\nb\r\nC\r\n'
    assert replay.merge3('a\nb\nc', 'A\nb\nc', 'a\nb\nC') == 'A\nb\nC'
    assert replay.merge3('a\né\nc\n', 'A\né\nc\n', 'a\né\nC\n') == 'A\né\nC\n'


def _retry(tmp_path, start, old, new, hunks):
    """A Write, an Edit, then a report diffed against the Write's text (so only the earlier-text retry fits)."""
    p = write_lines(tmp_path / 'h.jsonl', [
        msg('user', 'go', ts=ts(0)),
        msg('assistant', [tool('w', 'Write', file_path=f'{ROOT}/f.py', content=start)], ts=ts(1)),
        msg('assistant', [tool('e', 'Edit', file_path=f'{ROOT}/f.py', old_string=old, new_string=new)], ts=ts(2)),
        msg('assistant', [tool('y', 'Bash', command='python3 fix.py f.py')], ts=ts(3)),
        bash_result('y', ts(4), {'filePath': f'{ROOT}/f.py', 'hunks': hunks})])
    return replay.steps(p, ROOT)[-1]


def test_a_retry_that_conflicts_with_a_later_edit_is_not_applied(tmp_path):
    h1 = _retry(tmp_path, 'a\ny\nz\nq\n', 'y', 'b', [{'oldStart': 2, 'lines': ['-y', '+y', '+b']}])
    assert not h1.applied and h1.content['f.py'] == 'a\nb\nz\nq\n'
    h1b = _retry(tmp_path, 'x = 1\nfoo()\n', 'x = 1', 'x = 2', [{'oldStart': 1, 'lines': ['-x = 1', '+x = 1', '+x = 2']}])
    assert not h1b.applied and h1b.content['f.py'] == 'x = 2\nfoo()\n'
    h4 = _retry(tmp_path, 'if (x) {\n}\n\n', 'if (x) {\n}\n\n', 'if (x) {\nif (x) {\n}\n',
                [{'oldStart': 1, 'lines': [' if (x) {', '+if (x) {', '+ZZ', ' }', ' ', '-']}])
    assert not h4.applied and h4.content['f.py'] == 'if (x) {\nif (x) {\n}\n'


def test_a_retry_keeps_a_later_deletion_of_a_repeated_line(tmp_path):
    h3 = _retry(tmp_path, '    }\n    }\n    }\n', '    }\n    }\n    }', '    }\n    }',
                [{'oldStart': 1, 'lines': ['     }', '+    log();', '     }', '     }', ' ']}])
    assert h3.applied and h3.content['f.py'] == '    }\n    log();\n    }\n'
