"""Tiny Tk app for tests/e2e_sandbox.py: prints every key/button press and release with a timestamp
(one JSON line each, to stdout = deskpilot's process log), and shows a 'ready' label and a Save button."""
import json
import sys
import time
import tkinter as tk


def log(**kw):
    print(json.dumps({"t": round(time.monotonic() * 1000), **kw}), flush=True)


root = tk.Tk()
root.title("deskpilot probe")
root.geometry("500x300+40+40")
tk.Label(root, text="ready   Autosave off", font=("DejaVu Sans", 22)).pack(pady=20)
status = tk.Label(root, text="", font=("DejaVu Sans", 22))
status.pack()


def save():
    status.config(text="saved")
    log(ev="saved")


tk.Button(root, text="Save", font=("DejaVu Sans", 24), command=save).pack(pady=20)
root.bind_all("<ButtonPress>", lambda e: log(ev="press", b=e.num, x=e.x_root, y=e.y_root))
root.bind_all("<ButtonRelease>", lambda e: log(ev="release", b=e.num))
root.bind_all("<KeyPress>", lambda e: log(ev="keydown", k=e.keysym))
root.bind_all("<KeyRelease>", lambda e: log(ev="keyup", k=e.keysym))
root.after(100, lambda: log(ev="up", wid=root.winfo_id()))
root.mainloop()
sys.exit(0)
