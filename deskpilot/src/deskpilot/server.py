"""deskpilot MCP server: drive the whole Linux/X11 desktop with text-first, token-cheap tools."""
from __future__ import annotations

import os
import subprocess
import time

import functools

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import a11y, osd, vision, xutil

INSTRUCTIONS = """Control any app on this Linux (X11/GNOME) desktop. Cheapest first:
1. ui(window) -> accessibility tree as text with refs like [e12]; then act(ref, 'click'|'set_text'|...).
   Works for GTK/GNOME apps, Firefox, Qt (with a11y), LibreOffice. Use query= to search instead of dumping.
2. read_screen(window, find=) -> OCR text lines with screen coords; then input('click', x=, y=).
3. screenshot(window|region, grid=100) -> downscaled JPEG (~300-800 tokens). Use only when layout/visuals matter.
Coordinates everywhere are absolute screen pixels. Windows are targeted by id (0x...), or title/app substring;
omitted = active window. input() is raw xdotool keyboard/mouse for anything else."""

mcp = MCPServer("deskpilot", instructions=INSTRUCTIONS, log_level="WARNING")


def tool(fn):
    """Register fn as a tool; surface our own error messages to the model instead of a generic one."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
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
           w: int | None = None, h: int | None = None) -> str:
    """Manage windows. action: focus | close | minimize | maximize | unmaximize | fullscreen | move | resize
    | launch. target: window id / title or app substring (default active). move uses x,y; resize uses w,h.
    launch: target is a shell command run detached (e.g. 'gnome-text-editor', 'firefox https://x.com')."""
    a = action.lower()
    if a == "launch":
        if not target:
            raise RuntimeError("launch needs target=<command>")
        env = dict(os.environ)
        before = {w.id for w in xutil.list_windows()}
        subprocess.Popen(target, shell=True, start_new_session=True, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 15
        while time.time() < deadline:
            time.sleep(0.5)
            new = [w for w in xutil.list_windows() if w.id not in before]
            if new:
                time.sleep(0.5)  # let it finish mapping/painting
                return f"launched {target!r}; new window {new[0].hex} {new[0].app} {new[0].title!r}"
        return f"launched {target!r}; no new window after 15s (may be single-instance or headless). " + _status()
    win = xutil.resolve_window(target)
    hx = win.hex
    if a in ("focus", "activate", "raise"):
        xutil.focus_window(win)
    elif a == "close":
        xutil.run("wmctrl", "-i", "-c", hx)
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


@tool
def input(action: str, keys: str | None = None, text: str | None = None, x: int | None = None,
          y: int | None = None, x2: int | None = None, y2: int | None = None, button: str = "left",
          count: int = 1, direction: str = "down", amount: int = 3, window: str | None = None) -> str:
    """Raw keyboard/mouse. ALWAYS pass window= (id/title): it is focused (switching workspace if
    needed) and verified before anything is sent, and pointer coords must lie on it. scroll/move without
    x,y use the window's center. Without window=, input goes to whatever is at x,y / focused now. Keys/text are refused if they would land in Claude's own terminal. action:
    key (keys='ctrl+l', 'enter', 'alt+tab', several space-separated; count repeats) |
    type (text=; types literally, \\n = Enter) | click (x,y; button left/right/middle; count=2 double) |
    move (x,y) | scroll (direction up/down/left/right, amount notches, optional x,y) |
    drag (x,y -> x2,y2) | mouse (report pointer position)."""
    a = action.lower()
    if a == "mouse":
        mx, my = xutil.mouse_location()
        return f"pointer at {mx},{my}; {_status()}"
    if a not in ("key", "type", "click", "move", "scroll", "drag"):
        raise RuntimeError(f"unknown input action {action!r}")
    if a in ("click", "drag") and (x is None or y is None):
        raise RuntimeError(f"{a} needs x and y")

    pointer = a in ("click", "scroll", "drag", "move")
    target = None
    if window:
        target = xutil.resolve_window(window)
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
        if xutil.is_own_terminal(aid) and not window:
            raise RuntimeError("refusing: the focused window is the terminal running Claude itself; "
                               "pass window= to target the app you mean")

    px, py = (x, y) if x is not None else (target.x + target.w // 2, target.y) if target else (None, None)
    if a == "key":
        if not keys:
            raise RuntimeError("key needs keys=")
        osd.show(f"pressing {keys}", px, py)
        xutil.key(keys, count)
    elif a == "type":
        if text is None:
            raise RuntimeError("type needs text=")
        preview = text if len(text) <= 40 else text[:37] + "…"
        osd.show(f"typing \"{preview}\"".replace("\n", "⏎"), px, py)
        xutil.type_text(text)
    elif a == "click":
        osd.show(f"{'double-' if count == 2 else ''}{button} click at {x},{y}", px, py)
        xutil.click(x, y, button, count)
    elif a == "move":
        xutil.move(x, y)
    elif a == "scroll":
        osd.show(f"scrolling {direction} ×{amount}", px, py)
        xutil.scroll(direction, amount, x, y)
    elif a == "drag":
        osd.show(f"dragging {x},{y} → {x2},{y2}", px, py)
        xutil.drag(x, y, x2, y2, button)
    time.sleep(0.3)
    went = f"; went to {target.title!r}" if target is not None and not window else ""
    return f"{a} ok{went}; {_status()}"


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
    _bootstrap_env()
    a11y.ensure_enabled()
    mcp.run()


if __name__ == "__main__":
    main()
