# Data for the context view draft: what one agent had in its context window at every API call, rebuilt from
# Claude Code's transcripts of a repo.
#   build_ctx.py REPO [--max-mb N] [SESSION_ID... | --all | --recent N]   data.json on stdout (default: the newest session)
#   build_ctx.py --check DATA.json                               checks a built data.json, report on stderr
#
# Recent Claude Code versions write every request into the transcript: `api-request` (keep, tail, shapeHash),
# `api-request-blob` (a message by hash) and `api-request-shape` (system and tools). A call's messages are its
# base call's first `keep` messages + `tail`. Blobs are content-addressed: sha256 of the message as compact JSON
# with sorted keys. Messages the transcript already holds are not written as blobs: assistant responses (all
# records of one message id), user records, and runs of consecutive user records sent as one message; their
# hashes are rebuilt the same way. A hash found in none of these is looked up in the project's other files.
# Sessions without request records are estimated from the conversation records (marked "estimated").
import base64, datetime, glob, hashlib, json, os, re, shlex, struct, sys
from collections import defaultdict

CONFIG = os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude"))

# Prices per million tokens, from the claude-api skill bundled with Claude Code 2.1.306 (model table "cached:
# 2026-10-06"; shared/models.md; shared/prompt-caching.md "Economics": cache writes 1.25x input for the 5-minute
# TTL, 2x for 1 hour; cache reads $0.20 on Opus 5.5 and Sonnet 5.5). Models not listed get tokens only.
PRICES = {
    "claude-opus-5-5": {"in": 4.00, "read": 0.20, "w5": 5.00, "w1h": 8.00, "out": 20.00},
    "claude-sonnet-5-5": {"in": 2.00, "read": 0.20, "w5": 2.50, "w1h": 4.00, "out": 10.00},
}
PRICE_SOURCE = ("claude-api skill (Claude Code 2.1.306), model table cached 2026-10-06; "
                "cache writes 1.25x input (5 min) / 2x (1 h), per its prompt-caching guide")
WINDOWS = {"claude-opus-5-5": 1_000_000, "claude-sonnet-5-5": 1_000_000, "claude-haiku-4-5": 200_000}


def H(m):
    return hashlib.sha256(json.dumps(m, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def norm(c):
    return [{"type": "text", "text": c}] if isinstance(c, str) else (c or [])


def epoch(ts):
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() if ts else None


# ---- reading files ----

_recs = {}


def records(f):
    if f not in _recs:
        out = []
        with open(f) as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        _recs[f] = out
    return _recs[f]


def responses(recs):
    """message id -> the assistant records of that response, in order."""
    out = {}
    for r in recs:
        if r.get("type") == "assistant" and isinstance(r.get("message"), dict):
            out.setdefault(r["message"].get("id") or r.get("uuid"), []).append(r)
    return out


def response_message(rr):
    return {"role": "assistant", "content": [c for x in rr for c in x["message"].get("content") or []]}


_stores = {}


def store(f):
    """hash -> message for everything one transcript file can supply."""
    if f in _stores:
        return _stores[f]
    recs, blobs, run = records(f), {}, []

    def flush():
        for a in range(len(run)):
            for b in range(a + 1, len(run) + 1):
                m = {"role": "user", "content": [x for u in run[a:b] for x in norm(u["message"].get("content"))]}
                blobs.setdefault(H(m), m)
        run.clear()

    for r in recs:
        t = r.get("type")
        if t == "api-request-blob":
            blobs[r["hash"]] = r["message"]
        elif t == "assistant":
            flush()
        elif t == "user" and isinstance(r.get("message"), dict):
            blobs.setdefault(H(r["message"]), r["message"])
            run.append(r)
    flush()
    for rr in responses(recs).values():
        m = response_message(rr)
        blobs.setdefault(H(m), m)
    _stores[f] = blobs
    return blobs


# ---- strings, labels ----

class Strings:
    def __init__(self):
        self.ix, self.list = {}, []

    def add(self, s):
        if s not in self.ix:
            self.ix[s] = len(self.list)
            self.list.append(s)
        return self.ix[s]


S = Strings()
ROOT = ""


def rel(p):
    if not isinstance(p, str):
        return ""
    if ROOT and p.startswith(ROOT + "/"):
        return p[len(ROOT) + 1:]
    home = os.path.expanduser("~")
    return "~" + p[len(home):] if p.startswith(home + "/") else p


def one_line(s, n=90):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


LANG = {".c": "c", ".h": "c", ".py": "python", ".js": "javascript", ".mjs": "javascript", ".ts": "typescript",
        ".sh": "bash", ".md": "markdown", ".json": "json", ".html": "xml", ".xml": "xml", ".css": "css",
        ".lua": "lua", ".vim": "vim", ".yaml": "yaml", ".yml": "yaml", ".toml": "ini", ".rs": "rust", ".go": "go",
        ".cpp": "cpp", ".hpp": "cpp", ".java": "java", ".rb": "ruby", ".txt": "plaintext"}


def lang_of(p):
    base = os.path.basename(p or "")
    if base in ("Makefile", "makefile"):
        return "makefile"
    return LANG.get(os.path.splitext(base)[1].lower(), "")


def action_label(name, inp):
    inp = inp if isinstance(inp, dict) else {}
    if name == "Read":
        rng = ""
        if inp.get("offset") or inp.get("limit"):
            a = int(inp.get("offset") or 1)
            rng = f" {a}-{a + int(inp.get('limit') or 2000) - 1}"
        return f"Read {rel(inp.get('file_path'))}{rng}"
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return f"{name} {rel(inp.get('file_path') or inp.get('notebook_path'))}"
    if name == "Bash":
        cmd = re.sub(r"^\s*cd\s+(\"[^\"]*\"|\S+)\s*&&\s*", "", inp.get("command", ""))  # the usual cd into the repo
        return "Bash " + one_line(cmd, 80)
    if name in ("Grep", "Glob"):
        where = f" in {rel(inp['path'])}" if inp.get("path") else ""
        return f"{name} {one_line(inp.get('pattern', ''), 50)}{where}"
    if name in ("Agent", "Task"):
        return f"{name} {inp.get('subagent_type') or ''}: {one_line(inp.get('description', ''), 60)}"
    if name == "Skill":
        return f"Skill {inp.get('skill', '')}"
    if name in ("WebFetch",):
        return f"WebFetch {one_line(inp.get('url', ''), 70)}"
    first = next((v for v in inp.values() if isinstance(v, str)), "")
    return f"{name} {one_line(first, 60)}".strip()


def img_info(src):
    """'png 1000x100' and a token estimate (w*h/750 after the API's 1568px long-edge resize)."""
    data = (src or {}).get("data") or ""
    mt = ((src or {}).get("media_type") or "image/?").split("/")[-1]
    w = h = 0
    try:
        raw = base64.b64decode(data[:4000] + "=" * (-len(data[:4000]) % 4))
        if raw[:8] == b"\x89PNG\r\n\x1a\n":
            w, h = struct.unpack(">II", raw[16:24])
        elif raw[:2] == b"\xff\xd8":
            i = 2
            while i < len(raw) - 9:
                if raw[i] != 0xFF:
                    break
                mk, ln = raw[i + 1], struct.unpack(">H", raw[i + 2:i + 4])[0]
                if mk in (0xC0, 0xC1, 0xC2):
                    h, w = struct.unpack(">HH", raw[i + 5:i + 9])
                    break
                i += 2 + ln
    except Exception:
        pass
    if w and h:
        s = min(1.0, 1568 / max(w, h))
        tok = int(w * s * h * s / 750)
    else:
        tok = 1000
    return f"{mt} {w}x{h}" if w else mt, tok


INJ_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)


def inj_source(t):
    head = t[:600]
    if "CLAUDE.md" in head or "claudeMd" in head or "Codebase and user instructions" in head:
        return "CLAUDE.md"
    if "MEMORY.md" in head or "auto-memory" in head:
        return "memory"
    m = re.match(r"\s*(?:<system-reminder>\s*)?([A-Za-z]+(?::[A-Za-z_*|]+)?) hook (?:additional context|blocking error|feedback)", t)
    if m:
        return "hook " + m.group(1)
    if "hook" in head.lower():
        return "hook output"
    if t.lstrip().startswith("<total_tokens>"):
        return "token count"
    if "<local-command" in head or "<command-name>" in head:
        return "slash command"
    if t.lstrip().startswith("<task-notification>"):
        return "task notification"
    return "system reminder"


def split_text(t):
    """[(kind, source, text)]: injected segments split out of the text they ride in."""
    out, pos = [], 0
    for m in INJ_RE.finditer(t):
        if t[pos:m.start()].strip():
            out.append(("prompt", None, t[pos:m.start()]))
        out.append(("injected", inj_source(m.group(0)), m.group(0)))
        pos = m.end()
    if t[pos:].strip() or not out:
        out.append(("prompt", None, t[pos:]))
    res = []
    for k, src, s in out:
        if k == "prompt":
            st = s.lstrip()
            if st.startswith(("<local-command", "<command-name>", "<command-message>", "<total_tokens>",
                              "<task-notification>", "<bash-input>", "<bash-stdout>", "<user-prompt-submit-hook>")):
                k, src = "injected", inj_source(s)
            elif st.startswith("This session is being continued from a previous conversation"):
                k, src = "summary", "compaction summary"
            elif st.startswith("[Request interrupted"):
                k, src = "injected", "interrupt"
            else:
                src = "prompt"
        res.append((k, src, s))
    return res


# ---- one agent ----

class Agent:
    def __init__(self, f, meta, session):
        self.f, self.meta, self.session = f, meta, session
        self.items, self.calls, self.inst = [], [], []
        self.tool_use = {}  # tool_use id -> action item
        self.result_of = {}  # tool_use id -> result item
        self.missing = 0
        self.broken = 0  # calls whose base call is not in the file (their kept messages are unknown)
        self.found_elsewhere = 0
        self.estimated = False

    # items -----------------------------------------------------------
    def new_item(self, **kw):
        kw["id"] = len(self.items)
        self.items.append(kw)
        return kw["id"]

    def blocks_of(self, msg, made=None, resp=None):
        """A message -> [(content key, item fields)] in order, one per item."""
        role, content = msg.get("role"), msg.get("content")
        out = []
        if role == "system" and not content:
            eff = (msg.get("output_config") or {}).get("effort")
            t = f"(effort: {eff})" if eff else "(empty system message)"
            out.append(("sys-empty:" + t, dict(k="injected", s="effort" if eff else "system message", t=t, w=4)))
            return out
        blocks = norm(content)
        has_results = any(b.get("type") == "tool_result" for b in blocks)
        # token weights for an assistant response: thinking gets the response's thinking tokens, the rest share
        # the remaining output tokens by size
        aw = {}
        if role == "assistant" and resp:
            u = resp[-1]["message"].get("usage") or {}
            out_t = u.get("output_tokens") or 0
            th_t = (u.get("output_tokens_details") or {}).get("thinking_tokens") or 0
            th = [i for i, b in enumerate(blocks) if b.get("type") in ("thinking", "redacted_thinking")]
            rest = [i for i in range(len(blocks)) if i not in th]
            sig = {i: len(blocks[i].get("signature") or blocks[i].get("data") or "") + len(blocks[i].get("thinking") or "") + 1 for i in th}
            for i in th:
                aw[i] = th_t * sig[i] / sum(sig.values()) if th_t else len(blocks[i].get("thinking") or "") / 4
            size = {i: len(json.dumps(blocks[i], ensure_ascii=False)) + 1 for i in rest}
            for i in rest:
                aw[i] = max(out_t - th_t, 0) * size[i] / sum(size.values()) if out_t else size[i] / 4
        for bi, b in enumerate(blocks):
            bt = b.get("type")
            key = role + ":" + H(b)
            if role == "system":
                if bt == "text":
                    t = b.get("text", "")
                    src = inj_source(t)
                    out.append((key, dict(k="injected", s=src, t=t, w=len(t) / 4)))
                elif bt == "tool_addition":
                    d = ((b.get("tool") or {}).get("definition")) or b.get("tool") or {}
                    t = json.dumps(d, ensure_ascii=False, indent=1)
                    out.append((key, dict(k="injected", s="tool added: " + str(d.get("name", "?")), t=t, w=len(t) / 4)))
                else:
                    t = json.dumps(b, ensure_ascii=False)
                    out.append((key, dict(k="injected", s="system " + str(bt), t=t, w=len(t) / 4)))
            elif role == "assistant":
                w = aw.get(bi)
                if bt == "text":
                    t = b.get("text", "")
                    out.append((key, dict(k="text", s="agent text", t=t, w=w if w is not None else len(t) / 4, m=made)))
                elif bt in ("thinking", "redacted_thinking"):
                    t = b.get("thinking") or ""
                    out.append((key, dict(k="thinking", s="thinking" if t else "thinking (not returned)", t=t,
                                          w=w if w is not None else len(t) / 4, m=made)))
                elif bt in ("tool_use", "server_tool_use"):
                    inp = b.get("input") or {}
                    t = json.dumps(inp, ensure_ascii=False, indent=1)
                    f = dict(k="action", s=action_label(b.get("name", "?"), inp), t=t,
                             w=w if w is not None else len(t) / 4 + 20, m=made, tool=b.get("name"), tuid=b.get("id"))
                    p = inp.get("file_path") or inp.get("notebook_path")
                    if p:
                        f["p"] = rel(p)
                    out.append((key, f))
                else:
                    t = json.dumps(b, ensure_ascii=False)
                    out.append((key, dict(k="text", s=str(bt), t=t, w=w if w is not None else len(t) / 4, m=made)))
            else:  # user
                if bt == "text":
                    for si, (k, src, t) in enumerate(split_text(b.get("text", ""))):
                        if k == "prompt" and has_results:  # text riding with tool results ("Tool loaded.") is not typed
                            k, src = "injected", "with tool results"
                        out.append((f"{key}:{si}", dict(k=k, s=src, t=t, w=len(t) / 4)))
                elif bt == "image":
                    info, tok = img_info(b.get("source"))
                    out.append((key, dict(k="prompt", s="image " + info, t=f"[image {info}: not embedded in the draft]", w=tok)))
                elif bt == "tool_result":
                    parts, segs, w, imgs = [], [], 0, []
                    for c in norm(b.get("content")):
                        if c.get("type") == "text":
                            for k, src, t in split_text(c.get("text", "")):
                                if k == "injected":
                                    segs.append((src, t))
                                else:
                                    parts.append(t)
                                    w += len(t) / 4
                        elif c.get("type") == "image":
                            info, tok = img_info(c.get("source"))
                            parts.append(f"[image {info}: not embedded in the draft]")
                            imgs.append(info)
                            w += tok
                        else:
                            t = json.dumps(c, ensure_ascii=False)
                            parts.append(t)
                            w += len(t) / 4
                    f = dict(k="result", s="result", t="\n".join(parts), w=w + 10, tuid=b.get("tool_use_id"))
                    if b.get("is_error"):
                        f["err"] = 1
                    out.append((key, f))
                    for si, (src, t) in enumerate(segs):
                        out.append((f"{key}:inj{si}", dict(k="injected", s=src, t=t, w=len(t) / 4)))
                else:
                    t = json.dumps(b, ensure_ascii=False)
                    out.append((key, dict(k="prompt", s=str(bt), t=t, w=len(t) / 4)))
        return out

    def finish_item(self, f):
        """Store an item's text in the string table and link tool calls with their results."""
        t = f.pop("t", "")
        f["x"] = S.add(t)
        f["n"] = len(t)
        iid = self.new_item(**f)
        tu = f.get("tuid")
        if tu and f["k"] == "action":
            self.tool_use[tu] = iid
        elif tu and f["k"] == "result":
            self.result_of[tu] = iid
        return iid

    # calls -----------------------------------------------------------
    def build(self, project_files):
        recs = records(self.f)
        reqs = [r for r in recs if r.get("type") == "api-request"]
        if reqs:
            self.build_exact(recs, reqs, project_files)
        else:
            self.estimated = True
            self.build_estimated(recs)
        self.post()

    def resolve(self, h, own, project_files):
        m = own.get(h)
        if m is not None:
            return m
        for g in project_files:
            if g != self.f:
                m = store(g).get(h)
                if m is not None:
                    self.found_elsewhere += 1
                    own[h] = m
                    return m
        self.missing += 1
        return None

    def build_exact(self, recs, reqs, project_files):
        own = store(self.f)
        shapes = {r["shapeHash"]: r["shape"] for r in recs if r.get("type") == "api-request-shape"}
        resp_by_req, msg_resp, block_resp = {}, {}, {}
        for mid, rr in responses(recs).items():
            ref = rr[0].get("requestRef")
            if ref:
                resp_by_req[ref] = rr  # a retried request keeps its last response
            msg_resp[H(response_message(rr))] = rr
            for b in response_message(rr)["content"]:
                block_resp.setdefault(b.get("id") or H(b), rr)
        reqid_call = {}
        dropped = defaultdict(list)  # requestId -> [(messageIndex, blockIndex)]
        compact_at, last_req_pos, pos_of = [], -1, {}
        for i, r in enumerate(recs):
            t = r.get("type")
            if t == "api-request":
                pos_of[r["id"]] = i
            elif t == "system" and r.get("subtype") == "compact_boundary":
                compact_at.append(i)
            elif t == "attachment" and (r.get("attachment") or {}).get("type") == "thinking_drop":
                a = r["attachment"]
                nd = a.get("newlyDropped") or {}
                if nd.get("first"):
                    dropped[a.get("requestId")].append((nd["first"]["messageIndex"], nd["first"]["blockIndex"]))
        shape_items = {}
        by_id, prev_pos = {}, -1
        for ci, r in enumerate(reqs):
            base = by_id.get(r.get("base"))
            # a request without a base (after a compaction, or a full resend) starts over: the previous call is
            # still the reference, so messages sent again unchanged keep their items
            ref = base or (self.calls[-1] if self.calls else None)
            prev_inst = ref["inst"] if ref else []
            keep = r.get("keep", 0)
            kept = keep if base else 0
            inst = list(prev_inst[:kept])
            dropped_inst = list(prev_inst[kept:])
            free_items = defaultdict(list)  # content key -> items of dropped messages, for rewritten messages
            for di in dropped_inst:
                for key, iid in self.inst[di]["blocks"]:
                    free_items[key].append(iid)
            used = set()
            for h in r.get("tail", []):
                # an unchanged message sent again (e.g. a full resend) keeps its instance
                same = next((di for di in dropped_inst if self.inst[di]["h"] == h and di not in used), None)
                if same is not None:
                    used.add(same)
                    inst.append(same)
                    for key, iid in self.inst[same]["blocks"]:
                        if iid in free_items.get(key, []):
                            free_items[key].remove(iid)
                    continue
                m = self.resolve(h, own, project_files)
                rr = msg_resp.get(h)
                if rr is None and m is not None and m.get("role") == "assistant":
                    # a response sent back changed (e.g. a block dropped): find it by any block it still has
                    rr = next((block_resp[k] for b in norm(m.get("content")) for k in (b.get("id"), H(b)) if k in block_resp), None)
                made = by_id[rr[0]["requestRef"]]["i"] if rr and rr[0].get("requestRef") in by_id else None
                if m is None:
                    iid = self.finish_item(dict(k="missing", s="message not found", t=f"[message {h[:12]}… not found in any transcript of the project]", w=0))
                    blocks = [("missing:" + h, iid)]
                else:
                    blocks = []
                    for key, f in self.blocks_of(m, made, rr):
                        if free_items.get(key):
                            blocks.append((key, free_items[key].pop(0)))  # same block, rewritten message
                        else:
                            blocks.append((key, self.finish_item(f)))
                self.inst.append({"h": h, "blocks": blocks})
                inst.append(len(self.inst) - 1)
            sh = r.get("shapeHash")
            if ref and ref.get("sh") == sh:
                shape_items[sh] = ref["shape"]
            else:  # a new or changed system/tools: unchanged system blocks keep their item, the rest are new
                shape = shapes.get(sh) or {}
                ids = []
                old = {self.items[i]["x"]: i for i in (ref["shape"] if ref else []) if self.items[i]["k"] == "system"}
                for b in shape.get("system") or []:
                    t = b.get("text", "") if isinstance(b, dict) else str(b)
                    same = old.pop(S.ix.get(t, -1), None)
                    ids.append(same if same is not None else self.finish_item(dict(k="system", s="system prompt", t=t, w=len(t) / 4)))
                tools = shape.get("tools") or []
                if tools:
                    tj = [json.dumps(tl, ensure_ascii=False, indent=1) for tl in tools]
                    iid = self.finish_item(dict(k="tools", s=f"tool definitions ({len(tools)})", t="", w=sum(len(x) for x in tj) / 4))
                    self.items[iid]["tx"] = [S.add(x) for x in tj]
                    self.items[iid]["names"] = [tl.get("name", "?") for tl in tools]
                    self.items[iid]["n"] = sum(len(x) for x in tj)
                    ids.insert(0, iid)  # tools render before system
                if not shape:
                    ids.append(self.finish_item(dict(k="missing", s="system + tools not found", t="[request shape not found]", w=0)))
                shape_items[sh] = ids
            rr = resp_by_req.get(r["id"])
            u = (rr[-1]["message"].get("usage") or {}) if rr else None
            pos = pos_of[r["id"]]
            comp = any(prev_pos < p < pos for p in compact_at) and keep == 0
            prev_pos = pos
            if keep and not base:
                self.broken += 1
            call = {"i": ci, "id": r["id"], "sh": sh, "t": epoch(r.get("timestamp")), "model": (r.get("params") or {}).get("model"),
                    "base": base["i"] if base else None, "keep": keep, "inst": inst, "shape": shape_items[sh],
                    "u": usage_of(u), "comp": comp, "src": r.get("querySource")}
            if rr:
                reqid_call[rr[-1].get("requestId")] = ci
                call["model"] = rr[-1]["message"].get("model") or call["model"]
            by_id[r["id"]] = call
            self.calls.append(call)
        # thinking blocks the API dropped (prefix mismatch): weight 0 at that call
        for rq, lst in dropped.items():
            ci = reqid_call.get(rq)
            if ci is None:
                continue
            c = self.calls[ci]
            for mi, bi in lst:
                if mi < len(c["inst"]):
                    th = [iid for _, iid in self.inst[c["inst"][mi]]["blocks"] if self.items[iid]["k"] == "thinking"]
                    if th:
                        c.setdefault("drop", []).append(th[min(bi, len(th) - 1)])

    def build_estimated(self, recs):
        """Older transcripts: the conversation records give the messages; one call per assistant response."""
        msgs = []  # [(hash, message)]
        resp_seen = set()
        sys_id = self.finish_item(dict(k="system", s="system prompt + tools (estimated)", t="[not in the transcript: estimated from the first call's usage]", w=0))
        comp_next = False
        by_mid = responses(recs)
        prev_hashes = []
        for r in recs:
            t = r.get("type")
            if t == "system" and r.get("subtype") == "compact_boundary":
                msgs, comp_next = [], True
            elif t == "user" and isinstance(r.get("message"), dict):
                m = r["message"]
                msgs.append((H(m), m))
            elif t == "attachment":
                a = r.get("attachment") or {}
                txt = a.get("content") if isinstance(a.get("content"), str) else a.get("text")
                if isinstance(txt, str) and txt.strip() and a.get("type") not in ("hook_success",):
                    m = {"role": "system", "content": [{"type": "text", "text": txt}]}
                    msgs.append((H(m), m))
            elif t == "assistant" and isinstance(r.get("message"), dict):
                mid = r["message"].get("id") or r.get("uuid")
                if mid in resp_seen:
                    continue
                resp_seen.add(mid)
                rr = by_mid[mid]
                hashes = [h for h, _ in msgs]
                keep = 0
                while keep < min(len(hashes), len(prev_hashes)) and hashes[keep] == prev_hashes[keep]:
                    keep += 1
                base = self.calls[-1] if self.calls else None
                inst = list(base["inst"][:keep]) if base else []
                for h, m in msgs[keep:]:
                    rr2 = by_mid.get(m.get("_mid")) if m.get("role") == "assistant" else None
                    blocks = [(key, self.finish_item(f)) for key, f in self.blocks_of(m, m.get("_made"), rr2)]
                    self.inst.append({"h": h, "blocks": blocks})
                    inst.append(len(self.inst) - 1)
                u = rr[-1]["message"].get("usage")
                ci = len(self.calls)
                self.calls.append({"i": ci, "id": mid, "t": epoch(rr[0].get("timestamp")), "model": rr[-1]["message"].get("model"),
                                   "base": base["i"] if base else None, "keep": keep, "inst": inst, "shape": [sys_id],
                                   "u": usage_of(u), "comp": comp_next})
                comp_next = False
                prev_hashes = hashes
                am = response_message(rr)
                am["_mid"], am["_made"] = mid, ci
                msgs.append((H({k: v for k, v in am.items() if not k.startswith("_")}), am))
        # the system prompt and tools: what the first call's usage leaves after its messages
        if self.calls and self.calls[0]["u"]:
            tot = sum(self.calls[0]["u"][:4])
            msgw = sum(self.items[iid]["w"] for ii in self.calls[0]["inst"] for _, iid in self.inst[ii]["blocks"])
            self.items[sys_id]["w"] = max(tot - msgw, 0)

    # after the calls -------------------------------------------------
    def post(self):
        order_of = []
        for c in self.calls:
            order = list(c["shape"]) + [iid for ii in c["inst"] for _, iid in self.inst[ii]["blocks"]]
            order_of.append(order)
            ref = c["base"] if c["base"] is not None else c["i"] - 1  # no base: compared with the call before
            base_set = set(order_of[ref]) if ref >= 0 else set()
            cur = set(order)
            for iid in cur - base_set:
                it = self.items[iid]
                if "e" not in it:
                    it["e"] = c["i"]
            for iid in base_set - cur:
                self.items[iid].setdefault("l", c["i"])
        self.order_of = order_of
        for tu, rid in self.result_of.items():
            aid = self.tool_use.get(tu)
            if aid is not None:
                self.items[rid]["a"] = aid
                self.items[aid]["r"] = rid
                a = self.items[aid]
                self.items[rid]["s"] = a["s"]
                self.items[rid]["tool"] = a.get("tool")
                if a.get("p"):
                    self.items[rid]["p"] = a["p"]


def usage_of(u):
    if not u:
        return None
    cc = u.get("cache_creation") or {}
    w = u.get("cache_creation_input_tokens") or 0
    w1h = cc.get("ephemeral_1h_input_tokens") or 0
    return [u.get("input_tokens") or 0, u.get("cache_read_input_tokens") or 0, w - w1h, w1h, u.get("output_tokens") or 0]


# ---- file changes across the project (for stale reads) ----

WRITE_CMD = re.compile(r"\bsed\s+(-[a-zA-Z]*i|--in-place)|[^&2]>|\btee\b|\bmv\b|\bcp\b|\brm\b|\btruncate\b|\bpatch\b|"
                       r"\bgit\s+(checkout|restore|reset|stash|apply|merge|rebase|pull|am)\b|\bperl\s+-[a-z]*i|"
                       r"open\([^)]*,\s*['\"][wa]|\.write_text\(|\.write\(")
PY_WRITE = re.compile(r"open\([^)]*,\s*['\"][wa]|\.write_text\(|\.write\(")
QUOTED_FILE = re.compile(r"(?:open\(\s*|\w\s*=\s*|[\[(,]\s*)['\"]([\w./~+-]+\.[A-Za-z]\w{0,4})['\"]")  # p='x.c', open('x.c', ['a.c', 'b.c']
READ_VERBS = {"cat", "sed", "head", "tail", "nl", "grep", "rg", "less", "awk", "wc", "bat", "diff"}
GIT_WRITES = {"checkout", "restore", "reset", "stash", "apply", "am", "merge", "rebase", "pull"}


def file_like(tok):
    tok = tok.strip("'\"")
    if not tok or tok.startswith("-") or "*" in tok or "$" in tok or not re.match(r"^[\w./~+@-]+$", tok):
        return False
    ext = os.path.splitext(tok)[1].lower()
    return ext in LANG or ext in FILE_EXT or os.path.basename(tok) in ("Makefile", "makefile")


def split_unquoted(s, seps):
    """Split on any of seps outside single or double quotes."""
    out, cur, q, i = [], [], None, 0
    while i < len(s):
        ch = s[i]
        if q:
            if ch == q:
                q = None
            elif ch == "\\" and q == '"' and i + 1 < len(s):
                cur.append(ch)
                i += 1
                ch = s[i]
        elif ch in "'\"":
            q = ch
        else:
            sep = next((x for x in seps if s.startswith(x, i)), None)
            if sep:
                out.append("".join(cur))
                cur = []
                i += len(sep)
                continue
        cur.append(ch)
        i += 1
    out.append("".join(cur))
    return out


def segments(cmd, cwd):
    """(cwd, words of the first command of each pipeline) for a shell command, following `cd`. Quote-aware, so a
    sed script like 's|a|b|' stays one word."""
    cwd = cwd or ROOT
    for seg in split_unquoted(cmd, ("&&", "||", ";", "\n")):
        first = split_unquoted(seg, ("|",))[0]
        try:
            words = shlex.split(first)
        except ValueError:
            words = first.split()
        if not words:
            continue
        if words[0] == "cd" and len(words) > 1:
            d = os.path.expanduser(words[1].strip("'\""))
            cwd = d if d.startswith("/") else os.path.normpath(os.path.join(cwd, d))
            continue
        yield cwd, seg, words


def absolute(tok, cwd):
    p = os.path.expanduser(tok.strip("'\""))
    return os.path.normpath(p if p.startswith("/") else os.path.join(cwd, p))


def cmd_write_files(cmd, cwd):
    """Absolute paths a shell command writes, as far as patterns see: redirect targets, sed/perl -i, tee, rm,
    truncate, patch, mv (both sides), the cp target, git checkout/restore/... naming files, and the file names a
    python heredoc assigns or opens when it writes (p='src/a.c', open('a.c', 'w')). A heuristic: commands can
    write files in ways no pattern sees."""
    out = set()
    py = bool(PY_WRITE.search(cmd))
    for d, seg, words in segments(cmd, cwd):
        for i, t in enumerate(words):
            m = re.match(r"^\d?>>?(.*)$", t)
            if m and not t.startswith("2>"):
                target = m.group(1) or (words[i + 1] if i + 1 < len(words) else "")
                if file_like(target):
                    out.add(absolute(target, d))
        w, args = words[0], [a for a in words[1:] if file_like(a)]
        if w in ("sed", "perl") and any(a.startswith("-") and "i" in a.lstrip("-")[:3] for a in words[1:]) \
                or w in ("tee", "rm", "truncate", "patch", "mv") or w == "git" and len(words) > 1 and words[1] in GIT_WRITES:
            out |= {absolute(a, d) for a in args}
        elif w == "cp" and words[-1:] and file_like(words[-1]):
            out.add(absolute(words[-1], d))
        if py and w.startswith("python"):
            out |= {absolute(q, d) for q in QUOTED_FILE.findall(cmd) if file_like(q)}
            py = False
    return out


def cmd_read_files(cmd, cwd):
    """Absolute paths of the files a read-only shell command shows (cat, sed -n, head, tail, grep… naming them).
    Empty when the command writes through python."""
    if PY_WRITE.search(cmd):
        return set()
    out = set()
    for d, seg, words in segments(cmd, cwd):
        if words[0] in READ_VERBS and not (words[0] == "sed" and any(a.startswith("-i") for a in words[1:])):
            out |= {absolute(a, d) for a in words[1:] if file_like(a) and ">" not in a}
    return out


def project_changes(files):
    """Edits of every agent in the project, with their result time: Edit/Write/MultiEdit/NotebookEdit calls and the
    files commands write (cmd_write_files), and files Claude Code noticed changing (edited_text_file attachments).
    -> {abspath: [(t, transcript file, tool_use id, label, by command)]}, {abspath: [(t, file)]}"""
    edits, seen = defaultdict(list), defaultdict(list)
    for f in files:
        uses, done = {}, {}
        for r in records(f):
            t = r.get("type")
            if t == "assistant":
                for b in (r.get("message") or {}).get("content") or []:
                    if b.get("type") == "tool_use" and b.get("name") in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"):
                        uses[b["id"]] = (b, r.get("cwd"))
            elif t == "user":
                c = (r.get("message") or {}).get("content")
                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in uses:
                            done[b["tool_use_id"]] = (epoch(r.get("timestamp")), bool(b.get("is_error")))
            elif t == "attachment":
                a = r.get("attachment") or {}
                if a.get("type") == "edited_text_file" and a.get("filename"):
                    seen[a["filename"]].append((epoch(r.get("timestamp")), f))
        for tu, (b, cwd) in uses.items():
            if tu not in done or done[tu][1]:
                continue
            inp = b.get("input") or {}
            if b["name"] == "Bash":
                cmd = inp.get("command", "")
                if WRITE_CMD.search(cmd):
                    for p in cmd_write_files(cmd, cwd):
                        edits[p].append((done[tu][0], f, tu, f"{os.path.basename(p)}: Bash " + one_line(cmd, 60), True))
            else:
                p = inp.get("file_path") or inp.get("notebook_path")
                if p:
                    edits[p].append((done[tu][0], f, tu, f"{b['name']} {rel(p)}", False))
    return edits, seen


def stale_events(agent, changes, all_agents_of_session):
    """Per read (a Read result, or a read-only command's output naming files): the changes to its file after the
    read, while it stayed in the context. kinds: 0 own Edit/Write, 1 own command, 2 another agent of this session,
    3 another session, 4 noticed by Claude Code (edited_text_file: the user, a linter or a command changed it)."""
    edits, seen = changes
    session_files = set(all_agents_of_session)
    read_time, cwd_of = {}, {}
    for r in records(agent.f):
        if r.get("type") == "user" and isinstance((r.get("message") or {}).get("content"), list):
            for b in r["message"]["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    read_time[b.get("tool_use_id")] = epoch(r.get("timestamp"))
        elif r.get("type") == "assistant":
            for b in (r.get("message") or {}).get("content") or []:
                if b.get("type") == "tool_use":
                    cwd_of[b.get("id")] = r.get("cwd")
    last_t = agent.calls[-1]["t"] if agent.calls else None
    for it in agent.items:
        if it["k"] != "result" or it.get("tool") not in ("Read", "Bash") or "a" not in it or it.get("err"):
            continue
        act = agent.items[it["a"]]
        tu = act.get("tuid")
        t0 = read_time.get(tu)
        try:
            inp = json.loads(S.list[act["x"]])
        except ValueError:
            continue
        paths = {inp.get("file_path")} if it["tool"] == "Read" else cmd_read_files(inp.get("command", ""), cwd_of.get(tu))
        paths.discard(None)
        t1 = agent.calls[it["l"]]["t"] if it.get("l") is not None else last_t
        if not t0 or not paths or t1 is None:
            continue
        ev = []
        for path in paths:
            for t, f, tu2, label, by_cmd in edits.get(path, []):
                if t0 < t <= t1:
                    kind = (1 if by_cmd else 0) if f == agent.f else (2 if f in session_files else 3)
                    ev.append([round(t, 1), kind, agent.tool_use.get(tu2, -1) if kind < 2 else -1, label])
            # Claude Code noticed a change it did not make with Edit/Write; skip it when a command of the project
            # (this read's own included) wrote the file just before
            for t, f in seen.get(path, []):
                if t0 < t <= t1 and not any(t0 - 5 <= e[0] <= t for e in edits.get(path, [])) \
                        and path not in (cmd_write_files(inp.get("command", ""), cwd_of.get(tu)) if it["tool"] == "Bash" else ()):
                    ev.append([round(t, 1), 4, -1, f"{rel(path)} changed by the user or a program (Claude Code noticed)"])
        if ev:
            it["st"] = sorted(ev)


# ---- why it decided ----

STOP = set("""self this that with from into have none true false null return const static void char size_t uint
int long else then elif while break continue print printf path file line lines text name type data value values list
dict item items index json args kwargs main test tests make build echo grep head tail true false string number object
command description file_path old_string new_string replace_all offset limit pattern output_mode content""".split())
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}(?:\.[a-z]{1,4})?")


FILE_EXT = {".png", ".jpg", ".jpeg", ".log", ".jsonl", ".svg", ".csv", ".diff", ".patch", ".cfg", ".ini", ".conf", ".tpl",
            ".mk", ".lock", ".out", ".tgz", ".gz", ".tar", ".pdf", ".so", ".a", ".o"}


def cmd_paths(cmd):
    """File-like words of a shell command (with an extension), repo-relative where they can be."""
    out = set()
    for tok in re.split(r"[\s;|&<>()=,]+", cmd):
        tok = tok.strip("'\"`")
        if not tok or tok.startswith(("-", "http", "/dev/", "$")) or "*" in tok or "@" in tok:
            continue
        ext = os.path.splitext(tok)[1].lower()
        if (ext in LANG or ext in FILE_EXT) and re.match(r"^[\w+-][\w.+-]*$", os.path.basename(tok)) and re.match(r"^[\w./~+-]+$", tok):
            out.add(rel(os.path.expanduser(tok)) if tok.startswith(("/", "~")) else tok)
    return out


def same_file(a, b):
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def links_of(agent):
    """For each action: the items most likely behind it ("likely" links), from the context of the call that made it:
    reads of the file it acts on (Read, or an earlier command naming the file), the prompt of the turn, its text and
    thinking just before in the same response, and results that mention names in its input, rarest first."""
    results = [it for it in agent.items if it["k"] == "result"]
    words = {it["id"]: set(WORD.findall(S.list[it["x"]][:200000])) for it in results}
    df_all = defaultdict(int)
    for ws in words.values():
        for w in ws:
            df_all[w] += 1
    common = {w for w, n in df_all.items() if n > max(3, 0.15 * len(results))}
    rare = max(2, 0.03 * len(results))  # a mention counts when it shares a name this rare
    pathy = set(re.split(r"[/\-_.]+", ROOT + "/" + os.path.expanduser("~") + "/" + CONFIG)) | {"claude", "jsonl", "projects", "scratchpad", "tmp"}
    stop = STOP | common | pathy
    cmd_files = {}

    def files_of(iid):  # the files a result is about
        r = agent.items[iid]
        if iid not in cmd_files:
            if r.get("tool") == "Read":
                cmd_files[iid] = {r.get("p")} if r.get("p") else set()
            elif "a" in r:
                a = agent.items[r["a"]]
                try:
                    inp = json.loads(S.list[a["x"]])
                except ValueError:
                    inp = {}
                cmd_files[iid] = cmd_paths(inp.get("command", "")) | ({rel(inp["path"])} if isinstance(inp.get("path"), str) else set())
            else:
                cmd_files[iid] = set()
        return cmd_files[iid]

    by_made = defaultdict(list)
    for it in agent.items:
        if it.get("m") is not None:
            by_made[it["m"]].append(it["id"])
    for it in agent.items:
        if it["k"] != "action" or it.get("m") is None or it["m"] >= len(agent.order_of):
            continue
        mc = it["m"]
        ctx = agent.order_of[mc]
        links = []
        try:
            inp = json.loads(S.list[it["x"]])
        except ValueError:
            inp = {}
        paths = set()
        for key in ("file_path", "notebook_path"):
            if isinstance(inp.get(key), str):
                paths.add(rel(inp[key]))
        if it.get("tool") == "Bash":
            paths |= cmd_paths(inp.get("command", ""))
        # reads of the file it acts on
        if paths:  # the latest two per file, four at most
            per = defaultdict(int)
            for iid in reversed(ctx):
                r = agent.items[iid]
                if r["k"] == "result" and r.get("tool") in ("Read", "Bash", "Grep"):
                    # sorted: set order is hash-randomized, and the first match names the link
                    hit = next((p for p in sorted(paths) for q in sorted(files_of(iid)) if same_file(p, q)), None)
                    if hit and per[hit] < 2 and sum(per.values()) < 4:
                        per[hit] += 1
                        links.append([iid, ("read of " if r.get("tool") == "Read" else "saw ") + os.path.basename(hit)])
        # the prompt that started the turn
        prompt = next((iid for iid in reversed(ctx) if agent.items[iid]["s"] in ("prompt", "compaction summary")), None)
        if prompt is not None:
            links.append([prompt, "the prompt of this turn"])
        # its text and thinking just before, in the same response
        for iid in by_made[mc]:
            if iid == it["id"]:
                break
            if agent.items[iid]["k"] in ("text", "thinking"):
                links.append([iid, "its " + agent.items[iid]["k"] + " just before"])
        # results that mention names in the action's input, rarest names first
        text = " ".join(v for v in inp.values() if isinstance(v, str)) if isinstance(inp, dict) else S.list[it["x"]]
        mine = {w for w in WORD.findall(text) if w not in stop and w.lower() not in stop}
        if mine:
            linked = {l[0] for l in links}
            scored = []
            for iid in ctx:
                if agent.items[iid]["k"] == "result" and iid not in linked:
                    hit = mine & words[iid]
                    if any(df_all[w] <= rare for w in hit):
                        hit = sorted(hit, key=lambda w: (df_all[w], w))  # rarest first, then by name: not set order
                        scored.append((sum(1 / df_all[w] for w in hit), iid, hit[:3]))
            scored.sort(reverse=True)
            for score, iid, hit in scored[:3]:
                if score >= 0.34:
                    links.append([iid, "mentions " + ", ".join(hit)])
        it["lk"] = links


# ---- sessions ----

def session_title(recs):
    t = [r.get("aiTitle") for r in recs if r.get("type") == "ai-title" and r.get("aiTitle")]
    if t:
        return t[-1]
    for r in recs:
        if r.get("type") == "user" and isinstance((r.get("message") or {}).get("content"), str):
            return one_line(r["message"]["content"], 70)
    return ""


def agent_json(a, t0):
    calls = []
    for c in a.calls:
        d = {"t": round(c["t"] - t0, 1) if c["t"] else None, "m": c["model"], "b": c["base"], "k": c["keep"],
             "tl": c["inst"][c["keep"]:] if c["base"] is not None else c["inst"], "sh": c["shape"], "u": c["u"]}
        if c["comp"]:
            d["cmp"] = 1
        if c.get("drop"):
            d["drop"] = c["drop"]
        calls.append(d)
    items = []
    for it in a.items:
        d = {k: v for k, v in it.items() if k not in ("id", "tuid")}
        d["w"] = round(d.get("w", 0), 1)
        if d.get("st"):
            d["st"] = [[round(e[0] - t0, 1)] + e[1:] for e in d["st"]]
        items.append(d)
    return {"calls": calls, "inst": [[iid for _, iid in m["blocks"]] for m in a.inst], "items": items}


def build(root, which, max_mb=None):
    global ROOT
    ROOT = os.path.abspath(root)
    proj = os.path.join(CONFIG, "projects", re.sub(r"[/.]", "-", ROOT))
    mains = sorted(glob.glob(os.path.join(proj, "*.jsonl")), key=os.path.getmtime)
    allfiles = mains + sorted(glob.glob(os.path.join(proj, "*", "subagents", "*.jsonl")))
    if which == "all":
        chosen = mains
    elif isinstance(which, int):
        chosen = [f for f in mains if any(r.get("type") == "assistant" for r in records(f))][-which:]
    else:
        chosen = [os.path.join(proj, s + ".jsonl") for s in which]
    changes = project_changes(allfiles)
    sessions, stats = [], {"missing": 0, "found_elsewhere": 0, "calls": 0, "broken": 0}
    t0 = None
    for f in chosen:
        recs = records(f)
        if not any(r.get("type") == "assistant" for r in recs):
            continue
        sid = os.path.basename(f)[:-6]
        subs = sorted(glob.glob(os.path.join(proj, sid, "subagents", "*.jsonl")))
        agents = [Agent(f, {"kind": "main", "name": "main"}, sid)]
        for g in subs:
            meta = {}
            try:
                meta = json.load(open(g[:-6] + ".meta.json"))
            except (OSError, ValueError):
                pass
            agents.append(Agent(g, {"kind": "subagent", "name": meta.get("agentType") or "subagent",
                                    "desc": meta.get("description", ""), "tu": meta.get("toolUseId"),
                                    "model": meta.get("model")}, sid))
        out_agents = []
        for a in agents:
            a.build(allfiles)
            if not a.calls:
                continue
            stale_events(a, changes, [x.f for x in agents])
            links_of(a)
            stats["missing"] += a.missing
            stats["found_elsewhere"] += a.found_elsewhere
            stats["calls"] += len(a.calls)
            stats["broken"] += a.broken
            out_agents.append(a)
        if not out_agents:
            continue
        ts = [c["t"] for a in out_agents for c in a.calls if c["t"]]
        if t0 is None or min(ts) < t0:
            t0 = min(ts)
        sessions.append((sid, session_title(recs), out_agents, min(ts), max(ts)))
    # a size budget (a published page has to stay under 16 MB): the newest sessions first, each main agent before
    # its subagents; what does not fit is left out and listed
    T = Strings()
    out, left_out, used = [], [], 0
    for sid, title, agents, a0, a1 in sorted(sessions, key=lambda s: -s[3]):
        keep = []
        for a in agents:
            aj = dict(a.meta, est=a.estimated, missing=a.missing, **agent_json(a, t0))
            new = {i for it in aj["items"] for i in ([it["x"]] if it.get("x") is not None else []) + it.get("tx", [])}
            fresh = {i for i in new if S.list[i] not in T.ix}
            size = len(json.dumps(aj, separators=(",", ":"))) + sum(len(json.dumps(S.list[i], ensure_ascii=False)) for i in fresh)
            if max_mb and used + size > max_mb * 1e6 and (out or keep):
                left_out.append({"session": sid, "agent": a.meta.get("name"), "desc": a.meta.get("desc", ""), "mb": round(size / 1e6, 1)})
                continue
            used += size
            for it in aj["items"]:
                if it.get("x") is not None:
                    it["x"] = T.add(S.list[it["x"]])
                if it.get("tx"):
                    it["tx"] = [T.add(S.list[i]) for i in it["tx"]]
            keep.append(aj)
        if keep:
            out.append({"id": sid, "title": title, "start": round(a0 - t0), "end": round(a1 - t0), "agents": keep})
    out.sort(key=lambda s: s["start"])
    stats["left_out"] = left_out
    data = {"meta": {"repo": ROOT, "built": datetime.datetime.now().isoformat(timespec="seconds"), "t0": t0,
                     "prices": PRICES, "price_source": PRICE_SOURCE, "windows": WINDOWS, "stats": stats},
            "sessions": out, "S": T.list}
    for x in left_out:
        print(f"left out for size: {x['session'][:8]} {x['agent']} {x['desc']} ({x['mb']} MB)", file=sys.stderr)
    print(f"sessions {len(sessions)}, calls {stats['calls']}, blobs found in other files {stats['found_elsewhere']}, "
          f"missing {stats['missing']}", file=sys.stderr)
    return data


# ---- checks on a built data.json (reconstructs calls the way the page does) ----

def check(path):
    d = json.load(open(path))
    S_ = d["S"]
    worst, n, est, bad_keep, scale = 0.0, 0, 0, 0, []
    for s in d["sessions"]:
        for a in s["agents"]:
            items, inst, calls = a["items"], a["inst"], a["calls"]
            insts, orders = [], []
            for ci, c in enumerate(calls):
                prev = insts[c["b"]] if c["b"] is not None else []
                cur = prev[: c["k"]] + c["tl"]
                insts.append(cur)
                order = c["sh"] + [iid for ii in cur for iid in inst[ii]]
                orders.append(order)
                drop = set(c.get("drop") or [])
                wsum = sum(items[i]["w"] for i in order if i not in drop)
                if c["u"]:
                    total = sum(c["u"][:4])
                    f = total / wsum if wsum else 0
                    scale.append(f)
                    toks = [round(items[i]["w"] * f) for i in order if i not in drop]
                    diff = abs(sum(toks) - total)
                    worst = max(worst, diff / max(len(toks), 1))
                    if diff > len(toks) / 2 + 1:
                        print(f"  token sum off: {s['id'][:8]} {a['name']} call {ci}: {sum(toks)} vs {total}", file=sys.stderr)
                    n += 1
                else:
                    est += 1
                # enter/leave against keep: what entered is in the tail, what left was past keep in the base call
                # (a call without a base is compared with the call before; nothing of it is kept)
                ref = c["b"] if c["b"] is not None else ci - 1
                if ref >= 0:
                    base_order = set(orders[ref])
                    kept = {iid for ii in prev[: c["k"]] for iid in inst[ii]}  # system/tools enter by shape
                    for i in set(order) - base_order:
                        if items[i].get("e") != ci:
                            bad_keep += 1
                        if i in kept:
                            bad_keep += 1
                    for i in base_order - set(order):
                        if items[i].get("l") != ci:
                            bad_keep += 1
                        if i in kept:
                            bad_keep += 1
    scale.sort()
    q = lambda p: scale[int(p * (len(scale) - 1))] if scale else 0
    print(f"calls with usage {n}, without (estimated scale) {est}; worst rounding error per item {worst:.3f} tokens", file=sys.stderr)
    print(f"scale (real tokens / estimated): p5 {q(.05):.2f} median {q(.5):.2f} p95 {q(.95):.2f}", file=sys.stderr)
    print(f"enter/leave disagreeing with keep: {bad_keep}", file=sys.stderr)
    print(f"missing blobs: {d['meta']['stats']['missing']} (found in other files: {d['meta']['stats']['found_elsewhere']})", file=sys.stderr)
    print(f"strings {len(S_)}, {sum(len(x) for x in S_) / 1e6:.1f} MB of text", file=sys.stderr)


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--check":
        check(args[1])
        sys.exit(0)
    max_mb = None
    if "--max-mb" in args:
        i = args.index("--max-mb")
        max_mb = float(args[i + 1])
        del args[i:i + 2]
    root = args[0] if args else "."
    rest = args[1:]
    if not rest:
        which = 1
    elif rest[0] == "--all":
        which = "all"
    elif rest[0] == "--recent":
        which = int(rest[1])
    else:
        which = rest
    # the page embeds this in a <script>: no "</" may close it early
    sys.stdout.write(json.dumps(build(root, which, max_mb), separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/"))
