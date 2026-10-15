"""Smoke test: text entry, keys, clipboard and a modal dialog in gedit (opens and closes a window)."""
import re
import sys
import time

from deskpilot import procs, server as s

procs.install_exit_cleanup()  # as server.main() does: a crash mid-test still stops the sandbox

sandbox = "--sandbox" in sys.argv  # on a private X display: nothing touches the desktop
if sandbox:
    print(s.display("sandbox"))
# gedit is single-instance over D-Bus; in the sandbox its own session bus keeps it off a running gedit
print(s.window("launch", "gedit" if sandbox else "gedit --new-window"))
w = "gedit"
q = s.ui(w, query="textbox")
print("query:", q)
ref = re.search(r"\[(e\d+)\] textbox", q).group(1)
print(s.act(ref, "set_text", "hello from deskpilot"))
print(s.input("key", keys="ctrl+End"))
print(s.input("type", text="\nsecond line"))
print(s.input("key", keys="ctrl+a ctrl+c"))
time.sleep(0.3)
clip = s.clipboard()
print("clipboard:", repr(clip))
assert clip == "hello from deskpilot\nsecond line", clip
print(s.act(ref, "read"))
print(s.window("close", w))
for _ in range(20):  # the save prompt can take a moment
    time.sleep(0.25)
    dlg = s.ui(w, query="without saving")
    if "[e" in dlg:
        break
print("dialog:", dlg)
print(s.act(re.search(r"\[(e\d+)\]", dlg).group(1), "click"))
time.sleep(0.8)
assert "gedit" not in s.windows().lower(), "gedit still open"
if sandbox:
    print(s.display("stop"))
print("PASS")
