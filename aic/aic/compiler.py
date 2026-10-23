"""The compiler Claude: `aic compile` (Sonnet, structured diagnostics) and
`aic ask` (Opus, free-form chat / ask-at-cursor / what-if).

Both are persistent headless Claude Code sessions resumed by id, restricted to
read-only tools, run with project settings disabled so the worker's hooks never
fire for them.
"""
import fcntl
import json
import os
import subprocess
import uuid

from . import anchors, compiler_prompts, docs, paths, prompts, usage

COMPILE_MODEL = os.environ.get("AIC_COMPILE_MODEL", "sonnet")
ASK_MODEL = os.environ.get("AIC_ASK_MODEL", "opus")
MAX_EVENTS_CHARS = 60000

ROLE = """You are the aic COMPILER: a reviewer that helps a human expert keep up with a \
separate coding AI (the WORKER) that is changing this repository right now.
The human writes DOCS (specs, notes, plans) in whatever form they think in: a markdown outline, pseudocode
snippets, or ASCII box diagrams (boxes = components/modules/states, arrows = calls, data or
control flow, labels on arrows = what flows). Read all of them as intent, not as code to run.
`@anchors` (`@file`, `@file:12`, `@file:10-20`, `@file:symbol`, `@dir/`, `@diff`, `@turn:N`) point
at real things; their resolved content is included below when present.
You compile ideas, not code: you tell the human whether their spec is coherent, whether it \
matches what the worker actually did, and whether their mental model of the code is right.
Rules:
- You NEVER edit files and never talk to the worker. You only advise the human.
- Verify claims against the real code with Read/Grep/Glob before asserting them.
- The worker's thinking is not visible; a tool call's "why" is the text it wrote before the call.
- Be terse and concrete. Point at exact lines."""

# The task instructions are compiler templates: prompts/compiler/*.md (compiler_prompts.py).

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "items": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["mismatch", "incoherent", "model", "unspecified"]},
                "severity": {"type": "string", "enum": ["error", "warning", "info"]},
                "file": {"type": "string"},
                "line": {"type": "integer"},
                "spec_line": {"type": ["integer", "null"]},
                "message": {"type": "string"},
            },
            "required": ["kind", "severity", "file", "line", "message"],
        }},
    },
    "required": ["summary", "items"],
}


class CompilerError(Exception):
    pass


# -- formatting ---------------------------------------------------------------
def numbered(text):
    return "\n".join("%4d| %s" % (i + 1, l) for i, l in enumerate(text.splitlines())) or "(empty)"


def fmt_event(ev, hunk_lines=60):
    k = ev.get("kind")
    if k == "prompt":
        return "\n[turn %s] USER PROMPT TO WORKER:\n%s" % (ev.get("turn"), ev.get("text", ""))
    if k == "text":
        return "WORKER SAID: %s" % ev.get("text", "")
    if k == "submit":
        return "HUMAN SUBMITTED TO WORKER:\n%s" % ev.get("text", "")
    if k in ("hold", "held", "release"):
        return "(%s)" % k
    if k == "compact":
        return "(worker context compacted: %s -> %s tokens)" % (ev.get("pre"), ev.get("post"))
    if k in ("tool", "human"):
        head = ("WORKER TOOL: %s" % ev.get("title")) if k == "tool" else ("HUMAN EDITED %s" % ev.get("file"))
        parts = [head + (" [ERROR]" if ev.get("error") else "")]
        if ev.get("why"):
            parts.append("  why: " + ev["why"].replace("\n", "\n       "))
        n = 0
        for h in ev.get("hunks") or []:
            parts.append("  @@ line %s" % h.get("start"))
            for l in h.get("lines") or []:
                if n >= hunk_lines:
                    parts.append("  …(truncated)")
                    break
                parts.append("  " + l)
                n += 1
        return "\n".join(parts)
    return ""


def fmt_events(evs, budget=MAX_EVENTS_CHARS):
    chunks, size = [], 0
    for ev in reversed(evs):  # keep the most recent when over budget
        s = fmt_event(ev)
        if not s:
            continue
        if size + len(s) > budget:
            chunks.append("…(%d earlier events omitted)" % (len(evs) - len(chunks)))
            break
        chunks.append(s)
        size += len(s)
    return "\n".join(reversed(chunks)) or "(no worker activity yet)"


# -- claude invocation --------------------------------------------------------
def _state(sdir):
    return paths.read_json(os.path.join(sdir, "state.json"), {}) or {}


def _set_state(sdir, **changes):
    paths.update_json(os.path.join(sdir, "state.json"), **changes)


def run_claude(root, sdir, key, model, prompt, schema=None, timeout=600, add_dirs=(), note=None):
    """Run one turn of the persistent compiler session `key`; returns the result JSON.
    Turns of one session are serialized (a second ask waits for the first)."""
    with paths.locked(os.path.join(sdir, "claude-" + key)):
        return _run_claude(root, sdir, key, model, prompt, schema, timeout, add_dirs, note or key)


def _run_claude(root, sdir, key, model, prompt, schema, timeout, add_dirs, note):
    st = _state(sdir)
    sid = st.get(key)
    seed = None if sid else st.get(key + "_seed")
    if seed:  # first call after a compact: carry the handoff summary over
        prompt = "== HANDOFF FROM YOUR PREVIOUS (COMPACTED) SESSION ==\n%s\n\n%s" % (seed, prompt)
    cmd = ["claude", "-p", "--model", model, "--output-format", "json",
           "--setting-sources", "", "--permission-mode", "default", "--strict-mcp-config",
           "--tools", "Read", "Grep", "Glob",
           "--allowedTools", "Read", "Grep", "Glob",
           "--disallowedTools", "Edit", "Write", "Bash", "NotebookEdit", "WebFetch", "WebSearch",
           "--append-system-prompt", ROLE]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    for d in add_dirs:
        cmd += ["--add-dir", d]
    if sid:
        cmd += ["--resume", sid]
    else:
        sid = str(uuid.uuid4())
        cmd += ["--session-id", sid]
        paths.add_compiler_id(root, sid)
    env = dict(os.environ, AIC_ROLE="compiler")
    try:
        p = subprocess.run(cmd, input=prompt, cwd=root, env=env, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CompilerError("compiler timed out after %ds" % timeout)
    except FileNotFoundError:
        raise CompilerError("`claude` not found on PATH")
    try:
        out = json.loads(p.stdout)
    except ValueError:
        raise CompilerError("compiler failed: " + (p.stderr or p.stdout).strip()[:300])
    if out.get("is_error"):
        raise CompilerError("compiler error: %s" % str(out.get("result"))[:300])
    changes = {key: out.get("session_id") or sid}
    if seed:
        changes[key + "_seed"] = None
    _set_state(sdir, **changes)
    out.setdefault("session_id", sid)
    try:
        usage.record_call(root, sdir, key, out, note)
    except Exception:  # usage display must never break a compile/ask
        pass
    return out


HANDOFF = """We are about to start a fresh session to free context. Write the handoff for your
successor, max 300 words, plain markdown: the human's goals and preferences as you understood
them, decisions taken, open issues and suspected bugs (with file:line), and what to watch next.
No preamble."""


def compact(root, sid, key, reset=False):
    """Summarize-and-restart (or plain reset) compiler session `key`. For the
    worker, aic types Claude Code's own `/compact` into its pane as the human
    (same safety checks as a submit); a worker can't be reset from aic."""
    if key == "worker":
        if reset:
            raise CompilerError("the worker can't be reset from aic; use /clear in its pane")
        from . import worker
        try:
            pane = worker.deliver(root, sid, "/compact")
        except worker.WorkerError as e:
            raise CompilerError(str(e))
        return "worker: /compact sent to %s" % pane
    if key not in ("compile", "ask"):
        raise CompilerError("compact what? compile, ask or worker")
    sdir = paths.session_dir(root, sid)
    with paths.locked(os.path.join(sdir, "claude-" + key)):
        old = _state(sdir).get(key)
        if not old:
            return "%s: no session yet" % key
        summary = None
        if not reset:
            model = COMPILE_MODEL if key == "compile" else ASK_MODEL
            out = _run_claude(root, sdir, key, model, HANDOFF, None, 300, (), "handoff")
            summary = (out.get("result") or "").strip() or None
        changes = {key: None, key + "_seed": summary}
        if key == "ask":
            changes["ask_upto_seq"] = 0  # the new session sees the activity again
        _set_state(sdir, **changes)
        usage.restart(sdir, key, old)
    return "%s: %s; the next %s starts a fresh session" % (
        key, "compacted (handoff summary carried over)" if summary else "reset", key)


# -- commands -----------------------------------------------------------------
def _try_lock(sdir):
    """Kernel lock on compiling.lock: released automatically if we die, so a
    crash or reboot can never leave a stale "compiling" state behind."""
    os.makedirs(sdir, exist_ok=True)
    fd = os.open(os.path.join(sdir, "compiling.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None


def compiling(sdir):
    fd = _try_lock(sdir)
    if fd is None:
        return True
    os.close(fd)
    return False


def _queue(sdir, add=None):
    """compile.again = the docs waiting for one more compile (JSON list).
    add=name: queue it. add=None: take (and clear) the whole queue."""
    again = os.path.join(sdir, "compile.again")
    with paths.locked(again):
        try:
            q = json.loads(paths.read_text(again) or "[]")
        except ValueError:
            q = []
        # an old-style marker (a timestamp) or junk: nothing to run
        q = [x for x in q if isinstance(x, str) and docs.NAME.match(x)] if isinstance(q, list) else []
        if add is not None:
            if add not in q:
                q.append(add)
            paths.write_text(again, json.dumps(q))
            return q
        if os.path.exists(again):
            os.unlink(again)
        return q


def compile_spec(root, sid, doc=docs.DEFAULT):
    """Compile doc `doc`; if a compile is already running, queue one more run
    of this doc instead of dropping the request. Returns this doc's
    diagnostics, or None when the request was queued (another process runs it).

    Hand-off: a busy caller adds its doc to compile.again and tries the lock
    once more; the holder takes the whole queue only after releasing the lock
    and re-takes it to run what was queued. Either side ends up running it,
    never neither. Other docs' failures are recorded in their own files."""
    docs.check_name(doc)
    sdir = paths.session_dir(root, sid)
    again = os.path.join(sdir, "compile.again")
    marker = os.path.join(sdir, "compiling")  # pid, for editors to watch
    last, queued, err = None, False, None
    while True:
        fd = _try_lock(sdir)
        if fd is None:
            if last is not None or err is not None:
                break  # another process took over the queued runs
            if queued:
                return None
            _queue(sdir, add=doc)
            queued = True
            continue
        try:
            # (a queue left by a crashed run is consumed here: at worst extra runs)
            todo = _queue(sdir)
            if last is None and err is None and doc not in todo:
                todo.insert(0, doc)
            paths.write_text(marker, str(os.getpid()))
            for d in todo:
                if d != doc and not os.path.isfile(docs.path(root, d)):
                    continue  # queued, then renamed or deleted
                try:
                    res = _compile_once(root, sid, d)
                except Exception as e:  # one doc's failure never costs the others their run
                    _record_error(sdir, d, e)
                    if d == doc:
                        last, err = None, e
                    continue
                if d == doc:
                    last, err = res, None
        finally:
            try:
                os.unlink(marker)
            except FileNotFoundError:
                pass
            os.close(fd)  # releases the lock
        if not os.path.exists(again):
            break
    if err is not None:
        raise err
    return last


def _record_error(sdir, doc, e):
    try:
        prev = paths.read_json(docs.diag_path(sdir, doc), {}) or {}
        prev.update({"doc": doc, "error": str(e) or e.__class__.__name__, "compiled_at": paths.now()})
        paths.write_json(docs.diag_path(sdir, doc), prev)
    except OSError:
        pass


def _template(root, name, task):
    try:
        return compiler_prompts.find(root, name, task)
    except prompts.PromptError as e:
        raise CompilerError(str(e))


def _template_info(t):
    shadows = [s for s in t["shadows"] if s != "built-in"]
    return dict({"name": t["name"], "source": t["source"]}, **({"shadows": shadows} if shadows else {}))


def compile_prompt(root, sid, doc=docs.DEFAULT):
    """-> dict(prompt, model, add_dirs, note, template, seq) for compiling `doc`
    (its marker may name a compile template: `compile=NAME`)."""
    sdir = paths.session_dir(root, sid)
    trace = paths.read_jsonl(os.path.join(sdir, "trace.jsonl"))
    st = _state(sdir)
    # one compiler session for all docs: activity is sent once, whichever doc compiles
    upto = st.get("compile_upto_seq", 0)
    new = [e for e in trace if e.get("seq", 0) > upto]
    first = not st.get("compile")
    text = docs.read(root, doc)
    t = _template(root, docs.marker(text).get("compile_template") or "compile", "compile")
    prev = paths.read_json(docs.diag_path(sdir, doc), {}) or {}
    anchored = anchors.blocks(text, root, sid)
    own, own_dirs = compiler_prompts.own_anchors(t, root, sid)
    values = {
        "doc.name": doc,
        "doc": "== THIS COMPILE IS FOR DOC `%s` (.aic/docs/%s.md, with line numbers) ==\n%s" % (doc, doc, numbered(text)),
        "previous_items": "== YOUR PREVIOUS ITEMS FOR DOC `%s` (your new list replaces exactly these) ==\n%s" % (
            doc, json.dumps(prev.get("items"), ensure_ascii=False)[:8000] if prev.get("items") else "(none)"),
        "anchors": "\n\n".join(x for x in (
            ("== ANCHORED CONTEXT (resolved from @anchors in the doc) ==\n" + anchored) if anchored else "", own) if x),
        "activity": ("== ALL WORKER ACTIVITY SO FAR ==\n" if first else
                     "== NEW ACTIVITY SINCE YOUR LAST COMPILE (of any doc) ==\n") + fmt_events(new if not first else trace),
    }
    return {"prompt": compiler_prompts.render(t, values), "model": compiler_prompts.model(t, COMPILE_MODEL),
            "add_dirs": anchors.add_dirs([text], root) + own_dirs,
            "note": "compile " + doc + compiler_prompts.tag(t, "compile"), "template": t,
            "seq": trace[-1]["seq"] if trace else 0}


def structured(out):
    """A run's structured output (else its result as JSON): a dict, or None."""
    res = out.get("structured_output")
    if not isinstance(res, dict):
        try:
            res = json.loads(out.get("result") or "")
        except ValueError:
            return None
    return res if isinstance(res, dict) else None


def _compile_once(root, sid, doc=docs.DEFAULT):
    sdir = paths.session_dir(root, sid)
    c = compile_prompt(root, sid, doc)
    out = run_claude(root, sdir, "compile", c["model"], c["prompt"], schema=SCHEMA,
                     add_dirs=c["add_dirs"], note=c["note"])
    res = structured(out)
    if res is None:
        raise CompilerError("compiler returned no diagnostics")
    diag = {"doc": doc, "compiled_at": paths.now(), "upto_seq": c["seq"],
            "summary": res.get("summary", ""), "items": res.get("items") or [],
            "template": _template_info(c["template"]),
            "cost_usd": out.get("total_cost_usd")}
    paths.write_json(docs.diag_path(sdir, doc), diag)
    _set_state(sdir, compile_upto_seq=c["seq"])
    return diag


def _where(ctx):
    if not ctx:
        return ""
    w = ctx.get("where") or "?"
    if ctx.get("file"):
        w += " %s" % ctx["file"]
        if ctx.get("line"):
            w += ":%s" % ctx["line"]
    return w


def ask_prompt(root, sid, question, ctx=None, whatif=False, doc=docs.DEFAULT, template=None):
    """-> dict(prompt, model, add_dirs, note, template, seq) for a question
    (template: a `task: ask` compiler template; with whatif, a `task: whatif` one)."""
    sdir = paths.session_dir(root, sid)
    trace = paths.read_jsonl(os.path.join(sdir, "trace.jsonl"))
    seen_upto = _state(sdir).get("ask_upto_seq", 0)
    new = [e for e in trace if e.get("seq", 0) > seen_upto]
    task = "whatif" if whatif else "ask"
    t = _template(root, template or task, task)
    diag = paths.read_json(docs.diag_path(sdir, doc), {}) or {}
    spec = docs.read(root, doc)
    pointing = []
    if ctx:
        c = dict(ctx)
        ev = c.pop("event", None)
        pointing.append("== THE HUMAN IS POINTING AT (%s) ==\n%s" % (_where(ctx), json.dumps(c, ensure_ascii=False)[:6000]))
        if ev:
            pointing.append("Trace event under cursor:\n" + fmt_event(ev, hunk_lines=120))
    # Outside-project anchors only from what the human typed: the excerpt is
    # code/chat text that the worker or the compiler may have written.
    anchored = "\n\n".join(x for x in (
        anchors.blocks(question, root, sid),
        anchors.blocks((ctx or {}).get("excerpt") or "", root, sid, allow_outside=False)) if x)
    own, own_dirs = compiler_prompts.own_anchors(t, root, sid)
    values = {
        "doc.name": doc,
        "doc": ("== THE HUMAN'S CURRENT DOC `%s` (.aic/docs/%s.md) ==\n%s" % (doc, doc, numbered(spec))
                if os.path.isfile(docs.path(root, doc)) else "== THE HUMAN'S CURRENT DOC ==\n(none yet)"),
        "diagnostics": ("== LATEST COMPILE DIAGNOSTICS OF THAT DOC ==\n"
                        + json.dumps(diag["items"], ensure_ascii=False)[:8000]) if diag.get("items") else "",
        "activity": "== WORKER ACTIVITY %s ==\n" % ("SO FAR" if not seen_upto else "SINCE WE LAST TALKED")
                    + fmt_events(new, budget=30000),
        "pointing": "\n\n".join(pointing),
        "question": question,
        "anchors": "\n\n".join(x for x in (
            ("== ANCHORED CONTEXT (resolved from @anchors in the question/excerpt) ==\n" + anchored)
            if anchored else "", own) if x),
    }
    note = "what-if" if whatif else ("ask " + _where(ctx)).strip() if ctx else "chat"
    return {"prompt": compiler_prompts.render(t, values), "model": compiler_prompts.model(t, ASK_MODEL),
            "add_dirs": anchors.add_dirs([question], root) + own_dirs, "note": note + compiler_prompts.tag(t, task),
            "template": t, "seq": trace[-1]["seq"] if trace else 0}


def ask(root, sid, question, ctx=None, whatif=False, doc=docs.DEFAULT, template=None):
    sdir = paths.session_dir(root, sid)
    a = ask_prompt(root, sid, question, ctx, whatif, doc, template)
    out = run_claude(root, sdir, "ask", a["model"], a["prompt"], add_dirs=a["add_dirs"], note=a["note"])
    answer = (out.get("result") or "").strip()

    _set_state(sdir, ask_upto_seq=a["seq"])
    head = "## You%s%s%s" % (" (what-if)" if whatif else "", (" — " + _where(ctx)) if ctx else "",
                             compiler_prompts.tag(a["template"], "whatif" if whatif else "ask"))
    with open(os.path.join(sdir, "chat.md"), "a", encoding="utf-8") as f:
        f.write("%s\n%s\n\n## Compiler\n%s\n\n" % (head, question.strip(), answer))
    return answer
