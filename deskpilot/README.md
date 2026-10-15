# deskpilot

MCP server that lets Claude drive the whole Linux desktop (X11 / GNOME) — mostly **without screenshots**.

| Tier | Tool | What Claude gets | Typical cost |
|---|---|---|---|
| 1 | `ui`, `act` | Accessibility tree as text with refs (`[e12] button "Send"`), act on refs | 100–2k tokens per window |
| 2 | `read_screen` | OCR lines `x,y  text` (clickable coords) | 100–1k tokens |
| 3 | `screenshot` | Cropped, downscaled JPEG, optional coordinate grid | ~130–800 tokens |

Plus `windows`, `window` (focus/close/move/resize/launch), `input` (keys, typing, mouse, scroll, drag,
click-by-text), `clipboard`, and for driving and testing apps: `sequence` (timed steps, one image strip),
`wait` (for a window or text, instead of sleeps), `process` (launched apps: status, log, kill) and
`display` (a private sandbox X display).

## Testing your own GUI app

```
display(action="sandbox")                         # private X display: your desktop is never touched
window(action="launch", target="./build/app --x", cwd="~/proj", env={"ASAN_OPTIONS": "detect_leaks=0"})
  -> launched pid 4242 on sandbox :91; window 0x00200007 app 'app' 1400x900+0+0; log /tmp/deskpilot-1000/launch-….log
wait(until="text", text="ready", window="pid:4242")
sequence(window="pid:4242", coords="window", steps=[
  {"do": "click", "find": "Save"}, {"do": "wait", "ms": 300}, {"do": "shot", "label": "saved"},
  {"do": "key", "keys": "Escape"}, {"do": "shot"}, {"do": "check"}])   # one call, one image back
process(action="log", pid=4242)                   # its stdout+stderr (sanitizer reports, crashes)
display(action="stop")                            # kills the sandbox and what ran in it
```

- **Held presses.** Keys and buttons stay down 60 ms (`hold_ms=`), so apps that poll input once per
  frame (games, raylib/GLFW/SDL, canvas UIs) register them; plain `xdotool click` is lost there.
- **`pid:N` targets** the window of a launched process or its children. The pid comes from `_NET_WM_PID`,
  else from the X server itself (XRes), so toolkits that don't set it (Tk, many games) work too.
- **`coords="window"`** makes x,y relative to the window. **`find="text"`** focuses the window, OCRs it
  and clicks the matching words. Safest first: a label that *is* the text ("Save" hits the Save
  button, never "Don't Save" or "Save As"); a multi-word phrase inside a longer label if it occurs
  once ("git status" in a command line); an OCR misread ("gil status") of the same length, if it's
  the only candidate. Several matches = an error listing them, nothing clicked. On the desktop it
  needs `window=`.
- **Tool calls run one at a time**, so the display or focus can't change between a call's safety
  checks and its input (a long `wait`/`sequence` makes other calls wait for it).
- **The sandbox** is Xvfb (invisible) when installed, else Xephyr: a window on your desktop, minimized
  right away (`visible=True` to watch it), though GNOME may focus it for a moment when it appears.
  Apps you start yourself join it with `DISPLAY=:91 ./app` (the number `display()` reports). It has no window manager: windows have no frames and can't be
  minimized; `window(close)` asks the app politely (it may still prompt about unsaved changes). It stops
  when deskpilot exits. For a fully invisible sandbox: `sudo apt install xvfb`.
- **Sandbox launches are kept off your session** as far as the environment allows:
  - a private D-Bus session bus per sandbox, so D-Bus single-instance apps (gedit, nautilus, most
    GNOME apps) can't hand off to a copy running on your desktop. Only dconf can be activated on it,
    so no portals. If it can't start, `display()` and each launch say why;
  - X11 forced (`WAYLAND_DISPLAY` unset, `GDK_BACKEND=x11`, `QT_QPA_PLATFORM=xcb`, SDL/Firefox/Electron
    likewise), `GTK_USE_PORTAL=0`, software GL (`LIBGL_ALWAYS_SOFTWARE=1`, Mesa's GLX), no input
    method, no HiDPI scaling;
  - a plain `firefox …`, `google-chrome …`/`chromium …` or `soffice …`/`libreoffice …` gets a
    throwaway profile, since those hand off through (or stop at) the profile your desktop copy holds.
    Firefox's skips its welcome dialog; Chrome also gets `--force-renderer-accessibility` for `ui()`.

  Not covered: other apps that hand off through a socket or lock file rather than D-Bus (VS Code,
  Electron apps, Chrome started via a wrapper script) can still open in your desktop copy: give them
  their own profile (`code --user-data-dir /tmp/x`). GSettings writes go to your real dconf database,
  as on the desktop (pass `env={"GSETTINGS_BACKEND": "memory"}` to keep them in the app, at the cost
  of seeing schema defaults instead of your settings). `env=` overrides any of these. The
  accessibility bus stays yours, so `ui()`/`act()` see sandbox apps. Every launch, desktop too, gets
  `QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1` and no `NO_AT_BRIDGE`. Apps you start yourself with
  `DISPLAY=:91` get none of this.

Works via AT-SPI for GTK/GNOME apps, Firefox, Electron/Chromium (Discord, VS Code, Chrome), LibreOffice,
Qt apps with accessibility. Anything else (games, canvas UIs, remote desktops) falls back to OCR / screenshots.
Modal dialogs (save prompts, file choosers) are appended to `ui()` output for the window they block.

## Install

```bash
sudo apt install python3-pyatspi gir1.2-atspi-2.0 python3-xlib xdotool wmctrl x11-utils tesseract-ocr xclip
sudo apt install xvfb   # optional: invisible sandbox display (else Xephyr, a minimized window)
uv venv --system-site-packages .venv      # system site-packages needed for gi/pyatspi
uv pip install --python .venv/bin/python -e .
```

deskpilot is **opt-in per session** (it is not registered globally). Start Claude with it:

```bash
claude --mcp-config ~/Documents/Projects/AIProjects/HarnessTools/deskpilot/deskpilot.mcp.json
```

(`deskpilot.mcp.json` holds an absolute path to `.venv/bin/deskpilot`; edit it if you move the repo.)
To make it always-on instead: `claude mcp add deskpilot -s user -- "$PWD/.venv/bin/deskpilot"`.

On start the server sets the AT-SPI bus flag `IsEnabled=true` so apps expose their trees. It deliberately
never sets `ScreenReaderEnabled`, which on GNOME launches the Orca narrator. If an app shows an empty tree,
restart it (Chromium/Electron can also be launched with `--force-renderer-accessibility`).

## Tests

Both open a throwaway window, drive it, and close it:

```bash
.venv/bin/python tests/e2e_sandbox.py                # held clicks/keys, find=, pid:, sequence, wait, process
.venv/bin/python tests/e2e_calculator.py [--sandbox] # over MCP stdio: 7 × 6 = 42 via a11y clicks, OCR, screenshot
.venv/bin/python tests/e2e_editor.py [--sandbox]     # gedit: set_text, keys, typing, clipboard, save-dialog
.venv/bin/python -m pytest tests/test_launch_env.py  # unit: sandbox launch env, profile rewrite, bus (no X)
```

`--sandbox` (and `e2e_sandbox.py` always) runs on a private X display, so your desktop isn't touched.

## Safety & visibility

- `input(window=…)` switches to the window's workspace, focuses it and verifies focus before sending
  anything; pointer coordinates must lie on that window or nothing is sent.
- Keys/text are refused if they would land in the terminal running Claude itself (tmux-aware).
- An on-screen indicator ("● Claude: scrolling down ×5") shows each action. It is override-redirect, so it
  can never take focus, and it is hidden before OCR/screenshots. Disable with `DESKPILOT_OSD=0`.

## Notes

- X11 only (xdotool/wmctrl). Wayland would need ydotool + portal screenshots.
- Coordinates are absolute screen pixels across monitors, unless `coords="window"`.
- Refs (`e12`) persist across calls while the element exists; a stale ref returns an error asking for a fresh `ui()`.
