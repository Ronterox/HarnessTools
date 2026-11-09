"""Headless Chrome for the page tests, driven through cdp_driver.js (node): Browser().goto/eval/wait/shot/heap."""
import json, os, re, shutil, signal, subprocess, tempfile, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CHROME = next((shutil.which(c) for c in ('google-chrome', 'chromium', 'chromium-browser') if shutil.which(c)), None)


class Browser:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix='aiblob-chrome-')
        self.chrome = subprocess.Popen(
            [CHROME, '--headless=new', '--remote-debugging-port=0', f'--user-data-dir={self.dir}', '--no-first-run',
             '--no-default-browser-check', '--disable-gpu', '--disable-extensions', '--window-size=1400,900',
             '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
             '--disable-features=BackForwardCache', 'about:blank'],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True)
        port = None
        for line in self.chrome.stderr:
            m = re.search(r'DevTools listening on ws://127\.0\.0\.1:(\d+)/', line)
            if m:
                port = int(m.group(1))
                break
        assert port, 'chrome did not start'
        for _ in range(100):
            pages = [t for t in json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list'))
                     if t['type'] == 'page']
            if pages:
                break
            time.sleep(0.05)
        self.drv = subprocess.Popen(['node', os.path.join(HERE, 'cdp_driver.js'), pages[0]['webSocketDebuggerUrl']],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        assert json.loads(self.drv.stdout.readline()).get('ready')

    def _cmd(self, **c):
        self.drv.stdin.write(json.dumps(c) + '\n')
        self.drv.stdin.flush()
        out = json.loads(self.drv.stdout.readline())
        if 'error' in out:
            raise AssertionError(f"{c['cmd']}: {out['error']}")
        return out

    def goto(self, url):
        self._cmd(cmd='goto', url=url)

    def eval(self, expr):
        return self._cmd(cmd='eval', expr=expr).get('value')

    def wait(self, expr, timeout=15000):
        return self._cmd(cmd='wait', expr=expr, timeout=timeout)['value']

    def shot(self, path):
        self._cmd(cmd='shot', path=path)

    def heap(self):
        return self._cmd(cmd='heap')

    def errors(self):
        return self._cmd(cmd='errors')['value']

    def close(self):
        try:
            self.drv.stdin.close()
            self.drv.wait(10)
        except (OSError, subprocess.TimeoutExpired):
            self.drv.kill()
        self.chrome.terminate()
        try:
            self.chrome.wait(10)
        except subprocess.TimeoutExpired:
            os.killpg(self.chrome.pid, signal.SIGKILL)      # (its own session: only its renderers)
            self.chrome.wait(10)
        shutil.rmtree(self.dir, ignore_errors=True)
