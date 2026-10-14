"""X11 helpers: window listing/management and raw input via wmctrl + xdotool."""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass


# The X display every command targets: None = the user's desktop ($DISPLAY), else e.g. ":91" (a
# sandbox from procs.py). Switched for the whole server, so all tools agree on one display.
_display: str | None = None
_has_wm: dict[str | None, bool] = {}


def use_display(display: str | None) -> None:
    global _display
    _display = display


def current_display() -> str | None:
    return _display


def env() -> dict[str, str]:
    """Environment for X clients aimed at the current display."""
    e = dict(os.environ)
    if _display:
        e["DISPLAY"] = _display
    return e


def run(*args: str, input: str | None = None, timeout: float = 15) -> str:
    r = subprocess.run(args, capture_output=True, text=True, input=input, timeout=timeout, env=env())
    if r.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {r.stderr.strip() or r.returncode}")
    return r.stdout


@dataclass
class Win:
    id: int
    desktop: int
    pid: int
    wm_class: str
    title: str
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0

    @property
    def hex(self) -> str:
        return f"0x{self.id:08x}"

    @property
    def app(self) -> str:
        return self.wm_class.rsplit(".", 1)[-1]


def has_wm() -> bool:
    """Is an EWMH window manager running on the current display? (A sandbox usually has none, and
    then wmctrl and _NET_* properties don't work: windows are found and focused with plain X calls.)
    Always True for the user's desktop: a slow or failing wmctrl there must never switch on the
    WM-less paths (frame windows would then defeat the own-terminal check)."""
    if _display is None:
        return True
    if _display not in _has_wm:
        try:
            run("wmctrl", "-m", timeout=5)
            _has_wm[_display] = True
        except Exception:
            _has_wm[_display] = False
    return _has_wm[_display]


def active_window_id() -> int | None:
    try:
        if not has_wm():
            return _toplevel_of(int(run("xdotool", "getwindowfocus", "-f").strip()))
        return int(run("xdotool", "getactivewindow").strip())
    except Exception:
        return None


def geometry(wid: int) -> tuple[int, int, int, int]:
    """Absolute client-area geometry (x, y, w, h) of a window."""
    out = run("xwininfo", "-id", str(wid))
    get = lambda k: int(re.search(rf"{k}:\s+(-?\d+)", out).group(1))
    return get("Absolute upper-left X"), get("Absolute upper-left Y"), get("Width"), get("Height")


def _is_desktop_or_dock(wid: int) -> bool:
    try:
        t = run("xprop", "-id", str(wid), "_NET_WM_WINDOW_TYPE")
    except Exception:
        return False
    return "DESKTOP" in t or "DOCK" in t


def _toplevel_of(wid: int) -> int:
    """Without a WM, focus can sit on a subwindow: climb to the child of the root."""
    root = None
    for _ in range(32):
        out = run("xwininfo", "-children", "-id", str(wid))
        m_root = re.search(r"Root window id: (0x[0-9a-f]+)", out)
        m_par = re.search(r"Parent window id: (0x[0-9a-f]+)", out)
        if not m_root or not m_par:
            return wid
        root = int(m_root.group(1), 16)
        parent = int(m_par.group(1), 16)
        if parent == root:
            return wid
        wid = parent
    return wid


class _XResSpec(ctypes.Structure):
    _fields_ = [("client", ctypes.c_ulong), ("mask", ctypes.c_uint)]


class _XResValue(ctypes.Structure):
    _fields_ = [("spec", _XResSpec), ("length", ctypes.c_long), ("value", ctypes.c_void_p)]


_xlibs = None


def _xres_libs():
    """libX11 + libXRes, set up once. Connections are opened per query instead of cached: a cached
    one to a sandbox that has since stopped would make Xlib's I/O error handler exit the server."""
    global _xlibs
    if _xlibs is not None:
        return _xlibs
    x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
    xres = ctypes.CDLL(ctypes.util.find_library("XRes") or "libXRes.so.1")
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    # A window that vanished between listing and asking must not kill the server (Xlib's default
    # error handler exits the process): ignore X protocol errors. Note this handler is process-wide
    # for libX11 (nothing else in deskpilot uses libX11 through ctypes; PIL and python-xlib don't).
    handler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(lambda d, e: 0)
    x11.XSetErrorHandler(handler)
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    xres.XResQueryClientIds.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.POINTER(_XResSpec),
                                        ctypes.POINTER(ctypes.c_long), ctypes.POINTER(ctypes.POINTER(_XResValue))]
    xres.XResGetClientPid.argtypes = [ctypes.POINTER(_XResValue)]
    xres.XResGetClientPid.restype = ctypes.c_int
    xres.XResClientIdsDestroy.argtypes = [ctypes.c_long, ctypes.POINTER(_XResValue)]
    _xlibs = (x11, xres, handler)
    return _xlibs


def xres_pid(wid: int, display: str | None = None) -> int:
    """Pid of the X client that owns wid, asked of the X server itself (XRes extension). Works for
    toolkits that don't set _NET_WM_PID (Tk, Xephyr, many games); 0 when unknown or remote.
    display: which X server to ask (default: the current one)."""
    try:
        x11, xres, _ = _xres_libs()
        dpy = x11.XOpenDisplay((display or _display or os.environ.get("DISPLAY", "")).encode())
        if not dpy:
            return 0
    except Exception:
        return 0
    try:
        spec = _XResSpec(wid, 2)  # XRES_CLIENT_ID_PID_MASK
        n = ctypes.c_long(0)
        vals = ctypes.POINTER(_XResValue)()
        if xres.XResQueryClientIds(dpy, 1, ctypes.byref(spec), ctypes.byref(n), ctypes.byref(vals)) != 0:
            return 0  # returns X's Success (0) when it worked
        pid = 0
        for i in range(n.value):
            p = xres.XResGetClientPid(ctypes.byref(vals[i]))
            if p > 0:
                pid = p
        xres.XResClientIdsDestroy(n, vals)
        return pid
    except Exception:
        return 0
    finally:
        x11.XCloseDisplay(dpy)


def request_close(wid: int) -> bool:
    """Ask a window to close the way a WM would (WM_DELETE_WINDOW), so the app can still ask about
    unsaved changes. False when the window doesn't take part in that protocol."""
    if "WM_DELETE_WINDOW" not in _prop(wid, "WM_PROTOCOLS"):
        return False
    from Xlib import X, display as xdisplay, protocol  # pure Python: a dead server can't exit us
    d = xdisplay.Display(_display or os.environ.get("DISPLAY"))
    try:
        w = d.create_resource_object("window", wid)
        ev = protocol.event.ClientMessage(window=w, client_type=d.intern_atom("WM_PROTOCOLS"),
                                          data=(32, [d.intern_atom("WM_DELETE_WINDOW"), X.CurrentTime, 0, 0, 0]))
        w.send_event(ev, event_mask=X.NoEventMask)
        d.flush()
    finally:
        d.close()
    return True


def _prop(wid: int, name: str) -> str:
    try:
        return run("xprop", "-id", str(wid), name, timeout=5)
    except Exception:
        return ""


def _list_windows_plain() -> list[Win]:
    """Viewable top-level windows straight from the X server (no WM), in stacking order bottom->top."""
    wins = []
    out = run("xwininfo", "-root", "-children")
    for m in re.finditer(r"^\s+(0x[0-9a-f]+) (?:\"(.*?)\"|\(has no name\))", out, re.M):
        wid = int(m.group(1), 16)
        try:
            info = run("xwininfo", "-id", str(wid), timeout=5)
        except Exception:
            continue
        if "IsViewable" not in info or "Override Redirect State: yes" in info:
            continue
        pm = re.search(r"= (\d+)", _prop(wid, "_NET_WM_PID"))
        cm = re.findall(r'"([^"]*)"', _prop(wid, "WM_CLASS"))
        w = Win(wid, 0, int(pm.group(1)) if pm else xres_pid(wid), ".".join(cm), m.group(2) or "")
        try:
            w.x, w.y, w.w, w.h = geometry(wid)
        except Exception:
            pass
        if w.w > 1 and w.h > 1:
            wins.append(w)
    return wins


def list_windows() -> list[Win]:
    if not has_wm():
        return _list_windows_plain()
    wins = []
    # WM_CLASS may contain spaces, so anchor on the client-machine column (hostname or N/A).
    host = re.escape(socket.gethostname())
    pat = re.compile(rf"^(0x[0-9a-f]+)\s+(-?\d+)\s+(\d+)\s+(.*?)\s+(?:{host}|N/A)(?: (.*))?$")
    for line in run("wmctrl", "-lpx").splitlines():
        m = pat.match(line)
        if not m:
            continue
        wid, desk, pid, wm_class, title = m.groups()
        w = Win(int(wid, 16), int(desk), int(pid) or xres_pid(int(wid, 16)), wm_class, title or "")
        if w.desktop == -1 and _is_desktop_or_dock(w.id):
            continue
        try:
            w.x, w.y, w.w, w.h = geometry(w.id)
        except Exception:
            pass
        wins.append(w)
    return wins


def resolve_window(target: str | None) -> Win:
    """None/'' -> active window; '0x..'/digits -> id; otherwise substring of title or app class."""
    wins = list_windows()
    if not target:
        aid = active_window_id()
        for w in wins:
            if w.id == aid:
                return w
        if aid:
            # Active window not managed by wmctrl (e.g. a popup); synthesize one.
            x, y, ww, hh = geometry(aid)
            return Win(aid, 0, 0, "", run("xdotool", "getwindowname", str(aid)).strip(), x, y, ww, hh)
        raise RuntimeError("no active window")
    t = target.strip()
    if t.lower().startswith("pid:"):  # a process we (or anyone) started: its window or its children's
        pids = descendants(int(t[4:]))
        hits = [w for w in wins if w.pid in pids]
        if not hits:
            raise RuntimeError(f"no window belongs to {t} (or its child processes) yet")
        aid = active_window_id()
        return next((w for w in hits if w.id == aid), hits[-1])
    if re.fullmatch(r"0x[0-9a-fA-F]+|\d+", t):
        n = int(t, 16) if t.lower().startswith("0x") else int(t)
        for w in wins:
            if w.id == n:
                return w
    tl = t.lower()
    for key in (lambda w: w.title.lower() == tl, lambda w: tl in w.title.lower(), lambda w: tl in w.wm_class.lower()):
        hits = [w for w in wins if key(w)]
        if hits:
            aid = active_window_id()
            return next((w for w in hits if w.id == aid), hits[0])
    raise RuntimeError(f"no window matches {target!r}; call windows() to list them")


def transients_of(wid: int) -> list[tuple[int, int, int, int]]:
    """Geometries of mapped windows whose WM_TRANSIENT_FOR is wid (its dialogs)."""
    out = []
    try:
        ids = run("xdotool", "search", "--onlyvisible", "--pid", str(_pid_of(wid)), "--name", ".*").split()
    except Exception:
        return out
    for i in ids:
        try:
            t = run("xprop", "-id", i, "WM_TRANSIENT_FOR")
        except Exception:
            continue
        m = re.search(r"window id # (0x[0-9a-f]+)", t)
        if m and int(m.group(1), 16) == wid:
            try:
                out.append(geometry(int(i)))
            except Exception:
                pass
    return out


def _pid_of(wid: int) -> int:
    return int(run("xdotool", "getwindowpid", str(wid)).strip())


def monitors() -> list[tuple[str, int, int, int, int]]:
    if _display:  # a sandbox: one screen, xrandr may not be there
        w, h = (int(v) for v in run("xdotool", "getdisplaygeometry").split())
        return [("sandbox", 0, 0, w, h)]
    mons = []
    for line in run("xrandr", "--listmonitors").splitlines()[1:]:
        m = re.search(r"(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)\s+(\S+)", line)
        if m:
            w, h, x, y, name = m.groups()
            mons.append((name, int(x), int(y), int(w), int(h)))
    return mons


def descendants(pid: int) -> set[int]:
    """pid and every process below it (a shell command's real app is usually a child)."""
    kids: dict[int, list[int]] = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        kids.setdefault(ppid, []).append(int(d))
    out, todo = set(), [pid]
    while todo:
        p = todo.pop()
        if p not in out:
            out.add(p)
            todo += kids.get(p, [])
    return out


def focus_window(w: Win) -> None:
    """Activate w (switching workspace if needed) and verify it really got focus; raise otherwise."""
    if active_window_id() == w.id:
        return
    if not has_wm():  # no WM to ask: raise it and set the X input focus ourselves
        run("xdotool", "windowraise", str(w.id))
        run("xdotool", "windowfocus", "--sync", str(w.id))
        if active_window_id() != w.id:
            raise RuntimeError(f"could not focus {w.hex} ({w.title!r}); input not sent")
        time.sleep(0.05)
        return
    for attempt in range(3):
        if attempt == 0:
            run("wmctrl", "-i", "-a", w.hex)
        else:
            run("xdotool", "windowactivate", str(w.id))
        for _ in range(10):
            time.sleep(0.05)
            if active_window_id() == w.id:
                time.sleep(0.1)  # let the app process the focus-in before input arrives
                return
    raise RuntimeError(f"could not focus {w.hex} ({w.title!r}); input not sent")


def window_at(x: int, y: int) -> Win | None:
    """Topmost managed window containing screen point (x, y) on the current workspace."""
    if not has_wm():
        for w in reversed(_list_windows_plain()):  # top of stack first
            if w.x <= x < w.x + w.w and w.y <= y < w.y + w.h:
                return w
        return None
    try:
        stack = run("xprop", "-root", "_NET_CLIENT_LIST_STACKING").split("#", 1)[1]
        order = [int(t.strip(), 16) for t in stack.split(",") if t.strip()]
        cur = int(run("xdotool", "get_desktop").strip())
    except Exception:
        return None
    wins = {w.id: w for w in list_windows()}
    for wid in reversed(order):  # top of stack first
        w = wins.get(wid)
        if w and w.desktop in (cur, -1) and w.x <= x < w.x + w.w and w.y <= y < w.y + w.h:
            return w
    return None


def _ancestors(pid: int) -> set[int]:
    pids = set()
    while pid > 1:
        pids.add(pid)
        try:
            with open(f"/proc/{pid}/stat") as f:
                pid = int(f.read().rsplit(")", 1)[1].split()[1])
        except OSError:
            break
    return pids


def _own_pids() -> set[int]:
    """Pids whose windows show this Claude session: our ancestors, plus (under tmux) the ancestors of
    every tmux client attached to our session, since the tmux server is not a child of the terminal."""
    pids = _ancestors(os.getpid())
    if os.environ.get("TMUX"):
        try:
            sess = run("tmux", "display", "-p", "#{session_name}").strip()
            for line in run("tmux", "list-clients", "-t", sess, "-F", "#{client_pid}").split():
                pids |= _ancestors(int(line))
        except Exception:
            pass
    return pids


def is_own_terminal(wid: int | None) -> bool:
    """True if wid belongs to the terminal hosting this Claude session (typing there = typing to Claude)."""
    if _display:  # a sandbox display can't hold our terminal
        return False
    if wid is None:
        return True  # can't tell what has focus: don't type blind
    try:
        pid = _pid_of(wid)
    except Exception:
        pid = xres_pid(wid)
    if not pid:
        return True  # unknown owner: fail closed rather than maybe type into Claude
    return pid in _own_pids()  # recomputed: tmux clients come and go


# ---------------------------------------------------------------- input

_KEY_ALIASES = {
    "enter": "Return", "return": "Return", "esc": "Escape", "escape": "Escape", "tab": "Tab",
    "backspace": "BackSpace", "bksp": "BackSpace", "del": "Delete", "delete": "Delete", "ins": "Insert",
    "insert": "Insert", "space": "space", "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "pageup": "Prior", "pgup": "Prior", "pagedown": "Next", "pgdn": "Next", "home": "Home", "end": "End",
    "win": "super", "meta": "super", "cmd": "super", "super": "super", "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt", "shift": "shift", "menu": "Menu", "printscreen": "Print",
}


def normalize_keys(keys: str) -> list[str]:
    """'ctrl+L enter' -> ['ctrl+l', 'Return'] in xdotool keysym syntax."""
    combos = []
    for combo in keys.split():
        parts = combo.split("+")
        out = []
        for p in parts:
            pl = p.lower()
            if pl in _KEY_ALIASES:
                out.append(_KEY_ALIASES[pl])
            elif re.fullmatch(r"f\d{1,2}", pl):
                out.append(pl.upper())
            elif len(p) == 1 and len(parts) > 1:
                out.append(p.lower())  # ctrl+L should be ctrl+l, not ctrl+shift+l
            else:
                out.append(p)
        combos.append("+".join(out))
    return combos


# How long a key or button stays down. Apps that poll input once per frame (games, raylib/GLFW/SDL,
# canvas UIs) never see a press and release that land within one frame, as plain `xdotool click`
# and `xdotool key` do; 60 ms spans several frames even at 30 fps.
HOLD_MS = 60


def key(keys: str, repeat: int = 1, hold_ms: int = HOLD_MS) -> None:
    combos = normalize_keys(keys)
    for _ in range(max(1, repeat)):
        for c in combos:
            if hold_ms <= 0:
                run("xdotool", "key", "--clearmodifiers", c)
            else:
                # Down, hold, up in ONE xdotool process, without --clearmodifiers: on keyup it would
                # "restore" the modifier our own keydown pressed, leaving e.g. Ctrl stuck down.
                try:
                    run("xdotool", "keydown", c, "sleep", f"{hold_ms / 1000:.3f}", "keyup", c)
                except Exception:
                    try:
                        run("xdotool", "keyup", c)  # never leave a key held
                    except Exception:
                        pass  # display gone: keep the original error
                    raise
            time.sleep(0.04)


def type_text(text: str) -> None:
    # xdotool's own newline handling is unreliable, so press Return between lines explicitly.
    for n, line in enumerate(text.split("\n")):
        if n:
            run("xdotool", "key", "--clearmodifiers", "Return")
        for i in range(0, len(line), 200):  # chunk to avoid dropped characters on long strings
            run("xdotool", "type", "--clearmodifiers", "--delay", "6", "--", line[i:i + 200], timeout=120)


_BUTTONS = {"left": "1", "middle": "2", "right": "3"}


def click(x: int, y: int, button: str = "left", count: int = 1, hold_ms: int = HOLD_MS) -> None:
    move(x, y)
    b = _BUTTONS.get(button, button)
    if hold_ms <= 0:
        run("xdotool", "click", "--repeat", str(max(1, count)), "--delay", "80", b)
        return
    # One xdotool process for all presses: exact timing, so a double-click (hold capped at 80 ms,
    # 60 ms apart) stays inside even a short double-click interval.
    hold = (min(hold_ms, 80) if count > 1 else hold_ms) / 1000
    chain: list[str] = []
    for i in range(max(1, count)):
        chain += (["sleep", "0.06"] if i else []) + ["mousedown", b, "sleep", f"{hold:.3f}", "mouseup", b]
    try:
        run("xdotool", *chain)
    except Exception:
        try:
            run("xdotool", "mouseup", b)  # never leave a button held
        except Exception:
            pass  # display gone: keep the original error
        raise


def move(x: int, y: int) -> None:
    # Not --sync: it waits for a motion event, which never comes if the pointer is already there.
    run("xdotool", "mousemove", str(x), str(y))
    time.sleep(0.05)


def scroll(direction: str, amount: int = 3, x: int | None = None, y: int | None = None) -> None:
    btn = {"up": "4", "down": "5", "left": "6", "right": "7"}[direction]
    if x is not None and y is not None:
        move(x, y)
    run("xdotool", "click", "--repeat", str(max(1, amount)), "--delay", "30", btn)


def drag(x1: int, y1: int, x2: int, y2: int, button: str = "left") -> None:
    b = _BUTTONS.get(button, button)
    move(x1, y1)
    run("xdotool", "mousedown", b)
    for i in range(1, 11):  # intermediate motion so apps register a drag
        move(x1 + (x2 - x1) * i // 10, y1 + (y2 - y1) * i // 10)
        time.sleep(0.02)
    run("xdotool", "mouseup", b)


def mouse_location() -> tuple[int, int]:
    out = run("xdotool", "getmouselocation", "--shell")
    vals = dict(l.split("=", 1) for l in out.split())
    return int(vals["X"]), int(vals["Y"])


def clipboard_get() -> str:
    try:
        return run("xclip", "-selection", "clipboard", "-o", timeout=5)
    except RuntimeError:
        return ""


def clipboard_set(text: str) -> None:
    # xclip forks to serve the selection; don't wait on its stdout.
    p = subprocess.Popen(["xclip", "-selection", "clipboard", "-i"], stdin=subprocess.PIPE, env=env(),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p.communicate(text.encode(), timeout=5)
