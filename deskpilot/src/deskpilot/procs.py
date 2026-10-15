"""Processes deskpilot launched (pid, output log, exit status) and the sandbox X display.

A sandbox is a private X server that apps are launched into so they can be driven without
touching the user's desktop: deskpilot's input there never moves the user's pointer or reaches a
window they work in. Xvfb is invisible. Xephyr (used when Xvfb is missing) is a window on the
desktop: when it first appears GNOME may focus it for a moment (keys typed then go into the
sandbox) before deskpilot minimizes it.
"""
from __future__ import annotations

import atexit
import glob
import json
import os
import pathlib
import re
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from xml.sax.saxutils import escape as xml_escape

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
    warning: str = ""  # something the caller should know about this launch

    def status(self) -> str:
        rc = self.popen.poll()
        return "running" if rc is None else f"exited with code {rc}"


PROCS: dict[int, Proc] = {}

# What a sandbox launch must not inherit from the user's session: the Wayland socket (toolkits
# would open their window on the real desktop), the input method (ibus/fcitx serve the real
# display only) and HiDPI scaling (Xvfb is 96 dpi, apps should render 1:1).
SANDBOX_UNSET = ("WAYLAND_DISPLAY", "QT_IM_MODULE", "GDK_SCALE", "GDK_DPI_SCALE", "QT_SCALE_FACTOR",
                 "QT_AUTO_SCREEN_SCALE_FACTOR")
SANDBOX_SET = {
    # File choosers etc. in-process: through xdg-desktop-portal they'd open on the real desktop
    # (GTK 4 also gets GDK_DEBUG=no-portals, added to any GDK_DEBUG already set).
    "GTK_USE_PORTAL": "0",
    # X11 only, whatever the toolkit would pick from the session.
    "GDK_BACKEND": "x11", "QT_QPA_PLATFORM": "xcb", "SDL_VIDEODRIVER": "x11", "MOZ_ENABLE_WAYLAND": "0",
    "ELECTRON_OZONE_PLATFORM_HINT": "x11",
    # Software GL: Mesa's hardware paths (zink/Vulkan) crash on Xephyr. Xvfb's and Xephyr's GLX is
    # Mesa's, so the NVIDIA vendor library could never serve it: always pick Mesa's.
    "LIBGL_ALWAYS_SOFTWARE": "1", "__GLX_VENDOR_LIBRARY_NAME": "mesa",
    "XMODIFIERS": "@im=none", "GTK_IM_MODULE": "gtk-im-context-simple",
}


def launch_env(base: dict[str, str], sandbox: bool = False, bus: str | None = None,
               a11y_bus: str | None = None) -> dict[str, str]:
    """Environment for a launched app. Every launch exposes its accessibility tree (Qt only does
    when asked; NO_AT_BRIDGE switches GTK's off). A sandbox launch is also cut off from the user's
    session: SANDBOX_UNSET removed (really unset: e.g. an empty WAYLAND_DISPLAY isn't the same as
    none), SANDBOX_SET applied, and bus (the sandbox's private D-Bus session bus) replaces the
    user's, so D-Bus single-instance apps can't find a copy running on the desktop and open their
    window there. a11y_bus: the user's accessibility bus, where deskpilot's own AT-SPI client looks."""
    e = dict(base)
    e.pop("NO_AT_BRIDGE", None)
    e["QT_LINUX_ACCESSIBILITY_ALWAYS_ON"] = "1"
    if not sandbox:
        return e
    for k in SANDBOX_UNSET:
        e.pop(k, None)
    e.update(SANDBOX_SET)
    flags = [f for f in base.get("GDK_DEBUG", "").split(",") if f]
    e["GDK_DEBUG"] = ",".join(flags if "no-portals" in flags else flags + ["no-portals"])
    if bus:
        e["DBUS_SESSION_BUS_ADDRESS"] = bus
        if a11y_bus:
            e["AT_SPI_BUS_ADDRESS"] = a11y_bus
    return e


# Apps whose single-instance hand-off doesn't go over D-Bus but through their profile (a lock or
# socket in it): a plain launch on the sandbox gets a throwaway one, else it would reach the copy on
# the desktop, or stop at the profile that copy has locked.
_PROFILE_APPS = {"firefox": "firefox", "firefox-esr": "firefox", "google-chrome": "chrome",
                 "google-chrome-stable": "chrome", "chromium": "chrome", "chromium-browser": "chrome",
                 "soffice": "soffice", "libreoffice": "soffice"}


def isolated_command(command: str, profile: str) -> str:
    """A plain command line starting one of _PROFILE_APPS, with the args that give it the throwaway
    profile dir `profile`. Returned unchanged when it already picks a profile, or isn't a plain
    command: unquoted shell operators, $ or backticks (expansions), backslashes, several lines."""
    if "\n" in command:
        return command
    lex = shlex.shlex(command, posix=False, punctuation_chars=True)  # posix=False: quotes kept
    lex.whitespace_split, lex.commenters = True, ""
    try:
        toks = list(lex)
    except ValueError:  # unbalanced quotes
        return command
    for t in toks:
        if all(c in lex.punctuation_chars for c in t):
            return command  # an unquoted operator: ; & | < > ( )
        if any(c in re.sub(r"'[^']*'", "", t) for c in "$`\\"):  # outside '…' the shell expands
            return command
        if '"' in t and any(c in t for c in "$`\\"):  # "a'$HOME'b": single quotes inside "…" are literal
            return command
    raw = command.strip()
    if not toks or not raw.startswith(toks[0]):
        return command
    app = _PROFILE_APPS.get(os.path.basename(toks[0].strip("'\"")))
    keys = {t.strip("'\"").lower().split("=", 1)[0] for t in toks[1:]}
    if app == "firefox" and not keys & {"-profile", "--profile", "-p", "--p", "-profilemanager",
                                        "--profilemanager"}:
        add = [] if keys & {"-new-instance", "--new-instance", "-no-remote", "--no-remote"} else ["--new-instance"]
        add += ["--profile", shlex.quote(profile)]
    elif app == "chrome" and "--user-data-dir" not in keys:
        # Chromium exposes web content to AT-SPI only when asked to (ui() would see just the frame).
        add = [shlex.quote(f"--user-data-dir={profile}"), "--no-first-run", "--no-default-browser-check",
               "--force-renderer-accessibility"]
    elif app == "soffice" and "-env:userinstallation" not in keys:
        add = [shlex.quote(f"-env:UserInstallation={pathlib.Path(profile).as_uri()}")]
    else:
        return command
    return " ".join([toks[0], *add]) + raw[len(toks[0]):]


# A fresh Firefox profile opens on a modal welcome / terms-of-use dialog that covers the page:
# skip it. Written into every throwaway profile; only Firefox reads user.js.
_FIREFOX_USER_JS = """user_pref("browser.preonboarding.enabled", false);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("browser.shell.checkDefaultBrowser", false);
"""

_A11Y_BUS: str | None = None  # cached once found (asking spawns gdbus)


def _a11y_bus() -> str | None:
    """Address of the user's accessibility bus, or None: $AT_SPI_BUS_ADDRESS if set (as libatspi
    does), else asked of their session bus."""
    global _A11Y_BUS
    if os.environ.get("AT_SPI_BUS_ADDRESS"):
        return os.environ["AT_SPI_BUS_ADDRESS"]
    if _A11Y_BUS:
        return _A11Y_BUS
    try:
        r = subprocess.run(["gdbus", "call", "--session", "--dest", "org.a11y.Bus", "--object-path",
                            "/org/a11y/bus", "--method", "org.a11y.Bus.GetAddress"],
                           env=dict(os.environ), capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"'([^']+)'", r.stdout)
    _A11Y_BUS = m.group(1) if r.returncode == 0 and m else None
    return _A11Y_BUS


def private_dir(path: str) -> str:
    """Create path 0700 if missing and refuse it unless it is a real directory we own: in a shared
    /tmp, someone else's directory (or a symlink) could feed us the bus config and the service files
    it runs. A mode left loose by older versions is tightened."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise RuntimeError(f"{path} is not a directory owned by you (or is a symlink); refusing to use it")
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path


def launch(command: str, cwd: str | None = None, env: dict[str, str] | None = None) -> Proc:
    """Run a shell command detached on the current display; stdout+stderr go to a log file. On the
    sandbox it gets launch_env's sandbox defaults (env, the caller's, overrides any of them) and
    isolated_command's throwaway profile."""
    private_dir(LOG_DIR)
    sb = SANDBOX if SANDBOX and SANDBOX.display == xutil.current_display() else None
    warning = ""
    if sb:
        a11y_bus = _a11y_bus() if sb.bus else None
        e = launch_env(xutil.env(), sandbox=True, bus=sb.bus, a11y_bus=a11y_bus)
        if not sb.bus:
            warning = f"no private D-Bus bus ({sb.bus_error}): a single-instance app may open on the desktop"
        elif not a11y_bus:
            warning = "your accessibility bus wasn't found: ui()/act() won't see this app"
        profile = os.path.join(sb.tmp, f"profile-{time.time_ns()}")
        if (isolated := isolated_command(command, profile)) != command:
            os.makedirs(profile)
            with open(os.path.join(profile, "user.js"), "w") as f:
                f.write(_FIREFOX_USER_JS)
            command = isolated
    else:
        e = launch_env(xutil.env())
    if env:
        e.update({k: str(v) for k, v in env.items()})
    fd, log = tempfile.mkstemp(prefix="launch-", suffix=".log", dir=LOG_DIR)
    with os.fdopen(fd, "w") as out:
        p = subprocess.Popen(command, shell=True, start_new_session=True, env=e,
                             cwd=os.path.expanduser(cwd) if cwd else None,
                             stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
    proc = Proc(p.pid, command, log, xutil.current_display(), p, warning=warning)
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
    tmp: str = ""  # its dir in LOG_DIR (owner record, bus config, throwaway profiles); removed on stop
    bus: str | None = None  # its private D-Bus session bus
    bus_popen: subprocess.Popen | None = field(default=None, repr=False)
    bus_error: str = ""  # why there's no private bus
    sock: str = ""  # the bus's socket file

    def describe(self) -> str:
        s = f"{self.display} ({self.server}, {self.size})"
        return s if self.bus else (f"{s}; no private D-Bus bus ({self.bus_error}): single-instance apps "
                                   f"launched there may open on the desktop")


SANDBOX: Sandbox | None = None

# The private session bus. Only dconf can be activated on it (GSettings writes keep working):
# everything else, portals above all, fails at once. With the standard service dirs, GTK's startup
# call to the inhibit portal waits 25 s for an xdg-desktop-portal that can't come up off-session.
_BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={sock}</listen>
  <auth>EXTERNAL</auth>
  <servicedir>{services}</servicedir>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


def _socket_dir() -> str:
    """A short private dir for bus sockets. A socket path has at most 107 bytes, which a long $TMPDIR
    pushes LOG_DIR past (and dbus 1.12's unix:tmpdir= makes an abstract name of the same length)."""
    for d in (os.environ.get("XDG_RUNTIME_DIR"), f"/run/user/{os.getuid()}"):
        if d and os.path.isdir(d):
            try:
                return private_dir(os.path.join(d, "deskpilot"))
            except (OSError, RuntimeError):
                pass
    return private_dir(LOG_DIR)


def _start_bus(tmp: str, disp: str) -> tuple[str | None, subprocess.Popen | None, str, str]:
    """A private D-Bus session bus for the sandbox: (address, process, "", socket), or (None, None,
    why not, ""). Never raises: without it the sandbox still works, less isolated, and says so.
    --nofork in its own session (not --fork / dbus-launch): we keep its Popen and stop it with the X
    server. Its stderr goes to LOG_DIR/bus-N.log. The accessibility bus stays the user's (launch_env)."""
    daemon = shutil.which("dbus-daemon")
    if not daemon:
        return None, None, "dbus-daemon not installed", ""
    p = None
    log = os.path.join(LOG_DIR, f"bus-{disp[1:]}.log")
    try:
        sock = os.path.join(_socket_dir(), os.path.basename(tmp) + ".bus")  # unique, like tmp
        if len(sock.encode()) > 107:
            return None, None, f"socket path too long: {sock}", ""
        services = os.path.join(tmp, "services")
        os.makedirs(services)
        for d in ("/usr/share/dbus-1/services", "/usr/local/share/dbus-1/services"):
            src = os.path.join(d, "ca.desrt.dconf.service")
            if os.path.exists(src):
                shutil.copy(src, services)
                break
        conf = os.path.join(tmp, "bus.conf")
        with open(conf, "w") as f:
            f.write(_BUS_CONFIG.format(sock=xml_escape(sock), services=xml_escape(services)))
        # Services it activates inherit its environment: give them the sandbox's display.
        with open(log, "w") as err:
            p = subprocess.Popen([daemon, f"--config-file={conf}", "--nofork", "--nopidfile", "--print-address=1"],
                                 env=launch_env(xutil.env() | {"DISPLAY": disp}, sandbox=True),
                                 start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=err)
        # The address line comes once it listens; never wait on it for long (tool calls hold a lock).
        out, deadline = b"", time.time() + 5
        with selectors.DefaultSelector() as sel:
            sel.register(p.stdout, selectors.EVENT_READ)
            while b"\n" not in out and (left := deadline - time.time()) > 0 and sel.select(left):
                chunk = os.read(p.stdout.fileno(), 4096)
                if not chunk:
                    break  # it exited
                out += chunk
        p.stdout.close()
        if addr := out.decode(errors="replace").strip():
            return addr, p, "", sock
        why = tail(log, 1) if os.path.getsize(log) else "no address within 5 s"
    except BaseException as e:
        if p:
            p.kill()
            p.wait()
        if not isinstance(e, Exception):
            raise  # Ctrl-C etc.: after the cleanup, let it through
        return None, None, f"{type(e).__name__}: {e}; see {log}", ""
    p.kill()
    p.wait()
    return None, None, f"{why}; see {log}", ""


def _proc_start(pid: int) -> int | None:
    """When a process started (clock ticks since boot): with its pid, it can't be mistaken for a
    later process that got the same pid."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def _write_owner(tmp: str, owner: dict) -> None:
    with open(os.path.join(tmp, "owner.tmp"), "w") as f:
        json.dump(owner, f)
    os.replace(os.path.join(tmp, "owner.tmp"), os.path.join(tmp, "owner.json"))


def _ours(path: str) -> bool:
    """A real file or dir (not a symlink) owned by us."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return st.st_uid == os.getuid() and not stat.S_ISLNK(st.st_mode)


def _is_pid(v) -> bool:
    return type(v) is int and v > 1 and v != os.getpid()  # never -1 (everyone), 0 (our group), init


def _sweep_stale() -> None:
    """A hard-killed deskpilot (SIGKILL skips the exit cleanup) leaves its sandbox behind for good: X
    server, private bus (its dconf-service exits with the bus), socket and dir. Each sandbox dir
    records its deskpilot and their processes as (pid, start time): when that deskpilot is gone,
    end the recorded processes still running and remove the socket and the dir. The record is
    trusted only as far as it can be checked: our own real files, plain pids, our socket path."""
    try:
        sock_dir = _socket_dir()
    except (OSError, RuntimeError):
        sock_dir = None
    for d in glob.glob(os.path.join(LOG_DIR, "sandbox*-*")):
        owner = os.path.join(d, "owner.json")
        if not (_ours(d) and _ours(owner)):
            continue
        try:
            with open(owner) as f:
                o = json.load(f)
            if not (_is_pid(o["pid"]) and type(o["start"]) is int) or _proc_start(o["pid"]) == o["start"]:
                continue  # its deskpilot still runs, or a record we can't judge: leave it
            alive = [(e[0], e[1]) for e in o.get("procs", [])
                     if isinstance(e, list) and len(e) == 2 and _is_pid(e[0]) and type(e[1]) is int
                     and _proc_start(e[0]) == e[1]]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue  # being set up right now, or not ours to judge: leave it
        for sig in (signal.SIGTERM, signal.SIGKILL):  # SIGTERM first: X removes its lock file
            for pid, start in alive:
                if _proc_start(pid) == start:  # still that process, not a recycled pid
                    try:
                        os.kill(pid, sig)
                    except OSError:
                        pass
            for _ in range(20):
                alive = [(pid, start) for pid, start in alive if _proc_start(pid) == start]
                if not alive:
                    break
                time.sleep(0.1)
        if sock_dir and o.get("sock") == os.path.join(sock_dir, os.path.basename(d) + ".bus"):
            try:
                os.unlink(o["sock"])
            except OSError:
                pass
        shutil.rmtree(d, ignore_errors=True)


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
    if SANDBOX:
        stop_sandbox()  # its X server died on its own: still end its apps and bus, remove its dir
    w, h = (int(v) for v in size.lower().split("x"))
    private_dir(LOG_DIR)
    _sweep_stale()
    disp = _free_display()
    xvfb, xephyr = shutil.which("Xvfb"), shutil.which("Xephyr")
    if xvfb and not visible:
        cmd, server = [xvfb, disp, "-screen", "0", f"{w}x{h}x24", "-nolisten", "tcp"], "Xvfb"
    elif xephyr:
        cmd, server = [xephyr, disp, "-screen", f"{w}x{h}", "-title", f"deskpilot sandbox {disp}",
                       "-nolisten", "tcp", "-no-host-grab"], "Xephyr"
    else:
        raise RuntimeError("no Xvfb or Xephyr: sudo apt install xvfb (or xserver-xephyr)")
    tmp = tempfile.mkdtemp(prefix=f"sandbox{disp[1:]}-", dir=LOG_DIR)
    owner = {"pid": os.getpid(), "start": _proc_start(os.getpid()), "procs": []}
    _write_owner(tmp, owner)
    p = bus_p = None
    sock = ""
    try:
        host = dict(os.environ)
        focused = subprocess.run(["xdotool", "getactivewindow"], env=host, capture_output=True,
                                 text=True).stdout.strip()  # to give focus back if Xephyr takes it
        p = subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        owner["procs"].append([p.pid, _proc_start(p.pid)])
        _write_owner(tmp, owner)
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
            raise RuntimeError(f"{server} on {disp} didn't come up within 10 s")
        if server == "Xephyr" and not visible:
            _minimize_host_window(p.pid, disp, focused)
        bus, bus_p, bus_error, sock = _start_bus(tmp, disp)
        if bus_p:
            owner["procs"].append([bus_p.pid, _proc_start(bus_p.pid)])
            owner["sock"] = sock
            _write_owner(tmp, owner)
    except BaseException:
        for q in (bus_p, p):
            if q:
                q.kill()
                q.wait()
        if sock:
            try:
                os.unlink(sock)
            except OSError:
                pass
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    SANDBOX = Sandbox(disp, server, f"{w}x{h}", p, tmp, bus, bus_p, bus_error, sock)
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
    """Kill the sandbox's apps, then its D-Bus session bus (services it started exit with it) and
    its X server."""
    global SANDBOX
    if not SANDBOX:
        return "no sandbox running"
    for pid, proc in list(PROCS.items()):
        if proc.display == SANDBOX.display:
            try:
                kill(pid)
            except Exception:
                pass  # the X server going away ends its clients anyway
    for p in (SANDBOX.bus_popen, SANDBOX.popen):
        if p is None:
            continue
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
    if SANDBOX.sock:
        try:
            os.unlink(SANDBOX.sock)
        except OSError:
            pass
    if SANDBOX.tmp:
        shutil.rmtree(SANDBOX.tmp, ignore_errors=True)
    disp, SANDBOX = SANDBOX.display, None
    xutil._has_wm.pop(disp, None)  # the next sandbox may reuse the number
    if xutil.current_display() == disp:
        xutil.use_display(None)
    return f"sandbox {disp} stopped"
