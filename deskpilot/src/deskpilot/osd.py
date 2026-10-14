"""On-screen indicator so the user can see what Claude is doing.

Runs as a separate long-lived Tk process fed over stdin. The window is override-redirect, so the window
manager never manages or focuses it: it cannot steal keystrokes from the window being driven.
Disable with env DESKPILOT_OSD=0.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

from . import xutil

_proc: subprocess.Popen | None = None
_lock = threading.Lock()


def _enabled() -> bool:
    # Not for a sandbox: its actions are off your screen, and the overlay lives on your desktop.
    return os.environ.get("DESKPILOT_OSD", "1") != "0" and not xutil.current_display()


def _send(msg: dict) -> None:
    global _proc
    if not _enabled():
        return
    with _lock:
        try:
            if _proc is None or _proc.poll() is not None:
                _proc = subprocess.Popen([sys.executable, "-m", "deskpilot.osd"], stdin=subprocess.PIPE,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
            _proc.stdin.write(json.dumps(msg) + "\n")
            _proc.stdin.flush()
        except Exception:
            _proc = None  # never let the indicator break an action


def _monitor_for(x: int | None, y: int | None):
    mons = xutil.monitors()
    if x is not None and y is not None:
        for m in mons:
            if m[1] <= x < m[1] + m[3] and m[2] <= y < m[2] + m[4]:
                return m
    return mons[0] if mons else ("", 0, 0, 1920, 1080)


def show(text: str, x: int | None = None, y: int | None = None, ms: int = 1800) -> None:
    """Show `text` at the bottom-center of the monitor containing (x, y)."""
    try:
        _, mx, my, mw, mh = _monitor_for(x, y)
    except Exception:
        mx, my, mw, mh = 0, 0, 1920, 1080
    _send({"text": text, "cx": mx + mw // 2, "bottom": my + mh - 70, "ms": ms})


def hide() -> None:
    """Hide immediately (before screenshots/OCR so the overlay isn't captured)."""
    if _proc is not None and _proc.poll() is None:
        _send({"hide": True})
        import time
        time.sleep(0.08)


# ---------------------------------------------------------------- overlay process

def _run_overlay() -> None:
    import queue
    import tkinter as tk

    q: queue.Queue = queue.Queue()

    def reader():
        for line in sys.stdin:
            try:
                q.put(json.loads(line))
            except ValueError:
                pass
        q.put(None)  # parent gone

    threading.Thread(target=reader, daemon=True).start()

    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    try:
        root.attributes("-alpha", 0.92)
    except tk.TclError:
        pass
    root.configure(bg="#d97757")  # 2px accent border
    label = tk.Label(root, bg="#1f1e1d", fg="#f5f4ef", font=("DejaVu Sans", 13, "bold"), padx=16, pady=8)
    label.pack(padx=2, pady=2)
    root.withdraw()
    hide_job = [None]

    def poll():
        try:
            while True:
                msg = q.get_nowait()
                if msg is None:
                    root.destroy()
                    return
                if msg.get("hide"):
                    root.withdraw()
                    continue
                label.config(text="● Claude: " + msg["text"])
                root.update_idletasks()
                w, h = root.winfo_reqwidth(), root.winfo_reqheight()
                root.geometry(f"+{msg['cx'] - w // 2}+{msg['bottom'] - h}")
                root.deiconify()
                root.lift()
                if hide_job[0]:
                    root.after_cancel(hide_job[0])
                hide_job[0] = root.after(msg.get("ms", 1800), root.withdraw)
        except queue.Empty:
            pass
        root.after(50, poll)

    root.after(50, poll)
    root.mainloop()


if __name__ == "__main__":
    _run_overlay()
