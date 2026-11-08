"""label.py --per-step against a fake llama-server (no GPU): the step walk, resume, --retry-failed,
the run log and its report, the thermal pause."""
import http.server
import json
import re
import sys
import threading

import pytest

import label

LOG_KEYS = {'step', 'idx', 'session', 'keys', 'blobs', 'asked', 'hits', 'reused', 'calls', 'secs', 'pause_s',
            'temp_start', 'temp_end', 'status', 'error', 'at'}


class Fake:
    """Answers each chat request with a label titled after the blob; names in `fail` get a 500."""

    def __init__(self):
        self.asked = []          # blob names, one per chat request
        self.fail = set()
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.reply(200, {'status': 'ok'})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                name = re.search(r'^Blob: (\S+)', body['messages'][1]['content'], re.M).group(1)
                fake.asked.append(name)
                if name in fake.fail:
                    return self.reply(500, {'error': 'boom'})
                label_ = {'emoji': 'x', 'title': f'{name.upper()} screen', 'summary': 's', 'risk': 'low: test'}
                self.reply(200, {'choices': [{'message': {'content': json.dumps(label_)}, 'finish_reason': 'stop'}],
                                 'usage': {'prompt_tokens': 10, 'completion_tokens': 5}})

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.httpd.shutdown()


@pytest.fixture(autouse=True)
def no_gpu(monkeypatch):
    monkeypatch.setattr(label, 'gpu_temp', lambda: None)
    monkeypatch.setattr(label.time, 'sleep', lambda s: None)


def blob(id_, h):
    return {'id': id_, 'name': id_, 'kind': 'route', 'module': 'm',
            'funcs': [{'qual': id_, 'name': id_, 'hash': h, 'lines': 3}]}


def step(i, *blobs):
    return {'idx': i, 'session': 's1', 'blobs': list(blobs), 'edges': []}


# step 0: a, b new; 1: b changed; 2: nothing changed; 3: c new; 4: b back to its step-0 version (cached)
STEPS = [step(0, blob('a', 'h1'), blob('b', 'h2')), step(1, blob('a', 'h1'), blob('b', 'h3')),
         step(2, blob('a', 'h1'), blob('b', 'h3')), step(3, blob('a', 'h1'), blob('b', 'h3'), blob('c', 'h4')),
         step(4, blob('a', 'h1'), blob('b', 'h2'), blob('c', 'h4'))]


@pytest.fixture
def paths(tmp_path):
    tl = tmp_path / 'tl.json'
    tl.write_text(json.dumps({'root': str(tmp_path), 'steps': STEPS}))
    return tl, tmp_path / 'labels.json', tmp_path / 'run.jsonl'


def run(monkeypatch, fake, paths, *extra):
    tl, cache, log = paths
    before = tl.read_text()
    monkeypatch.setattr(sys, 'argv', ['label.py', str(tl), '--per-step', '--server', fake.url,
                                      '--cache', str(cache), '--run-log', str(log), *extra])
    label.main()
    assert tl.read_text() == before          # per-step never writes the timeline
    return [json.loads(ln) for ln in log.read_text().splitlines()] if log.exists() else []


def test_step_walk_asks_each_new_or_changed_version_once(monkeypatch, fake, paths):
    recs = run(monkeypatch, fake, paths)
    assert fake.asked == ['a', 'b', 'b', 'c']
    assert [(r['step'], r['blobs'], r['asked'], r['hits'], r['calls'], r['status']) for r in recs] == [
        (0, 2, 2, 0, 2, 'ok'), (1, 1, 1, 0, 1, 'ok'), (3, 1, 1, 0, 1, 'ok')]   # 2: no change, 4: cached
    cache = json.loads(paths[1].read_text())
    assert set(cache) == {'h1', 'h2', 'h3', 'h4'} and cache['h4']['title'] == 'C screen'


def test_log_records_and_report(monkeypatch, fake, paths):
    recs = run(monkeypatch, fake, paths)
    for r in recs:
        assert set(r) == LOG_KEYS
        assert r['session'] == 's1' and r['error'] is None and r['pause_s'] == 0
        assert isinstance(r['secs'], float) and len(r['keys']) == 12
    report = label.run_report(str(paths[2]))
    assert '3 step runs over 3 steps, 3 ok, 0 failed' in report
    assert 'p50' in report and 'p90' in report and '4 model calls' in report


def test_rerun_skips_logged_and_cached_steps(monkeypatch, fake, paths):
    run(monkeypatch, fake, paths, '--steps', '0-1')
    assert fake.asked == ['a', 'b', 'b']
    recs = run(monkeypatch, fake, paths)               # resume: only step 3 is left
    assert fake.asked == ['a', 'b', 'b', 'c'] and [r['step'] for r in recs] == [0, 1, 3]
    run(monkeypatch, fake, paths)                      # all done: nothing asked, nothing logged
    assert len(fake.asked) == 4 and len(paths[2].read_text().splitlines()) == 3
    cache = json.loads(paths[1].read_text())          # logged ok skips even when the cache lost it
    del cache['h3']
    paths[1].write_text(json.dumps(cache))
    run(monkeypatch, fake, paths)
    assert len(fake.asked) == 4


def test_failed_step_is_retried_logged_and_run_goes_on(monkeypatch, fake, paths):
    fake.fail = {'c'}
    recs = run(monkeypatch, fake, paths, '--attempts', '3')
    assert fake.asked == ['a', 'b', 'b', 'c', 'c', 'c']
    assert [(r['step'], r['status']) for r in recs] == [(0, 'ok'), (1, 'ok'), (3, 'failed')]
    assert recs[2]['calls'] == 3 and 'c: HTTPError' in recs[2]['error'] and '500' in recs[2]['error']
    assert 'failed step 3: c: HTTPError' in label.run_report(str(paths[2]))
    fake.fail = set()
    recs = run(monkeypatch, fake, paths, '--retry-failed')
    assert fake.asked[6:] == ['c'] and [(r['step'], r['status']) for r in recs[3:]] == [(3, 'ok')]
    run(monkeypatch, fake, paths, '--retry-failed')     # nothing failed any more
    assert len(fake.asked) == 7


def test_failed_step_does_not_stop_the_next_ones(monkeypatch, fake, paths):
    fake.fail = {'a'}
    recs = run(monkeypatch, fake, paths, '--attempts', '1')
    assert [(r['step'], r['status']) for r in recs] == [(0, 'failed'), (1, 'ok'), (3, 'ok')]
    assert fake.asked == ['a', 'b', 'b', 'c']          # a 5xx blob error does not drop step 0's b


def test_hot_gpu_pauses_between_steps(monkeypatch, fake, paths):
    temps = iter([90, 88, 70] + [60] * 20)
    monkeypatch.setattr(label, 'gpu_temp', lambda: next(temps))
    recs = run(monkeypatch, fake, paths, '--hot', '85', '--cool', '75')
    assert recs[0]['pause_s'] == 10 and recs[0]['temp_start'] == 70 and recs[1]['pause_s'] == 0
    assert '10s in 1 thermal pauses' in label.run_report(str(paths[2]))


def test_step_range():
    assert list(label.step_range('2-4', 10)) == [2, 3, 4]
    assert list(label.step_range('7', 10)) == [7]
    assert list(label.step_range('8-', 10)) == [8, 9]
    assert list(label.step_range('-1', 10)) == [0, 1]
    assert list(label.step_range('5-99', 7)) == [5, 6]


def test_prompt_budget_drops_snippets_then_functions():
    b = {'name': 'big', 'kind': 'hub', 'module': 'm',
         'funcs': [{'name': f'f{i}', 'lines': 9, 'snippet': ['x = 1'] * 5} for i in range(100)]}
    assert label.blob_prompt(b, 10**6) == label.blob_prompt(b)
    cut = label.blob_prompt(b, 400)
    assert len(cut) <= 400 and '| x = 1' not in cut and cut.endswith('more functions')


# --- --relabel-at: keep a blob's label until the lines changed since it was made reach 20% of its lines then ---

def fn(qual, h, lines, added=0, removed=0):
    return {'qual': qual, 'name': qual, 'hash': h, 'lines': lines, 'delta': {'added': added, 'removed': removed}}


def ublob(id_, *funcs):
    return {'id': id_, 'name': id_, 'kind': 'route', 'module': 'm', 'funcs': list(funcs)}


def run_steps(monkeypatch, fake, tmp_path, steps, *extra):
    tl, cache, log = tmp_path / 'tl.json', tmp_path / 'labels.json', tmp_path / 'run.jsonl'
    if not tl.exists():
        tl.write_text(json.dumps({'root': str(tmp_path), 'steps': steps}))
    recs = run(monkeypatch, fake, (tl, cache, log), *extra)
    return json.loads(cache.read_text()), recs


# a: 100 lines. 1: 10 lines changed (10% since the label: kept); 2: 10 more (20% since the label: asked);
# 3: 6 lines (6% since step 2's label: kept)
EDITS = [step(0, ublob('a', fn('a.f', 'h1', 100))), step(1, ublob('a', fn('a.f', 'h2', 100, 5, 5))),
         step(2, ublob('a', fn('a.f', 'h3', 102, 8, 2))), step(3, ublob('a', fn('a.f', 'h4', 102, 3, 3)))]


def test_small_edit_keeps_the_label_without_a_call(monkeypatch, fake, tmp_path):
    cache, recs = run_steps(monkeypatch, fake, tmp_path, EDITS[:2])
    assert fake.asked == ['a']
    assert cache['h2'] == dict(cache['h1'], reused_from='h1')
    assert [(r['step'], r['asked'], r['reused'], r['calls']) for r in recs] == [(0, 1, 0, 1), (1, 0, 1, 0)]
    assert '1 blobs asked, 1 kept their label' in label.run_report(str(tmp_path / 'run.jsonl'))


def test_edits_add_up_since_the_label_until_a_relabel(monkeypatch, fake, tmp_path):
    cache, recs = run_steps(monkeypatch, fake, tmp_path, EDITS)
    assert fake.asked == ['a', 'a']                       # step 2: 10% since step 1, but 20% since the label
    assert 'reused_from' not in cache['h3']
    assert cache['h2']['reused_from'] == 'h1' and cache['h4']['reused_from'] == 'h3'
    assert [(r['step'], r['asked'], r['reused']) for r in recs] == [(0, 1, 0), (1, 0, 1), (2, 1, 0), (3, 0, 1)]


def test_added_or_removed_function_counts_its_whole_length(monkeypatch, fake, tmp_path):
    steps = [step(0, ublob('a', fn('a.f', 'h1', 100)), ublob('b', fn('b.f', 'k1', 80), fn('b.g', 'k2', 20)),
                  ublob('c', fn('c.f', 'm1', 100))),
             # a gains a 25-line function whose delta claims 1 line; b loses its 20-line g; c: 15 lines (kept)
             step(1, ublob('a', fn('a.f', 'h1', 100), fn('a.g', 'h2', 25, 1, 0)), ublob('b', fn('b.f', 'k1', 80)),
                  ublob('c', fn('c.f', 'm2', 100, 10, 5)))]
    cache, _ = run_steps(monkeypatch, fake, tmp_path, steps)
    assert fake.asked == ['a', 'b', 'c', 'a', 'b']
    assert cache['m2']['reused_from'] == 'm1'


def test_resumed_run_decides_as_an_uninterrupted_one(monkeypatch, fake, tmp_path):
    # the resume falls between step 1 and 2: both decisions there need the edits made before it
    steps = [step(0, ublob('a', fn('a.f', 'h1', 100)), ublob('b', fn('b.f', 'k1', 50))),
             step(1, ublob('a', fn('a.f', 'h2', 100, 5, 5)), ublob('b', fn('b.f', 'k2', 50, 3, 2))),
             step(2, ublob('a', fn('a.f', 'h3', 102, 8, 2)), ublob('b', fn('b.f', 'k3', 50, 1, 1))),
             step(3, ublob('a', fn('a.f', 'h4', 102, 3, 3)), ublob('b', fn('b.f', 'k3', 50)), ublob('c', fn('c.f', 'm1', 9)))]
    (tmp_path / 'whole').mkdir()
    (tmp_path / 'resumed').mkdir()
    whole, _ = run_steps(monkeypatch, fake, tmp_path / 'whole', steps)
    asked_whole, fake.asked = fake.asked, []
    run_steps(monkeypatch, fake, tmp_path / 'resumed', steps, '--steps', '0-1')
    assert fake.asked == ['a', 'b']
    resumed, recs = run_steps(monkeypatch, fake, tmp_path / 'resumed', steps)
    assert fake.asked == ['a', 'b', 'a', 'c']   # step 2: a is at 20% since its step-0 label, b at 14% (kept)
    assert fake.asked == asked_whole and resumed == whole
    assert [r['step'] for r in recs] == [0, 1, 2, 3]


def test_relabel_at_0_asks_for_every_new_version(monkeypatch, fake, tmp_path):
    cache, recs = run_steps(monkeypatch, fake, tmp_path, EDITS, '--relabel-at', '0')
    assert fake.asked == ['a', 'a', 'a', 'a']
    assert set(cache) == {'h1', 'h2', 'h3', 'h4'} and not any('reused_from' in v for v in cache.values())
    assert [(r['asked'], r['hits'], r['reused']) for r in recs] == [(1, 0, 0)] * 4


def test_whole_timeline_mode_keeps_labels_and_shows_them_unmarked(monkeypatch, fake, tmp_path):
    tl, cache = tmp_path / 'tl.json', tmp_path / 'labels.json'
    tl.write_text(json.dumps({'root': str(tmp_path), 'steps': EDITS}))
    monkeypatch.setattr(sys, 'argv', ['label.py', str(tl), '--server', fake.url, '--cache', str(cache)])
    label.main()
    assert fake.asked == ['a', 'a']                       # h1 and h3; h2 and h4 keep theirs
    c = json.loads(cache.read_text())
    assert c['h2'] == dict(c['h1'], reused_from='h1') and c['h4'] == dict(c['h3'], reused_from='h3')
    shown = [s['blobs'][0]['label'] for s in json.loads(tl.read_text())['steps']]
    assert shown == [c['h1'], c['h1'], c['h3'], c['h3']]
    # no server (port 1): a new small edit still keeps the label from the cache
    tl.write_text(json.dumps({'root': str(tmp_path), 'steps': EDITS + [step(4, ublob('a', fn('a.f', 'h5', 102, 1, 1)))]}))
    monkeypatch.setattr(sys, 'argv', ['label.py', str(tl), '--server', 'http://127.0.0.1:1', '--cache', str(cache)])
    label.main()
    assert json.loads(cache.read_text())['h5'] == c['h4'] and len(fake.asked) == 2


def test_count_mode_compares_the_rule_with_relabel_at_0(monkeypatch, tmp_path, capsys):
    tl = tmp_path / 'tl.json'
    tl.write_text(json.dumps({'root': str(tmp_path), 'steps': EDITS}))
    monkeypatch.setattr(sys, 'argv', ['label.py', str(tl), '--count', '--cache', str(tmp_path / 'none.json')])
    label.main()
    out = capsys.readouterr().out
    assert '--relabel-at 0.2: 2 label requests (2 blobs, 0 pieces, 0 files), 2 keys reuse a label' in out
    assert '--relabel-at 0: 4 label requests' in out and 'saved 2 of 4 requests (50.0%)' in out
    assert not (tmp_path / 'none.json').exists()


def test_pieces_keep_their_own_label_and_folded_blobs_keep_theirs(monkeypatch, fake, tmp_path):
    def state(x, s):
        parent = ublob('P', fn('p.root', 'r1', 100))
        piece = dict(ublob('x', fn('p.x', x[0], 50, *x[1:])), id='P>p.x', cut='P')
        host = dict(ublob('H', fn('h.f', 'g1', 60), fn('s.f', s[0], 10, *s[1:])),
                    merged=[{'id': 'S', 'name': 'S', 'emoji': 'e', 'kind': 'route', 'lines': 10, 'funcs': ['s.f']}])
        return [parent, piece, host]
    steps = [step(0, *state(('x1',), ('s1',))),
             step(1, *state(('x2', 3, 2), ('s2', 1, 0))),   # piece 10% (parent 3%), folded S 10%: kept
             step(2, *state(('x2',), ('s3', 2, 0)))]        # S at 30%: needs a label, and a folded blob is never asked
    (tmp_path / 'labels.json').write_text(json.dumps({'s1': {'emoji': 'e', 'title': 'Small screen', 'summary': '',
                                                              'risk': 'low'}}))
    cache, _ = run_steps(monkeypatch, fake, tmp_path, steps)
    assert fake.asked == ['P', 'x', 'H']                  # the piece gets a label of its own
    assert cache['x2'] == dict(cache['x1'], reused_from='x1')
    assert cache['r1,x2'] == dict(cache['r1,x1'], reused_from='r1,x1')
    assert cache['s2'] == dict(cache['s1'], reused_from='s1') and 's3' not in cache


def test_folded_blob_is_not_asked_when_another_blob_is(monkeypatch, fake, tmp_path):
    host = dict(ublob('H', fn('h.f', 'g1', 60), fn('s.f', 's1', 10)),
                merged=[{'id': 'S', 'name': 'S', 'emoji': 'e', 'kind': 'route', 'lines': 10, 'funcs': ['s.f']}])
    cache, _ = run_steps(monkeypatch, fake, tmp_path, [step(0, host)])
    assert fake.asked == ['H'] and set(cache) == {'g1'}   # S (key s1) has no label and is never asked


def test_steps_before_the_range_replay_without_writing(monkeypatch, fake, tmp_path):
    (tmp_path / 'labels.json').write_text(json.dumps({'h1': {'emoji': 'e', 'title': 'A screen', 'summary': '',
                                                              'risk': 'low'}}))
    steps = [EDITS[0], EDITS[1], step(2, ublob('a', fn('a.f', 'h3', 100, 1, 1)))]
    cache, _ = run_steps(monkeypatch, fake, tmp_path, steps, '--steps', '2-2')
    assert 'h2' not in cache                              # step 1 is outside the range: nothing kept there
    assert fake.asked == ['a'] and 'reused_from' not in cache['h3']   # so a has no label to keep at step 2


def test_version_after_a_gap_counts_its_line_difference(monkeypatch, fake, tmp_path):
    steps = [step(0, ublob('a', fn('a.f', 'h1', 100)), ublob('b', fn('b.f', 'k1', 5))),
             step(1, ublob('b', fn('b.f', 'k1', 5))),
             step(2, ublob('a', fn('a.f', 'h2', 130, 2, 0)), ublob('b', fn('b.f', 'k1', 5)))]   # delta: vs step 1
    run_steps(monkeypatch, fake, tmp_path, steps)
    assert fake.asked == ['a', 'b', 'a']                  # 30 lines of 100 since the label, not the delta's 2


def test_labels_kept_on_steps_not_relabelled_are_saved(monkeypatch, fake, tmp_path):
    cache, _ = run_steps(monkeypatch, fake, tmp_path, EDITS[:2])
    del cache['h2']                                       # step 1 stays logged ok, its kept label is lost
    (tmp_path / 'labels.json').write_text(json.dumps(cache))
    cache, recs = run_steps(monkeypatch, fake, tmp_path, EDITS[:2])
    assert cache['h2'] == dict(cache['h1'], reused_from='h1') and fake.asked == ['a'] and len(recs) == 2


def test_whole_timeline_mode_keeps_nothing_from_a_failed_ask(monkeypatch, fake, tmp_path):
    fake.fail = {'a'}
    tl, cache = tmp_path / 'tl.json', tmp_path / 'labels.json'
    tl.write_text(json.dumps({'root': str(tmp_path), 'steps': EDITS}))
    monkeypatch.setattr(sys, 'argv', ['label.py', str(tl), '--server', fake.url, '--cache', str(cache)])
    label.main()
    assert fake.asked == ['a', 'a'] and json.loads(cache.read_text()) == {}
    assert not any('label' in s['blobs'][0] for s in json.loads(tl.read_text())['steps'])


@pytest.mark.parametrize('at', ['-0.1', '1.5'])
def test_relabel_at_outside_0_to_1_is_rejected(monkeypatch, tmp_path, capsys, at):
    monkeypatch.setattr(sys, 'argv', ['label.py', str(tmp_path / 'tl.json'), '--count', '--relabel-at', at])
    with pytest.raises(SystemExit):
        label.main()
    assert '--relabel-at must be between 0 and 1' in capsys.readouterr().err
