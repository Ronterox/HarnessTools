"""`aic watch`: tail the attached worker's transcript (plus human/control logs)
into trace.jsonl and keep touched.json current. No AI involved.

The trace is append-only and its numbering (`seq`) is stable: the watcher
persists its read offsets and parser state in watch.json and resumes from
them. It rebuilds from scratch only when that state is missing or does not
match the trace (the rebuild is written as a new file so editors reload).
"""
import glob
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

from . import paths, usage
from .transcript import Parser

POLL = 0.3
SKIP_DIRS = {".git", ".aic", ".omc", ".claude", "node_modules", "__pycache__", ".venv", "venv",
             "dist", "build", ".next", ".cache", "target", "coverage", ".pytest_cache", "out"}
MAX_FILE = 512 * 1024
MAX_FILES = 5000
AGENT_TOOLS = ("Agent", "Task")
AGENT_QUIET = 120  # s without a transcript write after which an unfinished subagent counts as stopped
AGENT_STUCK = 1800  # ...even with a call in flight (its process was killed mid-call)


def log(msg):
    sys.stdout.write("aic watch: %s\n" % msg)
    sys.stdout.flush()


def to_ts(v):
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return paths.now()


# -- touched-line bookkeeping -------------------------------------------------
def apply_hunks(ranges, hunks, event_id, actor):
    """Move existing ranges through each hunk, then add the hunk's new lines.

    Hunks come in order with `start` in new-file coordinates, so once earlier
    hunks are applied the current file agrees with that numbering up to here.
    Context lines map 1:1, '-' lines vanish, everything after shifts by delta.
    """
    for h in hunks:
        if not isinstance(h, dict):
            continue
        start = int(h.get("start") or 1)
        lines = [l for l in (h.get("lines") or []) if isinstance(l, str)]
        o = n = start
        mapping, added = {}, []
        for l in lines:
            c = l[:1]
            if c == "-":
                o += 1
            elif c == "+":
                added.append(n)
                n += 1
            else:
                mapping[o] = n
                o += 1
                n += 1
        old_end, delta = o, n - o  # old lines [start, old_end) were covered

        def move(line, start=start, old_end=old_end, delta=delta, mapping=mapping):
            if line < start:
                return line
            if line >= old_end:
                return line + delta
            return mapping.get(line)  # None: the line was deleted

        kept = []
        for r in ranges:
            s = r["start"]
            while s <= r["end"] and move(s) is None:
                s += 1
            e = r["end"]
            while e >= s and move(e) is None:
                e -= 1
            if s <= e:
                kept.append(dict(r, start=move(s), end=move(e)))
        ranges[:] = kept
        if not added and lines:
            added = [start]  # pure deletion: mark where it happened
        run = None
        for p in added:
            if run and p == run["end"] + 1:
                run["end"] = p
            else:
                run = {"start": p, "end": p, "event": event_id, "actor": actor}
                ranges.append(run)
    ranges.sort(key=lambda r: r["start"])


# -- file snapshots: catch edits the worker makes through Bash ----------------
def _read_lines(path):
    try:
        if os.path.getsize(path) > MAX_FILE:
            return None
        with open(path, "rb") as f:
            data = f.read()
        if b"\0" in data[:4096]:
            return None
        return data.decode("utf-8", "replace").splitlines()
    except OSError:
        return None


def list_files(root):
    """Project files: git's view when available (respects .gitignore)."""
    try:
        p = subprocess.run(["git", "-c", "core.fsmonitor=", "-C", root, "ls-files", "-co", "--exclude-standard", "-z"],
                           capture_output=True, timeout=10)
        if p.returncode == 0:
            out = [rel for rel in p.stdout.decode("utf-8", "replace").split("\0")
                   if rel and not any(part in SKIP_DIRS for part in rel.split("/")[:-1])]
            return out[:MAX_FILES]
    except (OSError, subprocess.SubprocessError):
        pass
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for name in files:
            out.append(os.path.relpath(os.path.join(d, name), root))
            if len(out) >= MAX_FILES:
                return out
    return out


class Snapshot:
    """{rel: (mtime_ns, size, lines)}; a rescan reads only files whose stat changed.
    A file counts as deleted only if it is really gone (never because a
    listing was capped)."""

    def __init__(self, root):
        self.root = root
        self.files = {}
        self.scanned = False
        self.rescan()
        self.scanned = True

    def _stat(self, rel):
        try:
            st = os.stat(os.path.join(self.root, rel))
            return st.st_mtime_ns, st.st_size
        except OSError:
            return None

    def refresh(self, rel):
        st = self._stat(rel)
        lines = _read_lines(os.path.join(self.root, rel)) if st else None
        if lines is None:
            self.files.pop(rel, None)
        else:
            self.files[rel] = (st[0], st[1], lines)

    def rescan(self):
        """-> {rel: (old_lines, new_lines)} for files that changed since the last scan."""
        first = not self.scanned
        changed = {}
        for rel in set(list_files(self.root)) | set(self.files):
            old = self.files.get(rel)
            st = self._stat(rel)
            if st is None:
                if old is not None:
                    del self.files[rel]
                    changed[rel] = (old[2], [])
                continue
            if old is not None and (old[0], old[1]) == st:
                continue
            lines = _read_lines(os.path.join(self.root, rel))
            if lines is None:
                self.files.pop(rel, None)
                continue
            self.files[rel] = (st[0], st[1], lines)
            if old is None:
                if not first:
                    changed[rel] = ([], lines)
            elif old[2] != lines:
                changed[rel] = (old[2], lines)
        return changed


def diff_hunks(old, new):
    """Hunks in PROTOCOL format (no context) turning `old` lines into `new`."""
    import difflib
    hunks = []
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        lines = ["-" + l for l in old[i1:i2]] + ["+" + l for l in new[j1:j2]]
        hunks.append({"start": j1 + 1, "count": j2 - j1, "lines": lines})
    return hunks


class Watcher:
    def __init__(self, root):
        self.root = root
        self.sid = None
        self.snap = None

    # -- persisted state -----------------------------------------------------
    def _state_path(self):
        return os.path.join(self.dir, "watch.json")

    def save_state(self):
        paths.write_json(self._state_path(), {
            "session_id": self.sid, "seq": self.seq, "offsets": self.offsets,
            "parser": self.parser.to_state(),
            "agents": {aid: dict(a, parser=a["parser"].to_state()) for aid, a in self.agents.items()}})

    def resume(self):
        st = paths.read_json(self._state_path())
        trace = paths.read_jsonl(self.trace_path)
        last = trace[-1].get("seq", 0) if trace else 0
        if not (isinstance(st, dict) and st.get("session_id") == self.sid and st.get("seq") == last
                and isinstance(st.get("offsets"), dict)):
            return False
        self.seq = last
        self.offsets = st["offsets"]
        self.parser.from_state(st.get("parser") or {})
        for aid, a in (st.get("agents") or {}).items():
            if not isinstance(a, dict):
                continue
            sub = os.path.join(self.dir, "agents", aid + ".jsonl")
            tr = paths.read_jsonl(sub)
            if (tr[-1].get("seq", 0) if tr else 0) != a.get("seq"):  # (a crash between its append and save_state)
                if os.path.exists(sub):
                    os.unlink(sub)
                self.offsets.pop("agent:" + aid, None)
                continue  # read again from the start
            p = Parser(self.root, sidechain=True)
            p.from_state(a.get("parser") or {})
            self.agents[aid] = dict(a, parser=p)
        t = paths.read_json(os.path.join(self.dir, "touched.json"), {})
        self.touched = t if isinstance(t, dict) else {}
        return True

    def reset(self, sid):
        self.sid = sid
        self.dir = paths.session_dir(self.root, sid)
        os.makedirs(self.dir, exist_ok=True)
        self.parser = Parser(self.root)
        self.offsets = {}
        self.seq = 0
        self.touched = {}
        self.agents = {}  # subagent id -> {seq, calls, tool, type, description, parser}
        self.index = None  # agents.json as last written
        self.trace_path = os.path.join(self.dir, "trace.jsonl")
        self.tpath = paths.transcript_path(self.root, sid)
        self.snap = None
        if self.resume():
            log("resumed %s at seq %d" % (sid, self.seq))
        else:
            self.rebuild()
        self.snap = Snapshot(self.root)  # Bash attribution works from here on
        if self.parser.last_usage:
            usage.record_worker(self.dir, self.parser.model, self.parser.last_usage, self.parser.turn)

    def rebuild(self):
        """Everything that already exists, merged by time, written as a new
        file (new inode) so editors know to reload instead of appending."""
        for p in glob.glob(os.path.join(self.dir, "agents", "*.jsonl")):
            os.unlink(p)  # re-read from the start below (and their readers see the files go)
        evs = self.read_transcript() + self.read_side("human.jsonl") + self.read_side("control.jsonl")
        evs.sort(key=lambda e: e["ts"])
        turn = 0
        for ev in evs:
            if ev["kind"] == "prompt":
                turn = ev["turn"]
            elif ev.get("side"):
                ev["turn"] = turn
        final, self.trace_path = self.trace_path, self.trace_path + ".new"
        open(self.trace_path, "w").close()
        try:
            self.emit(evs, force=True)
            os.replace(self.trace_path, final)
        finally:
            self.trace_path = final
        log("rebuilt %s (%d events)" % (self.sid, len(evs)))

    # -- reading -------------------------------------------------------------
    def _new_records(self, path, key):
        """Complete new JSON objects since the last read (partial lines wait)."""
        if not path or not os.path.exists(path):
            return []
        off = self.offsets.get(key, 0)
        size = os.path.getsize(path)
        if size < off:
            off = 0
        if size == off:
            return []
        with open(path, "rb") as f:
            f.seek(off)
            chunk = f.read()
        end = chunk.rfind(b"\n")
        if end < 0:
            return []
        self.offsets[key] = off + end + 1
        out = []
        for raw in chunk[: end + 1].splitlines():
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def read_transcript(self):
        if not self.tpath:
            self.tpath = paths.transcript_path(self.root, self.sid)
        evs = []
        for rec in self._new_records(self.tpath, "transcript"):
            try:
                got = self.parser.feed(rec)
            except Exception as e:  # one odd record must not stop the trace
                log("skipped a transcript record: %r" % e)
                continue
            for ev in got:
                ev["ts"] = to_ts(ev.get("ts"))
                evs.append(ev)
        return evs

    def read_side(self, name):
        evs = []
        for rec in self._new_records(os.path.join(self.dir, name), name):
            ts = to_ts(rec.get("ts"))
            kind = str(rec.get("kind") or "human")
            ev = {"id": "%s-%.6f" % (kind, ts), "ts": ts, "kind": kind,
                  "turn": self.parser.turn, "side": True}
            if kind == "human":
                if not isinstance(rec.get("file"), str) or not isinstance(rec.get("hunks"), list):
                    continue
                ev.update(file=rec["file"], hunks=[h for h in rec["hunks"] if isinstance(h, dict)])
            else:
                ev.update({k: v for k, v in rec.items() if k not in ("ts", "kind")})
            evs.append(ev)
        return evs

    # -- subagents ------------------------------------------------------------
    def agents_dir(self):
        """Claude Code keeps a subagent's transcript next to its parent's:
        <transcript without .jsonl>/subagents/agent-<id>.jsonl (+ .meta.json)."""
        return os.path.join(os.path.splitext(self.tpath)[0], "subagents") if self.tpath else None

    def read_agents(self):
        """Each subagent's transcript -> agents/<id>.jsonl (trace events, own
        seq); agents.json indexes them by the trace id of the Agent call that
        started them. Returns True when anything was read."""
        d = self.agents_dir()
        read = False
        for p in sorted(glob.glob(os.path.join(d, "agent-*.jsonl"))) if d else []:
            aid = os.path.basename(p)[len("agent-"):-len(".jsonl")]
            a = self.agents.get(aid)
            if a is None:
                a = self.agents[aid] = {"seq": 0, "calls": 0, "tool": None, "type": None,
                                        "description": None, "parser": Parser(self.root, sidechain=True)}
            # the meta file can come after the transcript, and gets "finished" when the subagent is done
            mp = p[:-len(".jsonl")] + ".meta.json"
            try:
                mt = os.path.getmtime(mp)
            except OSError:
                mt = None
            if mt is not None and mt != a.get("meta_mtime"):
                meta = paths.read_json(mp)
                if isinstance(meta, dict) and isinstance(meta.get("toolUseId"), str):
                    a.update(tool=meta["toolUseId"], type=meta.get("agentType"), description=meta.get("description"),
                             finished=bool(meta.get("finished")), meta_mtime=mt)
                    read = True
            evs = []
            for rec in self._new_records(p, "agent:" + aid):
                read = True
                try:
                    got = a["parser"].feed(rec)
                except Exception as e:
                    log("skipped a subagent record: %r" % e)
                    continue
                for ev in got:
                    ev["ts"] = to_ts(ev.get("ts"))
                    a["seq"] += 1
                    ev["seq"] = a["seq"]
                    if ev["kind"] == "tool":
                        a["calls"] += 1
                        f = ev.get("file")
                        if self.snap and ev.get("hunks") and isinstance(f, str) and not os.path.isabs(f):
                            self.snap.refresh(f)  # (not the worker's next Bash call's edit)
                    evs.append(ev)
            if evs:
                os.makedirs(os.path.join(self.dir, "agents"), exist_ok=True)
                with open(os.path.join(self.dir, "agents", aid + ".jsonl"), "a", encoding="utf-8") as f:
                    for ev in evs:
                        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return read

    def write_index(self):
        """agents.json: {"t-<tool_use_id>": {agent, type, description, events,
        calls, state, running?, in?}}. `running` is the event of an Agent call
        still in flight (a trace has it only once it returns); `in` the subagent
        that made that call (absent: the worker itself). Written after the
        trace, so a returned call is never in neither."""
        d = self.agents_dir()
        if not d:
            return
        parsers = [(None, self.parser)] + [(aid, a["parser"]) for aid, a in self.agents.items()]
        busy = {tid for _, p in parsers for tid in p.tools}
        index = {}
        for aid, p in parsers:
            for tid, t in p.tools.items():
                if t.get("name") in AGENT_TOOLS:
                    ev = p.running_tool(tid)
                    ev["ts"] = to_ts(ev["ts"]) if ev.get("ts") else 0.0  # (never "now": the index would change every tick)
                    index["t-" + tid] = dict({"running": ev}, **({"in": aid} if aid else {}))
        for aid, a in self.agents.items():
            if not a["tool"]:
                continue
            try:
                quiet = paths.now() - os.path.getmtime(os.path.join(d, "agent-%s.jsonl" % aid))
            except OSError:
                quiet = AGENT_QUIET
            sp = a["parser"]
            st = "done" if a.get("finished") or (sp.stopped and not sp.tools) else \
                "running" if a["tool"] in busy or quiet < AGENT_QUIET or (sp.tools and quiet < AGENT_STUCK) \
                else "stopped"
            index.setdefault("t-" + a["tool"], {}).update(
                agent=aid, type=a["type"], description=a["description"], events=a["seq"], calls=a["calls"], state=st)
        if index != self.index:
            paths.write_json(os.path.join(self.dir, "agents.json"), index)
            self.index = index

    # -- Bash attribution ----------------------------------------------------
    def attribute_bash(self, evs):
        """After a worker Bash call, changes found in the tree become their own
        tool events. Files touched by this batch's Edit/Write/human events are
        refreshed first so they are not counted twice. Attribution means "seen
        after this Bash call": a subagent or background process editing at the
        same moment would be credited to it as well."""
        if self.snap is None:
            return evs
        for ev in evs:
            f = ev.get("file")
            if ev.get("kind") in ("human", "tool") and isinstance(f, str) and ev.get("tool") != "Bash" \
                    and not os.path.isabs(f):
                self.snap.refresh(f)
        out = []
        for ev in evs:
            out.append(ev)
            if ev.get("kind") == "tool" and ev.get("tool") == "Bash" and not ev.get("file"):
                for rel, (old, new) in sorted(self.snap.rescan().items()):
                    hunks = diff_hunks(old, new)
                    h = hunks[0] if hunks else {"start": 1, "count": 0}
                    out.append(dict(
                        ev, id="%s:%s" % (ev["id"], rel), file=rel, hunks=hunks, via="Bash",
                        title="Bash edit %s:%d-%d%s" % (rel, h["start"], h["start"] + max(h["count"], 1) - 1,
                                                        "" if new else " (deleted)")))
        return out

    # -- output --------------------------------------------------------------
    def emit(self, evs, force=False):
        if evs:
            with open(self.trace_path, "a", encoding="utf-8") as f:
                for ev in evs:
                    ev.pop("side", None)
                    self.seq += 1
                    ev["seq"] = self.seq
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                    if ev.get("hunks") and isinstance(ev.get("file"), str):
                        rs = self.touched.setdefault(ev["file"], [])
                        try:
                            apply_hunks(rs, ev["hunks"], ev["id"],
                                        "human" if ev["kind"] == "human" else "worker")
                        except (TypeError, ValueError, KeyError) as e:
                            log("bad hunk in %s: %r" % (ev["id"], e))
        if evs or force:
            paths.write_json(os.path.join(self.dir, "touched.json"), self.touched)
        self.save_state()

    def tick(self):
        sid = paths.attached(self.root)
        if not sid:
            return
        if sid != self.sid:
            self.reset(sid)
            return
        # human saves first: they refresh the snapshot, so a Bash call running
        # at the same moment isn't blamed for the human's edit
        before = dict(self.offsets)
        evs = self.read_side("human.jsonl") + self.read_transcript() + self.read_side("control.jsonl")
        agents = self.read_agents()
        if evs:
            self.emit(self.attribute_bash(evs))
        elif self.offsets != before or agents:
            self.save_state()
        self.write_index()
        if self.offsets.get("transcript") != before.get("transcript") and self.parser.last_usage:
            try:
                usage.record_worker(self.dir, self.parser.model, self.parser.last_usage, self.parser.turn)
            except Exception as e:  # display only
                log("usage: %r" % e)


def main(root):
    pidfile = os.path.join(paths.aic_dir(root), "watch.pid")
    old = paths.read_text(pidfile).strip()
    if old and old != str(os.getpid()) and paths.pid_alive(old):
        sys.stderr.write("aic watch: already running (pid %s)\n" % old)
        return 1
    paths.write_text(pidfile, str(os.getpid()))
    parent = os.getppid()
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    w = Watcher(root)
    try:
        while not stop:
            try:
                w.tick()
            except Exception as e:  # keep watching; the next tick may succeed
                log("tick failed: %r" % e)
                w.sid = None  # resume cleanly from persisted state next time
            if os.getppid() != parent:  # editor died without telling us
                break
            time.sleep(POLL)
    except KeyboardInterrupt:
        pass
    finally:
        if paths.read_text(pidfile).strip() == str(os.getpid()):
            os.unlink(pidfile)
    return 0
