"""A fake llama-server for the labelling tests: /health, /v1/chat/completions (a label made from the blob's name),
/tokenize, /props (is_sleeping). In a thread (FakeLlama: down() / up() on the same port, `loading` = 503, `latency` s, `fail` = the next N
completions answer `fail_code` (500), `log` of requests) or as a process (`python fakellama.py --port P --log FILE [--sleep-idle S]
[--load S]`, as liveserve starts a child: it logs start / sleep / wake / requests as JSON lines, and frees nothing but
says so: after S idle seconds it is `asleep` until the next completion)."""
import argparse, http.server, json, os, re, signal, sys, threading, time


def reply_for(body):
    """The completion for a chat request: a label named after the blob ("Login feature"), " variant" added on a
    follow-up (a rejected title)."""
    msgs = body.get('messages', [])
    user = next((m['content'] for m in msgs if m['role'] == 'user'), '')
    m = re.search(r'^Blob: (.*)$', user, re.M)
    words = re.findall(r'[A-Za-z]+', m.group(1) if m else '') or ['Part']
    title = ' '.join(w.capitalize() for w in words[-2:]) + ' feature'
    if len(msgs) > 2:
        title += ' variant'
    content = json.dumps({'emoji': '🧩', 'title': title, 'summary': 'a part of the app', 'risk': 'low: display'})
    return {'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': len(user) // 4, 'completion_tokens': 20}}, (m.group(1) if m else None)


class State:
    def __init__(self, log=None, sleep_idle=None, load=0.0):
        self.loading, self.latency, self.fail, self.fail_code, self.requests = False, 0.0, 0, 500, []
        self.log_path, self.sleep_idle, self.load_until = log, sleep_idle, time.time() + load
        self.last, self.asleep, self.lock = time.time(), False, threading.Lock()

    def note(self, **rec):
        rec['t'] = time.time()
        with self.lock:
            self.requests.append(rec)
            if self.log_path:
                with open(self.log_path, 'a', encoding='utf-8') as fh:
                    fh.write(json.dumps(rec) + '\n')

    def tick(self):
        if self.sleep_idle and not self.asleep and time.time() - self.last >= self.sleep_idle:
            self.asleep = True
            self.note(event='sleep')


def handler(state):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _loading(self):
            return state.loading or time.time() < state.load_until

        def do_GET(self):
            if self.path == '/props':                   # (llama-server's: whether it sleeps)
                state.tick()
                return self._json(200, {'is_sleeping': state.asleep})
            if self.path == '/health':
                state.note(event='health', loading=self._loading())
                return self._json(503, {'error': 'loading'}) if self._loading() else self._json(200, {'status': 'ok'})
            self._json(404, {})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
            if self.path == '/tokenize':
                return self._json(200, {'tokens': []})
            if self.path != '/v1/chat/completions':
                return self._json(404, {})
            if self._loading():
                return self._json(503, {'error': 'loading'})
            if state.asleep:
                state.asleep = False
                state.note(event='wake')
            state.last = time.time()
            time.sleep(state.latency)
            out, blob = reply_for(body)
            if state.fail > 0:
                state.fail -= 1
                state.note(event='completion', blob=blob, status=state.fail_code)
                return self._json(state.fail_code, {'error': 'fail'})
            state.note(event='completion', blob=blob, status=200, turns=len(body.get('messages', [])))
            state.last = time.time()
            self._json(200, out)
    return H


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class FakeLlama:
    """In a thread on 127.0.0.1:port (0: any); down() closes the socket (refused), up() serves again."""

    def __init__(self, port=0, **kw):
        self.state, self.port, self.srv = State(**kw), port, None
        self.up()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.port}'

    def up(self):
        self.srv = Server(('127.0.0.1', self.port), handler(self.state))
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()

    def down(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None

    def completions(self, status=200):
        return [r for r in self.state.requests if r.get('event') == 'completion' and r['status'] == status]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, required=True)
    ap.add_argument('--log')
    ap.add_argument('--sleep-idle', type=float)
    ap.add_argument('--load', type=float, default=0.0)
    ap.add_argument('--ignored', nargs='*', help='(flags a real llama-server takes, accepted and logged)')
    a, rest = ap.parse_known_args()
    state = State(a.log, a.sleep_idle, a.load)
    state.note(event='start', pid=os.getpid(), ppid=os.getppid(), pgid=os.getpgid(0), argv=sys.argv[1:] + rest)
    srv = Server(('127.0.0.1', a.port), handler(state))
    signal.signal(signal.SIGTERM, lambda *_: (state.note(event='sigterm'), os._exit(0)))
    threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
    while True:
        time.sleep(0.1)
        state.tick()


if __name__ == '__main__':
    main()
