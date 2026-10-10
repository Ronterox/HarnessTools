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

from . import anchors, paths, usage

COMPILE_MODEL = os.environ.get("AIC_COMPILE_MODEL", "sonnet")
ASK_MODEL = os.environ.get("AIC_ASK_MODEL", "opus")
MAX_EVENTS_CHARS = 60000

ROLE = """You are the aic COMPILER: a reviewer that helps a human expert keep up with a \
separate coding AI (the WORKER) that is changing this repository right now.
The human writes a SPEC in whatever form they think in: a markdown outline, pseudocode
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

COMPILE_TASK = """Compile the spec against the repository and the worker's recent activity.
Emit diagnostics of these kinds:
- mismatch: the spec says X but the worker did Y, or has not done it yet
- incoherent: the spec contradicts itself, is ambiguous, or has a hole the worker would have to guess
- model: the spec assumes something about the existing code that is not true
- unspecified: the worker changed things the spec does not mention (scope creep, side effects)
For each item set file="spec" and line=<spec line> when it is about a spec line, otherwise file=<repo-relative path> and line=<code line> (and spec_line if a spec line is related).
severity: error = will produce wrong behaviour or contradicts the spec; warning = likely problem or unclear; info = worth knowing.
Do not repeat a diagnostic that was already reported unless it is still true after new activity (then report it again — the list replaces the previous one).
An empty or nearly empty spec is fine: then only report 'unspecified' items that the human should know about.
A heading containing "[done]" marks a finished requirement: verify it still holds (report it if
the code broke it), but never report it as pending work or ask the human to remove it.
Pseudocode is intent: do not report syntax, naming or missing-detail nits in it; report only where its
logic is wrong, contradicts itself, or disagrees with the code. For box diagrams, check that every box
exists in the code and every arrow is a real call/data path (and flag real paths the diagram omits).
If the code has an obvious logic bug (off-by-one, inverted condition, wrong variable, missing await,
state not persisted...) report it even when the spec does not mention that area, as kind=mismatch
when it contradicts the spec's intent, otherwise kind=unspecified, severity=error.
summary: 1-3 sentences on the state of things."""

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


def compile_spec(root, sid):
    """Compile; if one is already running, queue exactly one more run on the
    latest spec instead of dropping this request. Returns the diagnostics, or
    None when the request was queued.

    Hand-off: a busy caller writes compile.again and tries the lock once more;
    the running compile checks compile.again only after releasing the lock and
    re-takes it to run again. Either side ends up running it, never neither."""
    sdir = paths.session_dir(root, sid)
    again = os.path.join(sdir, "compile.again")
    marker = os.path.join(sdir, "compiling")  # pid, for editors to watch
    last, queued = None, False
    while True:
        fd = _try_lock(sdir)
        if fd is None:
            if last is not None:
                return last  # another process took over the queued run
            if queued:
                return None
            paths.write_text(again, str(paths.now()))
            queued = True
            continue
        try:
            # (a compile.again left by a crashed run is consumed here: at worst one extra run)
            if os.path.exists(again):
                os.unlink(again)
            paths.write_text(marker, str(os.getpid()))
            last = _compile_once(root, sid)
        finally:
            try:
                os.unlink(marker)
            except FileNotFoundError:
                pass
            os.close(fd)  # releases the lock
        if not os.path.exists(again):
            return last


def _compile_once(root, sid):
    sdir = paths.session_dir(root, sid)
    try:
        trace = paths.read_jsonl(os.path.join(sdir, "trace.jsonl"))
        prev = paths.read_json(os.path.join(sdir, "diagnostics.json"), {}) or {}
        upto = prev.get("upto_seq", 0)
        new = [e for e in trace if e.get("seq", 0) > upto]
        first = not _state(sdir).get("compile")
        spec = paths.read_text(os.path.join(sdir, "spec.md"))
        parts = [
            COMPILE_TASK,
            "== SPEC (spec.md, with line numbers) ==\n" + numbered(spec),
            ("== ALL WORKER ACTIVITY SO FAR ==\n" if first else
             "== NEW ACTIVITY SINCE YOUR LAST COMPILE ==\n") + fmt_events(new if not first else trace),
        ]
        anchored = anchors.blocks(spec, root, sid)
        if anchored:
            parts.insert(2, "== ANCHORED CONTEXT (resolved from @anchors in the spec) ==\n" + anchored)
        out = run_claude(root, sdir, "compile", COMPILE_MODEL, "\n\n".join(parts), schema=SCHEMA,
                         add_dirs=anchors.add_dirs([spec], root), note="compile")
        res = out.get("structured_output")
        if not isinstance(res, dict):
            try:
                res = json.loads(out.get("result") or "")
            except ValueError:
                raise CompilerError("compiler returned no diagnostics")
        diag = {"compiled_at": paths.now(), "upto_seq": trace[-1]["seq"] if trace else 0,
                "summary": res.get("summary", ""), "items": res.get("items") or [],
                "cost_usd": out.get("total_cost_usd")}
        paths.write_json(os.path.join(sdir, "diagnostics.json"), diag)
        return diag
    except CompilerError as e:
        prev = paths.read_json(os.path.join(sdir, "diagnostics.json"), {}) or {}
        prev.update({"error": str(e), "compiled_at": paths.now()})
        paths.write_json(os.path.join(sdir, "diagnostics.json"), prev)
        raise


def _where(ctx):
    if not ctx:
        return ""
    w = ctx.get("where") or "?"
    if ctx.get("file"):
        w += " %s" % ctx["file"]
        if ctx.get("line"):
            w += ":%s" % ctx["line"]
    return w


def ask(root, sid, question, ctx=None, whatif=False):
    sdir = paths.session_dir(root, sid)
    trace = paths.read_jsonl(os.path.join(sdir, "trace.jsonl"))
    st = _state(sdir)
    seen_upto = st.get("ask_upto_seq", 0)
    new = [e for e in trace if e.get("seq", 0) > seen_upto]
    diag = paths.read_json(os.path.join(sdir, "diagnostics.json"), {}) or {}
    spec = paths.read_text(os.path.join(sdir, "spec.md"))

    parts = ["== CURRENT SPEC ==\n" + numbered(spec)]
    if diag.get("items"):
        parts.append("== LATEST COMPILE DIAGNOSTICS ==\n" + json.dumps(diag["items"], ensure_ascii=False)[:8000])
    parts.append("== WORKER ACTIVITY %s ==\n" % ("SO FAR" if not seen_upto else "SINCE WE LAST TALKED")
                 + fmt_events(new, budget=30000))
    if ctx:
        c = dict(ctx)
        ev = c.pop("event", None)
        parts.append("== THE HUMAN IS POINTING AT (%s) ==\n%s" % (_where(ctx), json.dumps(c, ensure_ascii=False)[:6000]))
        if ev:
            parts.append("Trace event under cursor:\n" + fmt_event(ev, hunk_lines=120))
    if whatif:
        parts.append(
            "== WHAT-IF ==\nThe human asks: at the decision above, what if the worker had done this instead:\n"
            + question + "\n\nSketch the alternative as a short indented outline tree in the spec's "
            "pseudocode style, titled '(reconstructed alternative)'. Then list the concrete consequences "
            "vs what was actually done (files that would differ, risks, what it would make easier/harder). "
            "This is a hypothetical: real alternatives are not recorded, so say what you are inferring.")
    else:
        parts.append("== QUESTION ==\n" + question)
    # Outside-project anchors only from what the human typed: the excerpt is
    # code/chat text that the worker or the compiler may have written.
    anchored = "\n\n".join(x for x in (
        anchors.blocks(question, root, sid),
        anchors.blocks((ctx or {}).get("excerpt") or "", root, sid, allow_outside=False)) if x)
    if anchored:
        parts.append("== ANCHORED CONTEXT (resolved from @anchors in the question/excerpt) ==\n" + anchored)
    out = run_claude(root, sdir, "ask", ASK_MODEL, "\n\n".join(parts), add_dirs=anchors.add_dirs([question], root),
                     note="what-if" if whatif else ("ask " + _where(ctx)).strip() if ctx else "chat")
    answer = (out.get("result") or "").strip()

    _set_state(sdir, ask_upto_seq=trace[-1]["seq"] if trace else 0)
    head = "## You%s%s" % (" (what-if)" if whatif else "", (" — " + _where(ctx)) if ctx else "")
    with open(os.path.join(sdir, "chat.md"), "a", encoding="utf-8") as f:
        f.write("%s\n%s\n\n## Compiler\n%s\n\n" % (head, question.strip(), answer))
    return answer
