"""specificity.py against a fake llama-server (no GPU): ownership as split_entry measures it, distractors,
prompt chunking and parsing, the run end to end with its answer cache, the split preview."""
import http.server
import json
import re
import threading

import pytest

import label
import specificity as sp


def fn(qual, lines=5, calls=(), file=None, start=1):
    name = qual.rsplit('.', 1)[-1]
    return {'qual': qual, 'name': name, 'file': file or qual.rsplit('.', 1)[0].replace('.', '/') + '.c',
            'start': start, 'end': start + lines - 1, 'lines': lines, 'hash': qual,
            'calls': [{'to': c, 'error': False} for c in calls]}


def blob(id_, funcs, kind='command', module='src.m', title=None, summary='s'):
    b = {'id': id_, 'name': id_.rsplit('.', 1)[-1], 'kind': kind, 'module': module, 'funcs': funcs,
         'size': sum(f['lines'] for f in funcs)}
    b['label'] = {'emoji': 'x', 'title': title or f'{b["name"]} part', 'summary': summary, 'risk': 'low'}
    return b


def test_dominators_shared_callee_belongs_to_the_common_caller():
    succ = {'main': ['run'], 'run': ['a', 'b', 'c'], 'a': ['h'], 'b': ['h'], 'c': [], 'h': []}
    idom = sp.dominators(succ, succ, ['main'])
    assert idom == {'main': None, 'run': 'main', 'a': 'run', 'b': 'run', 'c': 'run', 'h': 'run'}


def test_dominators_unreached_cycle_gets_a_start():
    succ = {'x': ['y'], 'y': ['x'], 's': []}
    idom = sp.dominators(succ, succ, ['s'])
    assert idom['s'] is None and idom['x'] is None and idom['y'] == 'x'


def test_ownership_descends_past_a_majority_owner_like_split_entry():
    # main -> run (wrapper owning everything) -> {a (owns a1, a2), b (owns b1), c}; h shared by a and b
    funcs = [fn('m.main', calls=['m.run']), fn('m.run', calls=['m.a', 'm.b', 'm.c']),
             fn('m.a', calls=['m.a1', 'm.a2', 'm.h']), fn('m.a1'), fn('m.a2'),
             fn('m.b', calls=['m.b1', 'm.h']), fn('m.b1'), fn('m.c'), fn('m.h')]
    b = blob('m.main', funcs)
    succ, blob_of = sp.callgraph([b])
    own = sp.ownership(b, succ, blob_of)
    assert own['chain'] == ['m.main', 'm.run']
    assert sorted(own['cands']) == ['m.a', 'm.b', 'm.c']
    assert own['owned']['m.a'] == {'m.a', 'm.a1', 'm.a2'} and own['owned']['m.b'] == {'m.b', 'm.b1'}
    assert own['stays'] == {'m.main', 'm.run', 'm.h'}
    n = sp.numbers(b, succ, blob_of)
    assert (n['funcs'], n['lines'], n['split_depth']) == (9, 45, 2)
    assert n['top_share'] == round(3 / 9, 3) and n['sub_roots_10'] == 3 and n['sub_roots_20'] == 2
    assert n['call_density'] == round(9 / 72, 4) and n['calls_inside'] == 1.0


def test_helper_called_by_two_others_is_no_candidate():
    funcs = [fn('m.main', calls=['m.a', 'm.b', 'm.h']), fn('m.a', calls=['m.h']), fn('m.b', calls=['m.h']), fn('m.h')]
    b = blob('m.main', funcs)
    succ, blob_of = sp.callgraph([b])
    own = sp.ownership(b, succ, blob_of)
    assert sorted(own['cands']) == ['m.a', 'm.b'] and 'm.h' in own['stays']


def test_hub_starts_are_the_functions_other_blobs_call():
    hub = blob('hub:lib', [fn('lib.p', calls=['lib.q']), fn('lib.q'), fn('lib.r')], kind='hub', module='lib')
    user = blob('u.cmd', [fn('u.cmd', calls=['lib.q', 'lib.r'])])
    succ, blob_of = sp.callgraph([hub, user])
    assert sp.blob_starts(hub, succ, blob_of) == ['lib.p', 'lib.q', 'lib.r']


def test_distractors_prefer_linked_same_module_and_similar_names():
    me = blob('src.m.draw_tree', [fn('src.m.draw_tree'), fn('src.m.tree_node')])
    near = blob('src.m.draw_list', [fn('src.m.draw_list'), fn('src.m.tree_row')])
    linked = blob('src.k.other', [fn('src.k.other')], module='src.k')
    far = [blob(f'x.y{i}', [fn(f'x.y{i}')], kind='tests', module='x') for i in range(5)]
    got = sp.distractors(me, [me, near, linked] + far, [{'a': me['id'], 'b': linked['id'], 'kind': 'main', 'n': 1}])
    assert [g['id'] for g in got[:2]] == ['src.m.draw_list', 'src.k.other'] and len(got) == 4


def test_cov_chunks_fit_the_budget_and_keep_numbering():
    items = [f'func_{i} (5 lines): int func_{i}(void)' for i in range(40)]
    chunks = sp.cov_chunks(items, 300)
    assert len(chunks) > 1 and all(sum(len(c) + 1 for c in ch) <= 300 for ch in chunks)
    assert all([int(c.split('.', 1)[0]) for c in ch] == list(range(1, len(ch) + 1)) for ch in chunks)
    assert [c.split('. ', 1)[1] for ch in chunks for c in ch] == items


@pytest.mark.parametrize('text, want', [('{"answer": "C"}', 'C'), ('{"answer": "b) foo"}', 'B'),
                                        ('answer is D', 'D'), ('{"answer": "Z"}', None)])
def test_parse_choice(text, want):
    assert sp.parse_choice(text) == want


def test_parse_assign_keeps_valid_numbers_and_letters():
    assert sp.parse_assign('{"1": "B", "2": "c", "5": "Z", "99": "A"}', {1, 2, 5}) == {1: 'B', 2: 'C'}
    assert sp.parse_assign('{\n  "3": "A",\n  "4": "E', {3, 4}) == {3: 'A', 4: 'E'}   # cut short
    assert sp.parse_assign('{"1": "B", "1": "C"}', {1}) == {1: 'B'}


def test_coverage_counts_the_own_slot_and_where_the_rest_went():
    class M:
        def ask(self, system, user, max_tokens):
            self.user = user
            own = re.search(r'^([A-E])\) "Mine', user, re.M).group(1)
            other = re.search(r'^([A-E])\) "Theirs', user, re.M).group(1)
            return json.dumps({'1': own, '2': other})
    me = blob('m.a', [fn('m.a', 30), fn('m.b', 10)], title='Mine')
    them = [blob(f'm.o{i}', [fn(f'm.o{i}')], title='Theirs' if i == 0 else f'X{i}') for i in range(4)]
    got = sp.coverage(M(), me, me['label'], them, ['a (30 lines)', 'b (10 lines)'], 4000, 'seed')
    assert got == {'cov': 0.5, 'cov_lines': 0.75, 'uncovered': [['b', 'm.o0']], 'unanswered': 0, 'same_letter': 0.5}
    # one function per chunk: each chunk is numbered from 1 and mapped back to its function
    got = sp.coverage(M(), me, me['label'], them, ['a (30 lines)', 'b (10 lines)'], 20, 'seed')
    assert got == {'cov': 1.0, 'cov_lines': 1.0, 'uncovered': [], 'unanswered': 0, 'same_letter': 1.0}


def test_first_line_reads_the_definition(tmp_path):
    (tmp_path / 'a.c').write_text('static int\nfoo(int x) {\n  return x;\n}\nint bar(void) {\n}\n')
    files = {}
    assert sp.first_line(str(tmp_path), {'name': 'foo', 'file': 'a.c', 'start': 1}, files) == 'static int foo(int x)'
    assert sp.first_line(str(tmp_path), {'name': 'bar', 'file': 'a.c', 'start': 5}, files) == 'int bar(void)'
    assert sp.first_line(str(tmp_path), {'name': 'gone', 'file': 'a.c', 'start': 5}, files) == ''
    assert sp.first_line(str(tmp_path), {'name': 'x', 'file': 'nope.c', 'start': 1}, files) == ''


def test_split_preview_cuts_big_and_folds_tiny_into_its_call_neighbour():
    big = blob('m.main', [fn('m.main', 1, ['m.a', 'm.b']), fn('m.a', 40, ['m.a1']), fn('m.a1', 40),
                          fn('m.b', 20)])
    tiny = blob('m.t', [fn('m.t', 2)], module='src.t')
    other = blob('m.o', [fn('m.o', 30, ['m.t'])], module='src.o')
    test = blob('tests.t', [fn('tests.t.main', 30, ['m.t'])], kind='tests', module='tests.t')
    edges = [{'a': 'm.o', 'b': 'm.t', 'kind': 'main', 'n': 1}, {'a': 'm.main', 'b': 'm.t', 'kind': 'data', 'n': 3},
             {'a': 'tests.t', 'b': 'm.t', 'kind': 'main', 'n': 5}]
    blobs = [big, tiny, other, test]
    succ, blob_of = sp.callgraph(blobs)
    pv, before, after = sp.split_preview(blobs, edges, succ, blob_of, 100, 50, 5)
    (s,) = pv['splits']
    assert [(p['root'], p['lines'], p['funcs']) for p in s['pieces']] == [('m.a', 80, 2), ('m.b', 20, 1)]
    assert s['parent'] == {'funcs': 1, 'lines': 1}
    assert pv['folds'] == [{'id': 'm.t', 'label': 't part', 'lines': 2, 'into': 'm.o', 'into_label': 'o part',
                            'via': 'call'}]
    assert sorted(before) == [2, 30, 30, 101] and sorted(after) == [1, 20, 30, 32, 80]   # never into the tests blob
    pv, _, after = sp.split_preview(blobs, edges, succ, blob_of, 100, 50, 5, min_share=0.5)
    assert [p['root'] for p in pv['splits'][0]['pieces']] == ['m.a'] and sorted(after) == [21, 30, 32, 80]
    # a same-module sibling (a header's .c) wins over a stronger link elsewhere, the biggest one
    sib = blob('m.s', [fn('m.s', 8)], module='src.t_h')
    sib2 = blob('m.s2', [fn('m.s2', 40)], module='src.t')
    pv, _, _ = sp.split_preview(blobs + [sib, sib2], edges, succ, blob_of, 100, 50, 5)
    assert [(f['into'], f['via']) for f in pv['folds']] == [('m.s2', 'module')]


# --- end to end against a fake server ------------------------------------------------------------

class Fake:
    """Discrimination: picks the candidate whose functions contain the description's first word (lowercased).
    Coverage: gives each function the description whose first word its name contains, none for the others."""

    def __init__(self):
        self.asked = []
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.reply({'status': 'ok'})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                assert body['chat_template_kwargs'] == {'enable_thinking': False} and body['temperature'] == 0
                system, user = (m['content'] for m in body['messages'])
                fake.asked.append(user)
                word = re.search(r'"(\w+)', user).group(1).lower()
                if 'assign each function' in system:
                    descs = re.findall(r'^([A-E])\) "(\w+)', user, re.M)
                    out = {}
                    for n, name in re.findall(r'^(\d+)\. (\w+)', user, re.M):
                        hit = [l for l, w in descs if w.lower() in name]
                        if hit:
                            out[n] = hit[0]
                else:
                    cands = re.findall(r'^([A-E])\) (.*)$', user, re.M)
                    out = {'answer': next((l for l, d in cands if word in d), '?')}
                self.reply({'choices': [{'message': {'content': json.dumps(out)}, 'finish_reason': 'stop'}]})

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.httpd.shutdown()


@pytest.fixture
def temps(monkeypatch):
    seq = []   # GPU temperatures nvidia-smi will read, then None
    monkeypatch.setattr(label, 'gpu_temp', lambda: seq.pop(0) if seq else None)
    monkeypatch.setattr(label.time, 'sleep', lambda s: None)
    return seq


def world(tmp_path):
    names = ['alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf']
    blobs = []
    for i, n in enumerate(names):
        funcs = [fn(f'src.{n}.{n}_run', 10, [f'src.{n}.{n}_step'], file=f'src/{n}.c', start=1),
                 fn(f'src.{n}.{n}_step', 6, file=f'src/{n}.c', start=11)]
        if n == 'alpha':
            funcs.append(fn('src.alpha.zzz_unrelated', 4, file='src/alpha.c', start=17))
        blobs.append(blob(f'src.{n}.{n}_run', funcs, module=f'src.{n}', title=f'{n.capitalize()} screen'))
        (tmp_path / 'src').mkdir(exist_ok=True)
        (tmp_path / 'src' / f'{n}.c').write_text(
            f'void {n}_run(void) {{\n' + '\n' * 9 + f'static int {n}_step(int x) {{\n' + '\n' * 5 + 'int zzz_unrelated(void) {\n')
    blobs.append({'id': 'file:README.md', 'name': 'README.md', 'kind': 'text', 'module': '.', 'funcs': [], 'size': 3})
    edges = [{'a': blobs[0]['id'], 'b': blobs[1]['id'], 'kind': 'main', 'n': 1}]
    tl = tmp_path / 'tl.json'
    tl.write_text(json.dumps({'root': str(tmp_path), 'steps': [{'idx': 0, 'blobs': blobs, 'edges': edges}]}))
    return tl


def test_run_end_to_end_then_rerun_is_free(tmp_path, fake, temps, capsys):
    tl, out = world(tmp_path), tmp_path / 'out'
    temps += [90, 86, 79]   # hot before the first call: paused until below 80
    sp.main([str(tl), '--out', str(out), '--server', fake.url, '--tiny-lines', '17', '--split-lines', '20', '--split-funcs', '50'])
    rows = {r['id']: r for r in json.loads((out / 'blobs.json').read_text())}
    assert len(rows) == 7                                          # code blobs only
    assert all(r['disc'] == 1.0 for r in rows.values())
    assert all(r['disc_ctl'] == 0.0 for r in rows.values())          # another blob's label never picks this one
    a = rows['src.alpha.alpha_run']
    assert a['cov'] == pytest.approx(2 / 3) and a['cov_lines'] == pytest.approx(16 / 20)
    assert a['uncovered'] == [['zzz_unrelated', None]] and a['unanswered'] == 1
    assert rows['src.bravo.bravo_run']['cov'] == 1.0 and rows['src.bravo.bravo_run']['cov_ctl'] == 0.0
    assert (a['funcs'], a['lines'], a['sub_roots_20']) == (3, 20, 2)
    calls = len(fake.asked)
    assert calls == 7 * (2 + 1 + 1 + 1)                             # 2 shuffles, control, coverage, coverage control
    assert any('alpha_step (6 lines): static int alpha_step(int x)' in u for u in fake.asked)
    report = (out / 'report.txt').read_text()
    assert 'control (random other label) disc mean 0.000 (n=7' in report and 'thermal pauses' in report
    assert '10s thermal pauses' in report                         # two 5 s polls at 86 and 79 °C
    pv = json.loads((out / 'split_preview.json').read_text())
    assert pv['tiny_lines'] == 17 and pv['every']['splits'][0]['id'] == 'src.alpha.alpha_run'
    assert (out / 'blobs.csv').read_text().count('\n') == 8
    capsys.readouterr()

    sp.main([str(tl), '--out', str(out), '--server', fake.url, '--tiny-lines', '17', '--split-lines', '20'])
    assert len(fake.asked) == calls                                # every answer came from the cache
    assert 'model: 0 calls, 35 cache hits' in capsys.readouterr().out


def test_no_model_uses_the_cache_only(tmp_path):
    tl, out = world(tmp_path), tmp_path / 'out'
    sp.main([str(tl), '--out', str(out), '--no-model'])
    rows = json.loads((out / 'blobs.json').read_text())
    assert len(rows) == 7 and all(r['disc'] is None and r['cov'] is None and r['cov_ctl'] is None for r in rows)
    assert rows[0]['funcs'] == 3 and 'model: 0 calls, 0 cache hits' in (out / 'report.txt').read_text()


def test_control_strides(tmp_path, fake, temps):
    tl, out = world(tmp_path), tmp_path / 'out'
    sp.main([str(tl), '--out', str(out), '--server', fake.url, '--disc-control-every', '0', '--cov-control-every', '3'])
    rows = json.loads((out / 'blobs.json').read_text())
    assert all(r['disc_ctl'] is None for r in rows) and sum(r['cov_ctl'] is not None for r in rows) == 3
    assert len(fake.asked) == 7 * 3 + 3                           # 2 shuffles + coverage each, 3 coverage controls
