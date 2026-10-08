"""Smoke test: text entry, keys, clipboard and a modal dialog in gedit (opens and closes a window)."""
import re
import time

from deskpilot import server as s

print(s.window("launch", "gedit --new-window"))
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
time.sleep(1)
dlg = s.ui(w, query="without saving")
print("dialog:", dlg)
print(s.act(re.search(r"\[(e\d+)\]", dlg).group(1), "click"))
time.sleep(0.8)
assert "gedit" not in s.windows().lower(), "gedit still open"
print("PASS")
