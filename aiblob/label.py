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
"""
import argparse
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

import timeline  # expand() / compact(): timeline.json is schema v3 / v4 (deltas)

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


def content_key(blob):
    if blob["kind"] in ("file", "text"):  # "_head" is set by main(); key = path + head hash
        head = blob.get("_head")
        if not head:
            return ""
        return "file:" + blob["path"] + ":" + hashlib.sha1("\n".join(head).encode()).hexdigest()[:10]
    return ",".join(sorted(f["hash"] for f in blob.get("funcs", [])))


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
    """Set each blob's `label` from the cache; a blob the cache does not back loses any old label.
    Blobs are shared between steps, so run this before dropping the `_head` keys content_key reads."""
    for step in steps:
        for b in step["blobs"]:
            label = cache.get(content_key(b))  # file blobs are keyed by their _head
            if label:
                b["label"] = label
            else:
                b.pop("label", None)


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
            key = cur[b["id"]] = content_key(b)
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


def save_cache(path, cache):
    """Write the cache through a temp file and rename, so a kill never leaves half a file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


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
    titles = {b["id"]: cache[content_key(b)]["title"] for b in step["blobs"] if content_key(b) in cache}
    todo = [b for b in blobs if content_key(b) not in cache]
    calls, errors = 0, []
    for module in dict.fromkeys(b["module"] for b in todo):
        for blob in (b for b in todo if b["module"] == module):
            blob["_edges"] = [e for e in step["edges"] if blob["id"] in (e["a"], e["b"])]
            taken = siblings(blob["id"], module, titles, module_of)[:MAX_TAKEN]
            for attempt in range(1, a.attempts + 1):
                stats = {"prompt": 0, "gen": 0, "reasoning": 0, "truncated": 0, "retries": 0,
                         "rejected": Counter(), "fallback": False, "calls": 0}
                try:
                    cache[content_key(blob)] = ask(server, system, blob, stats, taken, a.max_tokens,
                                                   a.think, a.prompt_chars)
                    titles[blob["id"]] = cache[content_key(blob)]["title"]
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
        keys = sorted(content_key(b) for b in versions[i])
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
    failed_in_row = 0
    for n, (i, digest) in enumerate(plan, 1):
        blobs = versions[i]
        hits = sum(content_key(b) in cache for b in blobs)
        if hits == len(blobs):  # an earlier step of this run labelled them
            continue
        pause, temp0 = cool_down(a.hot, a.cool)
        t0 = time.monotonic()
        calls, errors = label_step(server, steps[i], blobs, cache, a)
        secs = time.monotonic() - t0
        save_cache(a.cache, cache)  # the step's labels, failed or not, before its log line
        rec = {"step": i, "idx": steps[i].get("idx"), "session": steps[i].get("session"), "keys": digest,
               "blobs": len(blobs), "asked": len(blobs) - hits, "hits": hits, "calls": calls,
               "secs": round(secs, 2), "pause_s": pause, "temp_start": temp0, "temp_end": gpu_temp(),
               "status": "failed" if errors else "ok", "error": "; ".join(errors) or None,
               "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        with open(a.run_log, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"step {i} [{n}/{len(plan)}] {rec['asked']} asked, {calls} calls, {secs:.1f}s, "
              f"{rec['temp_end']} °C{f', paused {pause}s' if pause else ''}: {rec['status']}"
              f"{' ' + rec['error'] if errors else ''}", file=sys.stderr)
        failed_in_row = failed_in_row + 1 if errors else 0
        if a.give_up_after and failed_in_row >= a.give_up_after:
            print(f"label.py: {failed_in_row} failed steps in a row, stopping; rerun with --retry-failed",
                  file=sys.stderr)
            break
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
    secs = sorted(r["secs"] for r in recs)
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
           f"{sum(r['asked'] for r in recs)} blobs asked, {sum(r['hits'] for r in recs)} cache hits, "
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
    a = ap.parse_args()
    if a.run_report:
        print(run_report(a.run_report))
        return
    if not a.timeline:
        ap.error("timeline is required")
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
    if a.per_step:
        run_per_step(a, steps, cache)
        return

    keys = {content_key(b) for step in steps for b in step["blobs"]} - {""}
    hits, stale_n = len(keys & cache.keys()), len(keys & set(stale))

    # One representative blob per uncached content key (latest step wins, with its edges).
    todo = {}
    for step in steps:
        for b in step["blobs"]:
            key = content_key(b)
            if key and key not in cache:
                b["_edges"] = [e for e in step["edges"] if b["id"] in (e["a"], e["b"])]
                todo[key] = b

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
        titles = {b["id"]: cache[content_key(b)]["title"] for b in final if content_key(b) in cache}
        final_keys = {content_key(b) for b in final}
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
        with open(a.cache, "w") as f:
            json.dump(cache, f, indent=1, ensure_ascii=False)
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

    apply_labels(steps, cache)
    for step in steps:
        for b in step["blobs"]:
            b.pop("_edges", None)
            b.pop("_head", None)
    tl["steps"] = timeline.compact(steps, tl["schema"]) if tl.get("schema", 2) >= 3 else steps  # same schema back
    with open(a.timeline, "w") as f:
        json.dump(tl, f, indent=1, ensure_ascii=False)
    print(f"label.py: {hits} cache hits, {len(todo)} new calls, {stale_n} stale titles redone, "
          f"{a.timeline} updated")


if __name__ == "__main__":
    main()
