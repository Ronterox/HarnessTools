# Data for the structure draft, the way aigource would build it from Claude Code transcripts:
# repo tree -> files -> classes -> functions, every change mapped to the defs it landed in (each file rebuilt
# as it was at that moment), with its turn (the prompt), the agent's comment before it, which lines of today's
# code it wrote (AI blame), and flags: edited without reading it, untested or failing afterwards, the next
# prompt reading like a correction, the turn interrupted. Plus complexity per def.
import datetime, difflib, glob, json, os, re, subprocess, sys
from collections import Counter, defaultdict

root = os.path.abspath(sys.argv[1])
SCOPE = tuple(sys.argv[2:]) or ("aigource/src/", "aic/aic/")  # repo-relative folders to map
# Claude Code keeps a repo's transcripts under its path with "/" (and ".") turned into "-".
PROJ = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude")), "projects",
                    re.sub(r"[/.]", "-", os.path.abspath(root)))
FILES = [f for f in subprocess.run(["git", "-C", root, "ls-files", *SCOPE], capture_output=True, text=True).stdout.split()
         if f.endswith((".c", ".py")) and not f.endswith("__init__.py")]

# ---- code structure ----

def defs_c(text):
    out, L = [], text.split("\n")
    for i, l in enumerate(L):
        m = re.match(r"^(?:static\s+)?(?:inline\s+)?[A-Za-z_][\w\s\*]*?\b(\w+)\s*\([^;]*$", l)
        if not m or l.startswith((" ", "\t", "#", "}")) or m.group(1) in ("if", "for", "while", "switch"):
            continue
        j = i
        while j < len(L) and "{" not in L[j] and ";" not in L[j]:
            j += 1
        if j >= len(L) or ";" in L[j].split("{")[0]:
            continue
        k = j
        while k < len(L) and L[k] != "}" and not (k == j and L[k].rstrip().endswith("}")):
            k += 1
        out.append((m.group(1), "function", i + 1, k + 1))
    return out


def defs_py(text):  # by indentation, so half-edited files still parse
    L, out, stack = text.split("\n"), [], []
    for i, l in enumerate(L):
        m = re.match(r"^(\s*)(?:async\s+)?(def|class)\s+(\w+)", l)
        if not m:
            continue
        ind = len(m.group(1))
        while stack and (stack[-1][0] >= ind or stack[-1][2] < i + 1):  # not inside a def that already ended
            stack.pop()
        key = ".".join([s[1] for s in stack] + [m.group(3)])
        end = i + 1
        for j in range(i + 1, len(L)):
            s = L[j]
            if s.strip() and (len(s) - len(s.lstrip())) <= ind and not s.lstrip().startswith((")", "]", "}")):
                break
            if s.strip():
                end = j + 1
        out.append((key, "class" if m.group(2) == "class" else "function", i + 1, end))
        stack.append((ind, m.group(3), end))  # what's indented under it, up to its end, is inside it
    return out


def defs(p, t):
    return defs_c(t) if p.endswith(".c") else defs_py(t)


def complexity(path, lines):
    """1 + branch points, and the deepest nesting below the def itself."""
    text = "\n".join(lines)
    if path.endswith(".c"):
        text = re.sub(r"/\*.*?\*/|//[^\n]*|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'", "", text, flags=re.S)
        cx = len(re.findall(r"\b(?:if|for|while|case)\b|&&|\|\||\?", text))
        d = deep = 0
        for ch in text:
            d += (ch == "{") - (ch == "}")
            deep = max(deep, d)
        return 1 + cx, max(0, deep - 1)
    text = re.sub(r"#[^\n]*|\"\"\".*?\"\"\"|'''.*?'''|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'", "", text, flags=re.S)
    cx = len(re.findall(r"\b(?:if|elif|for|while|except|and|or|case)\b", text))
    ind = [len(l) - len(l.lstrip()) for l in lines if l.strip()]
    return 1 + cx, max(0, (max(ind) - ind[0]) // 4 - 1) if ind else 0


# ---- patches, with the line origins they carry (AI blame) ----

def apply(old, hunks, check=False, origins=None, cid=None, removed=None):
    """New text (and origins when given): kept lines keep theirs, added lines get cid. Removed lines'
    (old line number, origin) go to `removed`."""
    ol, new, at, no = old.split("\n"), [], 0, []
    for h in hunks:
        s = h["oldStart"] - 1 if h.get("oldLines", 1) else h["oldStart"]
        new += ol[at:s]
        if origins is not None:
            no += origins[at:s]
        at = s
        for x in h["lines"]:
            if x[:1] in (" ", "-"):
                if check and (at >= len(ol) or ol[at] != x[1:]):
                    return None
                if x[:1] == " ":
                    new.append(x[1:])
                    if origins is not None:
                        no.append(origins[at] if at < len(origins) else -1)
                elif removed is not None and origins is not None and at < len(origins):
                    removed.append((at + 1, origins[at]))
                at += 1
            elif x[:1] == "+":
                new.append(x[1:])
                if origins is not None:
                    no.append(cid)
    new += ol[at:]
    if origins is not None:
        no += origins[at:]
        return "\n".join(new), no
    return "\n".join(new)


def has_new(text, hunks):
    """The file already reads as these hunks leave it: a change recorded twice."""
    tl = text.split("\n")
    for h in hunks:
        at = h["newStart"] - 1
        for x in h["lines"]:
            if x[:1] in (" ", "+"):
                if at >= len(tl) or tl[at] != x[1:]:
                    return False
                at += 1
    return True


def relocate(text, hunks):
    """Each hunk's new side found in text (later edits shift lines): its start line there, or None."""
    tl, starts = text.split("\n"), []
    for h in hunks:
        block = [x[1:] for x in h["lines"] if x[:1] in (" ", "+")]
        if not block or not any(b.strip() for b in block):
            starts.append(h["newStart"])
            continue
        hits = [i for i in range(len(tl) - len(block) + 1) if tl[i] == block[0] and tl[i:i + len(block)] == block]
        if not hits:
            return None
        starts.append(min(hits, key=lambda i: abs(i + 1 - h["newStart"])) + 1)
    return starts


_git = {}
def git_text(rel, ts):
    """The file as of the last commit before ts: a baseline when the transcripts lost track of it."""
    h = subprocess.run(["git", "-C", root, "log", "-1", "--format=%H", f"--before={ts}", "--", rel],
                       capture_output=True, text=True).stdout.strip()
    if not h:
        return None
    if (h, rel) not in _git:
        r = subprocess.run(["git", "-C", root, "show", f"{h}:{rel}"], capture_output=True, text=True)
        _git[h, rel] = r.stdout if r.returncode == 0 else None
    return _git[h, rel]


def nlines(text):
    """Lines in a text the way an editor counts them: a final newline ends the last line, it doesn't start one."""
    return len((text[:-1] if text.endswith("\n") else text).split("\n"))


def first_line(t):
    for l in t.strip().split("\n"):
        if l.strip():
            return l.strip()[:140]
    return ""


def prompt_of(r, c):
    """A prompt's text when the record starts a turn: typed or pasted (the <pasted_content> tag dropped).
    Slash commands, Claude Code's own records (task notifications, command output) and meta records don't."""
    if not isinstance(c, str) or r.get("isMeta") or (r.get("origin") or {}).get("kind", "human") != "human":
        return None
    c = c.lstrip()
    if c.startswith("<pasted_content"):
        c = c.split(">", 1)[-1]
    elif c.startswith(("<", "Caveat:")):
        return None
    return c if c.strip() else None


def rel_of(path, cwd=None):
    p = path if os.path.isabs(path) else os.path.normpath(os.path.join(cwd or root, path))
    if p.startswith(root + "/"):
        rel = os.path.relpath(p, root)
        if rel.startswith(SCOPE) and rel.endswith((".c", ".py")):
            return rel
    tail = path.lstrip("./")
    hits = [f for f in FILES if f.endswith("/" + tail)]
    return hits[0] if len(hits) == 1 else None


def shown(path, cwd=None):
    """A path as the page shows it: repo-relative, ~/..., or absolute."""
    p = path if os.path.isabs(path) else os.path.normpath(os.path.join(cwd or root, path))
    if p.startswith(root + "/"):
        return os.path.relpath(p, root)
    home = os.path.expanduser("~")
    return "~" + p[len(home):] if p.startswith(home + "/") else p


def cmd_reads(cmd, cwd):
    """Files (and line ranges) a command reads: cat, head, less, bat, nl, sed -n 'a,bp'.
    (shown path, repo-relative path when it's one we map or None, first line, last line)"""
    out = []
    for seg in re.split(r"&&|\|\||;|\||\n", cmd):
        seg = seg.strip()
        m = re.match(r"cd\s+(\S+)", seg)
        if m:
            cwd = os.path.normpath(os.path.join(cwd, os.path.expanduser(m.group(1).strip("'\""))))
            continue
        m = re.match(r"(cat|head|less|bat|batcat|nl|sed)\b(.*)", seg)
        if not m:
            continue
        prog, rest = m.groups()
        if prog == "sed" and re.search(r"(^|\s)-i", rest):
            continue
        for tok in re.findall(r"(?<![\w-])[\w./~-]*[\w~]/?[\w.-]*\.[A-Za-z]\w*\b", rest):
            tok = os.path.expanduser(tok)
            if not os.path.isfile(tok if os.path.isabs(tok) else os.path.join(cwd, tok)):
                continue  # a sed pattern or a variable, not a file (or one gone since)
            rel, where = rel_of(tok, cwd), shown(tok, cwd)
            if prog == "sed":
                rngs = [(int(a), int(b)) for a, b in re.findall(r"(\d+),(\d+)p", rest)]
                out += [(where, rel, a, b) for a, b in rngs] or [(where, rel, 1, 10 ** 9)]
            elif prog == "head":
                n = re.search(r"-n\s*(\d+)|-(\d+)\b", rest)
                out.append((where, rel, 1, int(n.group(1) or n.group(2)) if n else 10))
            else:
                out.append((where, rel, 1, 10 ** 9))
    return out


# A command segment (see `segments`) that starts with:
VERIFY = re.compile(r"(?:(?:ba)?sh\s+|python3?\s+|uv\s+run\s+)?"
                    r"(?:make|pytest|python3? -m (?:pytest|unittest)|\S*tests?/\S+|\S*/test_\w+\S*|ctest|cargo (?:test|build)|"
                    r"npm (?:test|run)|cc|gcc|clang|nvim --headless)(?:\s|$)")
FIX = re.compile(r"\b(?:bugs?|broken|doesn'?t work|does not work|not working|didn'?t work|crash\w*|regress\w*|"
                 r"fail(?:s|ed|ing)|errors?|wrong|can'?t|cannot|still|isn'?t|not (?:showing|working|catching)|horrible)\b", re.I)
SEARCH = re.compile(r"(?:grep|rg|ag|fd|find|git\s+grep)(?:\s|$)")


def segments(cmd):
    """The commands in a command line, each without the wrappers that only set how it runs."""
    for seg in re.split(r"&&|\|\||;|\||\n", cmd):
        seg = re.sub(r"^\s*\(?\s*(?:env\s+|\w+=\S*\s+|sudo\s+|time\s+|timeout\s+(?:-\S+\s+(?:\S+\s+)?)*\S+\s+)*", "", seg)
        seg = re.sub(r"[\s)]+$", "", seg)  # the end of a subshell
        if seg:
            yield seg


def runs(pattern, cmd):
    return any(pattern.match(seg) for seg in segments(cmd))
CORRECTION = re.compile(r"\b(?:bugs?|broken|wrong|doesn'?t work|does not work|not working|didn'?t work|revert|undo|"
                        r"regress\w*|crash\w*|fail(?:s|ed|ing)?|errors?|strange|weird|horrible|not what i|why did you)\b|^no\b", re.I)

# ---- read the transcripts into one time-ordered stream ----
stream = []                  # (ts, seq, kind, data)
notes = {}                   # tool_use id -> (comment, tool name, latest text this turn)
prompts = defaultdict(list)  # session -> [(ts, title)]
seq, pushed = 0, set()
def push(ts, kind, key=None, **d):
    """key: what makes the record unique; a resumed session copies earlier records into its new file."""
    global seq
    if key is not None:
        if key in pushed:
            return
        pushed.add(key)
    seq += 1
    stream.append((ts, seq, kind, d))

usage = {}                   # response id -> (ts, ctx, session, output, new input, cached input): the largest seen
titles = {}                  # session -> its AI title
actors, actor_ix = [], {}    # who works: a main session or one subagent, by transcript file
def actor_of(path):
    if path not in actor_ix:
        sub = "/subagents/" in path
        a = {"kind": "subagent" if sub else "session", "id": os.path.basename(path)[:-6].removeprefix("agent-")[:8]}
        if sub:
            try:
                meta = json.load(open(path[:-6] + ".meta.json"))
                a.update(name=(meta.get("agentType") or "subagent").rsplit(":", 1)[-1], task=meta.get("description") or "")
            except (OSError, ValueError):
                a.update(name="subagent", task="")
        actor_ix[path] = len(actors)
        actors.append(a)
    return actor_ix[path]

for f in sorted(glob.glob(PROJ + "/**/*.jsonl", recursive=True)):
    sub = "/subagents/" in f
    session = f.split("/subagents/")[0].rsplit("/", 1)[1] if sub else os.path.basename(f)[:-6]
    ctx = f  # whose context: the main session, or one subagent
    pending, said, calls = [], "", {}
    for line in open(f, errors="replace"):
        if '"ai-title"' in line:
            try:
                r = json.loads(line)
                titles[r.get("sessionId") or session] = r.get("aiTitle", "")
            except ValueError:
                pass
            continue
        if '"type":"assistant"' not in line and '"type":"user"' not in line and "compact_boundary" not in line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        ts, typ, cwd = r.get("timestamp", ""), r.get("type"), r.get("cwd") or root
        if typ == "system" and r.get("subtype") == "compact_boundary" or r.get("isCompactSummary"):
            push(ts, "compact", ctx=ctx)
            pending, said = [], ""
            continue
        cont = (r.get("message") or {}).get("content")
        u, mid = (r.get("message") or {}).get("usage"), (r.get("message") or {}).get("id")
        if typ == "assistant" and isinstance(u, dict) and mid:
            # a response's parts each repeat its usage, output growing: keep the largest
            out = u.get("output_tokens") or 0
            if mid not in usage or out > usage[mid][3]:
                usage[mid] = (usage[mid][0] if mid in usage else ts, ctx, session, out,
                              (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0),
                              u.get("cache_read_input_tokens") or 0)
        if typ == "assistant" and isinstance(cont, list):
            for x in cont:
                if x.get("type") == "text" and x.get("text", "").strip():
                    pending.append(x["text"].strip())
                    said = x["text"].strip()
                elif x.get("type") == "tool_use":
                    notes[x["id"]] = ("\n\n".join(pending), x.get("name", ""), said)
                    calls[x["id"]] = (x.get("name", ""), x.get("input") or {}, cwd)
            continue
        if typ != "user":
            continue
        results = [x for x in cont if isinstance(x, dict) and x.get("type") == "tool_result"] if isinstance(cont, list) else []
        if not results:
            c = cont
            if isinstance(c, list):
                c = "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
            if isinstance(c, str) and c.startswith("[Request interrupted"):
                push(ts, "interrupt", key=("i", ts), session=session)
            elif prompt_of(r, c):
                pending, said = [], ""
                if not sub:
                    prompts[session].append((ts, first_line(prompt_of(r, c))))
            continue
        pending = []
        res, tid = r.get("toolUseResult"), results[0].get("tool_use_id")
        err = bool(results[0].get("is_error"))
        name, inp, ccwd = calls.get(tid, ("", {}, cwd))
        if name == "Bash" and isinstance(inp.get("command"), str):
            cmd = inp["command"]
            if runs(VERIFY, cmd):
                push(ts, "verify", key=("v", tid), session=session, ok=not err, cmd=cmd[:120])
            if runs(SEARCH, cmd):
                push(ts, "search", key=("s", tid), ctx=ctx)
            if not err:
                for where, rel, a, b in cmd_reads(cmd, ccwd):
                    push(ts, "read", key=("r", tid, where, a, b), ctx=ctx, rel=rel, shown=where, a=a, b=b)
        elif name in ("Grep", "Glob"):
            push(ts, "search", key=("s", tid), ctx=ctx)
        if not isinstance(res, dict):
            continue
        if res.get("type") == "text" and isinstance(res.get("file"), dict):
            fl = res["file"]
            a = int(fl.get("startLine") or 1)
            push(ts, "read", key=("r", tid), ctx=ctx, rel=rel_of(fl.get("filePath", "")), shown=shown(fl.get("filePath", "")),
                 a=a, b=a + int(fl.get("numLines") or 10 ** 9) - 1)
        if res.get("filePath") and (res.get("structuredPatch") is not None or res.get("type") == "create"):
            rel = rel_of(res["filePath"])
            if rel:
                push(ts, "change", key=("c", tid, rel), session=session, ctx=ctx, rel=rel, res=res, tid=tid)
        bd = res.get("bashEditDiff")
        for bf in bd.get("files", []) if isinstance(bd, dict) else []:
            rel = rel_of(bf.get("filePath", ""))
            if rel:
                push(ts, "change", key=("c", tid, rel), session=session, ctx=ctx, rel=rel, res={"bash": bf.get("hunks", [])}, tid=tid)
for mid, (ts, ctx, session, out, new, cached) in usage.items():
    push(ts, "usage", ctx=ctx, session=session, tok=(out, new, cached))
for s in prompts:
    prompts[s].sort()
stream.sort(key=lambda e: (e[0], e[1]))

# ---- turns ----
turns, turn_ix = [], {}
def turn_of(session, ts, make=True):
    ps, best = prompts.get(session, []), None
    for i, (t, _) in enumerate(ps):
        if t <= ts:
            best = i
    if best is None:
        return None
    key = (ps[best][0], ps[best][1])
    if key not in turn_ix:
        if not make:
            return None
        nxt = ps[best + 1][1] if best + 1 < len(ps) else None
        turn_ix[key] = len(turns)
        turns.append({"title": ps[best][1], "ts": ps[best][0], "session": session,
                      "corr": nxt if nxt and CORRECTION.search(nxt) else None, "intr": False,
                      "fix": bool(FIX.search(ps[best][1]))})
    return turn_ix[key]

comments, comment_ix = [], {}
def comment_of(tid):
    """The comment index (0+), or -1-index for the turn's earlier text when the call had none of its own."""
    text, tool, said = notes.get(tid, ("", "", ""))
    own = bool(text.strip())
    text = (text if own else said).strip()
    if len(text) > 900:
        text = text[:900].rsplit(" ", 1)[0] + " …"
    if text not in comment_ix:
        comment_ix[text] = len(comments)
        comments.append(text)
    return comment_ix[text] if own or not text else -1 - comment_ix[text], tool

# ---- replay ----
known = defaultdict(list)  # full texts of a file by time, for lining up commands' patches
for ts, _, kind, d in stream:
    if kind != "change" or "bash" in d["res"]:
        continue
    res = d["res"]
    if res.get("type") == "create":
        known[d["rel"]].append((ts, res.get("content", "")))
    elif res.get("originalFile") is not None:
        known[d["rel"]].append((ts, res["originalFile"]))
        known[d["rel"]].append((ts + "~", apply(res["originalFile"], res.get("structuredPatch") or [])))
for rel in FILES:
    known[rel].append(("~", open(f"{root}/{rel}").read()))
    known[rel].sort(key=lambda k: k[0])

def later_text(rel, ts, hunks):
    for kts, text in known.get(rel, []):
        if kts > ts:
            starts = relocate(text, hunks)
            if starts:
                return text, [dict(h, newStart=n, oldStart=n) for h, n in zip(hunks, starts)]
    return None, hunks

tracked = {}               # rel -> [text, origins]: origins[i] = change id that wrote line i, -1 unknown
unplaced = {}              # rel -> change id of the latest change whose lines couldn't be placed
def resync(rel, text):
    """Tracked text -> text: equal lines keep their origins, others came from changes we couldn't place."""
    t = tracked.get(rel)
    fill = unplaced.pop(rel, -1)
    if t is None:
        tracked[rel] = [text, [-1] * len(text.split("\n"))]
        return
    if t[0] == text:
        return
    a, b = t[0].split("\n"), text.split("\n")
    o = [fill] * len(b)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            o[j1:j2] = t[1][i1:i2]
    tracked[rel] = [text, o]

changes = []                                   # [turn, comment, tool, verify, session, ts]
node_rw = defaultdict(lambda: defaultdict(int))  # key -> (writing change, removing change) -> lines
node_tr = defaultdict(list)                    # key -> [[change, lines, cx]] after each change
gathered = defaultdict(list)                   # ctx -> [(ts, "r"/"s", path)] in its current turn
paths, path_index = [], {}
def path_ix(p):
    if p not in path_index:
        path_index[p] = len(paths)
        paths.append(p)
    return path_index[p]
node_ch = defaultdict(list)                    # "rel" or "rel::key" -> [[change id, +, -, blind]]
spent = defaultdict(list)                      # ctx -> [(ts, (output, new input, cached input))] since its last change
last_change = {}                               # ctx -> (turn, ts) of its last change
def epoch(ts):
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
IDLE_CAP = 300  # seconds: a longer gap between responses (a background wait, a long command) counts as this much
def active(stamps):
    """Seconds from the first to the last of these timestamps, each gap capped at IDLE_CAP."""
    xs = sorted(epoch(x) for x in stamps)
    return round(sum(min(b - a, IDLE_CAP) for a, b in zip(xs, xs[1:])))
seen = defaultdict(lambda: defaultdict(set))   # ctx -> rel -> def keys in its context
stats = defaultdict(int)
for ts, _, kind, d in stream:
    if kind == "compact":
        seen[d["ctx"]] = defaultdict(set)
        continue
    if kind == "interrupt":
        continue  # resolved after the replay, when the turn exists
    if kind == "search":
        gathered[d["ctx"]].append((ts, "s", None))
        continue
    if kind == "usage":
        spent[d["ctx"]].append((ts, d["tok"]))
        continue
    if kind == "read":
        gathered[d["ctx"]].append((ts, "r", d["shown"]))
        rel = d["rel"]
        if not rel:
            continue
        text = tracked[rel][0] if rel in tracked and rel not in unplaced else git_text(rel, ts)
        if text is None:
            continue
        for k, _, a, b in defs(rel, text):
            if a <= d["b"] and b >= d["a"]:
                seen[d["ctx"]][rel].add(k)
        continue
    if kind != "change":
        continue
    rel, res, tid = d["rel"], d["res"], d["tid"]
    if "bash" in res and rel in tracked and rel not in unplaced and has_new(tracked[rel][0], res["bash"]):
        stats["dups"] += 1  # an edit made while a command ran: Claude Code credits it to the command too
        continue
    t = turn_of(d["session"], ts)
    if t is None:
        continue
    c, tool = comment_of(tid)
    cid = len(changes)
    tool = "Bash" if "bash" in res else tool or ("Write" if res.get("type") == "create" else "Edit")
    stats["edit without text before"] += "bash" not in res and res.get("type") != "create" and res.get("originalFile") is None
    # Context cost: everything this agent read and searched in this turn before this change. A def's cost in a
    # turn is that of its first change there: the context it built up to touch it.
    items = gathered[d["ctx"]] = [x for x in gathered[d["ctx"]] if turn_of(d["session"], x[0], make=False) == t]
    reads = Counter(x[2] for x in items if x[1] == "r")
    cost = [sum(reads.values()), sum(1 for x in items if x[1] == "s"), [[path_ix(p), n] for p, n in reads.most_common(8)]]
    # Cost: the tokens of this agent's responses in this turn since its last change, and the time since then
    # (or since its first response toward it). One call's other files get none.
    mine = [x for x in spent[d["ctx"]] if turn_of(d["session"], x[0], make=False) == t]
    spent[d["ctx"]] = []
    tok = [sum(x[1][i] for x in mine) for i in range(3)]
    prev = last_change.get(d["ctx"])
    since = prev[1] if prev and prev[0] == t else (mine[0][0] if mine else ts)
    secs = active([since, ts] + [x[0] for x in mine if since <= x[0] <= ts])
    last_change[d["ctx"]] = (t, ts)
    changes.append([t, c, tool, 0, d["session"], ts, cost, actor_of(d["ctx"]), tok, secs])
    touched = defaultdict(lambda: [0, 0])
    old = new = no = None
    hunks, removed, later = None, [], False  # later: new is a later text the patch was lined up with
    if "bash" in res or res.get("type") != "create" and res.get("originalFile") is None:
        hunks = res["bash"] if "bash" in res else res.get("structuredPatch") or []  # no text before it: as a command
        if rel in tracked and rel not in unplaced:
            r2 = apply(tracked[rel][0], hunks, check=True, origins=tracked[rel][1], cid=cid, removed=removed)
            if r2:
                old, (new, no) = tracked[rel][0], r2
        if new is None:
            base = git_text(rel, ts)
            if base is not None and apply(base, hunks, check=True) is not None:
                resync(rel, base)
                old = base
                removed.clear()
                new, no = apply(base, hunks, origins=tracked[rel][1], cid=cid, removed=removed)
                stats["from git"] += 1
        if new is None:
            (new, hunks), old = later_text(rel, ts, hunks), None
            stats["lined up later" if new is not None else "file level only"] += 1
            unplaced[rel] = cid
            later = True
    elif res.get("type") == "create":
        new = res.get("content", "")
        no = [cid] * len(new.split("\n"))
        unplaced.pop(rel, None)
        for k, _, a, b in defs(rel, new):
            touched[k][0] += b - a + 1
    else:
        old, hunks = res.get("originalFile") or "", res.get("structuredPatch") or []
        resync(rel, old)
        new, no = apply(old, hunks, origins=tracked[rel][1], cid=cid, removed=removed)
    fa = fd = 0
    od = defs(rel, old) if old else (defs(rel, new) if new is not None else [])
    if hunks:
        nd = defs(rel, new) if new is not None else []
        for h in hunks:
            o, n = h["oldStart"], h["newStart"]
            for x in h["lines"]:
                if x[:1] == "+":
                    fa += 1
                    for k, _, a, b in nd:
                        if a <= n <= b:
                            touched[k][0] += 1
                    n += 1
                elif x[:1] == "-":
                    fd += 1
                    for k, _, a, b in od:
                        if a <= o <= b:
                            touched[k][1] += 1
                    o += 1
                else:
                    o += 1
                    n += 1
    elif res.get("type") == "create":
        fa = nlines(new)
    if no is not None:
        tracked[rel] = [new, no]
    # Rework: lines this change removed, by the change that wrote them (any def they were in, and the file).
    if old and removed:
        for line, origin in removed:
            if origin < 0:
                continue
            for k in [rel] + [rel + "::" + k for k, _, a, b in od if a <= line <= b]:
                node_rw[k][(origin, cid)] += 1
    # Trend: each def this change touched, its size and complexity right after it; the file's length.
    # (Not from a later text a command's patch was lined up with: that isn't the file right after it.)
    if new is not None and not later:
        nl = new.split("\n")
        for k, _, a, b in (defs(rel, new) if hunks else []):
            if k in touched:
                node_tr[rel + "::" + k].append([cid, b - a + 1, complexity(rel, nl[a - 1:b])[0]])
        node_tr[rel].append([cid, nlines(new), 0])
    # Blind: an existing def changed with none of it read (or written) in this context since its last compaction.
    ctx_seen, existed = seen[d["ctx"]][rel], {k for k, _, _, _ in od} if old else set()
    blind = {k for k in touched if k in existed and k not in ctx_seen}
    ctx_seen.update(touched)
    for key, (a, dl), b in [(rel, (fa, fd), bool(blind))] + [(rel + "::" + k, v, k in blind) for k, v in touched.items()]:
        node_ch[key].append([cid, a, dl, int(b)])
    stats["blind"] += bool(blind)

# Verification: a test or build run later in the same turn (its result: the last one's).
verifies = [(ts, d) for ts, _, kind, d in stream if kind == "verify"]
for ch in changes:
    t, session, ts = ch[0], ch[4], ch[5]
    after = [v for vts, v in verifies if v["session"] == session and vts > ts and turn_of(session, vts, make=False) == t]
    ch[3] = 0 if not after else 1 if after[-1]["ok"] else 2
for rel in FILES:
    resync(rel, open(f"{root}/{rel}").read())
for ts, _, kind, d in stream:
    if kind == "usage":
        t = turn_of(d["session"], ts, make=False)
        if t is not None:
            turns[t]["tok"] = [a + b for a, b in zip(turns[t].get("tok", [0, 0, 0]), d["tok"])]
            # Time: from the prompt to the last response in the turn (the wait for the next prompt is not in it)
            turns[t].setdefault("stamps", []).append(ts)
    if kind == "interrupt":
        t = turn_of(d["session"], ts, make=False)
        if t is not None:
            turns[t]["intr"] = True
for t in turns:
    t["actor"] = actor_of(os.path.join(PROJ, t["session"] + ".jsonl"))
    t["secs"] = active([t["ts"]] + t.pop("stamps", []))
for path, i in actor_ix.items():
    if actors[i]["kind"] == "session":
        actors[i]["name"] = titles.get(os.path.basename(path)[:-6], "") or "session " + actors[i]["id"]

# ---- the tree as it is now ----
tree = {"name": "HarnessTools", "kind": "dir", "kids": []}
def dir_of(parts):
    n = tree
    for p in parts:
        k = next((k for k in n["kids"] if k["name"] == p and k["kind"] == "dir"), None)
        if not k:
            k = {"name": p, "kind": "dir", "kids": []}
            n["kids"].append(k)
        n = k
    return n

def rle(xs):
    out = []
    for x in xs:
        if out and out[-1][1] == x:
            out[-1][0] += 1
        else:
            out.append([1, x])
    return out

for f in FILES:
    src = open(f"{root}/{f}").read()
    lines = src.split("\n")
    fn = {"name": f.split("/")[-1], "kind": "file", "lines": nlines(src), "kids": [],
          "ch": node_ch.get(f, []), "lang": "c" if f.endswith(".c") else "python", "src": src,
          "rw": [[o, c, n] for (o, c), n in node_rw.get(f, {}).items()], "tr": node_tr.get(f, []),
          "blame": rle(tracked[f][1][:nlines(src)]) if f in tracked else [[nlines(src), -1]]}
    by_key, cx_total = {}, 0
    for key, kind, a, b in defs(f, src):
        cx, depth = complexity(f, lines[a - 1:b])
        d = {"name": key.rsplit(".", 1)[-1], "kind": kind, "lines": b - a + 1, "a": a, "b": b, "kids": [],
             "ch": node_ch.get(f + "::" + key, []), "cx": cx, "depth": depth,
             "rw": [[o, c, n] for (o, c), n in node_rw.get(f + "::" + key, {}).items()],
             "tr": node_tr.get(f + "::" + key, [])}
        parent = by_key.get(key.rsplit(".", 1)[0]) if "." in key else None
        (parent["kids"] if parent else fn["kids"]).append(d)
        if not parent:
            cx_total += cx
        by_key[key] = d
    fn["cx"] = cx_total
    dir_of(f.split("/")[:-1])["kids"].append(fn)

json.dump({"tree": tree, "turns": [{k: v for k, v in t.items() if k != "session"} for t in turns],
           "comments": comments, "paths": paths, "actors": actors,
           # change: turn, comment, tool, verify, reads, searches, files read, time, actor, tokens (out, new, cached), seconds
           "changes": [c[:4] + c[6][:2] + [c[6][2]] + [round(epoch(c[5])), c[7], *c[8], c[9]] for c in changes]},
          sys.stdout, separators=(",", ":"))
blamed = sum(1 for f in FILES if f in tracked for o in tracked[f][1] if o >= 0)
total = sum(len(tracked[f][1]) for f in FILES if f in tracked)
print(f"changes {len(changes)} turns {len(turns)} {dict(stats)} blamed lines {blamed}/{total} "
      f"verify {[sum(1 for c in changes if c[3] == v) for v in (0, 1, 2)]} "
      f"corrections {sum(1 for t in turns if t['corr'])} interrupted {sum(1 for t in turns if t['intr'])}", file=sys.stderr)
