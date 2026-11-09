#!/usr/bin/env python3
"""Optional LLM labeling pass over a timeline.json (contract: SCHEMA.md).

    python label.py web/timeline.json [--server http://127.0.0.1:8080] [--cache demo/labels.json]
                    [--model-tag TAG] [--report] [--think] [--max-tokens N]

--model-tag TAG caches in demo/labels.TAG.json (one cache per model); --report prints per-blob
latency, prompt/generated/reasoning tokens (API usage), retries, rejected titles and fallbacks.
--think lets the model reason first (start the server with `--reasoning on --reasoning-format
deepseek` so the reasoning comes back apart from `content`); give it --max-tokens 1024 or more.

Asks a local llama-server (start it with ./serve-llm.sh) for emoji/title/summary/risk per
distinct blob content (sorted func hashes), caches by that key, and writes the result into
every blob with the same content as `label: {...}`. Heuristic emoji/name remain the fallback.
Schema v5 blobs (areas.py) keep the key of the blob as the analyzer drew it: functions folded in
from a small blob (`merged`) do not count, a cut parent's key also covers its pieces' functions,
and each `merged` entry gets the label of its own functions. The areas' captions and faces are
recomputed from the labels (areas.caption_table()).

Blobs are labelled one at a time, module by module, and each prompt lists the titles already
chosen (same module first). A title is never clipped: one over 4 words, an unfinished phrase or
an exact duplicate is rejected and retried with a stricter follow-up (2 retries; a duplicate gets
one), then the blob's structural name (its heuristic name) is used, never a fragment.

--per-step labels the whole history instead: step by step, the blob versions new or changed at that
step (cache hits are skipped), with that step's parts and titles as context. Each step is a unit:
its labels are flushed to the cache when it ends and one JSONL record goes to the run log
(--run-log, default demo/label-runs/<timeline name>.jsonl). A rerun skips steps logged ok or fully
cached; --steps A-B restricts, --retry-failed redoes only failed steps. Requests are retried with
backoff (--attempts, --backoff); a step that still fails is logged failed and the run goes on.
Between steps it pauses while nvidia-smi reads >= --hot °C until below --cool. The timeline is not
written: apply the cache afterwards with `label.py T --cache C --server http://127.0.0.1:1`.
--run-report LOG summarises a run log. Big blobs are cut to --prompt-chars, and the system prompt
lists every part of the step, so give the server one big slot (`-np 1`).

--relabel-at X (default 0.2; both modes): a blob keeps its label until the lines changed since that label
was made reach X of the blob's lines at that label (an added or removed function counts its whole length);
the new key then gets a copy of the label marked `reused_from: <labelled key>`, without a model call (Keeper).
A key already in the cache is always a hit. The state per blob is replayed from step 0 against the cache, so
a resumed --per-step run decides as an uninterrupted one. 0 asks for every new key, as before.
--count prints how many label requests the timeline needs from an empty cache, at X and at 0, and exits.

The cache is written by merging (save_cache: lock, read the file again, merge the entries this run changed, rename),
so a run next to another writer (another label.py, liveserve.py's labelling worker) loses none of its labels and
puts none of theirs back to what it read.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter

import areas
import timeline  # expand() / compact(): timeline.json is schema v3 / v4 / v5 (deltas)

TIMEOUT = 60
MAX_WORDS = 4
MAX_RETRIES = 2
MAX_TAKEN = 40
HEAD_LINES = 15

INSTRUCTIONS = """You label parts of a software application for a non-programmer who watches an AI agent edit it.
For the blob you are given, answer with JSON only, no prose, no code fences:
{"emoji": "<exactly one emoji>", "title": "<2 to 4 words, never more, a complete noun phrase naming what this part of the app does for the user>", "summary": "<at most 12 words>", "risk": "<low|medium|high>: <one short reason>"}
Risk means how bad it is if this code is wrong (auth, payments, data loss = high; display only = low).
Use the user's point of view (login, checkout, profile), not code names.
Every part needs its own title: you are told which titles other parts already have, pick a different one.
For a template or other non-code file you get its first lines instead of functions: say what that page or file shows or configures."""


def file_head(root, blob):
    """First HEAD_LINES lines of a file/text blob's file ([] when unreadable or binary)."""
    if blob["kind"] not in ("file", "text") or not blob.get("path"):
        return []
    try:
        with open(os.path.join(root, blob["path"]), encoding="utf-8") as f:
            return [ln.rstrip()[:160] for _, ln in zip(range(HEAD_LINES), f)]
    except (OSError, UnicodeDecodeError):
        return []


def own_funcs(blob):
    """A blob's own functions: not those folded in from a smaller blob (`merged`, schema v5)."""
    folded = {q for m in blob.get("merged", ()) for q in m["funcs"]}
    return [f for f in blob.get("funcs", []) if not folded or f.get("qual") not in folded]


def own_hashes(blob):
    return [f["hash"] for f in own_funcs(blob)]


def content_key(blob):
    if blob["kind"] in ("file", "text"):  # "_head" is set by main(); key = path + head hash
        head = blob.get("_head")
        if not head:
            return ""
        return "file:" + blob["path"] + ":" + hashlib.sha1("\n".join(head).encode()).hexdigest()[:10]
    return ",".join(sorted(own_hashes(blob)))


def step_keys(step):
    """{blob id: cache key} of a step's blobs: content_key(), except that a cut parent's key also covers its
    pieces' own functions, i.e. it is the key of the blob before the cut (schema v5). Set as step["_keys"]."""
    pieces = {}
    for b in step["blobs"]:
        if "cut" in b:
            pieces.setdefault(b["cut"], []).extend(own_hashes(b))
    return {b["id"]: ",".join(sorted(own_hashes(b) + pieces[b["id"]])) if b["id"] in pieces else content_key(b)
            for b in step["blobs"]}


def key_of(step, blob):
    keys = step.get("_keys")
    return keys[blob["id"]] if keys is not None else content_key(blob)


# --- keep a label until the blob has really changed (--relabel-at) ---

def units(step):
    """The blob identities the reuse rule follows in a step, as the analyzer drew them (schema v5), as
    [(id, key, functions or None, blob to ask or None)]: a blob under its id (a cut parent with its pieces'
    functions, key as step_keys()), each `merged` entry under its own id with its own functions (never asked,
    as before: it can only keep a label), and each piece under its piece id. File and text blobs have no
    functions: the rule does not apply to them."""
    pieces = {}
    for b in step["blobs"]:
        if "cut" in b:
            pieces.setdefault(b["cut"], []).extend(own_funcs(b))
    out = []
    for b in step["blobs"]:
        if b["kind"] in ("file", "text"):
            out.append((b["id"], key_of(step, b), None, b))
            continue
        out.append((b["id"], key_of(step, b), own_funcs(b) + pieces.get(b["id"], []), b))
        for m in b.get("merged", ()):
            mine = set(m["funcs"])
            funcs = [f for f in b["funcs"] if f.get("qual") in mine]
            out.append((m["id"], ",".join(sorted(f["hash"] for f in funcs)), funcs, None))
    return out


def plan_funcs(funcs):
    """A unit's functions as the Keeper reads them: ((qual, hash, lines, delta's added + removed), ...)."""
    out = []
    for f in funcs:
        d = f.get("delta") or {}
        out.append((f["qual"], f["hash"], f["lines"], d.get("added", 0) + d.get("removed", 0)))
    return tuple(out)


def changed_lines(prev, funcs, adjacent):
    """Lines changed from `prev` ({qual: (hash, lines)}) to `funcs` (plan_funcs()): a function whose hash changed
    counts its delta's added + removed lines (after a gap, or with no delta, its line-count difference, at least 1);
    an added or removed function counts its whole length."""
    n, cur = 0, set()
    for qual, h, lines, moved in funcs:
        cur.add(qual)
        old = prev.get(qual)
        if old is None:
            n += lines
        elif old[0] != h:
            n += moved if adjacent and moved else max(1, abs(lines - old[1]))
    return n + sum(lines for q, (_, lines) in prev.items() if q not in cur)


class Keeper:
    """The --relabel-at rule: a unit (units()) keeps its label until the lines changed since that label was made
    reach `at` x the unit's lines at that label (changed_lines(), summed over the versions since). A kept label
    goes into the cache under the new key as a copy marked `reused_from: <labelled key>`, without a model call.
    A key already in the cache is always a hit: a model label anchors the unit there; a reused one counts as the
    reuse it records (from another anchor: re-anchored there, at today's lines). Feed it every step from step 0:
    the state is rebuilt from the cache, so a resumed run decides as an uninterrupted one. at=0: no reuse."""

    def __init__(self, at):
        self.at = at
        self.state = {}  # unit id -> [labelled key or None, its lines, lines changed since, key, {qual: (hash, lines)}, step]
        self.asked = []  # (unit id, key, lines) asked at this step, for settle()
        self.reused = []

    def step(self, i, step, cache, live=True, us=None):
        """Step position `i`: hits and reuses update the state now (reuses are written into `cache`). -> {key: blob}
        to ask; call settle() once they are asked. Not `live` (outside the steps being labelled): nothing is
        written or asked, an uncached key leaves its unit unlabelled. `us`: the step's units given instead of the
        step, their functions as plan_funcs() (only those of a unit whose key changed since the step before are
        read); a unit's blob is then whatever the caller put there (returned as the blob to ask)."""
        ask, self.reused = {}, []
        for uid, key, funcs, blob in units(step) if us is None else us:
            st = self.state.get(uid)
            if not key:
                continue
            if st and st[3] == key and st[5] == i - 1:
                st[5] = i
                continue
            if us is None and funcs is not None:
                funcs = plan_funcs(funcs)
            adjacent = st is not None and st[5] == i - 1
            chg = changed_lines(st[4], funcs, adjacent) if st and funcs is not None else 0
            anchor, at_lines, since = (st[0], st[1], st[2] + chg) if st else (None, 0, 0)
            lines = sum(f[2] for f in funcs or ())
            entry = cache.get(key)
            if key in ask:  # another unit asks for it at this step
                anchor = None
                self.asked.append((uid, key, lines))
            elif entry is not None:
                src = entry.get("reused_from")
                if src is None:
                    anchor, at_lines, since = key, lines, 0
                elif src != anchor:
                    anchor, at_lines, since = src, lines, 0
            elif live and funcs is not None and anchor in cache and since / max(at_lines, 1) < self.at:
                cache[key] = dict(cache[anchor], reused_from=cache[anchor].get("reused_from", anchor))
                self.reused.append(key)
            else:
                if live and blob is not None:
                    ask.setdefault(key, blob)
                    self.asked.append((uid, key, lines))
                anchor = None
            self.state[uid] = [anchor, at_lines, since, key, {q: (h, n) for q, h, n, _ in funcs or ()}, i]
        return ask

    def settle(self, cache):
        """After asking: a unit whose key got a model label is anchored there; a failed one stays unlabelled."""
        for uid, key, lines in self.asked:
            if key in cache and "reused_from" not in cache[key]:
                self.state[uid][:3] = [key, lines, 0]
        self.asked = []


def plan_labels(steps, cache, at):
    """The rule replayed over the whole timeline, step by step, as if every ask succeeded (`cache` is not
    changed). -> ({key to ask: its first blob}, {reused key: labelled key})."""
    work, keeper, asks = dict(cache), Keeper(at), {}
    for i, step in enumerate(steps):
        for key, b in keeper.step(i, step, work).items():
            asks.setdefault(key, b)
            work[key] = {}  # stands in for the label it will get
        keeper.settle(work)
    return asks, {k: v["reused_from"] for k, v in work.items() if k not in cache and "reused_from" in v}


def count_report(steps, at):
    """--count: label requests from an empty cache, step by step, under the rule and at --relabel-at 0."""
    out, n = [f"label.py --count: from an empty cache, every ask assumed to succeed, {len(steps)} steps"], {}
    for t in dict.fromkeys((at, 0)):
        asks, reused = plan_labels(steps, {}, t)
        kinds = Counter("piece" if "cut" in b else "file" if b["kind"] in ("file", "text") else "blob"
                        for b in asks.values())
        n[t] = len(asks)
        out.append(f"--relabel-at {t:g}: {len(asks)} label requests ({kinds['blob']} blobs, {kinds['piece']} pieces, "
                   f"{kinds['file']} files), {len(reused)} keys reuse a label")
    if at:
        out.append(f"saved {n[0] - n[at]} of {n[0]} requests ({(n[0] - n[at]) / max(n[0], 1):.1%})")
    return "\n".join(out)


def system_prompt(last_step):
    names = "\n".join(f"- {b['name']} ({b['module']})" for b in last_step["blobs"])
    return f"{INSTRUCTIONS}\n\nThe whole application has these parts:\n{names}"


def blob_prompt(blob, budget=None):
    """The user prompt for `blob`. With `budget` (characters) a longer one drops the functions'
    snippets, then lists only the functions that fit, so it stays inside the server's context slot."""
    lines = [f"Blob: {blob['name']}", f"Kind: {blob['kind']}", f"Module: {blob['module']}"]
    edges = sorted({e["kind"] for e in blob.get("_edges", [])})
    lines.append("Edge kinds: " + (", ".join(edges) or "none"))
    if blob.get("_head"):
        lines.append(f"File {blob['path']} ({blob['size']} lines), first {len(blob['_head'])} lines:")
        lines += ["  | " + ln for ln in blob["_head"]]
    funcs = []
    for f in blob.get("funcs", []):
        block = [f"Function {f['name']} ({f['lines']} lines)"]
        if f.get("data"):
            block.append("  data: " + ", ".join(f["data"]))
        block += ["  | " + s for s in f.get("snippet", [])]
        funcs.append(block)
    text = "\n".join(lines + [ln for block in funcs for ln in block])
    if budget is None or len(text) <= budget:
        return text
    size = sum(len(ln) + 1 for ln in lines)
    for n, block in enumerate(funcs):
        if size + len(block[0]) + 1 > budget - 40:
            lines.append(f"... and {len(funcs) - n} more functions")
            break
        lines.append(block[0])
        size += len(block[0]) + 1
    return "\n".join(lines)


# Words a finished title cannot end with, and words that open a clause the title never closes.
DANGLING_END = set("""a an the this that these those each every all any some its their your his her
of to for with in on at by from into onto about as and or but nor if whether when while which who
is are be been was were has have do does can will may single specific given new existing current
other another same own using after before via per without within through between including
then so than like until upon across during vs""".split())
CLAUSE_OPENERS = {"if", "whether", "when", "while", "which", "who", "that"}


def title_problem(title):
    """Why a title cannot be shown as is ("empty", "long", "fragment" like "View a single" or
    "Checks if user"); None when it is a finished phrase of at most MAX_WORDS words."""
    words = title.split()
    bare = [w.strip(".,;:!?\"'()").lower() for w in words]
    if not words:
        return "empty"
    if len(words) > MAX_WORDS:
        return "long"
    if bare[-1] in DANGLING_END or CLAUSE_OPENERS & set(bare):
        return "fragment"
    if not any(c.isalnum() for c in words[-1]) or bare[-1].endswith(("'s", "\u2019s")):
        return "fragment"  # "Login form —", "User login ...", "Displays user's"
    return None


def parse_label(text):
    text = re.sub(r"<think>.*?(</think>|$)", "", text, flags=re.S)
    start = text.find("{")  # skip prose / code fences around the object
    if start < 0:
        raise ValueError("no JSON object in reply")
    d, _ = json.JSONDecoder().raw_decode(text, start)
    emoji = str(d["emoji"]).strip()
    title = " ".join(str(d["title"]).split())  # never clipped: title_problem judges it whole
    summary = " ".join(str(d["summary"]).split()[:12])
    risk = str(d["risk"]).strip()
    level = re.match(r"(low|medium|high)", risk, flags=re.I)
    if not (emoji and level):
        raise ValueError("incomplete label")
    return {"emoji": emoji, "title": title, "summary": summary,
            "risk": level.group(1).lower() + risk[level.end():]}


def siblings(blob_id, module, titles, module_of):
    """Titles of the other blobs (`titles`: blob id -> title), those in `module` first."""
    others = [i for i in titles if i != blob_id]
    return [titles[i] for i in others if module_of[i] == module] + \
           [titles[i] for i in others if module_of[i] != module]


def apply_labels(steps, cache):
    """Set each blob's `label` from the cache; a blob the cache does not back loses any old label. Each
    `merged` entry gets the label of its own functions, and a step's areas table is recomputed from the labels.
    Blobs are shared between steps, so run this before dropping the `_head` keys content_key reads; a shared
    blob whose label differs between two steps (a cut parent whose piece changed) is copied for the later one.
    A reused label (`reused_from`) is shown without that mark."""
    cache = {k: {f: x for f, x in v.items() if f != "reused_from"} if "reused_from" in v else v
             for k, v in cache.items()}
    given = {}
    for step in steps:
        for i, b in enumerate(step["blobs"]):
            label = cache.get(key_of(step, b))  # file blobs are keyed by their _head
            if id(b) in given and given[id(b)][1] is not label:
                b = step["blobs"][i] = dict(b)
            given[id(b)] = (b, label)       # the blob itself too, so its id is not reused while this runs
            if label:
                b["label"] = label
            else:
                b.pop("label", None)
            for m in b.get("merged", ()):
                mine = set(m["funcs"])
                lab = cache.get(",".join(sorted(f["hash"] for f in b["funcs"] if f.get("qual") in mine)))
                if lab:
                    m["label"] = lab
                else:
                    m.pop("label", None)
        if "areas" in step:
            step["areas"] = areas.caption_table(step["blobs"], [a["id"] for a in step["areas"]])


def post(server, path, body, timeout=TIMEOUT):
    req = urllib.request.Request(server + path, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def followup(problem, title, taken):
    if problem == "duplicate":
        return (f'Your title "{title}" is already taken. Titles already taken: {"; ".join(taken)}. '
                "Reply with the same JSON and a different complete noun phrase of 2 to 4 words "
                "that says what sets this part apart.")
    why = (f"has {len(title.split())} words" if problem == "long" else "is an unfinished phrase")
    return (f'Your title "{title}" {why}. Reply with the same JSON, but the title must be a complete '
            f'noun phrase of 2 to 4 words, such as "User login" or "Note sharing". '
            "Rephrase it; do not cut a longer phrase short.")


def ask(server, system, blob, stats, taken, max_tokens=200, think=False, budget=None):
    """Label for `blob`. `taken`: titles of the other blobs, same module first. A rejected title
    (long, fragment, duplicate) is retried with a stricter follow-up; if it stays rejected the
    blob's structural name becomes the title, so a fragment is never kept."""
    user = blob_prompt(blob, budget)
    if taken:
        user += "\n\nTitles already taken by other parts, pick a distinct one: " + "; ".join(taken)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    taken_lc = {t.casefold() for t in taken}
    for attempt in range(1 + MAX_RETRIES):
        stats["calls"] = stats.get("calls", 0) + 1
        reply = post(server, "/v1/chat/completions", {
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "cache_prompt": True,
            "chat_template_kwargs": {"enable_thinking": think},
        }, timeout=max(TIMEOUT, max_tokens // 10))
        usage = reply.get("usage", {})
        stats["prompt"] += usage.get("prompt_tokens", 0)
        stats["gen"] += usage.get("completion_tokens", 0)
        thought = reply["choices"][0]["message"].get("reasoning_content")
        if thought:  # llama-server's usage has no reasoning count: tokenize the thought instead
            stats["reasoning"] += len(post(server, "/tokenize", {"content": thought})["tokens"])
        stats["truncated"] += reply["choices"][0].get("finish_reason") == "length"
        text = reply["choices"][0]["message"]["content"]
        label = parse_label(text)
        problem = title_problem(label["title"])
        if problem is None and label["title"].casefold() in taken_lc:
            problem = "duplicate"
        if problem is None:
            return label
        stats["rejected"][problem] += 1
        if attempt == MAX_RETRIES or (problem == "duplicate" and stats["rejected"]["duplicate"] > 1):
            break
        stats["retries"] += 1
        messages += [{"role": "assistant", "content": text},
                     {"role": "user", "content": followup(problem, label["title"], taken)}]
    stats["fallback"] = True
    name = blob["name"]
    label["title"] = f"{name} ({blob['module']})" if name.casefold() in taken_lc else name
    return label


# --- per-step mode: label every blob version step by step, resumable, timed (--per-step) ---

def gpu_temp():
    """Temperature of the first GPU in °C from nvidia-smi; None when it cannot be read."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
        return int(out.split()[0])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def cool_down(hot, cool, poll=5):
    """At `hot` °C or more, wait until the GPU is below `cool` °C. -> (seconds paused, temperature)."""
    temp, paused = gpu_temp(), 0
    if temp is not None and temp >= hot:
        print(f"label.py: GPU at {temp} °C, pausing until below {cool} °C", file=sys.stderr)
        while temp is not None and temp >= cool:
            time.sleep(poll)
            paused += poll
            temp = gpu_temp()
    return paused, temp


def step_range(spec, n):
    """`A-B` (inclusive), `A`, `A-` or `-B` -> the step positions it names among n."""
    a, dash, b = spec.partition("-")
    lo = int(a) if a else 0
    hi = int(b) if b else (n - 1 if dash else lo)
    return range(max(lo, 0), min(hi, n - 1) + 1)


def changed_versions(steps):
    """Per step, its blobs whose content key is new or differs from the same blob's in the previous
    step, one blob per key (keyless blobs, i.e. unreadable files, are left out)."""
    out, prev = [], {}
    for step in steps:
        cur, todo = {}, {}
        for b in step["blobs"]:
            key = cur[b["id"]] = key_of(step, b)
            if key and prev.get(b["id"]) != key:
                todo.setdefault(key, b)
        out.append(list(todo.values()))
        prev = cur
    return out


def read_log(path):
    """Run log -> its records in order (a line cut short by a kill is skipped)."""
    recs = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except FileNotFoundError:
        pass
    return recs


def save_cache(path, cache, base=None):
    """Merge `cache` into the cache file: under an exclusive lock on PATH.lock the file is read again, `cache`'s
    entries replace or join it, and the result is written through a temp file and renamed. A kill never leaves half
    a file, and a concurrent writer (liveserve.py's labelling worker, another label.py) loses nothing. `base`: the
    cache as this process read it (and last saved it); only the entries changed since then are written, so an entry
    another writer changed meanwhile is not put back to what this process read. base is then brought up to date.
    -> the merged cache as written."""
    if base is not None:
        changed = {k: v for k, v in cache.items() if base.get(k) != v}
        base.update(changed)
        cache = changed
    with open(path + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(path, encoding="utf-8") as f:
                disk = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            disk = {}
        disk.update(cache)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(disk, f, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    return disk


def error_text(blob, e):
    text = f"{blob['id']}: {type(e).__name__}: {e}"
    if isinstance(e, urllib.error.HTTPError):
        try:
            text += " " + e.read().decode(errors="replace")[:300]
        except Exception:
            pass
    return text


def label_step(server, step, blobs, cache, a):
    """Label the uncached `blobs` of `step` into `cache`, module by module, with the step's own parts
    as context. Each blob gets `a.attempts` tries with backoff; a server that stops answering ends
    the step. -> (model calls, [error texts])."""
    system = system_prompt(step)
    module_of = {b["id"]: b["module"] for b in step["blobs"]}
    key = lambda b: key_of(step, b)
    titles = {b["id"]: cache[key(b)]["title"] for b in step["blobs"] if key(b) in cache}
    todo = [b for b in blobs if key(b) not in cache]
    calls, errors = 0, []
    for module in dict.fromkeys(b["module"] for b in todo):
        for blob in (b for b in todo if b["module"] == module):
            blob["_edges"] = [e for e in step["edges"] if blob["id"] in (e["a"], e["b"])]
            taken = siblings(blob["id"], module, titles, module_of)[:MAX_TAKEN]
            for attempt in range(1, a.attempts + 1):
                stats = {"prompt": 0, "gen": 0, "reasoning": 0, "truncated": 0, "retries": 0,
                         "rejected": Counter(), "fallback": False, "calls": 0}
                try:
                    cache[key(blob)] = ask(server, system, blob, stats, taken, a.max_tokens,
                                           a.think, a.prompt_chars)
                    titles[blob["id"]] = cache[key(blob)]["title"]
                    break
                except Exception as e:  # retried, then the step is marked failed
                    err = e
                finally:
                    calls += stats["calls"]
                if attempt < a.attempts:
                    time.sleep(a.backoff * 2 ** (attempt - 1))
            else:
                errors.append(error_text(blob, err))
                down = isinstance(err, OSError) and not (
                    isinstance(err, urllib.error.HTTPError) and err.code != 503)
                if down:  # refused, timed out, still loading: the rest of the step would fail too
                    return calls, errors
    return calls, errors


def run_per_step(a, steps, cache):
    server = a.server.rstrip("/")
    versions = changed_versions(steps)
    last = {}
    for r in read_log(a.run_log):
        last[r["step"]] = r
    picked = step_range(a.steps, len(steps)) if a.steps else range(len(steps))
    plan, skipped = [], 0
    for i in picked:
        keys = sorted(key_of(steps[i], b) for b in versions[i])
        if not keys:
            continue
        digest = hashlib.sha1("\n".join(keys).encode()).hexdigest()[:12]
        rec = last.get(i)
        if a.retry_failed and not (rec and rec["status"] == "failed"):
            continue
        if (rec and rec["status"] == "ok" and rec.get("keys") == digest) or all(k in cache for k in keys):
            skipped += 1
            continue
        plan.append((i, digest))
    print(f"label.py: {len(plan)} steps to label, {skipped} skipped (logged ok or all cached), "
          f"log {a.run_log}", file=sys.stderr)
    if plan:
        try:
            with urllib.request.urlopen(server + "/health", timeout=5):
                pass
        except (urllib.error.URLError, OSError) as e:
            sys.exit(f"label.py: llama-server unreachable at {server} ({e})")
        os.makedirs(os.path.dirname(a.run_log) or ".", exist_ok=True)
    plan = dict(plan)
    keeper, dirty, n, failed_in_row = Keeper(a.relabel_at), False, 0, 0
    for i in range(picked.stop):  # every step from 0: the rule's state is replayed from the cache
        blobs = versions[i]
        hits = sum(key_of(steps[i], b) in cache for b in blobs)
        ask = keeper.step(i, steps[i], cache, live=i in picked)
        reused = len({key_of(steps[i], b) for b in blobs} & set(keeper.reused))
        dirty = dirty or bool(keeper.reused)
        if i not in plan or not (ask or reused):  # nothing to do, or an earlier step of this run labelled them
            keeper.settle(cache)
            continue
        n += 1
        pause, temp0 = cool_down(a.hot, a.cool) if ask else (0, None)
        t0 = time.monotonic()
        calls, errors = label_step(server, steps[i], list(ask.values()), cache, a) if ask else (0, [])
        secs = time.monotonic() - t0
        keeper.settle(cache)
        save_cache(a.cache, cache, a.base)  # the step's labels, failed or not, before its log line
        dirty = False
        rec = {"step": i, "idx": steps[i].get("idx"), "session": steps[i].get("session"), "keys": plan[i],
               "blobs": len(blobs), "asked": len(ask), "hits": hits, "reused": reused, "calls": calls,
               "secs": round(secs, 2), "pause_s": pause, "temp_start": temp0, "temp_end": gpu_temp() if ask else None,
               "status": "failed" if errors else "ok", "error": "; ".join(errors) or None,
               "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        with open(a.run_log, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"step {i} [{n}/{len(plan)}] {rec['asked']} asked, {reused} reused, {calls} calls, {secs:.1f}s, "
              f"{rec['temp_end']} °C{f', paused {pause}s' if pause else ''}: {rec['status']}"
              f"{' ' + rec['error'] if errors else ''}", file=sys.stderr)
        failed_in_row = failed_in_row + 1 if errors else 0
        if a.give_up_after and failed_in_row >= a.give_up_after:
            print(f"label.py: {failed_in_row} failed steps in a row, stopping; rerun with --retry-failed",
                  file=sys.stderr)
            break
    if dirty:  # labels kept on steps that asked nothing
        save_cache(a.cache, cache, a.base)
    if a.report:
        print(run_report(a.run_log))


def run_report(path):
    """Summary of a run log: time, per-step p50/p90/max, calls, pauses, temperatures, failures."""
    recs = read_log(path)
    if not recs:
        return f"{path}: no records"
    last = {}
    for r in recs:
        last[r["step"]] = r
    ok = [r for r in last.values() if r["status"] == "ok"]
    failed = sorted((r for r in last.values() if r["status"] != "ok"), key=lambda r: r["step"])
    timed = [r for r in recs if r["asked"]] or recs  # not the steps that only kept labels
    secs = sorted(r["secs"] for r in timed)
    pct = lambda p: secs[max(0, -(-len(secs) * p // 100) - 1)]
    slowest = max(recs, key=lambda r: r["secs"])
    work = sum(r["secs"] for r in recs)
    pauses = [r["pause_s"] for r in recs if r["pause_s"]]
    calls = sum(r["calls"] for r in recs)
    temps = [t for r in recs for t in (r["temp_start"], r["temp_end"]) if t is not None]
    out = [f"{path}: {len(recs)} step runs over {len(last)} steps, {len(ok)} ok, {len(failed)} failed",
           f"time {work + sum(pauses):.0f}s: {work:.0f}s labelling + {sum(pauses):.0f}s in {len(pauses)} "
           f"thermal pauses",
           f"per step p50 {pct(50):.1f}s, p90 {pct(90):.1f}s, max {slowest['secs']:.1f}s (step {slowest['step']})",
           f"{sum(r['asked'] for r in recs)} blobs asked, {sum(r.get('reused', 0) for r in recs)} kept their "
           f"label, {sum(r['hits'] for r in recs)} cache hits, "
           f"{calls} model calls{f', {work / calls:.2f}s per call' if calls else ''}"]
    if temps:
        out.append(f"GPU {min(temps)}-{max(temps)} °C")
    out += [f"failed step {r['step']}: {r['error']}" for r in failed]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("timeline", nargs="?")
    ap.add_argument("--server", default="http://127.0.0.1:8080")
    ap.add_argument("--cache", help="label cache file (default demo/labels[.TAG].json)")
    ap.add_argument("--model-tag", help="per-model cache: demo/labels.TAG.json")
    ap.add_argument("--report", action="store_true",
                    help="print per-blob latency, tokens, retries, rejections, fallbacks")
    ap.add_argument("--think", action="store_true", help="let the model reason before it answers")
    ap.add_argument("--max-tokens", type=int, default=200,
                    help="completion budget per call, reasoning included (default 200)")
    ap.add_argument("--per-step", action="store_true",
                    help="label every blob version step by step into the cache (the timeline is not "
                         "written), one JSONL record per step, resumable")
    ap.add_argument("--run-log", help="per-step run log (default demo/label-runs/<timeline name>.jsonl)")
    ap.add_argument("--steps", help="per-step: only steps A-B (positions, inclusive; A, A- and -B work)")
    ap.add_argument("--retry-failed", action="store_true", help="per-step: redo only steps logged failed")
    ap.add_argument("--attempts", type=int, default=3, help="per-step: tries per blob (default 3)")
    ap.add_argument("--backoff", type=float, default=2, help="per-step: first retry wait in s, doubling")
    ap.add_argument("--hot", type=int, default=85, help="per-step: pause at this GPU °C (default 85)")
    ap.add_argument("--cool", type=int, default=75, help="per-step: resume below this GPU °C (default 75)")
    ap.add_argument("--prompt-chars", type=int, default=4000,
                    help="per-step: blob prompt budget in characters (default 4000)")
    ap.add_argument("--give-up-after", type=int, default=5,
                    help="per-step: stop after this many failed steps in a row (default 5, 0 never)")
    ap.add_argument("--run-report", metavar="LOG", help="print the summary of a per-step run log and exit")
    ap.add_argument("--relabel-at", type=float, default=0.2,
                    help="keep a blob's label until the lines changed since it was made reach this share of the "
                         "blob's lines then (default 0.2; 0 asks for every new version)")
    ap.add_argument("--count", action="store_true",
                    help="print how many label requests the timeline needs from an empty cache, at --relabel-at "
                         "and at 0, and exit (no server, nothing written)")
    a = ap.parse_args()
    if a.run_report:
        print(run_report(a.run_report))
        return
    if not a.timeline:
        ap.error("timeline is required")
    if not 0 <= a.relabel_at <= 1:
        ap.error(f"--relabel-at must be between 0 and 1 (got {a.relabel_at:g})")
    server = a.server.rstrip("/")
    a.cache = a.cache or (f"demo/labels.{a.model_tag}.json" if a.model_tag else "demo/labels.json")
    stem = os.path.splitext(os.path.basename(a.timeline))[0]
    a.run_log = a.run_log or f"demo/label-runs/{stem}.jsonl"

    with open(a.timeline) as f:
        tl = json.load(f)
    steps = timeline.expand(tl)  # full state per step, whatever the schema on disk
    try:
        with open(a.cache) as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    a.base = dict(cache)  # what this run read: only what it changes is merged back (save_cache)
    stale = [k for k, v in cache.items() if title_problem(v["title"])]
    for k in stale:
        del cache[k]
    if not steps:
        return
    for step in steps:
        for b in step["blobs"]:
            head = file_head(tl.get("root", "."), b)
            if head:
                b["_head"] = head
    for step in steps:
        step["_keys"] = step_keys(step)
    if a.count:
        print(count_report(steps, a.relabel_at))
        return
    if a.per_step:
        run_per_step(a, steps, cache)
        return

    keys = {key_of(step, b) for step in steps for b in step["blobs"]} - {""}
    hits, stale_n = len(keys & cache.keys()), len(keys & set(stale))

    # The keys the rule asks for; one representative blob per key (latest step wins, with its edges).
    asks, reuse = plan_labels(steps, cache, a.relabel_at)
    todo = {}
    for step in steps:
        for b in step["blobs"]:
            key = key_of(step, b)
            if key in asks:
                b["_edges"] = [e for e in step["edges"] if b["id"] in (e["a"], e["b"])]
                todo[key] = b
    dirty = bool(todo)

    if todo:
        try:
            with urllib.request.urlopen(server + "/health", timeout=5):
                pass
        except (urllib.error.URLError, OSError) as e:
            print(f"label.py: llama-server unreachable at {server} ({e}); keeping heuristic labels",
                  file=sys.stderr)
            todo = {}
        system = system_prompt(steps[-1])

        # Siblings = the other blobs of the last step, by blob id (older versions of a blob are not
        # siblings of it). Their titles so far: cache hits first, the rest as they get labelled.
        final = steps[-1]["blobs"]
        module_of = {b["id"]: b["module"] for b in final}
        titles = {b["id"]: cache[key_of(steps[-1], b)]["title"] for b in final if key_of(steps[-1], b) in cache}
        final_keys = {key_of(steps[-1], b) for b in final}
        order = sorted(todo, key=lambda k: k not in final_keys)  # last-step blobs first
        stats = {}
        t_all = time.monotonic()
        # Module by module, one blob at a time, so every blob sees its siblings' titles.
        for module in dict.fromkeys(todo[k]["module"] for k in order):
            for key in order:
                blob = todo[key]
                if blob["module"] != module:
                    continue
                taken = siblings(blob["id"], module, titles, module_of)
                stats[key] = {"prompt": 0, "gen": 0, "reasoning": 0, "truncated": 0, "retries": 0,
                              "rejected": Counter(), "fallback": False}
                t0 = time.monotonic()
                try:
                    cache[key] = ask(server, system, blob, stats[key], taken[:MAX_TAKEN],
                                     a.max_tokens, a.think)
                    if blob["id"] in module_of:
                        titles.setdefault(blob["id"], cache[key]["title"])
                except Exception as e:  # one bad blob must not sink the rest
                    print(f"label.py: {blob['name']}: {e}", file=sys.stderr)
                stats[key]["secs"] = time.monotonic() - t0
        rejected = sum((st["rejected"] for st in stats.values()), Counter())
        summary = (f"{sum(st['retries'] for st in stats.values())} retries, {sum(rejected.values())} rejections "
                   f"({', '.join(f'{n} {r}' for r, n in sorted(rejected.items())) or 'none'}), "
                   f"{sum(st['fallback'] for st in stats.values())} structural-name fallbacks, "
                   f"{sum(st['truncated'] for st in stats.values())} truncated replies")
        if a.report:
            print(f"{'blob':32} {'secs':>6} {'prompt':>6} {'gen':>5} {'think':>5} {'retry':>5}  title")
            for key, blob in todo.items():
                st = stats[key]
                rej = "".join(f" [{r}x{n}]" for r, n in st["rejected"].items())
                print(f"{blob['name'][:32]:32} {st['secs']:6.2f} {st['prompt']:6d} {st['gen']:5d} "
                      f"{st['reasoning']:5d} {st['retries']:5d}  {cache[key]['title'] if key in cache else 'FAILED'}"
                      f"{' (fallback)' if st['fallback'] else ''}{rej}")
            print(f"wall {time.monotonic() - t_all:.1f}s, {sum(k in cache for k in todo)}/{len(todo)} ok, "
                  f"{sum(st['gen'] for st in stats.values())} completion tokens "
                  f"({sum(st['reasoning'] for st in stats.values())} reasoning), {summary}")

    reused = 0
    for key, src in reuse.items():  # kept labels whose source is labelled now
        if src in cache:
            cache[key] = dict(cache[src], reused_from=src)
            reused += 1
    if dirty or reused:
        save_cache(a.cache, cache, a.base)
    apply_labels(steps, cache)
    for step in steps:
        step.pop("_keys", None)
        for b in step["blobs"]:
            b.pop("_edges", None)
            b.pop("_head", None)
    tl["steps"] = timeline.compact(steps, tl["schema"]) if tl.get("schema", 2) >= 3 else steps  # same schema back
    with open(a.timeline, "w") as f:
        json.dump(tl, f, indent=1, ensure_ascii=False)
    print(f"label.py: {hits} cache hits, {reused} kept labels (--relabel-at {a.relabel_at:g}), "
          f"{len(todo)} new calls, {stale_n} stale titles redone, "
          f"{a.timeline} updated")


if __name__ == "__main__":
    main()
