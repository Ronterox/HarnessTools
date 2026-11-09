"""The labelling worker of liveserve.py: label.py's model call for the blobs of new steps, one at a time, in a thread.

What to ask comes from the server's labeller (liveserve.Labeller.wants()): per blob identity changed on a new step, the
newest version that still lacks a label, as {key: Want}; a key leaves that set once the cache holds it (the server reads
every cache write back, ours too). A want is asked once it is quiet: QUIET s since its blob last changed, or its turn
ended (a later step of another turn or session was committed), or CAP s since it was first wanted, whichever comes
first. Once a blob's label comes back, both clocks start again from that answer: its next version is due QUIET s
after the later of its last change and that answer, or CAP s after the answer, whichever comes first. So a blob that
changes more often than every QUIET s is asked about once per CAP; one changing less often is asked once per change,
QUIET s after it (a blob edited every 25 s, every 25 s). Among the due ones, those shown at the step the page is on (POST /live/focus) go first, then the
newest. A burst of versions is one call: only the newest is wanted, and the 20% rule (label.py --relabel-at) gives the
label to the versions near it.

Each ask is label.ask() (titles of the step's other parts as context, rejected titles retried, the structural name as
the last resort) and its label is merged into the cache file at once (label.save_cache: lock, read again, merge,
rename). A failed ask is tried again 2 s, then 4 s later; after ATTEMPTS failures it waits BACKOFF (doubling to 10
min). A server that does not answer (refused, timed out, 503 while loading) is probed on /health after 15, 30, then
every 60 s, nothing asked meanwhile. At HOT degrees or more (nvidia-smi, read at most every 5 s) nothing is asked
until the GPU is below COOL. An error in a step of the worker shows on the chip ('error') and the next step goes on;
a worker thread that dies is started again by the server (liveserve.Live.watch_worker, 3 times, then 'error').

The llama-server (Llama): one the user runs at the configured URL is used as it is, never started nor stopped. Else
this process starts its own, only when a want is due and nvidia-smi shows VRAM_NEED MiB free and /proc/meminfo
RAM_NEED MiB available (read right then, not a reading kept from before; nothing is reserved before), on a free
port, with `-c 4096 -np 1 --cache-ram 0 --sleep-idle-seconds 60`: after 60 s without a request llama-server frees its
VRAM itself (3,098 -> 100 MiB measured) and a request wakes it in ~3 s. Waking takes those 3 GB back, so before an ask
to our own child that /props says is asleep (`is_sleeping`), free VRAM is read again and the ask waits (state down,
why 'vram', probed as above) while it is short. The child is in its own process group and gets SIGKILL when the
thread that started it (this worker's) ends, so it never outlives this process (PR_SET_PDEATHSIG, checked against a
parent that died before it was set). PR_SET_PDEATHSIG covers that process only: the default command runs llama-server
in it (the runtime's private glibc loader maps the program into its own process, no fork); a --llm-cmd that forks
(a shell wrapper) leaves its children to the stop below. It is stopped (SIGTERM to its whole process group, SIGKILL
to what is left of the group after 10 s, or 3 s once the child itself is gone) IDLE_STOP s after its last answer (or
started), whether or not asks wait meanwhile (a thermal pause, a backoff), when it exits on its own (what is left of
its group), and when the server stops. IDLE_STOP = 10 min: a sleeping child costs ~100 MiB of VRAM and its RAM, and
wakes in ~3 s where a new one loads the model again (measured 6.0 s on the 2026-11-09 RTX 2060 smoke, a page-cache read of 2.5 GB); the gaps between an agent's turns are mostly under 10 min, so a session at work keeps the cheap wake, one left
alone gives everything back.

Every ask and every change of state is a JSON line in the run log (demo/label-runs/<name>.live.jsonl)."""
import ctypes, json, os, signal, socket, subprocess, sys, threading, time, urllib.error, urllib.request
from collections import Counter

import label

QUIET, CAP = 20.0, 120.0            # s: a want is due once quiet this long, or wanted this long
HOT, COOL = 85, 80                  # °C: pause at HOT, resume below COOL
ATTEMPTS = 3                        # tries per ask before BACKOFF
RETRY = (2.0, 4.0)                  # s between those tries
BACKOFF = (60.0, 120.0, 300.0, 600.0)
PROBE = (15.0, 30.0, 60.0)          # s between /health probes of a server that does not answer
SMI_TTL = 5.0                       # s an nvidia-smi reading is kept
VRAM_NEED, RAM_NEED = 3300, 2048    # MiB free to start a child (Qwen3.5-4B Q4, -c 4096: 3,098 MiB of VRAM)
IDLE_STOP = 600.0                   # s with nothing wanted before the child is stopped
START_TIMEOUT = 180.0               # s for a child to answer /health
SLEEP_IDLE = 60                     # llama-server --sleep-idle-seconds
MODEL = os.path.expanduser('~/.cache/models/Qwen3.5-4B-Q4_K_M.gguf')
RUNTIMES = os.path.expanduser('~/.cache/remcli-runtimes')
PR_SET_PDEATHSIG = 1


def llama_cmd(runtime='b11960', model=MODEL, sleep=SLEEP_IDLE):
    """(argv with a {port} placeholder, cwd) for llama-server as serve-llm.sh --runtime starts it (its private glibc
    loader; CUDA is found only with cwd in its bin dir), with the live flags: one slot of 4096 tokens, no prompt cache
    in RAM, asleep after `sleep` s idle. runtime None: the system llama-server."""
    flags = ['-m', os.path.realpath(model), '--host', '127.0.0.1', '--port', '{port}', '-ngl', '99', '-c', '4096',
             '-np', '1', '--cache-ram', '0', '--sleep-idle-seconds', str(sleep), '-fa', 'on', '--reasoning', 'off']
    if not runtime:
        return ['llama-server'] + flags, None
    b = runtime.lstrip('b')
    libc = f'{RUNTIMES}/llama-b{b}/compat-libc/usr/lib/x86_64-linux-gnu'
    bin_dir = f'{RUNTIMES}/llama-b{b}/bin/llama-b{b}'
    cuda = f'{RUNTIMES}/prism-b12128-d7b2896/cuda12-libs/nvidia'
    return ([f'{libc}/ld-linux-x86-64.so.2', '--library-path',
             f'{libc}:{bin_dir}:{cuda}/cuda_runtime/lib:{cuda}/cublas/lib', './llama-server'] + flags, bin_dir)


def mem_available(path='/proc/meminfo'):
    """MemAvailable in MiB, None when unreadable."""
    try:
        with open(path) as fh:
            for line in fh:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def health(url, timeout=3.0):
    """'ok', 'loading' (503) or 'down' for a llama-server at url."""
    try:
        with urllib.request.urlopen(url + '/health', timeout=timeout) as r:
            return 'ok' if r.status == 200 else 'down'
    except urllib.error.HTTPError as e:
        return 'loading' if e.code == 503 else 'down'
    except (OSError, ValueError):
        return 'down'


def sleeping(url, timeout=3.0):
    """llama-server's /props says it is asleep (--sleep-idle-seconds: its model is out of VRAM until a request)."""
    try:
        with urllib.request.urlopen(url + '/props', timeout=timeout) as r:
            return json.load(r).get('is_sleeping') is True
    except (OSError, ValueError, AttributeError):
        return False


def is_down(e):
    """An error that says the server is not there (or still loading), not that this ask went wrong."""
    if isinstance(e, urllib.error.HTTPError):
        return e.code == 503
    return isinstance(e, (OSError, urllib.error.URLError))


class Gpu:
    """nvidia-smi's temperature (°C) and free memory (MiB) of the first GPU, read at most every SMI_TTL s."""

    def __init__(self, cmd=('nvidia-smi',), clock=time.monotonic, ttl=SMI_TTL):
        self.cmd, self.clock, self.ttl, self.at, self.last = list(cmd), clock, ttl, None, (None, None)

    def read(self, fresh=False):
        """(°C, MiB free); `fresh`: read now, whatever the age of the last reading."""
        now = self.clock()
        if fresh or self.at is None or now - self.at >= self.ttl:
            self.at, self.last = now, self._query()
        return self.last

    def _query(self):
        try:
            out = subprocess.run(self.cmd + ['--query-gpu=temperature.gpu,memory.free', '--format=csv,noheader,nounits'],
                                 capture_output=True, text=True, timeout=10).stdout
            temp, free = out.splitlines()[0].split(',')
            return int(temp), int(free)
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            return None, None


def _child_setup(parent):
    """preexec_fn: own process group, SIGKILL when the starting thread ends; a parent already gone: exit."""
    def setup():
        os.setpgid(0, 0)
        ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
        if os.getppid() != parent:
            os._exit(1)
    return setup


class Llama:
    """The llama-server labels come from: the user's at `url` when it answers there, else a child started on demand.
    ensure(now) -> (base url or None, state): 'external', 'child', 'starting', 'loading' (the user's is loading),
    'nomem' (not enough free VRAM / RAM to start one), 'failed' (the child exited or never answered)."""

    def __init__(self, url, cmd, cwd=None, gpu=None, meminfo='/proc/meminfo', log=None, clock=time.monotonic,
                 vram=VRAM_NEED, ram=RAM_NEED, idle_stop=IDLE_STOP, start_timeout=START_TIMEOUT, out=None):
        self.url, self.cmd, self.cwd, self.gpu, self.meminfo = url.rstrip('/'), list(cmd), cwd, gpu or Gpu(), meminfo
        self.log, self.clock, self.vram, self.ram = log or (lambda **kw: None), clock, vram, ram
        self.idle_stop, self.start_timeout, self.out = idle_stop, start_timeout, out
        self.proc, self.port, self.ready, self.started, self.used = None, None, False, None, None
        self.mode, self.pgid = None, None

    def child_url(self):
        return f'http://127.0.0.1:{self.port}'

    def ensure(self, now):
        if self.proc is not None:
            if self.proc.poll() is not None:
                self.log(event='child-exit', pid=self.proc.pid, code=self.proc.returncode)
                self.stop()                 # (what is left of its process group)
                return None, 'failed'
            if not self.ready:
                state = health(self.child_url())
                if state == 'ok':
                    self.ready, self.used = True, now
                    self.log(event='child-ready', pid=self.proc.pid, port=self.port, secs=round(now - self.started, 2))
                elif now - self.started >= self.start_timeout:
                    self.log(event='child-timeout', pid=self.proc.pid)
                    self.stop()
                    return None, 'failed'
                else:
                    return None, 'starting'
            self.mode = 'child'
            return self.child_url(), 'child'
        state = health(self.url)
        if state != 'down':
            self.mode = 'external' if state == 'ok' else None
            return (self.url, 'external') if state == 'ok' else (None, 'loading')
        temp, free = self.gpu.read(fresh=True)      # (not a reading up to 5 s old: 3 GB are about to go)
        avail = mem_available(self.meminfo)
        if free is None or free < self.vram or avail is None or avail < self.ram:
            self.mode = None
            return None, 'nomem'
        self._start(now, free, avail)
        return None, 'starting'

    def _start(self, now, free, avail):
        self.port = free_port()
        argv = [a.replace('{port}', str(self.port)) for a in self.cmd]
        out = open(self.out, 'ab') if self.out else subprocess.DEVNULL
        try:
            self.proc = subprocess.Popen(argv, cwd=self.cwd, stdin=subprocess.DEVNULL, stdout=out,
                                         stderr=subprocess.STDOUT, close_fds=True, preexec_fn=_child_setup(os.getpid()))
        finally:
            if out is not subprocess.DEVNULL:
                out.close()
        self.ready, self.started, self.used = False, now, now
        try:
            self.pgid = os.getpgid(self.proc.pid) if os.getpgid(self.proc.pid) == self.proc.pid else None
        except ProcessLookupError:
            self.pgid = None
        self.log(event='child-start', pid=self.proc.pid, port=self.port, vram_free=free, ram_free=avail)

    def room_to_wake(self):
        """Our child asleep and less than `vram` MiB free (read now): a request would wake it into too little."""
        if self.proc is None or not self.ready or not sleeping(self.child_url()):
            return True
        temp, free = self.gpu.read(fresh=True)
        return free is not None and free >= self.vram

    def touch(self, now):
        self.used = now

    def idle(self, now):
        """The child stops once it has not been asked for idle_stop s (since its last answer or its start), whether
        or not asks wait (a thermal pause, a backoff). -> when to look again (None: no child)."""
        if self.proc is None:
            return None
        if self.used is not None and now - self.used >= self.idle_stop:
            self.log(event='child-idle-stop', pid=self.proc.pid, idle=round(now - self.used, 1))
            self.stop()
            return None
        return (self.used or now) + self.idle_stop

    def stop(self):
        """SIGTERM to the child's process group (its own: a wrapper's children go too), SIGKILL to what is left of it
        after 10 s (3 s once the child itself is gone). By the group the child made at its start, never another."""
        p, pg = self.proc, self.pgid
        if p is None:
            return

        def signal_group(sig):
            try:
                if pg is not None:
                    os.killpg(pg, sig)
                elif p.poll() is None:
                    p.send_signal(sig)
                return True
            except ProcessLookupError:
                return False
        signal_group(signal.SIGTERM)
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            pass
        end = time.monotonic() + 3
        while signal_group(0) and pg is not None and time.monotonic() < end:
            time.sleep(0.05)
        signal_group(signal.SIGKILL)
        p.wait(10)
        self.log(event='child-stop', pid=p.pid, code=p.returncode)
        self._close()

    def _close(self):
        self.proc, self.port, self.ready, self.mode, self.pgid = None, None, False, None, None


class Worker:
    """The asks (module doc). set_wants() and set_focus() come from other threads; tick(now) does one step of work
    and returns when it wants to run again; run() is its thread. Status changes go to on_status(dict)."""

    def __init__(self, cache_path, llama, gpu=None, log_path=None, clock=time.monotonic, on_status=None,
                 title_of=None, quiet=QUIET, cap=CAP, hot=HOT, cool=COOL, max_tokens=200, prompt_chars=4000):
        self.cache_path, self.llama, self.gpu, self.log_path, self.clock = cache_path, llama, gpu or llama.gpu, log_path, clock
        self.on_status, self.title_of = on_status or (lambda st: None), title_of or (lambda key: None)
        self.quiet, self.cap, self.hot, self.cool = quiet, cap, hot, cool
        self.max_tokens, self.prompt_chars = max_tokens, prompt_chars
        self.cond = threading.Condition()
        self.wants, self.turn, self.focus = {}, None, None
        self.tries = {}                     # key -> [failures in a row, not before, backoffs]
        self.done = set()                   # keys answered (until the wants catch up)
        self.asking, self.paused, self.temp = None, False, None
        self.down, self.probe_at, self.probe_n, self.state = False, 0.0, 0, 'idle'
        self.status_v, self._status, self.error, self.why = 0, None, None, None
        self.last_ask = {}                  # blob identity -> when a label for it last came back
        self.llama.log = self.log

    # -- from other threads ---------------------------------------------------------------------------------------
    def set_wants(self, wants, turn):
        with self.cond:
            self.wants, self.turn = dict(wants), turn
            self.cond.notify_all()

    def set_focus(self, step):
        with self.cond:
            self.focus = step
            self.cond.notify_all()

    # -- the work ---------------------------------------------------------------------------------------------------
    def log(self, **rec):
        if not self.log_path:
            return
        rec = dict(at=time.strftime('%Y-%m-%dT%H:%M:%S'), **rec)
        os.makedirs(os.path.dirname(self.log_path) or '.', exist_ok=True)
        with open(self.log_path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + '\n')

    def due(self, w, now):
        """When want w may be asked (quiet, turn over, or the cap), not counting its failures' backoff. A blob asked
        for starts both clocks again from that answer: one that keeps changing is asked at most once per cap."""
        at, since = w.at, w.since
        last = max((self.last_ask[u] for u in w.uids if u in self.last_ask), default=None)
        if last is not None:
            at, since = max(at, last), max(since, last)
        if self.turn is not None and w.turn != self.turn:
            return at
        return min(at + self.quiet, since + self.cap)

    def _pick(self, ready):
        f = self.focus
        return min(ready, key=lambda w: (not (f is not None and w.vis <= f), -w.pos, -w.at, w.key))

    def _set(self, state, queued=0):
        self.state = state
        st = {'state': state, 'queued': queued, 'asking': self.asking, 'temp': self.temp,
              'server': self.llama.mode, 'child': self.llama.proc.pid if self.llama.proc else None,
              'keys': sorted(k for k in self.wants if k not in self.done), 'error': self.error,
              'why': self.why if self.down else None}
        if st != self._status:
            self._status = st
            self.status_v += 1
            self.on_status(dict(st))

    def tick(self, now):
        """One step: maybe one ask. -> the time to call again (no sooner than now)."""
        self.error = None
        with self.cond:
            wants = dict(self.wants)
            self.done &= wants.keys()
        for k in [k for k in self.tries if k not in wants]:
            del self.tries[k]
        todo = [w for k, w in wants.items() if k not in self.done]
        if not todo:
            self.paused = False
            self._set('idle')
            return self._rest(now, now + 60)
        times = {w.key: max(self.due(w, now), self.tries.get(w.key, (0, 0))[1]) for w in todo}
        ready = [w for w in todo if times[w.key] <= now]
        if not ready:
            self._set('paused' if self.paused else 'down' if self.down else 'waiting', len(todo))
            return self._rest(now, max(min(times.values()), self.probe_at if self.down else 0))
        self.temp, _ = self.gpu.read()
        if self.temp is not None and (self.temp >= self.hot or self.paused and self.temp >= self.cool):
            if not self.paused:
                self.log(event='pause', temp=self.temp)
            self.paused = True
            self._set('paused', len(todo))
            return self._rest(now, now + SMI_TTL)
        if self.paused:
            self.log(event='resume', temp=self.temp)
            self.paused = False
        if self.down and now < self.probe_at:
            self._set('down', len(todo))
            return self._rest(now, self.probe_at)
        url, state = self.llama.ensure(now)
        if url is not None and state == 'child' and not self.llama.room_to_wake():
            url, state = None, 'vram'       # (asleep, and waking it needs VRAM that is not free now)
        if url is None:
            if state == 'starting':         # (a child loading its model: /health every second)
                self._set('starting', len(todo))
                return now + 1
            self._down(now, state)
            self._set('down', len(todo))
            return self._rest(now, self.probe_at)
        if self.down:
            self.log(event='up', server=state)
            self.down, self.probe_n = False, 0
        w = self._pick(ready)
        self.asking = w.key
        self._set('asking', len(todo))
        self._ask(url, w, now, state)
        self.asking = None
        self._set('asking' if len(todo) > 1 else 'idle', len(todo) - 1)
        return now

    def _rest(self, now, nxt):
        """Nothing asked now (nothing wanted, or held back): a child not asked for idle_stop s stops. -> nxt, or
        sooner when that is due first."""
        stop = self.llama.idle(now)
        return nxt if stop is None else min(nxt, stop)

    def _down(self, now, why):
        if not self.down:
            self.log(event='down', why=why)
        self.down, self.why = True, why
        self.probe_at = now + PROBE[min(self.probe_n, len(PROBE) - 1)]
        self.probe_n += 1

    def _ask(self, url, w, now, server):
        blob = w.blob
        titles = {bid: t for bid, k in w.ctx.keys.items() if bid != blob['id'] for t in (self.title_of(k),) if t}
        taken = label.siblings(blob['id'], blob['module'], titles, w.ctx.module_of)[:label.MAX_TAKEN]
        stats = {'prompt': 0, 'gen': 0, 'reasoning': 0, 'truncated': 0, 'retries': 0, 'rejected': Counter(),
                 'fallback': False, 'calls': 0}
        t0 = time.monotonic()
        rec = {'event': 'ask', 'key': w.key[:80], 'blob': blob['id'], 'step': w.pos, 'server': server,
               'temp': self.temp}
        try:
            lab = label.ask(url, w.ctx.system, blob, stats, taken, self.max_tokens, False, self.prompt_chars)
        except Exception as e:      # the ask failed: tried again, then backed off
            t = self.tries.setdefault(w.key, [0, 0.0, 0])
            t[0] += 1
            if t[0] >= ATTEMPTS:
                t[1], t[0] = now + BACKOFF[min(t[2], len(BACKOFF) - 1)], 0
                t[2] += 1
            else:
                t[1] = now + RETRY[t[0] - 1]
            if is_down(e):
                self._down(now, f'{type(e).__name__}: {e}')
            self.log(**rec, status='failed', error=label.error_text(blob, e)[:300], calls=stats['calls'],
                     secs=round(time.monotonic() - t0, 2))
            return
        label.save_cache(self.cache_path, {w.key: lab})
        self.llama.touch(now)
        for u in w.uids:
            self.last_ask[u] = now
        self.tries.pop(w.key, None)
        with self.cond:
            self.done.add(w.key)
        self.log(**rec, status='ok', title=lab['title'], calls=stats['calls'], fallback=stats['fallback'],
                 secs=round(time.monotonic() - t0, 2), prompt=stats['prompt'], gen=stats['gen'])

    def run(self, stop, cap=1.0):
        """The thread: tick() until `stop` is set, waiting (at most `cap` real s) in between; the child goes with it."""
        try:
            while not stop.is_set():
                try:
                    nxt = self.tick(self.clock())
                except Exception as e:      # logged and shown (the chip), then the next tick
                    self.error = f'{type(e).__name__}: {e}'[:300]
                    print(f'livelabel: {self.error}', file=sys.stderr)
                    self.log(event='error', error=self.error)
                    self._set('error')
                    nxt = self.clock() + 5
                with self.cond:
                    wait = nxt - self.clock()
                    if wait > 0 and not stop.is_set():
                        self.cond.wait(min(wait, cap))
        finally:
            self.llama.stop()

    def wake(self):
        with self.cond:
            self.cond.notify_all()
