"""Claude Code transcript JSONL -> aic trace events.

The transcript format is internal to Claude Code, so every assumption about it
lives in this one module. Thinking blocks are stored empty, so a tool call's
"why" is the visible assistant text written since the previous tool call.
"""
import json
import os
import re

MAX_PROMPT = 2000
MAX_HUNK_LINES = 400


def _rel(root, path):
    if not path:
        return None
    ap = os.path.abspath(path)
    if ap == root or ap.startswith(root + os.sep):
        return os.path.relpath(ap, root)
    return ap


def _first_line(s, n=100):
    s = (s or "").strip().splitlines()
    s = s[0] if s else ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _short(obj, n=300):
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return s if len(s) <= n else s[: n - 1] + "…"


MAX_DETAIL = 4000
FILE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "Read")


def _detail(name, inp):
    """A tool's input as a person reads it (its title shows only the start):
    a Bash command with its description, an Agent's prompt, else the input as
    JSON. None for file tools (their file and diff say it)."""
    if name in FILE_TOOLS or not isinstance(inp, dict) or not inp:
        return None
    if name == "Bash":
        cmd = inp.get("command")
        parts = [inp.get("description"), ("$ " + cmd) if isinstance(cmd, str) else None]
    elif name in ("Agent", "Task"):
        parts = [inp.get("description"), inp.get("prompt")]
    else:
        parts = [json.dumps(inp, ensure_ascii=False, indent=1)]
    text = "\n".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
    return (text if len(text) <= MAX_DETAIL else text[: MAX_DETAIL - 1] + "…") or None


def hunks_from_result(result):
    """Normalized hunks from an Edit/Write toolUseResult."""
    if not isinstance(result, dict):
        return []
    out = []
    for p in result.get("structuredPatch") or []:
        if not isinstance(p, dict):
            continue
        lines = list(p.get("lines") or [])  # never truncated: line shifting needs every +/- line
        out.append({"start": p.get("newStart", 1), "count": p.get("newLines", 0),
                    "lines": lines})
    if not out and result.get("type") == "create" and isinstance(result.get("content"), str):
        lines = result["content"].splitlines()
        out.append({"start": 1, "count": len(lines),
                    "lines": ["+" + l for l in lines[:MAX_HUNK_LINES]]})
    return out


def _command_prompt(text):
    """'<command-name>/x</command-name>...<command-args>a</command-args>' -> '/x a'."""
    name = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
    args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
    if not name:
        return None
    s = name.group(1).strip()
    if args and args.group(1).strip():
        s += " " + args.group(1).strip()
    return s


class Parser:
    """Stateful, incremental: feed records in file order, get events back."""

    def __init__(self, root, sidechain=False):
        self.root = root
        self.sidechain = sidechain  # a subagent's own transcript (every record is a sidechain one)
        self.turn = 0
        self.pending_text = []   # assistant text since the last tool call
        self.pending_ts = None
        self.tools = {}          # tool_use_id -> partial event
        self.last_prompt_parent = None
        self.last_usage = None   # usage of the newest assistant message (context size)
        self.model = None
        self.stopped = False     # the newest assistant message ended its turn

    STATE_KEYS = ("turn", "pending_text", "pending_ts", "tools", "last_prompt_parent", "last_usage", "model",
                  "stopped")

    def to_state(self):
        return {k: getattr(self, k) for k in self.STATE_KEYS}

    def from_state(self, st):
        for k in self.STATE_KEYS:
            if k in st:
                setattr(self, k, st[k])

    # -- helpers -------------------------------------------------------------
    def _flush_text(self, ts, uid):
        text = "\n\n".join(t for t in self.pending_text if t.strip()).strip()
        self.pending_text = []
        if not text:
            return []
        return [{"id": "x-" + uid, "ts": self.pending_ts or ts, "kind": "text",
                 "turn": self.turn, "text": text}]

    def _title(self, name, inp, rel, hunks):
        if name in ("Edit", "Write", "MultiEdit", "NotebookEdit") and rel:
            if hunks:
                h = hunks[0]
                end = h["start"] + max(h["count"], 1) - 1
                return "%s %s:%d-%d" % (name, rel, h["start"], end)
            return "%s %s" % (name, rel)
        if name == "Read" and rel:
            return "Read " + rel
        if name == "Bash":
            return "Bash: " + _first_line(inp.get("description") or inp.get("command"), 80)
        if name in ("Grep", "Glob"):
            return "%s %s" % (name, _first_line(inp.get("pattern"), 60))
        if name in ("Agent", "Task"):
            return "%s: %s" % (name, _first_line(inp.get("description") or inp.get("prompt"), 70))
        for k in ("description", "query", "url", "command", "pattern"):
            if isinstance(inp.get(k), str):
                return "%s: %s" % (name, _first_line(inp[k], 70))
        return name

    # -- main entry ----------------------------------------------------------
    def feed(self, rec):
        if not isinstance(rec, dict) or (rec.get("isSidechain") and not self.sidechain) \
                or rec.get("isCompactSummary"):
            return []
        typ = rec.get("type")
        if typ == "user":
            self.stopped = False
        ts = rec.get("timestamp")
        uid = rec.get("uuid") or ""
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        content = msg.get("content")
        if isinstance(content, list):
            content = [c for c in content if isinstance(c, dict)]
        out = []

        att = rec.get("attachment") if typ == "attachment" else None
        if isinstance(att, dict) and att.get("type") == "queued_command" and att.get("prompt"):
            # A message the human sent while the worker was busy, absorbed mid-turn.
            text = re.sub(r"</?pasted_content[^>]*>", "", att["prompt"]).strip()
            return self._flush_text(ts, uid) + [{
                "id": "p-" + uid, "ts": ts, "kind": "prompt", "turn": self.turn,
                "text": text[:MAX_PROMPT], "midturn": True}]

        if typ == "system" and rec.get("subtype") == "compact_boundary":
            meta = rec.get("compactMetadata") if isinstance(rec.get("compactMetadata"), dict) else {}
            post = meta.get("postTokens")
            if isinstance(post, int):  # context size right after compaction
                self.last_usage = {"input_tokens": post}
            return self._flush_text(ts, uid) + [{
                "id": "c-" + uid, "ts": ts, "kind": "compact", "turn": self.turn,
                "trigger": meta.get("trigger"), "pre": meta.get("preTokens"), "post": post}]

        if typ == "system" and rec.get("subtype") in ("turn_duration", "stop_hook_summary"):
            return self._flush_text(ts, uid)

        if typ == "user":
            if isinstance(content, list) and any(c.get("type") == "tool_result" for c in content):
                for c in content:
                    if c.get("type") == "tool_result":
                        ev = self._finish_tool(c, rec.get("toolUseResult"), ts)
                        if ev:
                            out.append(ev)
                return out
            if rec.get("isMeta"):
                return []
            origin = rec.get("origin") if isinstance(rec.get("origin"), dict) else {}
            if origin.get("kind") not in (None, "human"):
                # Harness-generated turn (e.g. a background task finished): not the human.
                body = content if isinstance(content, str) else ""
                summ = re.search(r"<summary>(.*?)</summary>", body, re.S)
                self.turn += 1
                return self._flush_text(ts, uid) + [{
                    "id": "p-" + uid, "ts": ts, "kind": "prompt", "turn": self.turn, "system": True,
                    "text": "(%s) %s" % (origin["kind"], summ.group(1).strip() if summ else "")}]
            if isinstance(content, str):
                text = content
                if "<command-name>" in text:
                    text = _command_prompt(text) or text
                elif text.startswith("<local-command") or text.startswith("<command-message>"):
                    return []
            elif isinstance(content, list):
                text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
            else:
                return []
            text = re.sub(r"</?pasted_content[^>]*>", "", text).strip()
            if not text.strip() or text.startswith("[Request interrupted"):
                return []
            # Local slash commands (/model, /mcp...) leave no worker turn behind.
            if re.match(r"^/(model|mcp|usage|config|clear|status|cost|help|resume|login|logout)\b", text):
                return []
            out += self._flush_text(ts, uid)
            # Same parent as the previous prompt = the human re-sent/edited it: same turn.
            parent = rec.get("parentUuid")
            if self.turn and parent and parent == self.last_prompt_parent:
                out.append({"id": "p-" + uid, "ts": ts, "kind": "prompt", "turn": self.turn,
                            "text": text[:MAX_PROMPT], "resent": True})
                return out
            self.last_prompt_parent = parent
            self.turn += 1
            out.append({"id": "p-" + uid, "ts": ts, "kind": "prompt", "turn": self.turn,
                        "text": text[:MAX_PROMPT]})
            return out

        if typ == "assistant":
            self.stopped = msg.get("stop_reason") == "end_turn"
        if typ == "assistant" and isinstance(msg.get("usage"), dict):
            self.last_usage, self.model = msg["usage"], msg.get("model") or self.model
        if typ == "assistant" and isinstance(content, list):
            for c in content:
                ct = c.get("type")
                if ct == "text" and c.get("text", "").strip():
                    if not self.pending_text:
                        self.pending_ts = ts
                    self.pending_text.append(c["text"])
                elif ct == "tool_use":
                    why = "\n\n".join(t for t in self.pending_text if t.strip()).strip()
                    self.pending_text = []
                    self.tools[c.get("id")] = {"name": c.get("name"), "input": c.get("input") or {},
                                               "why": why, "ts": ts, "turn": self.turn}
            if msg.get("stop_reason") == "end_turn":
                out += self._flush_text(ts, uid)
        return out

    def _finish_tool(self, block, result, ts):
        tid = block.get("tool_use_id")
        t = self.tools.pop(tid, None)
        if not t:
            return None
        return self._tool_event(tid, t, result, ts, bool(block.get("is_error")))

    def running_tool(self, tid):
        """The event of tool call `tid` still in flight (no result yet), or None."""
        t = self.tools.get(tid)
        if not t:
            return None
        ev = self._tool_event(tid, t, None, None, False)
        ev["turn"] = t.get("turn", ev["turn"])  # (the turn it was made in)
        return ev

    def _tool_event(self, tid, t, result, ts, error):
        name, inp = t["name"], t["input"]
        path = inp.get("file_path") or inp.get("notebook_path") or inp.get("path")
        rel = _rel(self.root, path) if isinstance(path, str) else None
        hunks = hunks_from_result(result) if name in ("Edit", "Write", "MultiEdit") else []
        return {
            "id": "t-" + (tid or ""), "ts": t["ts"] or ts, "kind": "tool", "turn": self.turn,
            "tool": name, "title": self._title(name, inp, rel, hunks), "why": t["why"],
            "file": rel if name in FILE_TOOLS else None,
            "hunks": hunks, "input": _short(inp), "detail": _detail(name, inp), "error": error,
        }

    def pending_tools(self):
        """Tool calls in flight (no result yet) — shown live by the watcher."""
        return dict(self.tools)
