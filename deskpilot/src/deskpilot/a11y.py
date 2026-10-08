"""AT-SPI accessibility tree -> compact text snapshots with stable refs, plus element actions."""
from __future__ import annotations

import subprocess
import time

import pyatspi
from pyatspi import (STATE_ACTIVE, STATE_CHECKED, STATE_COLLAPSED, STATE_EDITABLE, STATE_ENABLED,
                     STATE_EXPANDABLE, STATE_EXPANDED, STATE_FOCUSED, STATE_PRESSED, STATE_SELECTABLE,
                     STATE_SELECTED, STATE_SENSITIVE, STATE_SHOWING, STATE_VISIBLE)

from . import xutil

OBJ = "￼"  # embedded-object placeholder inside AT-SPI text

ROLE_SHORT = {
    "push button": "button", "toggle button": "toggle", "check box": "checkbox", "radio button": "radio",
    "menu item": "menuitem", "check menu item": "menuitem", "radio menu item": "menuitem", "page tab": "tab",
    "page tab list": "tabs", "list item": "item", "password text": "password", "combo box": "combo",
    "spin button": "spin", "table cell": "cell", "tree item": "treeitem", "document web": "document",
    "document frame": "document", "menu bar": "menubar", "tool bar": "toolbar", "column header": "colheader",
    "row header": "rowheader", "push button menu": "menubutton", "tree table": "treetable", "entry": "entry",
    "text": "textbox",
}
INTERACTIVE = {
    "push button", "toggle button", "check box", "radio button", "menu item", "check menu item",
    "radio menu item", "menu", "combo box", "entry", "password text", "spin button", "slider", "link",
    "page tab", "list item", "tree item", "table cell", "icon", "push button menu", "switch",
    "column header", "row header", "scroll bar",
}
# Kept even without a name because they give structure the model needs.
STRUCTURAL = {"dialog", "alert", "menu bar", "tool bar", "page tab list", "document web", "document frame",
              "list", "table", "tree table", "tree", "popup menu", "notification", "status bar", "file chooser"}
TEXTUAL = {"static", "label", "paragraph", "heading", "text", "caption", "block quote", "list item",
           "table cell", "link", "description term", "description value", "footer", "header", "article",
           "section", "comment", "math", "mark", "content insertion"}
NAMED_CONTAINERS_WORTH_SHOWING = {"frame", "window", "panel", "filler", "section", "grouping", "landmark",
                                  "form", "scroll pane", "layered pane", "split pane", "viewport", "article",
                                  "region", "internal frame"}
TITLE_ROLES = {"frame", "window", "document web", "document frame", "dialog", "internal frame"}
# Unnamed wrappers around a real control (e.g. <li><a>…</a></li>) add nothing.
TRANSPARENT_IF_UNNAMED = {"list item", "table cell", "tree item"}
BLOCK_ROLES = {"heading", "paragraph", "list", "list item", "table", "form", "landmark", "article",
               "block quote", "separator", "page tab list", "tool bar", "dialog", "image map", "description list"}
CLICK_ACTIONS = ("click", "press", "activate", "jump", "open", "toggle", "select", "showmenu")

MAX_CHILDREN = 300


def ensure_enabled() -> None:
    """Turn on the AT-SPI bus flag so toolkits expose their trees.

    Deliberately NOT ScreenReaderEnabled: on GNOME that launches the Orca narrator.
    """
    subprocess.run(["gdbus", "call", "--session", "--dest", "org.a11y.Bus", "--object-path", "/org/a11y/bus",
                    "--method", "org.freedesktop.DBus.Properties.Set", "org.a11y.Status", "IsEnabled",
                    "<true>"], capture_output=True, timeout=5)


# ---------------------------------------------------------------- refs

class Refs:
    """Maps short ids (e1, e2, ...) <-> accessibles. Same object keeps the same ref across snapshots."""

    def __init__(self):
        self.by_ref: dict[str, tuple[object, int]] = {}
        self.by_obj: dict[object, str] = {}
        self.n = 0

    def ref(self, acc, wid: int) -> str:
        r = self.by_obj.get(acc)
        if r is None:
            if self.n > 50000:
                self.by_ref.clear(); self.by_obj.clear()
            self.n += 1
            r = f"e{self.n}"
            self.by_obj[acc] = r
        self.by_ref[r] = (acc, wid)
        return r

    def get(self, ref: str):
        ref = ref.strip().lstrip("[@").rstrip("]")
        if ref not in self.by_ref:
            raise RuntimeError(f"unknown ref {ref!r}; take a fresh ui() snapshot")
        return self.by_ref[ref]


REFS = Refs()


# ---------------------------------------------------------------- helpers

def _role(acc) -> str:
    try:
        return acc.getRoleName()
    except Exception:
        return "?"


def _states(acc):
    try:
        return acc.getState()
    except Exception:
        return None


def _text(acc, limit: int | None = None) -> str:
    try:
        t = acc.queryText()
    except Exception:
        return ""
    try:
        n = t.characterCount
        return t.getText(0, n if limit is None else min(n, limit))
    except Exception:
        return ""


def _value(acc) -> str | None:
    try:
        v = acc.queryValue()
        cur = v.currentValue
        return f"{cur:g} [{v.minimumValue:g}..{v.maximumValue:g}]"
    except Exception:
        return None


def _extents(acc):
    try:
        e = acc.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
        return e.x, e.y, e.width, e.height
    except Exception:
        return None


def _clean(s: str, limit: int) -> str:
    s = " ".join(s.replace(OBJ, " ").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _children(acc):
    try:
        n = acc.childCount
    except Exception:
        return [], 0
    out = []
    for i in range(min(n, MAX_CHILDREN)):
        try:
            c = acc.getChildAtIndex(i)
        except Exception:
            c = None
        if c is not None:
            out.append(c)
    return out, n


# Hidden containers that hold whole hidden UIs (background tabs, closed menus): never descend.
PRUNE_IF_HIDDEN = {"document web", "document frame", "internal frame", "scroll pane", "menu", "popup menu",
                   "window", "dialog", "frame", "page tab", "tool tip", "alert", "notification"}

SKIP, PASS, SHOW = 0, 1, 2


def _visibility(st, role: str) -> int:
    """SHOW: render it. PASS: wrapper marked not-showing but children may be (Firefox does this)
    -> descend without rendering. SKIP: prune subtree."""
    if st is None or not st.contains(STATE_VISIBLE):
        return SKIP
    if st.contains(STATE_SHOWING):
        return SHOW
    return SKIP if role in PRUNE_IF_HIDDEN else PASS


# ---------------------------------------------------------------- window <-> frame

def find_frame(win: xutil.Win):
    desktop = pyatspi.Registry.getDesktop(0)
    candidates = []
    for app in desktop:
        if app is None:
            continue
        try:
            pid = app.get_process_id()
        except Exception:
            pid = -1
        frames, _ = _children(app)
        for f in frames:
            candidates.append((pid == win.pid, f))
    title = win.title.strip()

    def name(f):
        try:
            return (f.name or "").strip()
        except Exception:
            return ""

    for same_pid in (True, False):
        pool = [f for p, f in candidates if p == same_pid]
        for f in pool:
            if name(f) == title:
                return f
        for f in pool:
            n = name(f)
            if n and (n in title or title in n):
                return f
    same = [f for p, f in candidates if p]
    for f in same:
        st = _states(f)
        if st is not None and st.contains(STATE_ACTIVE):
            return f
    return same[0] if len(same) == 1 else None


def modal_dialogs(frame, wid: int | None = None) -> list:
    """Other showing top-levels of the same app that are modal (save prompts, file choosers…).
    With wid, keep only dialogs whose geometry matches an X window transient for wid."""
    try:
        app = frame.getApplication()
    except Exception:
        return []
    out = []
    for f in _children(app)[0]:
        if f == frame:
            continue
        st = _states(f)
        if st is not None and st.contains(pyatspi.STATE_MODAL) and st.contains(STATE_SHOWING):
            out.append(f)
    if wid is None or len(out) < 2:
        return out
    geoms = xutil.transients_of(wid)

    def near(f):
        e = _extents(f)
        return e and any(abs(e[0] - g[0]) < 80 and abs(e[1] - g[1]) < 80 and abs(e[2] - g[2]) < 80
                         for g in geoms)
    return [f for f in out if near(f)]


# ---------------------------------------------------------------- snapshot

class _Out:
    def __init__(self, max_lines: int):
        self.lines: list[str] = []
        self.max = max_lines
        self.dropped = 0

    def add(self, s: str) -> bool:
        if self.lines and self.lines[-1] == s:
            return True
        if len(self.lines) >= self.max:
            self.dropped += 1
            return False
        self.lines.append(s)
        return True


def _flags(acc, role: str, st, interactive: bool) -> str:
    f = []
    if st is None:
        return ""
    if st.contains(STATE_FOCUSED):
        f.append("focused")
    if st.contains(STATE_CHECKED) or (role == "toggle button" and st.contains(STATE_PRESSED)):
        f.append("checked")
    elif role in ("check box", "radio button", "check menu item", "radio menu item", "switch"):
        f.append("unchecked")
    if st.contains(STATE_SELECTED) and st.contains(STATE_SELECTABLE) and role not in ("frame",):
        f.append("selected")
    if st.contains(STATE_EXPANDABLE) or st.contains(STATE_EXPANDED) or st.contains(STATE_COLLAPSED):
        f.append("expanded" if st.contains(STATE_EXPANDED) else "collapsed")
    if interactive and not (st.contains(STATE_SENSITIVE) and st.contains(STATE_ENABLED)):
        f.append("disabled")
    return (" (" + ",".join(f) + ")") if f else ""


def _is_editable(role, st) -> bool:
    return role in ("entry", "password text", "text", "combo box", "spin button") and st is not None \
        and st.contains(STATE_EDITABLE)


def _inline(acc, wid: int, text: str, kids, limit: int) -> str | None:
    """Render a paragraph whose text embeds child objects (links, etc.) as one line."""
    if text.count(OBJ) != len(kids) or (len(kids) == 1 and not text.replace(OBJ, "").strip()):
        return None
    parts = text.split(OBJ)
    out = [parts[0]]
    for k, tail in zip(kids, parts[1:]):
        r = _role(k)
        nm = ""
        try:
            nm = k.name or ""
        except Exception:
            pass
        sub = nm or _clean(_text(k, 300), 120)
        if (r in INTERACTIVE and r != "link") or r in BLOCK_ROLES:
            return None  # buttons/fields/blocks deserve their own lines
        if r == "link":
            out.append(f"[{REFS.ref(k, wid)} {ROLE_SHORT.get(r, r)} \"{_clean(sub, 80)}\"]")
        elif _children(k)[1]:
            return None  # nested structure; fall back to tree rendering
        else:
            out.append(sub)
        out.append(tail)
    return _clean("".join(out), limit)


def _walk(acc, wid: int, out: _Out, depth: int, indent: int, parent_label: str, opts: dict, level: int = 0):
    if level > 80 or out.dropped > 0:
        if level <= 80:
            out.dropped += 1
        return
    st = _states(acc)
    role = _role(acc)
    vis = SHOW if level == 0 else _visibility(st, role)
    if vis == SKIP:
        return
    kids, nkids = _children(acc)
    if vis == PASS:
        for k in kids:
            _walk(k, wid, out, depth, indent, parent_label, opts, level + 1)
        return
    try:
        name = (acc.name or "").strip()
    except Exception:
        name = ""
    interactive = role in INTERACTIVE or _is_editable(role, st)
    if interactive and not name and role in TRANSPARENT_IF_UNNAMED and kids and \
            not st.contains(STATE_SELECTED) and not _text(acc, 200).replace(OBJ, "").strip():
        interactive = False
    textual = role in TEXTUAL
    text = ""
    if textual or _is_editable(role, st):
        text = _text(acc, 4000 if textual else 400)

    if opts["interactive_only"] and not interactive and level > 0:
        for k in kids:
            _walk(k, wid, out, depth, indent, parent_label, opts, level + 1)
        return

    # `label` is what children may not repeat: only an interactive element's name or a text line.
    label = name if (interactive or role not in TITLE_ROLES) else ""
    line = None
    pad = "  " * indent
    tl = opts["text_limit"]
    pl = parent_label.lower()

    def redundant(s: str) -> bool:
        return bool(pl) and s.lower() in pl

    if interactive or role in STRUCTURAL or level == 0 or (
            name and role in NAMED_CONTAINERS_WORTH_SHOWING and not redundant(name)):
        r = REFS.ref(acc, wid)
        short = ROLE_SHORT.get(role, role)
        desc = f'"{_clean(name, tl)}"' if name else ""
        extra = ""
        if _is_editable(role, st):
            extra = f' value="{_clean(text, tl)}"' if text else ' value=""'
        elif role in ("slider", "spin button", "scroll bar", "progress bar"):
            v = _value(acc)
            extra = f" value={v}" if v else ""
        elif not name and textual and text.strip():
            inl = _inline(acc, wid, text, kids, tl) if OBJ in text else None
            desc = f'"{inl or _clean(text, tl)}"'
            if inl:
                kids = []
        if opts["coords"] and (ext := _extents(acc)):
            extra += f" @{ext[0] + ext[2] // 2},{ext[1] + ext[3] // 2}"
        line = f"{pad}[{r}] {short} {desc}{extra}{_flags(acc, role, st, interactive)}".rstrip()
    elif textual and (name or text.strip()):
        content = name or text
        if OBJ in content and kids:
            inl = _inline(acc, wid, content, kids, tl)
            if inl:
                content, kids = inl, []
        content = _clean(content, tl)
        if content and content.replace(" ", "") and not redundant(content):
            prefix = "# " if role == "heading" else ""
            line = f"{pad}{prefix}{content}"
        label = content
    elif role == "image" and name and not redundant(name):
        line = f"{pad}img \"{_clean(name, 60)}\""
    else:
        label = parent_label  # transparent wrapper: keep the ancestor's label in force

    if line is not None:
        if not out.add(line):
            return
        indent += 1
    if depth is not None and level >= depth:
        if kids and line is not None:
            out.add(f"{pad}  … ({nkids} children, depth limit)")
        return
    for k in kids:
        _walk(k, wid, out, depth, indent, label, opts, level + 1)
    if nkids > MAX_CHILDREN:
        out.add(f"{pad}  … {nkids - MAX_CHILDREN} more children not shown")


def snapshot(root, wid: int, *, max_lines=250, depth=None, interactive_only=False, coords=False,
             text_limit=120) -> str:
    out = _Out(max_lines)
    opts = dict(interactive_only=interactive_only, coords=coords, text_limit=text_limit)
    _walk(root, wid, out, depth, 0, "", opts)
    s = "\n".join(out.lines)
    if out.dropped:
        s += f"\n… truncated at {max_lines} lines. Narrow with query=, root=<ref>, interactive_only=True, " \
             f"or raise max_lines."
    return s


def search(root, wid: int, query: str, *, limit=40, coords=True) -> str:
    """Flat list of showing nodes whose name/text/role matches query (case-insensitive)."""
    q = query.lower()
    hits: list[str] = []
    stack = [(root, "", 0)]
    visited = 0
    while stack and len(hits) < limit:
        acc, ctx, level = stack.pop()
        visited += 1
        st = _states(acc)
        role = _role(acc)
        vis = SHOW if level == 0 else _visibility(st, role)
        if vis == SKIP:
            continue
        if vis == PASS:
            for k in reversed(_children(acc)[0]):
                stack.append((k, ctx, level + 1))
            continue
        try:
            name = (acc.name or "").strip()
        except Exception:
            name = ""
        text = _text(acc, 2000) if role in TEXTUAL or _is_editable(role, st) else ""
        hay = f"{name} {text}".lower()
        if q in hay or q == ROLE_SHORT.get(role, role):
            r = REFS.ref(acc, wid)
            ext = _extents(acc) if coords else None
            at = f" @{ext[0] + ext[2] // 2},{ext[1] + ext[3] // 2}" if ext else ""
            shown = _clean(name or text, 100)
            c = f"  in \"{_clean(ctx, 40)}\"" if ctx else ""
            hits.append(f"[{r}] {ROLE_SHORT.get(role, role)} \"{shown}\"{at}"
                        f"{_flags(acc, role, st, role in INTERACTIVE)}{c}")
            if role in TEXTUAL:
                continue  # don't also report its text leaves
        kids, _ = _children(acc)
        newctx = name if name and role not in TEXTUAL else ctx
        for k in reversed(kids):
            stack.append((k, newctx, level + 1))
    if not hits:
        return f"no showing element matches {query!r}"
    more = " (limit reached)" if len(hits) >= limit else ""
    return "\n".join(hits) + more


def full_text(acc, limit: int = 20000) -> str:
    """All visible text under a node, reading-order-ish, one block per text node."""
    out: list[str] = []
    size = 0

    def walk(a, level):
        nonlocal size
        if size > limit or level > 80:
            return
        st = _states(a)
        role = _role(a)
        vis = SHOW if level == 0 else _visibility(st, role)
        if vis == SKIP:
            return
        kids, _ = _children(a)
        if vis == PASS:
            for k in kids:
                walk(k, level + 1)
            return
        t = _text(a) if role in TEXTUAL or _is_editable(role, st) else ""
        if t and OBJ not in t:
            s = " ".join(t.split())
            if s:
                out.append(s); size += len(s)
            return
        if t and OBJ in t:
            parts = t.split(OBJ)
            if len(parts) - 1 == len(kids) and all(not _children(k)[1] for k in kids):
                buf = [parts[0]]
                for k, tail in zip(kids, parts[1:]):
                    buf.append(_text(k) or (k.name or "")); buf.append(tail)
                s = " ".join("".join(buf).split())
                if s:
                    out.append(s); size += len(s)
                return
        for k in kids:
            walk(k, level + 1)

    walk(acc, 0)
    s = "\n".join(out)
    return s if len(s) <= limit else s[:limit] + "\n… (truncated)"


# ---------------------------------------------------------------- actions

def _center(acc):
    ext = _extents(acc)
    if not ext or ext[2] <= 0 or ext[3] <= 0:
        return None
    return ext[0] + ext[2] // 2, ext[1] + ext[3] // 2


def _ensure_window(wid: int):
    if xutil.active_window_id() != wid:
        try:
            xutil.run("wmctrl", "-i", "-a", f"0x{wid:08x}")
            time.sleep(0.2)
        except Exception:
            pass


def _scroll_into_view(acc):
    try:
        acc.queryComponent().scrollTo(pyatspi.SCROLL_ANYWHERE)
        time.sleep(0.15)
    except Exception:
        pass


def _do_named_action(acc, names) -> str | None:
    try:
        a = acc.queryAction()
    except Exception:
        return None
    avail = [(i, (a.getName(i) or "").lower()) for i in range(a.nActions)]
    for want in names:
        for i, n in avail:
            if n == want:
                if a.doAction(i):
                    return n
    return None


def list_actions(acc) -> list[str]:
    try:
        a = acc.queryAction()
        return [a.getName(i) for i in range(a.nActions)]
    except Exception:
        return []


def mouse_on(acc, wid: int, button="left", count=1) -> str:
    _scroll_into_view(acc)
    c = _center(acc)
    if not c:
        raise RuntimeError("element has no on-screen geometry; try screenshot/read_screen instead")
    _ensure_window(wid)
    xutil.click(c[0], c[1], button, count)
    return f"mouse {button}x{count} at {c[0]},{c[1]}"


def act(ref: str, action: str, text: str | None) -> str:
    acc, wid = REFS.get(ref)
    try:
        role = _role(acc)
        st = _states(acc)
        if st is None or st.contains(pyatspi.STATE_DEFUNCT):
            raise RuntimeError
    except Exception:
        raise RuntimeError(f"{ref} is stale (element gone); take a fresh ui() snapshot")

    action = action.lower().replace("-", "_")
    if action == "click":
        how = _do_named_action(acc, CLICK_ACTIONS)
        if how:
            return f"did a11y action '{how}'"
        return mouse_on(acc, wid)
    if action in ("mouse_click", "left_click"):
        return mouse_on(acc, wid)
    if action == "double_click":
        return mouse_on(acc, wid, count=2)
    if action == "right_click":
        return mouse_on(acc, wid, button="right")
    if action == "hover":
        _scroll_into_view(acc)
        c = _center(acc)
        if not c:
            raise RuntimeError("element has no on-screen geometry")
        xutil.move(*c)
        return f"mouse at {c[0]},{c[1]}"
    if action == "focus":
        _ensure_window(wid)
        try:
            if acc.queryComponent().grabFocus():
                return "focused"
        except Exception:
            pass
        return mouse_on(acc, wid) + " (grabFocus unsupported, clicked instead)"
    if action in ("set_text", "fill"):
        if text is None:
            raise RuntimeError("set_text needs text=")
        try:
            if acc.queryEditableText().setTextContents(text):
                return "text set via a11y"
        except Exception:
            pass
        _ensure_window(wid)
        try:
            acc.queryComponent().grabFocus()
        except Exception:
            mouse_on(acc, wid)
        time.sleep(0.1)
        xutil.key("ctrl+a")
        xutil.key("BackSpace")
        xutil.type_text(text)
        return "text replaced via keyboard"
    if action == "type":
        if text is None:
            raise RuntimeError("type needs text=")
        _ensure_window(wid)
        try:
            ok = acc.queryComponent().grabFocus()
        except Exception:
            ok = False
        if not ok:
            mouse_on(acc, wid)
        time.sleep(0.1)
        xutil.type_text(text)
        return "typed"
    if action in ("toggle", "check", "uncheck"):
        if action != "toggle":
            checked = st.contains(STATE_CHECKED) or st.contains(STATE_PRESSED)
            if checked == (action == "check"):
                return f"already {action}ed"
        how = _do_named_action(acc, ("toggle", "click", "press", "activate"))
        return f"did a11y action '{how}'" if how else mouse_on(acc, wid)
    if action in ("expand", "collapse"):
        how = _do_named_action(acc, ("expand or contract", "expand", "collapse", "toggle", "click"))
        return f"did a11y action '{how}'" if how else mouse_on(acc, wid)
    if action == "scroll_into_view":
        _scroll_into_view(acc)
        return "scrolled"
    if action in ("read", "text"):
        return full_text(acc)
    if action == "actions":
        return "a11y actions: " + (", ".join(list_actions(acc)) or "(none)") + f"; role={role}"
    if action == "select":
        try:
            if acc.parent.querySelection().selectChild(acc.getIndexInParent()):
                return "selected via parent Selection"
        except Exception:
            pass
        how = _do_named_action(acc, ("select", "click", "activate"))
        return f"did a11y action '{how}'" if how else mouse_on(acc, wid)
    # Any other string: try it as a raw a11y action name.
    how = _do_named_action(acc, (action,))
    if how:
        return f"did a11y action '{how}'"
    raise RuntimeError(f"unknown action {action!r}; element supports: {list_actions(acc)}")
