"""X11 helpers: window listing/management and raw input via wmctrl + xdotool."""
from __future__ import annotations

import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass


def run(*args: str, input: str | None = None, timeout: float = 15) -> str:
    r = subprocess.run(args, capture_output=True, text=True, input=input, timeout=timeout)
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


def active_window_id() -> int | None:
    try:
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


def list_windows() -> list[Win]:
    wins = []
    # WM_CLASS may contain spaces, so anchor on the client-machine column (hostname or N/A).
    host = re.escape(socket.gethostname())
    pat = re.compile(rf"^(0x[0-9a-f]+)\s+(-?\d+)\s+(\d+)\s+(.*?)\s+(?:{host}|N/A)(?: (.*))?$")
    for line in run("wmctrl", "-lpx").splitlines():
        m = pat.match(line)
        if not m:
            continue
        wid, desk, pid, wm_class, title = m.groups()
        w = Win(int(wid, 16), int(desk), int(pid), wm_class, title or "")
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
    mons = []
    for line in run("xrandr", "--listmonitors").splitlines()[1:]:
        m = re.search(r"(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)\s+(\S+)", line)
        if m:
            w, h, x, y, name = m.groups()
            mons.append((name, int(x), int(y), int(w), int(h)))
    return mons


def focus_window(w: Win) -> None:
    """Activate w (switching workspace if needed) and verify it really got focus; raise otherwise."""
    if active_window_id() == w.id:
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
    if wid is None:
        return False
    try:
        return _pid_of(wid) in _own_pids()  # recomputed: tmux clients come and go
    except Exception:
        return False


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


def key(keys: str, repeat: int = 1) -> None:
    combos = normalize_keys(keys)
    for _ in range(max(1, repeat)):
        run("xdotool", "key", "--clearmodifiers", "--delay", "40", *combos)


def type_text(text: str) -> None:
    # xdotool's own newline handling is unreliable, so press Return between lines explicitly.
    for n, line in enumerate(text.split("\n")):
        if n:
            run("xdotool", "key", "--clearmodifiers", "Return")
        for i in range(0, len(line), 200):  # chunk to avoid dropped characters on long strings
            run("xdotool", "type", "--clearmodifiers", "--delay", "6", "--", line[i:i + 200], timeout=120)


_BUTTONS = {"left": "1", "middle": "2", "right": "3"}


def click(x: int, y: int, button: str = "left", count: int = 1) -> None:
    move(x, y)
    run("xdotool", "click", "--repeat", str(max(1, count)), "--delay", "80", _BUTTONS.get(button, button))


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
    p = subprocess.Popen(["xclip", "-selection", "clipboard", "-i"], stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    p.communicate(text.encode(), timeout=5)
