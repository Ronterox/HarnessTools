"""deskpilot MCP server: drive the whole Linux/X11 desktop with text-first, token-cheap tools."""
from __future__ import annotations

import os
import subprocess
import threading
import time

import functools

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import a11y, osd, procs, vision, xutil

INSTRUCTIONS = """Control any app on this Linux (X11/GNOME) desktop. Cheapest first:
1. ui(window) -> accessibility tree as text with refs like [e12]; then act(ref, 'click'|'set_text'|...).
   Works for GTK/GNOME apps, Firefox, Qt (with a11y), LibreOffice. Use query= to search instead of dumping.
2. read_screen(window, find=) -> OCR text lines with screen coords; then input('click', x=, y=).
3. screenshot(window|region, grid=100) -> downscaled JPEG (~300-800 tokens). Use only when layout/visuals matter.
Coordinates are absolute screen pixels (input/sequence take coords='window' for window-relative).
Windows are targeted by id (0x...), 'pid:N' (a launched process), or title/app substring; omitted = active.
input() is raw keyboard/mouse (presses are held 60 ms so per-frame-polling apps see them);
input(click, find='text') clicks OCR'd text. sequence() runs timed steps + snapshots in one call;
wait() blocks until a window/text appears or goes. Testing your own GUI app? Launch it with
window(launch, display='sandbox'): a private X display, nothing touches the user's desktop."""

mcp = MCPServer("deskpilot", instructions=INSTRUCTIONS, log_level="WARNING")


# Tool calls can arrive concurrently (each runs in a worker thread). There's one pointer, one focus
# and one current display, and a call checks them before acting (focus, own terminal), so calls run
# one at a time: nothing can switch the display or focus between a call's check and its action.
_CALL_LOCK = threading.RLock()


def tool(fn):
    """Register fn as a tool; surface our own error messages to the model instead of a generic one."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            with _CALL_LOCK:
                return fn(*a, **kw)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"{type(e).__name__}: {e}") from e
    return mcp.tool()(wrapper)


def _status() -> str:
    try:
        wid = xutil.active_window_id()
        return f"active window: {xutil.run('xdotool', 'getwindowname', str(wid)).strip()!r} (0x{wid:08x})"
    except Exception:
        return "active window: ?"


def _rect(window: str | None, region: str | None) -> tuple[int, int, int, int]:
    if region:
        r = region.strip().lower()
        if r in ("screen", "all", "desktop"):
            mons = xutil.monitors()
            x1 = min(m[1] for m in mons); y1 = min(m[2] for m in mons)
            x2 = max(m[1] + m[3] for m in mons); y2 = max(m[2] + m[4] for m in mons)
            return x1, y1, x2 - x1, y2 - y1
        for name, x, y, w, h in xutil.monitors():
            if r == name.lower():
                return x, y, w, h
        parts = [int(float(p)) for p in r.replace(" ", "").split(",")]
        if len(parts) != 4:
            raise RuntimeError("region must be 'x,y,w,h', a monitor name, or 'screen'")
        return tuple(parts)  # type: ignore[return-value]
    w = xutil.resolve_window(window)
    return w.x, w.y, w.w, w.h


@tool
def windows() -> str:
    """List open windows (id, workspace, app, geometry, title) and monitors. '*' marks the active window."""
    aid = xutil.active_window_id()
    lines = ["monitors: " + ", ".join(f"{n} {w}x{h}+{x}+{y}" for n, x, y, w, h in xutil.monitors())]
    try:
        cur = xutil.run("xdotool", "get_desktop").strip()
        lines.append(f"current workspace: {cur}")
    except Exception:
        pass
    for w in xutil.list_windows():
        star = "*" if w.id == aid else " "
        lines.append(f"{star}{w.hex} ws{w.desktop} {w.app:<14} {w.w}x{w.h}+{w.x}+{w.y}  "
                     f"{w.title or '(untitled: likely a dialog)'}")
    return "\n".join(lines)


@tool
def window(action: str, target: str | None = None, x: int | None = None, y: int | None = None,
           w: int | None = None, h: int | None = None, cwd: str | None = None,
           env: dict[str, str] | None = None, display: str | None = None, timeout: float = 15) -> str:
    """Manage windows. action: focus | close | minimize | maximize | unmaximize | fullscreen | move | resize
    | launch. target: window id / 'pid:N' / title or app substring (default active). move uses x,y;
    resize uses w,h.
    launch: target is a shell command run detached (e.g. 'gnome-text-editor', 'firefox https://x.com');
      optional cwd, env (extra variables). Returns its pid and the window it opened (found by pid, so
      target it as 'pid:N' afterwards); stdout+stderr go to a log, see process(). display='sandbox' runs
      it on a private X display (started on demand, see display()) and switches all tools there;
      display='desktop' the real one. Default: whichever display is current."""
    a = action.lower()
    if a == "launch":
        if not target:
            raise RuntimeError("launch needs target=<command>")
        if display:
            _switch_display(display)
        before = {w.id for w in xutil.list_windows()}
        p = procs.launch(target, cwd=cwd, env=env)
        where = f" on sandbox {p.display}" if p.display else ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.3)
            try:
                win = xutil.resolve_window(f"pid:{p.pid}")
                time.sleep(0.4)  # let it finish mapping/painting
                return (f"launched pid {p.pid}{where}; window {win.hex} {win.app} {win.title!r} "
                        f"{win.w}x{win.h}+{win.x}+{win.y}; log {p.log}")
            except RuntimeError:
                # Opened by another process (D-Bus activation, a launcher that hands off)?
                new = [w for w in xutil.list_windows() if w.id not in before]
                if new and time.time() - p.started > 2:
                    w0 = new[-1]
                    return (f"launched pid {p.pid}{where}; no window of its own yet, but a new window "
                            f"appeared, owned by another process (pid {w0.pid or '?'}), possibly opened for "
                            f"it (D-Bus activation, a launcher): {w0.hex} {w0.app} {w0.title!r}; log {p.log}")
                if p.popen.poll() is not None and not new:
                    return (f"launched pid {p.pid}{where}; it {p.status()} without a window. "
                            f"Log tail:\n{procs.tail(p.log, 20)}")
        return (f"launched pid {p.pid}{where}; no window of its own after {timeout:g}s (single-instance apps "
                f"hand off to an existing window; check windows()). log {p.log}")
    win = xutil.resolve_window(target)
    hx = win.hex
    if a in ("focus", "activate", "raise"):
        xutil.focus_window(win)
    elif a == "close":
        if xutil.has_wm():
            xutil.run("wmctrl", "-i", "-c", hx)
        elif not xutil.request_close(win.id):  # no WM: ask the app ourselves; if it can't be asked,
            mine = [p for p in procs.PROCS.values() if win.pid in xutil.descendants(p.pid)]
            if mine:  # end the process we launched, else destroy the window
                procs.kill(mine[0].pid)
            else:
                xutil.run("xdotool", "windowclose", str(win.id))
    elif not xutil.has_wm() and a in ("move", "resize"):  # sandbox: no WM, move it ourselves
        if x is not None or y is not None:
            xutil.run("xdotool", "windowmove", str(win.id), str(x if x is not None else win.x),
                      str(y if y is not None else win.y))
        if w is not None or h is not None:
            xutil.run("xdotool", "windowsize", str(win.id), str(w or win.w), str(h or win.h))
    elif not xutil.has_wm():
        raise RuntimeError(f"{a} needs a window manager; this display ({xutil.current_display()}) has none")
    elif a == "minimize":
        xutil.run("xdotool", "windowminimize", str(win.id))
    elif a == "maximize":
        xutil.run("wmctrl", "-i", "-r", hx, "-b", "add,maximized_vert,maximized_horz")
    elif a in ("unmaximize", "restore"):
        xutil.run("wmctrl", "-i", "-r", hx, "-b", "remove,maximized_vert,maximized_horz,fullscreen")
    elif a == "fullscreen":
        xutil.run("wmctrl", "-i", "-r", hx, "-b", "toggle,fullscreen")
    elif a in ("move", "resize"):
        xutil.run("wmctrl", "-i", "-r", hx, "-b", "remove,maximized_vert,maximized_horz")
        gx = x if x is not None else -1
        gy = y if y is not None else -1
        gw = w if w is not None else -1
        gh = h if h is not None else -1
        xutil.run("wmctrl", "-i", "-r", hx, "-e", f"0,{gx},{gy},{gw},{gh}")
    else:
        raise RuntimeError(f"unknown window action {action!r}")
    time.sleep(0.3)
    return f"{a} {hx} ok; " + _status()


@tool
def ui(window: str | None = None, query: str | None = None, root: str | None = None,
       interactive_only: bool = False, coords: bool = False, max_lines: int = 250,
       depth: int | None = None, text_limit: int = 120) -> str:
    """Accessibility-tree snapshot of a window as compact text. Lines: [ref] role "name" value=… (states).
    Plain lines are visible text; '# ' marks headings. Refs stay valid across calls while the element lives.
    query: case-insensitive search of names/text/roles -> flat matches with @x,y centers (cheapest way to
      locate one thing). root: a ref to expand only that subtree. interactive_only: just controls.
    coords: append @x,y element centers. If an app has no tree, use read_screen/screenshot instead."""
    if root:
        acc, wid = a11y.REFS.get(root)
        if query:
            return a11y.search(acc, wid, query)
        return a11y.snapshot(acc, wid, max_lines=max_lines, depth=depth, interactive_only=interactive_only,
                             coords=coords, text_limit=text_limit)
    win = xutil.resolve_window(window)
    frame = a11y.find_frame(win)
    if frame is None:
        return (f"No accessibility tree for {win.app!r} window {win.title!r} ({win.hex}). Use "
                f"read_screen() (OCR) or screenshot(). Electron/Chromium apps expose a tree only if started "
                f"with --force-renderer-accessibility (or env ACCESSIBILITY_ENABLED=1).")
    roots = [frame] + a11y.modal_dialogs(frame, win.id)
    parts = []
    for i, r in enumerate(roots):
        if query:
            body = a11y.search(r, win.id, query)
            if i and body.startswith("no showing element"):
                continue
        else:
            body = a11y.snapshot(r, win.id, max_lines=max_lines, depth=depth,
                                 interactive_only=interactive_only, coords=coords, text_limit=text_limit)
        parts.append(("--- modal dialog (blocks the window) ---\n" if i else "") + body)
    if query and len(parts) > 1 and parts[0].startswith("no showing element"):
        parts = parts[1:]
    s = "\n".join(parts)
    if not query and len(roots) == 1 and s.count("\n") < 3:
        s += ("\n(note: tree is nearly empty — app may not expose accessibility. Electron/Chromium: relaunch "
              "with --force-renderer-accessibility. Otherwise use read_screen() or screenshot().)")
    return s


@tool
def act(ref: str, action: str = "click", text: str | None = None) -> str:
    """Act on an element from ui(). action:
    click (a11y action, falls back to mouse) | double_click | right_click | mouse_click | hover | focus |
    set_text (replace field contents; needs text) | type (append text) | toggle | check | uncheck |
    expand | collapse | select | scroll_into_view | read (full text of the subtree, untruncated) |
    actions (list the element's native actions) | <any native action name>."""
    if action.lower() in ("read", "text", "actions"):
        return a11y.act(ref, action, text)
    what, cx, cy = a11y.describe(ref)
    verb = {"set_text": "filling", "type": "typing into", "click": "clicking"}.get(action.lower(), action)
    osd.show(f"{verb} {what}", cx, cy)
    res = a11y.act(ref, action, text)
    time.sleep(0.35)
    return f"{res}; {_status()}"


def _point(target: xutil.Win | None, x: int | None, y: int | None, coords: str) -> tuple[int | None, int | None]:
    """x,y in screen pixels; coords='window' means relative to target's top-left."""
    if x is None or y is None or coords == "screen":
        return x, y
    if coords != "window":
        raise RuntimeError("coords must be 'screen' or 'window'")
    if target is None:
        raise RuntimeError("coords='window' needs window=")
    return target.x + x, target.y + y


def _find_on(target: xutil.Win | None, find: str) -> tuple[int, int, str]:
    """Screen point of OCR'd text in the window (or the whole sandbox screen). The window is raised
    and focused first, so what is read is the window itself, not something covering it."""
    if target is None and not xutil.current_display():
        raise RuntimeError("find= on the desktop needs window= (the whole screen includes Claude's own "
                           "terminal); nothing sent")
    if target is not None:
        xutil.focus_window(target)
        time.sleep(0.15)  # let it repaint once raised
    osd.hide()
    rect = (target.x, target.y, target.w, target.h) if target else _rect(None, "screen")
    return vision.locate(vision.ocr_lines(rect), find)


def _do_input(a: str, *, keys=None, text=None, x=None, y=None, x2=None, y2=None, button="left",
              count=1, direction="down", amount=3, window=None, hold_ms=xutil.HOLD_MS,
              coords="screen", find=None) -> str:
    """One raw input action with every safety check; shared by input() and sequence()."""
    if a == "mouse":
        mx, my = xutil.mouse_location()
        return f"pointer at {mx},{my}; {_status()}"
    if a not in ("key", "type", "click", "move", "scroll", "drag"):
        raise RuntimeError(f"unknown input action {a!r}")

    pointer = a in ("click", "scroll", "drag", "move")
    target = xutil.resolve_window(window) if window else None
    found = ""
    if find:
        if a not in ("click", "move"):
            raise RuntimeError("find= works with click and move")
        x, y, txt = _find_on(target, find)
        found = f" on {txt!r}"
    else:
        x, y = _point(target, x, y, coords)
        x2, y2 = _point(target, x2, y2, coords)
    if a in ("click", "drag") and (x is None or y is None):
        raise RuntimeError(f"{a} needs x and y (or find=)")
    if target is not None:
        if pointer and x is None:  # scroll/move without coords: middle of the target window
            x, y = target.x + target.w // 2, target.y + target.h // 2
    elif x is not None and y is not None:
        target = xutil.window_at(x, y)  # current workspace only; pass window= to be sure
    if target is not None and a != "move":
        xutil.focus_window(target)  # switches workspace if needed, verifies focus
    if window and pointer and target is not None:
        under = xutil.window_at(x, y)
        if under is None or under.id != target.id:
            raise RuntimeError(f"point {x},{y} is not on {target.hex} ({target.title!r}) but on "
                               f"{under.title if under else 'nothing'!r}; nothing sent")
    if a in ("key", "type"):
        aid = xutil.active_window_id()
        if xutil.is_own_terminal(aid):
            raise RuntimeError("refusing: the focused window is the terminal running Claude itself"
                               + ("" if window else "; pass window= to target the app you mean"))

    px, py = (x, y) if x is not None else (target.x + target.w // 2, target.y) if target else (None, None)
    if a == "key":
        if not keys:
            raise RuntimeError("key needs keys=")
        osd.show(f"pressing {keys}", px, py)
        xutil.key(keys, count, hold_ms)
    elif a == "type":
        if text is None:
            raise RuntimeError("type needs text=")
        preview = text if len(text) <= 40 else text[:37] + "…"
        osd.show(f"typing \"{preview}\"".replace("\n", "⏎"), px, py)
        xutil.type_text(text)
    elif a == "click":
        osd.show(f"{'double-' if count == 2 else ''}{button} click at {x},{y}{found}", px, py)
        xutil.click(x, y, button, count, hold_ms)
    elif a == "move":
        xutil.move(x, y)
    elif a == "scroll":
        osd.show(f"scrolling {direction} ×{amount}", px, py)
        xutil.scroll(direction, amount, x, y)
    elif a == "drag":
        if x2 is None or y2 is None:
            raise RuntimeError("drag needs x2 and y2")
        osd.show(f"dragging {x},{y} → {x2},{y2}", px, py)
        xutil.drag(x, y, x2, y2, button)
    went = f"; went to {target.title!r}" if target is not None and not window else ""
    at = f" at {x},{y}{found}" if a in ("click", "move") else ""
    return f"{a}{at} ok{went}"


@tool
def input(action: str, keys: str | None = None, text: str | None = None, x: int | None = None,
          y: int | None = None, x2: int | None = None, y2: int | None = None, button: str = "left",
          count: int = 1, direction: str = "down", amount: int = 3, window: str | None = None,
          hold_ms: int = xutil.HOLD_MS, coords: str = "screen", find: str | None = None) -> str:
    """Raw keyboard/mouse. ALWAYS pass window= (id/'pid:N'/title): it is focused (switching workspace if
    needed) and verified before anything is sent, and pointer coords must lie on it. scroll/move without
    x,y use the window's center. Without window=, input goes to whatever is at x,y / focused now. Keys/text are refused if they would land in Claude's own terminal. action:
    key (keys='ctrl+l', 'enter', 'alt+tab', several space-separated; count repeats) |
    type (text=; types literally, \\n = Enter) | click (x,y or find=; button left/right/middle; count=2
    double) | move (x,y or find=) | scroll (direction up/down/left/right, amount notches, optional x,y) |
    drag (x,y -> x2,y2) | mouse (report pointer position).
    hold_ms: how long keys/buttons stay down (default 60, so apps that poll input once per frame see
    them; 0 = instant). coords='window': x,y (and x2,y2) relative to window='s top-left.
    find='text': click/move on that text, located by OCR in window= (or the whole screen)."""
    out = _do_input(action.lower(), keys=keys, text=text, x=x, y=y, x2=x2, y2=y2, button=button,
                    count=count, direction=direction, amount=amount, window=window, hold_ms=hold_ms,
                    coords=coords, find=find)
    time.sleep(0.3)
    return f"{out}; {_status()}"


def _switch_display(display: str) -> str:
    d = display.lower()
    if d == "sandbox":
        sb = procs.start_sandbox()
        xutil.use_display(sb.display)
        return f"sandbox {sb.display} ({sb.server}, {sb.size})"
    if d in ("desktop", "real", "host"):
        xutil.use_display(None)
        return "desktop"
    raise RuntimeError("display must be 'sandbox' or 'desktop'")


@tool
def display(action: str = "status", size: str | None = None, visible: bool = False) -> str:
    """Which X display the tools drive. action:
    status | sandbox (start a private X display if needed and drive it: apps launched there and all
      input/OCR/screenshots stay off the user's desktop; size WxH, default 1600x900 or the running
      sandbox's; visible=True shows it as a window on the desktop, needs Xephyr) | desktop (back to the user's screen; the sandbox keeps running) |
      stop (kill the sandbox and the apps launched in it).
    A sandbox has no window manager: windows get no frames, minimize/maximize aren't available."""
    a = action.lower()
    if a == "stop":
        return procs.stop_sandbox()
    if a == "sandbox":
        sb = procs.SANDBOX
        if sb and sb.popen.poll() is None and ((size and sb.size != size.lower())
                                                 or (visible and sb.server != "Xephyr")):
            procs.stop_sandbox()  # asked for a different one: restart it
        sb = procs.start_sandbox(size or "1600x900", visible)
        xutil.use_display(sb.display)
        return f"driving sandbox {sb.display} ({sb.server}, {sb.size}); launch apps with window(launch, ...)"
    if a == "desktop":
        return "driving the " + _switch_display("desktop")
    if a != "status":
        raise RuntimeError(f"unknown display action {action!r}")
    sb = procs.SANDBOX
    cur = xutil.current_display()
    return (f"driving {'sandbox ' + cur if cur else 'the desktop'}; sandbox: "
            + (f"{sb.display} ({sb.server}, {sb.size})" if sb and sb.popen.poll() is None else "none"))


@tool
def process(action: str = "list", pid: int | None = None, lines: int = 40) -> str:
    """Processes started with window(launch). action:
    list | status (pid) | log (pid; last `lines` lines of its stdout+stderr) | kill (pid; its whole
    process group: SIGTERM, then SIGKILL)."""
    a = action.lower()
    if a == "list":
        if not procs.PROCS:
            return "no processes launched"
        return "\n".join(f"pid {p.pid}  {p.status():<20} {'sandbox ' + p.display if p.display else 'desktop'}  "
                         f"{p.command[:80]!r}  log {p.log}" for p in list(procs.PROCS.values()))
    if pid is None:
        raise RuntimeError(f"{a} needs pid=")
    p = procs.get(pid)
    if a == "status":
        return f"pid {pid} {p.status()}, up {time.time() - p.started:.0f}s; log {p.log}"
    if a == "log":
        return f"pid {pid} {p.status()}; {p.log}:\n{procs.tail(p.log, lines)}"
    if a == "kill":
        return f"pid {pid} {procs.kill(pid)}"
    raise RuntimeError(f"unknown process action {action!r}")


def _wait_check(until: str, target: str | None, text: str | None, window: str | None) -> str | None:
    """None while the condition doesn't hold yet; a description once it does."""
    if until == "window":
        try:
            w = xutil.resolve_window(target)
            return f"window {w.hex} {w.title!r}"
        except RuntimeError:
            return None
    if until == "gone":
        if target and target.lower().startswith("pid:"):
            p = procs.PROCS.get(int(target[4:]))
            if p and p.popen.poll() is not None:
                return f"pid {p.pid} {p.status()}"
        try:
            xutil.resolve_window(target)
            return None
        except RuntimeError:
            return f"no window matches {target!r}"
    if until in ("text", "no_text"):
        win = xutil.resolve_window(window) if window else None
        osd.hide()
        rect = (win.x, win.y, win.w, win.h) if win else _rect(None, "screen")
        hits = vision.match(vision.ocr_lines(rect), text or "")
        if until == "text":
            return f"{text!r} at {hits[0][0]},{hits[0][1]}" if hits else None
        return None if hits else f"{text!r} gone"
    raise RuntimeError("until must be window | gone | text | no_text")


def _wait(until: str, target: str | None = None, text: str | None = None, window: str | None = None,
          timeout: float = 10) -> str:
    u = until.lower()
    if u in ("text", "no_text") and not text:
        raise RuntimeError(f"until={u} needs text=")
    if u in ("window", "gone") and not target:
        raise RuntimeError(f"until={u} needs target=")
    t0 = time.time()
    while True:
        got = _wait_check(u, target, text, window)
        if got:
            return f"{got} after {time.time() - t0:.1f}s"
        if time.time() - t0 > timeout:
            raise RuntimeError(f"timed out after {timeout:g}s waiting for {u} {target or text!r}")
        time.sleep(0.3)


@tool
def wait(until: str, target: str | None = None, text: str | None = None, window: str | None = None,
         timeout: float = 10) -> str:
    """Block until a condition holds (instead of guessing sleeps), polling every ~0.3 s. until:
    window (target: id/'pid:N'/title appears) | gone (target window closes, or 'pid:N' exits) |
    text (OCR finds text= in window=, or the whole screen) | no_text (it disappears).
    Errors after timeout seconds."""
    return f"{_wait(until, target, text, window, timeout)}; {_status()}"


_STEP_KEYS = {"keys", "text", "x", "y", "x2", "y2", "button", "count", "direction", "amount", "hold_ms",
              "find", "coords"}


@tool
def sequence(steps: list[dict], window: str | None = None, coords: str = "screen",
             max_side: int = 1400, shot_side: int = 640) -> list:
    """Run several steps in one call with exact timing, and get every snapshot back as ONE image strip
    (far cheaper than a screenshot per step). Stops at the first failing step and reports it.
    window= (id/'pid:N'/title) is the target of every step, focused and verified as in input();
    coords='window' makes x,y window-relative. Each step is a dict with "do" plus input() params:
      {"do": "click", "x": 200, "y": 300} | {"do": "click", "find": "Save"} |
      {"do": "key", "keys": "Escape"} | {"do": "type", "text": "hi"} | {"do": "scroll", ...} |
      {"do": "drag", ...} | {"do": "move", ...} |
      {"do": "wait", "ms": 500} | {"do": "wait", "until": "text", "text": "Done", "timeout": 5} |
      {"do": "shot", "label": "after click"} (snapshot of the window, or the screen) |
      {"do": "check"} (fail unless the window still exists, and is the same window).
    Returns the step log and the strip."""
    log, frames, labels = [], [], []
    failed = None
    win0 = xutil.resolve_window(window) if window else None
    for i, st in enumerate(steps):
        do = str(st.get("do", "")).lower()
        try:
            if do == "wait":
                if "until" in st:
                    got = _wait(st["until"], st.get("target"), st.get("text"), window, st.get("timeout", 10))
                    log.append(f"{i}: wait {got}")
                else:
                    time.sleep(float(st.get("ms", 300)) / 1000)
                    log.append(f"{i}: wait {st.get('ms', 300)}ms")
            elif do == "shot":
                osd.hide()
                w = xutil.resolve_window(window) if window else None
                rect = (w.x, w.y, w.w, w.h) if w else _rect(None, "screen")
                img = vision.grab(rect)
                img.thumbnail((shot_side, shot_side))
                frames.append(img)
                labels.append(f"{i}: {st.get('label', 'shot')}")
                log.append(f"{i}: shot")
            elif do == "check":
                w = xutil.resolve_window(window)
                if win0 and w.id != win0.id:
                    raise RuntimeError(f"window changed: {win0.hex} -> {w.hex}")
                log.append(f"{i}: check ok, {w.hex} alive")
            else:
                extra = set(st) - _STEP_KEYS - {"do", "label"}
                if extra:
                    raise RuntimeError(f"unknown keys {sorted(extra)}")
                kw = {k: v for k, v in st.items() if k in _STEP_KEYS}
                kw.setdefault("coords", coords)
                log.append(f"{i}: " + _do_input(do, window=window, **kw))
                time.sleep(0.15)  # let the app draw before the next step
        except Exception as e:
            failed = f"{i}: {do} FAILED: {e}"
            log.append(failed)
            break
    head = (f"stopped at step {failed}" if failed else f"all {len(steps)} steps ok") + "; " + _status()
    out: list = [head + "\n" + "\n".join(log)]
    if frames:
        data, note = vision.strip(frames, labels, max_side=max_side)
        out += [note, Image(data=data, format="jpeg")]
    return out


@tool
def read_screen(window: str | None = None, region: str | None = None, find: str | None = None) -> str:
    """OCR fallback for apps without an accessibility tree (games, Electron, canvas, remote desktops).
    Returns lines 'x,y  text' where x,y is the text's center in screen coords — clickable via input().
    window: id/title/app (default active). region: 'x,y,w,h' | monitor name | 'screen' (overrides window).
    find: only return lines containing this text."""
    osd.hide()
    return vision.ocr(_rect(window, region), find=find)


@tool
def screenshot(window: str | None = None, region: str | None = None, max_side: int = 1024,
               gray: bool = False, grid: int = 0, quality: int = 70) -> list:
    """Downscaled JPEG of a window/region — last resort; prefer ui() or read_screen().
    Cost ≈ w*h/750 tokens (1024x576 ≈ 790; max_side=640 ≈ 300). Crop with region='x,y,w,h' to see
    detail cheaply. grid=100 overlays labeled lines every 100 screen px for precise clicking.
    Returns a note with the img->screen coordinate mapping."""
    osd.hide()
    data, note = vision.screenshot(_rect(window, region), max_side=max_side, gray=gray, grid=grid,
                                   quality=quality)
    return [note, Image(data=data, format="jpeg")]


@tool
def clipboard(text: str | None = None) -> str:
    """Get the clipboard (no args) or set it (text=). Paste with input('key', keys='ctrl+v')."""
    if text is None:
        c = xutil.clipboard_get()
        return c if len(c) < 20000 else c[:20000] + "\n… (truncated)"
    xutil.clipboard_set(text)
    return f"clipboard set ({len(text)} chars)"


def _bootstrap_env() -> None:
    """MCP clients often start servers with a minimal env; recover the desktop session's variables."""
    uid = os.getuid()
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") and os.path.exists(f"/run/user/{uid}/bus"):
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{uid}/bus"
    if not os.environ.get("XAUTHORITY"):
        for p in (f"/run/user/{uid}/gdm/Xauthority", os.path.expanduser("~/.Xauthority")):
            if os.path.exists(p):
                os.environ["XAUTHORITY"] = p
                break
    if not os.environ.get("DISPLAY"):
        socks = sorted(f for f in os.listdir("/tmp/.X11-unix") if f.startswith("X")) \
            if os.path.isdir("/tmp/.X11-unix") else []
        for s in socks:
            disp = ":" + s[1:]
            try:
                r = subprocess.run(["xdotool", "getdisplaygeometry"], env={**os.environ, "DISPLAY": disp},
                                   capture_output=True, timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                continue  # a hung or missing display must not stop the server from starting
            if r.returncode == 0:
                os.environ["DISPLAY"] = disp
                break


def main():
    procs.install_exit_cleanup()  # main thread: signal handlers can't be set from tool threads
    _bootstrap_env()
    a11y.ensure_enabled()
    mcp.run()


if __name__ == "__main__":
    main()
