"""Processes deskpilot launched (pid, output log, exit status) and the sandbox X display.

A sandbox is a private X server that apps are launched into so they can be driven without
touching the user's desktop: deskpilot's input there never moves the user's pointer or reaches a
window they work in. Xvfb is invisible. Xephyr (used when Xvfb is missing) is a window on the
desktop: when it first appears GNOME may focus it for a moment (keys typed then go into the
sandbox) before deskpilot minimizes it.
"""
from __future__ import annotations

import atexit
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

from . import xutil

LOG_DIR = os.path.join(tempfile.gettempdir(), f"deskpilot-{os.getuid()}")


@dataclass
class Proc:
    pid: int
    command: str
    log: str
    display: str | None
    popen: subprocess.Popen = field(repr=False)
    started: float = field(default_factory=time.time)

    def status(self) -> str:
        rc = self.popen.poll()
        return "running" if rc is None else f"exited with code {rc}"


PROCS: dict[int, Proc] = {}


def launch(command: str, cwd: str | None = None, env: dict[str, str] | None = None) -> Proc:
    """Run a shell command detached on the current display; stdout+stderr go to a log file."""
    os.makedirs(LOG_DIR, exist_ok=True)
    e = xutil.env()
    if env:
        e.update({k: str(v) for k, v in env.items()})
    fd, log = tempfile.mkstemp(prefix="launch-", suffix=".log", dir=LOG_DIR)
    with os.fdopen(fd, "w") as out:
        p = subprocess.Popen(command, shell=True, start_new_session=True, env=e,
                             cwd=os.path.expanduser(cwd) if cwd else None,
                             stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
    proc = Proc(p.pid, command, log, xutil.current_display(), p)
    PROCS[p.pid] = proc
    return proc


def get(pid: int) -> Proc:
    if pid not in PROCS:
        raise RuntimeError(f"pid {pid} wasn't launched by deskpilot; known: {sorted(PROCS) or 'none'}")
    return PROCS[pid]


def tail(path: str, lines: int = 40, max_bytes: int = 200_000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode(errors="replace")
    except OSError as e:
        return f"(no log: {e})"
    out = data.splitlines()[-lines:]
    return "\n".join(out) if out else "(no output yet)"


def kill(pid: int, timeout: float = 3) -> str:
    """SIGTERM the process group, then SIGKILL if it lingers. The group goes even when its shell
    already exited (`app &`, forking launchers): start_new_session made pgid == pid, and that
    number can't be reused while any member lives."""
    proc = get(pid)
    if proc.popen.poll() is not None and os.path.exists(f"/proc/{pid}"):
        # Our leader is gone and reaped, yet a process has its pid: the number was reused, and
        # that group is someone else's. (Our orphans never include a process with that pid.)
        return proc.status()
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return proc.status()  # nothing left in the group
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            break
        proc.popen.poll()  # reap the leader so it doesn't keep the group alive as a zombie
        time.sleep(0.05)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.popen.wait(2)
    except subprocess.TimeoutExpired:
        pass  # stuck in the kernel; reported as running
    return proc.status()


# ---------------------------------------------------------------- sandbox display

@dataclass
class Sandbox:
    display: str
    server: str  # "Xvfb" or "Xephyr"
    size: str
    popen: subprocess.Popen = field(repr=False)


SANDBOX: Sandbox | None = None


def _free_display() -> str:
    for n in range(91, 200):
        if not os.path.exists(f"/tmp/.X11-unix/X{n}") and not os.path.exists(f"/tmp/.X{n}-lock"):
            return f":{n}"
    raise RuntimeError("no free X display number in :91..:199")


def start_sandbox(size: str = "1600x900", visible: bool = False) -> Sandbox:
    """Start (or reuse) the sandbox. Xvfb is invisible; Xephyr is used when Xvfb is missing or
    visible=True (a window on your desktop that mirrors the sandbox; minimized unless visible)."""
    global SANDBOX
    if SANDBOX and SANDBOX.popen.poll() is None:
        return SANDBOX
    w, h = (int(v) for v in size.lower().split("x"))
    disp = _free_display()
    xvfb, xephyr = shutil.which("Xvfb"), shutil.which("Xephyr")
    if xvfb and not visible:
        cmd, server = [xvfb, disp, "-screen", "0", f"{w}x{h}x24", "-nolisten", "tcp"], "Xvfb"
    elif xephyr:
        cmd, server = [xephyr, disp, "-screen", f"{w}x{h}", "-title", f"deskpilot sandbox {disp}",
                       "-nolisten", "tcp", "-no-host-grab"], "Xephyr"
    else:
        raise RuntimeError("no Xvfb or Xephyr: sudo apt install xvfb (or xserver-xephyr)")
    host = dict(os.environ)
    focused = subprocess.run(["xdotool", "getactivewindow"], env=host, capture_output=True,
                             text=True).stdout.strip()  # to give focus back if Xephyr takes it
    p = subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 10
    while time.time() < deadline:
        if p.poll() is not None:
            raise RuntimeError(f"{server} exited with code {p.returncode}")
        r = subprocess.run(["xdotool", "getdisplaygeometry"], env={**os.environ, "DISPLAY": disp},
                           capture_output=True, timeout=5)
        if r.returncode == 0:
            break
        time.sleep(0.2)
    else:
        p.kill()
        raise RuntimeError(f"{server} on {disp} didn't come up within 10 s")
    if server == "Xephyr" and not visible:
        _minimize_host_window(p.pid, disp, focused)
    SANDBOX = Sandbox(disp, server, f"{w}x{h}", p)
    return SANDBOX


def install_exit_cleanup() -> None:
    """Call once from the main thread at startup (tools run in worker threads, where signal handlers
    can't be installed). The X server runs in its own session, so a stray signal to us can't take
    it down mid-use, which also means it would outlive us: stop it when deskpilot exits, normally or
    by SIGTERM/SIGHUP."""
    atexit.register(lambda: SANDBOX and stop_sandbox())
    for sig in (signal.SIGTERM, signal.SIGHUP):
        prev = signal.getsignal(sig)
        if prev == signal.SIG_IGN:
            continue  # e.g. under nohup: keep ignoring it

        def handler(signum, frame, prev=prev):
            if SANDBOX:
                stop_sandbox()
            if callable(prev):
                prev(signum, frame)
            else:
                raise SystemExit(128 + signum)
        signal.signal(sig, handler)


def _minimize_host_window(pid: int, disp: str, focused: str) -> None:
    """Xephyr's window on the real desktop: out of the way (it still renders while minimized), and
    focus handed back to the window that had it. Xephyr sets no _NET_WM_PID, so the window is found
    by its exact title and confirmed by asking the desktop's X server who owns it."""
    host = dict(os.environ)
    title = f"^deskpilot sandbox {re.escape(disp)}$"
    for _ in range(30):
        r = subprocess.run(["xdotool", "search", "--name", title], env=host, capture_output=True, text=True)
        ids = [i for i in r.stdout.split() if xutil.xres_pid(int(i), host.get("DISPLAY")) == pid]
        if ids:
            for i in ids:
                subprocess.run(["xdotool", "windowminimize", i], env=host, capture_output=True)
            if focused:
                subprocess.run(["xdotool", "windowactivate", focused], env=host, capture_output=True)
            return
        time.sleep(0.1)


def stop_sandbox() -> str:
    """Kill the sandbox's apps, then its X server."""
    global SANDBOX
    if not SANDBOX:
        return "no sandbox running"
    for pid, proc in list(PROCS.items()):
        if proc.display == SANDBOX.display:
            try:
                kill(pid)
            except Exception:
                pass  # the X server going away ends its clients anyway
    SANDBOX.popen.terminate()
    try:
        SANDBOX.popen.wait(5)
    except subprocess.TimeoutExpired:
        SANDBOX.popen.kill()
    disp, SANDBOX = SANDBOX.display, None
    xutil._has_wm.pop(disp, None)  # the next sandbox may reuse the number
    if xutil.current_display() == disp:
        xutil.use_display(None)
    return f"sandbox {disp} stopped"
