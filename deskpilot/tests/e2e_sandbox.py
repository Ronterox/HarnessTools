"""End-to-end test over MCP stdio, entirely on a sandbox X display (nothing touches your desktop):
launch by pid with a log, wait for text, held clicks and keys, click by OCR'd text, window-relative
coords, a timed sequence with one image strip, process log/kill, sandbox stop.

Run: .venv/bin/python tests/e2e_sandbox.py
"""
import asyncio
import json
import os
import re
import sys

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))


def text_of(res) -> str:
    return "\n".join(getattr(c, "text", f"<{c.type}>") for c in res.content)


async def main():
    env = {**os.environ, "PYTHONPATH": os.path.join(HERE, "..", "src")}
    params = StdioServerParameters(command=sys.executable, args=["-m", "deskpilot.server"], env=env)
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()

        async def call(name, **kw):
            res = await s.call_tool(name, kw)
            out = text_of(res)
            if res.is_error:
                raise RuntimeError(f"{name} error: {out}")
            print(f"{name}: {out[:300]}")
            return res, out

        def events(log_text):
            return [json.loads(l) for l in log_text.splitlines() if l.startswith("{")]

        try:
            await call("display", action="sandbox", size="800x600")
            _, out = await call("window", action="launch", target=f"{sys.executable} {HERE}/probe_app.py")
            pid = int(re.search(r"pid (\d+)", out).group(1))
            win = f"pid:{pid}"
            assert "deskpilot probe" in out, out
            await call("wait", until="text", text="ready", window=win)

            # Held click on OCR'd text: the press lasts >= 50 ms, and the button fires. Whole words:
            # "Save" is the button, not the "Autosave" label above it.
            _, out = await call("input", action="click", find="Save", window=win)
            assert "'Save'" in out, out
            await call("wait", until="text", text="saved", window=win)
            # Held key.
            await call("input", action="key", keys="Escape", window=win)
            # Window-relative click (the window sits at 40,40 on the sandbox screen).
            await call("input", action="click", x=10, y=10, coords="window", window=win, hold_ms=120)

            res, out = await call("sequence", window=win, coords="window", steps=[
                {"do": "shot", "label": "start"},
                {"do": "click", "x": 20, "y": 250},
                {"do": "key", "keys": "a"},
                {"do": "wait", "ms": 200},
                {"do": "shot", "label": "after"},
                {"do": "check"},
            ])
            assert "all 6 steps ok" in out, out
            assert [c.type for c in res.content].count("image") == 1, "sequence should return one strip"

            _, log = await call("process", action="log", pid=pid, lines=200)
            ev = events(log)
            presses = [e for e in ev if e["ev"] == "press"]
            releases = [e for e in ev if e["ev"] == "release"]
            holds = [b["t"] - a["t"] for a, b in zip(presses, releases)]
            print("button holds (ms):", holds)
            assert any(e["ev"] == "saved" for e in ev), "Save not clicked"
            assert len(holds) >= 3 and min(holds) >= 45, holds
            esc = [e for e in ev if e.get("k") == "Escape"]
            key_hold = esc[1]["t"] - esc[0]["t"]
            print("Escape hold (ms):", key_hold)
            assert esc[0]["ev"] == "keydown" and key_hold >= 45, esc
            rel = next(e for e in presses if e["x"] < 100 and e["y"] < 100)
            assert (rel["x"], rel["y"]) == (50, 50), rel  # 40,40 + 10,10

            # Modifiers are released after combos (a stuck Ctrl would turn the user's next keys
            # and deskpilot's next clicks into Ctrl+key / Ctrl+click).
            from Xlib import display as xdisplay
            _, st = await call("display", action="status")
            sandbox = re.search(r"sandbox (:\d+)", st).group(1)
            for combo in ("ctrl+a", "shift+Tab", "ctrl+shift+z"):
                await call("input", action="key", keys=combo, window=win)
                d = xdisplay.Display(sandbox)
                down = [d.keycode_to_keysym(i * 8 + b, 0) for i, byte in enumerate(d.query_keymap())
                        for b in range(8) if byte >> b & 1]
                d.close()
                assert not down, f"keys still down after {combo}: {down}"

            # No substring hits: "ave" is part of words on screen but no word itself.
            res = await s.call_tool("input", {"action": "click", "find": "ave", "window": win})
            assert res.is_error and "nothing sent" in text_of(res), text_of(res)

            # A click that would land outside the target window is refused, nothing sent.
            res = await s.call_tool("input", {"action": "click", "x": 790, "y": 590, "window": win})
            assert res.is_error and "nothing sent" in text_of(res), text_of(res)

            await call("process", action="kill", pid=pid)
            await call("wait", until="gone", target=win, timeout=5)
            await call("process", action="list")
        finally:
            await call("display", action="stop")
        print("PASS")


asyncio.run(main())
