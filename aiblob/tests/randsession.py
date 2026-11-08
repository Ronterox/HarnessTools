"""Random multi-transcript Claude Code sessions for differential tests (from the phase-1 review's fuzzer): 1-3
sessions with subagents and maybe a resumed copy; Writes, Edits (some failing, some with originalFile), MultiEdits,
heredocs, calls cut off, whole-file Reads and Bash reports from calls with different ranks and windows."""
import json, os

from test_replay import msg, tool, text, result, bash_result, write_lines, ROOT

def ts(sec, ms=0):
    m, s = divmod(int(sec), 60)
    return f'2026-01-01T{m // 60:02d}:{m % 60:02d}:{s:02d}.{ms:03d}Z'

FILES = ['a.py', 'b.py', 'sub/c.py']
TOK = ['x', 'y', 'z', 'w']
CMDS = [  # (command, cwd)
    ('python3 fix.py', ROOT), ('sed -i s/x/y/ a.py', ROOT), ('make all', ROOT), ('python3 fix.py b.py', '/other'),
    (f'python3 {ROOT}/a.py', '/other'), ('python3 g.py', '/other'), ('cd sub && sed -i s/x/y/ c.py', ROOT), ('ls', ROOT),
    ('python3 fix.py a.py b.py', ROOT)]

def random_project(rnd, tmp):
    """Seeded random multi-transcript sessions in tmp/proj -> (proj, root or None)."""
    proj = os.path.join(tmp, 'proj')
    nsess = rnd.randint(1, 3)
    for _f in FILES:            # draws the review's fuzzer made here (unused): a seed gives the same session
        for _ in range(rnd.randint(2, 5)):
            rnd.choice(TOK)
    sessions = []
    mid = [0]
    def newmid():
        mid[0] += 1
        return f'mid{mid[0]}'
    def build(name, sidechain, start):
        recs = [msg('user', f'prompt {name}', ts=ts(start), isSidechain=sidechain)]
        t = start + rnd.random() * 3
        for k in range(rnd.randint(3, 14)):
            t += rnd.random() * 12
            kind = rnd.choice(['write', 'edit', 'edit', 'heredoc', 'patch', 'patch', 'patch', 'cut', 'read', 'multi', 'prompt', 'text'])
            f = rnd.choice(FILES)
            cid = f'{name}-{k}'
            M = newmid()
            def asst(content, at):
                o = msg('assistant', content, ts=at, cwd=ROOT, isSidechain=sidechain)
                o['message']['id'] = M
                return o
            sayings = [text(f'say {cid}')] if rnd.random() < .5 else []
            if kind == 'write':
                recs.append(asst(sayings + [tool(cid, 'Write', file_path=f'{ROOT}/{f}', content=''.join(rnd.choice(TOK) + '\n' for _ in range(rnd.randint(1, 5))))], ts(t)))
                dur = rnd.random() * .5
                r = result(cid, error=rnd.random() < .1); r['timestamp'] = ts(t + dur); recs.append(r); t += dur
            elif kind in ('edit', 'multi'):
                if kind == 'edit':
                    c = [tool(cid, 'Edit', file_path=f'{ROOT}/{f}', old_string=rnd.choice(TOK), new_string=rnd.choice(TOK) + rnd.choice(TOK), replace_all=rnd.random() < .3)]
                else:
                    c = [tool(cid, 'MultiEdit', file_path=f'{ROOT}/{f}', edits=[{'old_string': rnd.choice(TOK), 'new_string': rnd.choice(TOK)} for _ in range(2)])]
                recs.append(asst(sayings + c, ts(t)))
                dur = rnd.random() * 2
                r = result(cid, error=rnd.random() < .1); r['timestamp'] = ts(t + dur)
                if rnd.random() < .4:
                    r['toolUseResult'] = {'originalFile': ''.join(rnd.choice(TOK) + '\n' for _ in range(3))}
                recs.append(r); t += dur
            elif kind == 'heredoc':
                cmd = f"cat > {f} <<'EOF'\n{rnd.choice(TOK)}\nEOF"
                recs.append(asst(sayings + [tool(cid, 'Bash', command=cmd)], ts(t)))
                dur = rnd.random() * 10
                r = result(cid); r['timestamp'] = ts(t + dur); recs.append(r); t += dur
            elif kind == 'cut':
                cmd = f"cat > {f} <<'EOF'\n{rnd.choice(TOK)}\nEOF\nsleep 600"
                recs.append(asst([tool(cid, 'Bash', command=cmd)], ts(t)))
                t += rnd.random()
                if rnd.random() < .5:
                    recs.append(msg('user', [text('[Request interrupted]')], ts=ts(t), isSidechain=sidechain))
            elif kind == 'read':
                recs.append(asst([tool(cid, 'Read', file_path=f'{ROOT}/{f}')], ts(t)))
                body = ''.join(rnd.choice(TOK) + '\n' for _ in range(3))
                r = result(cid); r['timestamp'] = ts(t + .2)
                r['toolUseResult'] = {'type': 'text', 'file': {'startLine': 1, 'numLines': 3, 'totalLines': 3, 'content': body.rstrip('\n'), 'filePath': f'{ROOT}/{f}'}}
                recs.append(r)
            elif kind == 'patch':
                cmd, cwd = rnd.choice(CMDS)
                a = asst(sayings + [tool(cid, 'Bash', command=cmd)], ts(t)); a['cwd'] = cwd; recs.append(a)
                dur = rnd.choice([.5, 2, 6, 15, 40])
                nf = rnd.choice([1, 1, 2])
                files = [{'filePath': f'{ROOT}/{rnd.choice(FILES)}', 'created': rnd.random() < .1, 'deleted': rnd.random() < .05,
                          'hunks': [{'oldStart': rnd.randint(1, 3), 'lines': [' ' + rnd.choice(TOK), rnd.choice(['+', '-']) + rnd.choice(TOK)]}]}
                         for _ in range(nf)]
                r = bash_result(cid, ts(t + dur), *files)
                recs.append(r); t += dur
            elif kind == 'prompt':
                recs.append(msg('user', f'more {k}', ts=ts(t), isSidechain=sidechain))
            else:
                recs.append(asst([text(f'just text {k}')], ts(t)))
        return recs
    start = 0
    for si in range(nsess):
        name = f'{si:x}' * 8
        start += rnd.random() * 20
        main = build(name, False, start)
        sessions.append((os.path.join(proj, f'{name}-0000-0000-0000-000000000000.jsonl'), main))
        for sj in range(rnd.randint(0, 2)):
            sub = build(f'{name[:2]}s{sj}', True, start + rnd.random() * 30)
            sessions.append((os.path.join(proj, f'{name}-0000-0000-0000-000000000000', 'subagents', f'agent-{sj}.jsonl'), sub))
    # a resumed copy of the first session's first records
    if rnd.random() < .5:
        base = sessions[0][1]
        k = rnd.randint(1, len(base))
        copy = list(base[:k]) + [msg('user', 'resumed', ts=ts(start + 100))]
        sessions.append((os.path.join(proj, 'ffffffff-0000-0000-0000-000000000000.jsonl'), copy))
    for path, recs in sessions:
        write_lines(__import__('pathlib').Path(path), recs)
        if 'subagents' in path:
            open(path[:-6] + '.meta.json', 'w').write(json.dumps({'agentType': 'executor', 'description': 'd'}))
    return proj, rnd.choice([None, ROOT])
