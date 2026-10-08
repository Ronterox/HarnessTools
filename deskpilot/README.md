# deskpilot

MCP server that lets Claude drive the whole Linux desktop (X11 / GNOME) — mostly **without screenshots**.

| Tier | Tool | What Claude gets | Typical cost |
|---|---|---|---|
| 1 | `ui`, `act` | Accessibility tree as text with refs (`[e12] button "Send"`), act on refs | 100–2k tokens per window |
| 2 | `read_screen` | OCR lines `x,y  text` (clickable coords) | 100–1k tokens |
| 3 | `screenshot` | Cropped, downscaled JPEG, optional coordinate grid | ~130–800 tokens |

Plus `windows`, `window` (focus/close/move/resize/launch), `input` (keys, typing, mouse, scroll, drag), `clipboard`.

Works via AT-SPI for GTK/GNOME apps, Firefox, Electron/Chromium (Discord, VS Code, Chrome), LibreOffice,
Qt apps with accessibility. Anything else (games, canvas UIs, remote desktops) falls back to OCR / screenshots.
Modal dialogs (save prompts, file choosers) are appended to `ui()` output for the window they block.

## Install

```bash
sudo apt install python3-pyatspi gir1.2-atspi-2.0 xdotool wmctrl x11-utils tesseract-ocr xclip
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
.venv/bin/python tests/e2e_calculator.py   # over MCP stdio: 7 × 6 = 42 via a11y clicks, OCR, screenshot
.venv/bin/python tests/e2e_editor.py       # gedit: set_text, keys, typing, clipboard, save-dialog
```

## Safety & visibility

- `input(window=…)` switches to the window's workspace, focuses it and verifies focus before sending
  anything; pointer coordinates must lie on that window or nothing is sent.
- Keys/text are refused if they would land in the terminal running Claude itself (tmux-aware).
- An on-screen indicator ("● Claude: scrolling down ×5") shows each action. It is override-redirect, so it
  can never take focus, and it is hidden before OCR/screenshots. Disable with `DESKPILOT_OSD=0`.

## Notes

- X11 only (xdotool/wmctrl). Wayland would need ydotool + portal screenshots.
- All coordinates are absolute screen pixels across monitors.
- Refs (`e12`) persist across calls while the element exists; a stale ref returns an error asking for a fresh `ui()`.
