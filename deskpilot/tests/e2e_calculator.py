"""End-to-end smoke test over MCP stdio: drives gnome-calculator via the a11y tree.

Run: .venv/bin/python tests/e2e_calculator.py [--sandbox]   (opens and closes a Calculator window)
"""
import asyncio
import os
import re
import sys

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def text_of(res) -> str:
    return "\n".join(getattr(c, "text", f"<{c.type}>") for c in res.content)


async def main():
    # The client starts servers with a minimal env: pass ours, so this checkout's code is the one tested.
    env = {**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")}
    params = StdioServerParameters(command=sys.executable, args=["-m", "deskpilot.server"], env=env)
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        if "--sandbox" in sys.argv:  # on a private X display: nothing touches the desktop
            res = await s.call_tool("display", {"action": "sandbox"})
            print(text_of(res))
            if res.is_error:  # never fall through to driving the real desktop
                raise SystemExit("no sandbox: " + text_of(res))
        tools = (await s.list_tools()).tools
        print("tools:", [t.name for t in tools])

        async def call(name, **kw):
            res = await s.call_tool(name, kw)
            out = text_of(res)
            if res.is_error:
                raise RuntimeError(f"{name} error: {out}")
            return out

        print(await call("window", action="launch", target="gnome-calculator"))
        await asyncio.sleep(1.5)
        snap = await call("ui", window="calculator", interactive_only=True)
        print(f"snapshot: {len(snap)} chars")
        ref = lambda label: re.search(rf'\[(e\d+)\] button "{re.escape(label)}"', snap).group(1)
        for label in ("7", "×", "6", "="):
            print(label, "->", await call("act", ref=ref(label)))
        found = await call("ui", window="calculator", query="42")
        print("query 42:", found)
        ocr = await call("read_screen", window="calculator", find="42")
        print("ocr 42:", ocr)
        shot = await s.call_tool("screenshot", {"window": "calculator", "max_side": 400})
        print("screenshot:", [c.type for c in shot.content], text_of(shot).splitlines()[0])
        print(await call("window", action="close", target="calculator"))
        assert "42" in found, "result not found in a11y tree"
        if "--sandbox" in sys.argv:
            print(await call("display", action="stop"))
        print("PASS")


asyncio.run(main())
