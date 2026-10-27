#!/usr/bin/env python3
"""A fake `aigource --serve` for the nvim tests: aigource/README.md's protocol
(Serve) on its abstract socket, answers from a spec file the test rewrites.

  fake_aigource.py --serve -r ROOT

Environment:
  AIG_SOCK_NS     socket name prefix (as aigource)
  AIG_SERVE_IDLE  exit after this many seconds with no client (default 300)
  AIG_FAKE_DIR    spec.json: {ROOT: {"rev": N, "building": bool, "host": "headless"|"gui",
                    "files": {REL: {"text", "runs", "changes", "turns",
                                    "detail": {LINE: {"turn_key", "tool", "agent", "ts", "prompt",
                                                      "comment", "hunks", "truncated"?}}}},
                    "turn_lines": {KEY: [{"file", "runs"}]}}}, read at every request;
                  log.jsonl: one {"pid", "root", "req"} per request received;
                  ctl.<pid>: commands the test appends, one JSON object a line:
                    {"push": [REL, ...]}  a `changed` push to the subscribed clients
                    {"handover": true}    `handover` to every client, close, exit
                    {"eof": true}         close everything with nothing sent, exit
                    {"mute": true}        read requests from now on, answer none
                  pids: this server's pid, one line each (the test kills what is left).
"""
import hashlib, json, os, selectors, socket, sys, time

args = sys.argv[1:]
root = os.path.realpath(args[args.index("-r") + 1] if "-r" in args else os.getcwd())
fdir = os.environ["AIG_FAKE_DIR"]
ns = os.environ.get("AIG_SOCK_NS") or "aigource"
idle = float(os.environ.get("AIG_SERVE_IDLE", "300"))
name = "\0" + ns + "/" + hashlib.sha1(root.encode()).hexdigest()[:16]

lsock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    lsock.bind(name)
except OSError:
    sys.exit(0)  # another one serves this root
lsock.listen(16)
pid = os.getpid()
with open(os.path.join(fdir, "pids"), "a") as f:
    f.write("%d\n" % pid)
ctl = os.path.join(fdir, "ctl.%d" % pid)
open(ctl, "a").close()
ctl_off = 0

sel = selectors.DefaultSelector()
sel.register(lsock, selectors.EVENT_READ)
clients = {}  # socket -> {"buf": bytes, "sub": bool}
mute = False
last_client = time.time()


def spec():
    try:
        with open(os.path.join(fdir, "spec.json")) as f:
            return json.load(f).get(root, {})
    except (OSError, ValueError):
        return {}


def send(c, obj):
    try:
        c.sendall((json.dumps(obj) + "\n").encode())
    except OSError:
        pass


def rel(file):
    file = file or ""
    return file[len(root) + 1:] if file.startswith(root + "/") else file


def answer(c, req):
    s = spec()
    with open(os.path.join(fdir, "log.jsonl"), "a") as f:
        f.write(json.dumps({"pid": pid, "root": root, "req": req}) + "\n")
    cmd = req.get("cmd")
    if mute:
        return
    out = {"id": req.get("id"), "rev": s.get("rev", 1)}
    if cmd == "quit":
        return finish(True)
    if cmd == "blame":
        f = s.get("files", {}).get(rel(req.get("file")))
        if s.get("building"):
            out["status"] = "building"
        elif f is None:
            out["status"] = "none"
        else:
            out.update(f, status="ok")
    elif cmd in ("subscribe", "saved"):
        clients[c]["sub"] = clients[c]["sub"] or cmd == "subscribe"
        out["status"] = "ok"
    elif cmd == "detail":
        f = s.get("files", {}).get(rel(req.get("file")), {})
        d = f.get("detail", {}).get(str(req.get("line")))
        if req.get("rev") != out["rev"]:
            out["status"] = "stale"
        elif d is None:
            out["status"] = "none"
        else:
            out.update(d, status="ok")
    elif cmd == "turn_lines":
        out.update(status="ok", files=s.get("turn_lines", {}).get(req.get("turn_key"), []))
    elif cmd == "focus":
        if s.get("host", "headless") == "headless":
            out.update(status="ok", host="headless")
        else:
            out.update(status="ok", host="gui", shown={"file": rel(req.get("file")), "turn_key": req.get("turn_key"),
                                                       "line": req.get("line")})
    else:
        out.update(status="error", error="unknown cmd %s" % cmd)
    send(c, out)


def finish(handover):
    sel.unregister(lsock)
    lsock.close()  # first: the next host can bind while the clients hear it
    for c in list(clients):
        if handover:
            send(c, {"event": "handover", "rev": spec().get("rev", 1)})
        c.close()
    sys.exit(0)


def control():
    global ctl_off, mute
    with open(ctl) as f:
        f.seek(ctl_off)
        data = f.read()
    end = data.rfind("\n") + 1
    ctl_off += len(data[:end].encode())
    for line in data[:end].splitlines():
        cmd = json.loads(line)
        if "push" in cmd:
            for c, st in clients.items():
                if st["sub"]:
                    send(c, {"event": "changed", "files": cmd["push"], "rev": spec().get("rev", 1)})
        elif cmd.get("handover"):
            finish(True)
        elif cmd.get("eof"):
            finish(False)
        elif cmd.get("mute"):
            mute = True


while True:
    for key, _ in sel.select(0.05):
        s = key.fileobj
        if s is lsock:
            c, _ = lsock.accept()
            clients[c] = {"buf": b"", "sub": False}
            sel.register(c, selectors.EVENT_READ)
            continue
        try:
            b = s.recv(1 << 16)
        except OSError:
            b = b""
        if not b:
            sel.unregister(s)
            s.close()
            del clients[s]
            last_client = time.time()
            continue
        clients[s]["buf"] += b
        while s in clients and b"\n" in clients[s]["buf"]:
            line, clients[s]["buf"] = clients[s]["buf"].split(b"\n", 1)
            answer(s, json.loads(line))
    control()
    if not clients and time.time() - last_client > idle:
        sys.exit(0)
