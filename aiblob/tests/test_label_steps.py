"""label.py --per-step against a fake llama-server (no GPU): the step walk, resume, --retry-failed,
the run log and its report, the thermal pause."""
import http.server
import json
import re
import sys
import threading

import pytest

import label

LOG_KEYS = {'step', 'idx', 'session', 'keys', 'blobs', 'asked', 'hits', 'calls', 'secs', 'pause_s',
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
    return {'id': id_, 'name': id_, 'kind': 'route', 'module': 'm', 'funcs': [{'name': id_, 'hash': h, 'lines': 3}]}


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
